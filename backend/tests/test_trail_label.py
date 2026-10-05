"""An option stop at or above the premium paid is the trail locking a gain, not the stock's stop through δ (operator,
2026-10-06: "wrt bajfinance 1010ce the dashboard says equity projected stop at 19.21 and T1 as 26.45. entry was 18.4").
BAJFINANCE 5 Oct, engine B's FUDKII: filled 18.40, the stop through δ 11.69; the option peaked at 19.75 (+7.3 %), the
trail armed at +3 % and kept 60 % of the gain — 18.40 + 0.6 × 1.35 = 19.21 — and sold at 19.05 (+₹1,950 gross)."""

from __future__ import annotations

from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind, OptionType
from kotsin_nse.engine import Engine
from kotsin_nse.strategy.keys import StrategyKey

CE = Instrument("76201", "BAJFINANCE", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=750, strike=1010.0,
                option_type=OptionType.CE, underlying="BAJFINANCE")


def _stop_row(e: Engine, option_sl: float, **kw) -> str:
    plan = e._exit_plan(StrategyKey.FUDKII, CE, 3000, (26.45,), option_sl, 965.35, Segment.NSE_EQ, **kw)
    return next(r["at"] for r in plan["rows"] if r["kind"] == "stop")


def test_a_stop_above_the_premium_paid_reads_as_the_trails_lock(settings):
    e = Engine(settings)
    assert _stop_row(e, 19.21, entry=18.40, initial_sl=11.69) == "stop 19.21 — above the 18.40 paid: the trail locks +4.4%"
    assert _stop_row(e, 15.00, entry=18.40, initial_sl=11.69).startswith("stop now 15.00 — the rising line"), "raised, still under the entry"
    assert _stop_row(e, 11.69, entry=18.40, initial_sl=11.69).startswith("option stop 11.69 (equity stop through δ"), "as planned"
    assert _stop_row(e, 11.69).startswith("option stop 11.69 (equity stop through δ"), "a preview has no fill to compare with"
    assert _stop_row(e, 18.40, entry=18.40, initial_sl=11.69).startswith("stop now 18.40 — the rising line"), "breakeven is not a lock"
