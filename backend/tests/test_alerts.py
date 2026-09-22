"""The ported books, as detectors. Advisory by construction, and they must stay that way."""

from __future__ import annotations

from kotsin_nse.alerts.detectors import (
    BB_BOOKS,
    Alert,
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


def test_alerts_cannot_place_an_order():
    """Advisory means advisory. The invariant is "cannot reach a broker", not "cannot import".

    This deliberately allows ``exec.paper.walk_book``: it is pure fill arithmetic — it imports only
    domain and risk.costs and contains no order call — and the entry model must walk the ladder the
    *same* way the paper matcher does. A second implementation of the fill would drift from the one
    that books the trade, which is the failure this reuse prevents. What stays banned is the
    gateway, the live executor and the venue.
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "kotsin_nse" / "alerts"
    banned = ("..exec.gateway", "..exec.live", "..venue", "place_order", "square_off")
    for f in src.glob("*.py"):
        text = f.read_text()
        for token in banned:
            assert token not in text, f"{f.name} reaches for {token}"



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


def test_a_keepalive_carries_the_parent_signals_ladder_and_option_leg():
    """All 22 FUDKII-RT rows on 2026-09-22 had no plan and no OTM strike, because _enrich returned
    early for anything that was not a TRIGGER. A living signal has an entry, a stop and a target —
    there was never a reason to drop them."""
    from kotsin_nse.alerts.plan import from_living

    plan = from_living(
        entry=100.0, stop=98.0, target=110.0, direction="BULLISH", atr_value=4.0, listed=None
    )
    assert plan.targets == [110.0]
    assert plan.rr == 5.0
    assert plan.option_type == "CE"
    # getStrikeInterval is a strict `price > 100`, so a spot of exactly 100 sits on the 1.0
    # rung, not the 2.5 one. Ported verbatim, boundary included.
    assert plan.strike_interval == 1.0
    assert plan.strike == 101.0, "one ladder step OTM of a 100 spot"
    assert plan.note.startswith("ladder inherited")

    bear = from_living(
        entry=100.0, stop=102.0, target=90.0, direction="BEARISH", atr_value=4.0
    )
    assert bear.option_type == "PE"
    assert bear.strike == 99.0

    # Just above the boundary the rung changes, which is the behaviour worth pinning.
    above = from_living(
        entry=101.0, stop=99.0, target=110.0, direction="BULLISH", atr_value=4.0
    )
    assert above.strike_interval == 2.5


def test_a_keepalive_is_never_presented_as_an_entry():
    """A re-check every five minutes must not read as a fresh entry five minutes apart."""
    from kotsin_nse.alerts.plan import cta, from_living

    plan = from_living(entry=100.0, stop=98.0, target=110.0, direction="BULLISH", atr_value=4.0)
    out = cta(plan, score=80.0, kind="KEEPALIVE")
    assert out["action"] == "HOLD"
    assert "not a new entry" in out["text"]
    assert "100.00" in out["text"], "it must name the price actually entered at"

    # And a tight inherited stop still gets called out, since R is measured against it.
    tight = from_living(entry=100.0, stop=99.9, target=110.0, direction="BULLISH", atr_value=4.0)
    assert "artefact" in cta(tight, score=80.0, kind="KEEPALIVE")["text"]

    assert cta(plan, score=80.0, kind="EXPIRED")["action"] == "STAND_DOWN"


def test_an_alert_carries_the_bar_start_the_bar_close_and_the_moment_it_fired():
    """The card was showing bar-start as the alert time: a 14:45 firing read as 14:15:00.

    Half an hour early, and never with a second on it, because a bar timestamp is a bucket
    boundary and not a moment. Three fields, three facts.
    """
    import time as _t

    from kotsin_nse.alerts.engine import AlertEngine

    eng = AlertEngine.__new__(AlertEngine)
    eng.alerts, eng.counts = {}, {}

    bucket_start = 1_790_066_700  # 14:15 IST
    a = Alert(
        book="PIVOTBOSS", symbol="BHEL", scrip_code="1", tf="30m", ts=bucket_start,
        direction="BULLISH", score=50.0, reason="", price=1.0,
    )
    before = _t.time()
    eng._emit(a)

    assert a.ts == bucket_start, "the bar keeps its own identity"
    assert a.bar_close == bucket_start + 1800, "a 30m bucket closes 30 minutes after it opens"
    assert a.fired_at >= before, "and the firing is stamped when it actually happened"
    assert a.fired_at % 1 != 0 or a.fired_at > bucket_start, "fired_at is wall clock, not a bucket"

    j = a.to_json()
    assert j["barClose"] == bucket_start + 1800
    assert j["firedAt"] == a.fired_at


def test_a_one_minute_book_closes_one_minute_after_it_opens():
    from kotsin_nse.alerts.engine import AlertEngine

    eng = AlertEngine.__new__(AlertEngine)
    eng.alerts, eng.counts = {}, {}
    a = Alert(
        book="FUDKII_RT", symbol="X", scrip_code="1", tf="1m", ts=1_790_066_700,
        direction="BULLISH", score=1.0, reason="", price=1.0, kind="KEEPALIVE",
    )
    eng._emit(a)
    assert a.bar_close == 1_790_066_760


def test_an_entry_is_priced_off_the_ask_ladder_not_the_last_trade():
    """A buy lifts the ask and walks it. Reporting the LTP as the entry understates cost on every
    trade in the same direction — and the measured round trip is already 0.299%."""
    from types import SimpleNamespace

    from kotsin_nse.alerts.entry import model

    now = 1_790_000_000.0
    quote = SimpleNamespace(ltp=10.0, bid=9.8, ask=10.4, ts=now - 1)
    book = SimpleNamespace(
        asks=[(10.4, 100), (10.6, 400), (11.0, 5000)], bids=[(9.8, 500)], ts=now - 1
    )
    e = model(
        now=now, bar_close=now - 2, fired_at=now - 1.2, underlying_ltp=1500.0,
        quote=quote, book=book, lot_size=500,
    )
    assert e.fill is not None and e.fill > quote.ltp, "an entry costs more than the last trade"
    assert e.fill_source == "ladder"
    # 100 + 400 fills 500 exactly, so the 11.00 rung is never touched.
    assert e.levels_walked == 2, "one lot of 500 does not fit on a 100-lot touch"
    # 100@10.4 + 400@10.6 = 5280 for 500 -> 10.56
    assert round(e.fill, 2) == 10.56
    assert e.slippage_vs_ltp_pct is not None and e.slippage_vs_ltp_pct > 5
    assert e.notional == 10.56 * 500
    assert e.lag_from_bar_close_s == 2.0
    assert not e.stale


def test_a_stale_quote_yields_no_entry_price_at_all():
    """A premium from a minute ago is not the premium now, and a confident wrong number is worse
    than an absent one — the same judgement position_quote_max_age_s already encodes."""
    from types import SimpleNamespace

    from kotsin_nse.alerts.entry import model

    now = 1_790_000_000.0
    stale = SimpleNamespace(ltp=10.0, bid=9.8, ask=10.4, ts=now - 120)
    e = model(
        now=now, bar_close=now - 2, fired_at=now - 1, underlying_ltp=1500.0,
        quote=stale, book=None, lot_size=500,
    )
    assert e.stale
    assert e.fill is None
    assert "120s old" in e.note

    never = model(
        now=now, bar_close=now - 2, fired_at=now - 1, underlying_ltp=1500.0,
        quote=None, book=None, lot_size=500,
    )
    assert never.fill is None
    assert never.note == "no quote for this contract"


def test_a_thin_ladder_falls_back_to_the_touch_and_says_so():
    from types import SimpleNamespace

    from kotsin_nse.alerts.entry import model

    now = 1_790_000_000.0
    e = model(
        now=now, bar_close=now - 2, fired_at=now - 1, underlying_ltp=1500.0,
        quote=SimpleNamespace(ltp=10.0, bid=9.8, ask=10.4, ts=now),
        book=None, lot_size=500,
    )
    assert e.fill == 10.4, "no depth — the touch is the honest price"
    assert "no live depth" in e.fill_source


def test_an_exit_sells_into_the_bid_and_is_priced_per_quantity():
    """Selling is not buying in reverse: it hits the bid, and one lot and three lots walk to
    different depths, so they are different prices — not one average describing neither."""
    from types import SimpleNamespace

    from kotsin_nse.alerts.entry import exit_walk

    now = 1_790_000_000.0
    quote = SimpleNamespace(ltp=10.0, bid=9.8, ask=10.4, ts=now)
    # 9.45 is inside the 5% ladder ceiling below the 9.80 touch; a 9.00 rung would not be, and
    # walk_book refuses to model a fill that far through the book.
    book = SimpleNamespace(
        bids=[(9.8, 500), (9.6, 400), (9.45, 5000)], asks=[(10.4, 500)], ts=now
    )
    one = exit_walk(quote=quote, book=book, lot_size=500, lots=1, now=now)
    three = exit_walk(quote=quote, book=book, lot_size=500, lots=3, now=now)

    assert one.fill == 9.8 and one.levels_walked == 1, "one lot clears the touch"
    # 500@9.80 + 400@9.60 + 600@9.45 = 14,410 for 1500 -> 9.6067
    assert round(three.fill, 3) == 9.607
    assert three.fill < one.fill, "size pays for depth"
    assert three.levels_walked == 3
    assert one.slippage_vs_mid_pct is not None and one.slippage_vs_mid_pct < 0, "a sale realises below mid"
    assert three.proceeds is not None and three.proceeds > 0
    assert not three.capped, "every rung taken sits inside the ceiling"


def test_a_thin_bid_book_reports_a_partial_rather_than_a_confident_price():
    from types import SimpleNamespace

    from kotsin_nse.alerts.entry import exit_walk

    now = 1_790_000_000.0
    thin = SimpleNamespace(bids=[(9.8, 100)], asks=[(10.4, 100)], ts=now)
    out = exit_walk(
        quote=SimpleNamespace(ltp=10.0, bid=9.8, ask=10.4, ts=now),
        book=thin, lot_size=500, lots=3, now=now,
    )
    assert out.capped
    assert "too thin" in out.source, "a size the market will not take is the number worth seeing"


def test_a_stale_bid_book_falls_back_to_the_touch_and_says_so():
    from types import SimpleNamespace

    from kotsin_nse.alerts.entry import exit_walk

    now = 1_790_000_000.0
    out = exit_walk(
        quote=SimpleNamespace(ltp=10.0, bid=9.8, ask=10.4, ts=now),
        book=SimpleNamespace(bids=[(9.8, 5000)], asks=[], ts=now - 300),
        lot_size=500, lots=1, now=now,
    )
    assert out.fill == 9.8 and "no live depth" in out.source
