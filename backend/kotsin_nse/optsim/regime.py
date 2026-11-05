"""The volatility regime from the bhavcopy, one row a session: the index's monthly and weekly at-the-money implied
volatility, their spread (the term structure), the 25-delta skew (put IV minus call IV, monthly), the straddle as a
share of the future, the days to each expiry, and the percentile of the monthly IV against the trailing year — the
go / no-go the playbooks read ("weekly got hard in 2022", "VIX up 30 % in May") as fields rather than memory. The
history is kept in ``<root>/history.parquet`` and extended one day at a time; ``latest.json`` is the current row.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from .chain import Store, dte


def row(store: Store, day: str) -> dict[str, Any] | None:
    d = store.get(day)
    ms, ws = d.monthlies, d.weeklies
    if not ms:
        return None
    m = ms[0] if dte(ms[0], day) > 2 or len(ms) == 1 else ms[1]
    out: dict[str, Any] = {"day": day, "symbol": store.symbol, "monthly": m, "dte_m": dte(m, day)}
    s = d.straddle(m)
    f = d.future(m)
    if s:
        out.update({"atm_m": s[0], "straddle_m": round(s[1], 1), "iv_m": round(s[2], 2), "straddle_m_pct": round(s[1] / f * 100, 2) if f else None})
    w = next((e for e in ws if dte(e, day) > 1), None)
    if w:
        sw = d.straddle(w)
        out.update({"weekly": w, "dte_w": dte(w, day)})
        if sw:
            out.update({"iv_w": round(sw[2], 2), "straddle_w": round(sw[1], 1)})
            if "iv_m" in out and out["iv_m"] == out["iv_m"]:
                out["term_w_minus_m"] = round(sw[2] - out["iv_m"], 2)
    kp, kc = d.strike_by_delta(m, "PE", 0.25), d.strike_by_delta(m, "CE", 0.25)
    if kp is not None and kc is not None:
        ip, ic = d.iv(m, kp, "PE"), d.iv(m, kc, "CE")
        if ip is not None and ic is not None:
            out.update({"skew25": round((ip - ic) * 100, 2), "put25": kp, "call25": kc})
    out["F"] = round(f, 1) if f else None
    return out


def history(store: Store, root: Path, *, upto: str | None = None) -> pd.DataFrame:
    """The regime rows for every bhavcopy day, cached and extended."""
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"history-{store.symbol}.parquet"
    H = pd.read_parquet(p) if p.exists() else pd.DataFrame()
    done = set(H["day"]) if len(H) else set()
    rows = []
    for day in store.days:
        if day in done or (upto and day > upto):
            continue
        r = row(store, day)
        if r:
            rows.append(r)
    if rows:
        H = pd.concat([H, pd.DataFrame(rows)]).sort_values("day").drop_duplicates("day", keep="last").reset_index(drop=True)
        H.to_parquet(p)
    return H


def with_percentiles(H: pd.DataFrame, window: int = 250) -> pd.DataFrame:
    H = H.sort_values("day").reset_index(drop=True)
    for col in ("iv_m", "iv_w", "skew25", "term_w_minus_m"):
        if col in H:
            H[f"{col}_pct"] = H[col].rolling(window, min_periods=60).apply(lambda w: (w[:-1] < w[-1]).mean() * 100, raw=True)
    H["rv20"] = None
    return H


def latest(store: Store, root: Path) -> dict[str, Any] | None:
    H = with_percentiles(history(store, root))
    if H.empty:
        return None
    r = H.iloc[-1].to_dict()
    r = {k: (None if pd.isna(v) else (round(float(v), 2) if isinstance(v, float) else v)) for k, v in r.items()}
    (root / f"latest-{store.symbol}.json").write_text(json.dumps(r, default=str, indent=1))
    return r


__all__ = ["history", "latest", "row", "with_percentiles"]
