"""The review's "same option elsewhere" block (operator, 2026-10-09: "add there insights/pnl from another engine or/and
strategy that traded the same OTM at that time and if it made better profit than that startegy on the card. mention its
features/parameters that led to better outcomes"): the peers are the trades on the same contract held at the same time,
every better one's gap is split into parts that add up to it exactly, the lesson is never read into a refusal or a halt,
the twin is read with one GET and its absence costs only its own rows, and nothing the engine trades with reaches it."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from kotsin_nse.api import peer
from kotsin_nse.api import review_peers as rp
from kotsin_nse.api.routes import build_app
from kotsin_nse.config import Settings
from kotsin_nse.engine import Engine
from kotsin_nse.risk import limits as L
from kotsin_nse.strategy.keys import STOP_MIRRORS

T0 = 1_791_517_500.0  # 9 Oct 2026 09:45 IST
SC, NAME = "113227", "TCS 27 OCT 2026 CE 2200.00"
BOOKS = {"FUDKII": L.RiskLimits(max_lots=4, fixed_lots_under_inr=L.FIXED_LOTS_UNDER_INR), "FUDKII_RT_X": L.RT_X_LIMITS,
         "FUDKII_RT_N": L.RT_N_LIMITS, "FUDKII_RT_Y": L.RT_Y_LIMITS, "FUDKII_RT_Y_W1": L.RT_Y_W1_LIMITS, "FUDKII_RT_Y_F": L.RT_Y_F_LIMITS}
for _m, (_s, _r) in STOP_MIRRORS.items():
    if _s.value in BOOKS:
        BOOKS[_m.value] = L.stop_rule_limits(BOOKS[_s.value], _r)


def params_of(book: str) -> dict:
    return rp.params_json(BOOKS.get(book, BOOKS["FUDKII"]))


def leg(kind: str, qty: float, fill: float, ts: float, *, ref: float | None = None, reason: str = "TRAIL", stop: dict | None = None) -> dict:
    return {"kind": kind, "qty": qty, "fillPrice": fill, "ref": fill if ref is None else ref, "filledTs": ts, "reason": reason, "stop": stop}


def trade(pid: str, book: str, *, q: int = 900, e: float = 34.65, r: float | None = None, legs: list[dict], charges: float = 150.0,
          opened: float = T0 + 10, scrip: str = SC, name: str = NAME, note: str = "", reason: str = "TRAIL", lot: int = 225,
          sl0: float = 25.99, sl: float = 25.99, sig: str = "FUDKII-TCS-1791517500-B") -> tuple[dict, dict]:
    """A closed position and its trade row, shaped as the engine stores them."""
    closed = max(x["filledTs"] for x in legs)
    gross = sum(x["qty"] * x["fillPrice"] for x in legs) - q * e
    pos = {"id": pid, "strategy": book, "symbol": "TCS", "scrip_code": scrip, "status": "CLOSED", "qty": q, "qty_remaining": 0, "entry": e,
           "opened_ts": opened, "closed_ts": closed, "direction": "BULLISH", "signal_id": sig, "grade": "C", "note": note,
           "instrument": {"scrip_code": scrip, "name": name, "lot_size": lot, "tick_size": 0.05}, "initial_option_sl": sl0, "option_sl": sl,
           "exit_reason": reason, "exit_price": legs[-1]["fillPrice"],
           "exec_log": {"entry": {"signalTs": opened - 2, "filledTs": opened, "ref": e if r is None else r, "fillPrice": e, "waitS": 2.0,
                                  "reprices": [], "bookAtPlace": {"bid": e - 0.05, "ask": e + 0.05}}, "exits": legs}}
    trd = {"id": f"trd-{pid}", "position_id": pid, "strategy": book, "symbol": name, "underlying": "TCS", "qty": q, "entry": e,
           "gross": gross, "charges": charges, "net": gross - charges, "exit_reason": reason, "opened_ts": opened, "closed_ts": closed,
           "r_multiple": 0.0}
    return pos, trd


def records(*pairs: tuple[dict, dict], engine: str = "phase 35", orders: tuple[dict, ...] = ()) -> list[dict]:
    return rp.build_records([p for p, _ in pairs], [t for _, t in pairs], list(orders), engine=engine, params_of=params_of, now=T0 + 9000)


def parts_of(a: dict, b: dict, every: list[dict]) -> dict:
    sib, src = rp.sibling_maps(every)
    parts, _chain, _note = rp.pair_parts(a, b, sib, src)
    return parts


def gap(a: dict, b: dict) -> float:
    return b["net"] - a["net"]


def exact(parts: dict, a: dict, b: dict) -> None:
    g = gap(a, b)
    assert abs(sum(parts.values()) - g) < 1e-6, (parts, g)
    assert sum(rp.whole_rupees(parts, g).values()) == round(g), "to the rupee, the parts add up to the gap"


TRAIL = [leg("exit", 900, 40.35, T0 + 250, ref=41.10)]


# -- the parts --------------------------------------------------------------------------------------------------------


def test_an_entry_only_difference_is_the_entry_parts():
    a = records(trade("a", "FUDKII_RT_Y", e=34.70, r=34.65, legs=TRAIL), engine="phase 34")[0]
    b = records(trade("b", "FUDKII_RT_Y", e=34.60, r=34.55, legs=TRAIL))[0]
    p = parts_of(a, b, [a, b])
    exact(p, a, b)
    assert p["entry_time"] == pytest.approx(90.0) and p["entry_paid"] == pytest.approx(0.0)
    assert p["exit_rule"] == p["exit_exec"] == p["ladder"] == p["size"] == 0.0


def test_a_size_only_difference_is_the_per_unit_outcome_times_the_extra_qty():
    a = records(trade("a", "FUDKII_RT_Y", legs=TRAIL), engine="phase 34")[0]
    b = records(trade("b", "FUDKII_RT_Y", q=1800, legs=[leg("exit", 1800, 40.35, T0 + 250, ref=41.10)], charges=150.0))[0]
    p = parts_of(a, b, [a, b])
    exact(p, a, b)
    assert p["size"] == pytest.approx(900 * (40.35 - 34.65)) and abs(p["exit_rule"]) < 1e-9


def test_a_different_exit_is_the_exit_decision_and_its_fill():
    a, b = records(trade("a", "FUDKII_RT_X", legs=[leg("exit", 900, 34.40, T0 + 120, ref=34.65, reason="SL-OP", stop={"level": 34.65, "triggerOn": "option mid"})],
                         reason="SL-OP", sl=34.65),
                   trade("b", "FUDKII_RT_Y", legs=TRAIL))
    p = parts_of(a, b, [a, b])
    exact(p, a, b)
    assert p["exit_rule"] == pytest.approx(900 * (41.10 - 34.65)) and p["exit_exec"] == pytest.approx(900 * ((40.35 - 41.10) - (34.40 - 34.65)))
    assert a["anat"]["mech"].startswith("raised stop"), "an option stop at the entry is a raised stop"


def test_the_same_rungs_sold_by_both_leave_the_final_exit_as_the_difference():
    """TCS 9 Oct: both sold T1 36.40 and T2 42.75 (half the qty), then the trail at 41.35 against 40.35 — the ₹450 is the
    final exit on the 450 left, not "rungs −₹450, exit +₹900" (the study's spec splits it that way; the total is the same)."""
    rungs = [leg("target", 225, 36.40, T0 + 90, reason="TARGET"), leg("target", 225, 42.75, T0 + 220, reason="TARGET")]
    a = records(trade("a", "FUDKII_RT_Y", legs=[*rungs, leg("exit", 450, 40.35, T0 + 250, ref=41.10)], charges=177.52))[0]
    b = records(trade("b", "FUDKII_RT_Y", legs=[*rungs, leg("exit", 450, 41.35, T0 + 230, ref=41.50)], charges=178.39), engine="phase 34")[0]
    p = parts_of(a, b, [a, b])
    exact(p, a, b)
    assert abs(p["ladder"]) < 1e-9 and p["exit_rule"] == pytest.approx(450 * 0.40) and p["exit_exec"] == pytest.approx(450 * 0.60)
    assert a["anat"]["D"] + a["anat"]["X"] + a["anat"]["T"] + a["anat"]["rho"] == pytest.approx(a["x"])


def test_charges_alone():
    a, b = records(trade("a", "FUDKII_RT_Y", legs=TRAIL, charges=180.0), trade("b", "FUDKII_RT_Y_W1", legs=TRAIL, charges=150.0))
    p = parts_of(a, b, [a, b])
    exact(p, a, b)
    assert p["charges"] == pytest.approx(30.0) and sum(abs(v) for k, v in p.items() if k != "charges") < 1e-9


def test_a_different_stop_rule_goes_through_this_fills_own_sibling():
    """MANAPPURAM 9 Oct (card_spec.md example 6), to the rupee: phase 34's RT-Y (current) against phase 35's RT-Y-W1 stop-E,
    through phase 34's own RT-Y stop-E."""
    q, e, r = 12000, 6.0, 6.025
    a = trade("a", "FUDKII_RT_Y", q=q, e=e, r=r, lot=3000, legs=[leg("exit", q, 5.86, T0 + 38, ref=5.90, reason="SL-EQ")], charges=214.73, reason="SL-EQ")
    a2 = trade("a2", "FUDKII_RT_Y_SE", q=q, e=e, r=r, lot=3000, legs=[leg("exit", q, 5.64, T0 + 226, ref=5.75, reason="SL-EQ")], charges=209.55,
               reason="SL-EQ", note="stop-rule mirror of a")
    b = trade("b", "FUDKII_RT_Y_W1_SE", q=q, e=e, r=r, lot=3000, charges=287.28, note="stop-rule mirror of pos-b0", legs=[
        leg("target", 3000, 6.30, T0 + 300, reason="TARGET"), leg("exit", 3000, 7.15, T0 + 1281, ref=7.25),
        leg("exit", 3000, 7.20, T0 + 1281, ref=7.275), leg("exit", 3000, 7.20, T0 + 1281, ref=7.275)])
    A = records(a, a2, engine="phase 34")
    B = records(b)
    p = parts_of(A[0], B[0], A + B)
    exact(p, A[0], B[0])
    rupees = rp.whole_rupees(p, gap(A[0], B[0]))
    assert {k: v for k, v in rupees.items() if v} == {"ladder": -2650, "exit_rule": 18200, "exit_exec": 320, "stop_rule": -2640, "charges": -73}
    assert round(gap(A[0], B[0]), 2) == 13157.45


def test_the_fortis_worked_example_to_the_rupee():
    """card_spec.md example 1: RT-X's breakeven at first touch against RT-Y on the same fill."""
    q, e = 3100, 18.95
    x = trade("x", "FUDKII_RT_X", q=q, e=e, lot=775, legs=[leg("exit", q, 18.57, T0 + 331, ref=18.80, reason="SL-OP", stop={"level": 18.95})],
              charges=184.24, reason="SL-OP", sl=18.95)
    y = trade("y", "FUDKII_RT_Y", q=q, e=e, lot=775, charges=216.85,
              legs=[leg("target", 775, 20.20, T0 + 400, reason="TARGET"), leg("exit", 2325, 20.05, T0 + 631, ref=20.15)])
    a, b = records(x, y, engine="phase 34")
    p = parts_of(a, b, [a, b])
    exact(p, a, b)
    assert {k: round(v, 2) for k, v in p.items() if abs(v) > 0.005} == {"ladder": 116.25, "exit_rule": 4185.0, "exit_exec": 403.0, "charges": -32.61}


def _concor() -> tuple[list[dict], list[dict]]:
    """CONCOR 8 Oct: the current rule and its stop-E mirror both had the stock-stop exit REFUSED at 10:18:37 (book 12.2 s old,
    executable 9.85); the current rule then sold on its option stop at 8.85, the mirror held and sold T1 10.95 and the trail 10.45."""
    q, e, t_r = 5000, 10.2, T0 + 2000
    cur = trade("cur", "FUDKII_RT_Y_F", q=q, e=e, r=10.175, lot=1250, charges=155.15, reason="SL-OP",
                legs=[leg("exit", q, 8.85, t_r + 2644, ref=8.80, reason="SL-OP", stop={"level": 9.27, "triggerOn": "option mid"})])
    mir = trade("mir", "FUDKII_RT_Y_F_SE", q=q, e=e, r=10.175, lot=1250, charges=195.31, note="stop-rule mirror of cur",
                legs=[leg("target", 1250, 10.95, t_r + 8098, reason="TARGET"), leg("exit", 3750, 10.45, t_r + 8125, ref=10.50)])

    def refused(pid: str, reason: str) -> dict:
        return {"id": f"o-{pid}", "position_id": pid, "purpose": "EXIT", "status": "REJECTED", "reason": reason, "ts": t_r + 0.2,
                "note": "book for CONCOR is 12198 ms old (limit 6000)",
                "exec": {"signalTs": t_r, "ref": 9.80, "bookAtPlace": {"bid": 9.85, "ask": 10.5},
                         "stop": {"level": 444.25, "triggerOn": "underlying", "triggerTs": t_r, "executable": 9.85}}}
    orders = (refused("cur", "underlying confirmed the breach at 444.30 — no grace"), refused("mir", "stock 444.30 through its stop 444.25"))
    recs = records(cur, mir, orders=orders, engine="phase 34")
    return recs, list(orders)


def test_a_win_that_came_from_a_refused_paper_exit_is_a_paper_artefact():
    (a, b), _ = _concor()
    p = parts_of(a, b, [a, b])
    exact(p, a, b)
    assert p["refusal"] == pytest.approx(8625.0) and round(p["charges"], 2) == -40.16
    assert a["anat"]["refused"]["kind"] == "SL-EQ" and a["anat"]["refused"]["ageMs"] == 12198.0
    les = rp.lesson(a, b, p, gap(a, b), rp.load_evidence(None))
    assert les["key"] == "artefact_refusal" and les["grade"] == rp.GRADE_ONE_OFF and les["text"].startswith("No lesson")


def test_a_halt_is_never_a_lesson():
    a, b = records(trade("a", "FUDKII_RT_Y", legs=[leg("exit", 900, 30.0, T0 + 5000, reason="SL-OP")], reason="SL-OP"),
                   trade("b", "FUDKII_RT_N", legs=[leg("exit", 900, 33.0, T0 + 4000, reason="HALT")], reason="HALT"))
    p = parts_of(a, b, [a, b])
    les = rp.lesson(a, b, p, gap(a, b), rp.load_evidence(None))
    assert les["key"] == "artefact_halt" and les["grade"] == rp.GRADE_ONE_OFF


def test_the_lesson_names_the_systematic_difference_only_where_the_pooled_record_holds_it():
    """RT-X → RT-Y on the same fill is one of the two contrasts the study found in both windows and both engines (L1: RT-X →
    RT-Y 12 better, 3 worse, and RT-N → RT-Y): it is graded systematic and names every parameter in the set; what decided
    this pair keeps its own, weaker grade."""
    a, b = records(trade("a", "FUDKII_RT_X", legs=[leg("exit", 900, 34.40, T0 + 120, ref=34.65, reason="SL-OP", stop={"level": 34.65})],
                         reason="SL-OP", sl=34.65),
                   trade("b", "FUDKII_RT_Y", legs=TRAIL))
    p = parts_of(a, b, [a, b])
    les = rp.lesson(a, b, p, gap(a, b), rp.load_evidence(None))
    assert les["grade"] == rp.GRADE_SYSTEMATIC and les["systematic"]["n"] == 15
    assert {d["key"] for d in les["systematic"]["params"]} >= {"arm_at_pct", "sl_lag", "post_arm_sustain", "band_exit", "max_premium_loss_pct"}
    assert les["decided"]["what"].startswith("RT-X's raised stop") and les["decided"]["grade"] != rp.GRADE_SYSTEMATIC
    # the same books the other way round: the pooled record points against this card, so no lesson is graded systematic
    c, d = records(trade("c", "FUDKII_RT_Y", legs=[leg("exit", 900, 34.40, T0 + 120, reason="SL-OP")], reason="SL-OP"),
                   trade("d", "FUDKII_RT_X", legs=TRAIL))
    les2 = rp.lesson(c, d, parts_of(c, d, [c, d]), gap(c, d), rp.load_evidence(None))
    assert les2["systematic"] is None and les2["grade"] != rp.GRADE_SYSTEMATIC


def test_a_stop_rule_or_an_engine_difference_is_graded_on_its_own_contrast():
    a, b = records(trade("a", "FUDKII_RT_Y", legs=[leg("exit", 900, 30.0, T0 + 900, reason="SL-OP")], reason="SL-OP"),
                   trade("b", "FUDKII_RT_Y_SE", legs=TRAIL, note="stop-rule mirror of a"))
    les = rp.lesson(a, b, parts_of(a, b, [a, b]), gap(a, b), rp.load_evidence(None))
    assert les["key"] == "stop_rule" and "RT-Y stop-E did better on 6" in les["decided"]["text"] and les["grade"] == rp.GRADE_ONE_OFF
    c = records(trade("c", "FUDKII_RT_Y", legs=TRAIL), engine="phase 34")[0]
    d = records(trade("d", "FUDKII_RT_Y", legs=[leg("exit", 900, 41.35, T0 + 230, ref=41.50)]), engine="phase 34")[0]
    e_ = records(trade("e", "FUDKII_RT_Y", legs=TRAIL))[0]
    les2 = rp.lesson(e_, d, parts_of(e_, d, [c, d, e_]), gap(e_, d), rp.load_evidence(None))
    assert les2["key"] == "engine" and les2["grade"] == rp.GRADE_ONE_OFF and "feed timing" in les2["text"]


# -- the peers ---------------------------------------------------------------------------------------------------------


def test_peers_are_the_same_contract_held_at_the_same_time_and_nothing_else():
    me = trade("me", "FUDKII_RT_Y", legs=TRAIL)
    same = trade("same", "FUDKII_RT_X", legs=[leg("exit", 900, 36.0, T0 + 200)])
    later = trade("later", "FUDKII_RT_X", opened=T0 + 600, legs=[leg("exit", 900, 36.0, T0 + 900)])
    strike = trade("strike", "FUDKII_RT_X", scrip="113230", name="TCS 27 OCT 2026 CE 2220.00", legs=[leg("exit", 900, 36.0, T0 + 200)])
    expiry = trade("expiry", "FUDKII_RT_X", scrip="120001", name="TCS 24 NOV 2026 CE 2200.00", legs=[leg("exit", 900, 36.0, T0 + 200)])
    recs = records(me, same, later, strike, expiry)
    got = rp.peers_of(recs[0], recs, T0 + 9000)
    assert [r["positionId"] for r in got] == ["same"], "the same scrip code, overlapping; not a later trade, another strike or expiry"


def test_the_block_groups_identical_outcomes_and_explains_every_better_one():
    me = trade("me", "FUDKII_RT_X", legs=[leg("exit", 900, 34.40, T0 + 120, ref=34.65, reason="SL-OP", stop={"level": 34.65})], reason="SL-OP", sl=34.65)
    y = trade("y", "FUDKII_RT_Y", legs=TRAIL)
    w1 = trade("w1", "FUDKII_RT_Y_W1", legs=TRAIL, note="shadow of y")
    worse = trade("worse", "FUDKII_RT_N", legs=[leg("exit", 900, 30.0, T0 + 900, reason="SL-OP")], reason="SL-OP")
    recs = records(me, y, w1, worse)
    blk = rp.peer_blocks([{"id": "me"}], recs, engine="phase 35", now=T0 + 9000, ev=rp.load_evidence(None))["me"]
    assert blk["class"] == "peer won where this lost" and blk["n"] == 3 and blk["betterExists"]
    assert len(blk["better"]) == 1 and {m["label"] for m in blk["better"][0]["members"]} == {"RT-Y", "RT-Y-W1"}
    g = blk["better"][0]
    assert sum(g["rupees"].values()) == g["gapRupees"] and g["lines"][0]["key"] == "exit_rule"
    assert g["exits"]["this"]["mech"].startswith("raised stop") and g["exits"]["peer"]["mech"] == "trail / give-back"


def test_an_open_trade_and_a_trade_with_no_peer_say_so():
    me = trade("me", "FUDKII_RT_Y", legs=TRAIL)
    recs = records(me)
    assert rp.peer_blocks([{"id": "me"}], recs, engine="phase 35", now=T0, ev=rp.load_evidence(None))["me"]["class"] == "no peer"
    op, _ = trade("op", "FUDKII_RT_X", legs=TRAIL)
    op.update(status="OPEN", closed_ts=None)
    recs = rp.build_records([me[0], op], [me[1]], [], engine="phase 35", params_of=params_of, now=T0 + 100)
    blk = rp.peer_blocks([{"id": "me"}], recs, engine="phase 35", now=T0 + 100, ev=rp.load_evidence(None))["me"]
    assert blk["class"] == "open" and blk["rows"][0]["status"] == "OPEN" and not blk["better"]


def test_a_trade_before_the_exit_log_has_one_synthesised_leg():
    pos, trd = trade("old", "FUDKII", legs=TRAIL)
    pos["exec_log"] = {}
    r = records((pos, trd))[0]
    assert r["audit"] is False and r["anat"]["D"] == pytest.approx(r["x"]) and r["anat"]["X"] == 0.0


# -- the route: the twin read, the filters' parameters, read-only ------------------------------------------------------


def _settings(tmp_path: Path, name: str) -> Settings:
    d = tmp_path / name
    d.mkdir()
    return Settings(_env_file=None, data_dir=d, db_url=f"sqlite+aiosqlite:///{d}/t.db", engine_enabled=False)


async def _app(tmp_path: Path, name: str, ident: dict, rows: list[tuple[dict, dict]], orders: tuple[dict, ...] = ()):
    s = _settings(tmp_path, name)
    (s.data_dir / peer.FILE).write_text(json.dumps(ident))
    e = Engine(s)
    await e.ledger.init()
    for p, t in rows:
        await e.ledger.upsert_position(p)
        await e.ledger.insert_trade(t)
    for o in orders:
        await e.ledger.insert_order({"client_order_id": o["id"], "strategy": "FUDKII_RT_Y_F", "symbol": "CONCOR", "scrip_code": SC, "mode": "PAPER", **o}, "TEST")
    return e, build_app(e)


A_ID = {"name": "phase 34", "peer": {"name": "phase 35", "url": "http://127.0.0.1:8501"}}
B_ID = {"name": "phase 35", "peer": {"name": "phase 34", "url": "http://127.0.0.1:8500"}}


def _b_rows() -> list[tuple[dict, dict]]:
    return [trade("b-y", "FUDKII_RT_Y", legs=[*[leg("target", 225, 36.40, T0 + 90, reason="TARGET"), leg("target", 225, 42.75, T0 + 220, reason="TARGET")],
                                             leg("exit", 450, 40.35, T0 + 250, ref=41.10)], charges=177.52),
            trade("b-y-se", "FUDKII_RT_Y_SE", note="stop-rule mirror of b-y", charges=177.52,
                  legs=[leg("target", 225, 36.40, T0 + 90, reason="TARGET"), leg("target", 225, 42.75, T0 + 220, reason="TARGET"),
                        leg("exit", 450, 40.35, T0 + 250, ref=41.10)])]


def _a_rows() -> list[tuple[dict, dict]]:
    return [trade("a-y", "FUDKII_RT_Y", legs=[leg("target", 225, 36.40, T0 + 90, reason="TARGET"), leg("target", 225, 42.75, T0 + 220, reason="TARGET"),
                                             leg("exit", 450, 41.35, T0 + 230, ref=41.50)], charges=178.39),
            # a copy of one of phase 35's rows (phase 35's 22 Sep - 1 Oct ledger is phase 34's): never a peer of itself
            trade("b-y-se", "FUDKII_RT_Y_SE", note="stop-rule mirror of b-y", legs=[leg("exit", 900, 30.0, T0 + 600)])]


async def _counts(e: Engine) -> dict:
    return await e.ledger.counts()


@pytest.mark.asyncio
async def test_the_review_reads_the_twin_once_and_compares_both_engines(tmp_path, monkeypatch):
    ea, a = await _app(tmp_path, "a", A_ID, _a_rows())
    eb, b = await _app(tmp_path, "b", B_ID, _b_rows())
    seen: list[httpx.Request] = []

    class Twin(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request)
            assert request.method == "GET" and request.url.host == "127.0.0.1" and request.url.port == 8500
            return await httpx.ASGITransport(app=a).handle_async_request(request)

    monkeypatch.setattr(peer, "_client", lambda: httpx.AsyncClient(transport=Twin(), timeout=5))
    before = (await _counts(ea), await _counts(eb))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=b), base_url="http://t") as c:
        r = (await c.get("/api/review", params={"day": "2026-10-09", "mirrors": 1, "peers": 1})).json()
    assert [x.url.path for x in seen] == ["/api/review/legs"], "one GET of the twin's records; the twin reads no twin"
    by = {t["id"]: t for t in r["trades"]}
    assert set(by) == {"b-y", "b-y-se"} and by["b-y-se"]["mirror"] and by["b-y-se"]["variant"] == "E" and not by["b-y"]["mirror"]
    assert r["summary"]["trades"] == 1, "a twin re-trades its source's fill: it is a card, never counted"
    blk = by["b-y"]["peers"]
    engines = {(x["engine"], x["positionId"]) for x in blk["rows"]}
    assert ("phase 34", "a-y") in engines and ("phase 34", "b-y-se") not in engines, "the twin's copy of this engine's row is dropped"
    g = blk["better"][0]
    assert g["peer"]["engine"] == "phase 34" and g["gapRupees"] == round(g["gap"]) == sum(g["rupees"].values())
    assert g["rupees"]["ladder"] == 0 and g["rupees"]["exit_rule"] == 180 and g["rupees"]["exit_exec"] == 270
    assert by["b-y"]["directionRead"]["label"]["text"], "the direction block rides on every card"
    assert by["b-y"]["direction"] == "BULLISH", "the trade's own direction is kept"
    assert (await _counts(ea), await _counts(eb)) == before, "record-only: nothing written on either engine"


