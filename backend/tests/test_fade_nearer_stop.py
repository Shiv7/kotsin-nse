"""CT-Y's fade stop: the nearer of 1 ATR30 and just past the next classic level (operator, 2026-10-06: "why did bajfinance
not have an SL based on pivot clusters or ATR based on what is nearer?").

The level is the first raw classic level (1d / 1wk / 1mo pivot, TC, BC, R1-R4, S1-S4) at least 0.25 ATR30 past the close
the trigger's way — one right at the close is the level the trigger just reached — and the stop sits 0.1 ATR30 beyond it
when that is nearer than 1 ATR30. Route study on CT-Y's published gap fades: 1-28 Sep +₹39,515 -> +₹50,219 on 18,
29 Sep - 5 Oct −₹15,551 -> −₹11,742 on 8. BAJFINANCE 5 Oct keeps its 1-ATR stop (993.45): its only level above the
985.95 close was the monthly pivot 995.07, 1.21 ATR30 away."""

from __future__ import annotations

import time

import pytest

from kotsin_nse.bars.pivots import PivotPoint, Zone
from kotsin_nse.domain import Direction
from kotsin_nse.engine import Engine
from kotsin_nse.risk.limits import CT_M_LIMITS, CT_Y_LIMITS
from kotsin_nse.strategy.keys import StrategyKey
from tests.test_gate_b_and_gap_fade import UND, _seed, _trigger

CTX = {"openBar": True, "gapDatr": 0.45, "atr30": 2.0}  # the trigger closes at 187.25; 1 ATR30 = 2.0


def _levels(e: Engine, *pts: tuple[float, str]) -> None:
    e._pivot_points = lambda symbol: [PivotPoint(px, lab, lab.split(".")[0], 4.0) for px, lab in pts]  # type: ignore[method-assign]


def test_ct_y_takes_the_nearer_stop_and_ct_m_keeps_its_one_atr():
    assert CT_Y_LIMITS.fade_stop_nearer_level is True and CT_M_LIMITS.fade_stop_nearer_level is False


def test_the_stop_sits_just_past_the_first_level_that_is_not_at_the_close(settings):
    e = Engine(settings)
    _seed(e, zones=[Zone(184.0, 6.0, ["1d.PIVOT"])])
    # 1d.R1 0.03 ATR over the close: the level the trigger just reached, skipped; 1wk.R1 0.43 ATR over it: the stop
    _levels(e, (187.30, "1d.R1"), (188.10, "1wk.R1"), (188.10, "1mo.BC"), (190.0, "1d.R2"), (185.0, "1d.PIVOT"))
    plan = e.gap_fade_plan(_trigger(Direction.BULLISH), CTX)
    assert plan["side"] == "PE" and plan["stop"] == pytest.approx(188.30), "188.10 + 0.1 ATR30, nearer than 189.25"
    assert plan["stopNote"] == "0.1 ATR30 past 1wk.R1,1mo.BC 188.10 — nearer than 1 ATR30"
    assert plan["rr"] == pytest.approx(1.62), "the RR stays T1 against 1 ATR30: a label, as before"
    # a bearish trigger: the levels below the close
    _levels(e, (186.60, "1d.S1"), (183.0, "1d.S2"))
    plan = e.gap_fade_plan(_trigger(Direction.BEARISH), CTX)
    assert plan["side"] == "CE" and plan["stop"] == pytest.approx(186.40) and "1d.S1 186.60" in plan["stopNote"]


def test_a_level_past_one_atr_leaves_the_one_atr_stop(settings):
    e = Engine(settings)
    _seed(e, zones=[])
    _levels(e, (189.70, "1mo.PIVOT"))  # BAJFINANCE's shape: the only level above is 1.2 ATR30 away
    plan = e.gap_fade_plan(_trigger(Direction.BULLISH), CTX)
    assert plan["stop"] == pytest.approx(189.25) and plan["stopNote"] == "1 ATR30 past the close"
    _levels(e)
    assert e.gap_fade_plan(_trigger(Direction.BULLISH), CTX)["stop"] == pytest.approx(189.25), "no levels: 1 ATR30"
    assert e.fade_plan(_trigger(Direction.BULLISH), CTX)["stop"] == pytest.approx(189.25), "the plan CT-M uses keeps 1 ATR30"


@pytest.mark.asyncio
async def test_the_gap_fade_names_its_stop(settings):
    e = Engine(settings)
    await e.start()
    try:
        _seed(e, zones=[Zone(184.0, 6.0, ["1d.PIVOT"])])
        _levels(e, (188.10, "1wk.R1"))
        trig = _trigger(Direction.BULLISH)
        e._breadth_at[trig.signal_id] = {**CTX, "share": 0.6, "names": 220}
        handled = []

        async def capture(sig, bar, *, adopt=True):
            handled.append(sig)

        e._handle_signal = capture  # type: ignore[method-assign]
        await e._gap_fade(trig, None, UND)
        fade = handled[0]
        assert fade.strategy is StrategyKey.FUDKII_CT_Y and fade.stop == pytest.approx(188.30)
        assert fade.reason.endswith("stop 0.1 ATR30 past 1wk.R1 188.10 — nearer than 1 ATR30")
        assert fade.context["confluence"]["stop_zone"] == "0.1 ATR30 past 1wk.R1 188.10 — nearer than 1 ATR30"
        ev = [x for x in await e.ledger.rows_between("events", 0, time.time() + 5) if x.get("kind") == "counter.gap_fade"]
        assert ev[-1]["stopNote"].startswith("0.1 ATR30 past 1wk.R1")
    finally:
        await e.stop()
