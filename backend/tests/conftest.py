from __future__ import annotations

from datetime import datetime

import pytest

from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Instrument, InstrumentKind, OptionType
from kotsin_nse.market.session import IST, from_ist


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        db_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        engine_enabled=False,
    )


@pytest.fixture
def equity() -> Instrument:
    return Instrument(
        scrip_code="2885",
        symbol="RELIANCE",
        segment=Segment.NSE_EQ,
        kind=InstrumentKind.EQUITY,
        name="RELIANCE",
        lot_size=1,
        tick_size=0.05,
        multiplier=1,
        underlying="RELIANCE",
    )


@pytest.fixture
def option() -> Instrument:
    return Instrument(
        scrip_code="45678",
        symbol="RELIANCE",
        segment=Segment.NSE_FO,
        kind=InstrumentKind.OPTION,
        name="RELIANCE 25 SEP 2026 CE 1500.00",
        lot_size=250,
        tick_size=0.05,
        multiplier=1,
        expiry="2026-09-25",
        strike=1500.0,
        option_type=OptionType.CE,
        underlying="RELIANCE",
    )


@pytest.fixture
def mcx_future() -> Instrument:
    """ALUMINI: quoted per kg on a 1,000 kg contract. The multiplier is the whole point."""
    return Instrument(
        scrip_code="255555",
        symbol="ALUMINI",
        segment=Segment.MCX_FO,
        kind=InstrumentKind.FUTURE,
        name="ALUMINI 30 SEP 2026",
        lot_size=1,
        tick_size=0.05,
        multiplier=1000,
        expiry="2026-09-30",
        option_type=OptionType.FUT,
        underlying="ALUMINI",
    )


def ist_ts(day: str, hm: str) -> float:
    return from_ist(datetime.fromisoformat(f"{day} {hm}:00").replace(tzinfo=IST))


def bar(
    ts: float,
    o: float,
    h: float,
    low: float,
    c: float,
    v: float = 1000.0,
    *,
    symbol: str = "RELIANCE",
    tf: str = "30m",
    oi: int | None = None,
    oi_change_pct: float | None = None,
) -> UnifiedBar:
    return UnifiedBar(
        symbol=symbol,
        scrip_code="2885",
        tf=tf,
        ts=int(ts),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
        source=BarSource.LIVE,
        complete=True,
        oi=oi,
        oi_change_pct=oi_change_pct,
    )


#: Every synthetic series ends here unless told otherwise: 11:00 IST on Monday 2026-01-05, i.e.
#: mid-session on a weekday. Anchoring on ``time.time()`` made the suite pass in the morning and
#: fail at night, because ``session_phase`` correctly returned EOD after 14:45 IST and the last
#: bar's wall-clock time leaked into the assertion.
ANCHOR_DAY = "2026-01-05"
ANCHOR_HM = "11:00"


def series(
    closes: list[float], *, start: float | None = None, step: int = 1800, vol: float = 1000.0
) -> list[UnifiedBar]:
    """A bar series from closes, with a small symmetric range around each close.

    Deterministic by construction — see :data:`ANCHOR_DAY`.
    """
    t0 = start if start is not None else ist_ts(ANCHOR_DAY, ANCHOR_HM) - (len(closes) - 1) * step
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        hi = max(prev, c) * 1.002
        lo = min(prev, c) * 0.998
        out.append(bar(t0 + i * step, prev, hi, lo, c, vol))
        prev = c
    return out
