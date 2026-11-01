"""Option signals from a bhavcopy, regime labels that keep their meaning across refits, day-type rows
built from 1m bars alone (a bonus or split must not misplace the levels)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from kotsin_nse.fno.chain import black76
from kotsin_nse.market.session import ist_at
from kotsin_nse.research.daytype import BREAK_SKIP, stock_days
from kotsin_nse.research.optsignals import SESSIONS_PER_YEAR, day_features, sessions_to
from kotsin_nse.research.regimes import FEATURES, K, label

D = date(2025, 3, 3)
# the sessions of March 2025: weekdays, Holi (Friday the 14th) a holiday
SESSIONS = [d for d in (date(2025, 3, 1) + timedelta(days=i) for i in range(31)) if d.weekday() < 5 and d != date(2025, 3, 14)]


def _t_sessions(d: date, exp: date, sessions: list[date] = SESSIONS) -> float:
    return sum(1 for s in sessions if d < s <= exp) / SESSIONS_PER_YEAR


def _bhav(put_iv: float = 0.25, otm_put_iv: float = 0.30, d: date = D, exp: date = date(2025, 3, 27), kind: str = "STK", sym: str = "X") -> pd.DataFrame:
    """A chain priced at σ on the sessions left to expiry (not calendar days), with a future's settle."""
    f, t, e = 1000.0, _t_sessions(d, exp), exp.isoformat()
    rows = [{"symbol": sym, "kind": f"FUT{kind}", "expiry": e, "strike": 0.0, "option_type": "", "settle": f, "close": f, "oi": 1e6, "oi_change": 0.0, "volume": 100.0}]
    for k in (900.0, 950.0, 1000.0, 1050.0, 1100.0):
        c = black76(f, k, t, 0.25, True)
        rows.append({"symbol": sym, "kind": f"OPT{kind}", "expiry": e, "strike": k, "option_type": "CE", "settle": c, "close": c,
                     "oi": 1e5, "oi_change": 1000.0 if k > 1020 else 0.0, "volume": 50.0})
        iv = otm_put_iv if k == 950.0 else put_iv
        p = black76(f, k, t, iv, False)
        rows.append({"symbol": sym, "kind": f"OPT{kind}", "expiry": e, "strike": k, "option_type": "PE", "settle": p, "close": p,
                     "oi": 1e5, "oi_change": 5000.0 if k < 980 else 0.0, "volume": 50.0})
    return pd.DataFrame(rows)


def test_option_features_read_the_iv_spread_skew_and_the_otm_build():
    (r,), _ = day_features(_bhav(put_iv=0.27, otm_put_iv=0.33), D, SESSIONS)
    assert r["iv_c"] == pytest.approx(0.25, abs=1e-3) and r["iv_p"] == pytest.approx(0.27, abs=1e-3)
    assert r["iv_otm_p"] == pytest.approx(0.33, abs=1e-3), "the traded put nearest 0.95 F"
    assert (r["otm_put_add"], r["otm_call_add"]) == (10_000.0, 2_000.0)  # the 900 / 950 puts, the 1050 / 1100 calls
    assert r["t"] == pytest.approx(17 / 252), "17 sessions to the 27th (Holi out), not 24 calendar days / 365"


def test_sessions_to_expiry_counts_the_session_list_and_weekdays_past_its_end():
    assert sessions_to(date(2025, 3, 6), "2025-03-11", SESSIONS) == 3  # Fri, Mon, Tue
    assert sessions_to(date(2025, 3, 13), date(2025, 3, 17), SESSIONS) == 1  # Holi out: Thu → Mon is one session, 4 calendar days
    assert sessions_to(date(2025, 3, 31), "2025-04-03", SESSIONS) == 3, "past the list: weekdays stand in"
    assert sessions_to(date(2025, 3, 27), "2025-03-27", SESSIONS) == 1, "never zero time"


def test_an_option_priced_at_sigma_three_sessions_out_round_trips_through_the_solver():
    from kotsin_nse.research.condor import open_fly

    d, exp = date(2025, 3, 6), date(2025, 3, 11)  # Thursday → Tuesday: 5 calendar days, 3 sessions
    assert sessions_to(d, exp, SESSIONS) == 3
    (r,), _ = day_features(_bhav(put_iv=0.25, otm_put_iv=0.25, d=d, exp=exp), d, SESSIONS)
    assert r["iv_c"] == pytest.approx(0.25, abs=1e-4) and r["iv_p"] == pytest.approx(0.25, abs=1e-4)
    assert r["iv_otm_p"] == pytest.approx(0.25, abs=1e-4)
    assert r["t"] == pytest.approx(3 / 252)
    # the index butterfly solves on the same clock: its IV round-trips and its wings are σ_true·F·√(3/252) out
    fly = open_fly(_bhav(0.25, 0.25, d=d, exp=exp, kind="IDX", sym="NIFTY"), d, SESSIONS, wing_sd=1.0, to_expiry=True)
    assert fly is not None and fly["bdays"] == 3 and fly["dte"] == 5
    assert fly["iv"] == pytest.approx(0.25, abs=1e-3)
    assert fly["sd_pts"] == pytest.approx(0.25 * fly["f"] * math.sqrt(3 / 252), rel=1e-3)


