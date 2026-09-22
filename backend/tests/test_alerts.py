"""The ported books, as detectors. Advisory by construction, and they must stay that way."""

from __future__ import annotations

from kotsin_nse.alerts.detectors import (
    BB_BOOKS,
    BbBreakDetector,
    Cooldown,
    DailyCap,
    FudkiiRtDetector,
    FudkoiDetector,
)
from kotsin_nse.bars.unified import BarSource, UnifiedBar


def _bar(ts: int, c: float, v: float = 1000.0, tf: str = "30m", **kw) -> UnifiedBar:
    return UnifiedBar(
        symbol=kw.pop("symbol", "RELIANCE"),
        scrip_code="2885",
        tf=tf,
        ts=ts,
        open=c,
        high=c + 1,
        low=c - 1,
        close=c,
        volume=v,
        source=BarSource.REST,
        complete=True,
        **kw,
    )


def _flat_history(n: int = 60, price: float = 100.0, vol: float = 1000.0) -> list[UnifiedBar]:
    return [_bar(1_700_000_000 + i * 1800, price, vol) for i in range(n)]


def test_a_break_on_no_volume_does_not_fire():
    """The surge floor is the whole reason this family has one."""
    cfg = next(c for c in BB_BOOKS if c.book == "NSE_BB_30")
    det = BbBreakDetector(cfg)
    hist = _flat_history()
    breakout = _bar(hist[-1].ts + 1800, 130.0, v=100.0)  # far outside the band, volume collapsed
    assert det.on_bar(breakout, [*hist, breakout]) is None


def test_a_break_with_volume_fires_once_then_respects_the_cooldown():
    cfg = next(c for c in BB_BOOKS if c.book == "NSE_BB_30")
    det = BbBreakDetector(cfg)
    hist = _flat_history()
    b1 = _bar(hist[-1].ts + 1800, 130.0, v=5000.0)
    a = det.on_bar(b1, [*hist, b1])
    assert a is not None
    assert a.direction == "BULLISH"
    assert a.book == "NSE_BB_30"
    assert a.evidence["volumeSurge"] >= cfg.min_volume_surge
    assert 0 < a.score <= 100

    # Same symbol, one bar later: inside the 90-minute cooldown the old stack deployed.
    b2 = _bar(b1.ts + 1800, 131.0, v=5000.0)
    assert det.on_bar(b2, [*hist, b1, b2]) is None


def test_the_three_bb_books_differ_only_where_the_old_stack_differed():
    by = {c.book: c for c in BB_BOOKS}
    assert by["NSE_BB_30"].min_volume_surge == 1.5
    assert by["MCX_BB_15"].cooldown_s == 1800
    assert by["NSE_BB_30"].cooldown_s == 5400
    assert by["MCX_BB_15"].tf == "15m"
    # Geometry is shared — a Bollinger break is a Bollinger break.
    assert len({(c.bb_period, c.bb_mult, c.st_period, c.st_mult) for c in BB_BOOKS}) == 1


def test_fudkoi_declines_when_there_is_no_open_interest_rather_than_assuming_zero():
    """Cash equity has no OI. Reading it as 0 pinned one old score at zero for its entire life."""
    det = FudkoiDetector()
    hist = _flat_history()
    bar = _bar(hist[-1].ts + 1800, 130.0, v=5000.0)  # oi_change_pct defaults to None
    assert bar.oi_change_pct is None
    assert det.on_bar(bar, [*hist, bar], exch="N") is None


def test_fudkoi_thresholds_are_per_exchange_as_deployed():
    det = FudkoiDetector()
    assert det.threshold("M") == 100.0
    assert det.threshold("N") == 5.0


