"""The research loop's guards: holdout split and time blocks, the Bonferroni-adjusted grade, the
monthly spread, blinded packs and tables, the daily archive, the backtester's daily/delivery mode
and warm-up context, and the autopilot with its veto — all without a network."""

from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from kotsin_nse.committee.evidence import BLIND_SYMBOL, Case, case_pack, render_case
from kotsin_nse.committee.experiments import (
    changes_key,
    grade,
    holdout_split,
    monthly,
    time_blocks,
    walk_forward_score,
)
from kotsin_nse.committee.forensics import blind_view, flat, forensics, from_backtest, render
from kotsin_nse.committee.service import CommitteeService
from kotsin_nse.config import Settings
from kotsin_nse.domain import Direction, ExitReason
from kotsin_nse.engine import Engine
from kotsin_nse.ops.archive import DailyArchive
from kotsin_nse.research.backtest import Backtester, BacktestParams, BtTrade, OpenTrade
from kotsin_nse.research.history import HistoryStore
from kotsin_nse.strategy.base import Outcome, Signal
from kotsin_nse.strategy.keys import StrategyKey

from .conftest import bar, ist_ts
from .test_committee import _arm, _bt, _fake, _rows, _signal

DAY0 = "2026-01-05"


def _write_history(root: Path, symbol: str, trading_days: int = 100) -> HistoryStore:
    """A deterministic wiggle: enough bars for the indicators, no look-ahead, weekends skipped."""
    store = HistoryStore(root)
    rows30, rows1d = [], []
    d = date.fromisoformat(DAY0)
    price = 1000.0
    n = 0
    while n < trading_days:
        if d.weekday() < 5:
            n += 1
            day_open = price
            hi = lo = price
            for i in range(13):  # 09:15 … 15:15
                ts = int(ist_ts(d.isoformat(), "09:15")) + i * 1800
                drift = ((n * 7 + i * 3) % 11 - 5) * 0.4
                o, c = price, price + drift
                h, lo_ = max(o, c) + 1.5, min(o, c) - 1.5
                rows30.append({"ts": ts, "o": o, "h": h, "l": lo_, "c": c, "v": 1000.0})
                hi, lo = max(hi, h), min(lo, lo_)
                price = c
            rows1d.append(
                {"ts": int(ist_ts(d.isoformat(), "00:00")), "o": day_open, "h": hi, "l": lo, "c": price, "v": 13000.0}
            )
        d += timedelta(days=1)
    store.save(symbol, "30m", pd.DataFrame(rows30))
    store.save(symbol, "1d", pd.DataFrame(rows1d))
    return store


def _t(day: str, r: float) -> BtTrade:
    return BtTrade(
        strategy="FUDKII", symbol="X", direction="BULLISH", day=day, entry_ts=1, exit_ts=2, entry=100.0,
        exit=100.0 + r, stop=99.0, target1=102.0, qty=1, gross=r, charges=0.0, net=r, r_multiple=r,
        mfe_r=max(r, 0.0), mae_r=min(r, 0.0), exit_reason="TARGET", grade="A", bars_held=2,
    )


def _always(direction: Direction):
    """A stand-in for the strategies: one signal on every bar, so the loop's plumbing can be seen."""

    def decide(fudkii: Any, fukaa: Any, ctx: Any, fukaa_ctx: Any, b: Any) -> Outcome:
        up = direction is Direction.BULLISH
        sig = Signal(
            strategy=StrategyKey.FUDKII, symbol=b.symbol, direction=direction, ts=b.ts, entry=b.close,
            stop=b.close * (0.99 if up else 1.01), targets=(b.close * (1.02 if up else 0.98),), grade="A", rr=2.0,
        )
        return Outcome(signals=[sig])

    return staticmethod(decide)


# -- ranges and statistics -------------------------------------------------------------------------


def test_time_blocks_and_holdout_split(tmp_path):
    blocks = time_blocks(date(2026, 1, 1), date(2026, 4, 30), 4)
    assert len(blocks) == 4 and blocks[0][0] == date(2026, 1, 1) and blocks[-1][1] == date(2026, 4, 30)
    assert all(blocks[i][1] + timedelta(days=1) == blocks[i + 1][0] for i in range(3))
    with pytest.raises(ValueError):
        time_blocks(date(2026, 1, 1), date(2025, 1, 1), 2)

    store = _write_history(tmp_path / "history", "AAA")
    split = holdout_split(store, ["AAA"], frac=0.3)
    assert split.train_end + timedelta(days=1) == split.test_start
    span = (split.test_end - split.train_start).days
    assert abs((split.test_start - split.train_start).days / span - 0.7) < 0.05
    assert set(split.to_json()) == {"train_start", "train_end", "test_start", "test_end"}
    with pytest.raises(ValueError):
        holdout_split(store, ["AAA"], frac=0.0)
    with pytest.raises(RuntimeError):
        holdout_split(store, ["NOPE"])


