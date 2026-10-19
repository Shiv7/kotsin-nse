"""The daily contract roll (2026-10-19). A group's futures were picked once, at boot: engine A, up from
10 Oct, read and traded CRUDEOIL 19 Oct to its last day although the MCX roll (5 days or fewer to
expiry reads the next month) had moved it to November on 14 Oct, and from 20 Oct it would have had no
feed for the name at all. Every day from CONTRACT_ROLL_HM the engine now re-picks with the boot's own
rule and moves what changed; until a roll has happened no book enters on the old month."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Any

import pytest

from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment
from kotsin_nse.domain import (
    Direction,
    Instrument,
    InstrumentKind,
    OrderIntent,
    OrderSide,
    Position,
    PosSide,
    Purpose,
)
from kotsin_nse.engine import CONTRACT_ROLL_RETRY_S, Engine
from kotsin_nse.exec.resting import Resting
from kotsin_nse.instrument.catalogue import Catalogue
from kotsin_nse.instrument.select import Quote
from kotsin_nse.instrument.universe import UniverseBuilder
from kotsin_nse.market.session import IST, ist_today
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.keys import StrategyKey

TODAY = ist_today()
BOOT = TODAY - timedelta(days=6)
OLD = TODAY - timedelta(days=60)  # a session the new month's history does not cover: only a drop removes it


def _at(d, hh: int, mm: int = 0) -> float:
    return datetime.combine(d, dtime(hh, mm), tzinfo=IST).timestamp()


NIGHT = _at(TODAY, 1, 5)  # past the roll slot, outside every session's hours


def _fut(code: str, root: str, days: int, seg: Segment) -> Instrument:
    return Instrument(code, root, seg, InstrumentKind.FUTURE, name=f"{root} {code}", underlying=root, lot_size=100,
                      expiry=(TODAY + timedelta(days=days)).isoformat())


C_OCT, C_NOV, C_DEC = (_fut("C_OCT", "CRUDEOIL", 3, Segment.MCX_FO), _fut("C_NOV", "CRUDEOIL", 34, Segment.MCX_FO),
                       _fut("C_DEC", "CRUDEOIL", 63, Segment.MCX_FO))
N_OCT, N_NOV, N_DEC = (_fut("N_OCT", "RELIANCE", -1, Segment.NSE_FO), _fut("N_NOV", "RELIANCE", 27, Segment.NSE_FO),
                       _fut("N_DEC", "RELIANCE", 55, Segment.NSE_FO))
I_OCT, I_NOV, I_DEC = (_fut("I_OCT", "NIFTYNXT50", -1, Segment.NSE_FO), _fut("I_NOV", "NIFTYNXT50", 27, Segment.NSE_FO),
                       _fut("I_DEC", "NIFTYNXT50", 55, Segment.NSE_FO))
G_DEC = _fut("G_DEC", "GOLD", 45, Segment.MCX_FO)
R_EQ = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, name="RELIANCE", lot_size=1)


def _catalogue() -> Catalogue:
    cat = Catalogue()
    for f in (C_OCT, C_NOV, C_DEC, N_OCT, N_NOV, N_DEC, I_OCT, I_NOV, I_DEC, G_DEC):
        cat.by_code[f.scrip_code] = f
        cat.futures_by_symbol[f.underlying].append(f)
    cat.by_code[R_EQ.scrip_code] = R_EQ
    cat.equity_by_symbol[R_EQ.symbol] = R_EQ
    return cat


class Feed:
    def __init__(self, held: set[tuple[str, str]]) -> None:
        self.held = set(held)
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def is_subscribed(self, ch: str, code: str) -> bool:
        return (ch, code) in self.held

    async def subscribe(self, ch: str, insts: list[Instrument]) -> None:
        self.calls.append(("s", ch, tuple(i.scrip_code for i in insts)))
        self.held |= {(ch, i.scrip_code) for i in insts}

    async def unsubscribe(self, ch: str, insts: list[Instrument]) -> None:
        self.calls.append(("u", ch, tuple(i.scrip_code for i in insts)))
        self.held -= {(ch, i.scrip_code) for i in insts}

    def subscribed(self, op: str, ch: str) -> set[str]:
        return {c for o, k, codes in self.calls if o == op and k == ch for c in codes}


class Rest:
    def __init__(self, fail: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.fail = fail
        self.asked: list[tuple[str, str]] = []

    async def candles(self, inst: Instrument, tf: str, start: str, end: str) -> list[dict[str, Any]]:
        self.asked.append((inst.scrip_code, tf))
        if inst.scrip_code in self.fail:
            raise RuntimeError("broker down")
        price = {"C_NOV": 9388.0, "I_NOV": 70000.0}.get(inst.scrip_code, 100.0)
        days = [TODAY - timedelta(days=k) for k in range(40, 0, -1) if (TODAY - timedelta(days=k)).weekday() < 5]
        if tf == "1d":
            return [{"dt": f"{d.isoformat()} 00:00:00", "o": price, "h": price + 50, "l": price - 50, "c": price, "v": 1000.0}
                    for d in days]
        return [{"dt": f"{d.isoformat()} {hm}:00", "o": price, "h": price + 5, "l": price - 5, "c": price, "v": 10.0}
                for d in days[-5:] for hm in ("10:00", "10:30", "11:00")]


class Ledger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def event(self, kind: str, data: dict[str, Any]) -> None:
        self.events.append((kind, data))


def _bar(inst: Instrument, tf: str, ts: float, c: float) -> UnifiedBar:
    return UnifiedBar(inst.symbol, inst.scrip_code, tf, int(ts), c, c, c, c, 1.0, source=BarSource.REST, complete=True)


def _engine(settings, *, rest: Rest | None = None) -> tuple[Engine, Feed]:
    """An engine as booted on BOOT (six days ago), with the old months' bars in its store."""
    e = Engine(settings)
    cat = _catalogue()
    e.catalogue_loader.catalogue = cat
    e.universe_builder = UniverseBuilder(cat)
    e.groups = e.universe_builder.build_underlyings([Segment.NSE_FO, Segment.MCX_FO], BOOT)
    e.underlyings = {g.root: g.underlying for g in e.groups.values()}
    e._contract_roll_day = BOOT.isoformat()
    e._future_to_underlying = {f.scrip_code: g.root for g in e.groups.values() for f in g.futures}
    for g in e.groups.values():
        e.aggregator.track(g.underlying)
        e.store.seed(g.underlying.symbol, "30m", [_bar(g.underlying, "30m", _at(OLD, 10), 9572.0)])
        e.store.seed(g.underlying.symbol, "1d", [_bar(g.underlying, "1d", _at(OLD, 0), 9572.0)])
    held = {(ch, i.scrip_code) for g in e.groups.values() for i in (g.underlying, *g.futures) for ch in ("mf", "oi")}
    feed = Feed(held)
    e.feed = feed  # type: ignore[assignment]
    e.rest = rest or Rest()  # type: ignore[assignment]
    e.ledger = Ledger()  # type: ignore[assignment]
    e.reconciler_ready = True
    e.booting = False
    return e, feed


