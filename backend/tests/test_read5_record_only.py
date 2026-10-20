"""RECORD-ONLY proof (operator, 2026-10-19: "yes - the engine should start recording the 5-read and strike-wall state
live, for research only, so CT-B1 and later B2 can use them"). No decision, gate, book or order may change:

* ``_decide`` on the same scripted trigger, the recorder off and on — on, with the recorder fed absurd values and the
  read run to its end: the same signals and decisions, positions, orders, wallets, ``_breadth_at`` and events (but
  ``read5.*``);
* the read adds nothing to ``_breadth_at``, the signal's context or evidence; CT-B1's conviction keeps ``read5: None``;
* the only places in the engine that touch the recorder, the reads or their records are the recorder's own glue, the
  card and health (an AST check of every function in engine.py); the decision path's one reference is a call whose
  result is unused;
* ``_read5_note`` awaits nothing and costs well under a millisecond a trigger.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind, OptionType
from kotsin_nse.engine import Engine
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.instrument.catalogue import Catalogue
from kotsin_nse.instrument.select import Quote, Selection
from kotsin_nse.market.session import ist_day
from kotsin_nse.ops.optrec import read_detail_day
from kotsin_nse.strategy import b1 as B1
from kotsin_nse.strategy.base import Outcome, Signal
from kotsin_nse.strategy.keys import StrategyKey

from .test_rt_twin import RELIANCE, RELIANCE_OPT, _bars30, _paper

ENGINE = Path(__file__).resolve().parent.parent / "kotsin_nse" / "engine.py"


def _catalogue(today: date) -> Catalogue:
    exp = (today + timedelta(days=9)).isoformat()
    cat = Catalogue()
    for k in range(1450, 1560, 10):
        for cp in ("CE", "PE"):
            i = Instrument(f"R{k}{cp}", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION, expiry=exp, strike=float(k),
                           option_type=OptionType(cp), underlying="RELIANCE", lot_size=250)
            cat.by_code[i.scrip_code] = i
            cat.options_by_symbol["RELIANCE"].append(i)
    f = Instrument("RFUT", "RELIANCE", Segment.NSE_FO, InstrumentKind.FUTURE, expiry=exp, underlying="RELIANCE")
    cat.by_code[f.scrip_code] = f
    cat.futures_by_symbol["RELIANCE"].append(f)
    cat.by_code[RELIANCE.scrip_code] = RELIANCE
    cat.equity_by_symbol["RELIANCE"] = RELIANCE
    return cat


def _c0(now: float) -> float:
    """The latest 30m bar start on the NSE grid that closed at least 5 s ago (not the opening or the auction bar)."""
    from kotsin_nse.research.read5 import GRID, at_ist

    days = (ist_day(now), ist_day(now) - timedelta(days=1))
    starts = [at_ist(d, g) for d in days for g in GRID if g not in ("09:15", "15:15")]
    return max(t for t in starts if t <= now - 1805)


def _absurd_frames(e: Engine, c0: float, s: float) -> None:
    """The legs' prices, OI and volume as wild as they come: whatever the read says, nothing may move."""
    rec = e.recorder
    assert rec is not None
    rec.roll(ist_day(s))
    rec.set_watch({**{c: "opt" for c in e.catalogue_loader.catalogue.by_code if c.startswith("R1")}, "RFUT": "fut",
                   RELIANCE.scrip_code: "eq"})
    for i, code in enumerate(("R1510CE", "R1520CE", "R1490PE", "R1480PE")):
        rec.on_frame("mf", {"Token": code, "LastRate": 0.05, "BidRate": 0.05, "OffRate": 0.1, "TotalQty": 0,
                            "TickDt": f"/Date({int((c0 - 86_400) * 1000)})/"}, c0 - 30)
        rec.on_frame("oi", {"Token": code, "OpenInterest": 10 + i}, c0 - 20)
        rec.on_frame("mf", {"Token": code, "LastRate": 9_999.0 / (i + 1), "BidRate": 9_000.0, "OffRate": 10_000.0,
                            "TotalQty": 10**9, "TickDt": f"/Date({int((s - 9) * 1000)})/"}, s - 8)
        rec.on_frame("oi", {"Token": code, "OpenInterest": 10**9 * (i + 1)}, s - 7)
    rec.on_frame("mf", {"Token": "RFUT", "LastRate": 1500.0, "BidRate": 1499.0, "OffRate": 1501.0}, c0 - 30)
    rec.on_frame("mf", {"Token": "RFUT", "LastRate": 3.0e6, "BidRate": 2.9e6, "OffRate": 3.1e6}, s - 6)
    rec.on_frame("mf", {"Token": RELIANCE.scrip_code, "LastRate": 1500.0}, c0 - 30)
    rec.on_frame("mf", {"Token": RELIANCE.scrip_code, "LastRate": 1.0e7}, s - 5)