def test_grade_adjusts_the_p_for_hypotheses_tested():
    g = grade(_arm(60, -1.0), _arm(60, 0.5), n_tested=50)
    assert g["verdict"] == "confirmed" and g["p_adjusted"] == pytest.approx(min(1.0, g["p_value"] * 50))
    g2 = grade(_arm(60, -1.0), _arm(60, 0.5), n_tested=2000)
    assert g2["verdict"] == "inconclusive" and "tested" in g2["note"] and g2["n_tested"] == 2000


def test_monthly_spread_counts_months_the_change_won():
    base = [_t("2026-02-03", -1.0), _t("2026-02-10", -1.0), _t("2026-03-03", -1.0)]
    pat = [_t("2026-02-04", 0.5), _t("2026-03-05", -2.0), _t("2026-04-01", 1.0)]
    m = monthly(base, pat)
    assert m["months"] == 2 and m["patched_beats_baseline"] == 1
    assert [r["month"] for r in m["rows"]] == ["2026-02", "2026-03", "2026-04"]
    assert m["rows"][2]["baseline_avg_r"] is None


def test_changes_key_ignores_order_and_container():
    from kotsin_nse.committee.schemas import ParamChange

    a = changes_key([{"path": "b", "value": 1}, {"path": "a", "value": 2.0}])
    b = changes_key([ParamChange(path="a", value=2), ParamChange(path="b", value=1.0)])
    assert a == b and a != changes_key([{"path": "a", "value": 3}])


# -- blinding ----------------------------------------------------------------------------------------


def test_blind_pack_hides_names_and_dates_but_keeps_every_number():
    ts = int(ist_ts(DAY0, "10:15"))
    sig = _signal(ts)
    case = Case(
        ref=sig["signal_id"], kind="ledger", strategy="FUDKII", symbol="RELIANCE", direction="BULLISH", ts=ts,
        entry=1000.0, stop=998.0, targets=(1010.0,), grade="A", rr=5.0, signal=sig,
        trade={"symbol": "RELIANCE 25 SEP 2026 CE 1020.00", "gross": -1.0, "charges": 1.0, "net": -2.0, "r_multiple": -1.0,
               "mfe_r": 0.0, "mae_r": -1.0, "exit_reason": "SL-EQ", "duration_s": 1800, "qty": 1, "entry": 1, "exit": 0.5},
        bars_after=_rows([(1001.0, 997.5, 998.0)], t0=ts + 1800),
    )
    blind = case_pack(case, blind=True)
    clear = case_pack(case)
    assert blind["case.symbol"] == BLIND_SYMBOL and blind["case.ref"] == "case"
    assert re.fullmatch(r"[A-Z][a-z]{2} \d\d:\d\d", blind["case.ts_ist"]) and clear["case.ts_ist"] == "2026-01-05 10:15"
    assert "ts" not in blind["bars_after"][0] and "ts" in clear["bars_after"][0]
    assert blind["out.instrument"] == "option" and clear["out.instrument"].startswith("RELIANCE")
    assert blind["lvl.stop_dist_atr"] == clear["lvl.stop_dist_atr"] == 0.25
    text = render_case(blind)
    assert "RELIANCE" not in text and "2026" not in text and "lvl.stop_dist_atr: 0.25" in text


def test_blind_view_tokenises_symbols_and_months():
    rows = [_bt(i, r=-1.0, net=-100, exit_reason="SL-EQ", stop_pct=0.2, bars_held=1) for i in range(12)]
    for d in rows[:6]:
        d["symbol"] = "TCS"
    f = forensics([from_backtest(d) for d in rows])
    view, mapping = blind_view(f)
    assert [b["label"] for b in view["dims"]["symbol"]] == ["S01", "S02"]
    assert set(mapping.values()) >= {"TCS", "RELIANCE"} and mapping["M01"] == "2026-01"
    assert "first_day" not in view["cohort"] and "first_day" in f["cohort"]
    text = render(view, title="cohort")
    assert "RELIANCE" not in text and "TCS" not in text and "2026-01" not in text and "by_symbol.S01" in text
    assert "by_symbol.RELIANCE.n" in flat(f)


