"""The review committee: forensic buckets, the case pack's path maths, the pipeline's call shape
(FakeLLM), the experiment patch/grade rules, the review log, and one end-to-end review of a ledger
signal through the real Engine."""

from __future__ import annotations

from typing import Any

import pytest

from kotsin_nse.committee.evidence import BarRow, Case, case_pack, path_metrics, render_case
from kotsin_nse.committee.experiments import apply_changes, grade
from kotsin_nse.committee.forensics import flat, forensics, from_backtest, from_ledger, render
from kotsin_nse.committee.llm import FakeLLM
from kotsin_nse.committee.memory import ReviewLog
from kotsin_nse.committee.pipeline import run_case_committee, run_cohort_committee
from kotsin_nse.committee.schemas import (
    CaseAnalystReport,
    CaseDebate,
    CohortFinding,
    CohortReport,
    FailureMode,
    Hypothesis,
    ParamChange,
    PostMortem,
    Reflection,
)
from kotsin_nse.committee.service import CommitteeService
from kotsin_nse.engine import Engine
from kotsin_nse.research.backtest import BacktestParams, BtTrade

from .conftest import bar, ist_ts

DAY0 = "2026-01-05"  # a Monday


def _bt(
    i: int,
    *,
    r: float,
    net: float,
    exit_reason: str,
    stop_pct: float,
    bars_held: int,
    mfe_r: float = 0.0,
    mae_r: float = -1.0,
    grade: str = "A",
) -> dict[str, Any]:
    day = f"2026-01-{5 + i % 12:02d}"
    ts = int(ist_ts(day, "10:15"))
    entry = 1000.0
    stop = entry * (1 - stop_pct / 100)
    return {
        "strategy": "FUDKII",
        "symbol": "RELIANCE",
        "direction": "BULLISH",
        "day": day,
        "entry_ts": ts,
        "exit_ts": ts + bars_held * 1800,
        "entry": entry,
        "exit": entry + r * (entry - stop),
        "stop": stop,
        "target1": entry + 2.5 * (entry - stop),
        "qty": 10,
        "gross": net + 80,
        "charges": 80.0,
        "net": net,
        "r_multiple": r,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "exit_reason": exit_reason,
        "grade": grade,
        "bars_held": bars_held,
    }


def _rows(specs: list[tuple[float, float, float]], t0: int = 1_000) -> list[BarRow]:
    return [BarRow(ts=t0 + i * 1800, o=100.0, h=h, l=lo, c=c, v=1.0) for i, (h, lo, c) in enumerate(specs)]


# -- forensics ---------------------------------------------------------------------------------


def test_forensics_buckets_and_headline():
    rows = []
    for i in range(20):  # the modal loss: a stop inside 0.25% taken out on the first bar
        rows.append(_bt(i, r=-1.5, net=-300, exit_reason="SL-EQ", stop_pct=0.1, bars_held=1, mfe_r=0.2, mae_r=-1.5))
    for i in range(10):
        rows.append(_bt(i, r=2.0, net=400, exit_reason="TARGET", stop_pct=0.8, bars_held=6, mfe_r=2.5, mae_r=-0.3))
    for i in range(10):  # gave back: MFE ≥ 1 R, closed at a loss
        rows.append(_bt(i, r=-0.5, net=-100, exit_reason="TRAIL", stop_pct=0.8, bars_held=8, mfe_r=1.5, mae_r=-0.6))
    f = forensics([from_backtest(d) for d in rows])
    c = f["cohort"]
    assert c["n"] == 40 and c["n_days"] == 12
    assert c["first_bar_stop_rate"] == 50.0 and c["stop_hit_rate"] == 50.0
    assert c["give_back_rate"] == 25.0 and c["t1_hit_rate"] == 25.0
    assert c["first_bar_stop_avg_r"] == -1.5
    by_stop = {b["label"]: b for b in f["dims"]["stop_pct"]}
    assert by_stop["<0.25"]["n"] == 20 and by_stop["<0.25"]["avg_r"] == -1.5
    assert by_stop["0.5-1"]["n"] == 20
    by_exit = {b["label"]: b for b in f["dims"]["exit_reason"]}
    assert by_exit["TARGET"]["n"] == 10 and by_exit["TARGET"]["win_rate"] == 100.0
    assert abs(sum(b["loss_share"] for b in f["dims"]["exit_reason"]) - 100.0) < 0.01
    assert [b["label"] for b in f["dims"]["bars_held"]] == ["1", "5-12"]  # empty buckets are dropped
    keys = flat(f)
    assert "by_stop_pct.<0.25.avg_r" in keys and "cohort.first_bar_stop_rate" in keys
    text = render(f, title="test")
    assert "cohort.first_bar_stop_rate: 50" in text and "by_stop_pct.<0.25: n=20" in text
    assert forensics([]) == {"cohort": {"n": 0}, "dims": {}}