def test_the_boot_held_october_and_today_the_rule_says_november():
    b = UniverseBuilder(_catalogue())
    groups = b.build_underlyings([Segment.NSE_FO, Segment.MCX_FO], BOOT)
    assert groups["CRUDEOIL"].underlying is C_OCT and [f.scrip_code for f in groups["CRUDEOIL"].futures] == ["C_OCT", "C_NOV"]
    assert b.roll_plan(groups, BOOT) == [], "the boot's own day owes nothing"
    plan = {r.root: r for r in b.roll_plan(groups, TODAY)}
    assert set(plan) == {"CRUDEOIL", "RELIANCE", "NIFTYNXT50"}, "GOLD's only month is far off: nothing to roll"
    assert plan["CRUDEOIL"].new_underlying is C_NOV and [f.scrip_code for f in plan["CRUDEOIL"].new] == ["C_NOV", "C_DEC"]
    assert plan["RELIANCE"].new_underlying is None, "a cash-led group keeps its equity"
    assert [f.scrip_code for f in plan["RELIANCE"].new] == ["N_NOV", "N_DEC"]
    assert plan["NIFTYNXT50"].new_underlying is I_NOV, "an index with no cash leg reads its front future"
    fresh = b.build_underlyings([Segment.NSE_FO, Segment.MCX_FO], TODAY)
    for root, r in plan.items():
        assert [f.scrip_code for f in r.new] == [f.scrip_code for f in fresh[root].futures], "the roll and a boot today agree"


