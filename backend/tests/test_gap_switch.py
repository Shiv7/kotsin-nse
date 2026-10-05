"""The gap switch (operator, 2026-10-06: "include relative volume to decide trade in-trend/countertrend"): at a 09:45
trigger that gapped 0.3 daily ATR or more its own way, the trigger bar's volume against the NIFTY50's decides the side.
At 3x the members' or more the move is followed — RT-Y takes it through its pivot and gap gates, CT-Y does not fade
it; below that, as before.

Route study, 6 Oct (option model, published and graded-F triggers): 1-28 Sep 17 such triggers in trend +₹1,11,091,
15 won (+₹76,058 on 10 without 11 and 24 Sep); 29 Sep - 5 Oct +₹6,747 on 3 (AMBUJACEM 1 Oct 3.15x +₹20,862; PETRONET
5 Oct 3.01x −₹13,898; NYKAA 5 Oct 5.68x −₹217); the same triggers faded lost in both windows."""

from __future__ import annotations

import time

import pytest

from kotsin_nse.bars.pivots import Zone
from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind
from kotsin_nse.engine import IN_TREND_BOOKS, Engine
from kotsin_nse.market.indices import NIFTY50
from kotsin_nse.risk.limits import (
    CT_X_LIMITS,
    CT_Y_LIMITS,
    RT_N_LIMITS,
    RT_X_LIMITS,
    RT_Y_F_LIMITS,
    RT_Y_LIMITS,
)
from kotsin_nse.strategy.regime_gates import gap_switch, rt_gate_reasons, trigger_verdicts
from tests.test_gate_b_and_gap_fade import OPT, UND, _books, _drive, _paper, _seed, _trigger

#: BAJFINANCE 5 Oct 09:45 as logged, with a key pivot just ahead added (gate B would stand aside twice)
GAP = {"share": 0.62, "names": 218, "openBar": True, "gapDatr": 0.76, "pivotsAheadAtr": [["1mo.PIVOT", 0.4]],
       "pivotsAhead": ["1mo.PIVOT +0.4 ATR"], "atr30": 2.0}


def test_only_rt_y_follows_the_gap_and_only_ct_y_stands_aside_from_fading_it():
    assert RT_Y_LIMITS.gap_switch_rel_vol == 3.0 and RT_Y_LIMITS.gap_fade_max_rel_vol is None
    assert CT_Y_LIMITS.gap_fade_max_rel_vol == 3.0 and CT_Y_LIMITS.gap_switch_rel_vol is None
    assert RT_Y_F_LIMITS.gap_switch_rel_vol is None, "the graded-F shadow keeps RT-Y's rules as they were"
    for lim in (RT_X_LIMITS, RT_N_LIMITS, CT_X_LIMITS):
        assert lim.gap_switch_rel_vol is None and lim.gap_fade_max_rel_vol is None


def test_the_volume_of_the_gap_waives_the_pivot_and_the_gap_gates_and_nothing_else():
    assert [g for g, _ in rt_gate_reasons({**GAP, "relVol": 2.83}, RT_Y_LIMITS)] == ["pivot_ahead", "open_gap"], "BAJFINANCE: 2.83x"
    assert rt_gate_reasons({**GAP, "relVol": 3.15}, RT_Y_LIMITS) == [], "AMBUJACEM 1 Oct: 3.15x"
    assert [g for g, _ in rt_gate_reasons({**GAP, "relVol": 3.15, "share": 0.44}, RT_Y_LIMITS)] == ["breadth"], "breadth still stands"
    assert [g for g, _ in rt_gate_reasons({**GAP, "relVol": 3.15}, RT_Y_F_LIMITS)] == ["pivot_ahead", "open_gap"]
    assert [g for g, _ in rt_gate_reasons({**GAP, "relVol": None}, RT_Y_LIMITS)] == ["pivot_ahead", "open_gap"], "no reading: as before"
    off_gap = {**GAP, "relVol": 5.0, "openBar": False}
    assert [g for g, _ in rt_gate_reasons(off_gap, RT_Y_LIMITS)] == ["pivot_ahead"], "off a 09:45 gap volume decides nothing"
    small = {**GAP, "relVol": 5.0, "gapDatr": 0.29}
    assert [g for g, _ in rt_gate_reasons(small, RT_Y_LIMITS)] == ["pivot_ahead"]
    assert "3.1x the NIFTY50's volume" in gap_switch({**GAP, "relVol": 3.15}, RT_Y_LIMITS)