def test_ledger_trade_normalises_to_the_underlying_levels():
    d = {
        "strategy": "FUKAA",
        "symbol": "RELIANCE 25 SEP 2026 PE 1500.00",
        "underlying": "RELIANCE",
        "opened_ts": ist_ts(DAY0, "10:15"),
        "closed_ts": ist_ts(DAY0, "12:15"),
        "entry": 12.5,
        "exit": 9.0,
        "gross": -875.0,
        "charges": 80.0,
        "net": -955.0,
        "r_multiple": -1.2,
        "mfe_r": 0.4,
        "mae_r": -1.2,
        "exit_reason": "SL-OP",
        "grade": "B",
        "equity_entry": 1480.0,
        "equity_sl": 1490.0,
        "equity_targets": [1450.0, 1430.0],
        "duration_s": 7200,
    }
    r = from_ledger(d)
    assert r.direction == "BEARISH" and r.symbol == "RELIANCE" and r.day == DAY0
    assert r.bars_held == 4 and r.target1 == 1450.0 and r.source == "ledger"
    assert r.stop_pct == pytest.approx(10 / 1480 * 100)
    assert r.rr == pytest.approx(3.0)


# -- the case pack ------------------------------------------------------------------------------


def test_path_metrics_first_touch_and_the_stop_that_would_have_survived():
    m = path_metrics(
        bullish=True,
        entry=100.0,
        stop=99.0,
        t1=103.0,
        bars=_rows([(100.5, 99.5, 100.2), (101.0, 99.2, 100.8), (103.2, 99.6, 102.9), (104.0, 102.0, 103.5)]),
    )
    assert m["path.first_touch"] == "T1" and m["path.bars_to_t1"] == 3 and m["path.bars_to_stop"] is None
    assert m["path.max_adverse_before_t1_r"] == pytest.approx(-0.8)
    assert m["path.mfe_r"] == pytest.approx(4.0) and m["path.mfe_bar"] == 4
    assert m["path.close_r_1b"] == pytest.approx(0.2) and m["path.direction_right_at_end"] is True
    # both touched on the same bar → the stop wins (the backtester's convention, the pessimistic one)
    m2 = path_metrics(bullish=True, entry=100.0, stop=99.0, t1=101.0, bars=_rows([(101.5, 98.9, 101.0)]))
    assert m2["path.first_touch"] == "STOP" and m2["path.bars_to_stop"] == 1 and m2["path.bars_to_t1"] == 1
    # bearish mirror
    m3 = path_metrics(bullish=False, entry=100.0, stop=101.0, t1=97.0, bars=_rows([(100.4, 99.0, 99.2), (100.2, 96.9, 97.5)]))
    assert m3["path.first_touch"] == "T1" and m3["path.bars_to_t1"] == 2 and m3["path.mae_r"] == pytest.approx(-0.4)
    assert path_metrics(bullish=True, entry=100.0, stop=100.0, t1=None, bars=[])["path.window_bars"] == 0


