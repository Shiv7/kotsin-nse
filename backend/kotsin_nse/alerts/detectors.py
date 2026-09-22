"""The unported books, as live detectors.

Each one is transcribed from the parameters that were actually deployed in the old stack — the
values in :mod:`kotsin_nse.strategy.catalog`, which cite ``application.properties`` key by key.
Nothing here invents a threshold.

**These emit alerts, never orders.** They are deliberately outside the execution path: no wallet,
no sizing, no position. FUDKII — the one book here with a real artefact — backtests at −1.40R over
481 trades, so wiring a freshly ported book straight into the money path would be repeating the
mistake that measurement exists to prevent. A detector's job is to say "this fired, here is the
evidence"; whether that is worth trading is a separate question with a separate burden of proof.

Every detector is a pure function of bars it is handed plus its own cooldown state. No I/O, no
clock beyond the bar's own timestamp, so a detector replays identically in a backtest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..bars.indicators import (
    atr,
    bars_in_trend,
    bollinger,
    supertrend,
    volume_surge_median,
)
from ..bars.pivots import PivotLevels, Zone
from ..bars.unified import UnifiedBar


def _trend_name(point: Any) -> str | None:
    """SuperTrendPoint.trend is +1/-1. The UI and the reason strings want a word."""
    if point is None:
        return None
    return "UP" if point.trend > 0 else "DOWN"


@dataclass(slots=True)
class Alert:
    """One firing. ``evidence`` carries the numbers that made it fire, never a bare verdict.

    Three different times, because they are three different facts and the card was showing the
    least useful one:

    * ``ts`` — the bar's bucket **start**. The bar's identity, and why it always lands on a
      boundary with ``:00`` seconds. A 30m bar stamped 14:15 is the 14:15-14:45 bucket.
    * ``bar_close`` — when that bucket closed, i.e. when the book could first have decided.
    * ``fired_at`` — when this process actually emitted it, to the millisecond. Later than the
      close by however long the exchange-candle reconcile took.

    Showing ``ts`` as the alert time put a 14:45 firing on the card as 14:15 — half an hour early
    and never with a second on it. The dashboard hit the same thing and left a note about it
    (``getEpoch``: "prefer firedAt (actual publish moment) over triggerTime (candle close)").
    """

    book: str
    symbol: str
    scrip_code: str
    tf: str
    ts: int
    direction: str  # BULLISH | BEARISH | NEUTRAL
    score: float
    reason: str
    price: float
    evidence: dict[str, Any] = field(default_factory=dict)
    kind: str = "TRIGGER"  # TRIGGER | KEEPALIVE | EXPIRED
    #: The stop/target ladder and OTM contract this alert implies. Attached after the detector
    #: returns, because geometry needs zones and the chain, and a detector stays pure.
    plan: dict[str, Any] | None = None
    cta: dict[str, str] = field(default_factory=dict)
    company: str = ""
    exchange: str = "N"
    bar_close: int = 0
    fired_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "book": self.book,
            "symbol": self.symbol,
            "scripCode": self.scrip_code,
            "tf": self.tf,
            "ts": self.ts,
            "direction": self.direction,
            "score": round(self.score, 2),
            "reason": self.reason,
            "price": self.price,
            "evidence": self.evidence,
            "kind": self.kind,
            "plan": self.plan,
            "cta": self.cta,
            "company": self.company,
            "exchange": self.exchange,
            "barClose": self.bar_close,
            "firedAt": self.fired_at,
        }


class Cooldown:
    """Per-symbol suppression window, in the bar clock rather than wall time."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._last: dict[str, float] = {}

    def ready(self, symbol: str, ts: float) -> bool:
        last = self._last.get(symbol)
        return last is None or ts - last >= self.seconds

    def stamp(self, symbol: str, ts: float) -> None:
        self._last[symbol] = ts


