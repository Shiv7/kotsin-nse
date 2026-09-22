"""What an alert would actually be traded as: a stop/target ladder, an OTM strike, and a CTA.

The old dashboard's strategy tabs never showed a bare trigger. Each card carried the plan the
trigger implied — entry, stop, T1-T4, R:R, the ATR it was measured against — and the OTM contract
that plan would be expressed through, because "RELIANCE is bullish" is not something anyone can
act on and "buy the 1250 CE, stop at 1233, first target 1262" is.

Two rules hold here.

**The plan is the engine's own.** Stop and targets come from ``compute_confluence`` — the same
function the traded book uses — not a second implementation that would drift from it. A port whose
levels disagree with the live engine's is worse than no port.

**A plan that cannot be built is absent, not approximated.** No zones, no ATR, no listed strike:
the field is ``None`` and the card says so. The alternative is a card that looks actionable and
is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..bars.pivots import Zone, compute_confluence
from ..bars.unified import UnifiedBar

#: NSE strike ladders, transcribed from the dashboard's ``tradingUtils.getStrikeInterval``.
#: A heuristic there and a heuristic here — but the same one, so a strike shown on this page is the
#: strike that page would have shown.
STRIKE_STEPS: tuple[tuple[float, float], ...] = (
    (40000, 500),
    (20000, 200),
    (10000, 100),
    (5000, 50),
    (2000, 20),
    (1000, 10),
    (500, 5),
    (100, 2.5),
)


def strike_interval(price: float) -> float:
    for threshold, step in STRIKE_STEPS:
        if price > threshold:
            return step
    return 1.0


def otm_strike(price: float, direction: str) -> tuple[float, float]:
    """One step out of the money, and the ladder step used. ``getOTMStrike``'s rule exactly."""
    interval = strike_interval(price)
    atm = round(price / interval) * interval
    strike = atm + interval if direction == "BULLISH" else atm - interval
    return strike, interval


def approximate_delta(spot: float, strike: float, option_type: str) -> float:
    """The dashboard's logistic approximation. Labelled approximate wherever it is shown."""
    import math

    if strike <= 0:
        return 0.0
    moneyness = (spot - strike) / strike
    ce = 1 / (1 + math.exp(-10 * moneyness))
    return ce if option_type == "CE" else 1 - ce


@dataclass(slots=True)
class TradePlan:
    entry: float
    stop: float | None
    targets: list[float]
    rr: float
    grade: str
    atr: float | None
    option_type: str
    strike: float
    strike_interval: float
    delta: float
    fortress: float
    room_atr: float
    stop_zone: str
    target_zones: list[str]
    note: str
    listed: dict[str, Any] | None  # the real contract, when the chain has it

    @property
    def contract(self) -> str:
        return f"{self.option_type} {self.strike:g}"

    def to_json(self) -> dict[str, Any]:
        return {
            "entry": round(self.entry, 2),
            "sl": None if self.stop is None else round(self.stop, 2),
            "t1": round(self.targets[0], 2) if len(self.targets) > 0 else None,
            "t2": round(self.targets[1], 2) if len(self.targets) > 1 else None,
            "t3": round(self.targets[2], 2) if len(self.targets) > 2 else None,
            "t4": round(self.targets[3], 2) if len(self.targets) > 3 else None,
            "rr": round(self.rr, 2),
            "grade": self.grade,
            "atr": None if self.atr is None else round(self.atr, 2),
            "hasPivots": bool(self.stop_zone),
            "optionType": self.option_type,
            "strike": self.strike,
            "strikeInterval": self.strike_interval,
            "deltaApprox": round(self.delta, 2),
            "fortress": round(self.fortress, 2),
            "roomAtr": round(self.room_atr, 2),
            "stopZone": self.stop_zone,
            "targetZones": self.target_zones,
            "note": self.note,
            "listed": self.listed,
        }