def _signal(ts: int) -> dict[str, Any]:
    return {
        "signal_id": f"FUDKII-RELIANCE-{ts}-B",
        "strategy": "FUDKII",
        "symbol": "RELIANCE",
        "direction": "BULLISH",
        "ts": ts,
        "entry": 1000.0,
        "stop": 998.0,
        "targets": [1010.0, 1020.0],
        "grade": "A",
        "rr": 5.0,
        "score": 100.0,
        "confidence": 1.0,
        "reason": "st flip + bb break",
        "decision": "PAPER_FILLED",
        "decision_reason": "",
        "gates": [
            {"name": "st_flip", "passed": True, "required": True, "value": 0.0, "threshold": 0.0, "missing": False, "note": ""}
        ],
        "evidence": {"bars_since_flip": 0.0, "score": 100.0, "volume": 5000.0, "atr": 8.0},
        "context": {
            "indicators": {
                "bb_upper": 995.0, "bb_middle": 980.0, "bb_lower": 965.0, "st_value": 990.0, "st_trend": 1,
                "bars_in_trend": 1, "atr": 8.0,
                "params": {"bb_period": 20, "bb_mult": 2.0, "st_atr_period": 7, "st_mult": 3.0},
            },
            "confluence": {
                "stop": 998.0, "stop_zone": "1d:S1", "targets": [1010.0, 1020.0], "target_zones": ["1d:R1", "1wk:R1"],
                "grade": "A", "rr": 5.0, "fortress": 6.0, "room_ratio": 1.2, "note": "", "policy": {"rr_a": 2.5},
            },
            "zones": [
                {"price": 998.0, "strength": 4.0, "wall": False, "members": ["1d:S1"]},
                {"price": 1010.0, "strength": 6.0, "wall": True, "members": ["1d:R1"]},
                {"price": 1020.0, "strength": 5.5, "wall": True, "members": ["1wk:R1"]},
            ],
        },
    }


def test_case_pack_reads_the_stored_context_and_computes_only_the_path():
    ts = int(ist_ts(DAY0, "10:15"))
    sig = _signal(ts)
    case = Case(
        ref=sig["signal_id"], kind="ledger", strategy="FUDKII", symbol="RELIANCE", direction="BULLISH", ts=ts,
        entry=1000.0, stop=998.0, targets=(1010.0, 1020.0), grade="A", rr=5.0, exchange="N", session_phase="MID",
        mode="PAPER", signal=sig, trade=None, bars_before=[],
        bars_after=_rows([(1001.0, 997.5, 998.0), (1012.0, 999.0, 1011.0)], t0=ts + 1800),
    )
    pack = case_pack(case)
    assert pack["trig.close_vs_band_pct"] == pytest.approx((1000 / 995 - 1) * 100, abs=1e-3)
    assert pack["trig.atr_source"] == "signal" and pack["trig.bars_since_flip"] == 0.0
    assert pack["lvl.stop_dist_atr"] == pytest.approx(0.25) and pack["lvl.stop_dist_pct"] == pytest.approx(0.2)
    assert pack["lvl.stop_zone_strength"] == 4.0 and pack["lvl.walls_ahead"] == 2 and pack["lvl.walls_behind"] == 0
    assert pack["lvl.t1_dist_atr"] == pytest.approx(1.25) and pack["lvl.t1_zone"] == "1d:R1"
    assert pack["gate.st_flip"].startswith("pass") and pack["out.filled"] is False
    assert pack["path.first_touch"] == "STOP" and pack["path.bars_to_stop"] == 1 and pack["path.bars_to_t1"] == 2
    assert pack["case.session_phase"] == "MID" and pack["case.ts_ist"] == "2026-01-05 10:15"
    text = render_case(pack)
    assert "lvl.stop_dist_atr: 0.25" in text and "zones (" in text and "bars after the decision" in text

    # a backtest trade has no Signal.context: the ATR is a proxy from the bars and the pack says so
    before = _rows([(101.0 + i * 0.1, 99.0 - i * 0.1, 100.0) for i in range(20)], t0=ts - 20 * 1800)
    bt_case = Case(
        ref="bt-x#0", kind="backtest", strategy="FUDKII", symbol="ADANIENT", direction="BULLISH", ts=ts,
        entry=2516.26, stop=2513.7, targets=(2527.65,), grade="A", rr=4.45, exchange="N", session_phase="MID",
        mode="backtest", signal=None,
        trade={"gross": -148.76, "charges": 128.94, "net": -277.7, "r_multiple": -2.784, "mfe_r": 15.54, "mae_r": -1.117,
               "exit_reason": "SL-EQ", "bars_held": 1, "entry": 2516.26, "exit": 2512.44, "qty": 39},
        bars_before=before, bars_after=_rows([(2530.0, 2510.0, 2512.0)], t0=ts + 1800),
    )
    p2 = case_pack(bt_case)
    assert p2["trig.atr_source"] == "proxy_from_bars" and "trig.note" in p2 and p2["trig.atr"] > 0
    assert p2["out.charges_share_of_gross_pct"] == pytest.approx(128.94 / 148.76 * 100, rel=1e-3)
    assert p2["out.bars_held"] == 1 and p2["lvl.n_zones"] is None