class DailyCap:
    """``max signals per scrip per day`` and a global cap, both of which the old books carried."""

    def __init__(self, per_symbol: int | None = None, global_cap: int | None = None) -> None:
        self.per_symbol, self.global_cap = per_symbol, global_cap
        self._day = ""
        self._by_symbol: dict[str, int] = {}
        self._total = 0
        #: Firings the caps swallowed. A cap that binds silently is a cap nobody knows is binding:
        #: 216 symbols evaluate at the same 30m boundary, so a 30/day global cap is consumed in
        #: arrival order on the first one — the survivors are the earliest, not the strongest.
        self.suppressed = 0

    def _roll(self, day: str) -> None:
        if day != self._day:
            self._day, self._by_symbol, self._total = day, {}, 0

    def allows(self, symbol: str, day: str) -> bool:
        self._roll(day)
        if self.per_symbol is not None and self._by_symbol.get(symbol, 0) >= self.per_symbol:
            self.suppressed += 1
            return False
        if self.global_cap is not None and self._total >= self.global_cap:
            self.suppressed += 1
            return False
        return True

    @property
    def global_cap_reached(self) -> bool:
        return self.global_cap is not None and self._total >= self.global_cap

    def take(self, symbol: str, day: str) -> None:
        self._roll(day)
        self._by_symbol[symbol] = self._by_symbol.get(symbol, 0) + 1
        self._total += 1


# ── Bollinger-break family: MCX_BB30, MCX_BB15, NSE_BB30 ────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BbConfig:
    book: str
    tf: str
    cooldown_s: float
    min_volume_surge: float
    bb_period: int = 20
    bb_mult: float = 2.0
    st_period: int = 7
    st_mult: float = 3.0
    warm_bars: int = 25


#: Deployed values. nsebb30 cooldown 5400000ms / surge 1.5 and mcxbb15 1800000ms / 1.0 are read
#: straight off application.properties; mcxbb30 sits between them on the same 30m frame as its NSE
#: sibling, which is the only shape its own cooldown key can take.
BB_BOOKS: tuple[BbConfig, ...] = (
    BbConfig(book="NSE_BB_30", tf="30m", cooldown_s=5400, min_volume_surge=1.5),
    BbConfig(book="MCX_BB_30", tf="30m", cooldown_s=5400, min_volume_surge=1.0),
    BbConfig(book="MCX_BB_15", tf="15m", cooldown_s=1800, min_volume_surge=1.0),
)


class BbBreakDetector:
    """Close outside the band, with volume behind it, at most once a cooldown.

    Scored 0-100 so the tab can rank: half the score is how far outside the band the close sits
    (in band-widths), half is the volume surge over its own median. A break on no volume is the
    pattern that made this family need a surge floor in the first place.
    """

    def __init__(self, cfg: BbConfig, segment_filter: str | None = None) -> None:
        self.cfg = cfg
        self.segment_filter = segment_filter
        self.cooldown = Cooldown(cfg.cooldown_s)

    def on_bar(self, bar: UnifiedBar, history: list[UnifiedBar]) -> Alert | None:
        c = self.cfg
        if bar.tf != c.tf or not bar.complete or len(history) < c.warm_bars:
            return None
        if self.segment_filter and not bar.scrip_code:
            return None

        closes = [b.close for b in history]
        bb = bollinger(closes, c.bb_period, c.bb_mult)
        if bb is None or bb.upper <= bb.lower:
            return None

        above = bar.close > bb.upper
        below = bar.close < bb.lower
        if not (above or below):
            return None

        # volume_surge_median returns the *surge* — current over the median of the prior 20 —
        # not the median itself. Dividing by it again turned a volume collapse into a 1000x surge.
        vols = [b.volume for b in history]
        surge = volume_surge_median(vols, 20)
        if surge is None or surge < c.min_volume_surge:
            return None

        if not self.cooldown.ready(bar.symbol, bar.ts):
            return None

        width = bb.upper - bb.lower
        beyond = (bar.close - bb.upper) if above else (bb.lower - bar.close)
        beyond_ratio = beyond / width if width > 0 else 0.0

        st = supertrend(history, c.st_period, c.st_mult)
        st_dir = _trend_name(st[-1]) if st and st[-1] else None
        agrees = (st_dir == "UP" and above) or (st_dir == "DOWN" and below)

        score = min(100.0, min(beyond_ratio, 1.0) * 50 + min(surge / 3.0, 1.0) * 50)
        self.cooldown.stamp(bar.symbol, bar.ts)
        return Alert(
            book=c.book,
            symbol=bar.symbol,
            scrip_code=bar.scrip_code,
            tf=bar.tf,
            ts=bar.ts,
            direction="BULLISH" if above else "BEARISH",
            score=score,
            reason=(
                f"close {bar.close:.2f} {'above' if above else 'below'} band "
                f"{(bb.upper if above else bb.lower):.2f} by {beyond_ratio:.2f}w on "
                f"{surge:.2f}x volume"
                + ("; SuperTrend agrees" if agrees else "; SuperTrend does not agree")
            ),
            price=bar.close,
            evidence={
                "bbUpper": round(bb.upper, 2),
                "bbMiddle": round(bb.middle, 2),
                "bbLower": round(bb.lower, 2),
                "beyondBandWidths": round(beyond_ratio, 3),
                "volumeSurge": round(surge, 2),
                "volumeSurgeFloor": c.min_volume_surge,
                "superTrend": st_dir,
                "superTrendAgrees": agrees,
                "barsInTrend": bars_in_trend(st) if st else None,
            },
        )


