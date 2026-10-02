"""Operator, 2026-09-28: "execute and make live: RT-Y 25% premium cap on normal trades. keep 'Graded-F
triggers for RT-Y (with the raw-pivot ladder)' as shadow book" — and "keep every day's scrip master …
if [it] has no change, then just log the date … then the scrip changes, again log the date"."""

from __future__ import annotations

import gzip
import json
import time
from dataclasses import replace
from datetime import date

import pytest

from kotsin_nse.api import daybook, shadow
from kotsin_nse.bars.pivots import Zone
from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind
from kotsin_nse.instrument.catalogue import CatalogueLoader
from kotsin_nse.instrument.select import Selection
from kotsin_nse.strategy.base import published
from kotsin_nse.strategy.fudkii import Fudkii
from kotsin_nse.strategy.keys import SHADOW_BOOKS, StrategyKey

from .test_strategies import FakeCtx, _breakout_series
from .test_wide_stop_shadow import _rt_y_trigger

F_PARENT = {"published": False, "gate": "confluence_grade", "reason": "grade F rr=0.0 no wall ahead — no target"}


def _unpublished(sig):
    return replace(sig, grade="F", context={"parent": dict(F_PARENT)})


# -- the 25 % cap: RT-Y's normal trades, not the wide-stop shadow, not CT-Y --------------------------


@pytest.mark.asyncio
async def test_rt_y_caps_its_option_stop_at_25_percent_and_the_wide_shadow_does_not(settings):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        from kotsin_nse.engine import IN_TREND_BOOKS

        far = replace(sig, stop=1440.0)  # 60 points away: the straight line reads the whole premium lost
        assert far.signal_id == sig.signal_id
        await e._handle_signal(far, None, books=IN_TREND_BOOKS)
        y = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y")
        w = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y_W1")
        x = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_X")
        assert y.option_sl == pytest.approx(y.entry * 0.75), "never more than 25 % under the premium paid"
        assert w.option_sl < y.option_sl and x.option_sl < y.option_sl, "the wide-stop shadow and RT-X keep their uncapped stops"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_stop_nearer_than_25_percent_is_rt_ys_own(settings):
    """The cap only ever raises a stop that would lose more than 25 % — a near one stands."""
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)  # 10 points away: the line says 16.07
    try:
        from kotsin_nse.engine import IN_TREND_BOOKS

        await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
        y = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y")
        x = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_X")
        assert y.option_sl > y.entry * 0.75 and y.option_sl == x.option_sl, "the same δ-projected stop as the uncapped RT-X"
    finally:
        await e.stop()


# -- the graded-F shadow ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unpublished_trigger_the_gates_pass_is_traded_by_the_shadow_alone(settings):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        trig = _unpublished(replace(sig, stop=1440.0))  # graded F: no wall ahead, the stop 4 % away
        assert not published(trig) and trig.signal_id == sig.signal_id
        await e._handle_unpublished(trig, None)
        books = sorted(p.strategy for p in e.positions.values())
        assert books == ["FUDKII_RT_Y_F"], "no trading book, no mirror: the graded-F shadow alone"
        f = next(iter(e.positions.values()))
        assert f.option_sl == pytest.approx(f.entry * 0.75), "RT-Y's 25 % cap"
        assert e.wallets["FUDKII_RT_Y_F"].available < e.wallets["FUDKII_RT_Y"].available, "its own purse"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_unpublished_trigger_the_gates_refuse_never_costs_a_strike_choice(settings):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.3)  # breadth 30 %: gate B
    try:
        asked = []
        real = e._select_instrument

        async def counting(underlying, s, *, tape=True):
            asked.append(s.signal_id)
            return await real(underlying, s, tape=tape)

        e._select_instrument = counting  # type: ignore[method-assign]
        await e._handle_unpublished(_unpublished(sig), None)
        assert asked == [] and not e.positions
        ev = [r for r in await e.ledger.rows_between("events", 0, time.time() + 60) if r.get("kind") == "rt_twin.skipped"]
        assert [(r["book"], r["gate"]) for r in ev] == [("FUDKII_RT_Y_F", "breadth")], "recorded as the shadow's own skip"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_unpublished_mcx_trigger_reaches_no_book(settings):
    """RT-Y trades NSE only, and RT-MCX takes FUDKII's published commodity triggers only."""
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        crude = Instrument("477176", "CRUDEOIL", Segment.MCX_FO, InstrumentKind.FUTURE, lot_size=100, underlying="CRUDEOIL")
        e.underlyings["CRUDEOIL"] = crude
        trig = replace(_unpublished(sig), symbol="CRUDEOIL")
        asked = []

        async def select(underlying, s, *, tape=True):
            asked.append(s.symbol)
            return Selection(crude, premium=6000.0, reason="ok", spread_pct=0.1)

        e._select_instrument = select  # type: ignore[method-assign]
        await e._handle_unpublished(trig, None)
        assert asked == [] and not e.positions
        await e._handle_signal(trig, None)  # should one ever arrive: never routed to RT-MCX
        rows = [r for r in await e.ledger.rows_between("signals", 0, time.time() + 60) if r["signal_id"] == trig.signal_id]
        assert all(r["decision"] != "ROUTED" for r in rows)
        assert not any(p.strategy == "FUDKII_RT_MCX" for p in e.positions.values())
    finally:
        await e.stop()


