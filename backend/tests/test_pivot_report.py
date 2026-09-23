import asyncio
from datetime import date, timedelta

from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind, OptionType
from kotsin_nse.research.pivot_report import build, floor_outer, ladders_from_rows, render

EQ = Instrument(scrip_code="2885", symbol="RELIANCE", segment=Segment.NSE_EQ, kind=InstrumentKind.EQUITY, name="RELIANCE")
FUT = Instrument(scrip_code="68777", symbol="RELIANCE", segment=Segment.NSE_FO, kind=InstrumentKind.FUTURE,
                 name="RELIANCE 29 SEP 2026", expiry="2026-09-29", underlying="RELIANCE")


def _opt(code: str, strike: float, ot: OptionType) -> Instrument:
    return Instrument(scrip_code=code, symbol="RELIANCE", segment=Segment.NSE_FO, kind=InstrumentKind.OPTION,
                      expiry="2026-09-29", strike=strike, option_type=ot, underlying="RELIANCE",
                      name=f"RELIANCE 29 SEP 2026 {ot.value} {strike:g}")


def _row(day: date, o: float, h: float, lo: float, c: float, v: float = 1_000_000) -> dict:
    return {"dt": f"{day.isoformat()}T09:15:00", "o": o, "h": h, "l": lo, "c": c, "v": v}


def _sessions(first: date, last: date, base: float) -> list[dict]:
    rows, d, i = [], first, 0
    while d <= last:
        if d.weekday() < 5 and d != date(2026, 9, 14):  # 14 Sep 2026 is an NSE holiday
            rows.append(_row(d, base + i, base + i + 10, base + i - 10, base + i + 2))
            i += 1
        d += timedelta(days=1)
    return rows


class FakeRest:
    def __init__(self, by_code: dict[str, list[dict]]):
        self.by_code = by_code

    async def candles(self, inst, interval, start, end):
        assert interval == "1d"
        return [r for r in self.by_code.get(inst.scrip_code, []) if start <= r["dt"][:10] <= end]


class FakeCatalogue:
    def __init__(self, equity, future, options):
        self._eq, self._fut, self._opts = equity, future, options

    def equity(self, symbol):
        return self._eq

    def front_future(self, symbol, *, on=None):
        return self._fut

    def expiries(self, symbol, *, on=None):
        return sorted({o.expiry for o in self._opts})

    def chain(self, symbol, expiry, option_type):
        return sorted((o for o in self._opts if o.option_type is option_type), key=lambda o: o.strike)


def test_the_outer_rungs_are_reported_in_both_conventions():
    """Kite (what classic_pivots deliberately uses) and floor-trader (what the old stack's pivot
    API uses) differ only in R3/S3/R4/S4. On H 110 L 90 C 100: Kite R3 140, floor R3 130."""
    rows = [_row(date(2026, 9, 22), 100, 110, 90, 100)]
    rep = ladders_from_rows(EQ, rows, date(2026, 9, 23), tfs=("1d",), min_volume=0)
    (ld,) = rep.ladders
    assert ld.levels.r3 == 140 and ld.floor.r3 == 130
    assert ld.levels.s3 == 60 and ld.floor.s3 == 70
    fl = floor_outer(110, 90, 100)
    assert fl.r4 == 130 + 10 and fl.s4 == 70 - 10


def test_daily_comes_from_the_last_completed_session_and_skips_the_holiday():
    rows = _sessions(date(2026, 8, 1), date(2026, 9, 23), 1200)
    rep = ladders_from_rows(EQ, rows, date(2026, 9, 15), tfs=("1d", "1wk", "1mo"), min_volume=0)
    by_tf = {ld.tf: ld for ld in rep.ladders}
    assert by_tf["1d"].source == "2026-09-11", "14 Sep was a holiday, so Friday the 11th is the previous session"
    assert by_tf["1wk"].source == "2026-09-07..2026-09-11" and by_tf["1wk"].sessions == 5
    assert by_tf["1mo"].source == "2026-08-03..2026-08-31"


def test_a_week_with_a_holiday_reports_four_sessions_not_five():
    rows = _sessions(date(2026, 8, 1), date(2026, 9, 23), 1200)
    rep = ladders_from_rows(EQ, rows, date(2026, 9, 23), tfs=("1wk",), min_volume=0)
    (wk,) = rep.ladders
    assert wk.source == "2026-09-15..2026-09-18" and wk.sessions == 4


def test_build_covers_underlying_future_and_the_otm_strikes_with_the_thin_bar_guard():
    eq_rows = _sessions(date(2026, 8, 1), date(2026, 9, 23), 1200)
    prev_close = eq_rows[-2]["c"]  # 22 Sep, the session before the 23rd
    ce_liquid = _opt("106364", prev_close + 8, OptionType.CE)
    ce_thin = _opt("144392", prev_close + 18, OptionType.CE)
    pe_liquid = _opt("144391", prev_close - 8, OptionType.PE)
    by_code = {
        EQ.scrip_code: eq_rows,
        FUT.scrip_code: _sessions(date(2026, 8, 1), date(2026, 9, 23), 1205),
        ce_liquid.scrip_code: _sessions(date(2026, 8, 1), date(2026, 9, 23), 20),
        ce_thin.scrip_code: [_row(date(2026, 9, 22), 5, 9, 4, 6, v=5)],  # five contracts: refused
        pe_liquid.scrip_code: _sessions(date(2026, 8, 1), date(2026, 9, 23), 15),
    }
    cat = FakeCatalogue(EQ, FUT, [ce_liquid, ce_thin, pe_liquid])
    rep = asyncio.run(build("reliance", date(2026, 9, 23), catalogue=cat, rest=FakeRest(by_code)))

    assert rep.spot_close == prev_close
    assert {ld.tf for ld in rep.underlying.ladders} == {"1d", "1wk", "1mo"}
    assert rep.future is not None and [ld.tf for ld in rep.future.ladders] == ["1d"]
    by_code_rep = {o.instrument.scrip_code: o for o in rep.options}
    assert {ld.tf for ld in by_code_rep["106364"].ladders} == {"1d", "1wk"}, "options get daily and weekly"
    assert by_code_rep["144392"].ladders == [] and "thin" in by_code_rep["144392"].note
    text = render(rep)
    assert "UNDERLYING" in text and "FRONT FUTURE" in text and "(floor)" in text


def test_a_commodity_with_no_cash_leg_uses_the_front_future_as_underlying():
    crude = Instrument(scrip_code="482", symbol="CRUDEOIL", segment=Segment.MCX_FO, kind=InstrumentKind.FUTURE,
                       name="CRUDEOIL 19 OCT 2026", expiry="2026-10-19", underlying="CRUDEOIL")
    cat = FakeCatalogue(None, crude, [])
    rows = _sessions(date(2026, 8, 1), date(2026, 9, 23), 5000)
    rep = asyncio.run(build("CRUDEOIL", date(2026, 9, 23), catalogue=cat, rest=FakeRest({"482": rows})))
    assert rep.underlying.instrument is crude and rep.future is None, "not laddered twice"
    assert rep.options == []
