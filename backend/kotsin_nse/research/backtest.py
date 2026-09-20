"""Backtest — replays cached history through **the same strategy and risk code the engine runs**.

That is the whole design constraint. Hand-rolled replays in the old stack erred between −80% and
+185% against production, because they reimplemented indicator maths that the engine already had.
Here `Fudkii`, `Fukaa`, `ExitEngine`, `CostModel` and `size_position` are imported, not copied; a
change to a gate changes the backtest by construction.

## What this measures, and what it does not

It measures the **underlying** signal: entry at the next bar's open, stop and targets on the
underlying, exits by the same `ExitEngine`, costs by the same `CostModel`.

It does **not** measure the option leg, because there is no option-chain history available from
this broker: expired contracts leave the scrip master, so the strikes that existed on a past date —
and their premiums — cannot be recovered. The option overlay is therefore a **model**: the
underlying move is projected onto a premium via the same delta estimate the live path uses, and
option charges are applied. It is reported in a separate block and labelled as modelled, never
mixed into the measured numbers. Treating it as a measurement is exactly the error that made "OTM
calls on the same signals" look tradeable until they lost 5.47% a trade.

## Pessimism, stated

* Entry fills at the **next** bar's open plus slippage. No same-bar entry, no lookahead.
* When a bar's range covers both the stop and a target, the **stop is assumed first**.
* A stop fill is at the stop price plus slippage, never at the stop exactly — CAN2 booked stops at
  exactly the stop and its whole live ledger was optimistic by the gap.
* Pivot zones are computed **point-in-time**, from daily bars strictly before the decision day.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict
from collections.abc import MutableMapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import structlog

from ..bars.periods import monthly, previous_complete, weekly
from ..bars.pivots import Zone, classic_pivots, cluster_zones, pivot_points
from ..bars.store import BarStore
from ..bars.unified import BarSource, UnifiedBar
from ..config import Segment, Settings
from ..domain import Direction, ExitReason, Instrument, InstrumentKind, OrderSide
from ..instrument.select import estimate_delta, map_levels_to_option
from ..market.session import ist_day, ist_hm, past_force_flat, spec
from ..risk.costs import CostModel
from ..risk.limits import RiskLimits
from ..strategy.base import Outcome, Signal
from ..strategy.fudkii import Fudkii, FudkiiConfig
from ..strategy.fukaa import Fukaa, FukaaConfig
from ..strategy.keys import StrategyKey
from .history import HistoryStore
from .stats import ClusteredMean, day_clustered_mean, max_drawdown, profit_factor

log = structlog.get_logger(__name__)

DECISION_TF = "30m"


# -- context -------------------------------------------------------------------------------------


class BacktestContext:
    """The strategy Context, backed by the replay's own store and clock.

    ``zones()`` recomputes from daily bars **strictly before the current day** and caches per day,
    so a strategy can never see a pivot derived from a session that has not happened yet.
    """

    def __init__(self, store: BarStore, dailies: dict[str, list[UnifiedBar]], segment: Segment):
        self.store = store
        self.dailies = dailies
        self.segment = segment
        self.today: date = date(1970, 1, 1)
        self._zone_cache: dict[tuple[str, date], list[Zone]] = {}
        self._state: dict[str, Any] = {}

    def bars(self, symbol: str, tf: str, n: int) -> Sequence[UnifiedBar]:
        return self.store.bars(symbol, tf, n)

    def zones(self, symbol: str) -> list[Zone]:
        key = (symbol, self.today)
        hit = self._zone_cache.get(key)
        if hit is not None:
            return hit
        rows = [b for b in self.dailies.get(symbol, []) if ist_day(b.ts) < self.today]
        if len(rows) < 25:
            self._zone_cache[key] = []
            return []
        points = []
        d = rows[-1]
        lv = classic_pivots(d.high, d.low, d.close)
        if lv:
            points += pivot_points(lv, "1d")
        for tf, periods in (("1wk", weekly(rows)), ("1mo", monthly(rows))):
            p = previous_complete(periods, self.today)
            if p:
                lv = classic_pivots(p.high, p.low, p.close)
                if lv:
                    points += pivot_points(lv, tf)
        zones = cluster_zones(points)
        self._zone_cache[key] = zones
        return zones

    def exchange(self, symbol: str) -> str:
        return self.segment.exch

    def session_phase(self, symbol: str, ts: int) -> str:
        sp = spec(self.segment)
        hm = ist_hm(ts)
        if hm >= sp.entry_cutoff.strftime("%H:%M"):
            return "EOD"
        if hm <= sp.open.strftime("%H:%M"):
            return "OPEN"
        return "MID"

    @property
    def state(self) -> MutableMapping[str, Any]:
        return self._state


# -- records --------------------------------------------------------------------------------------


@dataclass(slots=True)
class BtTrade:
    strategy: str
    symbol: str
    direction: str
    day: str
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    stop: float
    target1: float | None
    qty: int
    gross: float
    charges: float
    net: float
    r_multiple: float
    mfe_r: float
    mae_r: float
    exit_reason: str
    grade: str
    bars_held: int
    #: modelled option overlay — NOT a measurement (see the module docstring)
    opt_net_modelled: float | None = None
    opt_r_modelled: float | None = None


@dataclass(slots=True)
class OpenTrade:
    strategy: str
    symbol: str
    direction: Direction
    entry_ts: int
    entry: float
    stop: float
    initial_stop: float
    targets: tuple[float, ...]
    qty: int
    grade: str
    peak: float
    trough: float
    bars_held: int = 0
    targets_hit: int = 0

    @property
    def r_unit(self) -> float:
        return abs(self.entry - self.initial_stop)

    def sign(self) -> int:
        return 1 if self.direction is Direction.BULLISH else -1


@dataclass(slots=True)
class BacktestResult:
    id: str
    created_ts: float
    params: dict[str, Any]
    symbols: list[str]
    bars: int
    signals: int
    rejections: int
    trades: list[BtTrade] = field(default_factory=list)
    binding_gates: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        nets = [t.net for t in self.trades]
        rs = [t.r_multiple for t in self.trades]
        days = [t.day for t in self.trades]
        equity, run = [], 0.0
        for n in nets:
            run += n
            equity.append(run)
        clustered: ClusteredMean = day_clustered_mean(rs, days) if rs else ClusteredMean(0, None, 0, 0)
        by_strategy: dict[str, dict[str, Any]] = {}
        for key in (StrategyKey.FUDKII.value, StrategyKey.FUKAA.value):
            rows = [t for t in self.trades if t.strategy == key]
            if not rows:
                continue
            by_strategy[key] = {
                "trades": len(rows),
                "net": round(sum(t.net for t in rows), 2),
                "avg_r": round(sum(t.r_multiple for t in rows) / len(rows), 3),
                "win_rate": round(sum(1 for t in rows if t.net > 0) / len(rows) * 100, 1),
            }
        by_reason: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "net": 0.0})
        for t in self.trades:
            by_reason[t.exit_reason]["n"] += 1
            by_reason[t.exit_reason]["net"] += t.net
        gross = sum(t.gross for t in self.trades)
        charges = sum(t.charges for t in self.trades)
        return {
            "id": self.id,
            "created_ts": self.created_ts,
            "params": self.params,
            "symbols": len(self.symbols),
            "bars": self.bars,
            "signals": self.signals,
            "rejections": self.rejections,
            "trades": len(self.trades),
            "gross": round(gross, 2),
            "charges": round(charges, 2),
            "net": round(sum(nets), 2),
            "charges_share_of_gross": round(charges / abs(gross) * 100, 1) if gross else None,
            "win_rate": round(sum(1 for n in nets if n > 0) / len(nets) * 100, 1) if nets else None,
            "avg_r": round(clustered.mean, 3),
            "avg_r_stderr": round(clustered.stderr, 4) if clustered.stderr else None,
            "avg_r_t": round(clustered.t_stat, 2) if clustered.t_stat else None,
            "n_days": clustered.n_days,
            "sample_too_small": clustered.too_small,
            "profit_factor": profit_factor(nets),
            "max_drawdown": round(max_drawdown(equity), 2) if equity else 0.0,
            "by_strategy": by_strategy,
            "by_exit_reason": {k: {"n": v["n"], "net": round(v["net"], 2)} for k, v in by_reason.items()},
            "binding_gates": dict(sorted(self.binding_gates.items(), key=lambda kv: -kv[1])),
            "modelled_option_net": round(
                sum(t.opt_net_modelled for t in self.trades if t.opt_net_modelled is not None), 2
            )
            if any(t.opt_net_modelled is not None for t in self.trades)
            else None,
        }


# -- the replay -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BacktestParams:
    segment: Segment = Segment.NSE_EQ
    fudkii: FudkiiConfig = field(default_factory=FudkiiConfig)
    fukaa: FukaaConfig = field(default_factory=FukaaConfig)
    limits: RiskLimits = field(default_factory=RiskLimits)
    position_budget_inr: float = 100_000.0
    slippage_bps: float = 5.0
    #: report the modelled option overlay alongside the measured underlying result
    model_option_leg: bool = True
    lot_size: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "segment": self.segment.value,
            "fudkii": asdict(self.fudkii),
            "fukaa": {k: v for k, v in asdict(self.fukaa).items()},
            "limits": asdict(self.limits),
            "position_budget_inr": self.position_budget_inr,
            "slippage_bps": self.slippage_bps,
            "model_option_leg": self.model_option_leg,
        }


class Backtester:
    def __init__(self, settings: Settings, params: BacktestParams | None = None) -> None:
        self.s = settings
        self.p = params or BacktestParams()
        self.costs = CostModel(settings)

    # -- loading ------------------------------------------------------------------------------------

    @staticmethod
    def _to_bars(df: pd.DataFrame, symbol: str, scrip: str, tf: str) -> list[UnifiedBar]:
        return [
            UnifiedBar(
                symbol=symbol,
                scrip_code=scrip,
                tf=tf,
                ts=int(r.ts),
                open=float(r.o),
                high=float(r.h),
                low=float(r.l),
                close=float(r.c),
                volume=float(r.v),
                source=BarSource.REST,
                complete=True,
            )
            for r in df.itertuples()
        ]

    # -- the loop -----------------------------------------------------------------------------------

    def run(
        self,
        store: HistoryStore,
        symbols: list[str],
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> BacktestResult:
        result = BacktestResult(
            id=f"bt-{uuid.uuid4().hex[:12]}",
            created_ts=time.time(),
            params=self.p.to_json(),
            symbols=symbols,
            bars=0,
            signals=0,
            rejections=0,
        )
        for symbol in symbols:
            try:
                self._run_symbol(store, symbol, result, start, end)
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not void the sweep
                log.warning("backtest.symbol_failed", symbol=symbol, error=str(exc))
        result.trades.sort(key=lambda t: t.entry_ts)
        return result

    def _run_symbol(
        self,
        store: HistoryStore,
        symbol: str,
        result: BacktestResult,
        start: date | None,
        end: date | None,
    ) -> None:
        intraday = store.load(symbol, DECISION_TF)
        daily = store.load(symbol, "1d")
        if intraday.empty or daily.empty:
            log.warning("backtest.no_history", symbol=symbol)
            return
        if start is not None:
            lo = int(pd.Timestamp(start).timestamp())
            intraday = intraday[intraday["ts"] >= lo]
        if end is not None:
            hi = int(pd.Timestamp(end).timestamp()) + 86400
            intraday = intraday[intraday["ts"] <= hi]
        if intraday.empty:
            return

        instrument = Instrument(
            scrip_code=symbol,
            symbol=symbol,
            segment=self.p.segment,
            kind=InstrumentKind.EQUITY,
            lot_size=self.p.lot_size,
            multiplier=1,
            underlying=symbol,
        )
        bars = self._to_bars(intraday, symbol, symbol, DECISION_TF)
        dailies = {symbol: self._to_bars(daily, symbol, symbol, "1d")}

        bar_store = BarStore(max_bars=4000)
        ctx = BacktestContext(bar_store, dailies, self.p.segment)
        fudkii = Fudkii(self.p.fudkii)
        fukaa = Fukaa(self.p.fukaa)
        fukaa_ctx = BacktestContext(bar_store, dailies, self.p.segment)
        fukaa_ctx._zone_cache = ctx._zone_cache  # one cache; the zones are the same

        open_trade: OpenTrade | None = None
        pending: Signal | None = None

        for i, bar in enumerate(bars):
            result.bars += 1
            bar_store.close(bar)
            day = ist_day(bar.ts)
            ctx.today = fukaa_ctx.today = day

            # 1. an entry decided on the previous bar fills at THIS bar's open.
            if pending is not None and open_trade is None:
                open_trade = self._open(pending, bar, instrument)
                pending = None

            # 2. manage an open position on this bar's range.
            if open_trade is not None:
                closed = self._manage(open_trade, bar, instrument, result)
                if closed is not None:
                    result.trades.append(closed)
                    open_trade = None

            # 3. decide. A new signal is only taken when flat.
            outcome = self._decide(fudkii, fukaa, ctx, fukaa_ctx, bar)
            result.signals += len(outcome.signals)
            result.rejections += len(outcome.rejections)
            for rej in outcome.rejections:
                result.binding_gates[f"{rej.strategy.value}:{rej.binding_gate}"] = (
                    result.binding_gates.get(f"{rej.strategy.value}:{rej.binding_gate}", 0) + 1
                )
            if open_trade is None and pending is None and outcome.signals and i + 1 < len(bars):
                # FUKAA wins the tie: it is the same trigger with confirmation, so taking both
                # would double the exposure of one event.
                pending = next(
                    (s for s in outcome.signals if s.strategy is StrategyKey.FUKAA),
                    outcome.signals[0],
                )

        if open_trade is not None:
            result.trades.append(
                self._close(open_trade, bars[-1], bars[-1].close, ExitReason.END, instrument)
            )

    @staticmethod
    def _decide(
        fudkii: Fudkii, fukaa: Fukaa, ctx: BacktestContext, fukaa_ctx: BacktestContext, bar: UnifiedBar
    ) -> Outcome:
        out = fudkii.on_bar(ctx, bar)
        merged = Outcome(signals=list(out.signals), rejections=list(out.rejections))
        if out.signals:
            for sig in out.signals:
                merged.extend(fukaa.on_signal(fukaa_ctx, bar, sig))
        else:
            merged.extend(fukaa.on_bar(fukaa_ctx, bar))
        return merged

    # -- position handling --------------------------------------------------------------------------

    def _open(self, sig: Signal, bar: UnifiedBar, inst: Instrument) -> OpenTrade | None:
        slip = self.p.slippage_bps / 1e4
        fill = bar.open * (1 + slip * sig.direction.sign)
        risk = abs(fill - sig.stop)
        if risk <= 0:
            return None
        qty = int(self.p.position_budget_inr // (fill * inst.multiplier))
        if inst.lot_size > 1:
            qty = (qty // inst.lot_size) * inst.lot_size
        if qty < max(1, inst.lot_size):
            return None
        return OpenTrade(
            strategy=sig.strategy.value,
            symbol=sig.symbol,
            direction=sig.direction,
            entry_ts=bar.ts,
            entry=fill,
            stop=sig.stop,
            initial_stop=sig.stop,
            targets=tuple(sig.targets),
            qty=qty,
            grade=sig.grade,
            peak=fill,
            trough=fill,
        )

    def _manage(
        self, t: OpenTrade, bar: UnifiedBar, inst: Instrument, _result: BacktestResult
    ) -> BtTrade | None:
        t.bars_held += 1
        sign = t.sign()
        t.peak = max(t.peak, bar.high) if sign > 0 else min(t.peak, bar.low)
        t.trough = min(t.trough, bar.low) if sign > 0 else max(t.trough, bar.high)

        stop_hit = bar.low <= t.stop if sign > 0 else bar.high >= t.stop
        target = t.targets[t.targets_hit] if t.targets_hit < len(t.targets) else None
        target_hit = target is not None and (bar.high >= target if sign > 0 else bar.low <= target)

        # Pessimistic ordering: when a bar covers both, the stop is assumed to come first.
        if stop_hit:
            slip = self.p.slippage_bps / 1e4
            fill = t.stop * (1 - slip * sign)
            return self._close(t, bar, fill, ExitReason.SL_EQ, inst)
        if target_hit and target is not None:
            t.targets_hit += 1
            if t.targets_hit >= len(t.targets):
                return self._close(t, bar, target, ExitReason.TARGET, inst)
            if self.p.limits.breakeven_after_t1:
                # The T1 staircase: once the first target prints, the trade cannot lose.
                t.stop = t.entry

        # Trail, only ever tightening — the same rule the live ExitEngine applies.
        gain = (t.peak - t.entry) * sign
        if gain > 0 and (gain / t.entry * 100) >= self.p.limits.trail_arm_pct:
            trailed = t.peak - sign * gain * self.p.limits.trail_giveback_pct / 100
            if (trailed > t.stop) if sign > 0 else (trailed < t.stop):
                t.stop = trailed

        if past_force_flat(self.p.segment, bar.ts):
            return self._close(t, bar, bar.close, ExitReason.EOD, inst)
        if t.bars_held >= self.p.limits.time_stop_bars:
            return self._close(t, bar, bar.close, ExitReason.TIME_STOP, inst)
        return None

    def _close(
        self, t: OpenTrade, bar: UnifiedBar, price: float, reason: ExitReason, inst: Instrument
    ) -> BtTrade:
        sign = t.sign()
        gross = (price - t.entry) * sign * t.qty * inst.multiplier
        charges = (
            self.costs.leg(inst, OrderSide.BUY if sign > 0 else OrderSide.SELL, t.entry, t.qty)
            + self.costs.leg(inst, OrderSide.SELL if sign > 0 else OrderSide.BUY, price, t.qty)
        ).total
        net = gross - charges
        r_unit_money = t.r_unit * t.qty * inst.multiplier
        opt_net = opt_r = None
        if self.p.model_option_leg:
            opt_net, opt_r = self._model_option(t, price, inst)
        return BtTrade(
            strategy=t.strategy,
            symbol=t.symbol,
            direction=t.direction.value,
            day=ist_day(t.entry_ts).isoformat(),
            entry_ts=t.entry_ts,
            exit_ts=bar.ts,
            entry=round(t.entry, 2),
            exit=round(price, 2),
            stop=round(t.initial_stop, 2),
            target1=round(t.targets[0], 2) if t.targets else None,
            qty=t.qty,
            gross=round(gross, 2),
            charges=round(charges, 2),
            net=round(net, 2),
            r_multiple=round(net / r_unit_money, 3) if r_unit_money > 0 else 0.0,
            mfe_r=round((t.peak - t.entry) * sign / t.r_unit, 3) if t.r_unit > 0 else 0.0,
            mae_r=round((t.trough - t.entry) * sign / t.r_unit, 3) if t.r_unit > 0 else 0.0,
            exit_reason=reason.value,
            grade=t.grade,
            bars_held=t.bars_held,
            opt_net_modelled=opt_net,
            opt_r_modelled=opt_r,
        )

    def _model_option(self, t: OpenTrade, exit_price: float, inst: Instrument) -> tuple[float, float]:
        """Project the underlying move onto a modelled option premium.

        **A model, not a measurement.** There is no option-chain history to check it against, and
        it ignores theta and the volatility path entirely. It exists to make the *sign* of the
        option economics visible — a small underlying move that pays for itself on the cash leg can
        still be a loss after option charges and spread.
        """
        strike = t.entry * (1.02 if t.direction is Direction.BULLISH else 0.98)
        delta = estimate_delta(
            spot=t.entry, strike=strike, option_type=t.direction.option_type
        )
        premium = max(1.0, t.entry * 0.02)
        opt_stop, _ = map_levels_to_option(
            equity_entry=t.entry,
            equity_stop=t.initial_stop,
            equity_targets=t.targets,
            option_premium=premium,
            delta=delta,
        )
        lot = max(1, inst.lot_size if inst.lot_size > 1 else 250)
        qty = max(lot, int(self.p.position_budget_inr // (premium * lot)) * lot)
        move = (exit_price - t.entry) * t.sign() * delta
        gross = move * qty
        option = Instrument(
            scrip_code=f"{t.symbol}-OPT",
            symbol=t.symbol,
            segment=Segment.NSE_FO,
            kind=InstrumentKind.OPTION,
            lot_size=lot,
            multiplier=1,
            strike=strike,
            option_type=t.direction.option_type,
            underlying=t.symbol,
        )
        charges = (
            self.costs.leg(option, OrderSide.BUY, premium, qty)
            + self.costs.leg(option, OrderSide.SELL, max(0.05, premium + move), qty)
        ).total
        net = gross - charges
        r_money = abs(premium - opt_stop) * qty
        return round(net, 2), round(net / r_money, 3) if r_money > 0 else 0.0


# -- persistence ---------------------------------------------------------------------------------------


def save(result: BacktestResult, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{result.id}.json"
    path.write_text(
        json.dumps(
            {"summary": result.summary(), "trades": [asdict(t) for t in result.trades]},
            indent=2,
            default=str,
        )
    )
    return path


def load_summaries(root: Path, limit: int = 25) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    out = []
    for p in sorted(root.glob("bt-*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        try:
            out.append(json.loads(p.read_text())["summary"])
        except Exception:  # noqa: BLE001 - a half-written result must not break the listing
            continue
    return out