def test_the_shadow_books_are_off_the_trading_totals():
    assert StrategyKey.FUDKII_RT_Y_F in SHADOW_BOOKS and StrategyKey.FUDKII_RT_Y_W1 in SHADOW_BOOKS
    assert StrategyKey.FUDKII_RT_Y not in SHADOW_BOOKS


# -- the raw-pivot ladder -----------------------------------------------------------------------


def test_a_graded_f_trigger_with_no_wall_ahead_gets_the_raw_pivots_as_its_ladder():
    bars = _breakout_series()
    c = bars[-1].close
    zones = [Zone(price=c * 0.95, strength=8.0, members=["1d.S1", "1wk.S1"]),
             Zone(price=c * 1.012, strength=3.2, members=["1wk.R1"]),
             Zone(price=c * 1.004, strength=4.0, members=["1d.R1"]),
             Zone(price=c * 1.03, strength=2.0, members=["1mo.R1"])]
    out = Fudkii().on_bar(FakeCtx(bars, zones), bars[-1])
    assert not out.signals and len(out.triggers) == 1
    trig = out.triggers[0]
    want = tuple(round(round(c * k / 0.05) * 0.05, 2) for k in (1.004, 1.012, 1.03))
    assert trig.grade == "F" and trig.targets == want, "the pivots ahead, nearest first"
    conf = trig.context["confluence"]
    assert conf["targets"] == list(want) and conf["target_note"] == "raw pivots ahead — no cluster wall"
    assert trig.evidence["targets_from_raw_pivots"] == 1.0


def test_a_published_signal_keeps_its_walls_as_targets():
    bars = _breakout_series()
    out = Fudkii().on_bar(FakeCtx(bars), bars[-1])
    sig = out.signals[0]
    assert published(sig) and sig.targets and "target_note" not in sig.context["confluence"]
    assert "targets_from_raw_pivots" not in sig.evidence


# -- the views: an unpublished trigger is no FUDKII signal ----------------------------------------


def _row(sid: str, decision: str, symbol: str = "TCS", ts: int = 1790568900) -> dict:  # 28 Sep 09:45
    return {"signal_id": sid, "strategy": "FUDKII", "symbol": symbol, "direction": "BULLISH", "ts": ts, "created_ts": ts + 1800,
            "entry": 100.0, "stop": 99.0, "targets": [101.0], "grade": "F" if decision == "NOT_PUBLISHED" else "A",
            "decision": decision, "decision_reason": "FUDKII's own confluence_grade: grade F", "context": {}, "evidence": {}}


def test_the_day_book_counts_published_signals_only():
    sigs = [_row("A", "NO_INSTRUMENT", "INFY"), _row("B", "NOT_PUBLISHED")]
    ev = [{"kind": "regime.breadth", "signal_id": s, "share": 0.6, "names": 200, "ts": 1790487000} for s in ("A", "B")]
    rows = daybook.assemble(signals=sigs, positions=[], trades=[], events=ev)
    assert [r["symbol"] for r in rows] == ["INFY"]
    ab = daybook.ab_summary(signals=sigs, positions=[], trades=[], events=ev)
    assert ab["total"]["triggers"] == 1, "RT-Y's A/B counts the triggers RT-Y judges"


