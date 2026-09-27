"""The shareable day-book page: what it shows, and that it really does expire."""

from __future__ import annotations

import json
import time
from datetime import date

from kotsin_nse.api.daybook import TTL_HOURS, TemporaryPage, assemble, render


def _signal(**kw):
    base = dict(
        signal_id="S1", strategy="FUDKII", symbol="AUBANK", direction="BEARISH",
        ts=1_790_221_500, created_ts=1_790_223_304,
        grade="A", entry=1000.9, decision="PAPER_FILLED", decision_reason="ST flip DOWN; grade A",
        evidence={"atr": 7.69, "atr_pct": 0.77, "oi": 17_837_000.0, "oi_change_pct": 1.4},
        context={
            "confluence": {"stop": 1002.15, "stop_zone": "1d.S3", "targets": [979.55],
                           "target_zones": ["1d.S4"], "rr": 17.08, "fortress": 6.0, "room_ratio": 2.78},
            "zones": [{"price": 979.56, "strength": 6.0, "wall": True, "members": ["1d.S4"]},
                      {"price": 1200.0, "strength": 3.0, "wall": True, "members": ["1d.R2"]}],
        },
    )
    base.update(kw)
    return base


def _route():
    return {
        "kind": "counter.route", "signal_id": "S1", "symbol": "AUBANK",
        "legs": [
            {"name": "equity", "open": 1022.0, "high": 1025.3, "low": 999.5, "close": 1000.9,
             "atr": 7.69, "surgeT": 2.31, "surgeT1": 0.0, "volume": "average",
             "levels": {"1d.S3": 1002.13, "1d.S4": 979.5}},
            {"name": "future", "open": 1043.8, "high": 1043.8, "low": 1001.2, "close": 1004.4,
             "atr": 9.44, "surgeT": 11.15, "surgeT1": 0.68, "volume": "surge",
             "levels": {"1wk.S2": 995.4, "1d.S3": 993.7, "1d.S4": 966.7, "1wk.S1": 1018.2}},
        ],
    }


def _position():
    return {
        "id": "p1", "symbol": "AUBANK", "strategy": "FUDKII", "status": "CLOSED",
        "opened_ts": 1_790_221_504, "closed_ts": 1_790_221_571, "qty": 15_000, "entry": 6.55,
        "initial_option_sl": 5.98, "option_sl": 5.98, "option_targets": [13.51],
        "exit_price": 5.51, "exit_reason": "SL-OP",
        "instrument": {"name": "AUBANK 29 SEP 2026 PE 980.00", "lot_size": 1000},
    }


def test_a_trigger_carries_its_levels_surge_and_pivot_read():
    rows = assemble(signals=[_signal()], positions=[_position()],
                    trades=[{"position_id": "p1", "net": -15_748, "r_multiple": -1.84}],
                    events=[_route()])
    assert len(rows) == 1
    r = rows[0]
    assert r["eq_stop"] == 1002.15 and r["eq_targets"] == [979.55]
    assert r["wall_price"] == 979.56 and r["wall_atr"] == 2.78, "the nearest wall AHEAD, in ATR"
    assert (r["surge_t"], r["surge_t1"], r["vol_label"]) == (2.31, 0.0, "average")
    assert (r["fut_surge_t"], r["fut_vol_label"]) == (11.15, "surge")
    # the pivot read: 1d.S3 at 1002.13 sits inside the 999.5-1025.3 bar, a sixth of an ATR from close
    p = r["pivot_eq"]
    assert p["label"] == "1d.S3" and p["inside_bar"] and p["nearest_to"] == "close"
    assert p["atr_from_nearest"] == 0.16
    assert r["pivot_fut"]["label"] == "1wk.S2" and not r["pivot_fut"]["inside_bar"]
    f = r["fills"][0]
    assert f["lots"] == 15 and f["premium"] == 6.55 and f["net"] == -15_748 and f["r"] == -1.84