# -- the pipeline --------------------------------------------------------------------------------


def _hyp() -> Hypothesis:
    return Hypothesis(
        title="require the stop to be ≥ 0.5 ATR away",
        rationale="first-bar stops carry the losses [by_stop_pct.<0.25.avg_r]",
        changes=[ParamChange(path="fudkii.grade_policy.min_stop_atr", value=0.5)],
        expected="avg R of the <0.25 bucket rises above −1 and overall avg R improves",
    )


def _fake() -> FakeLLM:
    def analyst(system: str, user: str) -> CaseAnalystReport:
        role = "entry" if "ENTRY analyst" in system else ("levels" if "LEVELS analyst" in system else "execution")
        return CaseAnalystReport(
            role=role, summary="s", findings=["f [lvl.stop_dist_atr]"], evidence_keys=["lvl.stop_dist_atr"], severity=0.7
        )

    return FakeLLM(
        {
            CaseAnalystReport: analyst,
            CaseDebate: CaseDebate(
                construction_case="c", thesis_case="t", unrebutted_construction_point="p [lvl.stop_dist_atr]",
                unrebutted_thesis_point="q [path.close_r_16b]", unresolved=[],
            ),
            PostMortem: PostMortem(
                failure_mode=FailureMode.NOISE_STOP, secondary=[FailureMode.COST_DOMINATED], confidence=0.8,
                what_happened="w", why="y", counterfactual="c", evidence_keys=["lvl.stop_dist_atr"],
                hypothesis=_hyp(), lesson="a stop inside 0.3 ATR is noise",
            ),
            CohortReport: CohortReport(
                verdict="no edge",
                findings=[CohortFinding(title="first-bar stops", failure_mode=FailureMode.NOISE_STOP, magnitude="n=20 avg_r −1.5",
                                        evidence_keys=["by_stop_pct.<0.25.avg_r"], confidence=0.9)],
                hypotheses=[_hyp()], not_explained=[], evidence_keys=["cohort.avg_r"],
            ),
            Reflection: Reflection(lesson="wider stops help", hypothesis_supported=True),
        }
    )


async def test_case_pipeline_makes_five_calls_and_a_verdict():
    llm = _fake()
    run = await run_case_committee(llm, ref="x", evidence_text="lvl.stop_dist_atr: 0.25", past_context="past")
    assert run.error is None and run.verdict is not None and run.verdict.failure_mode is FailureMode.NOISE_STOP
    assert run.calls == 5 and len(llm.calls) == 5
    assert [a.role for a in run.analysts] == ["entry", "levels", "execution"]
    assert "EVIDENCE PACK" in llm.calls[0][2]
    assert "DEBATE" in llm.calls[-1][2] and "PAST LESSONS" in llm.calls[-1][2]
    assert run.to_json()["verdict"]["hypothesis"]["changes"][0]["path"] == "fudkii.grade_policy.min_stop_atr"


async def test_cohort_pipeline_makes_four_calls():
    llm = _fake()
    run = await run_cohort_committee(llm, ref="c", forensics_text="cohort.avg_r: -1.4")
    assert run.error is None and run.calls == 4 and run.report is not None
    assert run.report.findings[0].failure_mode is FailureMode.NOISE_STOP
    assert [a.role for a in run.analysts] == ["entry", "levels", "execution"]


async def test_pipeline_records_a_failure_instead_of_raising():
    class Boom:
        model = "boom"

        async def structured(self, **kw: Any) -> Any:
            raise RuntimeError("rate limited")

        def stats(self) -> dict[str, Any]:
            return {}

    run = await run_case_committee(Boom(), ref="x", evidence_text="e")
    assert run.verdict is None and run.error is not None and "rate limited" in run.error


# -- experiments ----------------------------------------------------------------------------------


