"""The alerts page after a restart, or after a boundary the process was not running for.

2026-09-25: the page was blank. Nothing ran before 10:58, so 09:45, 10:15 and 10:45 were never
evaluated; the rings lived only in memory, so each of four restarts emptied them; and the web
server opened only after a ~2.5 minute backfill. These pin the three fixes.
"""

from __future__ import annotations

import json
import time

from kotsin_nse.alerts.detectors import Alert
from kotsin_nse.alerts.engine import AlertEngine
from kotsin_nse.bars.unified import BarSource, UnifiedBar

T0 = 1_790_313_300  # a 30m bucket start


def _alert(book: str, ts: int, symbol: str = "RELIANCE", kind: str = "TRIGGER") -> Alert:
    return Alert(book=book, symbol=symbol, scrip_code="2885", tf="30m", ts=ts, direction="BULLISH",
                 score=1.0, reason="t", price=100.0, kind=kind, evidence={"atr": 3.2})


def _bar(ts: int, symbol: str = "RELIANCE", close: float = 100.0) -> UnifiedBar:
    return UnifiedBar(symbol=symbol, scrip_code="2885", tf="30m", ts=ts, open=99.0, high=101.0, low=98.5,
                      close=close, volume=1e5, source=BarSource.REST, complete=True)


# -- a restart brings the session back --------------------------------------------------------------


def test_a_saved_session_comes_back_after_a_restart_exactly(tmp_path):
    a = AlertEngine(engine=None)
    a.store_dir = tmp_path
    for i, book in enumerate(("PIVOTBOSS", "PIVOTBOSS", "FUDKOI")):
        a._emit(_alert(book, T0 + i * 1800))
    a.evaluated = {"PIVOTBOSS": 480, "FUDKOI": 480}
    assert a.dirty, "a new alert is something to save"
    a.save()
    assert not a.dirty and (tmp_path / "alerts.json").exists()

    b = AlertEngine(engine=None)
    b.store_dir = tmp_path
    assert b.restore(since_ts=time.time() - 3600) == 3
    assert b.feed() == a.feed(), "the page after the restart is the page before it"
    assert b.evaluated == {"PIVOTBOSS": 480, "FUDKOI": 480}, "so the empty state is not '0 evaluations'"
    assert b.counts == {"PIVOTBOSS": 2, "FUDKOI": 1}


def test_a_session_saved_before_the_last_reset_never_comes_back(tmp_path):
    a = AlertEngine(engine=None)
    a.store_dir = tmp_path
    a._emit(_alert("PIVOTBOSS", T0))
    a.save()
    b = AlertEngine(engine=None)
    b.store_dir = tmp_path
    assert b.restore(since_ts=time.time() + 10) == 0 and b.feed() == [], "yesterday stays gone after 00:30"

    (tmp_path / "alerts.json").write_text("{not json")
    assert AlertEngine(engine=None).restore(0) == 0, "no store dir: memory only, never an error"
    c = AlertEngine(engine=None)
    c.store_dir = tmp_path
    assert c.restore(0) == 0, "a corrupt save is an empty page, not a crash"


def test_the_0030_reset_empties_the_saved_session_too(tmp_path):
    a = AlertEngine(engine=None)
    a.store_dir = tmp_path
    a._emit(_alert("PIVOTBOSS", T0))
    a.save()
    a.reset_day("2026-09-26")
    assert a.dirty, "or a restart after 00:30 would bring yesterday back"
    a.save()
    saved = json.loads((tmp_path / "alerts.json").read_text())
    assert saved["rings"] == {} and saved["reset_day"] == "2026-09-26"


def test_card_marks_are_saved_too(tmp_path):
    a = AlertEngine(engine=None)
    a._emit(Alert(book="FUDKII_RT", symbol="X", scrip_code="1", tf="30m", ts=T0, direction="BULLISH",
                  score=1.0, reason="t", price=1.0, kind="ENTRY", evidence={"signalId": "S1"}))
    a.save()  # no store dir: a no-op, and clears nothing it should not
    a.dirty = False
    a.mark_skipped("S1", book="FUDKII_RT_X", reason="dried volume")
    assert a.dirty, "a skip written onto a card must survive a restart"


# -- a boundary the process never saw ---------------------------------------------------------------