@pytest.mark.asyncio
async def test_a_twin_that_is_down_or_older_costs_only_its_own_rows(tmp_path, monkeypatch):
    _eb, b = await _app(tmp_path, "b", B_ID, _b_rows())

    class Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

    class Old(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Not Found"})

    for transport, words in ((Down(), "is not answering"), (Old(), "runs code without the same-option read")):
        monkeypatch.setattr(peer, "_client", lambda t=transport: httpx.AsyncClient(transport=t, timeout=5))
        _eb, b = await _app(tmp_path, f"b-{words[:4]}", B_ID, _b_rows())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=b), base_url="http://t") as c:
            r = await c.get("/api/review", params={"day": "2026-10-09", "mirrors": 1, "peers": 1})
        assert r.status_code == 200
        body = r.json()
        assert any(words in n for n in body["notes"]) and body["twin"]["read"] is False
        y = next(t for t in body["trades"] if t["id"] == "b-y")
        assert [x["positionId"] for x in y["peers"]["rows"]] == ["b-y-se"], "this engine's own peers are still shown"


@pytest.mark.asyncio
async def test_the_legs_read_never_reaches_a_twin_and_the_day_range_is_checked(tmp_path, monkeypatch):
    _eb, b = await _app(tmp_path, "b", B_ID, _b_rows())

    def no_twin():
        raise AssertionError("the legs read must never read a twin")

    monkeypatch.setattr(peer, "_client", no_twin)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=b), base_url="http://t") as c:
        legs = (await c.get("/api/review/legs", params={"day": "2026-10-09"})).json()
        assert legs["engine"] == "phase 35" and {x["positionId"] for x in legs["records"]} == {"b-y", "b-y-se"}
        assert legs["records"][0]["params"]["arm_at_pct"] == 5.0, "each book's parameters as the engine holds them"
        old = (await c.get("/api/review", params={"day": "2026-10-09"})).json()
        assert "peers" not in old["trades"][0] and [t["id"] for t in old["trades"]] == ["b-y"], "without the flags: the review as it was"
        two = (await c.get("/api/review", params={"day": "2026-10-08", "to": "2026-10-09"})).json()
        assert two["days"] == ["2026-10-08", "2026-10-09"] and two["summary"]["trades"] == 1
        assert (await c.get("/api/review", params={"day": "2026-10-09", "to": "2026-10-08"})).status_code == 400
        assert (await c.get("/api/review", params={"day": "2026-09-01", "to": "2026-10-09"})).status_code == 400


