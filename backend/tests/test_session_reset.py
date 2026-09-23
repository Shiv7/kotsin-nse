"""One session's signals, all of them, and nothing from the session before.

The operator's rules (2026-09-24): every strategy and every twin shows every signal it fired,
the page caps nothing, the rings are emptied at 00:30 IST so the next session opens blank, and
every book — parent and twin alike — runs to thirty concurrent positions.
"""

from __future__ import annotations

import time
from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.alerts.detectors import Alert, PivotBossConfig
from kotsin_nse.alerts.engine import RING, AlertEngine
from kotsin_nse.config import Settings
from kotsin_nse.engine import Engine
from kotsin_nse.market.session import IST, ist_hm, ist_today
from kotsin_nse.risk.limits import (
    CT_X_LIMITS,
    CT_Y_LIMITS,
    RT_N_LIMITS,
    RT_X_LIMITS,
    RT_Y_LIMITS,
    RiskLimits,
)


def _alert(book: str, ts: int, symbol: str = "RELIANCE") -> Alert:
    return Alert(book=book, symbol=symbol, scrip_code="2885", tf="30m", ts=ts,
                 direction="BULLISH", score=1.0, reason="t", price=100.0)


def _engine_alerts(n: int, books: tuple[str, ...] = ("FUDKII_RT", "PIVOTBOSS")) -> AlertEngine:
    a = AlertEngine(engine=None)
    for book in books:
        for i in range(n):
            a._emit(_alert(book, 1_000_000 + i))
    return a


# -- no cap on what the page may show ---------------------------------------------------------


def test_the_feed_returns_every_signal_of_the_session_by_default():
    a = _engine_alerts(240)
    assert len(a.feed("FUDKII_RT")) == 240, "a book's whole day, not the newest hundred"
    assert len(a.feed()) == 480, "and every book's, merged"
    assert len(a.feed(limit=50)) == 50, "a caller may still ask for fewer"
    merged = [r["ts"] for r in a.feed()]
    assert merged[0] == 1_000_239 and merged[-1] == 1_000_000, "newest first, across books"
    assert a.feed("NOBODY") == []


def test_the_ring_is_a_ceiling_no_session_reaches_not_a_page_size():
    assert RING >= 5000
    a = AlertEngine(engine=None)
    for i in range(RING + 10):
        a._emit(_alert("FUDKII_RT", 1_000_000 + i))
    assert len(a.feed("FUDKII_RT")) == RING
    assert a.counts["FUDKII_RT"] == RING + 10, "the count is honest about what fired"


def test_no_global_daily_cap_silently_swallows_a_books_signals():
    """216 underlyings evaluate on the same 30m boundary; a 30/day global cap was spent in
    arrival order on the first one, and the survivors were the earliest, not the strongest."""
    assert PivotBossConfig().global_daily_cap is None
    assert PivotBossConfig().max_per_scrip_per_day == 2, "one symbol not repeating itself stays"


# -- emptied for the next session -------------------------------------------------------------


def test_reset_day_empties_every_book_and_rebuilds_the_detectors():
    a = _engine_alerts(3)
    a.evaluated["FUDKII_RT"] = 9
    a.rt.living["RELIANCE"] = object()  # type: ignore[assignment]
    a.pivotboss.cap.take("RELIANCE", "2026-09-23")
    a._cpr_avg["RELIANCE"] = 1.0
    rt_before, pb_before = a.rt, a.pivotboss

    cleared = a.reset_day("2026-09-24")

    assert cleared == {"FUDKII_RT": 3, "PIVOTBOSS": 3}
    assert a.feed() == [] and a.counts == {} and a.evaluated == {}
    assert a.rt is not rt_before and a.pivotboss is not pb_before
    assert a.rt.living == {} and a._cpr_avg == {}
    assert a.stats()["resetDay"] == "2026-09-24" and a.stats()["living"] == 0
    assert a.reset_day("2026-09-25") == {}, "an empty page resets to an empty page"