def test_a_wall_behind_the_trade_is_never_reported_as_the_one_ahead():
    """A bearish trade's wall is below it. The 1200 zone is behind and must not be picked."""
    rows = assemble(signals=[_signal()], positions=[], trades=[], events=[_route()])
    assert rows[0]["wall_price"] == 979.56
    bull = _signal(direction="BULLISH", signal_id="S2")
    rows = assemble(signals=[bull], positions=[], trades=[], events=[])
    assert rows[0]["wall_price"] == 1200.0, "bullish looks up"


def test_a_refused_trigger_still_reports_everything_but_the_fill():
    sig = _signal(decision="NO_INSTRUMENT", decision_reason="no tradeable strike: 980:one-sided")
    rows = assemble(signals=[sig], positions=[], trades=[], events=[_route()])
    r = rows[0]
    assert r["fills"] == [] and r["decision"] == "NO_INSTRUMENT"
    assert "one-sided" in r["reason"] and r["surge_t"] == 2.31, "the read survives the refusal"


def test_only_the_parents_triggers_are_listed_and_twin_skips_hang_off_them():
    twin = _signal(strategy="FUDKII_RT_X", signal_id="S9")
    skip = {"kind": "rt_twin.skipped", "signal_id": "S1", "book": "FUDKII_RT_Y",
            "reason": "dried volume equity 0.53/0.00 < 0.85"}
    rows = assemble(signals=[_signal(), twin], positions=[], trades=[], events=[_route(), skip])
    assert len(rows) == 1, "one row per parent trigger, not one per book"
    assert rows[0]["skips"] == [{"book": "FUDKII_RT_Y", "why": "dried volume equity 0.53/0.00 < 0.85"}]


def test_the_page_renders_every_number_it_was_given():
    rows = assemble(signals=[_signal()], positions=[_position()],
                    trades=[{"position_id": "p1", "net": -15_748, "r_multiple": -1.84}],
                    events=[_route()])
    page = TemporaryPage.__new__(TemporaryPage)  # render needs only a ticket
    from kotsin_nse.api.daybook import Ticket

    t = Ticket(day="2026-09-24", created_ts=time.time(), expires_ts=time.time() + 3600)
    out = render(rows, t)
    for token in ("AUBANK", "1,002.15", "979.55", "2.31", "11.15", "1d.S3", "PE 980.00",
                  "6.55", "SL-OP", "-15,748", "24 September 2026", "deletes itself"):
        assert token in out, f"{token} missing from the page"
    assert out.startswith("<!doctype html>") and out.count("<table") == 3
    assert "overflow-x:auto" in out, "the wide tables scroll rather than clipping"
    assert page is not None


def test_the_ticket_expires_by_deleting_itself(tmp_path):
    store = TemporaryPage(tmp_path)
    assert store.read() is None, "no ticket, no page"

    t = store.create(date(2026, 9, 24))
    assert abs(t.expires_ts - t.created_ts - TTL_HOURS * 3600) < 1
    assert store.read() is not None and store.path.exists()

    # wind it past its expiry: the next read serves nothing AND removes the file
    d = json.loads(store.path.read_text())
    d["expires_ts"] = time.time() - 1
    store.path.write_text(json.dumps(d))
    assert store.read() is None
    assert not store.path.exists(), "an expired page deletes itself rather than lingering"

    store.create(date(2026, 9, 24), hours=1)
    store.revoke()
    assert store.read() is None and not store.path.exists()

    store.path.write_text("{not json")
    assert store.read() is None, "a corrupt ticket is no page, never an exception"


def test_the_signal_time_is_when_it_fired_not_when_its_bar_opened():
    """The bar bucket starts half an hour before the decision. Reporting the bucket as the signal
    time said 09:15 for a signal that fired at 09:45:04."""
    rows = assemble(signals=[_signal()], positions=[], trades=[], events=[_route()])
    r = rows[0]
    assert r["ts"] == 1_790_221_500 and r["fired_ts"] == 1_790_223_304
    assert r["fired_ts"] - r["ts"] == 1804, "half an hour and change apart"
    from kotsin_nse.api.daybook import Ticket
    out = render(rows, Ticket(day="2026-09-24", created_ts=time.time(), expires_ts=time.time() + 60))
    assert ">09:45:04<" in out and ">09:15:00<" in out, "both shown, the fired time first"
    assert out.index(">09:45:04<") < out.index(">09:15:00<")