# ── FUDKOI: FUDKII's trigger, admitted only on an open-interest move ─────────────────────────────


@dataclass(frozen=True, slots=True)
class FudkoiConfig:
    oi_threshold_mcx: float = 100.0
    oi_threshold_nse: float = 5.0  # fukaa.selection.refoi.nse, the same reference OI move
    bb_period: int = 20
    bb_mult: float = 2.0
    st_period: int = 7
    st_mult: float = 3.0
    warm_bars: int = 25
    cooldown_s: float = 5400


class FudkoiDetector:
    """The FUDKII geometry plus an OI confirmation.

    Cash equity carries no open interest — reading it there pinned one old score at zero for its
    whole life — so the OI is the underlying's front future, which the engine already stamps onto
    the bar. Without it the detector declines to fire rather than firing unconfirmed.
    """

    def __init__(self, cfg: FudkoiConfig | None = None) -> None:
        self.cfg = cfg or FudkoiConfig()
        self.cooldown = Cooldown(self.cfg.cooldown_s)

    def threshold(self, exch: str) -> float:
        return self.cfg.oi_threshold_mcx if exch == "M" else self.cfg.oi_threshold_nse

    def on_bar(self, bar: UnifiedBar, history: list[UnifiedBar], *, exch: str) -> Alert | None:
        c = self.cfg
        if not bar.complete or len(history) < c.warm_bars:
            return None
        oi_pct = bar.oi_change_pct
        if oi_pct is None:
            return None  # no OI on this instrument — decline, do not assume zero

        closes = [b.close for b in history]
        bb = bollinger(closes, c.bb_period, c.bb_mult)
        st = supertrend(history, c.st_period, c.st_mult)
        if bb is None or not st or st[-1] is None:
            return None
        point, prev = st[-1], (st[-2] if len(st) > 1 else None)
        # SuperTrendPoint carries the trend as +1/-1 and no flip flag, so the flip is the change
        # between two consecutive points — not an attribute to read off one.
        flipped = prev is not None and prev.trend != point.trend
        above, below = bar.close > bb.upper, bar.close < bb.lower
        bullish = flipped and point.trend > 0 and above
        bearish = flipped and point.trend < 0 and below
        if not (bullish or bearish):
            return None

        need = self.threshold(exch)
        if abs(oi_pct) < need:
            return None
        if not self.cooldown.ready(bar.symbol, bar.ts):
            return None

        self.cooldown.stamp(bar.symbol, bar.ts)
        return Alert(
            book="FUDKOI",
            symbol=bar.symbol,
            scrip_code=bar.scrip_code,
            tf=bar.tf,
            ts=bar.ts,
            direction="BULLISH" if bullish else "BEARISH",
            score=min(100.0, abs(oi_pct) / need * 100),
            reason=(
                f"ST flip {_trend_name(point)} + close {'above' if above else 'below'} band, "
                f"OI {oi_pct:+.1f}% vs {need:.0f}% required"
            ),
            price=bar.close,
            evidence={
                "oiChangePct": round(oi_pct, 2),
                "oiThreshold": need,
                "oi": bar.oi,
                "futScripCode": bar.fut_scrip_code,
                "bbUpper": round(bb.upper, 2),
                "bbLower": round(bb.lower, 2),
                "superTrend": _trend_name(point),
                "trendChanged": flipped,
            },
        )