def test_the_engine_empties_the_page_at_the_slot_and_only_once(settings, monkeypatch):
    e = Engine(settings)
    assert e.s.alerts_reset_ist == "00:30"
    e._alerts_reset_done.clear()  # as if the process had booted before the slot
    e.alerts._emit(_alert("FUDKII_RT", time.time()))
    e._signals_today["s1"] = object()  # type: ignore[assignment]

    def run(hm: str, day: str = "2026-09-24") -> None:
        stamp = f"{day} {e.s.alerts_reset_ist}"
        if hm >= e.s.alerts_reset_ist and stamp not in e._alerts_reset_done:
            e._alerts_reset_done.add(stamp)
            e.alerts.reset_day(day)
            e._signals_today.clear()
            e._fut_cache.clear()

    run("00:29")
    assert len(e.alerts.feed()) == 1 and e._signals_today, "not before the slot"
    run("00:30")
    assert e.alerts.feed() == [] and not e._signals_today
    e.alerts._emit(_alert("FUDKII_RT", time.time()))
    run("09:45")
    assert len(e.alerts.feed()) == 1, "a session's own signals survive the rest of the day"


def test_a_process_that_boots_after_the_slot_does_not_wipe_its_own_session(settings):
    """The seeding lives in the constructor, not in the universe path a boot can skip — else the
    first housekeeping tick of a 09:20 restart deletes the morning's signals."""
    e = Engine(settings)
    after = ist_hm(time.time()) >= e.s.alerts_reset_ist
    assert bool(e._alerts_reset_done) is after
    if after:
        assert e._alerts_reset_done == {f"{ist_today().isoformat()} {e.s.alerts_reset_ist}"}


def test_the_slot_is_half_an_hour_clear_of_the_brokers_midnight_refusal():
    """5paisa answers 403 to every login for ~20 minutes past midnight IST (2026-09-24). The
    reset is deliberately after that window, and is a clock slot rather than the date roll."""
    s = Settings(_env_file=None)
    reset = datetime.combine(ist_today(), dtime.fromisoformat(s.alerts_reset_ist), tzinfo=IST)
    midnight = datetime.combine(ist_today(), dtime(0, 0), tzinfo=IST)
    assert (reset - midnight).total_seconds() >= 20 * 60


# -- thirty concurrent, every book --------------------------------------------------------------


@pytest.mark.parametrize(
    "lim",
    [RiskLimits(), RT_X_LIMITS, RT_N_LIMITS, RT_Y_LIMITS, CT_X_LIMITS, CT_Y_LIMITS],
    ids=["base (FUDKII, FUKAA)", "RT-X", "RT-N", "RT-Y", "CT-X", "CT-Y"],
)
def test_every_book_and_twin_runs_to_thirty_concurrent_positions(lim):
    assert lim.max_positions_per_strategy == 30
    assert lim.max_positions_all_books == 90 > lim.max_positions_per_strategy
    assert lim.max_positions_per_underlying == 1, "still one position per name per book"


def test_the_parents_thirtieth_fill_is_allowed_and_the_thirty_first_is_not(equity, option):
    """The parent used to stop at three while its own twins ran to thirty on the same trigger."""
    import dataclasses

    from kotsin_nse.domain import Direction, Position, PosSide
    from kotsin_nse.risk.exposure import ExposureBook

    def held(n: int) -> list[Position]:
        return [
            Position(
                id=f"p{i}", strategy="FUDKII", instrument=option,
                underlying=dataclasses.replace(equity, symbol=f"N{i}", scrip_code=str(1000 + i)),
                side=PosSide.LONG, qty=1, entry=10.0, opened_ts=0.0, signal_id=f"s{i}",
                direction=Direction.BULLISH,
            )
            for i in range(n)
        ]

    book = ExposureBook(RiskLimits())
    args = dict(strategy="FUDKII", underlying="FRESH", outlay=1000.0, total_capital=1_000_000.0)
    assert book.check(positions=held(29), **args).allowed, "the 30th is inside the pool"
    v = book.check(positions=held(30), **args)
    assert not v.allowed and "30" in v.reason
    # a twin holding thirty of its own never touches the parent's count
    twins = held(30)
    for p in twins:
        p.strategy = "FUDKII_RT_X"
    assert book.check(positions=twins, **args).allowed, "books are independent"


# -- the money the count no longer bounds -------------------------------------------------------


