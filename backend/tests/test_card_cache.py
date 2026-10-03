"""The live card's cache (Stage 4, review 2026-10-03): a card whose inputs have not moved is not
recomputed — and a book that ages past the freshness limit is an input, or the exit walks kept
pricing off a dead ladder while the card said "marked just now"."""

from __future__ import annotations

import time
from collections import deque
from types import SimpleNamespace

from kotsin_nse.alerts import entry as entry_model
from kotsin_nse.alerts.detectors import Alert
from kotsin_nse.alerts.engine import AlertEngine
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.instrument.select import Quote


def test_a_book_that_goes_stale_reprices_the_exit_walks_once(monkeypatch):
    t0 = 1_000_000.0
    clock = [t0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    code = "45678"
    book = BookSnapshot(scrip_code=code, bids=[(5.40, 50_000)], asks=[(5.50, 50_000)], ts=t0 - 30)
    eng = SimpleNamespace(
        quotes={code: Quote(ltp=5.45, bid=5.40, ask=5.50, ts=t0)}, book_for=lambda c: book if c == code else None,
        underlyings={}, ltps={}, tape=SimpleNamespace(follow=lambda *a, **k: None),
        archive=SimpleNamespace(option_quote=lambda *a, **k: None),
    )
    ae = AlertEngine(eng)
    alert = Alert(book="FUDKII", symbol="X", scrip_code="1", tf="30m", ts=int(t0) - 1800, direction="BULLISH", score=1.0,
                  reason="r", price=100.0, plan={"listed": {"scripCode": code, "strike": 105.0, "lotSize": 1000, "ltp": 5.45},
                                                "entry": 100.0},
                  card={"greeks": {}}, fired_at=t0 - 7200)
    ae.alerts["FUDKII"] = deque([alert])
    ae.refresh_live()
    assert alert.card["exitWalks"]["t1_1lot"]["source"] == "ladder"
    clock[0] = t0 + 15
    ae.refresh_live()
    assert alert.card["exitWalks"]["t1_1lot"]["source"] == "ladder", "nothing moved: served as it was"
    clock[0] = t0 + entry_model.MAX_QUOTE_AGE_S  # the book is now past the limit, nothing else moved
    ae.refresh_live()
    assert alert.card["exitWalks"]["t1_1lot"]["source"] == "touch (no live depth)"
