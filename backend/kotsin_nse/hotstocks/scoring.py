"""The HotStocks v2 signed-directional score, ported bucket for bucket.

Every constant here is transcribed from ``trading-dashboard`` ``HotStocksScoringEngine.java`` —
caps at lines 41-45, rotation thresholds at 48-49, the bucket functions at 360-441 and the clamp
ladder at 452-510. It is a port, not a reinterpretation: where the Java rounds, this rounds; where
it uses ``Math.min`` to cap rather than to scale, so does this. A number that disagrees with the
Java is a bug in this file.

Range is [-100, +100] for an F&O name (30 + 25 + 20 + 15 + 10) and [-80, +80] for a cash-only one,
which has no OI bucket. Positive is accumulation, negative is distribution, zero is no edge.

**A bucket whose inputs are unknown scores zero and says so.** That is the difference between "the
exchange published no deals for this name" and "the institutions sold it", and the old stack lost
real money to a score that could not tell them apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BUCKET1_CAP = 30  # institutional flow
BUCKET2_CAP = 25  # price momentum
BUCKET3_CAP = 20  # OI / futures congruence
BUCKET4_CAP = 15  # relative strength
BUCKET5_CAP = 10  # volume regime

ROTATION_HALF_THRESHOLD = 0.30  # conviction below this halves the flow tier
ROTATION_QUARTER_THRESHOLD = 0.50  # [0.30, 0.50) takes three quarters

#: Live default in the Java engine is OFF, per its own 2026-08-03 backtest note.
DELIVERY_INSTITUTIONAL_BONUS_ENABLED = False
ROTATION_PENALTY_ENABLED = True


def _clamp(v: float, cap: int) -> int:
    return int(max(-cap, min(cap, v)))


def _bounded_linear(value: float, slope: float, cap: float) -> float:
    return max(-cap, min(cap, value * slope))


@dataclass(slots=True)
class FlowInput:
    """Disclosed bulk/block deal flow over the window, in crore."""

    buy_cr: float = 0.0
    sell_cr: float = 0.0
    conviction: float = 0.0  # 0..1, how one-sided the flow is
    known: bool = False  # False = the exchange published nothing for this name

    def net(self) -> float:
        return self.buy_cr - self.sell_cr


@dataclass(slots=True)
class OiInput:
    oi_5d_pct: float = 0.0
    available: bool = False


@dataclass(slots=True)
class ScoreInput:
    change_1d_pct: float = 0.0
    change_5d_pct: float = 0.0
    change_20d_pct: float = 0.0
    weekly_52_position_pct: float | None = None
    vs_sector_label: str = "INLINE"
    vs_nifty_label: str = "INLINE"
    volume_regime: str = "NORMAL"
    price_regime: str = "RANGE_BOUND"
    delivery_pct_latest: float = 0.0
    delivery_institutional: bool = False
    smart_buy_cr: float = 0.0
    smart_sell_cr: float = 0.0
    smart_buy_clients: list[str] = field(default_factory=list)
    smart_sell_clients: list[str] = field(default_factory=list)
    fno_eligible: bool = True


@dataclass(slots=True)
class ScoreResult:
    final: int
    pre_clamp: int
    bucket1: int
    bucket2: int
    bucket3: int
    bucket4: int
    bucket5: int
    without_volume: int
    clamps: list[str]
    tier: str
    data_confidence: float


def score_flow(flow: FlowInput, m: ScoreInput) -> int:
    """Bucket 1 — institutional flow (``HotStocksScoringEngine.java:340-391``)."""
    if not flow.known:
        return 0
    net = flow.net()
    if net >= 50:
        s: float = 20
    elif net >= 20:
        s = 10
    elif net > -20:
        s = 0
    elif net >= -50:
        s = -10
    else:
        s = -20

    # Rotation runs before the delivery bonus on purpose: it erodes the raw net-tier
    # contribution, not a bonus that rotation says nothing about.
    if ROTATION_PENALTY_ENABLED:
        if flow.conviction < ROTATION_HALF_THRESHOLD:
            s = round(s * 0.5)
        elif flow.conviction < ROTATION_QUARTER_THRESHOLD:
            s = round(s * 0.75)

    if net < -100 and flow.sell_cr < 2 * max(flow.buy_cr, 0.01):
        s -= 5

    if DELIVERY_INSTITUTIONAL_BONUS_ENABLED and m.delivery_institutional and m.price_regime in (
        "BULLISH_TREND",
        "NEUTRAL",
        None,
    ):
        s += 10
    return _clamp(s, BUCKET1_CAP)


def score_price(m: ScoreInput) -> int:
    """Bucket 2 — price momentum (``:395-408``)."""
    w52 = m.weekly_52_position_pct or 0.0
    s = 0.0
    s += _bounded_linear(m.change_1d_pct, 2.0, 5)
    s += _bounded_linear(m.change_5d_pct, 2.0, 10)
    s += _bounded_linear(m.change_20d_pct, 0.5, 5)
    if w52 > 80:
        s += 5
    elif 0 < w52 < 20:
        s -= 5
    return _clamp(round(s), BUCKET2_CAP)


def score_oi(oi: OiInput, m: ScoreInput, flow: FlowInput) -> int:
    """Bucket 3 — OI / futures congruence, F&O only (``:411-420``)."""
    if not oi.available:
        return 0
    o, c = oi.oi_5d_pct, m.change_5d_pct
    if o > 1 and c > 1:
        return 15  # long buildup
    if o > 1 and c < -1:
        return -15  # short buildup
    if o < -1 and c > 1:
        return 5 if flow.net() >= 0 else 0  # short covering, only if flow is not distributing
    if o < -1 and c < -1:
        return -5  # long unwinding
    return 0


def score_relative_strength(m: ScoreInput) -> int:
    """Bucket 4 — relative strength (``:423-430``)."""
    s = 0
    if m.vs_sector_label == "LEADING":
        s += 10
    elif m.vs_sector_label == "LAGGING":
        s -= 10
    if m.vs_nifty_label == "LEADING":
        s += 5
    elif m.vs_nifty_label == "LAGGING":
        s -= 5
    return _clamp(s, BUCKET4_CAP)


def score_volume(m: ScoreInput) -> int:
    """Bucket 5 — volume regime (``:433-441``)."""
    vr, c1 = m.volume_regime, m.change_1d_pct
    if vr == "ELEVATED" and c1 > 0:
        return 5
    if vr == "ELEVATED" and c1 < 0:
        return -7
    if vr == "DRYING_UP" and c1 > 0:
        return -3
    if vr == "DRYING_UP" and c1 < 0:
        return 3
    return 0


def apply_clamps(score: int, m: ScoreInput, flow: FlowInput, clamps: list[str]) -> int:
    """The veto ladder (``:452-510``). Independent and additive; many may stack."""
    s = score
    if m.price_regime == "BEARISH_TREND":
        s = min(s, 0)
        clamps.append("BEARISH_REGIME")
    if m.change_5d_pct < -5:
        s = min(s, -30)
        clamps.append("FALLING_KNIFE")
    if flow.known and flow.sell_cr > 2 * max(flow.buy_cr, 0.01) and flow.sell_cr > 25:
        s -= 25
        clamps.append("DISTRIBUTION")
    w52 = m.weekly_52_position_pct
    if w52 is not None and 0 < w52 < 15:
        s = min(s, -10)
        clamps.append("BOTTOM_RANGE")
    if m.change_1d_pct < -3 and m.volume_regime == "ELEVATED":
        s -= 15
        clamps.append("HEAVY_SELL_YEST")
    if flow.known and m.smart_buy_cr > 0 and m.smart_sell_cr < 5 and flow.net() < -100:
        s -= 20
        clamps.append("SMART_BUY_TAPE_SELL_DIVERGENCE")
    if s > 0 and 0 < m.delivery_pct_latest < 25 and m.smart_buy_cr > 0:
        s -= 15
        clamps.append("LOW_DELIVERY_SPECULATION")
    if m.smart_buy_clients and m.smart_sell_clients:
        if set(m.smart_buy_clients) & set(m.smart_sell_clients):
            clamps.append("ROTATION_NOT_ACCUMULATION")
    return s


def tier_of(score: int) -> str:
    if score >= 60:
        return "STRONG_ACCUMULATION"
    if score >= 30:
        return "ACCUMULATION"
    if score > -30:
        return "NEUTRAL"
    if score > -60:
        return "DISTRIBUTION"
    return "STRONG_DISTRIBUTION"


def compute(m: ScoreInput, flow: FlowInput, oi: OiInput) -> ScoreResult:
    b1 = score_flow(flow, m)
    b2 = score_price(m)
    b3 = score_oi(oi, m, flow) if m.fno_eligible else 0
    b4 = score_relative_strength(m)
    b5 = score_volume(m)

    pre = b1 + b2 + b3 + b4 + b5
    clamps: list[str] = []
    cap = 100 if m.fno_eligible else 80
    final = max(-cap, min(cap, apply_clamps(pre, m, flow, clamps)))

    # The volume ablation: the same score with bucket 5 zeroed, so a card can show how much of
    # its conviction is nothing but yesterday's volume.
    ablation_clamps: list[str] = []
    without_vol = max(
        -cap, min(cap, apply_clamps(b1 + b2 + b3 + b4, m, flow, ablation_clamps))
    )

    # Confidence is the share of buckets that had real inputs, not a model probability.
    known = [flow.known, True, oi.available or not m.fno_eligible, m.vs_sector_label != "UNKNOWN", True]
    confidence = round(sum(1 for k in known if k) / len(known), 2)

    return ScoreResult(
        final=final,
        pre_clamp=pre,
        bucket1=b1,
        bucket2=b2,
        bucket3=b3,
        bucket4=b4,
        bucket5=b5,
        without_volume=without_vol,
        clamps=clamps,
        tier=tier_of(final),
        data_confidence=confidence,
    )
