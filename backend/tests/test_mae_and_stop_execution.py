"""MAE on what the position could be sold for, and a stop that sells at its trigger (operator, 2026-10-04).

The finding: 47 of 120 closed trades had filled BELOW their MAE price (median 2.75 % of the premium). Two
causes, both fixed here and each pinned below:

* MFE / MAE were marked on the option's LAST TRADE, and only on a fresh quote; a stop sells into the BID,
  and the fill itself was never folded in. Now: the bid when there is one, the last trade when not, and
  every fill — so the exit is never below the MAE nor above the MFE. ``peak_r`` (the trail's input) stays
  on the last trade: a reporting fix moves no exit rule.
* a calm option stop RESTED a limit at the mid for up to 15 s after its rule fired, then crossed at
  whatever bid was left (DMART, 2026-10-01: trigger 109.00, bid 107.10, fill 94.28 fifteen seconds
  later). The engine's stop is a software stop — a 1 s read of the level, then an order — so what it can
  execute at the trigger is the bid, walked through the depth for the lots. Now every stop sells there,
  at once, and its record keeps the LEVEL, the breaching READ, the BID / walk price at that instant and
  the FILL apart (``Engine._stop_record``). The trail, the targets and the close keep their walk.
"""

from __future__ import annotations

import time
from datetime import datetime
from datetime import time as dtime

import httpx
import pytest

from kotsin_nse.api.routes import build_app
from kotsin_nse.config import Settings
from kotsin_nse.domain import Direction, ExitDecision, ExitReason, Position, PosSide
from kotsin_nse.engine import Engine, _trade_from, _trade_json
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.instrument.select import Quote
from kotsin_nse.market.session import IST, ist_today
from kotsin_nse.risk.exits import ExitEngine, MarketView, apply_exit
from kotsin_nse.risk.limits import RiskLimits
from tests.test_limit_orders import OPT, UND, _book, _engine, _rows


def _at(h: int, m: int, s: int = 0) -> float:
    return datetime.combine(ist_today(), dtime(h, m, s), tzinfo=IST).timestamp()


@pytest.fixture
def clock(monkeypatch):
    now = [_at(11, 0)]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def _pos(now: float, *, pid="p1", strategy="FUDKII", qty=2750, option_sl=9.0, entry=12.0, targets=()) -> Position:
    """Entry 12.00, first stop 9.00: 1R = 3.00, so every R below reads straight off the price."""
    return Position(id=pid, strategy=strategy, instrument=OPT, underlying=UND, side=PosSide.LONG, qty=qty, entry=entry,
                    opened_ts=now - 600, signal_id=f"s-{pid}", direction=Direction.BULLISH, equity_entry=187.0,
                    equity_sl=185.0, option_sl=option_sl, option_targets=targets, qty_remaining=qty)


def _view(now: float, ltp: float, bid: float | None, ask: float | None = None, *, quote_ok: bool = True) -> MarketView:
    mid = (bid + ask) / 2 if bid and ask else ltp
    return MarketView(option_ltp=ltp, underlying_ltp=None, now=now, bars_held=1, past_force_flat=False,
                      option_mid=mid, option_bid=bid, quote_ok=quote_ok)


def _price(pos: Position, r: float) -> float:
    return round(pos.entry + r * pos.r_unit, 2)


def _depth(e: Engine, bids: list[tuple[float, int]], asks: list[tuple[float, int]], now: float, *, ltp: float | None = None) -> None:
    e.books[OPT.scrip_code] = BookSnapshot(OPT.scrip_code, bids=bids, asks=asks, ts=now)
    e.quotes[OPT.scrip_code] = Quote(ltp=ltp or bids[0][0], bid=bids[0][0], ask=asks[0][0], ts=now)
    e.ltps[OPT.scrip_code] = ltp or bids[0][0]


# -- the marks, pure ---------------------------------------------------------------------------------


def test_mfe_and_mae_mark_the_bid_and_peak_r_keeps_the_last_trade():
    """Normal, liquid quotes: the position is worth what it can be sold for — the bid."""
    pos = _pos(1000.0)
    eng = ExitEngine(RiskLimits())
    assert eng.evaluate(pos, _view(1001.0, ltp=12.3, bid=12.15, ask=12.45)) is None  # under the trail's +3 % arm
    assert pos.mfe_r == pytest.approx(0.05), "the bid 12.15, not the print at 12.30"
    assert pos.peak_r == pytest.approx(0.1), "the trail's watermark still reads the last trade"
    assert pos.mae_r == 0.0 and pos.mark_basis == "bid"
    assert eng.evaluate(pos, _view(1002.0, ltp=11.4, bid=11.1, ask=11.7)) is None
    assert pos.mae_r == pytest.approx(-0.3) and _price(pos, pos.mae_r) == 11.1
    assert pos.mfe_r == pytest.approx(0.05) and pos.peak_r == pytest.approx(0.1)
    # no bid on the quote: the last trade is all there is
    assert eng.evaluate(pos, _view(1003.0, ltp=10.8, bid=None)) is None
    assert pos.mae_r == pytest.approx(-0.4)


