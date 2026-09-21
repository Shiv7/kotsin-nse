"""FUKAA — FUDKII, admitted only when volume says someone actually showed up.

**Thesis.** A SuperTrend flip with a Bollinger break is a real event, but base FUDKII takes it
whether or not anyone participated. FUKAA requires the 30-minute volume to run a multiple of its
recent baseline on either the trigger bar or the one before it. Volume that large is hard to fake
and marks genuine commitment to the break. It also allows the confirmation to arrive **one bar
late**: a signal that fires without volume is kept ``WATCHING`` for 35 minutes and promoted if the
next bar delivers.

**Falsifier.** If volume-confirmed FUDKII signals stop outperforming unconfirmed ones, the filter is
only reducing sample size.

Derived, not duplicated: FUKAA consumes a FUDKII :class:`~.base.Signal` and never recomputes
Bollinger or SuperTrend. In the old service both books were published from inside one 6,732-line
trigger class, which is why a comment describing their interaction stayed in the code for two
months after the interaction was deleted.

**Three dead things removed** (see ``docs/strategies/FUKAA.md`` for the evidence):

* ``fukaa.trigger.volume.multiplier`` was set in the properties file and read by nothing — the code
  read three *other* keys. NSE's default happened to equal the configured value, so it looked fine;
  MCX silently ran at **1.0×**, meaning a bar at its own average volume passed a gate named "volume
  confirmation". The Mongo audit proved it: ``ex=M volumeMultiplier=[1]``, and 11 of 16 MCX passes
  would have failed a 4.0× bar. Here there is one field per exchange and all three are read.
  **MCX now defaults to 2.0, which is a decision, not a recovered value — it needs recalibration.**
* ``selection.top.n=999`` and ``selection.max.same.direction=999`` were sentinels that disabled a
  stage called "Top-N selection". They are ``int | None`` here; ``None`` means off and says so at
  boot.
* The cross-strategy dedup comment ("FUKAA wins over FUDKII if both fire within 35s") described
  behaviour excised on 2026-06-24. Both books co-trade, deliberately; see ``risk.exposure`` for the
  aggregate-exposure guard that makes that survivable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..bars.indicators import atr, volume_surges
from ..bars.unified import UnifiedBar
from ..domain import Direction
from .base import Context, Outcome, Rejection, Signal
from .conviction import ConvictionInput, Tier, score, thresholds_for
from .gates import Gate, GateResult, GateStats, OnMissing, binding_gate, chain_passed
from .keys import StrategyKey


@dataclass(frozen=True, slots=True)
class FukaaConfig:
    #: Volume multiple required, per exchange. All three are read — that is the point.
    volume_multiplier_nse: float = 4.0
    volume_multiplier_mcx: float = 2.0
    volume_multiplier_cds: float = 2.0
    #: baseline = mean of T-2 … T-(avg_bars+1). T-1 is excluded from its own baseline so the bar
    #: being judged cannot inflate what it is judged against.
    avg_bars: int = 6
    #: hard floor on the baseline, so a dead scrip's 3-share average cannot manufacture a 400× surge
    avg_volume_floor: float = 1000.0
    #: how long a volume-less base signal stays eligible for T+1 promotion
    watching_ttl_minutes: int = 35
    composite_min: float = 60.0
    rr_floor: float = 0.5
    #: |OI change %| floor. [UNVERIFIED] the original `fukaa.selection.refoi` values (5.0 NSE /
    #: 8.0 MCX / 3.0 CDS) are preserved in `conviction.ExchangeThresholds.ref_oi_floor`, but the
    #: exact quantity they were compared against was not traced; this is the honest reading.
    require_ref_oi: bool = True
    #: no surge cap — extreme surges ARE the signal
    tier_floor: Tier = Tier.S4
    #: R4: None means OFF, not 999.
    top_n: int | None = None
    max_same_direction: int | None = None
    atr_period: int = 14
    promote_on_t_plus_1: bool = True


@dataclass(slots=True)
class Watching:
    """A base signal parked until the next bar can confirm it."""

    symbol: str
    direction: Direction
    ts: int
    entry: float
    stop: float
    targets: tuple[float, ...]
    grade: str
    rr: float
    expires_ts: int
    base_signal_id: str
    evidence: dict[str, float] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)


class Fukaa:
    key = StrategyKey.FUKAA
    source = StrategyKey.FUDKII

    def __init__(self, cfg: FukaaConfig | None = None) -> None:
        self.cfg = cfg or FukaaConfig()
        self.stats = GateStats()
        self.g_history = Gate("history", OnMissing.FAIL_CLOSED)
        self.g_volume = Gate("volume_surge", OnMissing.FAIL_CLOSED)
        self.g_composite = Gate("composite", OnMissing.FAIL_CLOSED)
        self.g_rr = Gate("rr", OnMissing.FAIL_CLOSED)
        self.g_refoi = Gate("ref_oi", OnMissing.FAIL_CLOSED, required=self.cfg.require_ref_oi)
        self.g_tier = Gate("conviction_tier", OnMissing.FAIL_CLOSED)

    def multiplier(self, exchange: str) -> float:
        return {
            "N": self.cfg.volume_multiplier_nse,
            "M": self.cfg.volume_multiplier_mcx,
            "C": self.cfg.volume_multiplier_cds,
        }.get(exchange.upper(), self.cfg.volume_multiplier_nse)

    # -- entry points ---------------------------------------------------------------------------

    def on_signal(self, ctx: Context, bar: UnifiedBar, base: Signal) -> Outcome:
        """A FUDKII signal just fired on this bar. Confirm it, park it, or reject it."""
        return self._evaluate(ctx, bar, base=base, watching=None)

    def on_bar(self, ctx: Context, bar: UnifiedBar) -> Outcome:
        """No base signal on this bar — but a parked one may now be confirmable (T+1 promotion)."""
        out = Outcome()
        if not self.cfg.promote_on_t_plus_1:
            return out
        parked = self._parked(ctx)
        w = parked.get(bar.symbol)
        if w is None:
            return out
        if bar.ts > w.expires_ts:
            parked.pop(bar.symbol, None)
            out.rejections.append(
                Rejection(
                    strategy=self.key,
                    symbol=bar.symbol,
                    ts=bar.ts,
                    direction=w.direction,
                    binding_gate="watching_expired",
                    gates=(),
                    evidence=dict(w.evidence),
                    note=f"no volume within {self.cfg.watching_ttl_minutes} min of the base trigger",
                )
            )
            return out
        if bar.ts == w.ts:
            return out  # the bar that parked it
        return self._evaluate(ctx, bar, base=None, watching=w)

    # -- core -----------------------------------------------------------------------------------

    def _evaluate(
        self, ctx: Context, bar: UnifiedBar, *, base: Signal | None, watching: Watching | None
    ) -> Outcome:
        cfg = self.cfg
        out = Outcome()
        src = base or watching
        if src is None:
            return out
        direction = src.direction
        exchange = ctx.exchange(bar.symbol)
        mult = self.multiplier(exchange)
        th = thresholds_for(exchange)

        hist = list(ctx.bars(bar.symbol, bar.tf, cfg.avg_bars + 4))
        gates: list[GateResult] = [
            self.g_history.evaluate(
                float(len(hist)),
                lambda v: v >= cfg.avg_bars + 2,
                threshold=float(cfg.avg_bars + 2),
                note="baseline is T-2..T-(N+1), so it needs N+2 bars",
            )
        ]
        if not chain_passed(gates):
            return self._reject(out, bar, direction, gates, {}, "insufficient history", src)

        vols = [b.volume for b in hist]
        surge_t, surge_t1, baseline = volume_surges(
            vols, window=cfg.avg_bars, floor=cfg.avg_volume_floor
        )
        best = max((s for s in (surge_t, surge_t1) if s is not None), default=None)
        passed_candle = "T" if (surge_t is not None and best == surge_t) else "T-1"

        a = atr(hist, cfg.atr_period) if len(hist) > cfg.atr_period else None
        price_over_atr = (abs(bar.close - bar.open) / a) if (a and a > 0) else None
        rr = src.rr  # both Signal and Watching carry the confluence RR

        conv = score(
            ConvictionInput(
                exchange=exchange,
                volume_surge=best,
                oi_change_pct=bar.oi_change_pct,
                oi_buildup_pct=bar.oi_change_pct,
                price_change_over_atr=price_over_atr,
                rr=rr,
            )
        )
        evidence: dict[str, float] = {
            "surge_t": surge_t if surge_t is not None else -1.0,
            "surge_t1": surge_t1 if surge_t1 is not None else -1.0,
            "surge_used": best if best is not None else -1.0,
            "baseline_volume": baseline if baseline is not None else -1.0,
            "multiplier": mult,
            "avg_bars": float(cfg.avg_bars),
            "composite": conv.composite,
            "volume_score": conv.volume_score,
            "oi_score": conv.oi_score,
            "momentum_score": conv.momentum_score,
            "rr_score": conv.rr_score,
            "rr": rr,
            "promoted": 1.0 if watching is not None else 0.0,
        }
        if bar.oi_change_pct is not None:
            evidence["oi_change_pct"] = bar.oi_change_pct

        gates.append(
            self.g_volume.evaluate(
                best,
                lambda v: v >= mult,
                threshold=mult,
                note=f"{exchange} bar {passed_candle}",
            )
        )
        volume_failed = not gates[-1].passed and not gates[-1].missing

        # T+1: park instead of rejecting, but only on the original trigger bar.
        if volume_failed and base is not None and cfg.promote_on_t_plus_1:
            self._park(ctx, bar, base)
            self.stats.record(gates)
            out.rejections.append(
                Rejection(
                    strategy=self.key,
                    symbol=bar.symbol,
                    ts=bar.ts,
                    direction=direction,
                    binding_gate="volume_surge",
                    gates=tuple(gates),
                    evidence=evidence,
                    note=f"WATCHING — {cfg.watching_ttl_minutes} min to confirm",
                )
            )
            return out

        gates.append(
            self.g_composite.evaluate(
                conv.composite, lambda v: v >= cfg.composite_min, threshold=cfg.composite_min
            )
        )
        gates.append(self.g_rr.evaluate(rr, lambda v: v >= cfg.rr_floor, threshold=cfg.rr_floor))
        gates.append(
            self.g_refoi.evaluate(
                abs(bar.oi_change_pct) if bar.oi_change_pct is not None else None,
                lambda v: v >= th.ref_oi_floor,
                threshold=th.ref_oi_floor,
                note="|OI change %| on the front future",
            )
        )
        gates.append(
            self.g_tier.verdict(
                conv.tier.tradeable,
                value=conv.composite,
                note=f"tier {conv.tier.value} (S5/S6 skip)",
            )
        )

        if not chain_passed(gates):
            if watching is not None:
                self._parked(ctx).pop(bar.symbol, None)
            return self._reject(out, bar, direction, gates, evidence, "", src)

        if watching is not None:
            self._parked(ctx).pop(bar.symbol, None)
        self.stats.record(gates)
        out.signals.append(
            Signal(
                strategy=self.key,
                symbol=bar.symbol,
                direction=direction,
                ts=bar.ts,
                entry=bar.close if watching is not None else src.entry,
                stop=src.stop,
                targets=tuple(src.targets),
                grade=src.grade,
                rr=rr,
                score=conv.composite,
                confidence=min(1.0, conv.composite / 100),
                reason=(
                    f"volume {best:.2f}x ≥ {mult:g}x on {passed_candle}, composite "
                    f"{conv.composite:.0f} ({conv.tier.value})"
                    + (" [T+1 promotion]" if watching is not None else "")
                ),
                gates=tuple(gates),
                evidence=evidence,
                source_signal_id=(
                    base.signal_id if base is not None else (watching.base_signal_id if watching else "")
                ),
                context={**(dict(base.context) if base is not None else (dict(watching.context) if watching else {})),
                         "conviction": {"composite": conv.composite, "tier": conv.tier.value,
                                        "volume_score": conv.volume_score, "oi_score": conv.oi_score,
                                        "momentum_score": conv.momentum_score, "rr_score": conv.rr_score},
                         "volume": {"surge_t": surge_t, "surge_t1": surge_t1, "baseline": baseline,
                                    "multiplier": mult, "exchange": exchange}},
            )
        )
        return out

    # -- watching state ----------------------------------------------------------------------------

    def _parked(self, ctx: Context) -> dict[str, Watching]:
        store = ctx.state.setdefault("watching", {})
        assert isinstance(store, dict)
        return store

    def _park(self, ctx: Context, bar: UnifiedBar, base: Signal) -> None:
        self._parked(ctx)[bar.symbol] = Watching(
            symbol=bar.symbol,
            direction=base.direction,
            ts=bar.ts,
            entry=base.entry,
            stop=base.stop,
            targets=tuple(base.targets),
            grade=base.grade,
            rr=base.rr,
            expires_ts=bar.ts + self.cfg.watching_ttl_minutes * 60,
            base_signal_id=base.signal_id,
            evidence=dict(base.evidence),
            context=dict(base.context),
        )

    def _reject(
        self,
        out: Outcome,
        bar: UnifiedBar,
        direction: Direction,
        gates: list[GateResult],
        evidence: dict[str, float],
        note: str,
        src: Signal | Watching,
    ) -> Outcome:
        self.stats.record(gates)
        out.rejections.append(
            Rejection(
                strategy=self.key,
                symbol=bar.symbol,
                ts=bar.ts,
                direction=direction,
                binding_gate=binding_gate(gates) or "unknown",
                gates=tuple(gates),
                evidence=evidence,
                note=note,
            )
        )
        return out


def select(signals: list[Signal], cfg: FukaaConfig) -> tuple[list[Signal], list[Signal]]:
    """Top-N and same-direction caps across one batch. Returns ``(admitted, dropped)``.

    ``None`` means the cap is OFF — and because it is ``None`` rather than ``999``, the boot banner
    can say so truthfully instead of advertising a selection stage that selects nothing.
    """
    if cfg.top_n is None and cfg.max_same_direction is None:
        return list(signals), []
    ranked = sorted(signals, key=lambda s: (-s.score, -s.rr))
    admitted: list[Signal] = []
    dropped: list[Signal] = []
    per_direction: dict[Direction, int] = {}
    for s in ranked:
        if cfg.top_n is not None and len(admitted) >= cfg.top_n:
            dropped.append(s)
            continue
        n = per_direction.get(s.direction, 0)
        if cfg.max_same_direction is not None and n >= cfg.max_same_direction:
            dropped.append(s)
            continue
        per_direction[s.direction] = n + 1
        admitted.append(s)
    return admitted, dropped
