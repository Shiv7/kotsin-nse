"""The 2026-11-01 end-to-end review: one test per engine-side defect it fixed, each written to
fail on the code as it was. The research-side fixes have their tests beside their modules."""

from __future__ import annotations

from datetime import date

import pytest

from kotsin_nse.analysis.dayread import Band, cpr, level_events
from kotsin_nse.bars.aggregator import Aggregator
from kotsin_nse.bars.store import BarStore
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Instrument, InstrumentKind, OrderIntent, OrderSide, Purpose
from kotsin_nse.exec.gateway import Decision, Gateway, Mode
from kotsin_nse.exec.paper import PaperMatcher
from kotsin_nse.fno.chain import OptQuote, StrikeRow, read_chain
from kotsin_nse.market.session import (
    SESSIONS,
    continuous_end_ts,
    daily_final_ts,
    ist_at,
    session_open_ts,
)
from kotsin_nse.orderflow.meter import FlowMeter, classify
from kotsin_nse.risk.costs import CostModel
from kotsin_nse.strategy import cpr_retest as cr
from kotsin_nse.structure.engine import Bar5, analyze, resample_5m

DAY = date(2026, 10, 30)
OPEN = int(session_open_ts(Segment.NSE_EQ, DAY))


def _tick(code: str, ts: float, total: int, ltp: float) -> dict:
    return {"scrip_code": code, "ltp": ltp, "total_qty": total, "ts": ts}


# -- the session clock ---------------------------------------------------------------------------------


def test_the_session_knows_where_continuous_trading_ends_and_when_the_daily_candle_is_final():
    eq = SESSIONS[Segment.NSE_EQ]
    assert (eq.continuous_end.hour, eq.continuous_end.minute) == (15, 15)  # the closing auction follows
    assert continuous_end_ts(Segment.NSE_EQ, DAY) == ist_at(DAY, "15:15")
    assert daily_final_ts(Segment.NSE_EQ, DAY) == ist_at(DAY, "15:45")  # after the auction and F&O's 15:40
    mcx = SESSIONS[Segment.MCX_FO]
    assert mcx.continuous_end == mcx.close == mcx.daily_final  # no auction there
    assert not eq.contains(eq.close)  # half-open, like every bucket


# -- bars ---------------------------------------------------------------------------------------------------


async def test_a_tick_for_a_bucket_the_clock_already_closed_is_late_not_a_new_bar(equity):
    """Feed latency after the 1 Hz flush: a frame stamped :59 arriving at :00.9 used to open a
    one-print bar for the closed bucket, and the next tick closed that stub OVER the full bar."""
    store = BarStore()
    agg = Aggregator(store, timeframes=("1m",))
    agg.track(equity)
    t = OPEN + 60 * 49  # the 10:04 bucket
    await agg.on_tick(_tick(equity.scrip_code, t + 1, 100, 100.0))
    await agg.on_tick(_tick(equity.scrip_code, t + 20, 120, 102.0))
    await agg.on_tick(_tick(equity.scrip_code, t + 40, 130, 99.0))
    await agg.flush_stale(now=t + 60.5)  # the clock closes 10:04
    full = store.last(equity.symbol, "1m")
    assert full is not None and (full.open, full.high, full.low, full.close, full.volume) == (100.0, 102.0, 99.0, 99.0, 30.0)
    await agg.on_tick(_tick(equity.scrip_code, t + 59.4, 135, 97.0))  # stamped inside 10:04, delivered late
    await agg.on_tick(_tick(equity.scrip_code, t + 61, 140, 97.5))  # 10:05 opens and would close 10:04 again
    kept = store.bars(equity.symbol, "1m")
    assert [b.ts for b in kept] == [t] and (kept[0].high, kept[0].low, kept[0].volume) == (102.0, 99.0, 30.0)
    assert agg.late_ticks == 1 and agg.bars_closed == 1


