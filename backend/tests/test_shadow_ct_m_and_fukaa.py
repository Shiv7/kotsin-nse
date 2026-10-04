"""The Shadow page and every card, for the two shadows added 2-3 Oct (operator, 2026-10-03: "make the
requisite changes in shadow page in dashboard and strategy pages"): FUDKII-CT-M's market-against fade —
its own tab, a column in the trigger table and a chip on every card — and FUKAA in shadow, its own tab
with the inputs and the alignment it logged."""

from __future__ import annotations

from datetime import date, datetime
from datetime import time as dtime

from kotsin_nse.api.daybook import ab_summary
from kotsin_nse.api.shadow import (
    ShadowData,
    fukaa_shadow_summary,
    market_fade_summary,
    render_shadow,
    shadow_rows,
)
from kotsin_nse.market.session import IST
from kotsin_nse.risk.limits import RT_Y_LIMITS
from kotsin_nse.strategy.regime_gates import market_fade_verdict, trigger_verdicts

DAY = date(2026, 10, 5)
T0 = int(datetime.combine(DAY, dtime(9, 15), tzinfo=IST).timestamp())


def _ledger():
    signals = [
        {"signal_id": "P", "strategy": "FUDKII", "symbol": "INFY", "direction": "BULLISH", "ts": T0, "rr": 3.0, "decision": "PAPER_FILLED"},
        {"signal_id": "Q", "strategy": "FUDKII", "symbol": "TCS", "direction": "BEARISH", "ts": T0 + 1800, "rr": 2.0, "decision": "PAPER_FILLED"},
        {"signal_id": "MP", "strategy": "FUDKII_CT_M", "symbol": "INFY", "direction": "BEARISH", "ts": T0, "source_signal_id": "P",
         "decision": "PAPER_FILLED", "reason": "MARKET FADE of P"},
        {"signal_id": "KP", "strategy": "FUKAA", "symbol": "INFY", "direction": "BULLISH", "ts": T0, "source_signal_id": "P", "decision": "SHADOW"},
    ]
    events = [
        {"kind": "regime.breadth", "signal_id": "P", "share": 0.30, "names": 220, "openBar": True, "gapDatr": 0.1, "pivotsAhead": []},
        {"kind": "regime.breadth", "signal_id": "Q", "share": 0.48, "names": 220, "openBar": False, "pivotsAhead": []},
        {"kind": "counter.market_fade", "signal_id": "P", "symbol": "INFY", "fade_signal_id": "MP", "side": "PE", "stop": 1512.0,
         "targets": [1490.0, 1480.0], "rr": 1.8, "grade": "B", "breadth": 0.30},
        {"kind": "rt_twin.skipped", "signal_id": "Q", "book": "FUDKII_CT_M", "gate": "market_with", "breadth": 0.48,
         "reason": "market not against the trigger: 48% of 220 names agree > 45%"},
        {"kind": "fukaa.shadow", "signal_id": "KP", "symbol": "INFY", "direction": "BULLISH", "ts": T0 + 1805, "entry": 1500.0, "stop": 1490.0,
         "targets": [1530.0], "rr": 3.0,
         "evidence": {"composite": 65.0, "surge_used": 5.2, "rel_volume": 2.1, "momentum_score": 1.4, "oi_change_pct": 3.2, "oi_rel_z": 1.1, "promoted": 0.0},
         "alignment": {"breadth": 0.30, "withMarket": False, "priceChangePct": 1.2, "oiChangePct": 3.2, "oiQuadrant": "long build-up", "oiAgrees": True}},
    ]
    positions = [
        {"id": "m-P", "strategy": "FUDKII_CT_M", "signal_id": "MP", "status": "CLOSED", "entry": 12.5, "exit_reason": "TRAIL",
         "instrument": {"name": "INFY 27 OCT 2026 PE 1500.00"}},
        {"id": "x-P", "strategy": "FUDKII_RT_X", "signal_id": "P", "status": "CLOSED"},
    ]
    trades = [{"position_id": "m-P", "strategy": "FUDKII_CT_M", "net": 5000.0}, {"position_id": "x-P", "strategy": "FUDKII_RT_X", "net": -2000.0}]
    return signals, positions, trades, events


