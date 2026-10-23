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
from kotsin_nse.research.optsignals import day_features
from kotsin_nse.research.regimes import FEATURES, K, label

D = date(2025, 3, 3)


def _bhav(put_iv: float = 0.25, otm_put_iv: float = 0.30) -> pd.DataFrame:
    f, exp = 1000.0, "2025-03-27"
    t = (date(2025, 3, 27) - D).days / 365
    rows = [{"symbol": "X", "kind": "FUTSTK", "expiry": exp, "strike": 0.0, "option_type": "", "settle": f, "oi": 1e6, "oi_change": 0.0, "volume": 100.0}]
    for k in (900.0, 950.0, 1000.0, 1050.0):
        rows.append({"symbol": "X", "kind": "OPTSTK", "expiry": exp, "strike": k, "option_type": "CE", "settle": black76(f, k, t, 0.25, True),
                     "oi": 1e5, "oi_change": 1000.0 if k > 1020 else 0.0, "volume": 50.0})
        iv = otm_put_iv if k == 950.0 else put_iv
        rows.append({"symbol": "X", "kind": "OPTSTK", "expiry": exp, "strike": k, "option_type": "PE", "settle": black76(f, k, t, iv, False),
                     "oi": 1e5, "oi_change": 5000.0 if k < 980 else 0.0, "volume": 50.0})
    return pd.DataFrame(rows)


def test_option_features_read_the_iv_spread_skew_and_the_otm_build():
    (r,), _ = day_features(_bhav(put_iv=0.27, otm_put_iv=0.33), D)
    assert r["iv_c"] == pytest.approx(0.25, abs=1e-3) and r["iv_p"] == pytest.approx(0.27, abs=1e-3)
    assert r["iv_otm_p"] == pytest.approx(0.33, abs=1e-3), "the traded put nearest 0.95 F"
    assert (r["otm_put_add"], r["otm_call_add"]) == (10_000.0, 1_000.0)


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