async def test_frames_after_the_end_of_continuous_trading_never_become_bars(equity):
    """From 15:15 the cash feed carries the closing auction's indicative quantity in TotalQty; booked
    as deltas it was hundreds of thousands of phantom shares at a flat price in the 15:1x bars."""
    store = BarStore()
    agg = Aggregator(store, timeframes=("1m", "30m", "1d"))
    agg.track(equity)
    t = ist_at(DAY, "15:14")
    await agg.on_tick(_tick(equity.scrip_code, t + 5, 1_000_000, 100.0))
    await agg.on_tick(_tick(equity.scrip_code, t + 30, 1_000_500, 100.2))
    for i, total in enumerate((40_000, 95_000, 180_000, 420_000)):  # the auction's indicative totals
        await agg.on_tick(_tick(equity.scrip_code, ist_at(DAY, "15:16") + 60 * i, total, 100.2))
    assert agg.auction_ticks == 4
    await agg.flush_stale(now=ist_at(DAY, "15:31"))
    assert [b.ts for b in store.bars(equity.symbol, "1m")] == [t]
    day = store.bars(equity.symbol, "1d")[-1]
    assert day.volume == 500.0 and day.close == 100.2
    assert all(b.volume == 500.0 for b in store.bars(equity.symbol, "30m"))


async def test_a_frame_stamped_on_an_earlier_day_does_not_roll_the_day_backwards(equity):
    store = BarStore()
    agg = Aggregator(store, timeframes=("1m",))
    agg.track(equity)
    t = OPEN + 60 * 100
    await agg.on_tick(_tick(equity.scrip_code, t, 9_000_000, 100.0))
    await agg.on_tick(_tick(equity.scrip_code, t + 10, 9_000_100, 100.0))
    stale = _tick(equity.scrip_code, t - 86_400, 50, 98.0)  # yesterday's stamp, after a reconnect
    await agg.on_tick(stale)
    await agg.on_tick(_tick(equity.scrip_code, t + 20, 9_000_150, 100.1))
    bar = store.forming(equity.symbol, "1m")
    assert bar is not None and bar.volume == 150.0  # not 9,000,150
    assert agg.late_ticks == 1 and agg.state[equity.scrip_code].day == DAY.isoformat()


def test_a_live_close_never_overwrites_the_exchanges_candle_in_the_store():
    store = BarStore()
    rest = UnifiedBar("ABC", "1", "5m", OPEN, 100.0, 101.0, 99.0, 100.5, 1000.0, source=BarSource.REST, complete=True)
    store.replace_closed(rest)
    live = UnifiedBar("ABC", "1", "5m", OPEN, 100.3, 100.4, 100.2, 100.3, 12.0, source=BarSource.LIVE)
    store.close(live)
    kept = store.last("ABC", "5m")
    assert kept is rest and kept.volume == 1000.0


def test_resampling_drops_pre_open_rows_and_repeated_minutes():
    rows = [(OPEN - 60, 99.0, 99.5, 98.5, 99.0, 50.0), (OPEN, 100.0, 101.0, 99.5, 100.5, 10.0), (OPEN, 100.0, 101.0, 99.5, 100.5, 10.0), (OPEN + 60, 100.5, 102.0, 100.0, 101.5, 20.0)]
    out = resample_5m(rows, OPEN)
    assert len(out) == 1 and out[0].ts == OPEN and out[0].o == 100.0 and out[0].v == 30.0


# -- order flow ---------------------------------------------------------------------------------------------


def test_the_quote_rule_classifies_nothing_against_a_crossed_previous_quote():
    """The pre-open auction's crossed book (RELIANCE 1302.9 / 1066.1): against it every price is
    'at or above the ask', and the opening match was booked as a buy every day."""
    assert classify(1184.5, 1302.9, 1066.1, 1184.5, 0) == 0  # crossed quote, unchanged price: unknown
    assert classify(1184.5, 1302.9, 1066.1, 1180.0, 0) == 1  # crossed quote → the tick rule decides (up-tick)
    assert classify(1175.0, 1302.9, 1066.1, 1180.0, 0) == -1  # … and a down-tick is a sell, not "at the ask"
    assert classify(1184.5, 1184.4, 1184.5, 1180.0, 0) == 1  # a real quote: at the ask → buy


def test_the_first_regular_frame_after_the_auction_uses_the_tick_rule():
    m = FlowMeter()
    ts = ist_at(DAY, "09:14") + 50
    m.on_tick("1", ts, 1180.0, 100_000, 1302.9, 1066.1, auction=True)  # the crossed pre-open quote
    m.on_tick("1", ts + 70, 1184.5, 102_000, 1184.4, 1184.6)  # opens UP from 1180 at a real quote
    first = m.minutes("1")[-1]
    assert first.buy == 2000 and first.sell == 0 and first.unknown == 0  # up-tick → buy, not "at the auction's ask"
    m2 = FlowMeter()
    m2.on_tick("1", ts, 1180.0, 100_000, 1302.9, 1066.1, auction=True)
    m2.on_tick("1", ts + 70, 1175.0, 102_000, 1174.9, 1175.1)  # opens DOWN
    assert m2.minutes("1")[-1].sell == 2000