@pytest.mark.asyncio
async def test_an_mcx_group_moves_onto_the_new_month_bars_and_all(settings):
    e, feed = _engine(settings)
    rolled = await e._roll_contracts(now=NIGHT)
    assert {r["root"] for r in rolled} == {"CRUDEOIL", "RELIANCE", "NIFTYNXT50"}
    assert e.underlyings["CRUDEOIL"] is C_NOV and e.groups["CRUDEOIL"].underlying is C_NOV
    assert [f.scrip_code for f in e.groups["CRUDEOIL"].futures] == ["C_NOV", "C_DEC"]
    for tf in ("30m", "1d"):
        bars = e.store.bars("CRUDEOIL", tf)
        assert bars and {b.scrip_code for b in bars} == {"C_NOV"}, f"{tf}: not one October bar left"
        assert {b.close for b in bars} == {9388.0}
    assert "C_NOV" in e.aggregator.state and "C_OCT" not in e.aggregator.state
    assert e._future_to_underlying.get("C_DEC") == "CRUDEOIL" and "C_OCT" not in e._future_to_underlying
    assert "C_DEC" in feed.subscribed("s", "mf") and "C_DEC" in feed.subscribed("s", "oi")
    assert "C_NOV" not in feed.subscribed("s", "mf"), "already streaming as the next month: not sent again"
    assert "C_OCT" in feed.subscribed("u", "mf") and "C_OCT" in feed.subscribed("u", "oi")
    assert e._contract_roll_day == TODAY.isoformat() and e._contract_roll_pending == {}
    assert e.contract_drift(TODAY) == []
    assert any(k == "contracts.rolled" for k, _ in e.ledger.events)  # type: ignore[attr-defined]
    assert e._contracts_check(now=_at(TODAY, 2)).ok


@pytest.mark.asyncio
async def test_a_tick_on_the_dead_month_builds_nothing_after_the_roll(settings):
    e, _ = _engine(settings)
    await e._roll_contracts(now=NIGHT)
    before = e.store.bars("CRUDEOIL", "30m")
    await e.aggregator.on_tick({"scrip_code": "C_OCT", "ltp": 9572.0, "ts": _at(TODAY, 10, 5)})
    assert e.store.forming("CRUDEOIL", "30m") is None and e.store.bars("CRUDEOIL", "30m") == before


@pytest.mark.asyncio
async def test_an_nse_group_rolls_its_oi_legs_and_leaves_the_equity_alone(settings):
    e, feed = _engine(settings)
    eq_bars = e.store.bars("RELIANCE", "30m")
    await e._roll_contracts(now=NIGHT)
    g = e.groups["RELIANCE"]
    assert g.underlying is R_EQ and e.underlyings["RELIANCE"] is R_EQ
    assert [f.scrip_code for f in g.futures] == ["N_NOV", "N_DEC"]
    assert e.store.bars("RELIANCE", "30m") == eq_bars, "the equity's bars are not the futures' business"
    assert ("N_OCT", "RELIANCE") not in e._future_to_underlying.items()
    assert "N_DEC" in feed.subscribed("s", "oi") and "N_OCT" in feed.subscribed("u", "oi")
    assert "2885" not in feed.subscribed("u", "mf"), "the equity is never unsubscribed"