# -- record-only: nothing in the order path reaches it -----------------------------------------------------------------

PKG = Path(__file__).resolve().parents[1] / "kotsin_nse"
NEW = ("review_peers", "review_direction", "review_evidence")


def _imports_new(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").endswith(NEW) or any(a.name in NEW for a in node.names):
                return True
        elif isinstance(node, ast.Import) and any(a.name.endswith(NEW) for a in node.names):
            return True
    return False


def test_only_the_api_reads_the_same_option_block():
    importers = {p.relative_to(PKG).as_posix() for p in PKG.rglob("*.py") if p.stem not in NEW and _imports_new(p)}
    assert importers == {"api/routes.py"}, importers


def test_the_order_path_never_loads_the_review_reads():
    code = ("import sys, kotsin_nse.engine, kotsin_nse.exec.gateway, kotsin_nse.exec.paper, kotsin_nse.exec.resting, kotsin_nse.risk.exits; "
            "print(any(m.startswith('kotsin_nse.api.review') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=PKG.parent).stdout.split()
    assert out == ["False"], out


# -- the 10 Oct review's fixes (numbers, safety, UI lenses) ---------------------------------------------------------------


def test_only_the_l1_contrasts_are_systematic_and_fudkii_to_w1_is_not():
    """lessons.md §4: only L1 (RT-Y's exits over RT-X's and RT-N's) holds in both windows. FUDKII → RT-Y-W1 was graded
    systematic on a 3/3 tie and −₹13,507 to 1 Oct: each window must now lean the card's way in cases and in rupees."""
    ev = rp.load_evidence(None)
    systematic = {(con, says) for con in ev["contrasts"] for says in (True, False)
                  if rp.contrast_evidence(ev, con, says)["grade"] == rp.GRADE_SYSTEMATIC}
    assert systematic == {("RT-X -> RT-Y", True), ("RT-N -> RT-Y", True)}
    w1 = rp.contrast_evidence(ev, "FUDKII -> RT-Y-W1", True)
    assert w1["grade"] == rp.GRADE_SUGGESTIVE and w1["strength"].startswith("consistent in 5–9 Oct only")
    a, b = records(trade("a", "FUDKII", legs=[leg("exit", 900, 35.0, T0 + 200, ref=35.10, reason="SL-OP", stop={"level": 35.10})],
                         reason="SL-OP", sl=35.10),
                   trade("b", "FUDKII_RT_Y_W1", legs=[leg("exit", 900, 42.0, T0 + 3000, ref=42.10)]))
    les = rp.lesson(a, b, parts_of(a, b, [a, b]), gap(a, b), ev)
    assert les["systematic"] is None and les["grade"] != rp.GRADE_SYSTEMATIC


def test_a_pair_against_the_record_of_what_decided_it_is_not_promoted():
    """TCS / INDIGO RT-N: RT-N's give-back sold first and RT-Y did better — the pooled record says the early give-back is
    usually right (7 of 8), so this card is the exception; RT-N → RT-Y's systematic line is shown as context only."""
    a, b = records(trade("a", "FUDKII_RT_N", legs=[leg("exit", 900, 36.0, T0 + 900, ref=36.10, reason="TRAIL")]),
                   trade("b", "FUDKII_RT_Y", legs=[leg("exit", 900, 41.0, T0 + 4000, ref=41.10)]))
    les = rp.lesson(a, b, parts_of(a, b, [a, b]), gap(a, b), rp.load_evidence(None))
    assert les["systematic"] is not None and les["systematic"]["grade"] == rp.GRADE_SYSTEMATIC
    assert les["decided"]["strength"] == rp.EXCEPTION and les["grade"] == rp.GRADE_ONE_OFF


def _paytm(engine_b: str = "phase 35") -> list[dict]:
    """PAYTM 8 Oct 1500PE: RT-X's current rule held to a late option stop; its stop-E mirror's 25 % premium cap on the bid
    sold at once (lessons.md L5a)."""
    q, e = 2900, 24.70
    cur = trade("cur", "FUDKII_RT_X", q=q, e=e, lot=725, reason="SL-OP", charges=120.0,
                legs=[leg("exit", q, 13.90, T0 + 5400, ref=14.00, reason="SL-OP", stop={"level": 14.10, "triggerOn": "option mid"})])
    mir = trade("mir", "FUDKII_RT_X_SE", q=q, e=e, lot=725, reason="SL-OP", charges=145.0, note="stop-rule mirror of cur",
                legs=[leg("exit", q, 18.52, T0 + 600, ref=18.52, reason="SL-OP", stop={"level": 18.525, "triggerOn": "option bid"})])
    return records(cur, mir, engine=engine_b)


def test_the_bid_cap_is_named_and_graded_on_its_own_pooled_record():
    """The stop-E mirror's 25 % premium cap on the bid decided it: the card names the cap as a parameter (off under the
    option stop, 25 % under stop E) and grades it on L5a's 9 copies (FUDKII and RT-X, 9 of 9 for the cap): suggestive."""
    a, b = _paytm()
    assert b["anat"]["mech"] == rp.CAP_MECH
    p = parts_of(a, b, [a, b])
    exact(p, a, b)
    les = rp.lesson(a, b, p, gap(a, b), rp.load_evidence(None))
    assert les["key"] == "stop_rule" and les["decided"]["what"] == "the stock-stop rules' 25 % bid cap firing first"
    assert les["grade"] == rp.GRADE_SUGGESTIVE and les["decided"]["n"] == 9 and "9 times and holding 0" in les["decided"]["text"]
    assert {d["key"]: (d["this"], d["peer"]) for d in les["decided"]["params"]}["bid_cap_pct"] == (
        "off (the option stop rules)", "25 % under paid, sold at once")
    assert les["also"] is not None and les["also"]["what"].startswith("RT-X vs RT-X stop-E")


def test_the_other_engines_copy_under_another_rule_is_a_stop_rule_lesson():
    """PGEL 8 Oct: phase 34's FUDKII against phase 35's FUDKII stop-E is 100 % stop rule (through phase 34's own stop-E):
    a stop-rule lesson, not a first-out one."""
    a = records(trade("a", "FUDKII", legs=[leg("exit", 900, 30.0, T0 + 3000, ref=30.1, reason="SL-EQ")], reason="SL-EQ"), engine="phase 34")[0]
    a2 = records(trade("a2", "FUDKII_SE", note="stop-rule mirror of a", reason="SL-OP",
                       legs=[leg("exit", 900, 31.0, T0 + 600, ref=31.0, reason="SL-OP", stop={"level": 26.0, "triggerOn": "option bid"})]),
                 engine="phase 34")[0]
    b = records(trade("b", "FUDKII_SE", note="stop-rule mirror of pos-b0", reason="SL-OP",
                      legs=[leg("exit", 900, 31.0, T0 + 600, ref=31.0, reason="SL-OP", stop={"level": 26.0, "triggerOn": "option bid"})]))[0]
    p = parts_of(a, b, [a, a2, b])
    exact(p, a, b)
    les = rp.lesson(a, b, p, gap(a, b), rp.load_evidence(None))
    assert les["key"] == "stop_rule" and "the other engine's copy" in les["decided"]["text"] and les["grade"] == rp.GRADE_SUGGESTIVE


def test_the_engines_stops_set_at_the_fill_come_before_the_re_projection():
    """AMBER 8 Oct: the two engines' RT-Y plans differed at the fill (stock stops 6,723.8 against 6,821.15, option stops
    134.12 against 125.93); phase 34's re-projection only moved its line from 134.12 to 142.01."""
    pa, ta = trade("a", "FUDKII_RT_Y", q=400, e=167.9, lot=100, sl0=134.12, sl=142.01, reason="SL-OP",
                   legs=[leg("exit", 400, 135.88, T0 + 1200, ref=138.30, reason="SL-OP", stop={"level": 142.01, "triggerOn": "option mid"})])
    pb, tb = trade("b", "FUDKII_RT_Y", q=400, e=167.9, lot=100, sl0=125.93, sl=125.93, reason="SL-OP",
                   legs=[leg("exit", 400, 123.19, T0 + 1900, ref=124.40, reason="SL-OP", stop={"level": 125.93, "triggerOn": "option mid"})])
    pa["equity_sl"], pb["equity_sl"] = 6723.8, 6821.15
    a = records((pa, ta), engine="phase 34")[0]
    b = records((pb, tb), engine="phase 35")[0]
    les = rp.lesson(b, a, parts_of(b, a, [a, b]), gap(b, a), rp.load_evidence(None))
    assert les["key"] == "engine" and les["grade"] == rp.GRADE_ONE_OFF
    t = les["decided"]["text"]
    assert "differed from the fill" in t and "134.12" in t and "125.93" in t and "6723.8" in t and "142.01" in t
    assert "The one rule the engines run differently decided it" not in t


def test_a_cards_own_refused_exit_makes_its_result_a_paper_artefact():
    """CONCOR 8 Oct RT-Y-F stop-E: +₹1,680 on the card, about −₹1,945 had the refused 10:18:37 exit filled at 9.85."""
    (cur, mir), _ = _concor()
    s = rp.self_refusal(mir)
    assert s["artefact"] and round(s["effect"]) == 3625 and round(s["netIfFilled"], 2) == -1945.31
    assert "paper artefact" in s["text"] and "−₹1,945" in s["text"]
    blk = rp.peer_blocks([{"id": "mir"}], [cur, mir], engine="phase 34", now=T0 + 99999, ev=rp.load_evidence(None))["mir"]
    assert blk["selfRefusal"]["artefact"] and blk["class"] == "best or within threshold"
    plain = records(trade("p", "FUDKII_RT_Y", legs=TRAIL))[0]
    assert rp.self_refusal(plain) is None


def test_the_headline_names_the_peer_the_group_names():
    """LTF 9 Oct: phase 35's RT-N and RT-Y left identically; the headline, the chip and the first group all name RT-Y,
    the one closest to this RT-Y trade (the study's tie order put RT-N first)."""
    me = records(trade("me", "FUDKII_RT_Y", legs=[leg("exit", 900, 30.0, T0 + 9000, ref=30.1, reason="SL-OP")], reason="SL-OP"),
                 engine="phase 34")[0]
    out = [trade(pid, book, legs=[leg("exit", 900, 33.0, T0 + 4000, ref=33.0)]) for pid, book in (("n", "FUDKII_RT_N"), ("y", "FUDKII_RT_Y"))]
    them = records(*out)
    blk = rp.peer_blocks([{"id": "me"}], [me, *them], engine="phase 34", now=T0 + 99999, ev=rp.load_evidence(None))["me"]
    assert blk["headline"].startswith("Phase 35's RT-Y made") and blk["better"][0]["peer"]["label"] == "RT-Y" and blk["best"]["label"] == "RT-Y"


def test_a_twin_not_read_qualifies_every_no_one_did_better_line():
    me = records(trade("me", "FUDKII_RT_Y", legs=TRAIL))[0]
    alone = rp.peer_blocks([{"id": "me"}], [me], engine="phase 35", now=T0 + 99999, ev=rp.load_evidence(None), unread="phase 34")["me"]
    assert alone["headline"] == "No other book held this option at the same time (this engine only: phase 34 was not read)."
    worse = records(trade("w", "FUDKII_RT_X", legs=[leg("exit", 900, 30.0, T0 + 900)]))[0]
    blk = rp.peer_blocks([{"id": "me"}], [me, worse], engine="phase 35", now=T0 + 99999, ev=rp.load_evidence(None), unread="phase 34")["me"]
    assert blk["headline"].startswith("No book did meaningfully better on this option (this engine only: phase 34 was not read)")
    assert blk["scope"] == "this engine only: phase 34 was not read"


def test_a_final_exit_sold_in_legs_of_different_reasons_says_so_and_charges_show_paise_when_the_rupees_match():
    rungs = [leg("target", 225, 36.40, T0 + 90, reason="TARGET")]
    a = records(trade("a", "FUDKII_RT_Y", legs=[*rungs, leg("exit", 675, 40.35, T0 + 250, ref=41.10)], charges=177.52))[0]
    b = records(trade("b", "FUDKII_RT_Y_W1", charges=177.91, legs=[*rungs, leg("exit", 225, 43.0, T0 + 900, ref=43.0, reason="TARGET"),
                                                                    leg("exit", 450, 42.0, T0 + 900, ref=42.2, reason="TRAIL")]))[0]
    p = parts_of(a, b, [a, b])
    line = rp.part_line("exit_rule", p["exit_rule"], a, b, None)
    assert "its final legs: TARGET 225 at 43 (decision 43), TRAIL 450 at 42 (decision 42.2)" in line
    assert rp.part_line("charges", p["charges"], a, b, None).endswith("₹177.52 here vs ₹177.91")


def test_a_refused_exits_reason_is_cut_at_a_word_with_an_ellipsis():
    long = "underlying confirmed the breach at 444.30 — no grace; " * 5
    assert rp._clip(long).endswith("…") and len(rp._clip(long)) <= 161 and rp._clip("short") == "short"


def test_a_record_of_another_shape_is_named_not_compared():
    (cur, mir), _ = _concor()
    assert rp.record_problem(cur) is None and rp.record_problem(mir) is None
    missing = {k: v for k, v in mir.items() if k != "mirrorOf"}
    as_text = {**mir, "closedTs": "10:18"}
    flag = {**mir, "q": True}
    good, bad = rp.checked_records([cur, missing, as_text, flag, "nope", {**mir, "anat": {**mir["anat"], "final": {}}}])
    assert good == [cur] and len(bad) == 5 and bad[0].startswith("mir: mirrorOf is missing")


@pytest.mark.asyncio
async def test_twin_records_of_another_shape_cost_only_themselves(tmp_path, monkeypatch):
    """Safety review, 10 Oct: a twin record missing ``mirrorOf`` or with ``closedTs`` as text gave HTTP 500 and none of
    this engine's cards (and was cached for 10 min). Now each is checked: a bad one is left out with a note, the good
    ones are still compared; and should the comparison itself fail on the twin's records, the page falls back to this
    engine's own."""
    _ea, a = await _app(tmp_path, "a", A_ID, _a_rows())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url="http://t") as c:
        legs = (await c.get("/api/review/legs", params={"day": "2026-10-09"})).json()
    good = next(r for r in legs["records"] if r["positionId"] == "a-y")
    broken = {**{k: v for k, v in good.items() if k != "mirrorOf"}, "positionId": "a-x"}
    texty = {**good, "positionId": "a-z", "closedTs": "09:20"}

    class Twin(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"engine": "phase 34", "records": [good, broken, texty]})

    monkeypatch.setattr(peer, "_client", lambda: httpx.AsyncClient(transport=Twin(), timeout=5))
    _eb, b = await _app(tmp_path, "b", B_ID, _b_rows())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=b), base_url="http://t") as c:
        r = await c.get("/api/review", params={"day": "2026-10-09", "mirrors": 1, "peers": 1})
        assert r.status_code == 200
        body = r.json()
        assert any("2 of phase 34's records are of a shape this code does not read" in n for n in body["notes"])
        y = next(t for t in body["trades"] if t["id"] == "b-y")
        assert {x["positionId"] for x in y["peers"]["rows"]} == {"b-y-se", "a-y"}, "the good twin record is still compared"

        def boom(*_a, **_k):
            raise KeyError("mirrorOf")

        real = rp.peer_blocks
        monkeypatch.setattr(rp, "peer_blocks", lambda cards, recs, **kw: boom() if any(x["engine"] == "phase 34" for x in recs) else real(cards, recs, **kw))
        r = await c.get("/api/review", params={"day": "2026-10-08", "to": "2026-10-09", "mirrors": 1, "peers": 1})
        assert r.status_code == 200
        body = r.json()
        assert any("records could not be compared (KeyError)" in n for n in body["notes"]) and body["twin"]["read"] is False
        y = next(t for t in body["trades"] if t["id"] == "b-y")
        assert [x["positionId"] for x in y["peers"]["rows"]] == ["b-y-se"] and "this engine only" in y["peers"]["scope"]