@pytest.mark.asyncio
async def test_the_last_signal_is_never_an_unpublished_trigger(settings):
    from kotsin_nse.engine import Engine

    e = Engine(settings)
    await e.ledger.init()
    try:
        await e.ledger.insert_signal({**_row("A", "FILLED", "INFY", 1790568900)}, "FILLED", "ok")
        await e.ledger.insert_signal({**_row("B", "NOT_PUBLISHED", "TCS", 1790570700)}, "NOT_PUBLISHED", "graded F")
        last = await e.ledger.last_signal("FUDKII")
        assert last is not None and last["signal_id"] == "A"
    finally:
        await e.ledger.close()


def test_the_shadow_page_lists_every_graded_f_trigger_and_what_the_shadow_did():
    sigs = [_row("B", "NOT_PUBLISHED"), _row("C", "NOT_PUBLISHED", "INFY"), _row("D", "FILLED", "WIPRO")]
    ev = [{"kind": "regime.breadth", "signal_id": s} for s in ("B", "C", "D")]
    ev.append({"kind": "rt_twin.skipped", "book": "FUDKII_RT_Y_F", "signal_id": "C", "gate": "breadth", "reason": "breadth 30 %"})
    pos = [{"id": "p1", "strategy": "FUDKII_RT_Y_F", "signal_id": "B", "status": "CLOSED", "entry": 20.0, "exit_reason": "TRAIL",
            "instrument": {"name": "TCS 27 OCT 2026 CE 3100.00"}}]
    got = shadow.graded_f_summary(signals=sigs, positions=pos, trades=[{"position_id": "p1", "net": 1234.0}], events=ev)
    assert [(r["symbol"], r["status"]) for r in got["rows"]] == [("TCS", "EXITED"), ("INFY", "NONE")]
    assert got["rows"][1]["skip"] == "breadth 30 %" and got["rows"][0]["net"] == 1234.0
    assert got["total"] == {"triggers": 2, "traded": 1, "closed": 1, "net": 1234.0, "win": 1, "worst": 1234.0, "best": 1234.0}


# -- the scrip masters: every distinct one kept, every day logged ---------------------------------


class _Settings:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.segment_list = [Segment.NSE_FO]


def _master(*rows: str) -> str:
    return "Exch,ExchType,ScripCode,Name\n" + "".join(r + "\n" for r in rows)


@pytest.mark.asyncio
async def test_an_unchanged_master_is_logged_not_stored_and_a_changed_one_is_kept(tmp_path):
    served = {"text": _master("N,D,1,A", "N,D,2,B")}
    fetched = []

    async def fetch(segment):
        fetched.append(segment)
        return served["text"]

    ld = CatalogueLoader(_Settings(tmp_path), fetch)
    d1, d2, d3 = date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1)
    await ld._text_for(Segment.NSE_FO, d1)
    await ld._text_for(Segment.NSE_FO, d2)  # the broker serves the same master
    served["text"] = _master("N,D,2,B", "N,D,3,C", "N,D,4,D")  # 1 expired, 3 and 4 listed
    await ld._text_for(Segment.NSE_FO, d3)
    sm = tmp_path / "scripmaster"
    assert sorted(p.name for p in sm.glob("nse_fo-*")) == ["nse_fo-2026-09-29.csv.gz", "nse_fo-2026-10-01.csv"]
    log = [json.loads(x) for x in (sm / "validity.jsonl").read_text().splitlines()]
    assert [(r["day"], r["file"], r["changed"], r["added"], r["removed"]) for r in log] == [
        ("2026-09-29", "nse_fo-2026-09-29.csv", True, 2, 0),
        ("2026-09-30", "nse_fo-2026-09-29.csv", False, 0, 0),
        ("2026-10-01", "nse_fo-2026-10-01.csv", True, 2, 1),
    ]
    # which codes were valid on each day — the older file read back from its gzip
    assert "N,D,1,A" in ld._read(ld.valid_on(Segment.NSE_FO, d2))
    assert gzip.decompress((sm / "nse_fo-2026-09-29.csv.gz").read_bytes()).decode() == _master("N,D,1,A", "N,D,2,B")
    # a restart the same day reads what it holds, no second fetch
    n = len(fetched)
    assert "N,D,4,D" in await ld._text_for(Segment.NSE_FO, d3)
    assert len(fetched) == n