@pytest.mark.asyncio
async def test_a_failed_history_call_keeps_the_old_month_and_is_retried(settings):
    e, feed = _engine(settings, rest=Rest(fail={"C_NOV"}))
    await e._roll_contracts(now=NIGHT)
    assert e.underlyings["CRUDEOIL"] is C_OCT, "no history for the new month: the old one stands"
    assert {b.scrip_code for b in e.store.bars("CRUDEOIL", "30m")} == {"C_OCT"}
    assert "CRUDEOIL" in e._contract_roll_pending and "failed" in e._contract_roll_pending["CRUDEOIL"]
    assert "C_OCT" not in feed.subscribed("u", "mf"), "the month still read is not dropped"
    assert [f.scrip_code for f in e.groups["RELIANCE"].futures] == ["N_NOV", "N_DEC"], "one failure costs no other group"
    assert e._contract_roll_day != TODAY.isoformat()
    e._contract_roll_last_try = NIGHT
    assert not e._contract_roll_due(NIGHT + 60) and e._contract_roll_due(NIGHT + CONTRACT_ROLL_RETRY_S)
    check = e._contracts_check(now=_at(TODAY, 2))
    assert not check.ok and "CRUDEOIL" in check.detail
    e.rest = Rest()  # type: ignore[assignment]
    await e._roll_contracts(now=NIGHT + CONTRACT_ROLL_RETRY_S)
    assert e.underlyings["CRUDEOIL"] is C_NOV and e._contract_roll_day == TODAY.isoformat()


@pytest.mark.asyncio
async def test_never_swapped_in_session_hours_nor_under_an_open_position(settings):
    e, _ = _engine(settings)
    await e._roll_contracts(now=_at(TODAY, 10, 0))
    assert e.underlyings["CRUDEOIL"] is C_OCT and "session hours" in e._contract_roll_pending["CRUDEOIL"]
    assert [f.scrip_code for f in e.groups["RELIANCE"].futures] == ["N_NOV", "N_DEC"], "an OI leg can move any time"

    e, _ = _engine(settings)
    e.positions["p"] = Position(id="p", strategy="FUDKII_RT_MCX", instrument=C_OCT, underlying=C_OCT, side=PosSide.LONG,
                                qty=1, entry=9572.0, opened_ts=time.time(), signal_id="s", direction=Direction.BULLISH)
    await e._roll_contracts(now=NIGHT)
    assert e.underlyings["CRUDEOIL"] is C_OCT and "open" in e._contract_roll_pending["CRUDEOIL"]


@pytest.mark.asyncio
async def test_no_entry_on_the_expiring_month_until_the_roll(settings):
    e, _ = _engine(settings)
    now = time.time()
    e.quotes["C_OCT"] = Quote(ltp=9572.0, bid=9571.0, ask=9573.0, ts=now)
    e.quotes["C_NOV"] = Quote(ltp=9388.0, bid=9387.0, ask=9389.0, ts=now)
    sig = Signal(strategy=StrategyKey.FUDKII, symbol="CRUDEOIL", direction=Direction.BULLISH, ts=int(now) // 1800 * 1800,
                 entry=9572.0, stop=9500.0)
    sel = await e._select_instrument(C_OCT, sig, tape=False)
    assert sel.instrument is None and "not today's contract" in sel.reason
    await e._roll_contracts(now=NIGHT)
    sel = await e._select_instrument(e.underlyings["CRUDEOIL"], sig, tape=False)
    assert sel.instrument is C_NOV


