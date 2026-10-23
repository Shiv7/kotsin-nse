"""Market regimes (pre-registered: docs/research/2026-10-23-prereg-unsupervised.md §2).

Per session D, from what is known after its close: breadth (% of F&O stocks up, % above their 20-day
average), dispersion (σ of the stocks' returns), mean |return|, NIFTY's return and 20-day realised
vol, NIFTY's ATM IV (nearest weekly, ``optsignals._index_row``), IV − realised vol, the IV change,
NIFTY's PCR and the change in all stock-futures OI. A Gaussian mixture (k = 4) on the standardised
readings, **fitted only on sessions before** each quarter it labels (from one year of warm-up); the
clusters of each refit are matched to the previous fit's by their means (Hungarian), so "regime 2"
keeps meaning the same thing.

Tests (the regime known at D's close applies to D + 1):

* **R1** NIFTY's next-session |return| ÷ the move its IV priced. Fairly priced, E|r| = σ·√(2/π) for a
  normal move, so the ratio is scaled by that: **1.0 = fair**, below = the options overpriced the move
  (sellers' regime), above = underpriced (buyers').
* **R2** the intraday rules already tested, their average R by the regime at the open.
* **R3** the next session's one-day reversal: IC of −return(D) against D + 1's open → close, by regime.
"""

from __future__ import annotations

import json
import math
import time as _time
import uuid
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ..config import Settings
from .history import HistoryStore
from .optsignals import HALVES, HOLDOUT_START, _weekly_t

K = 4
FEATURES = ("breadth_up", "above20", "dispersion", "mean_abs", "nifty_ret", "rv20", "iv", "iv_rv", "d_iv", "pcr", "d_fut_oi")
FAIR = math.sqrt(2 / math.pi)


def market_table(settings: Settings) -> pd.DataFrame:
    """One row per session: the readings above and the next session's outcomes."""
    from ..market.fo_universe import load as load_members

    fm, _ = load_members(settings.data_dir)
    store = HistoryStore(settings.data_dir / "history")
    rets, above, fwd = [], [], []
    for sym in store.symbols("1d"):
        if sym in ("NIFTY", "BANKNIFTY"):
            continue
        d1 = store.load(sym, "1d").sort_values("ts").drop_duplicates("ts")
        if len(d1) < 30:
            continue
        day = [(pd.Timestamp(t, unit="s") + pd.Timedelta(hours=5, minutes=30)).date().isoformat() for t in d1["ts"]]
        c, o = d1["c"].to_numpy(float), d1["o"].to_numpy(float)
        r = pd.Series(c / np.r_[np.nan, c[:-1]] - 1, index=day)
        mem = pd.Series([fm.covers(date.fromisoformat(x)) and fm.member(sym, date.fromisoformat(x)) for x in day], index=day)
        rets.append(r.where(mem & (r.abs() < 0.25)).rename(sym))  # a split or bonus is not a return
        above.append(pd.Series(c > pd.Series(c).rolling(20).mean().to_numpy(), index=day).where(mem).rename(sym))
        fwd.append(pd.Series(np.r_[c[1:], np.nan] / np.r_[o[1:], np.nan] - 1, index=day).where(mem & (r.abs() < 0.25)).rename(sym))
    R, A, F = pd.concat(rets, axis=1), pd.concat(above, axis=1).astype(float), pd.concat(fwd, axis=1)
    m = pd.DataFrame({"breadth_up": (R > 0).sum(axis=1) / R.notna().sum(axis=1), "above20": A.mean(axis=1),
                      "dispersion": R.std(axis=1), "mean_abs": R.abs().mean(axis=1)})
    nifty = store.load("NIFTY", "1d").sort_values("ts").drop_duplicates("ts")
    nday = [(pd.Timestamp(t, unit="s") + pd.Timedelta(hours=5, minutes=30)).date().isoformat() for t in nifty["ts"]]
    nc = pd.Series(nifty["c"].to_numpy(float), index=nday)
    lr = np.log(nc / nc.shift(1))
    m["nifty_ret"] = lr
    m["rv20"] = lr.rolling(20).std() * math.sqrt(252)
    m["nifty_next_abs"] = lr.shift(-1).abs()
    mk = pd.read_parquet(settings.data_dir / "oi_daily" / "_market.parquet").drop_duplicates("day").set_index("day").sort_index()
    m = m.join(mk[["nifty_iv", "nifty_pcr", "stock_fut_oi"]], how="inner")
    m["iv"] = m["nifty_iv"]
    m["iv_rv"] = m["iv"] - m["rv20"]
    m["d_iv"] = m["iv"].diff()
    m["pcr"] = m["nifty_pcr"]
    m["d_fut_oi"] = np.log(m["stock_fut_oi"] / m["stock_fut_oi"].shift(1))
    m["r1_ratio"] = m["nifty_next_abs"] / (m["iv"] / math.sqrt(252) * FAIR)
    # R3: the IC of −return(D) against D + 1's open → close, across stocks
    ic = []
    for d in m.index:
        if d in R.index and d in F.index:
            x = pd.concat([-R.loc[d], F.loc[d]], axis=1).dropna()
            ic.append(float(x.iloc[:, 0].rank().corr(x.iloc[:, 1].rank())) if len(x) >= 30 else np.nan)
        else:
            ic.append(np.nan)
    m["r3_reversal_ic"] = ic
    return m.sort_index()