@pytest.mark.asyncio
async def test_the_0920_refetch_replaces_the_days_own_file_and_deletes_nothing_older(tmp_path):
    served = {"text": _master("N,D,1,A")}

    async def fetch(segment):
        return served["text"]

    ld = CatalogueLoader(_Settings(tmp_path), fetch)
    d1, d2 = date(2026, 9, 29), date(2026, 9, 30)
    await ld._text_for(Segment.NSE_FO, d1)
    served["text"] = _master("N,D,1,A", "N,D,2,B")
    await ld._text_for(Segment.NSE_FO, d2)
    served["text"] = _master("N,D,1,A", "N,D,2,B", "N,D,5,E")  # strikes listed 09:00–09:15
    await ld._text_for(Segment.NSE_FO, d2, refetch=True)
    sm = tmp_path / "scripmaster"
    assert sorted(p.name for p in sm.glob("nse_fo-*")) == ["nse_fo-2026-09-29.csv.gz", "nse_fo-2026-09-30.csv"]
    assert "N,D,5,E" in (sm / "nse_fo-2026-09-30.csv").read_text()
    log = [json.loads(x) for x in (sm / "validity.jsonl").read_text().splitlines()]
    assert (log[-1]["day"], log[-1]["changed"], log[-1]["added"], log[-1]["removed"]) == ("2026-09-30", True, 1, 0)


@pytest.mark.asyncio
async def test_the_brokers_crlf_master_is_recognised_as_unchanged(tmp_path):
    """5paisa ends every line in CRLF; compared after newline translation it never equalled itself
    and every day wrote a new file (review, 2026-09-29)."""
    text = "Exch,ExchType,ScripCode,Name\r\nN,D,1,A\r\nN,D,2,B\r\n"

    async def fetch(segment):
        return text

    ld = CatalogueLoader(_Settings(tmp_path), fetch)
    await ld._text_for(Segment.NSE_FO, date(2026, 9, 29))
    await ld._text_for(Segment.NSE_FO, date(2026, 9, 30))
    sm = tmp_path / "scripmaster"
    assert sorted(p.name for p in sm.glob("nse_fo-*")) == ["nse_fo-2026-09-29.csv"]
    assert (sm / "nse_fo-2026-09-29.csv").read_bytes() == text.encode(), "kept byte for byte"
    log = [json.loads(x) for x in (sm / "validity.jsonl").read_text().splitlines()]
    assert [(r["day"], r["changed"], r["rows"]) for r in log] == [("2026-09-29", True, 2), ("2026-09-30", False, 2)]
    assert await ld._text_for(Segment.NSE_FO, date(2026, 9, 30)) == text


@pytest.mark.asyncio
async def test_gate_b_refuses_before_any_broker_read(settings):
    """Breadth, a pivot just ahead and the 09:45 gap are in memory: a trigger they refuse never asks
    the broker for its future's candles (review, 2026-09-29)."""
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.3)
    try:
        reads = []

        async def vol(underlying, **kw):
            reads.append(kw)
            return {}

        e._volume_surges = vol  # type: ignore[method-assign]
        await e._handle_unpublished(_unpublished(sig), None)
        assert reads == [] and not e.positions
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_overview_headline_is_the_trading_books(settings):
    import httpx

    from kotsin_nse.api.routes import build_app
    from kotsin_nse.engine import Engine

    e = Engine(settings)
    await e.ledger.init()
    await e._load_wallets()
    try:
        for w in e.wallets.values():  # day P&L is balance − the day's opening balance
            w.day_start_balance = w.balance
        e.wallets["FUDKII_RT_Y_F"].day_start_balance += 13_251.0
        e.wallets["FUDKII_RT_Y"].day_start_balance -= 2_000.0
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(e)), base_url="http://t") as c:
            j = (await c.get("/api/overview")).json()
            assert j["day_pnl"] == 2_000.0 and j["shadow_day_pnl"] == -13_251.0, "a shadow's loss is not the day's"
            await e.ledger.insert_signal(_row("B", "NOT_PUBLISHED"), "NOT_PUBLISHED", "graded F")
            await e.ledger.insert_signal(_row("A", "FILLED", "INFY"), "FILLED", "ok")
            assert [r["signal_id"] for r in (await c.get("/api/signals")).json()] == ["A"]
            assert {r["signal_id"] for r in (await c.get("/api/signals", params={"unpublished": "true"})).json()} == {"A", "B"}
    finally:
        await e.ledger.close()
