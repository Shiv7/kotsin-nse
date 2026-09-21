"""Gateway, paper fills, reconciliation. The parts that can lose money."""

from __future__ import annotations

import time

import pytest

from kotsin_nse.config import Settings
from kotsin_nse.domain import Instrument, InstrumentKind, OrderIntent, OrderSide, Purpose
from kotsin_nse.exec.gateway import Decision, Gateway, LiveCaps, LiveContext, Mode
from kotsin_nse.exec.paper import BookSnapshot, NoBook, PaperMatcher, walk_book
from kotsin_nse.exec.reconcile import MismatchKind, Reconciler
from kotsin_nse.risk.costs import CostModel


def _intent(option, *, purpose=Purpose.ENTRY, qty=250, side=OrderSide.BUY, coid="c1") -> OrderIntent:
    return OrderIntent(
        strategy="FUDKII",
        instrument=option,
        side=side,
        qty=qty,
        purpose=purpose,
        signal_id="FUDKII-RELIANCE-1-B",
        client_order_id=coid,
        ref_price=50.0,
    )


def _book(scrip: str = "45678", *, age_s: float = 0.0) -> BookSnapshot:
    return BookSnapshot(
        scrip_code=scrip,
        bids=[(49.5, 200), (49.0, 500), (48.0, 1000)],
        asks=[(50.0, 100), (50.5, 200), (51.0, 1000)],
        ts=time.time() - age_s,
    )


def _gateway(mode: Mode, *, halted=(False, ""), book=None, caps=None) -> Gateway:
    return Gateway(
        matcher=PaperMatcher(CostModel(Settings(_env_file=None))),
        mode=lambda: mode,
        halted=lambda: halted,
        book_for=lambda _c: book,
        ltp_for=lambda _c: 50.0,
        caps=caps or LiveCaps(),
    )


# -- book walking ------------------------------------------------------------------------------------


def test_walk_takes_lots_first_then_price():
    w = walk_book([(50.0, 100), (50.5, 200)], 250, touch=50.0, ceiling_pct=10.0, buy=True)
    assert w.filled == 250
    assert w.levels == 2
    assert 50.0 < w.avg_price < 50.5


def test_walk_stops_at_the_ten_percent_ceiling():
    """A synthetic fill 15% up the ladder is not a fill, it is an excuse."""
    w = walk_book([(50.0, 10), (60.0, 1000)], 500, touch=50.0, ceiling_pct=10.0, buy=True)
    assert w.filled == 10
    assert w.capped is True


def test_stale_book_refuses_to_fill(option):
    m = PaperMatcher(CostModel(Settings(_env_file=None)), max_book_age_ms=1000)
    with pytest.raises(NoBook):
        m.fill(_intent(option), _book(age_s=5))


def test_paper_fill_records_slippage_and_charges(option):
    m = PaperMatcher(CostModel(Settings(_env_file=None)))
    fill = m.fill(_intent(option), _book())
    assert fill.qty == 250
    assert fill.slippage_bps is not None and fill.slippage_bps > 0
    assert fill.charges > 0
    assert fill.levels >= 2


def test_stop_exits_are_not_booked_at_exactly_the_stop(option):
    """CAN2 booked every stop-out at exactly the stop price, so its entire live ledger was
    optimistic by the gap."""
    m = PaperMatcher(CostModel(Settings(_env_file=None)))
    fill = m.fill(_intent(option, side=OrderSide.SELL, qty=250), _book())
    assert fill.price < 49.5  # walked down the bid ladder, not filled at the touch


def test_fill_without_a_book_falls_back_to_ltp_and_says_so(option):
    m = PaperMatcher(CostModel(Settings(_env_file=None)))
    fill = m.fill(_intent(option), None, fallback_ltp=50.0)
    assert fill.levels == 0
    assert fill.book_age_ms is None
    assert fill.price > 50.0  # slippage applied on a buy


# -- gateway -------------------------------------------------------------------------------------------


def test_shadow_records_but_places_nothing(option):
    g = _gateway(Mode.SHADOW)
    r = g.submit(_intent(option))
    assert r.decision is Decision.SHADOW_OK
    assert r.fill is None and r.order.status == "SHADOW"


def test_paper_fills_against_the_book(option):
    g = _gateway(Mode.PAPER, book=_book())
    r = g.submit(_intent(option))
    assert r.decision is Decision.PAPER_FILLED
    assert r.filled and r.order.avg_price is not None


def test_halt_blocks_entries_but_never_exits(option):
    g = _gateway(Mode.PAPER, halted=(True, "manual"), book=_book())
    entry = g.submit(_intent(option, coid="e1"))
    assert entry.decision is Decision.REJECTED_HALT
    exit_ = g.submit(_intent(option, purpose=Purpose.EXIT, side=OrderSide.SELL, coid="x1"))
    assert exit_.decision is Decision.PAPER_FILLED, "refusing to let a position out is worse"


def test_idempotency_blocks_a_repeated_client_order_id(option):
    g = _gateway(Mode.PAPER, book=_book())
    assert g.submit(_intent(option, coid="same")).decision is Decision.PAPER_FILLED
    assert g.submit(_intent(option, coid="same")).decision is Decision.DUP_BLOCKED


def test_remembered_ids_survive_a_restart(option):
    g = _gateway(Mode.PAPER, book=_book())
    g.remember("already-sent")
    assert g.submit(_intent(option, coid="already-sent")).decision is Decision.DUP_BLOCKED