def test_a_bid_that_dips_and_recovers_between_prints_is_the_mae():
    """Rapidly changing bid and last trade: the print sits still while the bid is pulled and comes back."""
    pos = _pos(1000.0)
    eng = ExitEngine(RiskLimits())
    for t, bid in ((1001.0, 11.9), (1002.0, 11.0), (1003.0, 11.95)):
        assert eng.evaluate(pos, _view(t, ltp=12.0, bid=bid, ask=bid + 0.2)) is None
    assert pos.mae_r == pytest.approx(-1 / 3) and _price(pos, pos.mae_r) == 11.0, "the pulled bid IS the worst sale"
    assert pos.mfe_r == 0.0 and pos.peak_r == 0.0, "a print at entry is no excursion"
    assert eng.evaluate(pos, _view(1004.0, ltp=12.9, bid=12.1, ask=13.0)) is None
    assert pos.mfe_r == pytest.approx(0.1 / 3) and pos.peak_r == pytest.approx(0.3), "MFE on the bid, peak_r on the print"


def test_a_stale_quote_marks_nothing_and_a_fill_is_always_folded_in():
    pos = _pos(1000.0)
    eng = ExitEngine(RiskLimits())
    assert eng.evaluate(pos, _view(1001.0, ltp=12.0, bid=11.8, ask=12.2)) is None
    # the quote has gone stale: the engine judges only the equity stop and the backstops, and marks nothing
    assert eng.evaluate_stale(pos, _view(1100.0, ltp=12.0, bid=None, quote_ok=False)) is None
    assert pos.mae_r == pytest.approx(-0.2 / 3), "no new price was seen: the excursion did not move"
    # a bid passed with a quote the caller says is not ok is not a price either
    assert eng.evaluate(pos, _view(1101.0, ltp=12.0, bid=9.0, ask=9.4, quote_ok=False)) is None
    assert pos.mae_r == pytest.approx(-0.2 / 3)
    # whatever the marks saw, the fill is a price the position WAS sold at
    apply_exit(pos, ExitDecision(pos.id, ExitReason.SL_EQ, 12.0, pos.qty, "stale-path stop"), fill_price=9.6, charges=0.0, now=1102.0)
    assert pos.status == "CLOSED" and pos.mae_r == pytest.approx(-0.8) and _price(pos, pos.mae_r) == pos.exit_price == 9.6


def test_a_trade_without_a_stop_folds_its_target_fill_into_the_mfe_and_carries_no_stop_record():
    pos = _pos(1000.0, targets=(13.5,))
    eng = ExitEngine(RiskLimits())
    assert eng.evaluate(pos, _view(1001.0, ltp=13.2, bid=13.0, ask=13.4)) is None
    apply_exit(pos, ExitDecision(pos.id, ExitReason.TARGET, 13.5, pos.qty, "T1"), fill_price=13.6, charges=0.0, now=1002.0)
    assert pos.mfe_r == pytest.approx(1.6 / 3) and pos.mae_r == 0.0, "a sale at 13.60 is the best sale; the MAE is untouched"
    t = _trade_from(pos, 1002.0)
    assert t.stop == {} and t.mark_basis == "bid" and _trade_json(t)["stop"] == {}