def test_apply_changes_walks_nested_paths_and_rejects_unknown_ones():
    p = BacktestParams()
    q = apply_changes(
        p,
        [
            ParamChange(path="fudkii.grade_policy.min_stop_atr", value=0.5),
            ParamChange(path="limits.max_positions_per_strategy", value=2),
            ParamChange(path="fudkii.grade_policy.stop_requires_wall", value=1),
            ParamChange(path="slippage_bps", value=8),
        ],
    )
    assert q.fudkii.grade_policy.min_stop_atr == 0.5 and q.fudkii.grade_policy.stop_requires_wall is True
    assert q.limits.max_positions_per_strategy == 2 and isinstance(q.limits.max_positions_per_strategy, int)
    assert q.slippage_bps == 8.0
    assert p.fudkii.grade_policy.min_stop_atr == 0.0  # the original is untouched
    with pytest.raises(ValueError):
        apply_changes(p, [ParamChange(path="fudkii.nope", value=1)])
    with pytest.raises(ValueError):
        apply_changes(p, [ParamChange(path="engine.mode", value=1)])


def _arm(n: int, r: float, days: int = 15) -> list[BtTrade]:
    out = []
    for i in range(n):
        rr = r + ((i * 37) % 7 - 3) * 0.05
        out.append(
            BtTrade(
                strategy="FUDKII", symbol="X", direction="BULLISH", day=f"2026-02-{1 + i % days:02d}",
                entry_ts=i, exit_ts=i + 1, entry=100.0, exit=100.0 + rr, stop=99.0, target1=102.0, qty=1,
                gross=rr, charges=0.0, net=rr, r_multiple=rr, mfe_r=max(rr, 0.0), mae_r=min(rr, 0.0),
                exit_reason="TARGET" if rr > 0 else "SL-EQ", grade="A", bars_held=2,
            )
        )
    return out


def test_grade_confirms_refutes_or_declines_to_conclude():
    g = grade(_arm(60, -1.0), _arm(60, 0.5))
    assert g["verdict"] == "confirmed" and g["delta_avg_r"] == pytest.approx(1.5, abs=0.02) and g["p_value"] <= 0.1
    assert grade(_arm(60, -1.0), _arm(60, -1.6))["verdict"] == "refuted"
    small = grade(_arm(60, -1.0), _arm(10, 0.5))
    assert small["verdict"] == "inconclusive" and "too small" in small["note"]
    assert grade(_arm(60, -1.0), [])["verdict"] == "inconclusive"
    assert grade(_arm(60, -1.0), _arm(60, -1.0))["verdict"] == "inconclusive"


# -- memory ---------------------------------------------------------------------------------------


def test_review_log_hypotheses_lifecycle_and_past_context(tmp_path):
    path = tmp_path / "c" / "reviews.json"
    log = ReviewLog(path)
    e = log.append(
        {
            "kind": "case", "symbol": "RELIANCE", "strategy": "FUDKII", "failure_mode": "NOISE_STOP",
            "confidence": 0.8, "lesson": "a stop inside 0.3 ATR is noise",
            "hypotheses": [_hyp().model_dump()],
        }
    )
    hyp = log.hypotheses()[0]
    assert hyp["status"] == "pending" and hyp["review_id"] == e["id"] and hyp["id"].startswith("hyp")
    assert "NOISE_STOP" in log.past_context(symbol="RELIANCE")
    assert log.past_context(symbol="TCS") == ""  # pending hypotheses are never injected
    log.update_hypothesis(
        hyp["id"], status="confirmed", reflection="wider stops help",
        result={"baseline": {"avg_r": -1.4}, "patched": {"avg_r": -0.9, "n": 300}, "delta_avg_r": 0.5, "p_value": 0.01},
    )
    ctx = log.past_context()
    assert "CONFIRMED" in ctx and "0.5 ATR" in ctx and "wider stops help" in ctx
    assert ReviewLog(path).hypotheses()[0]["status"] == "confirmed"  # persisted
    st = log.stats()
    assert st["entries"] == 1 and st["hypotheses_by_status"] == {"confirmed": 1}
    assert st["by_failure_mode"] == {"NOISE_STOP": 1}


# -- the service, through the real engine ----------------------------------------------------------