def test_a_huge_day_span_is_refused_before_a_date_list_is_built():
    from fastapi import HTTPException

    from kotsin_nse.api.routes import _review_days

    t0 = time.perf_counter()
    with pytest.raises(HTTPException) as e:
        _review_days("0001-01-01", "9999-12-31")
    assert e.value.status_code == 400 and time.perf_counter() - t0 < 0.05
    assert len(_review_days("2026-09-26", "2026-10-11")) == 10, "16 calendar days, 10 sessions: still allowed"


@pytest.mark.asyncio
async def test_a_past_days_bars_are_kept_only_from_a_whole_read(tmp_path):
    """Safety review, 10 Oct: a read that raced the midnight compaction got 0 bars and kept them for the life of the
    process. A file that cannot be read now makes the read not whole and nothing of it is kept; a temp file (phase 35's
    compaction, phase 34's rewrite of the day file) is never read as a day."""
    import pandas as pd

    from kotsin_nse.api.routes import ReviewReads

    s = _settings(tmp_path, "bars")
    e = Engine(s)
    d = tmp_path / "bars" / "archive" / "bars"
    d.mkdir(parents=True)
    day = "2026-10-08"
    rows = pd.DataFrame([{"symbol": "TCS", "ts": T0 - 86_400 + 60 * i, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10.0} for i in range(5)])
    rows.to_parquet(d / f"{day}.part-0001.parquet", index=False)
    (d / f"{day}.part-0002.parquet").write_bytes(b"half written")
    (d / f"{day}.tmp.parquet").write_bytes(b"the compaction's temp file")
    reads = ReviewReads(e)
    from datetime import date as _date
    got = await reads.bars({"TCS"}, _date(2026, 10, 8))
    assert got == {"TCS": []} and not reads.past_bars[_date(2026, 10, 8)], "a broken read keeps nothing"
    (d / f"{day}.part-0002.parquet").unlink()
    got = await reads.bars({"TCS"}, _date(2026, 10, 8))
    assert len(got["TCS"]) == 5 and len(reads.past_bars[_date(2026, 10, 8)]["TCS"]) == 5, "read again, whole, and kept"
    # phase 34's layout: the day file alone, rewritten through its temp name
    rows.assign(ts=rows.ts - 86_400).to_parquet(d / "2026-10-07.parquet", index=False)
    (d / "2026-10-07.tmp.parquet").write_bytes(b"being rewritten")
    assert len((await reads.bars({"TCS"}, _date(2026, 10, 7)))["TCS"]) == 5