# -- the archive ------------------------------------------------------------------------------------


def test_daily_archive_writes_parquet_and_dedups(tmp_path):
    a = DailyArchive(tmp_path / "archive")
    ts = ist_ts(DAY0, "10:00")
    a.bar(bar(ts, 100, 101, 99, 100.5, symbol="AAA", tf="1m"))
    a.oi("123", ts, 1000.0, 1.5)
    a.micro("2885", int(ts), {"ofi_l1": 12.0, "spread_bps": 3.5, "levels": {"bids": []}, "tape_available": False})
    assert a.rows_buffered == 3
    assert a.flush() == 3 and a.rows_buffered == 0
    bars = pd.read_parquet(tmp_path / "archive" / "bars" / f"{DAY0}.parquet")
    assert len(bars) == 1 and bars.iloc[0]["c"] == 100.5
    micro = pd.read_parquet(tmp_path / "archive" / "micro" / f"{DAY0}.parquet")
    assert set(micro.columns) == {"scrip_code", "bucket_ts", "ofi_l1", "spread_bps"}
    # the same key again → replaced, not duplicated
    a.bar(bar(ts, 100, 101, 99, 100.9, symbol="AAA", tf="1m"))
    a.flush()
    bars = pd.read_parquet(tmp_path / "archive" / "bars" / f"{DAY0}.parquet")
    assert len(bars) == 1 and bars.iloc[0]["c"] == 100.9
    st = a.stats()
    # Asserts the streams this test writes, not the full set: adding a new archive stream is
    # not a regression in daily rollover, and an exact-dict assertion made it look like one.
    assert st["rows_written"] == 4 and st["errors"] == 0
    assert {k: st["days"][k] for k in ("bars", "oi", "micro")} == {"bars": 1, "oi": 1, "micro": 1}
    off = DailyArchive(tmp_path / "off", enabled=False)
    off.oi("1", ts, 1.0, None)
    assert off.rows_buffered == 0 and off.flush() == 0


# -- the backtester: daily mode, delivery costs, warm-up context ----------------------------------------


def test_daily_mode_adjusts_the_strategy_and_delivery_charges_stt_on_both_legs(settings, equity):
    daily = Backtester(settings, BacktestParams(decision_tf="1d"))
    assert daily.p.fudkii.tf == "1d" and daily.p.fudkii.eod_strong_only is False
    intraday = Backtester(settings, BacktestParams())
    assert intraday.p.fudkii.tf == "30m" and intraday.p.fudkii.eod_strong_only is True
    assert BacktestParams(decision_tf="1d", holding="delivery").to_json()["holding"] == "delivery"

    ts = int(ist_ts(DAY0, "10:15"))
    t = OpenTrade(
        strategy="FUDKII", symbol="RELIANCE", direction=Direction.BULLISH, entry_ts=ts, entry=100.0, stop=99.0,
        initial_stop=99.0, targets=(103.0,), qty=100, grade="A", peak=101.0, trough=99.5,
    )
    b = bar(ts + 1800, 100, 101.5, 99.5, 101)
    i = intraday._close(t, b, 101.0, ExitReason.TARGET, equity)
    d = Backtester(settings, BacktestParams(holding="delivery"))._close(t, b, 101.0, ExitReason.TARGET, equity)
    rate = settings.cost_stt_pct_delivery_equity / 100
    extra = 100 * 100 * rate + 101 * 100 * (rate - settings.cost_stt_pct_sell_equity / 100)
    assert d.charges - i.charges == pytest.approx(extra, abs=0.02)
    assert d.net < i.net