async def _run(settings: Any, tmp: Path, *, on: bool) -> dict[str, Any]:
    await asyncio.to_thread(tmp.mkdir, parents=True, exist_ok=True)
    s_ = settings.model_copy(update={"data_dir": tmp, "db_url": f"sqlite+aiosqlite:///{tmp}/t.db", "record5_enabled": on})
    e = await _paper(s_)
    try:
        assert (e.recorder is not None) is on
        e.underlyings[RELIANCE.symbol] = RELIANCE
        now = time.time()
        e.catalogue_loader.catalogue = _catalogue(ist_day(now))

        async def select(underlying, sig, *, tape=True):
            return Selection(RELIANCE_OPT, premium=50.0, reason="ok", spread_pct=0.5)

        e._select_instrument = select  # type: ignore[method-assign]
        e.books[RELIANCE_OPT.scrip_code] = BookSnapshot(RELIANCE_OPT.scrip_code, bids=[(49.95, 500_000)], asks=[(50.0, 500_000)], ts=now)
        e.quotes[RELIANCE_OPT.scrip_code] = Quote(ltp=50.0, bid=49.95, ask=50.0, ts=now)
        e.ltps[RELIANCE_OPT.scrip_code] = 50.0
        c0 = _c0(now)
        sig = Signal(strategy=StrategyKey.FUDKII, symbol="RELIANCE", direction=Direction.BULLISH, ts=int(c0), entry=1500.0,
                     stop=1490.0, targets=(1550.0,), grade="A", rr=5.0, reason="ST flip UP + close above upper band",
                     evidence={"atr": 7.5})
        ctx_before, ev_before = dict(sig.context), dict(sig.evidence)
        e.fudkii.on_bar = lambda ctx, bar: Outcome(signals=[sig])  # type: ignore[method-assign]
        e.fukaa.on_signal = lambda ctx, bar, s: Outcome()  # type: ignore[method-assign]
        if on:
            e._read5_live = True

            async def no_wait(until: float) -> None:
                return None

            e._read5_wait = no_wait  # type: ignore[method-assign]
            _absurd_frames(e, c0, now)
        bar = _bars30("RELIANCE", RELIANCE.scrip_code, [1500.0] * 8)[-1]
        await e._decide(bar)
        while e._decision_tasks or e._read5_tasks:
            await asyncio.gather(*list(e._decision_tasks), *list(e._read5_tasks), return_exceptions=True)
        end = time.time() + 60
        signals = sorted((r["strategy"], r["symbol"], r.get("decision"), r.get("decision_reason")) for r in await e.ledger.rows_between("signals", 0, end))
        positions = sorted((p["strategy"], p.get("status"), p.get("qty"), round(float(p.get("entry_price") or 0), 4))
                           for p in await e.ledger.rows_between("positions", 0, end))
        orders = sorted((o.get("purpose"), o.get("status"), o.get("decision")) for o in await e.ledger.rows_between("orders", 0, end))
        events = await e.ledger.rows_between("events", 0, end)
        return {
            "signals": signals, "positions": positions, "orders": orders,
            "events": sorted(ev["kind"] for ev in events if not str(ev["kind"]).startswith("read5.")),
            "read5": [ev for ev in events if str(ev["kind"]).startswith("read5.")],
            "breadth": e._breadth_at.get(sig.signal_id),
            "wallets": {k: (round(w.balance, 4), round(w.deployed, 4), w.trades) for k, w in e.wallets.items()},
            "ctx_same": dict(sig.context) == ctx_before, "ev_same": dict(sig.evidence) == ev_before,
            "stats": dict(e.read5_stats),
            "detail": read_detail_day(tmp / "archive" / "read5_detail", ist_day(now).isoformat()),
        }
    finally:
        await e.stop()