@pytest.mark.asyncio
async def test_old_twin_reads_are_dropped(tmp_path, monkeypatch):
    from datetime import date as _date

    from kotsin_nse.api import routes

    _eb, _b = await _app(tmp_path, "b", B_ID, _b_rows())
    reads = routes.ReviewReads(_eb)
    old = time.time() - routes.TWIN_TTL_PAST_S - 1
    for i in range(20):
        reads.twin_reads[(f"2026-09-{i + 1:02d}",)] = (old, [], {"read": True})

    class Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(peer, "_client", lambda: httpx.AsyncClient(transport=Down(), timeout=5))
    await reads.twin([_date(2026, 10, 9)])
    assert list(reads.twin_reads) == [("2026-10-09",)]


@pytest.mark.asyncio
async def test_a_past_range_is_encoded_once_and_sent_again(tmp_path, monkeypatch):
    _eb, b = await _app(tmp_path, "b", {"name": "phase 35"}, _b_rows())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=b), base_url="http://t") as c:
        first = await c.get("/api/review", params={"day": "2026-10-09", "peers": 1})
        monkeypatch.setattr(rp, "peer_blocks", lambda *a, **k: (_ for _ in ()).throw(AssertionError("not computed again")))
        again = await c.get("/api/review", params={"day": "2026-10-09", "peers": 1})
    assert first.status_code == again.status_code == 200 and first.content == again.content
    assert first.headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_a_shadow_books_twins_go_with_it(tmp_path):
    """UI review, 10 Oct: "shadows hidden" left RT-Y-W1's stop-E and adaptive twins on screen. Each card now says whether
    the book whose fill it is is a shadow (``sourceShadow``), as the Options page does."""
    rows = [trade("w", "FUDKII_RT_Y_W1", legs=TRAIL), trade("w-se", "FUDKII_RT_Y_W1_SE", note="stop-rule mirror of w", legs=TRAIL),
            trade("y", "FUDKII_RT_Y", legs=TRAIL), trade("y-se", "FUDKII_RT_Y_SE", note="stop-rule mirror of y", legs=TRAIL)]
    _eb, b = await _app(tmp_path, "b", {"name": "phase 35"}, rows)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=b), base_url="http://t") as c:
        body = (await c.get("/api/review", params={"day": "2026-10-09", "mirrors": 1})).json()
    by = {t["id"]: (t["shadow"], t["sourceShadow"], t["mirror"]) for t in body["trades"]}
    assert by == {"w": (True, True, False), "w-se": (False, True, True), "y": (False, False, False), "y-se": (False, False, True)}


