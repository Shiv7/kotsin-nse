"""The feed checks' detail says what IS (operator, 28 Sep: a connected feed showed the 08:53 DNS error
and a fresh one "no message for over 2 minutes" beside two passing checks)."""

from __future__ import annotations

import time

from kotsin_nse.engine import Engine


def _checks(e: Engine) -> dict[str, dict]:
    return {c["name"]: c for c in e.health_snapshot()["checks"]}


def test_a_connected_fresh_feed_says_so_not_its_last_error(settings, monkeypatch):
    e = Engine(settings)
    monkeypatch.setattr(e, "market_open_now", lambda: True)
    fh = e.feed.health
    fh.connected, fh.reconnects, fh.last_error = True, 46, "[Errno 8] nodename nor servname provided, or not known"
    fh.last_message_ts = time.time() - 0.4
    c = _checks(e)
    assert c["feed_connected"]["ok"] and c["feed_connected"]["detail"] == "connected · 46 reconnects since boot"
    assert c["feed_fresh"]["ok"] and c["feed_fresh"]["detail"].startswith("last message 0.")


def test_a_failing_check_still_says_why(settings, monkeypatch):
    e = Engine(settings)
    monkeypatch.setattr(e, "market_open_now", lambda: True)
    fh = e.feed.health
    fh.connected, fh.last_error = False, "no close frame received or sent"
    fh.last_message_ts = time.time() - 300
    c = _checks(e)
    assert not c["feed_connected"]["ok"] and c["feed_connected"]["detail"] == "disconnected — no close frame received or sent"
    assert not c["feed_fresh"]["ok"] and c["feed_fresh"]["detail"] == "no message for 300 s (over 2 minutes)"


def test_outside_the_session_both_read_market_closed(settings, monkeypatch):
    e = Engine(settings)
    monkeypatch.setattr(e, "market_open_now", lambda: False)
    c = _checks(e)
    assert c["feed_connected"]["detail"] == c["feed_fresh"]["detail"] == "market closed"