def test_the_future_reports_its_own_levels_either_side_of_its_close():
    """Not a second ladder the trade acts on — the levels the future would pass through."""
    rows = assemble(signals=[_signal()], positions=[], trades=[], events=[_route()])
    r = rows[0]
    assert r["fut_stop"] == {"label": "1wk.S1", "price": 1018.2}, "nearest level behind a short"
    assert [t["price"] for t in r["fut_targets"]] == [995.4, 993.7, 966.7], "ahead, nearest first"
    assert all(t["price"] < 1004.4 for t in r["fut_targets"])


def test_open_interest_and_the_contract_reach_the_page():
    rows = assemble(signals=[_signal()], positions=[_position()],
                    trades=[{"position_id": "p1", "net": -15_748, "r_multiple": -1.84}],
                    events=[_route()])
    assert rows[0]["oi"] == 17_837_000.0 and rows[0]["oi_change_pct"] == 1.4
    assert rows[0]["contract"] == "AUBANK 29 SEP 2026 PE 980.00"
    from kotsin_nse.api.daybook import Ticket
    out = render(rows, Ticket(day="2026-09-24", created_ts=time.time(), expires_ts=time.time() + 60))
    for token in ("17,837,000", "1.40", "PE 980.00", "OI chg%", "Fut T1", "Eq T4", "OTM contract"):
        assert token in out, f"{token} missing"


def test_the_page_downloads_as_a_workbook_of_exactly_what_it_shows():
    """Operator, 2026-09-25: anything shared on /temporary must also download in a fitting format.
    The workbook is read off the rendered page, so it can never disagree with it: one sheet per
    table, numbers as numbers, percentages as percentages, the prose on a Notes sheet."""
    import io
    import xml.etree.ElementTree as ET
    import zipfile

    from kotsin_nse.api.daybook import Ticket
    from kotsin_nse.api.export import html_to_xlsx, tables_from_html

    rows = assemble(signals=[_signal()], positions=[_position()],
                    trades=[{"position_id": "p1", "net": -15_748, "r_multiple": -1.84}],
                    events=[_route()])
    t = Ticket(day="2026-09-24", created_ts=time.time(), expires_ts=time.time() + 3600)
    out = render(rows, t)
    assert 'href="/temporary.xlsx"' in out, "the page offers its own download"

    title, sheets, notes = tables_from_html(out)
    assert title.startswith("FUDKII Day Book")
    assert [s.name.split(" · ")[0] for s in sheets] == ["Signals", "Executions", "Twins that stood aside"]
    sig = sheets[0]
    assert len(sig.header) == 54 and sig.header[1] == "Symbol" and sig.rows[0][1] == "AUBANK"
    assert len(notes) >= 5, "the footer's explanations travel with the numbers"

    data = html_to_xlsx(out)
    z = zipfile.ZipFile(io.BytesIO(data))
    parts = z.namelist()
    for name in parts:
        ET.fromstring(z.read(name))  # every part well-formed
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    assert [s.get("name") for s in wb.findall(".//m:sheet", ns)] == [
        "Signals", "Executions", "Twins that stood aside", "Notes"]
    s1 = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    cells = {c.get("r"): c for c in s1.iter(f"{{{ns['m']}}}c")}
    assert cells["B2"].get("t") == "inlineStr", "text stays text"
    entry_col = sig.header.index("Entry") + 1
    from kotsin_nse.api.export import _col

    entry = cells[f"{_col(entry_col)}2"]
    assert entry.get("t") is None and float(entry.find("m:v", ns).text) == 1000.9, "1,000.90 is a number"
    atr_pct = cells[f"{_col(sig.header.index('ATR%') + 1)}2"]
    assert atr_pct.get("s") == "2" and abs(float(atr_pct.find("m:v", ns).text) - 0.0077) < 1e-12, "0.77% is 0.0077"
    assert s1.find("m:sheetViews/m:sheetView/m:pane", ns).get("state") == "frozen", "the header stays put"