#: the card as the review sent it before 9 Oct (75a0e1e ``review_trade``), and what 9-10 Oct added to it
OLD_CARD_KEYS = {"id", "book", "label", "symbol", "contract", "direction", "grade", "counterTrend", "status", "qty", "entry", "exit",
                 "exitReason", "openedTs", "closedTs", "signalTs", "net", "gross", "charges", "r", "mfeR", "maeR", "maxDdR", "stock", "option",
                 "execution", "levels", "checks", "cause", "mirrors", "chart"}
ADDED_CARD_KEYS = {"mirror", "variant", "sourceBook", "sourceLabel", "shadow", "sourceShadow", "triggerDirection", "engine", "day",
                   "directionRead"}


@pytest.mark.asyncio
async def test_without_the_flags_every_old_field_is_as_the_review_computes_it(tmp_path):
    """The review as it was, field by field: without ``mirrors``/``peers`` each card holds exactly the old keys plus the
    additive ones, and every old key's value is the one ``review_day`` computes from the same rows with its old
    arguments (no mirrors, no annotation)."""
    from datetime import date as _date
    from datetime import datetime as _dt

    from kotsin_nse.api import review
    from kotsin_nse.market.session import IST
    from kotsin_nse.strategy.keys import StrategyKey

    e, b = await _app(tmp_path, "b", {"name": "phase 35"}, _b_rows())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=b), base_url="http://t") as c:
        body = (await c.get("/api/review", params={"day": "2026-10-09"})).json()
    start = _dt(2026, 10, 9, tzinfo=IST).timestamp()
    rows = {k: await e.ledger.rows_between(k, start + (-3600 if k in ("signals", "events") else 0), start + 86_400 + (3 * 86_400 if k == "trades" else 0))
            for k in ("positions", "trades", "signals", "events")}
    old = review.review_day(positions=rows["positions"], trades=rows["trades"], signals=rows["signals"], events=rows["events"],
                            bars_by_symbol={}, session_end=start + 86_400, labels={k.value: k.display_name for k in StrategyKey},
                            mirror_books={k.value: (s.value, r) for k, (s, r) in STOP_MIRRORS.items()})
    assert _date.fromisoformat(body["day"]) == _date(2026, 10, 9) and len(body["trades"]) == len(old["trades"]) == 1
    for new, was in zip(body["trades"], old["trades"], strict=True):
        assert set(new) == OLD_CARD_KEYS | ADDED_CARD_KEYS and set(new) - ADDED_CARD_KEYS == OLD_CARD_KEYS
        assert {k: new[k] for k in OLD_CARD_KEYS} == json.loads(json.dumps({k: was[k] for k in OLD_CARD_KEYS}))
    assert body["summary"] == json.loads(json.dumps(old["summary"])) and body["thresholds"] == old["thresholds"]


@pytest.mark.asyncio
async def test_a_malformed_skip_rules_file_shows_nothing_and_says_so(tmp_path):
    """Safety review, 10 Oct: ``"all": 5`` in skip_rules.candidate.json raised on every card, so the whole review was a 500."""
    from kotsin_nse.api.review_direction import SKIP_RULES_FILE

    e, b = await _app(tmp_path, "b", {"name": "phase 35"}, _b_rows())
    (e.s.data_dir / SKIP_RULES_FILE).write_text(json.dumps({"rules": [{"id": "R1", "all": 5}]}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=b), base_url="http://t") as c:
        r = await c.get("/api/review", params={"day": "2026-10-09"})
    assert r.status_code == 200
    body = r.json()
    assert body["skipRules"] is False and any("not in the expected shape" in n for n in body["notes"])
    assert all(t["directionRead"]["skip"] is None for t in body["trades"])
