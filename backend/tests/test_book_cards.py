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

        async def capture(s, bar, **kw):
            handled.append(s)
            return {}

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


@pytest.mark.asyncio
async def test_a_fade_book_describes_its_own_side_never_the_triggers_contract(settings):
    """NAM-INDIA, 2026-09-25 14:45: a bullish trigger, routed COUNTER, no PE fade plan. The CT-Y card
    showed the trigger's CE contract, stop and targets as if CT-Y would buy them. A fade book now
    shows the fade's own side and levels, and with no fade it shows no contract at all."""
    e = Engine(settings)
    await e.start()
    try:
        ts = int(datetime.combine(ist_today(), dtime(14, 15), tzinfo=IST).timestamp())
        trig = _sig(ts, symbol="NAM-INDIA", entry=1157.5, stop=1150.0, targets=(1180.0,))
        await e.ledger.insert_signal(trig.to_json(), "NO_INSTRUMENT", "no tradeable strike")
        await e.ledger.event("counter.route", {"signal_id": trig.signal_id, "symbol": "NAM-INDIA", "route": "COUNTER",
                                               "reason": "at-pivot", "summary": "at-pivot", "wall": {"members": []}, "reads": []})
        # 1. no fade plan: the CT card is red (the side it WOULD trade), and shows no contract
        for book in ("FUDKII_CT_X", "FUDKII_CT_Y"):
            c = next(x for x in (await e.book_cards(book, ist_today()))["cards"] if x["symbol"] == "NAM-INDIA")
            assert c["state"] == "COUNTER_NO_PLAN" and c["side"] == "PE" and c["rtCard"] is None and c["plan"] is None
            assert c["describes"] == "trigger" and c["triggerDirection"] == "BULLISH"
        # the in-trend books keep the trigger's side
        r = next(x for x in (await e.book_cards("FUDKII_RT_Y", ist_today()))["cards"] if x["symbol"] == "NAM-INDIA")
        assert r["side"] == "CE" and r["direction"] == "BULLISH"
        # 2. with a fade: the CT card carries the FADE's direction and levels
        fade = _sig(ts, strategy=StrategyKey.FUDKII_CT_X, symbol="NAM-INDIA", direction=Direction.BEARISH, entry=1157.5,
                    stop=1163.0, targets=(1140.0,), source_signal_id=trig.signal_id, reason="fade at the 1156.50 cluster")
        await e.ledger.insert_signal(fade.to_json(), "NO_INSTRUMENT", "no tradeable strike")
        c = next(x for x in (await e.book_cards("FUDKII_CT_Y", ist_today()))["cards"] if x["symbol"] == "NAM-INDIA")
        assert c["describes"] == "fade" and c["direction"] == "BEARISH" and c["side"] == "PE"
        assert c["stop"] == 1163.0 and c["targets"] == [1140.0] and c["triggerDirection"] == "BULLISH"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_card_that_cannot_be_taken_still_names_its_option_greyed_out_with_the_reason(settings):
    """NAM-INDIA, 2026-09-25 14:45 — the operator's ask: "the CTA should be the option but then grey
    it out / disable and then below that give a reason". RT-Y (trigger side) found no tradeable CE;
    CT-Y (fade side) had no fade plan. Both buttons now name the contract they are about."""
    from datetime import timedelta

    from kotsin_nse.bars.unified import BarSource, UnifiedBar
    from kotsin_nse.instrument.select import Quote

    e = Engine(settings)
    await e.start()
    try:
        exp = (ist_today() + timedelta(days=5)).isoformat()
        und = Instrument("357", "NAM-INDIA", Segment.NSE_EQ, InstrumentKind.EQUITY, name="NAM-INDIA", tick_size=0.05, underlying="NAM-INDIA")
        def opt(code, k, ot):
            return Instrument(code, "NAM-INDIA", Segment.NSE_FO, InstrumentKind.OPTION, name=f"NAM-INDIA {exp} {ot.value} {k:.2f}",
                              lot_size=800, tick_size=0.05, expiry=exp, strike=k, option_type=ot, underlying="NAM-INDIA")
        chain = [opt(f"C{k}", float(k), OptionType.CE) for k in (1140, 1160, 1180, 1200)] + [opt(f"P{k}", float(k), OptionType.PE) for k in (1100, 1120, 1140, 1160)]
        cat = e.catalogue_loader.catalogue
        cat.equity_by_symbol["NAM-INDIA"] = und
        cat.by_code[und.scrip_code] = und
        for i in chain:
            cat.by_code[i.scrip_code] = i
            cat.options_by_symbol.setdefault("NAM-INDIA", []).append(i)
        e.underlyings["NAM-INDIA"] = und
        base = int(datetime.combine(ist_today(), dtime(9, 15), tzinfo=IST).timestamp())
        e.store.seed("NAM-INDIA", "30m", [  # an ATR for the strike picker: 30m bars ~20 wide
            UnifiedBar("NAM-INDIA", "357", "30m", base - (20 - n) * 1800, 1150.0 + n, 1160.0 + n, 1140.0 + n, 1152.0 + n, 1000.0,
                       source=BarSource.REST, complete=True)
            for n in range(20)
        ])
        now = time.time()
        for i in chain:  # every strike one-sided: nothing is tradeable
            e.quotes[i.scrip_code] = Quote(ltp=5.0, bid=0.0, ask=5.0, ts=now)
        ts = int(datetime.combine(ist_today(), dtime(14, 15), tzinfo=IST).timestamp())
        trig = _sig(ts, symbol="NAM-INDIA", entry=1157.5, stop=1150.0, targets=(1180.0,))
        await e.ledger.insert_signal(trig.to_json(), "NO_INSTRUMENT", "no tradeable strike")
        await e.ledger.event("counter.route", {"signal_id": trig.signal_id, "symbol": "NAM-INDIA", "route": "COUNTER",
                                               "reason": "at-pivot", "summary": "at-pivot 1.43", "wall": {"members": []}, "reads": []})
        await e.ledger.event("counter.no_plan", {"signal_id": trig.signal_id, "symbol": "NAM-INDIA", "grade": "F", "rr": 0.81,
                                                 "reason": "fade graded F: first target 1150 is 7.50 away, stop 1166.7 is 9.20 away — RR 0.81"})

        ry = next(x for x in (await e.book_cards("FUDKII_RT_Y", ist_today()))["cards"] if x["symbol"] == "NAM-INDIA")
        cta = ry["cta"]
        assert cta["enabled"] is False and cta["type"] == "CE" and cta["contract"] and "CE" in cta["contract"]
        assert "one-sided" in cta["reason"] and ":" not in cta["reason"].split("strike:")[-1].split(",")[0].strip()

        cy = next(x for x in (await e.book_cards("FUDKII_CT_Y", ist_today()))["cards"] if x["symbol"] == "NAM-INDIA")
        cta = cy["cta"]
        assert cta["enabled"] is False and cta["type"] == "PE" and "PE" in (cta["contract"] or "")
        assert float(cta["strike"]) < 1157.5, "the fade's put is out of the money below the trigger"
        assert "RR 0.81" in cta["reason"] and "COUNTER-TREND" in cta["reason"], "the planner's real refusal, not a generic one"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_every_card_shows_which_books_bought_the_trigger_and_which_still_hold_it(settings):
    """The operator, 2026-09-26: "with a coloured glow circle, show all strategies that bought that
    trade" — and an EXITED label on a trade that is over. The `books` row resolves each book's
    position on the trigger the way its own card does: the trigger's id for the in-trend books, the
    fade's id for the counter books."""
    from kotsin_nse.domain import ExitDecision, ExitReason
    from kotsin_nse.engine import _trade_from, _trade_json
    from kotsin_nse.risk.exits import apply_exit

    e = Engine(settings)
    await e.start()
    try:
        ts = int(datetime.combine(ist_today(), dtime(11, 15), tzinfo=IST).timestamp())
        sig = _sig(ts)
        await e.ledger.insert_signal(sig.to_json(), "PAPER_FILLED", sig.reason)
        e.underlyings["RELIANCE"] = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE")
        opt = Instrument("45678", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=250, strike=1520.0, option_type=OptionType.CE, underlying="RELIANCE")
        und = e.underlyings["RELIANCE"]

        def pos(pid, book, sid, inst=opt, direction=Direction.BULLISH):
            return Position(id=pid, strategy=book, instrument=inst, underlying=und, side=PosSide.LONG, qty=250, entry=20.0,
                            opened_ts=ts + 1805, signal_id=sid, direction=direction, equity_entry=1500.0, equity_sl=1490.0, option_sl=16.0)

        parent, rtx, rty = pos("p-par", "FUDKII", sig.signal_id), pos("p-x", "FUDKII_RT_X", sig.signal_id), pos("p-y", "FUDKII_RT_Y", sig.signal_id)
        for closed, px in ((parent, 23.0), (rty, 18.0)):
            apply_exit(closed, ExitDecision(closed.id, ExitReason.SL_OP, px, 250, "stop"), fill_price=px, charges=20.0, now=ts + 2400)
            await e.ledger.insert_trade(_trade_json(_trade_from(closed, ts + 2400)))
        for p in (parent, rtx, rty):
            e.positions[p.id] = p
            await e.ledger.upsert_position(_position_json(p))

        cards = (await e.book_cards("FUDKII_RT_X", ist_today()))["cards"]
        rows = {b["book"]: b for b in cards[0]["books"]}
        assert [b["book"] for b in cards[0]["books"]] == ["FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y", "FUDKII_CT_X", "FUDKII_CT_Y"]
        assert rows["FUDKII_RT_X"]["status"] == "OPEN" and rows["FUDKII_RT_X"]["side"] == "CE" and rows["FUDKII_RT_X"]["label"] == "RT-X"
        assert rows["FUDKII"]["status"] == "EXITED" and rows["FUDKII_RT_Y"]["status"] == "EXITED"
        assert rows["FUDKII_RT_Y"]["exitReason"] and rows["FUDKII_RT_Y"]["pnl"] == pytest.approx(250 * (18.0 - 20.0) - 20.0)
        assert rows["FUDKII_RT_N"]["status"] == rows["FUDKII_CT_X"]["status"] == rows["FUDKII_CT_Y"]["status"] == "NONE"
        assert rows["FUDKII_RT_N"]["side"] is None and rows["FUDKII_RT_N"]["closedTs"] is None
        # every book's card carries the same row: RT-Y's own card reads it as EXITED
        y = (await e.book_cards("FUDKII_RT_Y", ist_today()))["cards"][0]
        assert {b["book"]: b["status"] for b in y["books"]} == {b: r["status"] for b, r in rows.items()}

        # a fade: CT-X holds the PE under the FADE's id, and the trigger's card shows it
        fade = _sig(ts, strategy=StrategyKey.FUDKII_CT_X, direction=Direction.BEARISH, entry=1500.0, stop=1508.0,
                    targets=(1485.0,), source_signal_id=sig.signal_id, reason="fade at the wall")
        await e.ledger.insert_signal(fade.to_json(), "PAPER_FILLED", fade.reason)
        pe = Instrument("45679", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=250, strike=1480.0, option_type=OptionType.PE, underlying="RELIANCE")
        ctx = pos("p-ctx", "FUDKII_CT_X", fade.signal_id, inst=pe, direction=Direction.BEARISH)
        e.positions[ctx.id] = ctx
        await e.ledger.upsert_position(_position_json(ctx))
        rows = {b["book"]: b for b in (await e.book_cards("FUDKII_RT_X", ist_today()))["cards"][0]["books"]}
        assert rows["FUDKII_CT_X"]["status"] == "OPEN" and rows["FUDKII_CT_X"]["side"] == "PE"
        assert rows["FUDKII_CT_Y"]["status"] == "NONE"
    finally:
        await e.stop()