# -- the engine ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_option_stop_sells_into_the_bid_at_its_trigger_and_the_exit_is_never_below_the_mae(settings, clock):
    """The last trade prints AT the stop; the bid is a tick under it and the lots walk two ticks down. The
    fill is the walk price, the record keeps every price apart, and the MAE is the fill."""
    e = await _engine(settings, clock)
    try:
        pos = _pos(clock[0])
        e.positions[pos.id] = pos
        e.wallets["FUDKII"].commit(12.0 * 2750, clock[0])
        _depth(e, [(8.9, 1000), (8.85, 1000), (8.8, 5000)], [(9.5, 5000)], clock[0], ltp=9.0)
        await e._manage_positions()
        assert pos.status == "CLOSED" and e._exit_resting(pos.id) is None, "no limit rested at the mid, no 15 s deadline"
        x = pos.exec_log["exits"][-1]
        assert x["reason"] == "SL-OP" and x["outcome"] == "sold into the bid at once — a stop sells at its trigger"
        assert x["stop"] == {"level": 9.0, "triggerPrice": 9.0, "triggerOn": "option last", "triggerTs": clock[0],
                             "bidAtTrigger": 8.9, "askAtTrigger": 9.5, "executable": 8.85}
        assert x["fillPrice"] == 8.85 == pos.exit_price, "1,000 at 8.90, 1,000 at 8.85, 750 at 8.80"
        assert _price(pos, pos.mae_r) == 8.85 and pos.mark_basis == "bid", "the MAE is the fill, not the 9.00 print"
        (row,) = await _rows(e, "trades")
        assert row["mark_basis"] == "bid" and row["stop"]["level"] == 9.0 and row["stop"]["fill"] == 8.85
        assert row["exit"] >= row["entry"] + row["mae_r"] * row["r_unit"] - 1e-9
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_stale_option_quote_still_takes_the_equity_stop_at_the_depth_bid_and_the_mae_follows(settings, clock):
    """The option's quote is two minutes old (only the equity stop and the backstops are judged); the depth
    socket's book is fresh. The stock breaks its stop: sold at the book's bid at once, the fill folded in."""
    e = await _engine(settings, clock)
    try:
        pos = _pos(clock[0])
        e.positions[pos.id] = pos
        e.wallets["FUDKII"].commit(12.0 * 2750, clock[0])
        _book(e, 11.0, 11.4, clock[0] - 120, ltp=11.2)  # stale: last read 11.20 / bid 11.00
        e.books[OPT.scrip_code] = BookSnapshot(OPT.scrip_code, bids=[(9.6, 50_000)], asks=[(10.4, 50_000)], ts=clock[0])
        e.ltps[UND.scrip_code] = 184.0  # through the 185.00 stop
        await e._manage_positions()
        assert pos.id in e._stale_positions or pos.status == "CLOSED"
        assert pos.status == "CLOSED"
        x = pos.exec_log["exits"][-1]
        assert x["reason"] == "SL-EQ" and x["stop"]["level"] == 185.0 and x["stop"]["triggerOn"] == "underlying"
        assert x["stop"]["triggerPrice"] == 184.0 and x["stop"]["bidAtTrigger"] == 9.6 and x["fillPrice"] == 9.6
        assert _price(pos, pos.mae_r) == 9.6, "the stale 11.00 bid was never the worst sale; the fill was"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_stop_takes_a_resting_target_off_and_sells_at_once_while_the_close_still_rests(settings, clock):
    e = await _engine(settings, clock)
    try:
        pos = _pos(clock[0], strategy="FUDKII_RT_X", qty=5500)
        e.positions[pos.id] = pos
        e.wallets["FUDKII_RT_X"].commit(12.0 * 5500, clock[0])
        _book(e, 18.0, 19.0, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.TARGET, 18.5, 2750, "T1"), clock[0])
        assert e._exit_resting(pos.id).deadline_s == 45, "a target rests and walks, as before"
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 18.5, 5500, "hard floor", level=18.2, trigger_price=18.5,
                                        trigger_on="option mid"), clock[0])
        assert not e._resting and pos.status == "CLOSED" and pos.exit_price == 18.0, "the target's sell came off; the stop sold at the bid"
        assert pos.exec_log["exits"][-1]["stop"]["level"] == 18.2
        eod = _pos(clock[0], pid="p2", strategy="FUDKII_RT_X")
        e.positions[eod.id] = eod
        await e._exit(eod, ExitDecision(eod.id, ExitReason.EOD, 18.5, 2750, "segment force-flat"), clock[0])
        r = e._exit_resting(eod.id)
        assert r is not None and r.deadline_s == 10 and r.stop is None, "the close rests and walks; it has no level to record"
    finally:
        await e.stop()


# -- the ledger as served ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rows_closed_before_the_rule_say_last_and_keep_their_numbers(tmp_path):
    """Historical trades are not rewritten: their MAE stays the last-trade figure (which a stop's fill can sit
    below) and the page says so; a trade marked under the rule says "bid" and carries its stop record."""
    s = Settings(_env_file=None, data_dir=tmp_path, db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db", engine_enabled=False)
    e = Engine(s)
    await e.ledger.init()
    old = {"id": "trd-old", "position_id": "pos-old", "strategy": "FUDKII", "symbol": "DMART 27 OCT 2026 CE 4700.00", "underlying": "DMART",
           "closed_ts": 1790000000.0, "net": -9000.0, "charges": 300.0, "r_multiple": -0.85, "exit_reason": "SL-EQ", "entry": 103.15,
           "exit": 94.28, "qty": 1000, "multiplier": 1, "r_unit": 10.97, "side": "LONG", "mfe_r": 0.1, "mae_r": -0.2416}
    new = {**old, "id": "trd-new", "position_id": "pos-new", "closed_ts": 1790000001.0, "mark_basis": "bid", "mae_r": -0.8085,
           "stop": {"level": 108.04, "triggerPrice": 109.0, "triggerOn": "option mid", "bidAtTrigger": 107.1, "askAtTrigger": 111.3,
                    "executable": 107.1, "fill": 94.28}}
    for t in (old, new):
        await e.ledger.insert_trade(t)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(e)), base_url="http://t") as c:
        rows = {r["id"]: r for r in (await c.get("/api/trades")).json()}
    o, n = rows["trd-old"], rows["trd-new"]
    assert o["mark_basis"] == "last" and o["mae_r"] == -0.2416 and o["mae_price"] == 100.5 and o["exit"] < o["mae_price"]
    assert o.get("stop") in (None, {}), "nothing is invented for a trade that kept no record"
    assert n["mark_basis"] == "bid" and n["mae_price"] == pytest.approx(94.28, abs=0.01) and n["exit"] >= n["mae_price"] - 0.01
    assert n["stop"]["level"] == 108.04 and n["stop"]["bidAtTrigger"] == 107.1 and n["stop"]["fill"] == 94.28