def test_rt_ys_verdict_names_the_switch():
    ctx = {"kind": "regime.breadth", **GAP, "relVol": 3.15}
    v = trigger_verdicts({"signal_id": "S", "direction": "BULLISH"}, [ctx], fade_x=None, gap_fade=None, rt_y_held=False, lim_y=RT_Y_LIMITS)
    assert v["rtY"]["action"] == "TAKE" and any(w.startswith("gap switch: 09:45 gap 0.76") for w in v["rtY"]["why"])


@pytest.mark.asyncio
async def test_rt_y_takes_the_heavy_gap_through_gate_b(settings):
    e = await _paper(settings)
    try:
        sig = _trigger()
        e._breadth_at[sig.signal_id] = {**GAP, "relVol": 3.15}
        await _drive(e, sig, OPT, IN_TREND_BOOKS)
        assert {"FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"} <= _books(e)
    finally:
        await e.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(("rel", "faded"), [(3.15, False), (2.83, True), (None, True)])
async def test_ct_y_does_not_fade_a_gap_on_the_volume_rt_y_follows(settings, rel, faded):
    e = Engine(settings)
    await e.start()
    try:
        _seed(e, zones=[Zone(184.0, 6.0, ["1d.PIVOT"])])
        trig = _trigger(Direction.BULLISH)
        e._breadth_at[trig.signal_id] = {**GAP, "relVol": rel}
        handled = []

        async def capture(sig, bar, *, adopt=True):
            handled.append(sig)

        e._handle_signal = capture  # type: ignore[method-assign]
        await e._gap_fade(trig, None, UND)
        assert bool(handled) is faded
        ev = [x for x in await e.ledger.rows_between("events", 0, time.time() + 5) if x.get("kind") == "counter.gap_fade"]
        assert ev and (("blocked" in ev[-1]) is not faded)
        if not faded:
            assert ev[-1]["blocked"].startswith("volume 3.1x the NIFTY50's (≥ 3x)") and trig.signal_id not in e._gap_faded
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_gap_trigger_waits_briefly_for_the_nifty50s_reading(settings, monkeypatch):
    import kotsin_nse.engine as eng

    e = Engine(settings)
    for m in sorted(NIFTY50)[:30]:
        e.underlyings[m] = Instrument(m, m, Segment.NSE_EQ, InstrumentKind.EQUITY, underlying=m)
    e.underlyings["TATASTEEL"] = UND
    monkeypatch.setattr(eng, "REL_VOL_POLL_S", 0.01)
    monkeypatch.setattr(e, "_slot_ratio", lambda m, t: 3.6)
    calls = {"n": 0}

    def n50(t):
        calls["n"] += 1
        return (1.4, None if calls["n"] < 4 else 1.2)  # the members' bars come in over the first reads

    monkeypatch.setattr(e, "n50_relative_volume", n50)
    sig = _trigger()
    ctx = {**GAP, "volSurgeT": 2.0}
    await e._await_gap_rel_vol(sig, ctx)
    assert ctx["relVol"] == pytest.approx(3.0) and calls["n"] >= 4
    # not a gap: nothing waited for
    calls["n"] = 0
    flat = {**GAP, "gapDatr": 0.1}
    await e._await_gap_rel_vol(sig, flat)
    assert calls["n"] == 0 and "relVol" not in flat
    # never comes: gives up after REL_VOL_WAIT_S, the books decide as before
    monkeypatch.setattr(eng, "REL_VOL_WAIT_S", 0.05)
    monkeypatch.setattr(e, "n50_relative_volume", lambda t: (None, None))
    late = dict(GAP)
    t0 = time.monotonic()
    await e._await_gap_rel_vol(sig, late)
    assert "relVol" not in late and time.monotonic() - t0 < 1.0
