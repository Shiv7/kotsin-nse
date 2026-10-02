"""The Shadow page (operator, 2026-09-26): "create a page for all shadow items and [the RT-Y A/B daily
table] named Shadow on the new page http://127.0.0.1:8500/shadow" — downloadable, like /temporary."""

from __future__ import annotations

from datetime import date, datetime
from datetime import time as dtime

import pytest

from kotsin_nse.api.shadow import (
    TABS,
    ShadowData,
    gap_fade_summary,
    label_summary,
    render_shadow,
    shadow_rows,
    wide_stop_summary,
)
from kotsin_nse.market.session import IST
from kotsin_nse.risk.limits import RT_Y_LIMITS

DAY = date(2026, 9, 28)
T0 = int(datetime.combine(DAY, dtime(9, 15), tzinfo=IST).timestamp())


def _ledger():
    signals = [
        {"signal_id": "A", "strategy": "FUDKII", "symbol": "LODHA", "direction": "BEARISH", "ts": T0, "rr": 19.2, "decision": "PAPER_FILLED"},
        {"signal_id": "B", "strategy": "FUDKII", "symbol": "NYKAA", "direction": "BULLISH", "ts": T0 + 4 * 3600, "rr": 10.0, "decision": "PAPER_FILLED"},
        {"signal_id": "GA", "strategy": "FUDKII_CT_Y", "symbol": "LODHA", "direction": "BULLISH", "ts": T0, "source_signal_id": "A",
         "stop": 1143.3, "targets": [1160.0], "rr": 1.4, "grade": "C", "decision": "PAPER_FILLED", "reason": "GAP FADE of A"},
    ]
    events = [
        {"kind": "regime.breadth", "signal_id": "A", "share": 0.61, "names": 220, "efficiency": 0.42, "volBand": "NEUTRAL", "gapDatr": 0.45,
         "openBar": True, "pivotsAhead": ["1d.S1 +0.47 ATR"], "pivotsAheadAtr": [["1d.S1", 0.47]]},
        {"kind": "regime.breadth", "signal_id": "B", "share": 0.52, "names": 220, "efficiency": 0.71, "volBand": "ELEVATED", "gapDatr": -0.2,
         "openBar": False, "pivotsAhead": [], "pivotsAheadAtr": []},
        {"kind": "rt_twin.skipped", "signal_id": "A", "book": "FUDKII_RT_Y", "gate": "pivot_ahead", "reason": "pivot just ahead: 1d.S1 +0.47 ATR"},
        {"kind": "counter.gap_fade", "signal_id": "A", "side": "CE", "stop": 1143.3, "targets": [1160.0], "rr": 1.4, "grade": "C", "gapDatr": 0.45},
    ]
    positions = [
        {"id": "x-A", "strategy": "FUDKII_RT_X", "signal_id": "A", "status": "CLOSED"},
        {"id": "y-B", "strategy": "FUDKII_RT_Y", "signal_id": "B", "status": "OPEN"},
        {"id": "cy-A", "strategy": "FUDKII_CT_Y", "signal_id": "GA", "status": "CLOSED"},
    ]
    trades = [{"position_id": "x-A", "strategy": "FUDKII_RT_X", "net": -4200.0}, {"position_id": "cy-A", "strategy": "FUDKII_CT_Y", "net": 3100.0}]
    return signals, positions, trades, events


def test_each_trigger_carries_its_labels_verdicts_and_every_books_position():
    signals, positions, trades, events = _ledger()
    rows = shadow_rows(signals=signals, positions=positions, trades=trades, events=events, lim_y=RT_Y_LIMITS)
    a, b = rows
    assert (a["symbol"], a["breadth"], a["efficiency"], a["volBand"], a["gapDatr"], a["openBar"]) == ("LODHA", 0.61, 0.42, "NEUTRAL", 0.45, True)
    assert a["rtY"]["action"] == "SKIP" and a["rtY"]["gate"] == "pivot_ahead"
    assert a["ctY"]["action"] == "GAP FADE" and a["books"]["FUDKII_CT_Y"] == {"status": "EXITED", "net": 3100.0}
    assert a["books"]["FUDKII_RT_X"] == {"status": "EXITED", "net": -4200.0} and a["books"]["FUDKII_RT_N"]["status"] == "NONE"
    assert b["rtY"]["action"] == "TAKE" and b["rtY"]["state"] == "taken" and b["books"]["FUDKII_RT_Y"]["status"] == "OPEN"
    assert b["ctY"]["action"] == "NONE"