def _strip_volatile(b: dict[str, Any] | None) -> dict[str, Any] | None:
    """The measurement's own clock (``ts``) and India VIX differ between two runs; nothing else may."""
    return None if b is None else {k: v for k, v in b.items() if k not in ("vix", "ts")}


@pytest.mark.asyncio
async def test_the_decision_is_the_same_with_the_recorder_off_and_on_fed_absurd_values(settings, tmp_path, monkeypatch):
    monkeypatch.setattr("kotsin_nse.engine.rec_hms", lambda t: "10:45:05")  # the read's own 15:15 rule, off the wall clock
    off = await _run(settings, tmp_path / "off", on=False)
    on = await _run(settings, tmp_path / "on", on=True)
    assert on["signals"] == off["signals"] and on["signals"], "the same signals and decisions"
    assert on["positions"] == off["positions"] and on["orders"] == off["orders"] and on["wallets"] == off["wallets"]
    assert on["events"] == off["events"]
    assert _strip_volatile(on["breadth"]) == _strip_volatile(off["breadth"])
    assert off["read5"] == [] and off["detail"] == []
    kinds = [ev["kind"] for ev in on["read5"]]
    assert kinds == ["read5.trigger", "read5.follow"], (kinds, on["stats"])
    trig = on["read5"][0]
    assert not trig.get("excluded") and trig["legs"]["CE1"]["strike"] == 1510.0 and trig["walls"]["overall"] is not None
    assert on["read5"][1]["complete"] is True and on["read5"][1]["stages"] == 16
    # the ledger keeps the compact copy; the day's detail file the whole record (legs' snapshots, the sixteen stages)
    assert "rows" not in on["read5"][1] and "X" not in trig["legs"]["CE1"] and "stage0" not in trig
    assert [d["kind"] for d in on["detail"]] == kinds
    assert len(on["detail"][1]["record"]["rows"]) == 16 and "X" in on["detail"][0]["record"]["legs"]["CE1"]
    assert on["ctx_same"] and on["ev_same"], "nothing written into the signal"
    assert not any("read5" in k.lower() for k in (on["breadth"] or {})), "nothing written into the logged context"


def test_ct_b1s_conviction_still_does_not_read_the_5_read():
    conv = B1.conviction({"gapOfficial": 0.3, "atr30": 2.0, "dAtr": 5.0, "officialClose": 100.0}, close=101.0, bullish=True, zones=[])
    assert conv["read5"] is None and conv["note"] == B1.NO_READ5


#: the engine functions allowed to touch the recorder, the reads or their records — the recorder's glue, the card
#: and health. ``_decide`` only calls ``_read5_note`` and drops its result.
ALLOWED = {
    "__init__", "_on_record_frame", "_rec_watch", "_rec_prepare", "_boot_market", "start_market", "_on_feed_connect",
    "_decide", "_read5_note", "_read5_excluded", "_read5_wait", "_read5_atr", "_read5_record", "_rec_anchor", "_rec_sync",
    "_rec_wall_candidates", "_rec_tick", "record5_health", "stop", "_housekeeping", "health_snapshot", "book_cards",
    "_read5_card", "_rec_owner_sync",
}
NAMES = ("recorder", "read5rec", "_read5", "_rec_", "volume_tape", "record5", "rec_hms", "rec_legs_expiry", "READ5_",
         "r5_", "RootBand", "nearest_first", "chain_pair", "OptionRecorder", "VolumeTape", "subscribe_record",
         "is_record", "record_counts")