def test_the_roll_runs_once_a_day_from_its_slot(settings):
    e, _ = _engine(settings)
    e._contract_roll_day = TODAY.isoformat()
    assert not e._contract_roll_due(_at(TODAY, 2)), "the day the engine booted is never rolled again"
    e._contract_roll_day = (TODAY - timedelta(days=1)).isoformat()
    assert not e._contract_roll_due(_at(TODAY, 0, 30)), "inside the login window: not yet"
    assert e._contract_roll_due(_at(TODAY, 1, 0))
    e.booting = True
    assert not e._contract_roll_due(_at(TODAY, 1, 0)), "never under a boot"


@pytest.mark.asyncio
async def test_a_daily_refetch_in_flight_for_the_old_month_never_lands_on_the_new_one(settings):
    """``_refetch_daily`` resolves the contract, awaits the broker, then installs: a roll in between must not
    let the old month's candles back into the series."""
    e, _ = _engine(settings)
    await e._roll_contracts(now=NIGHT)
    e._seed_daily(C_OCT, [{"dt": f"{(TODAY - timedelta(days=1)).isoformat()} 00:00:00", "o": 9572.0, "h": 9600.0,
                           "l": 9500.0, "c": 9572.0, "v": 5.0}])
    assert {b.scrip_code for b in e.store.bars("CRUDEOIL", "1d")} == {"C_NOV"}


# -- review, 2026-10-19: a partial master is never followed; a working order blocks the swap -----------------


def _drop_from_master(e: Engine, *codes: str) -> None:
    """The day's master without ``codes`` — a truncated ScripMaster download parses as a short list."""
    cat = e.catalogue_loader.catalogue
    for code in codes:
        inst = cat.by_code.pop(code)
        cat.futures_by_symbol[inst.underlying].remove(inst)


@pytest.mark.asyncio
async def test_a_partial_master_never_drops_a_contract_that_is_not_due(settings):
    e, feed = _engine(settings)
    _drop_from_master(e, "C_NOV")  # still 34 days to run: only a broken master leaves it out
    plan = {r.root: r for r in e.contract_drift(TODAY)}
    assert plan["CRUDEOIL"].suspect and "C_NOV" in plan["CRUDEOIL"].suspect
    await e._roll_contracts(now=NIGHT)
    assert e.underlyings["CRUDEOIL"] is C_OCT and [f.scrip_code for f in e.groups["CRUDEOIL"].futures] == ["C_OCT", "C_NOV"]
    assert "partial scrip master" in e._contract_roll_pending["CRUDEOIL"]
    assert "C_NOV" not in feed.subscribed("u", "mf") and "C_DEC" not in feed.subscribed("s", "mf")
    assert [f.scrip_code for f in e.groups["RELIANCE"].futures] == ["N_NOV", "N_DEC"], "the rest of the master is still followed"
    assert e._contract_roll_day != TODAY.isoformat(), "owed: retried, alarmed"
    assert any(k == "contracts.roll_pending" for k, _ in e.ledger.events)  # type: ignore[attr-defined]
    check = e._contracts_check(now=_at(TODAY, 2))
    assert not check.ok and "CRUDEOIL" in check.detail


@pytest.mark.asyncio
async def test_a_due_contract_the_master_lists_nothing_after_is_kept_and_alarms(settings):
    e, _ = _engine(settings)
    _drop_from_master(e, "I_NOV", "I_DEC")  # NIFTYNXT50's October future expired yesterday; nothing later listed
    plan = {r.root: r for r in e.contract_drift(TODAY)}
    assert plan["NIFTYNXT50"].new == [] and "lists no" in plan["NIFTYNXT50"].suspect
    assert "nothing listed" in plan["NIFTYNXT50"].describe()
    await e._roll_contracts(now=NIGHT)
    assert e.underlyings["NIFTYNXT50"] is I_OCT, "never left without a contract"
    assert "NIFTYNXT50" in e._contract_roll_pending
    assert not e._contracts_check(now=_at(TODAY, 2)).ok


