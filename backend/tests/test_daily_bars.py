"""The broker's daily history as 1d bars: finished sessions only."""

from __future__ import annotations

from kotsin_nse.bars.daily import daily_bars
from kotsin_nse.bars.unified import BarSource
from kotsin_nse.market.session import ist_day

from .conftest import ist_ts


def _rows():
    return [
        {"dt": "2026-10-26 00:00:00", "o": 1217.5, "h": 1228.0, "l": 1210.1, "c": 1220.0, "v": 7826610},
        # the running day, as 5paisa returned it at a 09:25 restart on 2026-10-27 (NIFTY's was stamped 09:14)
        {"dt": "2026-10-27 09:14:00", "o": 1224.8, "h": 1224.8, "l": 1216.0, "c": 1221.6, "v": 241330},
    ]


def test_a_running_sessions_candle_is_not_a_day(equity):
    """Seeded as a complete 1d bar at 09:14 it was 'the last bar before the open' — NIFTY's CPR on
    2026-10-27 was its own first 5m bar — and at 09:15 it shadowed the live day bar of every stock."""
    bars = daily_bars(equity, _rows(), now=ist_ts("2026-10-27", "09:28"))
    assert [ist_day(b.ts).isoformat() for b in bars] == ["2026-10-26"]
    b = bars[0]
    assert b.source is BarSource.REST and b.complete and b.high == 1228.0 and b.ts == ist_ts("2026-10-26", "00:00")


def test_a_finished_sessions_candle_is_kept_at_midnight_of_its_day(equity):
    bars = daily_bars(equity, _rows(), now=ist_ts("2026-10-27", "15:31"))
    assert [ist_day(b.ts).isoformat() for b in bars] == ["2026-10-26", "2026-10-27"]
    assert bars[1].ts == ist_ts("2026-10-27", "00:00") and bars[1].close == 1221.6