def _mentions(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        name = n.attr if isinstance(n, ast.Attribute) else n.id if isinstance(n, ast.Name) else None
        if name and any(name.startswith(p) or name == p for p in NAMES):
            out.add(name)
    return out


def test_only_the_recorders_glue_the_card_and_health_touch_the_5_read():
    tree = ast.parse(ENGINE.read_text())
    eng = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Engine")
    offenders = {}
    for fn in eng.body:
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            hit = _mentions(fn)
            if hit and fn.name not in ALLOWED:
                offenders[fn.name] = sorted(hit)
    assert offenders == {}, offenders
    decide = next(fn for fn in eng.body if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "_decide")
    uses = [n for n in ast.walk(decide) if isinstance(n, ast.Attribute) and n.attr.startswith(("_read5", "recorder", "_rec_"))]
    assert [u.attr for u in uses] == ["_read5_note"]
    calls = [n for n in ast.walk(decide) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
             and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "_read5_note"]
    assert len(calls) == 1, "a bare call: its result is not read"


def test_nothing_that_trades_imports_the_recorder_or_the_reads():
    root = ENGINE.parent
    for pkg in ("strategy", "risk", "exec", "venue", "feed"):
        for f in (root / pkg).rglob("*.py"):
            for n in ast.walk(ast.parse(f.read_text())):
                mods = [n.module or ""] if isinstance(n, ast.ImportFrom) else [a.name for a in n.names] if isinstance(n, ast.Import) else []
                names = [a.name for a in n.names] if isinstance(n, ast.ImportFrom) else []
                assert not any(b in m for m in (*mods, *names) for b in ("read5", "optrec", "recband")), (f, mods, names)


@pytest.mark.asyncio
async def test_the_note_awaits_nothing_and_is_cheap(settings):
    assert not inspect.iscoroutinefunction(Engine._read5_note)
    e = Engine(settings)
    e._read5_live = True
    started: list[str] = []

    async def record(sig: Signal, s: float) -> None:
        started.append(sig.signal_id)

    e._read5_record = record  # type: ignore[method-assign]
    sigs = [Signal(strategy=StrategyKey.FUDKII, symbol=f"S{i}", direction=Direction.BULLISH, ts=1_792_035_900, entry=100.0,
                   stop=99.0, targets=(), grade="F", rr=0.0, reason="t") for i in range(40)]
    t0 = time.perf_counter()
    for sg in sigs:
        e._read5_note(sg)
    took = time.perf_counter() - t0
    assert took < 0.02, f"40 notes took {took * 1000:.2f} ms"
    e._read5_note(sigs[0])
    fukaa = Signal(strategy=StrategyKey.FUKAA, symbol="X", direction=Direction.BULLISH, ts=1, entry=1.0, stop=0.5,
                   targets=(), grade="A", rr=1.0, reason="t")
    e._read5_note(fukaa)
    await asyncio.gather(*list(e._read5_tasks))
    assert len(started) == 40, "once a trigger, FUDKII's alone"


@pytest.mark.asyncio
async def test_the_card_reads_the_day_s_events_and_says_when_a_restart_cut_the_follow_up(settings):
    from kotsin_nse.market.session import ist_today

    e = await _paper(settings)
    try:
        e.underlyings["TCS"] = Instrument("11536", "TCS", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="TCS")
        from datetime import datetime
        from datetime import time as dtime

        from kotsin_nse.market.session import from_ist

        bar_ts = int(from_ist(datetime.combine(ist_today(), dtime(9, 15))))
        sig = Signal(strategy=StrategyKey.FUDKII, symbol="TCS", direction=Direction.BULLISH, ts=bar_ts, entry=4000.0,
                     stop=3990.0, targets=(4050.0,), grade="A", rr=5.0, reason="t")
        await e.ledger.insert_signal(sig.to_json(), "PUBLISHED", "ok")
        trig = {"signal_id": sig.signal_id, "symbol": "TCS", "s": bar_ts + 1805.0, "split": "2-1", "with": ["OI", "VOL"],
                "against": ["STOCK"], "noVote": ["IV", "PREMIUM"], "lead": "Bullish", "leadVsEngine": "with",
                "basis": "5-read", "missingReads": [], "reads": {},
                "walls": {"ce": None, "pe": None, "overall": ""}}
        await e.ledger.event("read5.trigger", trig)
        card = (await e.book_cards("FUDKII", ist_today()))["cards"][0]
        r5 = card["read5"]
        assert r5["recordOnly"] is True and r5["split"] == "2-1" and r5["lines"][0].startswith("5-read at trigger 2-1")
        expected = "follow-up interrupted (restart)" if time.time() > bar_ts + 1805 + 17 * 60 else "follow-up running"
        assert r5["lines"][1] == expected
        await e.ledger.event("read5.follow", {"signal_id": sig.signal_id, "complete": True,
                                              "f3": {"call": "Bullish", "time": "09:46:05", "minute": 1, "label": "Weak",
                                                     "vsEngine": "with engine"}})
        r5 = (await e.book_cards("FUDKII", ist_today()))["cards"][0]["read5"]
        assert r5["lines"][1] == "first 3 of 5: Bullish 09:46:05 (+1 min, Weak) — with engine" and r5["f3"]["label"] == "Weak"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_read_touches_the_chain_only_after_its_wait(settings, monkeypatch):
    """Nothing of the read runs at ``_decide``'s first await (review, 20 Oct: the legs' lookup cost 2.2 ms a 40-trigger
    burst before the books decided): the chain is read after the signal + 10 s wait."""
    from kotsin_nse.ops import read5rec
    from kotsin_nse.research.read5 import at_ist

    e = await _paper(settings)
    try:
        order: list[str] = []

        async def wait(until: float) -> None:
            order.append("wait")

        def contracts(*_a: Any, **_k: Any) -> tuple[None, str]:
            order.append("contracts")
            return None, "no NSE option chain (MCX / no options listed)"

        e._read5_wait = wait  # type: ignore[method-assign]
        monkeypatch.setattr(read5rec, "contracts", contracts)
        e.underlyings["TCS"] = Instrument("11536", "TCS", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="TCS")
        c0 = at_ist("2026-10-19", "10:15")
        sig = Signal(strategy=StrategyKey.FUDKII, symbol="TCS", direction=Direction.BULLISH, ts=int(c0), entry=4000.0,
                     stop=3990.0, targets=(), grade="A", rr=5.0, reason="t")
        await e._read5_record(sig, c0 + 1805)
        assert order == ["wait", "contracts"] and e.read5_stats["excluded"] == 1
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_new_day_empties_the_seen_triggers_however_the_recorder_rolled(settings):
    from kotsin_nse.research.read5 import at_ist

    e = Engine(settings)
    assert e.recorder is not None
    e._read5_seen.add("FUDKII-TCS-old")
    e._read5_day = "2026-10-16"
    e.recorder.roll(date(2026, 10, 19))  # a frame rolled the recorder before the housekeeping saw the day
    await e._rec_tick(at_ist("2026-10-19", "09:00"))
    assert e._read5_seen == set() and e._read5_day == "2026-10-19"


@pytest.mark.asyncio
async def test_only_the_sockets_owner_writes_the_minute_volumes(settings):
    from kotsin_nse.research.read5 import at_ist

    e = Engine(settings)
    rec = e.recorder
    assert rec is not None
    rec.roll(date(2026, 10, 19))
    rec.set_watch({"O1": "opt"})
    rec.on_frame("mf", {"Token": "O1", "LastRate": 1.0, "TotalQty": 0, "TickDt": f"/Date({int(at_ist('2026-10-16', '15:29') * 1000)})/"},
                 at_ist("2026-10-19", "09:08"))
    rec.on_frame("mf", {"Token": "O1", "LastRate": 1.1, "TotalQty": 50, "TickDt": f"/Date({int(at_ist('2026-10-19', '09:20') * 1000)})/"},
                 at_ist("2026-10-19", "09:20"))
    e.feed.hub = ("127.0.0.1", 8510)  # this engine reads its twin's socket
    await e._rec_tick(at_ist("2026-10-19", "09:30"))
    assert e.volume_tape.enabled is False and e.volume_tape.rows_skipped == 1 and e.volume_tape.rows_written == 0


@pytest.mark.asyncio
async def test_the_recorders_streams_follow_the_hub_role_from_boot(settings):
    """Re-check, 20 Oct: ``volume_tape.enabled`` was set only before the first minute rows were written, so the twin's
    health said true until then. It is set with the hub's wiring (``start_market``), and the recorder learns there whether
    every OI subscription prints on subscribe (its own socket) or not (reading the hub)."""
    e = Engine(settings)
    assert e.recorder is not None
    assert e.record5_health()["volume_tape"]["enabled"] is True and e.recorder.oi_on_subscribe is True
    e.feed.hub = ("127.0.0.1", 8510)
    e._rec_owner_sync()
    assert e.record5_health()["volume_tape"]["enabled"] is False and e.recorder.oi_on_subscribe is False
    src = inspect.getsource(Engine.start_market)
    assert src.index("self.feed.hub = hub.connect") < src.index("self._rec_owner_sync()") < src.index("self.feed.run()")