def test_ranged_runs_keep_warm_up_context_and_delivery_refuses_shorts(settings, tmp_path, monkeypatch):
    store = _write_history(tmp_path / "history", "AAA", trading_days=60)
    monkeypatch.setattr(Backtester, "_decide", _always(Direction.BEARISH))
    days = sorted({date.fromtimestamp(int(t)).isoformat() for t in store.load("AAA", "30m")["ts"]})
    start, end = date.fromisoformat(days[40]), date.fromisoformat(days[-1])
    lo = int(ist_ts(start.isoformat(), "00:00"))

    res = Backtester(settings, BacktestParams()).run(store, ["AAA"], start=start, end=end)
    in_range = int((store.load("AAA", "30m")["ts"] >= lo).sum())
    assert res.trades and all(t.entry_ts >= lo for t in res.trades)
    assert res.bars > in_range  # the warm-up bars were replayed…
    assert res.signals <= in_range  # …but never traded or counted

    dl = Backtester(settings, BacktestParams(holding="delivery")).run(store, ["AAA"], start=start, end=end)
    assert dl.trades == [] and dl.binding_gates["DELIVERY:no_short"] > 0 and dl.signals == 0


def test_walk_forward_score_blocks_and_penalty(settings, tmp_path, monkeypatch):
    store = _write_history(tmp_path / "history", "AAA", trading_days=60)
    monkeypatch.setattr(Backtester, "_decide", _always(Direction.BULLISH))
    days = sorted({date.fromtimestamp(int(t)).isoformat() for t in store.load("AAA", "30m")["ts"]})
    start, end = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    wf = walk_forward_score(settings, BacktestParams(), store, ["AAA"], start=start, end=end, n_blocks=3)
    assert wf["blocks_total"] == 3 and wf["trades"] > 0 and isinstance(wf["score"], float)
    starved = walk_forward_score(
        settings, BacktestParams(), store, ["AAA"], start=start, end=end, n_blocks=3, min_trades=100_000
    )
    assert starved["score"] < wf["score"]


# -- the autopilot ----------------------------------------------------------------------------------


async def test_autopilot_reviews_experiments_out_of_sample_then_vetoes(settings, tmp_path, monkeypatch):
    _write_history(tmp_path / "history", "AAA", trading_days=120)
    monkeypatch.setattr(Backtester, "_decide", _always(Direction.BULLISH))
    runs = tmp_path / "backtests"
    runs.mkdir()
    trades = [_bt(i, r=-1.5, net=-300, exit_reason="SL-EQ", stop_pct=0.1, bars_held=1) for i in range(40)]
    (runs / "bt-test.json").write_text(
        json.dumps({"summary": {"id": "bt-test", "params": {"segment": "NSE_EQ"}, "trades": 40}, "trades": trades})
    )
    engine = Engine(settings)
    await engine.ledger.init()
    llm = _fake()
    svc = CommitteeService(engine, settings, llm=llm)
    assert await svc.autopilot_source() == "backtest:bt-test"

    summary = await svc.autopilot_once()
    assert summary["kind"] == "autopilot" and len(summary["ran"]) == 1 and summary["skipped"] == []
    status = summary["ran"][0]["status"]
    assert status in ("confirmed", "refuted", "inconclusive")
    hyp = svc.log.hypotheses()
    graded = next(h for h in hyp if h["status"] == status)
    r = graded["result"]
    assert set(r["split"]) == {"train_start", "train_end", "test_start", "test_end"}
    assert "in_sample" in r and "out_of_sample" in r and "cost_stress" in r and "monthly" in r
    assert r["n_tested"] == 1 and svc.n_tested() == 2
    # the committee read a blinded table
    cohort = next(e for e in svc.log.entries if e["kind"] == "cohort")
    assert cohort["blind"] is True and "RELIANCE" in cohort["blind_map"].values()
    assert "RELIANCE" not in llm.calls[0][2] and "S01" in llm.calls[0][2]

    again = await svc.autopilot_once()
    assert again["ran"] == [] and len(again["skipped"]) == 1 and again["skipped"][0]["prior"] == status
    assert any(h["status"] == "vetoed" for h in svc.log.hypotheses())
    assert svc.vetoed([{"path": "fudkii.grade_policy.min_stop_atr", "value": 0.5}]) == status
    assert svc.vetoed([{"path": "fudkii.grade_policy.min_stop_atr", "value": 0.7}]) is None

    assert svc.autopilot_due(time.time(), "2026-09-22", "") is False  # off by default
    on = Settings(
        _env_file=None, data_dir=tmp_path, db_url=settings.db_url, engine_enabled=False,
        committee_autopilot=True, committee_autopilot_ist="00:00",
    )
    due = CommitteeService(engine, on, llm=llm)
    assert due.autopilot_due(time.time(), "2026-09-22", "") is True
    assert due.autopilot_due(time.time(), "2026-09-22", "2026-09-22") is False
    await engine.ledger.close()