def test_ct_ms_verdict_says_what_it_did_or_would_do():
    fade = {"kind": "counter.market_fade", "side": "PE", "stop": 1512.0, "targets": [1490.0], "rr": 1.8, "grade": "B", "breadth": 0.30}
    v = market_fade_verdict([fade], {"share": 0.30}, "PE")
    assert v["action"] == "FADE" and v["breadth"] == 0.30 and v["targets"] == [1490.0] and "≤ 45%" in v["why"]
    skip = {"kind": "rt_twin.skipped", "book": "FUDKII_CT_M", "breadth": 0.62, "reason": "market not against the trigger: 62% of 220 names agree > 45%"}
    assert market_fade_verdict([skip], {"share": 0.62}, "PE")["why"].startswith("market not against")
    assert market_fade_verdict([], {"share": 0.40}, "PE")["action"] == "WOULD FADE", "a trigger from before CT-M: what its rule would do"
    assert market_fade_verdict([], {"share": 0.46}, "PE")["action"] == "NONE"
    assert market_fade_verdict([], None, "PE")["why"] == "no breadth logged at the trigger"
    sgn = {"signal_id": "P", "direction": "BULLISH"}
    assert trigger_verdicts(sgn, [{"kind": "regime.breadth", "share": 0.30}, fade], fade_x=None, gap_fade=None, rt_y_held=False,
                            lim_y=RT_Y_LIMITS)["ctM"]["action"] == "FADE"


def test_the_market_fade_tally_compares_ct_m_with_the_books_on_the_same_triggers():
    signals, positions, trades, events = _ledger()
    m = market_fade_summary(signals=signals, positions=positions, trades=trades, events=events)
    (r,) = m["rows"]
    assert (r["symbol"], r["breadth"], r["side"], r["contract"], r["fill"], r["exit_reason"]) == ("INFY", 0.30, "PE", "INFY 27 OCT 2026 PE 1500.00", 12.5, "TRAIL")
    assert r["ct_m"] == {"status": "EXITED", "net": 5000.0} and r["rt_x"] == {"status": "EXITED", "net": -2000.0}
    t = m["total"]
    assert (t["fades"], t["closed"], t["net"], t["win"], t["rt_x_net"]) == (1, 1, 5000.0, 1, -2000.0)
    assert (t["skipped"], t["near"], t["mid"], t["far"]) == (1, 1, 0, 0) and m["skipped"][0]["symbol"] == "TCS"


def test_fukaas_tab_lists_its_inputs_alignment_and_the_books_on_the_same_trigger():
    signals, positions, trades, events = _ledger()
    f = fukaa_shadow_summary(signals=signals, positions=positions, trades=trades, events=events)
    (r,) = f["rows"]
    assert (r["parent"], r["surge"], r["rel_volume"], r["oi_change"], r["oi_quadrant"], r["with_market"], r["oi_agrees"]) == (
        "P", 5.2, 2.1, 3.2, "long build-up", False, True)
    assert r["rt_x"]["net"] == -2000.0 and r["ct_m"]["net"] == 5000.0, "the same FUDKII trigger, traded in-trend and faded"
    assert f["total"]["signals"] == 1 and f["total"]["counter"] == 1 and f["total"]["oi_agrees"] == 1


def test_the_page_has_both_tabs_the_ct_m_column_and_the_tallies():
    signals, positions, trades, events = _ledger()
    rows = shadow_rows(signals=signals, positions=positions, trades=trades, events=events, lim_y=RT_Y_LIMITS)
    assert rows[0]["ctM"]["action"] == "FADE" and rows[1]["ctM"]["action"] == "NONE"
    page = render_shadow(ShadowData(
        day=DAY, days=[DAY.isoformat()], rows=rows, ab=ab_summary(signals=signals, positions=positions, trades=trades, events=events),
        market_fade=market_fade_summary(signals=signals, positions=positions, trades=trades, events=events),
        fukaa=fukaa_shadow_summary(signals=signals, positions=positions, trades=trades, events=events)))
    for s in ("Market fade · CT-M ≤45%", "Market fade · every fade · 1", "the closest skips", "INFY 27 OCT 2026 PE 1500</td>",
              "FUKAA · shadow", "FUKAA · every shadow signal · 1", "long build-up", "<th>CT-M</th>", "CT-M fades", "FUKAA signals",
              "market not against the trigger: 48%"):
        assert s in page, s