def test_the_page_renders_every_section_and_offers_the_download():
    from kotsin_nse.api.daybook import ab_summary

    signals, positions, trades, events = _ledger()
    rows = shadow_rows(signals=signals, positions=positions, trades=trades, events=events, lim_y=RT_Y_LIMITS)
    ab = ab_summary(signals=signals, positions=positions, trades=trades, events=events)
    assert ab["total"]["gated"] == 1 and ab["total"]["gate_pivot_ahead"] == 1, "every RT-Y gate counts as gated out, by name"
    data = ShadowData(day=DAY, days=[DAY.isoformat()], rows=rows, ab=ab,
                      wide=wide_stop_summary(positions=positions, trades=trades),
                      gap=gap_fade_summary(signals=signals, positions=positions, trades=trades, events=events),
                      labels=label_summary(rows))
    page = render_shadow(data)
    for s in ("<h1>Shadow</h1>", "RT-Y gate · paper A/B since 2026-09-28", "what RT-Y stood aside from", "Gap fade · every 09:45 gap trigger",
              'href="/shadow.xlsx?day=2026-09-28"', "LODHA", "1d.S1 +0.47 ATR", "pivot ahead 1"):
        assert s in page, s


@pytest.mark.asyncio
async def test_the_shadow_routes_serve_the_page_the_workbook_and_json(settings):
    import httpx

    from kotsin_nse.api.routes import build_app
    from kotsin_nse.engine import Engine

    engine = Engine(settings)
    await engine.ledger.init()
    signals, _, _, events = _ledger()
    for s in signals:
        await engine.ledger.insert_signal(s, s.get("decision", ""), "")
    for ev in events:
        await engine.ledger.event(ev["kind"], {k: v for k, v in ev.items() if k != "kind"})
    app = build_app(engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        page = await c.get("/shadow", params={"day": DAY.isoformat()})
        assert page.status_code == 200 and "<h1>Shadow</h1>" in page.text and "LODHA" in page.text
        x = await c.get("/shadow.xlsx", params={"day": DAY.isoformat()})
        assert x.status_code == 200 and x.content[:2] == b"PK", "a real workbook"
        assert x.headers["content-disposition"] == 'attachment; filename="shadow-2026-09-28.xlsx"'
        j = (await c.get("/api/shadow", params={"day": DAY.isoformat()})).json()
        assert j["day"] == "2026-09-28" and [r["symbol"] for r in j["rows"]] == ["LODHA", "NYKAA"]
        assert [t["id"] for t in j["tabs"]] == ["gate-b", "wide-stop", "graded-f", "gap-fade", "volume", "labels"] and all(t["brief"]["decide"] for t in j["tabs"])
        assert set(j) >= {"wideStop", "gapFade", "labels", "ab"}
        assert (await c.get("/shadow", params={"day": "yesterday"})).status_code == 400
    await engine.ledger.close()


def test_every_tab_opens_with_its_brief_and_is_deep_linkable():
    """Operator, 2026-09-26: "each shadow in a different tab and name it well and explain the logic and
    pros cons and what are we testing in the shadow against and for in the beginning of the list"."""
    signals, positions, trades, events = _ledger()
    rows = shadow_rows(signals=signals, positions=positions, trades=trades, events=events, lim_y=RT_Y_LIMITS)
    from kotsin_nse.api.daybook import ab_summary

    page = render_shadow(ShadowData(day=DAY, days=[DAY.isoformat()], rows=rows,
                                    ab=ab_summary(signals=signals, positions=positions, trades=trades, events=events)))
    assert [t.id for t in TABS] == ["gate-b", "wide-stop", "graded-f", "gap-fade", "volume", "labels"]
    for t in TABS:
        assert f'href="#{t.id}"' in page and f'<section class="tab" id="{t.id}"' in page, t.id
        section = page.split(f'id="{t.id}"', 1)[1].split("</section>", 1)[0]
        brief_at = section.index('class="brief"')
        assert brief_at < section.find("<table") or section.find("<table") < 0, "the brief comes first"
        for label in ("What we are testing.", "For (the rule under test).", "Against (the baseline).", "The logic.", "Pros", "Cons", "How we decide."):
            assert label in section, (t.id, label)
        b = t.brief
        assert all((b.name, b.testing, b.for_rule, b.against, b.logic, b.pros, b.cons, b.decide)), t.id
    for colour in ("purple", "violet", "fuchsia", "indigo", "pink"):
        assert colour not in page.split("<style>", 1)[1].split("</style>", 1)[0].replace("--indigo", ""), colour


def test_the_wide_stop_tab_pairs_each_shadow_trade_with_its_rt_y_trade():
    positions = [
        {"id": "y1", "strategy": "FUDKII_RT_Y", "signal_id": "A", "status": "CLOSED", "equity_sl": 1490.0, "opened_ts": 1.0, "symbol": "RELIANCE",
         "instrument": {"name": "RELIANCE CE 1520"}, "entry": 20.0, "closed_ts": 2.0},
        {"id": "w1", "strategy": "FUDKII_RT_Y_W1", "signal_id": "A", "status": "CLOSED", "equity_sl": 1475.1, "opened_ts": 1.0, "symbol": "RELIANCE",
         "instrument": {"name": "RELIANCE CE 1520"}, "entry": 20.0, "closed_ts": 3.0},
        {"id": "y2", "strategy": "FUDKII_RT_Y", "signal_id": "B", "status": "CLOSED", "equity_sl": 800.0, "opened_ts": 4.0, "symbol": "NYKAA"},
        {"id": "w2", "strategy": "FUDKII_RT_Y_W1", "signal_id": "B", "status": "OPEN", "equity_sl": 792.0, "opened_ts": 4.0, "symbol": "NYKAA"},
    ]
    trades = [
        {"position_id": "y1", "net": -3000.0, "exit_reason": "SL-EQ"}, {"position_id": "w1", "net": 1200.0, "exit_reason": "TRAIL"},
        {"position_id": "y2", "net": 500.0, "exit_reason": "TARGET"},
    ]
    w = wide_stop_summary(positions=positions, trades=trades)
    a, b = w["pairs"]
    assert (a["symbol"], a["y"]["net"], a["w"]["net"], a["diff"], a["y"]["stop"], a["w"]["stop"]) == ("RELIANCE", -3000.0, 1200.0, 4200.0, 1490.0, 1475.1)
    assert b["w"]["status"] == "OPEN" and b["diff"] is None, "an open shadow waits for its close"
    t = w["total"]
    assert (t["pairs"], t["closed"], t["y_net"], t["w_net"], t["diff"], t["better"], t["worse"]) == (2, 1, -3000.0, 1200.0, 4200.0, 1, 0)


def test_the_gap_fade_tab_shows_ct_ys_plan_and_trade_against_the_in_trend_books():
    signals, positions, trades, events = _ledger()
    g = gap_fade_summary(signals=signals, positions=positions, trades=trades, events=events)
    (r,) = g["rows"]
    assert (r["symbol"], r["side"], r["stop"], r["targets"], r["rr"], r["grade"]) == ("LODHA", "CE", 1143.3, [1160.0], 1.4, "C")
    assert r["ct"]["net"] == 3100.0 and r["x"]["net"] == -4200.0 and r["n"]["status"] == "NONE"
    assert g["total"]["closed"] == 1 and g["total"]["ct_net"] == 3100.0 and g["total"]["x_net"] == -4200.0


def test_the_labels_tab_buckets_rt_xs_outcome_by_each_label():
    signals, positions, trades, events = _ledger()
    rows = shadow_rows(signals=signals, positions=positions, trades=trades, events=events, lim_y=RT_Y_LIMITS)
    groups = {g["label"]: g["buckets"] for g in label_summary(rows)["groups"]}
    assert groups["Breadth"]["> 50% agree"]["triggers"] == 2
    assert groups["Key pivot ≤ 0.5 ATR ahead"]["yes"] == {"triggers": 1, "x_n": 1, "x_net": -4200.0, "x_win": 0}
    assert groups["09:45 gap its own way ≥ 0.3 dATR"]["yes"]["triggers"] == 1
    assert set(groups["Own-volatility band"]) == {"NEUTRAL", "ELEVATED"}


def test_the_workbook_has_every_tabs_tables_and_the_briefs():
    from kotsin_nse.api.daybook import ab_summary
    from kotsin_nse.api.export import tables_from_html

    signals, positions, trades, events = _ledger()
    rows = shadow_rows(signals=signals, positions=positions, trades=trades, events=events, lim_y=RT_Y_LIMITS)
    page = render_shadow(ShadowData(day=DAY, days=[DAY.isoformat()], rows=rows,
                                    ab=ab_summary(signals=signals, positions=positions, trades=trades, events=events),
                                    wide=wide_stop_summary(positions=positions, trades=trades),
                                    gap=gap_fade_summary(signals=signals, positions=positions, trades=trades, events=events),
                                    labels=label_summary(rows)))
    _, sheets, notes = tables_from_html(page)
    names = [s.name for s in sheets]
    for want in ("RT-Y gate", "Gate B · what RT-Y stood aside from", "Wide stop · running total", "Wide stop · every paired trade",
                 "Gap fade · running total", "Gap fade · every 09:45 gap trigger", "Labels · RT-X's outcome by label", "Labels · every trigger"):
        assert any(n.startswith(want) for n in names), (want, names)
    assert any(n.startswith("What we are testing.") for n in notes) and any("How we decide." in n for n in notes)


def test_the_volume_tab_buckets_the_three_readings_and_opens_with_its_brief():
    """Operator, 2026-09-26: "shall we check volume for equity and FUT both before deciding dried
    volume, against the market's volume at that time?" — logged on every trigger, tabulated here."""
    from datetime import date

    from kotsin_nse.api.shadow import BOOKS, ShadowData, render_shadow, volume_summary

    def row(sym, dried, rel, surge, x_net, y_net):
        return {"signal_id": sym, "symbol": sym, "direction": "BULLISH", "fired": 1790566200.0, "rr": 2.0, "decision": "PAPER_FILLED",
                "breadth": 0.6, "names": 214, "efficiency": 0.4, "volBand": "NEUTRAL", "gapDatr": 0.1, "openBar": False, "pivotsAhead": [],
                "logged": True, "rtY": {"action": "TAKE", "state": "taken", "gate": None, "why": []},
                "ctY": {"action": "NONE", "side": "PE", "stop": None, "targets": [], "rr": None, "grade": None, "why": "in trend — no fade"},
                "books": {b: {"status": "NONE", "net": None} for b in BOOKS}
                | {"FUDKII_RT_X": {"status": "EXITED", "net": x_net}, "FUDKII_RT_Y": {"status": "EXITED" if y_net is not None else "NONE", "net": y_net}},
                "volSurgeT": 0.5 if dried else 1.6, "volSurgeT1": 0.6, "mktSurgeT": 0.7, "mktSurgeT1": 0.9, "volDried": dried, "volDriedRel": rel, "volSurge": surge}
    rows = [row("A", True, False, False, 500.0, None), row("B", False, False, True, -300.0, -200.0), row("C", False, True, True, -100.0, 50.0)]
    v = volume_summary(rows)
    dried = next(g for g in v["groups"] if g["label"].startswith("Dried — the RT"))
    assert dried["buckets"]["yes"]["x_net"] == 500.0 and dried["buckets"]["no"]["x_n"] == 2
    surge = next(g for g in v["groups"] if g["label"].startswith("Surge"))
    assert surge["buckets"]["yes"]["y_n"] == 2 and surge["buckets"]["yes"]["y_net"] == -150.0
    page = render_shadow(ShadowData(day=date(2026, 9, 28), days=["2026-09-28"], rows=rows,
                                    ab={"since": "2026-09-28", "target": 50, "days": [], "total": {"y_n": 0}}, volume=v))
    assert 'id="volume"' in page and "Volume · dried &amp; surge" in page and "What we are testing" in page
    assert "Dried vs market" in page and "67 triggers only" in page


def test_every_trigger_logs_its_volume_readings():
    from kotsin_nse.strategy.regime_gates import volume_labels

    assert volume_labels({"volSurgeT": 0.5, "volSurgeT1": 0.6, "mktSurgeT": 0.5, "mktSurgeT1": 0.6}) == {
        "volDried": True, "volSurge": False, "volDriedRel": False}, "quiet stock in a quiet market: dried, but not against the market"
    assert volume_labels({"volSurgeT": 1.8, "volSurgeT1": 1.0}) == {"volDried": False, "volSurge": True}
    assert volume_labels({}) == {}