async def test_review_signal_end_to_end_through_the_engine(settings):
    engine = Engine(settings)
    await engine.ledger.init()
    ts = int(ist_ts(DAY0, "10:15"))
    sig = _signal(ts)
    await engine.ledger.insert_signal(sig, "PAPER_FILLED", "")
    await engine.ledger.insert_trade(
        {
            "id": "trd-1", "position_id": "pos-1", "strategy": "FUDKII", "scrip_code": "45678",
            "symbol": "RELIANCE 25 SEP 2026 CE 1020.00", "underlying": "RELIANCE", "instrument_kind": "OPTION",
            "side": "LONG", "qty": 250, "entry": 12.0, "exit": 9.0, "gross": -750.0, "charges": 80.0, "net": -830.0,
            "r_multiple": -1.1, "mfe_r": 0.3, "mae_r": -1.1, "exit_reason": "SL-EQ", "opened_ts": ts + 60,
            "closed_ts": ts + 1800, "duration_s": 1740, "signal_id": sig["signal_id"], "grade": "A",
            "equity_entry": 1000.0, "equity_sl": 998.0, "equity_targets": [1010.0], "r_unit": 3.0, "multiplier": 1,
        }
    )
    # decision-frame bars either side of the signal, in the live store: stop on bar 1, T1 on bar 4
    bars = []
    for k in range(-30, 17):
        if k == 0:
            continue
        if k < 0:
            bars.append(bar(ts + k * 1800, 1000, 1001, 999, 1000, symbol="RELIANCE", tf="30m"))
        else:
            bars.append(bar(ts + k * 1800, 1000, 1000 + 3 * k, 997.5 if k == 1 else 999 + k, 1000 + k, symbol="RELIANCE", tf="30m"))
    engine.store.seed("RELIANCE", "30m", bars)

    svc = CommitteeService(engine, settings, llm=_fake())
    assert svc.available and not svc.auto
    entry = await svc.review_signal(sig["signal_id"])
    assert entry["failure_mode"] == "NOISE_STOP" and entry["confidence"] == 0.8
    assert entry["hypotheses"][0]["status"] == "pending" and "run" not in entry and "pack" not in entry
    full = svc.log.get(entry["id"])
    assert full is not None and full["run"]["calls"] == 5
    assert full["pack"]["out.filled"] is True and full["pack"]["path.bars_to_stop"] == 1
    assert full["pack"]["path.bars_to_t1"] == 4 and len(full["pack"]["bars_after"]) == 16
    assert svc.runs_today == 1 and svc.status()["log"]["by_failure_mode"] == {"NOISE_STOP": 1}
    assert "NOISE_STOP" in svc.log.past_context(symbol="RELIANCE")

    # forensics need no key and no call
    f = await svc.forensics("ledger")
    assert f["cohort"]["n"] == 1 and f["meta"]["source"] == "ledger"
    with pytest.raises(KeyError):
        await svc.review_signal("nope")
    with pytest.raises(KeyError):
        await svc.forensics("backtest:bt-missing")
    with pytest.raises(KeyError):
        await svc.review_cohort("backtest:bt-missing")
    await engine.ledger.close()


async def test_service_without_a_key_is_unavailable_and_says_so(settings):
    engine = Engine(settings)
    svc = engine.committee
    assert not svc.available and svc.status()["model"] is None
    with pytest.raises(RuntimeError, match="KN_ANTHROPIC_API_KEY"):
        await svc.review_signal("x")
    svc.on_trade_closed({"signal_id": "x"})  # auto-review off: a no-op, never an error
    await engine.ledger.close()


async def test_a_person_can_propose_a_hypothesis_without_a_key(settings):
    engine = Engine(settings)
    svc = engine.committee
    assert not svc.available
    h = svc.propose(
        title="stop at least 1 ATR away",
        changes=[ParamChange(path="fudkii.grade_policy.min_stop_atr", value=1.0)],
        expected="first-bar stops fall, avg R rises",
    )
    assert h["status"] == "pending" and h["id"].startswith("hyp")
    assert svc.log.hypotheses()[0]["review_kind"] == "manual"
    with pytest.raises(ValueError):  # an unknown path fails at proposal time, not in the worker
        svc.propose(title="x", changes=[ParamChange(path="fudkii.nope", value=1)])
    with pytest.raises(ValueError):
        svc.propose(title="x", changes=[])
    await engine.ledger.close()
