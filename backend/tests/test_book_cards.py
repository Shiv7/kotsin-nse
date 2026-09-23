"""The trigger-card page: one card per FUDKII trigger, read per book, from the ledger."""

from __future__ import annotations

import time
from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind, OptionType, Position, PosSide
from kotsin_nse.engine import Engine, _position_json
from kotsin_nse.market.session import IST, ist_today
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.keys import StrategyKey


def _sig(ts, **kw):
    base = dict(strategy=StrategyKey.FUDKII, symbol="RELIANCE", direction=Direction.BULLISH, ts=ts, entry=1500.0, stop=1490.0,
                targets=(1520.0,), grade="A", rr=2.0, reason="ST flip UP + close above upper band",
                context={"confluence": {"room_ratio": 2.4, "fortress": 6.0}})
    base.update(kw)
    return Signal(**base)


@pytest.mark.asyncio
async def test_a_trigger_reads_differently_in_every_book(settings):
    e = Engine(settings)
    await e.start()
    try:
        # Anchored to TODAY's session, not to the wall clock. `book_cards` reads the ledger for
        # `ist_today()`, so a `now`-relative timestamp puts rows on the wrong side of midnight
        # when the suite runs late: at 23:51 the second signal (+30 min) landed on tomorrow, and
        # at 00:01 the first one (−10 min) landed on yesterday. Both were seen on 2026-09-23/24.
        ts = int(datetime.combine(ist_today(), dtime(10, 0), tzinfo=IST).timestamp())
        now = ts + 600
        sig = _sig(ts)
        await e.ledger.insert_signal(sig.to_json(), "PAPER_FILLED", sig.reason)
        opt = Instrument("45678", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=250, strike=1500.0, option_type=OptionType.CE, underlying="RELIANCE")
        und = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE")
        pos = Position(id="p-rtx", strategy="FUDKII_RT_X", instrument=opt, underlying=und, side=PosSide.LONG, qty=250, entry=50.0,
                       opened_ts=now - 500, signal_id=sig.signal_id, direction=Direction.BULLISH, equity_entry=1500.0, equity_sl=1490.0)
        e.positions[pos.id] = pos
        await e.ledger.upsert_position(_position_json(pos))
        await e.ledger.event("rt_twin.skipped", {"book": "FUDKII_RT_Y", "signal_id": sig.signal_id, "symbol": "RELIANCE", "reason": "dried volume future 0.79/0.51 < 0.85"})
        await e.ledger.event("counter.route", {"signal_id": sig.signal_id, "symbol": "RELIANCE", "route": "IN_TREND", "reason": "no wall", "summary": "no wall", "wall": {"members": []}, "reads": [{"leg": "future", "volume": "dried"}]})

        x = await e.book_cards("FUDKII_RT_X", ist_today())
        assert x["counts"] == {"OPEN": 1} and x["cards"][0]["state"] == "OPEN" and x["cards"][0]["live"]["qtyRemaining"] == 250
        ep = x["cards"][0]["exitPlan"]
        assert ep["policy"].startswith("RT-X") and [r["kind"] for r in ep["rows"]][-2:] == ["trail", "time"]
        assert any(r["kind"] == "stop" and r["qty"] == 250 for r in ep["rows"])
        assert x["cards"][0]["routeLabel"] == "IN TREND" and x["cards"][0]["plan"] is None, "a held trigger needs no preview"
        y = await e.book_cards("FUDKII_RT_Y", ist_today())
        assert y["cards"][0]["state"] == "SKIPPED" and "dried volume" in y["cards"][0]["skip"]["reason"]
        assert "dried volume on future" in y["cards"][0]["cons"] and "room 2.4 ATR" in y["cards"][0]["pros"]
        n = await e.book_cards("FUDKII_RT_N", ist_today())
        assert n["cards"][0]["state"] == "NOT_MIRRORED", "filled by the parent, nothing recorded for N: mirrored-not, not skipped"
        c = await e.book_cards("FUDKII_CT_X", ist_today())
        assert c["cards"][0]["state"] == "IN_TREND"
        p = await e.book_cards("FUDKII", ist_today())
        assert p["cards"][0]["state"] == "PAPER_FILLED" and p["wallet"]["strategy"] == "FUDKII"
        # a trigger the parent could not fill reads NO_FILL in the mirror books
        s2 = _sig(ts + 1800, symbol="TCS")
        await e.ledger.insert_signal(s2.to_json(), "NO_INSTRUMENT", "no tradeable strike")
        x = await e.book_cards("FUDKII_RT_X", ist_today())
        assert [c["state"] for c in x["cards"]] == ["OPEN", "NO_FILL"] and x["cards"][1]["parentReason"] == "no tradeable strike"
        assert x["cards"][1]["plan"] is None, "TCS is not in this offline universe: no preview, no crash"
        e.underlyings["TCS"] = Instrument("11536", "TCS", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="TCS")
        x = await e.book_cards("FUDKII_RT_X", ist_today())
        assert x["cards"][1]["plan"] == {"ok": False, "reason": x["cards"][1]["plan"]["reason"]} and x["cards"][1]["plan"]["reason"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_operator_take_and_skip_go_through_the_ordinary_paths_and_are_audited(settings):
    e = Engine(settings)
    await e.start()
    try:
        sig = _sig(int(time.time()) - 60)
        e._signals_today[sig.signal_id] = sig
        handled = []

        async def capture(s, bar):
            handled.append(s)

        e._handle_signal = capture  # type: ignore[method-assign]
        out = await e.operator_take("FUDKII_RT_X", sig.signal_id)
        assert handled[0].strategy is StrategyKey.FUDKII_RT_X and handled[0].direction is Direction.BULLISH and "operator take" in handled[0].reason
        assert out["entered"] is False, "nothing filled in this offline engine — reported honestly"
        with pytest.raises(KeyError):
            await e.operator_take("FUDKII_RT_X", "nope")
        # skip: an open position in the book on that trigger closes through _exit with reason MANUAL
        opt = Instrument("45678", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=250, strike=1500.0, option_type=OptionType.CE, underlying="RELIANCE")
        und = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE")
        pos = Position(id="p1", strategy="FUDKII_RT_X", instrument=opt, underlying=und, side=PosSide.LONG, qty=250, entry=50.0,
                       opened_ts=time.time(), signal_id=sig.signal_id, direction=Direction.BULLISH)
        e.positions[pos.id] = pos
        exits = []

        async def fake_exit(p, decision, now):
            exits.append((p.id, decision.reason.value, decision.qty, decision.note))

        e._exit = fake_exit  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await e.operator_take("FUDKII_RT_X", sig.signal_id)  # already held
        out = await e.operator_skip("FUDKII_RT_X", sig.signal_id)
        assert exits == [("p1", "MANUAL", 250, "operator skip")] and out["positionId"] == "p1"
        with pytest.raises(KeyError):
            await e.operator_skip("FUDKII_RT_Y", sig.signal_id)
        kinds = [r["kind"] for r in await e.ledger.rows_between("events", time.time() - 60, time.time() + 1)]
        assert kinds.count("operator.take") == 1 and kinds.count("operator.skip") == 1
    finally:
        await e.stop()