# ── PIVOTBOSS: CPR width regime as a day bias ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PivotBossConfig:
    strong: float = 65.0  # pivotboss.bias.threshold.strong
    mild: float = 40.0  # pivotboss.bias.threshold.mild
    narrow_factor: float = 0.5  # pivotboss.cpr.width.narrowFactor
    wide_factor: float = 1.5  # pivotboss.cpr.width.wideFactor
    max_per_scrip_per_day: int = 2  # pivotboss.cadence.maxPerScripPerDay
    cooldown_minutes: int = 60  # pivotboss.cadence.cooldownMinutes
    global_daily_cap: int = 30  # pivotboss.cadence.globalDailyCap


class PivotBossDetector:
    """A narrow CPR is a trend day waiting to happen; a wide one is a range day.

    The regime is the CPR's own width against its recent average, not an absolute number — a
    commodity and a bank cannot share a threshold in rupees. Bias is then how far price sits from
    the central pivot, weighted by the confluence strength it is pushing through, which is what
    the old scorer's zone weighting amounts to.
    """

    def __init__(self, cfg: PivotBossConfig | None = None) -> None:
        self.cfg = cfg or PivotBossConfig()
        self.cooldown = Cooldown(self.cfg.cooldown_minutes * 60)
        self.cap = DailyCap(self.cfg.max_per_scrip_per_day, self.cfg.global_daily_cap)

    def on_bar(
        self,
        bar: UnifiedBar,
        history: list[UnifiedBar],
        *,
        levels: PivotLevels | None,
        zones: list[Zone],
        cpr_avg_width: float | None,
        day: str,
    ) -> Alert | None:
        c = self.cfg
        if not bar.complete or levels is None or not zones or len(history) < 25:
            return None
        width = levels.cpr_width
        if width <= 0 or not cpr_avg_width or cpr_avg_width <= 0:
            return None

        ratio = width / cpr_avg_width
        regime = (
            "NARROW" if ratio <= c.narrow_factor
            else "WIDE" if ratio >= c.wide_factor
            else "NORMAL"
        )
        if regime != "NARROW":
            return None  # only the trend-day regime is actionable; a wide CPR is a no-trade note

        pivot = levels.pivot
        a = atr(history, 14)
        if not a or a <= 0 or pivot <= 0:
            return None
        distance_atr = (bar.close - pivot) / a
        direction = "BULLISH" if distance_atr > 0 else "BEARISH"

        # Confluence in the direction of travel: the walls price would have to push through.
        ahead = [z for z in zones if (z.price > bar.close) == (distance_atr > 0)]
        wall = min(ahead, key=lambda z: abs(z.price - bar.close)) if ahead else None
        room_atr = abs(wall.price - bar.close) / a if wall else 0.0

        # 0-100: distance from the pivot, room to the next wall, and how tight the CPR is.
        score = min(100.0, min(abs(distance_atr), 2.0) * 25 + min(room_atr, 2.0) * 15 + (1 - ratio) * 30)
        if score < c.mild:
            return None
        if not self.cooldown.ready(bar.symbol, bar.ts) or not self.cap.allows(bar.symbol, day):
            return None

        self.cooldown.stamp(bar.symbol, bar.ts)
        self.cap.take(bar.symbol, day)
        return Alert(
            book="PIVOTBOSS",
            symbol=bar.symbol,
            scrip_code=bar.scrip_code,
            tf=bar.tf,
            ts=bar.ts,
            direction=direction,
            score=score,
            reason=(
                f"CPR {regime} ({ratio:.2f}x its average) · price {distance_atr:+.2f} ATR from "
                f"pivot {pivot:.2f} · {room_atr:.2f} ATR of room"
                + (f" to {','.join(wall.members)}" if wall else " with no wall ahead")
            ),
            price=bar.close,
            evidence={
                "cprWidth": round(width, 2),
                "cprWidthRatio": round(ratio, 3),
                "cprRegime": regime,
                "pivot": round(pivot, 2),
                "tc": round(levels.tc, 2),
                "bc": round(levels.bc, 2),
                "distanceAtr": round(distance_atr, 2),
                "roomAtr": round(room_atr, 2),
                "nextWall": wall.price if wall else None,
                "nextWallMembers": wall.members if wall else [],
                "tier": "STRONG" if score >= c.strong else "MILD",
            },
        )


