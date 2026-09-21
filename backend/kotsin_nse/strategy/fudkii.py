"""FUDKII — a 30-minute SuperTrend flip that coincides with a Bollinger break.

**Thesis.** On a 30m chart, a SuperTrend flip landing on the same bar as a close outside the
Bollinger band is a regime change with momentum behind it: trend reversal *and* volatility
expansion at once. The trade is expressed as an OTM option chosen so it is roughly at-the-money by
the confluence T1 (see ``instrument.select``).

**Falsifier.** If signals graded A/B stop outperforming C, the geometry that produces the grade has
stopped carrying information and the book is paying option premium for noise.

Differences from the Java original, each deliberate:

* ``bb_period`` / ``st_atr_period`` are **read**. In the old service they were bound from
  ``application.properties`` into fields that appeared in exactly one place — a startup log line —
  while the calculator used hardcoded constants that happened to equal the configured values. The
  class javadoc said ``SuperTrend(10,3)``; the code ran 7. Both are arguments here.
* The **10-minute flip debounce is gone.** It existed because SuperTrend state was persisted and
  could be lost or raced across restarts; a pure recomputation over the trailing window cannot
  disagree with itself, so a flip either happened on this bar or it did not. ``flip_max_bars_ago``
  keeps the knob, defaulted to the strict reading.
* The two ``fudkii.router.*`` flags are **not here**. They read like a kill switch and were bound by
  Spring, but nothing ever read them; they gated a router that was later scoped to a different book.
* An F-grade candidate is **recorded as a rejection**, not dropped. ~61% of graded signals were F in
  the live log and nothing counted them, so nobody could say whether the RR floor was mis-set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..bars.indicators import atr, bars_in_trend, bollinger, supertrend
from ..bars.pivots import GradePolicy, compute_confluence
from ..bars.unified import UnifiedBar
from ..domain import Direction
from .base import Context, Outcome, Rejection, Signal
from .gates import Gate, GateResult, GateStats, OnMissing, binding_gate, chain_passed
from .keys import StrategyKey

SCORE_ST_FLIP = 50.0
SCORE_BB_BREAK = 50.0


@dataclass(frozen=True, slots=True)
class FudkiiConfig:
    tf: str = "30m"
    bb_period: int = 20
    bb_mult: float = 2.0
    st_atr_period: int = 7
    st_mult: float = 3.0
    #: both conditions required → threshold 100. The live setting; a flip alone never fires.
    require_both: bool = True
    #: 0 = the flip must be on the decision bar. See the module docstring on the removed debounce.
    flip_max_bars_ago: int = 0
    #: the calculator hard-requires ``max(bb, atr) + 1``; 50 is the number it wants for a clean
    #: Wilder warm-up, and below it the ATR is still converging.
    warm_bars: int = 50
    #: period for the ATR that scales the confluence room ratio (not the SuperTrend ATR)
    room_atr_period: int = 14
    #: block grade F at publish
    grade_floor: bool = True
    #: the last bar of the session takes only strong signals — a 30m trade opened at 15:15 has
    #: 15 minutes to work before the force-flat
    eod_strong_only: bool = True
    eod_min_fortress: float = 10.0
    grade_policy: GradePolicy = field(default_factory=GradePolicy)

    @property
    def min_bars(self) -> int:
        return max(self.bb_period, self.st_atr_period) + 1

    @property
    def score_threshold(self) -> float:
        return 100.0 if self.require_both else 50.0


class Fudkii:
    key = StrategyKey.FUDKII

    def __init__(self, cfg: FudkiiConfig | None = None) -> None:
        self.cfg = cfg or FudkiiConfig()
        self.timeframes = (self.cfg.tf,)
        self.stats = GateStats()
        self.g_history = Gate("history", OnMissing.FAIL_CLOSED)
        self.g_flip = Gate("st_flip", OnMissing.FAIL_CLOSED)
        self.g_bb = Gate("bb_break", OnMissing.FAIL_CLOSED)
        self.g_score = Gate("score", OnMissing.FAIL_CLOSED)
        self.g_grade = Gate("confluence_grade", OnMissing.FAIL_CLOSED, required=self.cfg.grade_floor)
        self.g_eod = Gate("eod_strong_only", OnMissing.FAIL_OPEN)

    # -----------------------------------------------------------------------------------------

    def on_bar(self, ctx: Context, bar: UnifiedBar) -> Outcome:
        cfg = self.cfg
        out = Outcome()
        if bar.tf != cfg.tf or not bar.complete:
            return out

        hist = list(ctx.bars(bar.symbol, cfg.tf, cfg.warm_bars + 5))
        if not hist or hist[-1].ts != bar.ts:
            return out

        gates: list[GateResult] = [
            self.g_history.evaluate(
                float(len(hist)), lambda v: v >= cfg.min_bars, threshold=float(cfg.min_bars)
            )
        ]
        if not chain_passed(gates):
            return self._reject(out, bar, None, gates, {}, "insufficient history")

        closes = [b.close for b in hist]
        bb = bollinger(closes, cfg.bb_period, cfg.bb_mult)
        st_series = supertrend(hist, cfg.st_atr_period, cfg.st_mult)
        st = st_series[-1]
        room_atr = atr(hist, cfg.room_atr_period)
        if bb is None or st is None or room_atr is None or room_atr <= 0:
            gates.append(self.g_flip.evaluate(None, lambda v: True))
            return self._reject(out, bar, None, gates, {}, "indicators not warm")

        trend_bars = bars_in_trend(st_series)
        bars_since_flip = trend_bars - 1  # 0 on the bar the trend changed
        flipped = bars_since_flip <= cfg.flip_max_bars_ago
        direction = Direction.BULLISH if st.trend > 0 else Direction.BEARISH
        broke = (
            bar.close > bb.upper if direction is Direction.BULLISH else bar.close < bb.lower
        )
        band = bb.upper if direction is Direction.BULLISH else bb.lower

        score = (SCORE_ST_FLIP if flipped else 0.0) + (SCORE_BB_BREAK if broke else 0.0)
        evidence: dict[str, float] = {
            "close": bar.close,
            "bb_upper": bb.upper,
            "bb_middle": bb.middle,
            "bb_lower": bb.lower,
            "bb_width": bb.width,
            "st_value": st.value,
            "st_trend": float(st.trend),
            "bars_in_trend": float(trend_bars),
            "bars_since_flip": float(bars_since_flip),
            "atr": room_atr,
            "atr_pct": room_atr / bar.close * 100 if bar.close else 0.0,
            "score": score,
            "volume": bar.volume,
        }
        if bar.oi is not None:
            evidence["oi"] = float(bar.oi)
        if bar.oi_change_pct is not None:
            evidence["oi_change_pct"] = bar.oi_change_pct

        gates.append(
            self.g_flip.evaluate(
                float(bars_since_flip),
                lambda v: v <= cfg.flip_max_bars_ago,
                threshold=float(cfg.flip_max_bars_ago),
                note=f"trend {'UP' if st.trend > 0 else 'DOWN'} for {trend_bars} bars",
            )
        )
        gates.append(
            self.g_bb.evaluate(
                bar.close,
                lambda v: (v > band) if direction is Direction.BULLISH else (v < band),
                threshold=band,
                note=f"close vs {'upper' if direction is Direction.BULLISH else 'lower'} band",
            )
        )
        gates.append(
            self.g_score.evaluate(score, lambda v: v >= cfg.score_threshold, threshold=cfg.score_threshold)
        )
        if not chain_passed(gates):
            return self._reject(out, bar, direction, gates, evidence, "")

        zones = ctx.zones(bar.symbol)
        conf = compute_confluence(
            close=bar.close,
            bullish=direction is Direction.BULLISH,
            zones=zones,
            atr_value=room_atr,
            policy=cfg.grade_policy,
        )
        context: dict[str, Any] = {
            "indicators": {
                "bb_upper": bb.upper, "bb_middle": bb.middle, "bb_lower": bb.lower,
                "st_value": st.value, "st_trend": st.trend, "bars_in_trend": trend_bars,
                "atr": room_atr,
                "params": {"bb_period": cfg.bb_period, "bb_mult": cfg.bb_mult,
                           "st_atr_period": cfg.st_atr_period, "st_mult": cfg.st_mult},
            },
            "confluence": {
                "stop": conf.stop, "stop_zone": conf.stop_zone,
                "targets": list(conf.targets), "target_zones": list(conf.target_zones),
                "grade": conf.grade, "rr": conf.rr, "fortress": conf.fortress,
                "room_ratio": conf.room_ratio, "note": conf.note,
                "policy": {"rr_hard_floor": cfg.grade_policy.rr_hard_floor,
                           "rr_a": cfg.grade_policy.rr_a, "rr_b": cfg.grade_policy.rr_b,
                           "rr_c": cfg.grade_policy.rr_c, "room_min_atr": cfg.grade_policy.room_min_atr},
            },
            "zones": [
                {"price": round(z.price, 2), "strength": round(z.strength, 2),
                 "wall": z.is_wall, "members": list(z.members)}
                for z in sorted(zones, key=lambda z: z.price)
            ],
        }
        evidence.update(
            {
                "rr": conf.rr,
                "fortress": conf.fortress,
                "room_ratio": conf.room_ratio,
                "zones": float(conf.zones_considered),
                "risk": abs(bar.close - conf.stop),
            }
        )
        gates.append(
            self.g_grade.verdict(
                conf.grade != "F",
                value=conf.rr,
                note=f"grade {conf.grade or 'none'} rr={conf.rr} {conf.note}".strip(),
            )
        )

        phase = ctx.session_phase(bar.symbol, bar.ts)
        if cfg.eod_strong_only and phase == "EOD":
            gates.append(
                self.g_eod.evaluate(
                    conf.fortress,
                    lambda v: v >= cfg.eod_min_fortress,
                    threshold=cfg.eod_min_fortress,
                    note="last bar of the session takes strong signals only",
                )
            )
        else:
            gates.append(self.g_eod.verdict(True, note=f"phase {phase}"))

        if not chain_passed(gates):
            return self._reject(out, bar, direction, gates, evidence, conf.note)

        self.stats.record(gates)
        out.signals.append(
            Signal(
                strategy=self.key,
                symbol=bar.symbol,
                direction=direction,
                ts=bar.ts,
                entry=bar.close,
                stop=conf.stop,
                targets=conf.targets,
                grade=conf.grade,
                rr=conf.rr,
                score=score,
                confidence=min(1.0, conf.rr / cfg.grade_policy.rr_a),
                reason=(
                    f"ST flip {'UP' if st.trend > 0 else 'DOWN'} + close "
                    f"{'above upper' if direction is Direction.BULLISH else 'below lower'} band; "
                    f"grade {conf.grade} rr {conf.rr}"
                ),
                gates=tuple(gates),
                evidence=evidence,
                context=context,
            )
        )
        return out

    # -----------------------------------------------------------------------------------------

    def _reject(
        self,
        out: Outcome,
        bar: UnifiedBar,
        direction: Direction | None,
        gates: list[GateResult],
        evidence: dict[str, float],
        note: str,
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