def test_sheet_names_obey_excels_rules():
    from kotsin_nse.api.export import Sheet, _names

    got = _names([Sheet("Signals · 26"), Sheet("a/b:c*d?[e]"), Sheet("x" * 40), Sheet("x" * 40)])
    assert got[0] == "Signals" and all(len(n) <= 31 for n in got)
    assert not any(ch in got[1] for ch in "/:*?[]")
    assert got[2] != got[3], "duplicates are made unique"


async def test_the_temporary_route_serves_the_xlsx_and_both_stop_when_the_ticket_does(settings):
    import httpx

    from kotsin_nse.api.routes import build_app
    from kotsin_nse.engine import Engine

    engine = Engine(settings)
    await engine.ledger.init()
    app = build_app(engine)
    TemporaryPage(settings.data_dir).create(date(2026, 9, 24))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        page = await c.get("/temporary")
        assert page.status_code == 200 and 'href="/temporary.xlsx"' in page.text
        x = await c.get("/temporary.xlsx")
        assert x.status_code == 200
        assert x.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml")
        assert 'attachment; filename="fudkii-day-book-24-september-2026.xlsx"' == x.headers["content-disposition"]
        assert x.content[:2] == b"PK", "a zip, i.e. a real workbook"
        TemporaryPage(settings.data_dir).revoke()
        assert (await c.get("/temporary.xlsx")).status_code == 410, "the download expires with the page"
    await engine.ledger.close()


def test_the_rt_y_ab_compares_the_books_on_the_same_triggers():
    """Operator, 2026-09-26: "decide after about 50 trades but give updates every day"."""
    from kotsin_nse.api.daybook import ab_summary, render_ab

    t0 = 1790566200  # a Monday session
    sig = lambda sid, k: {"signal_id": sid, "strategy": "FUDKII", "symbol": "X", "ts": t0 + k * 1800}  # noqa: E731
    signals = [sig("A", 1), sig("B", 2), sig("C", 3)]
    events = [
        {"kind": "regime.breadth", "signal_id": s, "share": sh} for s, sh in (("A", 0.7), ("B", 0.3), ("C", 0.6))
    ] + [{"kind": "rt_twin.skipped", "signal_id": "B", "book": "FUDKII_RT_Y", "gate": "breadth"}]
    positions = [{"id": f"{b}-{s}", "strategy": b, "signal_id": s} for s in ("A", "B") for b in ("FUDKII_RT_X", "FUDKII_RT_N")]
    positions += [{"id": "FUDKII_RT_Y-A", "strategy": "FUDKII_RT_Y", "signal_id": "A"}]
    trades = [
        {"position_id": "FUDKII_RT_X-A", "strategy": "FUDKII_RT_X", "net": 1000.0},
        {"position_id": "FUDKII_RT_N-A", "strategy": "FUDKII_RT_N", "net": 400.0},
        {"position_id": "FUDKII_RT_Y-A", "strategy": "FUDKII_RT_Y", "net": 700.0},
        {"position_id": "FUDKII_RT_X-B", "strategy": "FUDKII_RT_X", "net": -900.0},
        {"position_id": "FUDKII_RT_N-B", "strategy": "FUDKII_RT_N", "net": -300.0},
    ]
    ab = ab_summary(signals=signals, positions=positions, trades=trades, events=events)
    t = ab["total"]
    assert (t["triggers"], t["y_n"], t["y_win"], t["y_net"]) == (3, 1, 1, 700.0)
    assert (t["x_on_y"], t["n_on_y"]) == (1000.0, 400.0), "the ungated books on RT-Y's own trades"
    assert (t["gated"], t["x_on_gated"], t["n_on_gated"]) == (1, -900.0, -300.0), "what the gate kept RT-Y out of"
    assert "1 of 50 RT-Y trades" in render_ab(ab)
