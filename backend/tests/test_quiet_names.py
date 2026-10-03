"""Quiet names (operator, 2026-10-03): "keep cardamom but name it quiet for now. show alerts but dont
trade till there is liquidity". data/quiet.txt lists them; while a listed name's daily series is behind
(no recent trade at the broker) its triggers stand and alert, no book trades them; once it trades again
it is an ordinary name."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.market.session import IST, ist_today

from .test_rt_twin import RELIANCE, RELIANCE_OPT, _paper, _trigger


def _daily(last_days_ago: int, n: int = 40) -> list[UnifiedBar]:
    from datetime import datetime

    out, d = [], ist_today() - timedelta(days=last_days_ago)
    while len(out) < n:
        if d.weekday() < 5:
            ts = datetime(d.year, d.month, d.day, tzinfo=IST).timestamp()
            out.append(UnifiedBar(symbol="RELIANCE", scrip_code="2885", tf="1d", ts=ts, open=1500, high=1510, low=1490,
                                  close=1500, volume=1e6, source=BarSource.REST, complete=True))
        d -= timedelta(days=1)
    return list(reversed(out))


@pytest.mark.asyncio
async def test_a_quiet_names_trigger_alerts_but_no_book_trades_it_until_it_trades_again(settings):
    e = await _paper(settings)
    try:
        (settings.data_dir / "quiet.txt").write_text("# illiquid, kept for alerts\nreliance  # the operator's word\n")
        assert e.quiet_listed() == frozenset({"RELIANCE"})
        e.store.seed("RELIANCE", "1d", _daily(12))  # nothing for over a week: behind
        e.underlyings["RELIANCE"] = RELIANCE
        assert "no trade until it trades again" in (e.quiet_reason("RELIANCE") or "")
        sig = await _trigger(e, RELIANCE_OPT, RELIANCE)
        assert not e.positions, "alerts only"
        rows = await e.ledger.rows_between("signals", 0, time.time() + 5)
        assert [r["decision"] for r in rows if r["signal_id"] == sig.signal_id] == ["QUIET"]
        assert "RELIANCE" in e.daily_audit().quiet
        # it trades again: the previous session's candle arrives
        prev = e.calendar.previous_trading_day(ist_today())
        e.store.replace_series("RELIANCE", "1d", _daily((ist_today() - prev).days))
        assert e.quiet_reason("RELIANCE") is None and "RELIANCE" not in e.daily_audit().quiet
        await _trigger(e, RELIANCE_OPT, RELIANCE, shift=1)
        assert e.positions, "traded again: an ordinary name"
    finally:
        await e.stop()


def test_no_file_no_quiet_names(settings):
    from kotsin_nse.engine import Engine

    e = Engine(settings)
    assert e.quiet_listed() == frozenset() and e.quiet_reason("RELIANCE") is None