def test_restoring_the_archive_shifts_the_live_minutes_onto_its_cumulative_delta():
    """The restore lands after the feed has been live for a while; the live minutes started their
    CVD at zero and the day read +N up to the restart and ~0 after it."""
    m = FlowMeter()
    t_live = ist_at(DAY, "11:00")
    m.on_tick("1", t_live, 100.0, 50_000, 99.95, 100.05)
    m.on_tick("1", t_live + 10, 100.05, 50_500, 99.95, 100.05)  # +500 at the ask
    rows = [{"code": "1", "ts": ist_at(DAY, "10:58"), "buy": 40_000, "sell": 0, "unknown": 0, "frames": 3, "cum_delta": 40_000}]
    assert m.restore(rows, int(t_live // 86400)) == 1  # the meter's day is the UTC day, as the engine passes it
    mins = m.minutes("1")
    assert [x.cum_delta for x in mins] == [40_000, 40_500]
    m.on_tick("1", t_live + 70, 100.0, 50_600, 99.95, 100.05)  # the next minute continues from there
    assert m.minutes("1")[-1].cum_delta == 40_500 - 100


# -- the exit plan and the costs ----------------------------------------------------------------------------


def test_a_bar_that_opens_through_the_stop_fills_at_the_open():
    p = cr.ExitPlan.open("fixed_wall", 1, 100.0, 98.0, [105.0], 1.0)
    assert p.step(99.0, 96.0, 97.0, open=96.5) == (96.5, "stop") and p.gapped  # the gap is paid
    q = cr.ExitPlan.open("fixed_wall", 1, 100.0, 98.0, [105.0], 1.0)
    assert q.step(99.5, 97.5, 99.0, open=99.2) == (98.0, "stop") and not q.gapped  # touched inside the bar: the stop price
    s = cr.ExitPlan.open("fixed_wall", -1, 100.0, 102.0, [95.0], 1.0)
    assert s.step(104.0, 102.5, 103.0, open=103.5) == (103.5, "stop") and s.gapped
    rt = cr.ExitPlan.from_state(p.state())
    assert rt.done == p.done and rt.gapped and rt.walls == p.walls


def test_a_short_pays_stt_on_its_entry_and_a_scaled_exit_pays_every_order(equity):
    costs = CostModel(Settings(_env_file=None))
    long_ = costs.round_trip(equity, 100.0, 110.0, 100)
    short = costs.round_trip(equity, 110.0, 100.0, 100, entry_side=OrderSide.SELL)
    assert long_.stt == pytest.approx(110.0 * 100 * 0.025 / 100) and long_.stamp == pytest.approx(100.0 * 100 * 0.003 / 100)
    assert short.stt == pytest.approx(110.0 * 100 * 0.025 / 100) and short.stamp == pytest.approx(100.0 * 100 * 0.003 / 100)
    three = costs.trade(equity, OrderSide.BUY, 100.0, [(105.0, 50), (110.0, 50)])
    assert three.brokerage == pytest.approx(3 * 40.0) and three.total > costs.round_trip(equity, 100.0, 107.5, 100).total


def test_stamp_duty_and_stt_follow_the_product():
    s = Settings(_env_file=None)
    costs = CostModel(s)
    fut = Instrument(scrip_code="9", symbol="ABC", segment=Segment.NSE_FO, kind=InstrumentKind.FUTURE, name="ABC FUT", lot_size=500, tick_size=0.05, multiplier=1, underlying="ABC")
    eq = Instrument(scrip_code="1", symbol="ABC", segment=Segment.NSE_EQ, kind=InstrumentKind.EQUITY, name="ABC", lot_size=1, tick_size=0.05, multiplier=1, underlying="ABC")
    assert costs.leg(fut, OrderSide.BUY, 100.0, 500).stamp == pytest.approx(100.0 * 500 * 0.002 / 100)
    delivery = costs.leg(eq, OrderSide.BUY, 100.0, 100, delivery=True)
    assert delivery.stamp == pytest.approx(100.0 * 100 * 0.015 / 100) and delivery.stt == pytest.approx(100.0 * 100 * 0.1 / 100)
    assert s.cost_exchange_txn_pct_option == 0.03503 and s.positional_enabled is False


def test_the_entry_cutoff_must_be_two_digit_hh_mm():
    with pytest.raises(ValueError):
        Settings(_env_file=None, strategy_entry_cutoff="9:30")  # "9:30" > "14:25" as a string: every entry skipped
    assert Settings(_env_file=None, strategy_entry_cutoff="").strategy_entry_cutoff == ""


# -- the gateway --------------------------------------------------------------------------------------------


def _gateway(mode: Mode, halted: bool = False) -> Gateway:
    costs = CostModel(Settings(_env_file=None))
    return Gateway(matcher=PaperMatcher(costs), mode=lambda: mode, halted=lambda: (halted, "operator halt"), book_for=lambda _c: None, ltp_for=lambda _c: None)


def _intent(inst: Instrument, coid: str, purpose: Purpose = Purpose.ENTRY) -> OrderIntent:
    return OrderIntent(strategy="T", instrument=inst, side=OrderSide.BUY, qty=10, purpose=purpose, signal_id="s", client_order_id=coid, ref_price=100.0)


def test_a_halt_neither_spends_the_idempotency_key_nor_trips_the_breaker(equity):
    gw = _gateway(Mode.SHADOW, halted=True)
    for _ in range(5):
        assert gw.submit(_intent(equity, "c1")).decision is Decision.REJECTED_HALT
    assert gw.consecutive_rejects == 0 and not gw.breaker_tripped
    gw._halted = lambda: (False, "")
    assert gw.submit(_intent(equity, "c1")).decision is Decision.SHADOW_OK  # the same id goes through once the halt lifts
    assert gw.submit(_intent(equity, "c1")).decision is Decision.DUP_BLOCKED


def test_a_live_mode_without_a_live_route_is_loud(equity):
    gw = _gateway(Mode.LIVE_CAPPED)
    res = gw.submit(_intent(equity, "c1", Purpose.EXIT))
    assert res.decision is Decision.REJECTED_BROKER and "not wired" in res.order.note and gw.stats()["unrouted_live_intents"] == 1


# -- the F&O board ------------------------------------------------------------------------------------------


def test_walls_are_one_sided():
    """An in-the-money call pile below the future was the 'call wall' and went to the hedge
    setups as resistance; the call wall is the biggest call OI at or above the future."""
    q = lambda oi, ltp: OptQuote(ltp=ltp, prev_close=ltp, oi=oi, oi_ref=oi)  # noqa: E731
    rows = [
        StrikeRow(9400, q(900_000, 150.0), q(10_000, 5.0)),
        StrikeRow(9500, q(100_000, 70.0), q(120_000, 60.0)),
        StrikeRow(9600, q(400_000, 25.0), q(50_000, 130.0)),
        StrikeRow(9700, q(300_000, 8.0), q(800_000, 220.0)),
    ]
    cr_ = read_chain(rows, future=9500.0, future_prev=9480.0, t=7 / 365, t_prev=8 / 365)
    assert cr_.view["call_wall"]["strike"] == 9600 and cr_.view["put_wall"]["strike"] == 9500


def test_the_daily_positioning_reads_price_and_oi_within_a_contract_across_the_roll(tmp_path):
    import pandas as pd

    from kotsin_nse.analysis.positioning import positioning
    from kotsin_nse.market.fo_bhavcopy import OiDailyStore

    store = OiDailyStore(tmp_path)

    def fut(expiry, close, oi, chg):
        return {"scrip_code": expiry, "symbol": "ABC", "kind": "FUTSTK", "expiry": expiry, "strike": 0, "option_type": "", "close": close, "settle": close, "oi": oi, "oi_change": chg, "volume": 1}

    store.write(date(2026, 10, 26), pd.DataFrame([fut("2026-10-27", 100.0, 1000, 0), fut("2026-11-24", 101.0, 100, 0)]))
    store.write(date(2026, 10, 27), pd.DataFrame([fut("2026-10-27", 100.0, 1000, 0), fut("2026-11-24", 101.0, 110, 10)]))  # expiry day
    store.write(date(2026, 10, 28), pd.DataFrame([fut("2026-11-24", 101.0, 1110, 1000)]))  # the day after: one contract, same total OI, same price
    out = positioning(store, "ABC", date(2026, 10, 29))
    assert out is not None
    rows = {r["day"]: r for r in out["sessions"]}
    # price unchanged within the November contract and summed OI unchanged → FLAT, not "LB by the roll premium"
    assert rows["2026-10-28"]["build_up"] == "FLAT"
    assert rows["2026-10-27"]["build_up"] == "LB"  # +0.9 % OI with the near contract flat (price "up" on ≥)


# -- structure and the day read -----------------------------------------------------------------------------


def _b(rows, t0=OPEN):
    return [Bar5(t0 + 300 * i, o, h, lo, c, 100) for i, (o, h, lo, c) in enumerate(rows)]


def test_a_zone_judged_at_its_bar_does_not_change_when_the_afternoon_is_appended():
    """The displacement test and the stop buffer used the mean candle of the input's LAST twenty
    bars: the same 10:00 zone appeared and vanished between two as-of calls."""
    rows = [
        (100.0, 101.0, 99.5, 100.8), (100.8, 102.0, 100.5, 101.8), (101.8, 103.0, 101.5, 102.5),
        (102.5, 102.6, 101.0, 101.2), (101.2, 101.5, 100.2, 100.4), (100.4, 102.8, 100.4, 102.6),
        (102.6, 104.5, 102.9, 104.3), (104.3, 104.6, 103.5, 103.8), (103.8, 103.9, 101.3, 101.6),
    ]
    morning = analyze(_b(rows), adr=4.0)
    assert morning.zones, "the fixture must form a zone"
    big = [(104.0, 140.0, 104.0, 130.0)] * 25  # huge afternoon candles that never close under the zone
    whole = analyze(_b(rows + big), adr=4.0)
    a, b = morning.zones[0], whole.zones[0]
    assert (a.ob_i, a.bos_i, a.z, a.s1, a.impulse) == (b.ob_i, b.bos_i, b.z, b.s1, b.impulse)


def test_a_break_right_after_a_hold_is_reported_and_a_two_bar_break_counts():
    lv = cpr(110.0, 100.0, 104.0)  # band 104.33–105.00
    band = [Band("CPR", min(lv["BC"], lv["TC"]), max(lv["BC"], lv["TC"]))]
    held_then_broke = [(106.0, 106.5, 105.2, 106.2), (106.2, 106.3, 104.9, 105.3), (105.3, 105.4, 103.8, 103.9)]
    kinds = [e.kind for e in level_events(_b(held_then_broke), band, tol=0.5)]
    assert kinds == ["SUPPORT", "BREAK_DOWN"]  # the break within two tolerances of the hold was silent before
    two_bar = [(103.0, 103.5, 102.8, 103.0), (103.0, 105.2, 102.9, 105.1), (105.1, 106.2, 105.0, 106.0), (106.0, 106.4, 105.1, 105.4), (105.4, 107.0, 105.3, 106.8)]
    kinds = [e.kind for e in level_events(_b(two_bar), band, tol=0.5)]
    assert kinds == ["BREAK_UP"]  # the close at the line then through it is one break; the hold-and-resume after it is not a second


def test_the_sequence_windows_are_bar_positions_not_list_indices():
    """A missing opening bucket shifted every window by one bar; measured from the session open a
    hole shifts nothing, and on a complete day positions equal indices."""
    lv = cpr(110.0, 100.0, 104.0)
    tol = 1.0
    rows = [
        (102.0, 102.4, 101.5, 101.8), (101.8, 102.6, 100.2, 101.0), (101.0, 102.8, 100.8, 102.3), (102.3, 102.5, 101.4, 101.6),
        (101.6, 103.5, 101.5, 103.3), (103.3, 104.0, 103.0, 103.8), (103.8, 106.5, 103.7, 106.2), (106.2, 106.8, 105.9, 106.4),
    ]
    full = _b(rows)
    complete = cr.sequence(full, lv, tol, open_ts=OPEN)
    assert complete == cr.sequence(full, lv, tol)
    holed = full[:1] + full[2:]  # the 09:20 bucket never arrived
    st = cr.sequence(holed, lv, tol, open_ts=OPEN)[1]
    assert st.i_os is not None  # the open test is still inside the first six positions
