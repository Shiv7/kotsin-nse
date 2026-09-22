"""Daily pivots for the legs actually traded — the future and the OTM strikes."""

from __future__ import annotations

from datetime import date

from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind, OptionType
from kotsin_nse.instrument.legs import levels_from_candles, otm_legs


def _opt(strike: float, ot: OptionType) -> Instrument:
    return Instrument(
        scrip_code=f"{ot.value}{strike:g}", symbol="X", segment=Segment.NSE_FO,
        kind=InstrumentKind.OPTION, name=f"X {strike:g} {ot.value}", lot_size=100,
        tick_size=0.05, multiplier=1, expiry="2026-09-29", strike=strike,
        option_type=ot, underlying="X",
    )


def test_only_genuinely_out_of_the_money_strikes_are_taken():
    """A call below spot and a put above it are in the money — they behave like the underlying
    with extra cost, which is not what this book trades."""
    chain = [_opt(s, OptionType.CE) for s in (90, 100, 110, 120, 130, 140)]
    chain += [_opt(s, OptionType.PE) for s in (60, 70, 80, 90, 100, 110)]
    legs = otm_legs(chain=chain, spot=100.0, per_side=2)

    ce = sorted(o.strike for o in legs if o.option_type is OptionType.CE)
    pe = sorted(o.strike for o in legs if o.option_type is OptionType.PE)
    assert ce == [110.0, 120.0], "calls ABOVE spot, nearest first"
    assert pe == [80.0, 90.0], "puts BELOW spot, nearest first"
    assert 100.0 not in ce and 100.0 not in pe, "at the money is not out of it"
    assert len(legs) == 4


def test_eight_legs_a_name_by_default_covers_the_lot_cap_walk():
    from kotsin_nse.instrument.legs import STRIKES_PER_SIDE

    assert STRIKES_PER_SIDE == 4, "four a side — eight legs, enough to walk out under the lot cap"
    chain = [_opt(s, OptionType.CE) for s in (110, 120, 130, 140, 150)]
    chain += [_opt(s, OptionType.PE) for s in (50, 60, 70, 80, 90)]
    assert len(otm_legs(chain=chain, spot=100.0)) == 8


def test_an_empty_or_unpriced_chain_yields_nothing_rather_than_guessing():
    assert otm_legs(chain=[], spot=100.0) == []
    assert otm_legs(chain=[_opt(110, OptionType.CE)], spot=0.0) == []


def test_levels_come_from_the_last_COMPLETED_session_not_today():
    """Walk-forward safe by construction: today's own bar can never set today's levels."""
    rows = [
        {"dt": "2026-09-18T09:15:00", "o": 10, "h": 12, "l": 8, "c": 11},
        {"dt": "2026-09-21T09:15:00", "o": 11, "h": 15, "l": 9, "c": 12},
        {"dt": "2026-09-22T09:15:00", "o": 12, "h": 20, "l": 5, "c": 18},  # today
    ]
    got = levels_from_candles(rows, date(2026, 9, 22))
    assert got is not None
    levels, session, close = got
    assert session == "2026-09-21", "the 22nd is today and must not be used"
    assert close == 12.0
    # classic pivot of the 21st: (15 + 9 + 12) / 3 = 12
    assert round(levels.pivot, 2) == 12.0


def test_no_completed_session_yields_nothing():
    rows = [{"dt": "2026-09-22T09:15:00", "o": 1, "h": 2, "l": 1, "c": 2}]
    assert levels_from_candles(rows, date(2026, 9, 22)) is None
    assert levels_from_candles([], date(2026, 9, 22)) is None