def test_a_pick_never_moves_backwards(settings):
    e, _ = _engine(settings)
    early = _fut("C_EARLY", "CRUDEOIL", 1, Segment.MCX_FO)  # a month before the held front: never listed after it
    later = _fut("C_LATER", "CRUDEOIL", 20, Segment.MCX_FO)
    e.groups["CRUDEOIL"].futures = [later, C_NOV]
    e.groups["CRUDEOIL"].underlying = later
    cat = e.catalogue_loader.catalogue
    cat.futures_by_symbol["CRUDEOIL"] = [early, C_NOV, later]
    e.universe_builder = UniverseBuilder(cat, e.universe_builder.policy)  # type: ignore[union-attr]
    e.universe_builder.policy = type(e.universe_builder.policy)(mcx_roll_days=0)
    plan = {r.root: r for r in e.contract_drift(TODAY)}
    assert "expires before" in plan["CRUDEOIL"].suspect


def test_a_partial_master_never_stops_entries_on_a_contract_that_is_not_due(settings):
    e, _ = _engine(settings)
    e.groups["CRUDEOIL"].futures = [C_NOV, C_DEC]
    e.groups["CRUDEOIL"].underlying = C_NOV
    e.underlyings["CRUDEOIL"] = C_NOV
    _drop_from_master(e, "C_NOV")
    assert e._contract_not_current("CRUDEOIL", C_NOV) is None, "34 days to run: the contract a boot kept"
    assert "not today's contract" in (e._contract_not_current("CRUDEOIL", C_OCT) or ""), "in its roll window: refused"
    _drop_from_master(e, "C_DEC")
    assert e._contract_not_current("CRUDEOIL", C_OCT) is None, "the only month listed: a boot would read it (never all of them)"
    _drop_from_master(e, "I_NOV", "I_DEC")
    assert "no later month" in (e._contract_not_current("NIFTYNXT50", I_OCT) or ""), "expired, nothing after it"


@pytest.mark.asyncio
async def test_a_working_order_on_the_old_month_blocks_the_swap(settings):
    e, _ = _engine(settings)
    intent = OrderIntent(strategy="FUDKII_RT_MCX", instrument=C_OCT, side=OrderSide.BUY, qty=1, purpose=Purpose.ENTRY,
                         signal_id="s", client_order_id="s|ENTRY|p")
    e._resting[intent.client_order_id] = Resting(intent=intent, kind="entry", limit=9572.0, placed_ts=NIGHT - 5,
                                                 deadline_s=30.0, signal_ts=NIGHT - 5)
    await e._roll_contracts(now=NIGHT)
    assert e.underlyings["CRUDEOIL"] is C_OCT and "working entry order" in e._contract_roll_pending["CRUDEOIL"]
    e._resting.clear()
    e._contract_roll_last_try = NIGHT
    await e._roll_contracts(now=NIGHT + CONTRACT_ROLL_RETRY_S)
    assert e.underlyings["CRUDEOIL"] is C_NOV


@pytest.mark.asyncio
async def test_a_history_failure_alarms_only_once_the_login_window_has_had_its_hour(settings):
    """The 5paisa login after midnight ran to 04:09 IST on 17 Oct: a roll waiting on the broker's history at
    01:00 is retried quietly, and alarms from 02:00 if it is still owed."""
    e, _ = _engine(settings, rest=Rest(fail={"C_NOV"}))
    await e._roll_contracts(now=NIGHT)
    assert "CRUDEOIL" in e._contract_roll_pending
    assert not any(k == "contracts.roll_pending" for k, _ in e.ledger.events), "01:05: retried, not alarmed"  # type: ignore[attr-defined]
    await e._roll_contracts(now=_at(TODAY, 2, 5))
    assert any(k == "contracts.roll_pending" for k, _ in e.ledger.events), "02:05: still owed — alarmed"  # type: ignore[attr-defined]
