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
    levels, session, close, vol = got
    assert vol is None, "these rows carry no volume field — unverified, not thin"
    assert session == "2026-09-21", "the 22nd is today and must not be used"
    assert close == 12.0
    # classic pivot of the 21st: (15 + 9 + 12) / 3 = 12
    assert round(levels.pivot, 2) == 12.0


def test_no_completed_session_yields_nothing():
    rows = [{"dt": "2026-09-22T09:15:00", "o": 1, "h": 2, "l": 1, "c": 2}]
    assert levels_from_candles(rows, date(2026, 9, 22)) is None
    assert levels_from_candles([], date(2026, 9, 22)) is None


def test_a_session_too_thin_to_have_a_range_gets_no_ladder():
    """Measured on MCX crude options: the 22-Sep bars carried 46,418 and 18,553 lots and their wide
    ranges were real, but 17- and 18-Sep on the same contracts carried 11 and 5. A high and a low
    built from five contracts are one counterparty's opinion, and a pivot from them looks exactly
    like a level thousands of lots agreed on."""
    thin = [{"dt": "2026-09-18T09:15:00", "o": 902.8, "h": 1018.5, "l": 876.5, "c": 954.0, "v": 5}]
    assert levels_from_candles(thin, date(2026, 9, 23)) is None

    real = [{"dt": "2026-09-22T09:15:00", "o": 638.7, "h": 694.4, "l": 441.0, "c": 450.7, "v": 46418}]
    got = levels_from_candles(real, date(2026, 9, 23))
    assert got is not None
    levels, _session, _close, vol = got
    assert vol == 46418
    # (694.40 + 441.00 + 450.70) / 3 — a 56% range is real when 46,418 lots made it
    assert round(levels.pivot, 2) == 528.70


def test_a_feed_that_reports_no_volume_is_unverified_not_thin():
    """Absence of evidence, not evidence of thinness. Dropping every ladder from a source that
    does not report volume would be a worse failure than the one the floor guards against."""
    no_field = [{"dt": "2026-09-22T09:15:00", "o": 1, "h": 2, "l": 1, "c": 2}]
    got = levels_from_candles(no_field, date(2026, 9, 23))
    assert got is not None and got[3] is None

    # An explicit zero is a reported fact and does get refused.
    assert levels_from_candles(
        [{"dt": "2026-09-22T09:15:00", "o": 1, "h": 2, "l": 1, "c": 2, "v": 0}], date(2026, 9, 23)
    ) is None


def test_the_volume_floor_is_configurable_per_call():
    bar = [{"dt": "2026-09-22T09:15:00", "o": 1, "h": 2, "l": 1, "c": 2, "v": 50}]
    assert levels_from_candles(bar, date(2026, 9, 23)) is None
    assert levels_from_candles(bar, date(2026, 9, 23), min_volume=10) is not None


def test_a_session_that_printed_at_one_price_gets_no_ladder():
    """``classic_pivots`` returns S3 == pivot == R3 for a zero-range bar. Every level lands on every
    other, so "price is at S1" and "price is at R3" are true at the same instant and clustering
    merges nine coincident levels into a fortress wall made of nothing. Measured on MCX silver
    options, 2026-09-23 (SILVERM 238000 PE, one contract traded)."""
    flat = [{"dt": "2026-09-22T09:15:00", "o": 238.0, "h": 238.0, "l": 238.0, "c": 238.0, "v": 5000}]
    assert levels_from_candles(flat, date(2026, 9, 23)) is None, "high volume does not rescue it"