async def test_the_replay_rebuilds_missed_boundaries_once_each_and_says_so(monkeypatch):
    a = AlertEngine(engine=None)
    held = _alert("PIVOTBOSS", T0)            # restored from before the restart
    a._emit(held)
    fired_live = held.fired_at
    seen: list[int] = []

    def book_alerts(bar, history):
        seen.append(len(history))
        return [_alert("PIVOTBOSS", bar.ts)]

    monkeypatch.setattr(a, "_book_alerts", book_alerts)
    monkeypatch.setattr(a, "_enrich", lambda *args, **kw: None)

    def sig(ts: int, sid: str) -> dict:
        return {"signal_id": sid, "symbol": "RELIANCE", "scrip_code": "2885", "direction": "BULLISH",
                "entry": 100.0, "stop": 98.0, "targets": [104.0], "grade": "A"}

    fudkii_out = {T0 + 1800: [sig(T0 + 1800, "FUDKII-RELIANCE-a")], T0 + 3600: [sig(T0 + 3600, "FUDKII-RELIANCE-b")]}
    bars = [_bar(T0), _bar(T0 + 1800), _bar(T0 + 3600)]
    got = await a.catch_up(
        bars,
        history_of=lambda b: [_bar(T0 - 1800 * k) for k in range(30)] + [b],
        fudkii=lambda b: fudkii_out.get(b.ts, []),
        known_signal_ids={"FUDKII-RELIANCE-b"},  # the ledger has it: handled live, left alone
    )
    assert got == {"books": 2, "fudkii": 1, "bars": 3}
    assert seen == [31, 31, 31], "every book sees history cut at its own bar"

    pb = a.feed("PIVOTBOSS")
    assert [r["ts"] for r in pb] == [T0 + 3600, T0 + 1800, T0], "the held one is not duplicated"
    held_row = next(r for r in pb if r["ts"] == T0)
    assert held_row["firedAt"] == fired_live and "replayed" not in held_row["evidence"]
    rebuilt = next(r for r in pb if r["ts"] == T0 + 1800)
    assert rebuilt["evidence"]["replayed"] is True
    assert rebuilt["firedAt"] == T0 + 3600, "stamped at its bar's close, the instant it would have fired"

    entries = [r for r in a.feed("FUDKII_RT") if r["kind"] == "ENTRY"]
    assert len(entries) == 1 and entries[0]["evidence"]["replayed"] is True
    assert "not traded" in entries[0]["card"]["skipped"][0]["reason"]
    assert a.rt.living == {}, "a trade never taken is not re-checked as if it were live"
    assert a.counts == {"PIVOTBOSS": 3, "FUDKII_RT": 1}


async def test_one_bad_bar_does_not_cost_the_rest_of_the_day(monkeypatch):
    a = AlertEngine(engine=None)
    monkeypatch.setattr(a, "_enrich", lambda *args, **kw: None)

    def book_alerts(bar, history):
        if bar.ts == T0:
            raise RuntimeError("bad bar")
        return [_alert("PIVOTBOSS", bar.ts)]

    monkeypatch.setattr(a, "_book_alerts", book_alerts)
    got = await a.catch_up([_bar(T0), _bar(T0 + 1800)], history_of=lambda b: [b])
    assert got["books"] == 1 and [r["ts"] for r in a.feed()] == [T0 + 1800]


# -- the engine wiring ------------------------------------------------------------------------------


async def test_start_core_restores_the_session_before_the_market_boot(settings):
    from kotsin_nse.engine import Engine

    prior = AlertEngine(engine=None)
    prior.store_dir = settings.data_dir / "alerts"
    prior._emit(_alert("PIVOTBOSS", T0))
    prior.save()

    e = Engine(settings)
    assert e.booting
    await e.start_core()
    assert [r["ts"] for r in e.alerts.feed()] == [T0], "the page has its alerts before the backfill starts"
    assert e.booting, "still booting: the market half has not run"
    assert e.health_snapshot()["booting"] is True
    await e.ledger.close()


def test_the_last_reset_instant_is_the_most_recent_0030_ist(settings):
    from datetime import datetime

    from kotsin_nse.engine import Engine
    from kotsin_nse.market.session import IST

    cut = Engine(settings)._last_alerts_reset_ts()
    now = time.time()
    assert now - 86_400 < cut <= now
    assert datetime.fromtimestamp(cut, IST).strftime("%H:%M") == settings.alerts_reset_ist


def test_the_as_of_context_never_shows_a_bar_after_the_one_being_replayed(settings):
    from kotsin_nse.engine import AsOfContext, Engine

    e = Engine(settings)
    e.store.seed("RELIANCE", "30m", [_bar(T0 + 1800 * k, close=100.0 + k) for k in range(5)])
    assert len(e.store.bars("RELIANCE", "30m")) == 5
    ctx = AsOfContext(e)
    ctx.until = T0 + 1800 * 2
    got = ctx.bars("RELIANCE", "30m", 10)
    assert [b.ts for b in got] == [T0, T0 + 1800, T0 + 3600]
    assert [b.ts for b in ctx.bars("RELIANCE", "30m", 2)] == [T0 + 1800, T0 + 3600]


async def test_the_market_boot_waits_out_a_dead_broker_instead_of_exiting(settings, monkeypatch):
    """11:30 on 2026-09-25: a failed DNS lookup for the broker made the boot's login raise and the
    process exit, taking the pages down with it. The boot now retries until the broker answers."""
    import asyncio

    from kotsin_nse.engine import Engine

    e = Engine(settings)
    calls = {"n": 0}

    async def token():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("[Errno 8] nodename nor servname provided, or not known")
        return object()

    monkeypatch.setattr(e.auth, "token", token)
    await asyncio.wait_for(e._wait_for_broker(first_delay_s=0.01, max_delay_s=0.02), timeout=2)
    assert calls["n"] == 3, "two failures waited out, the third login goes through"
    assert any("reachable again" in n for n in e.boot_notes)
