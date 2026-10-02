"""A restart keeps the day's 1m bars (operator, 2026-09-29: the card's "underlying since trigger" read
"appears once 1m bars accrue" after the evening restarts): they come back from the engine's own
archive, with no broker call."""

from __future__ import annotations

from datetime import date

import pandas as pd

from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.engine import Engine
from kotsin_nse.market.session import IST, from_ist

DAY = date(2026, 9, 29)


def test_the_days_1m_bars_come_back_from_the_archive(settings):
    e = Engine(settings)
    inst = Instrument("15380", "MANKIND", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="MANKIND")
    t0 = int(from_ist(pd.Timestamp("2026-09-29 09:15").to_pydatetime().replace(tzinfo=IST)))
    rows = [{"symbol": "MANKIND", "scrip_code": "15380", "ts": t0 + 60 * i, "o": 2440.0 + i, "h": 2442.0 + i, "l": 2439.0 + i,
             "c": 2441.0 + i, "v": 1000.0 + i, "source": "live", "oi": 0.0} for i in range(5)]
    rows.append({**rows[-1], "c": 2450.0})  # the same minute written twice: the last write stands
    rows.append({**rows[0], "symbol": "NOTHERE", "scrip_code": "1"})  # not in the universe: left out
    (settings.data_dir / "archive" / "bars").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(settings.data_dir / "archive" / "bars" / "2026-09-29.parquet", index=False)
    assert e._seed_1m_from_archive([inst], DAY) == 5
    bars = e.store.bars("MANKIND", "1m")
    assert [b.ts for b in bars] == [t0 + 60 * i for i in range(5)] and bars[-1].close == 2450.0
    assert e.store.bars("NOTHERE", "1m") == []
    assert e._seed_1m_from_archive([inst], date(2026, 9, 30)) == 0, "no archive for the day: nothing, and no error"