def test_the_cards_spread_is_a_percent_not_a_hundred_times_it():
    """Quote.spread_pct is already in percent; the plan preview and the disabled button multiplied it
    by 100 again, so a 3.5 % spread read 350 % (found by the Sep 1-25 replay, 2026-09-26)."""
    from kotsin_nse.instrument.select import Quote

    q = Quote(ltp=10.0, bid=9.825, ask=10.175, ts=0.0)
    assert round(q.spread_pct, 2) == 3.5
    import inspect

    from kotsin_nse.engine import Engine

    src = inspect.getsource(Engine._plan_preview) + inspect.getsource(Engine._aim_contract)
    assert "spread_pct * 100" not in src


@pytest.mark.asyncio
async def test_a_counter_trend_card_in_an_in_trend_tab_offers_the_counter_trend_buy_and_greys_the_trend_one(settings):
    """KALYANKJIL, 2026-09-29 09:45 (operator: "the CTA has to be the OTM we are to buy in counter-trend as
    active CTA and the [trend CE] as inactive CTA … also ensure in all CTA, the lots, its price and total
    required"). The trigger was routed COUNTER-TREND; CT-Y bought the 540 PE — RT-Y's tab names that trade
    as its counter-trend button, with its lots, price and money; RT-X's tab names CT-X's."""
    from kotsin_nse.domain import ExitDecision, ExitReason
    from kotsin_nse.engine import _trade_from, _trade_json
    from kotsin_nse.risk.exits import apply_exit

    e = Engine(settings)
    await e.start()
    try:
        ts = int(datetime.combine(ist_today(), dtime(9, 15), tzinfo=IST).timestamp())
        und = Instrument("21327", "KALYANKJIL", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="KALYANKJIL", tick_size=0.05)
        e.underlyings["KALYANKJIL"] = und
        trig = _sig(ts, symbol="KALYANKJIL", entry=575.95, stop=571.0, targets=(590.0,))
        e._signals_today[trig.signal_id] = trig
        await e.ledger.insert_signal(trig.to_json(), "NO_INSTRUMENT", "no tradeable strike")
        await e.ledger.event("counter.route", {"signal_id": trig.signal_id, "symbol": "KALYANKJIL", "route": "COUNTER",
                                               "reason": "wall-counter", "summary": "wall 6.4 (1d/1wk) on the future", "wall": {"members": []}, "reads": []})
        gap = _sig(ts, strategy=StrategyKey.FUDKII_CT_Y, symbol="KALYANKJIL", direction=Direction.BEARISH, entry=575.95, stop=580.25,
                   targets=(565.65,), source_signal_id=trig.signal_id, reason="GAP FADE")
        await e.ledger.insert_signal(gap.to_json(), "PAPER_FILLED", gap.reason)
        pe = Instrument("87717", "KALYANKJIL", Segment.NSE_FO, InstrumentKind.OPTION, name="KALYANKJIL 27 OCT 2026 PE 540.00", lot_size=1350,
                        strike=540.0, option_type=OptionType.PE, underlying="KALYANKJIL")
        p = Position(id="p-cty", strategy="FUDKII_CT_Y", instrument=pe, underlying=und, side=PosSide.LONG, qty=5400, entry=12.6,
                     opened_ts=ts + 1854, signal_id=gap.signal_id, direction=Direction.BEARISH, equity_entry=575.95, equity_sl=580.25, option_sl=11.96)
        apply_exit(p, ExitDecision(p.id, ExitReason.TRAIL, 14.5625, 5400, "trail"), fill_price=14.5625, charges=299.67, now=ts + 3800)
        await e.ledger.insert_trade(_trade_json(_trade_from(p, ts + 3800)))
        await e.ledger.upsert_position(_position_json(p))

        y = next(x for x in (await e.book_cards("FUDKII_RT_Y", ist_today()))["cards"] if x["symbol"] == "KALYANKJIL")
        cc = y["ctaCounter"]
        assert cc["book"] == "FUDKII_CT_Y" and cc["action"] == "taken" and cc["enabled"] is False
        assert cc["contract"] == "KALYANKJIL 27 OCT 2026 PE 540.00" and (cc["lots"], cc["qty"], cc["premium"], cc["outlay"]) == (4, 5400, 12.6, 68040.0)
        assert "traded it — closed (TRAIL)" in cc["reason"] and "net ₹" in cc["reason"]
        assert y["cta"]["enabled"] is False and "routed COUNTER-TREND" in y["cta"]["reason"], "the trend buy is greyed"

        # CT-Y's own tab is untouched: its button is the PE it traded, sized — no greyed CE, no second button
        ct = next(c for c in (await e.book_cards("FUDKII_CT_Y", ist_today()))["cards"] if c["symbol"] == "KALYANKJIL")
        assert ct["ctaCounter"] is None and ct["cta"]["type"] == "PE" and ct["cta"]["action"] == "taken"
        assert ct["cta"]["contract"] == "KALYANKJIL 27 OCT 2026 PE 540.00" and (ct["cta"]["lots"], ct["cta"]["outlay"]) == (4, 68040.0)

        x = next(c for c in (await e.book_cards("FUDKII_RT_X", ist_today()))["cards"] if c["symbol"] == "KALYANKJIL")
        assert x["ctaCounter"]["book"] == "FUDKII_CT_X" and x["ctaCounter"]["action"] == "take"
        assert x["ctaCounter"]["enabled"] is False and x["ctaCounter"]["reason"].startswith("no fade plan"), "no zones here: no plan, said so"

        # RT-X's greyed trend buy says what its counter-trend button really is — not "the live button"
        assert "no CT-X counter-trend buy now (no fade plan" in x["cta"]["reason"]

        # a gap-fade trigger CT-Y did not enter: its decision stands — no operator fade under the gap fade's id
        trig3 = _sig(ts + 3600, symbol="KALYANKJIL", entry=578.0, stop=573.0, targets=(590.0,))
        e._signals_today[trig3.signal_id] = trig3
        await e.ledger.insert_signal(trig3.to_json(), "NO_INSTRUMENT", "no tradeable strike")
        await e.ledger.event("counter.route", {"signal_id": trig3.signal_id, "symbol": "KALYANKJIL", "route": "COUNTER",
                                               "reason": "wall-counter", "summary": "wall", "wall": {"members": []}, "reads": []})
        gap3 = _sig(ts + 3600, strategy=StrategyKey.FUDKII_CT_Y, symbol="KALYANKJIL", direction=Direction.BEARISH, entry=578.0, stop=582.0,
                    targets=(565.65,), source_signal_id=trig3.signal_id, reason="GAP FADE")
        await e.ledger.insert_signal(gap3.to_json(), "MISSED_NOT_FILLED", "limit not filled in 60 s")
        y3 = next(c for c in (await e.book_cards("FUDKII_RT_Y", ist_today()))["cards"] if c["signalId"] == trig3.signal_id)
        assert y3["ctaCounter"]["enabled"] is False and "gap fade owns this trigger: MISSED_NOT_FILLED" in y3["ctaCounter"]["reason"]

        # a trigger routed IN TREND carries no counter-trend button
        trig2 = _sig(ts + 1800, symbol="KALYANKJIL", entry=577.0, stop=572.0, targets=(590.0,))
        await e.ledger.insert_signal(trig2.to_json(), "NO_INSTRUMENT", "no tradeable strike")
        await e.ledger.event("counter.route", {"signal_id": trig2.signal_id, "symbol": "KALYANKJIL", "route": "IN_TREND",
                                               "reason": "no wall", "summary": "no wall", "wall": {"members": []}, "reads": []})
        y2 = next(c for c in (await e.book_cards("FUDKII_RT_Y", ist_today()))["cards"] if c["signalId"] == trig2.signal_id)
        assert y2["ctaCounter"] is None
    finally:
        await e.stop()


def test_a_fade_refused_on_its_stop_says_so_not_no_wall():
    """KALYANKJIL 2026-09-29 09:45: the fade's stop (1wk.PIVOT, 0.83 above the close) was inside one bar's
    noise; the walls below it existed. The refusal named "no wall ahead" — it names the stop now."""
    from kotsin_nse.bars.pivots import GradePolicy, Zone
    from kotsin_nse.strategy.counter import fade_refusal

    trig = _sig(0, symbol="KALYANKJIL", entry=575.95, stop=571.0, targets=(590.0,))
    zones = [Zone(551.64, 7.2, ["1d.S2", "1wk.S2"]), Zone(562.71, 5.2, ["1wk.S1", "1mo.S1"]), Zone(565.67, 12.0, ["1d.BC", "1d.PIVOT", "1d.TC"]),
             Zone(576.78, 3.2, ["1wk.PIVOT"]), Zone(580.17, 4.0, ["1d.R2"])]
    why = fade_refusal(trig, zones=zones, atr=4.15, tick_size=0.05, policy=GradePolicy(min_stop_atr_filter=0.5))
    assert why["reason"].startswith("the fade's stop 576.8") and "inside one bar's noise" in why["reason"] and "no wall" not in why["reason"]