def test_breaker_trips_after_consecutive_rejects(option):
    g = _gateway(Mode.PAPER, book=None, caps=LiveCaps(breaker_consecutive_rejects=2))
    g._ltp_for = lambda _c: None  # no book and no LTP → rejects
    for i in range(2):
        g.submit(_intent(option, coid=f"r{i}"))
    assert g.breaker_tripped is True
    g.reset_breaker()
    assert g.breaker_tripped is False


def test_live_caps_reject_before_the_broker_sees_anything(option):
    caps = LiveCaps(segments=("NSE_FO",), max_notional_inr=1000.0, max_positions=1,
                    max_orders_per_day=1, daily_loss_inr=500.0, entry_cutoff_ist="15:10")
    g = _gateway(Mode.LIVE_CAPPED, caps=caps)
    ctx = LiveContext(balance=100_000, open_positions=0, day_pnl_inr=0.0,
                      now_hm_ist="10:00", segment="NSE_FO")
    # notional 250 × 50 = 12,500 > 1,000
    assert "notional" in (g.check_live_caps(_intent(option), ctx) or "")
    small = _intent(option, qty=1)
    assert g.check_live_caps(small, ctx) is None
    # wrong segment
    assert "not in live whitelist" in (
        g.check_live_caps(small, LiveContext(100_000, 0, 0.0, "10:00", "NSE_EQ")) or ""
    )
    # past the cutoff
    assert "cutoff" in (
        g.check_live_caps(small, LiveContext(100_000, 0, 0.0, "15:20", "NSE_FO")) or ""
    )
    # daily loss
    assert "day P&L" in (
        g.check_live_caps(small, LiveContext(100_000, 0, -600.0, "10:00", "NSE_FO")) or ""
    )
    # too many positions
    assert "positions open" in (
        g.check_live_caps(small, LiveContext(100_000, 1, 0.0, "10:00", "NSE_FO")) or ""
    )


def test_live_caps_never_apply_to_an_exit(option):
    g = _gateway(Mode.LIVE_CAPPED, caps=LiveCaps(segments=("NOPE",)))
    ctx = LiveContext(0.0, 99, -99_999.0, "23:59", "NSE_FO")
    assert g.check_live_caps(_intent(option, purpose=Purpose.EXIT), ctx) is None


def test_index_instruments_are_excluded_from_live():
    idx = Instrument(scrip_code="99992000", symbol="NIFTY", segment=__import__(
        "kotsin_nse.config", fromlist=["Segment"]).Segment.NSE_FO, kind=InstrumentKind.OPTION)
    g = _gateway(Mode.LIVE_CAPPED, caps=LiveCaps(segments=("NSE_FO",)))
    ctx = LiveContext(100_000, 0, 0.0, "10:00", "NSE_FO")
    assert "index" in (g.check_live_caps(_intent(idx, qty=1), ctx) or "")


def test_live_mode_without_an_executor_is_refused(option):
    g = _gateway(Mode.LIVE_CAPPED)
    r = g.submit(_intent(option))
    assert r.decision is Decision.REJECTED_BROKER
    assert "submit_live" in r.order.note


# -- reconciliation --------------------------------------------------------------------------------------


class FakeRest:
    def __init__(self, rows):
        self._rows = rows

    async def net_positions(self):
        return self._rows


async def test_reconcile_detects_an_orphan_and_freezes():
    r = Reconciler(FakeRest([{"scrip_code": "99", "exch": "N", "exch_type": "D", "net_qty": 50,
                              "buy_avg": 10.0, "sell_avg": 0.0, "mtm": 0.0, "symbol": "X"}]))
    report = await r.run([])
    assert not report.clean
    assert report.mismatches[0].kind is MismatchKind.ORPHAN
    assert r.frozen is True
    r.acknowledge()
    assert r.frozen is False


async def test_reconcile_clean_thaws():
    r = Reconciler(FakeRest([]))
    report = await r.run([])
    assert report.clean and not r.frozen


# -- the broker's overloaded status=1 ------------------------------------------------------------


class _FakeAuth:
    def __init__(self) -> None:
        self.invalidations = 0

    async def token(self):
        from kotsin_nse.venue.fivepaisa.auth import Session

        return Session(access_token="t", client_code="1", expires_at=time.time() + 3600)

    def invalidate(self, **_kw) -> bool:
        self.invalidations += 1
        return True


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeHttp:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.posts = 0

    async def post(self, *_a, **_kw) -> _FakeResponse:
        self.posts += 1
        return _FakeResponse(self.payload)


async def test_a_flat_book_is_an_empty_reconcile_not_a_failed_one():
    """Observed live 2026-09-21: a flat account answers with head.status=1, 'No record found.'

    Treating that as an error froze entries on an account with nothing to reconcile, and — because
    status 1 also means 'session dead' — re-logged in every 60s in LIVE to ask the same question.
    """
    from kotsin_nse.venue.fivepaisa.rest import FivePaisaREST

    http = _FakeHttp(
        {"head": {"status": "1", "statusDescription": "No record found."}, "body": {}}
    )
    auth = _FakeAuth()
    rest = FivePaisaREST(Settings(), http, auth)

    assert await rest.net_positions() == []
    assert auth.invalidations == 0, "a flat book must not force a re-login"
    assert http.posts == 1, "and must not be retried"

    report = await Reconciler(rest).run([])
    assert report.error == ""
    assert report.clean is True
    assert report.mismatches == []