def build(
    *,
    bar: UnifiedBar,
    direction: str,
    zones: list[Zone],
    atr_value: float | None,
    tick_size: float = 0.05,
    listed: dict[str, Any] | None = None,
) -> TradePlan | None:
    """The plan an alert implies, or ``None`` when the geometry cannot be built."""
    if atr_value is None or atr_value <= 0 or not zones:
        return None
    bullish = direction == "BULLISH"
    conf = compute_confluence(
        close=bar.close, bullish=bullish, zones=zones, atr_value=atr_value, tick_size=tick_size
    )
    option_type = "CE" if bullish else "PE"
    strike, interval = otm_strike(bar.close, direction)
    return TradePlan(
        entry=bar.close,
        stop=conf.stop,
        targets=list(conf.targets),
        rr=conf.rr,
        grade=conf.grade,
        atr=atr_value,
        option_type=option_type,
        strike=strike,
        strike_interval=interval,
        delta=approximate_delta(bar.close, strike, option_type),
        fortress=conf.fortress,
        room_atr=conf.room_ratio,
        stop_zone=conf.stop_zone,
        target_zones=list(conf.target_zones),
        note=conf.note,
        listed=listed,
    )


#: A stop closer than this to entry, measured in ATR, is inside a single decision bar's noise.
#: Not a guess: ``docs/strategies/FUDKII.md`` §8 measured the median confluence stop at 0.23% of
#: price with 89% inside 0.5%, a median hold of one bar, and 80% of exits at the stop — and the
#: repaired variants that lifted −1.40R to −0.27R were exactly a 1.0-1.5 ATR stop floor.
NOISE_STOP_ATR = 1.0


#: What the card tells you to do. Deliberately conservative: the only book in this repo with a
#: measured edge has a negative one, so nothing here says BUY.
def cta(plan: TradePlan | None, score: float, kind: str) -> dict[str, str]:
    if kind == "EXPIRED":
        return {"action": "STAND_DOWN", "text": "Signal retired — no longer actionable."}
    if plan is None:
        return {
            "action": "OBSERVE",
            "text": "No stop/target ladder could be built (no zones or no ATR). Watch only.",
        }
    if plan.grade == "F" or plan.stop is None:
        return {"action": "AVOID", "text": f"Graded F — {plan.note or 'geometry does not support a trade'}."}
    if plan.rr < 1.0:
        return {
            "action": "AVOID",
            "text": f"Only {plan.rr:.2f}R to the first wall — the stop is wider than the reward.",
        }
    # Checked before the R:R tiers, because a high R:R is what a noise stop *produces*. Grade A
    # was the worst bucket in the measured run for precisely this reason, so a rich reward over a
    # stop this tight is a reason to decline, not a reason to act.
    stop_atr = (
        abs(plan.entry - plan.stop) / plan.atr if plan.atr and plan.atr > 0 else None
    )
    if stop_atr is not None and stop_atr < NOISE_STOP_ATR:
        return {
            "action": "AVOID",
            "text": (
                f"Stop is {stop_atr:.2f} ATR from entry — inside one bar's noise. The {plan.rr:.2f}R "
                f"is an artefact of that tightness; grade A averaged -1.73R over 481 trades for "
                f"exactly this shape (FUDKII.md §8)."
            ),
        }
    if plan.room_atr < 0.5:
        return {
            "action": "WAIT_PULLBACK",
            "text": f"{plan.room_atr:.2f} ATR of room to the next wall; entering here buys the wall.",
        }
    if score >= 70 and plan.rr >= 2.0:
        return {
            "action": "PRIMARY",
            "text": (
                f"{plan.contract} · stop {plan.stop:.2f} · "
                f"T1 {plan.targets[0]:.2f} ({plan.rr:.2f}R)"
                if plan.targets
                else f"stop {plan.stop:.2f}"
            ),
        }
    return {
        "action": "OBSERVE",
        "text": f"Grade {plan.grade}, {plan.rr:.2f}R — tradeable geometry, unproven edge.",
    }