def test_regime_labels_keep_their_meaning_across_quarterly_refits():
    rng = np.random.default_rng(0)
    days = [date(2021, 1, 1) + timedelta(days=i) for i in range(1400)]
    calm = np.array([(i // 50) % 2 == 0 for i in range(len(days))])
    x = rng.normal(size=(len(days), len(FEATURES))) * 0.3
    x[:, list(FEATURES).index("iv")] += np.where(calm, 0.0, 3.0)
    x[:, list(FEATURES).index("dispersion")] += np.where(calm, 0.0, 3.0)
    m = pd.DataFrame(x, columns=list(FEATURES), index=[d.isoformat() for d in days])
    reg = label(m, date(2022, 4, 1)).dropna()
    assert reg.nunique() <= K
    # the stressed days fall in the same few labels in the first and the last year (matched, not shuffled)
    stressed = pd.Series(~calm, index=m.index).reindex(reg.index)
    early, late = reg[(reg.index < "2023-01-01") & stressed], reg[(reg.index >= "2024-06-01") & stressed]
    assert set(late.value_counts().index[:1]) <= set(early.value_counts().index[:2])


def _m1(day: date, open_: float, scale: float = 1.0) -> list[dict]:
    t0 = ist_at(day, "09:15")
    px = (open_ + np.sin(np.arange(359) / 20) * 2) * scale
    return [{"ts": t0 + 60 * i, "o": p, "h": p + 0.5 * scale, "l": p - 0.5 * scale, "c": p, "v": 1000.0} for i, p in enumerate(px)]


def test_day_rows_come_from_1m_levels_and_a_bonus_skips_the_sessions_after_it():
    days = [date(2024, 4, 1) + timedelta(days=i) for i in range(80)]
    days = [d for d in days if d.weekday() < 5]
    bonus = days[40]
    rows = []
    for d in days:
        rows += _m1(d, 100.0, 0.5 if d >= bonus else 1.0)  # a 1:1 bonus halves the price
    got = pd.DataFrame(stock_days("X", pd.DataFrame(rows), days[0], days[-1]))
    assert len(got) > 20
    assert got["gap"].abs().max() < 1.0, "levels from the same 1m scale: no phantom gap"
    after = [d.isoformat() for d in days[40 : 40 + BREAK_SKIP]]
    assert not got["day"].isin(after).any()
    assert math.isfinite(got["vs_p"].abs().max())


def _m5(day: date, scale: float, phase: float = 0.0) -> list[dict]:
    """A session of 75 five-minute bars, 09:15 → 15:25, on ``scale``."""
    t0 = ist_at(day, "09:15")
    px = (100.0 + np.sin(np.arange(75) / 10 + phase) * 2) * scale
    return [{"ts": t0 + 300 * i, "o": p, "h": p + 0.5 * scale, "l": p - 0.5 * scale, "c": p, "v": 1000.0} for i, p in enumerate(px)]


def test_the_sweep_takes_its_levels_and_adr_from_the_5m_cache_and_skips_a_session_whose_previous_is_missing(tmp_path):
    from kotsin_nse.research.dayread_sweep import sweep_history

    days = [d for d in (date(2024, 1, 1) + timedelta(days=i) for i in range(90)) if d.weekday() < 5][:60]
    split, hole = days[45], days[30]  # a 2:1 split: the 5m cache halves, the daily cache is adjusted throughout
    m5, d1 = [], []
    for n, d in enumerate(days):
        scale = 2.0 if d < split else 1.0
        bars = _m5(d, scale, phase=n * 0.7)
        if d != hole:
            m5 += bars
        hi, lo, c = max(b["h"] for b in bars), min(b["l"] for b in bars), bars[-1]["c"]
        adj = 1.0 / scale  # the daily cache on today's scale
        d1.append({"ts": ist_at(d, "00:00"), "o": bars[0]["o"] * adj, "h": hi * adj, "l": lo * adj, "c": c * adj, "v": 75_000.0})
    (tmp_path / "history" / "5m").mkdir(parents=True)
    (tmp_path / "history" / "1d").mkdir(parents=True)
    pd.DataFrame(m5).to_parquet(tmp_path / "history" / "5m" / "X.parquet", index=False)
    pd.DataFrame(d1).to_parquet(tmp_path / "history" / "1d" / "X.parquet", index=False)
    out = tmp_path / "sweep"
    assert sweep_history(tmp_path, out, days[0], days[-1], log=lambda _s: None) > 40
    got = pd.read_parquet(out / "hist" / "X.days.parquet").set_index("day")
    assert days[0].isoformat() not in got.index, "no previous session"
    assert hole.isoformat() not in got.index and days[31].isoformat() not in got.index, "the session after the hole has no previous session in the 5m cache"
    assert days[29].isoformat() in got.index and days[32].isoformat() in got.index
    prev_bars = _m5(days[39], 2.0, phase=39 * 0.7)
    r = got.loc[days[40].isoformat()]
    assert r["pdc"] == pytest.approx(prev_bars[-1]["c"], abs=0.01), "the previous 5m session's last close (cpr rounds to 2 dp)"
    assert r["PDH"] == max(b["h"] for b in prev_bars) and r["PDL"] == min(b["l"] for b in prev_bars)
    assert abs(r["gap_pct"]) < 1.0, "levels on the bars' own scale: no phantom 50 % gap before the split"
    assert r["adr"] == pytest.approx(np.mean([max(b["h"] for b in _m5(days[k], 2.0, phase=k * 0.7)) - min(b["l"] for b in _m5(days[k], 2.0, phase=k * 0.7)) for k in range(0, 40) if days[k] != hole][-50:]), rel=1e-6)
    assert r["adr"] > 6.0, "the 5m sessions' own range (≈ 2 × the adjusted daily cache's)"
    assert r["prev_ret"] == pytest.approx((d1[39]["c"] / d1[38]["c"] - 1) * 100), "the context stays a ratio of the adjusted daily closes"
    after = got.loc[days[50].isoformat()]
    assert after["pdc"] == pytest.approx(_m5(days[49], 1.0, phase=49 * 0.7)[-1]["c"], abs=0.01) and abs(after["gap_pct"]) < 1.0


def test_the_crowd_so_far_counts_distinct_symbols_like_the_days_total():
    from kotsin_nse.research.dayread_stats import crowd_so_far

    sel = pd.DataFrame({
        "day": ["d1"] * 6 + ["d2"] * 2, "kind": ["SUPPORT"] * 5 + ["RESISTANCE"] + ["SUPPORT"] * 2,
        "sym": ["A", "A", "B", "A", "C", "A", "B", "B"], "i": [3, 3, 5, 7, 7, 2, 1, 4],
    })
    sel = sel.sample(frac=1, random_state=3)  # any row order
    seen = crowd_so_far(sel)
    s = sel.assign(seen=seen).sort_values(["day", "kind", "i", "sym"])
    assert s[(s["day"] == "d1") & (s["kind"] == "SUPPORT")]["seen"].tolist() == [1, 1, 2, 3, 3], "A twice at bar 3 is one of the crowd; A again at 7 adds nothing"
    assert s[(s["day"] == "d1") & (s["kind"] == "RESISTANCE")]["seen"].tolist() == [1]
    assert s[s["day"] == "d2"]["seen"].tolist() == [1, 1]
    total = sel.groupby(["day", "kind"])["sym"].transform("nunique")
    assert (seen <= total).all() and (s.groupby(["day", "kind"])["seen"].max() == s.groupby(["day", "kind"])["sym"].nunique()).all(), "by the close the two counts meet"


def test_the_race_scores_who_gets_there_first():
    from kotsin_nse.research.power import race

    c = np.array([100.0, 100.5, 101.2, 99.0])
    h, lo = c + 0.1, c - 0.1
    assert race(h, lo, c, 0, +1, 1.0) == 1.0  # 101.3 ≥ 101 before any low ≤ 99
    assert race(h, lo, c, 0, -1, 1.0) == 0.0
    assert race(h, lo, c, 0, +1, 5.0) == 0.5, "neither by the end"


def test_clusters_chain_levels_within_the_tolerance_and_weigh_them():
    from kotsin_nse.research.power import _cluster_weight

    w = _cluster_weight({"P": (100.0, "D"), "WP": (100.05, "W"), "MS1": (100.12, "M"), "R1": (105.0, "D")}, 0.1)
    assert w == {"P": 6, "WP": 6, "MS1": 6, "R1": 1}


def test_the_chain_score_portfolio_charges_both_legs_only_for_names_that_change():
    from kotsin_nse.research.chainscore import portfolio

    days = ["2024-06-07", "2024-06-14", "2024-06-21"]
    syms = [f"S{i:02d}" for i in range(40)]
    # scores: S00..S07 lowest, S32..S39 highest, the same every week → no turnover after the first week
    scored = pd.DataFrame([{"day": d, "symbol": s, "score": float(i)} for d in days for i, s in enumerate(syms)])
    closes = pd.DataFrame({s: [100.0, 100.0 + (i - 20) * 0.1, 100.0 + (i - 20) * 0.2] for i, s in enumerate(syms)}, index=days)
    p = portfolio(scored, closes, days, cost_bps=10.0)
    assert len(p) == 2
    first, second = p.iloc[0], p.iloc[1]
    assert first["cost_long"] == pytest.approx(5e-4) and first["cost_short"] == pytest.approx(5e-4), "every name entered: one fill each"
    assert second["cost_long"] == 0 and second["cost_short"] == 0, "nothing changed: no cost"
    assert first["ls_net"] == pytest.approx(first["ls_gross"] - 1e-3)
    assert second["ls_net"] == pytest.approx(second["ls_gross"])
    assert first["ls_gross"] > 0, "the top quintile rose more than the bottom"


def test_the_chain_score_fit_ends_before_any_target_reaches_the_test_month(monkeypatch):
    import sklearn.linear_model

    from kotsin_nse.research.chainscore import SIGNALS, TARGET_H, train_cut, walk_forward_scores

    sessions = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2021-03-01", "2022-05-31")]
    pos = {d: i for i, d in enumerate(sessions)}
    first = next(d for d in sessions if d >= "2022-04-01")
    cut = train_cut(sessions, "2022-04-01")
    assert cut is not None and pos[cut] == pos[first] - TARGET_H
    assert all(pos[d] + TARGET_H < pos[first] for d in sessions if d < cut), "every training target ends before the test month"
    assert pos[sessions[pos[cut]]] + TARGET_H == pos[first], "the first excluded row's target is the test month's first session"
    assert train_cut(sessions[:3], "2022-04-01") is None

    # the fit itself: fwd5 carries the row's session position, so the largest target seen tells how late the fit reached
    latest_rows: list[float] = []  # per fit, the position of its latest training row

    class SpyRidge:
        def __init__(self, alpha: float = 1.0) -> None:
            self.coef_ = np.zeros(len(SIGNALS))

        def fit(self, x: np.ndarray, y: np.ndarray) -> SpyRidge:
            latest_rows.append(float(y.max()))
            return self

        def predict(self, x: np.ndarray) -> np.ndarray:
            return np.zeros(len(x))

    monkeypatch.setattr(sklearn.linear_model, "Ridge", SpyRidge)
    rng = np.random.default_rng(1)
    rows = [{"day": d, "symbol": f"S{i:02d}", "fwd1": 0.0, "fwd5": float(pos[d]), **{c: rng.uniform(-0.5, 0.5) for c in SIGNALS}} for d in sessions for i in range(50)]
    scored, weights = walk_forward_scores(pd.DataFrame(rows))
    assert list(weights) == ["2022-04", "2022-05"] and set(scored["month"]) == {"2022-04", "2022-05"}
    assert latest_rows[0] + TARGET_H < pos[first], "the last training row of April's fit ends its target before April"
    assert latest_rows[1] + TARGET_H < pos[next(d for d in sessions if d >= "2022-05-01")]


def test_the_overnight_split_adds_up_and_drops_split_days():
    from kotsin_nse.research.overnight import split

    d1 = pd.DataFrame({"day": ["2024-01-01", "2024-01-02", "2024-01-03"], "o": [100.0, 102.0, 51.0], "h": [101, 103, 52], "l": [99, 101, 50], "c": [100.0, 103.0, 51.5]})
    x = split(d1)
    assert list(x.index) == ["2024-01-02"], "no previous close for the first day; the 1:2 split day is dropped"
    assert x.loc["2024-01-02", "overnight"] == pytest.approx(0.02)
    assert (1 + x.loc["2024-01-02", "overnight"]) * (1 + x.loc["2024-01-02", "intraday"]) == pytest.approx(1 + x.loc["2024-01-02", "close_to_close"])


def test_nightpick_nights_charge_a_round_trip_per_name_and_rank_within_the_session():
    from kotsin_nse.research.nightpick import nights

    rows = []
    for i in range(40):
        rows.append({"day": "2024-06-07", "symbol": f"S{i:02d}", "score": float(i), "overnight": 0.001 * (i - 20)})
    n = nights(pd.DataFrame(rows), cost_bps=10.0).set_index("day")
    r = n.loc["2024-06-07"]
    # pct rank >= 0.8 of 40 names takes ranks 32..40: nine names (the boundary is inclusive), scores 31..39
    assert r["n_top"] == 9 and r["top_gross"] == pytest.approx(0.015) and r["bottom_gross"] == pytest.approx(-0.0165)
    assert r["top_net"] == pytest.approx(0.015 - 1e-3) and r["spread"] == pytest.approx(0.0315)