def label(m: pd.DataFrame, start: date) -> pd.Series:
    """Regime per session, from a mixture fitted on sessions before each quarter (warm-up: a year)."""
    from scipy.optimize import linear_sum_assignment
    from sklearn.mixture import GaussianMixture

    x = m[list(FEATURES)].replace([np.inf, -np.inf], np.nan).dropna()
    q = pd.Series([d[:4] + "Q" + str((int(d[5:7]) - 1) // 3 + 1) for d in x.index], index=x.index)
    out = pd.Series(np.nan, index=x.index)
    ref: np.ndarray | None = None
    for qq in sorted(q.unique()):
        first = q.index[q == qq].min()
        if first < start.isoformat():
            continue
        train = x[x.index < first]
        if len(train) < 200:
            continue
        mu, sd = train.mean(), train.std()
        g = GaussianMixture(K, covariance_type="full", random_state=0, n_init=3).fit(((train - mu) / sd).to_numpy())
        means = g.means_
        perm = np.arange(K)
        if ref is not None:  # match this fit's clusters to the last fit's
            cost = ((means[:, None, :] - ref[None, :, :]) ** 2).sum(axis=2)
            rows, cols = linear_sum_assignment(cost)
            perm = np.empty(K, int)
            perm[rows] = cols
        ref = means[np.argsort(perm)] if ref is not None else means
        lab = g.predict(((x[q == qq] - mu) / sd).to_numpy())
        out[q == qq] = perm[lab]
    return out


def _by_regime(values: pd.Series, reg: pd.Series) -> dict[str, Any]:
    out = {}
    for k in range(K):
        v = values[reg.reindex(values.index) == k].dropna()
        t, weeks = _weekly_t(v - (1.0 if values.name == "r1_ratio" else 0.0))
        months = v.groupby([d[:7] for d in v.index]).mean()
        out[str(k)] = {"n": len(v), "mean": round(float(v.mean()), 4) if len(v) else None, "t_vs_null": round(t, 2) if t else None, "weeks": weeks,
                       "halves": [round(float(v[(v.index >= a.isoformat()) & (v.index <= b.isoformat())].mean()), 4) for a, b in HALVES],
                       "months": len(months)}
    return out


def _trades_r(settings: Settings, reg: pd.Series) -> dict[str, Any]:
    """R2: each tested intraday rule's average R by the regime of the session before."""
    root = settings.data_dir / "obtests"
    prev = reg.shift(1)  # the regime known at the open of a day = the label of the session before
    out: dict[str, Any] = {}
    for f in sorted(root.glob("*.json")):
        if not f.name.startswith(("confirm-", "dbcpr-", "mtf-29fe72e400")):
            continue
        d = json.loads(f.read_text())
        tr = pd.DataFrame(d.get("trades", []))
        if tr.empty or "r" not in tr:
            continue
        day = tr["day"] if "day" in tr else tr["signal_ts"].map(lambda t: (pd.Timestamp(t, unit="s") + pd.Timedelta(hours=5, minutes=30)).date().isoformat())
        tr = tr.assign(reg=day.map(prev), day=day).dropna(subset=["reg"])
        name = f"{f.stem} ({d.get('label') or d.get('params', {}).get('setup', '')})"
        out[name] = {}
        for k in range(K):
            g = tr[tr["reg"] == k]
            if len(g) < 30:
                continue
            t, _w = _weekly_t(g.groupby("day")["r"].mean())
            out[name][str(k)] = {"n": len(g), "avg_r": round(float(g["r"].mean()), 3), "t_by_week": round(t, 2) if t else None}
    return out


def run(settings: Settings, start: date = date(2022, 4, 1), end: date = date(2025, 9, 30), label_: str = "") -> dict[str, Any]:
    if end >= HOLDOUT_START:
        raise ValueError(f"{end} is inside the holdout (from {HOLDOUT_START})")
    t0 = _time.time()
    m = market_table(settings)
    m = m[m.index <= end.isoformat()]
    reg = label(m, start)
    m["regime"] = reg
    lab = m.dropna(subset=["regime"])
    profile = {str(k): {c: round(float(lab.loc[lab["regime"] == k, c].mean()), 4) for c in FEATURES} | {"sessions": int((lab["regime"] == k).sum())}
               for k in range(K)}
    out = {"id": f"regime-{uuid.uuid4().hex[:10]}", "kind": "regimes", "label": label_, "range": [start.isoformat(), end.isoformat()],
           "profile": profile,
           "R1_nifty_move_vs_implied": _by_regime(lab["r1_ratio"].rename("r1_ratio"), lab["regime"]),
           "R1_all": round(float(lab["r1_ratio"].mean()), 3),
           "R3_reversal_ic": _by_regime(lab["r3_reversal_ic"].rename("r3"), lab["regime"]),
           "R2_rules_by_regime": _trades_r(settings, m["regime"]),
           "seconds": round(_time.time() - t0, 1)}
    root = settings.data_dir / "obtests"
    (root / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    m.to_parquet(settings.data_dir / "oi_daily" / "_regimes.parquet")
    return out


__all__ = ["FEATURES", "K", "label", "market_table", "run"]