# ── FUDKII-RT: the living signal ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FudkiiRtConfig:
    ttl_s: float = 2100.0  # fudkii.rt.living.ttl.ms = 2100000
    refresh_s: float = 60.0  # fudkii.rt.living.refresh.ms = 60000
    reeval_s: float = 300.0  # fudkii.rt.living.reeval.ms = 300000
    rr_gate: float = 1.0  # fudkii.rt.living.reeval.rr.gate


@dataclass(slots=True)
class LivingSignal:
    signal_id: str
    symbol: str
    scrip_code: str
    direction: str
    entry: float
    stop: float
    target: float
    born_ts: float
    last_reeval_ts: float
    grade: str

    def rr(self, price: float) -> float:
        risk = abs(self.entry - self.stop)
        return 0.0 if risk <= 0 else abs(self.target - price) / risk


class FudkiiRtDetector:
    """A fired FUDKII signal, re-evaluated on the 1m close until it expires.

    The old engine's whole point: a 30m trigger decided once at the boundary is stale for the next
    twenty-nine minutes, and the trade it described may already be gone. This keeps the signal
    alive for its TTL, re-checks the reward left every few minutes, and retires it when the reward
    no longer clears the gate — emitting the retirement, because a signal that quietly vanished is
    indistinguishable from one that was never taken.
    """

    def __init__(self, cfg: FudkiiRtConfig | None = None) -> None:
        self.cfg = cfg or FudkiiRtConfig()
        self.living: dict[str, LivingSignal] = {}

    def adopt(self, sig: dict[str, Any], ts: float) -> None:
        targets = sig.get("targets") or []
        if not targets:
            return
        self.living[sig["signal_id"]] = LivingSignal(
            signal_id=sig["signal_id"],
            symbol=sig["symbol"],
            scrip_code=str(sig.get("scrip_code") or ""),
            direction=sig["direction"],
            entry=float(sig["entry"]),
            stop=float(sig["stop"]),
            target=float(targets[0]),
            born_ts=ts,
            last_reeval_ts=ts,
            grade=str(sig.get("grade") or ""),
        )

    def on_bar(self, bar: UnifiedBar) -> list[Alert]:
        """1m close. Returns keep-alive and expiry alerts."""
        c = self.cfg
        out: list[Alert] = []
        for sid, s in list(self.living.items()):
            if s.symbol != bar.symbol:
                continue
            age = bar.ts - s.born_ts
            if age >= c.ttl_s:
                del self.living[sid]
                out.append(self._alert(s, bar, "EXPIRED", f"TTL {c.ttl_s / 60:.0f}m reached", 0.0))
                continue
            if bar.ts - s.last_reeval_ts < c.reeval_s:
                continue
            s.last_reeval_ts = bar.ts
            rr = s.rr(bar.close)
            stopped = (
                (s.direction == "BULLISH" and bar.close <= s.stop)
                or (s.direction == "BEARISH" and bar.close >= s.stop)
            )
            if stopped:
                del self.living[sid]
                out.append(
                    self._alert(s, bar, "EXPIRED", f"price crossed the stop {s.stop:.2f}", rr)
                )
            elif rr < c.rr_gate:
                del self.living[sid]
                out.append(
                    self._alert(
                        s, bar, "EXPIRED", f"reward left {rr:.2f}R below the {c.rr_gate:.1f}R gate", rr
                    )
                )
            else:
                out.append(
                    self._alert(s, bar, "KEEPALIVE", f"still live, {rr:.2f}R of reward left", rr)
                )
        return out

    def _alert(self, s: LivingSignal, bar: UnifiedBar, kind: str, why: str, rr: float) -> Alert:
        return Alert(
            book="FUDKII_RT",
            symbol=s.symbol,
            scrip_code=s.scrip_code,
            tf=bar.tf,
            ts=bar.ts,
            direction=s.direction,
            score=min(100.0, rr * 25),
            reason=f"{s.signal_id}: {why}",
            price=bar.close,
            kind=kind,
            evidence={
                "signalId": s.signal_id,
                "ageMinutes": round((bar.ts - s.born_ts) / 60, 1),
                "entry": s.entry,
                "stop": s.stop,
                "target": s.target,
                "rrLeft": round(rr, 2),
                "grade": s.grade,
                "ttlMinutes": round(self.cfg.ttl_s / 60),
            },
        )