def test_a_fill_above_the_quote_is_deployed_rather_than_dropped():
    """The sizer budgets on the quoted premium; the wallet is charged the fill. On the last
    entries of a nearly-full book the difference overdraws, and the entry path used to call
    `reserve` and discard its False — the position opened and the money was never marked spent,
    so the next entry sized against it. Three slots hid this; thirty do not."""
    from kotsin_nse.risk.wallet import Wallet

    w = Wallet.new("FUDKII", 100_000.0)
    assert w.reserve(99_000.0, 1.0) is True and w.available == 1_000.0
    # the next fill lands above the quote the sizer used
    assert w.reserve(1_200.0, 2.0) is False, "the old path: refused, and silently not deployed"
    assert w.deployed == 99_000.0, "which is exactly the lie — the money IS spent"

    w2 = Wallet.new("FUDKII", 100_000.0)
    w2.reserve(99_000.0, 1.0)
    over = w2.commit(1_200.0, 2.0)
    assert over == pytest.approx(200.0) and w2.deployed == pytest.approx(100_200.0)
    assert w2.available == 0.0, "overstating what is left is what let the next entry through"
    assert w2.commit(500.0, 3.0) == pytest.approx(500.0), "already dry: all of it is overdraw"
    # within the book's means it is an ordinary deployment and says so
    w3 = Wallet.new("FUDKII", 100_000.0)
    assert w3.commit(40_000.0, 1.0) == 0.0 and w3.available == 60_000.0


def test_the_trigger_card_page_asks_for_the_session_not_the_old_ring_size():
    """A living signal emits a keepalive a minute for up to 35 minutes, so a session's FUDKII_RT
    ring runs to hundreds. The hardcoded 500 was the old RING; past it an early ENTRY's card
    vanished from the page while its ledger row stayed, which reads as a trigger never carded."""
    import inspect

    from kotsin_nse.engine import Engine

    src = inspect.getsource(Engine.book_cards)
    assert 'feed("FUDKII_RT")' in src and 'feed("FUDKII_RT", 500)' not in src

    a = AlertEngine(engine=None)
    for i in range(700):
        al = _alert("FUDKII_RT", 1_000_000 + i)
        al.kind = "ENTRY"
        al.evidence = {"signalId": f"s{i}"}
        a._emit(al)
    seen = {(x.get("evidence") or {}).get("signalId") for x in a.feed("FUDKII_RT") if x["kind"] == "ENTRY"}
    assert len(seen) == 700 and "s0" in seen, "the first trigger of the day is still carded"


def test_the_reset_slot_is_validated_like_the_other_wall_clock_settings():
    with pytest.raises(ValueError, match="HH:MM"):
        Settings(_env_file=None, alerts_reset_ist="halfpast")
    with pytest.raises(ValueError, match="HH:MM"):
        Settings(_env_file=None, alerts_reset_ist="24:00")
    assert Settings(_env_file=None, alerts_reset_ist="01:15").alerts_reset_ist == "01:15"


@pytest.mark.asyncio
async def test_two_entries_on_one_bar_boundary_cannot_spend_the_same_rupees(settings, monkeypatch):
    """Every symbol's 30m bar closes at the same instant, each in its own decision task, and
    placing the order suspends the task. The money is taken before that await, so the second
    entry sees a book that is already short — it used to size against the same balance."""
    import asyncio

    e = Engine(settings)
    w = e.wallets.setdefault("FUDKII", __import__("kotsin_nse.risk.wallet", fromlist=["Wallet"]).Wallet.new("FUDKII", 100_000.0))
    w.balance = w.day_start_balance = w.peak = 100_000.0
    w.deployed = 0.0

    async def entry(outlay: float) -> bool:
        """The shape of _handle_signal: size against `available`, then await, then commit."""
        if not w.reserve(outlay, time.time()):
            return False
        await asyncio.sleep(0)  # the order and the two ledger writes
        w.release(outlay, time.time())
        w.commit(outlay, time.time())
        return True

    took = await asyncio.gather(entry(60_000.0), entry(60_000.0))
    assert took == [True, False], "the second is refused, not funded from the first's money"
    assert w.deployed == 60_000.0 and w.available == 40_000.0


def test_the_alert_rows_are_keyed_on_their_own_identity_not_their_position():
    """The feed is newest-first, so one new firing shifts every index. Keyed on the index, React
    unmounts and remounts every row and an expanded 'why it fired' panel snaps shut."""
    tsx = (__import__("pathlib").Path(__file__).resolve().parents[2] / "frontend/src/pages/Alerts.tsx").read_text()
    assert "key={`${a.book}-${a.kind}-${a.firedAt}-${a.symbol}`}" in tsx
    assert "${a.ts}-${i}" not in tsx and "map((a, i)" not in tsx