def test_a_living_signal_retires_when_the_reward_falls_under_the_gate_and_says_so():
    """A signal that quietly vanished is indistinguishable from one that was never taken."""
    det = FudkiiRtDetector()
    t0 = 1_700_000_000
    det.adopt(
        {
            "signal_id": "FUDKII-RELIANCE-1-B",
            "symbol": "RELIANCE",
            "scrip_code": "2885",
            "direction": "BULLISH",
            "entry": 100.0,
            "stop": 98.0,
            "targets": [110.0],
            "grade": "A",
        },
        t0,
    )
    assert len(det.living) == 1

    # Past the re-eval interval, price has run to 109: only 0.5R of reward left, under the 1.0R gate.
    out = det.on_bar(_bar(int(t0 + 400), 109.0, tf="1m"))
    assert [a.kind for a in out] == ["EXPIRED"]
    assert "gate" in out[0].reason
    assert out[0].evidence["rrLeft"] == 0.5
    assert not det.living, "a retired signal must not linger"


def test_a_living_signal_expires_on_its_ttl():
    det = FudkiiRtDetector()
    t0 = 1_700_000_000
    det.adopt(
        {
            "signal_id": "s1", "symbol": "RELIANCE", "scrip_code": "2885",
            "direction": "BULLISH", "entry": 100.0, "stop": 98.0, "targets": [110.0],
        },
        t0,
    )
    out = det.on_bar(_bar(int(t0 + 2101), 101.0, tf="1m"))  # ttl is 2100s
    assert out[0].kind == "EXPIRED"
    assert "TTL" in out[0].reason


def test_cooldown_and_daily_cap_are_bar_clock_not_wall_clock():
    cd = Cooldown(600)
    assert cd.ready("X", 1000)
    cd.stamp("X", 1000)
    assert not cd.ready("X", 1500)
    assert cd.ready("X", 1600)

    cap = DailyCap(per_symbol=2, global_cap=3)
    for _ in range(2):
        assert cap.allows("X", "2026-09-22")
        cap.take("X", "2026-09-22")
    assert not cap.allows("X", "2026-09-22"), "per-symbol cap"
    assert cap.allows("Y", "2026-09-22")
    cap.take("Y", "2026-09-22")
    assert not cap.allows("Z", "2026-09-22"), "global cap"
    assert cap.allows("X", "2026-09-23"), "a new day resets both"


def test_detectors_cannot_reach_the_gateway():
    """Advisory means advisory: nothing in the alerts package may import exec or venue."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "kotsin_nse" / "alerts"
    for f in src.glob("*.py"):
        text = f.read_text()
        assert "from ..exec" not in text and "from ..venue" not in text, f
        assert "place_order" not in text, f


def test_a_noise_stop_is_declined_however_rich_its_reward():
    """The measured failure: grade A was worst (-1.73R) because high R:R came from a tiny stop.

    DRREDDY, live on 2026-09-22: entry 1210.70, stop 1208.70, ATR 5.93 — 0.34 ATR away — graded A
    at 7.40R. The CTA must decline that, not headline it.
    """
    from kotsin_nse.alerts.plan import TradePlan, cta

    plan = TradePlan(
        entry=1210.7, stop=1208.7, targets=[1225.5], rr=7.4, grade="A", atr=5.93,
        option_type="CE", strike=1220.0, strike_interval=10.0, delta=0.48,
        fortress=9.0, room_atr=11.56, stop_zone="1d.FIB_R1", target_zones=[], note="",
        listed=None,
    )
    out = cta(plan, score=82.0, kind="TRIGGER")
    assert out["action"] == "AVOID"
    assert "0.34 ATR" in out["text"]

    # The same geometry with a stop a full ATR out is allowed through.
    roomy = TradePlan(
        entry=1210.7, stop=1203.0, targets=[1240.0], rr=3.8, grade="A", atr=5.93,
        option_type="CE", strike=1220.0, strike_interval=10.0, delta=0.48,
        fortress=9.0, room_atr=4.0, stop_zone="1d.S1", target_zones=["1d.R1"], note="",
        listed=None,
    )
    assert cta(roomy, score=82.0, kind="TRIGGER")["action"] == "PRIMARY"
