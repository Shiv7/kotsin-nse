"""Post-results drift (pre-registered: docs/research/2026-10-23-prereg-unsupervised.md §7).

Part one — **the event calendar**: when did each company report. NSE's corporate-filings APIs, fetched
month by month into ``data-live/events/raw/`` (public filings, no login; the site wants a browser's
headers and the cookies its listing page sets) and reduced to two tables:

* ``results.parquet`` — one row per results filing with its **broadcast timestamp** (IST): the old
  financial-results API up to March 2025, the integrated-filing API from the quarter ended March 2025
  (SEBI's integrated filing replaced the old form; the old endpoint has next to nothing after that).
* ``board.parquet`` — board meetings whose purpose includes "Financial Results" (the date only; a
  cross-check for the first table, not a substitute: it has no time of day).

Part two — **the study** (``study()``), exactly as pre-registered: the event session E from the
broadcast time, the market-adjusted reaction as a z-score of the stock's own volatility, the drift to
E+k, and the one rule — |z| ≥ 2, with the reaction, close(E) → close(E+5), stock futures, costs once.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import structlog

from ..config import Settings
from ..market.session import from_ist
from .optsignals import HOLDOUT_START

log = structlog.get_logger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
LISTING = "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"
#: name → (url template with {a} {b} as dd-mm-yyyy, first month, last month or None = today)
SOURCES: dict[str, tuple[str, date, date | None]] = {
    "results": ("https://www.nseindia.com/api/corporates-financial-results?index=equities&period=Quarterly&from_date={a}&to_date={b}",
                date(2021, 1, 1), date(2025, 3, 31)),
    # the audited Q4 + annual filing of a company that files it in place of a "Quarterly" Q4 (same toDate)
    "annual": ("https://www.nseindia.com/api/corporates-financial-results?index=equities&period=Annual&from_date={a}&to_date={b}",
               date(2021, 1, 1), date(2025, 3, 31)),
    "board": ("https://www.nseindia.com/api/corporate-board-meetings?index=equities&from_date={a}&to_date={b}", date(2021, 1, 1), None),
    # paginated (page / size / totalCount); one page of 5,000 holds any month (May 2025: 3,479 rows)
    "integrated": ("https://www.nseindia.com/api/integrated-filing-results?index=equities&from_date={a}&to_date={b}"
                   "&type=Integrated%20Filing-%20Financials&page=1&size=5000", date(2025, 3, 1), None),
}
NSE_TS = "%d-%b-%Y %H:%M:%S"


def months(start: date, end: date) -> list[tuple[date, date]]:
    out, d = [], start.replace(day=1)
    while d <= end:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((d, min(nxt - timedelta(days=1), end)))
        d = nxt
    return out


def _ddmmyyyy(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def fetch(settings: Settings, pause: float = 1.5, sources: dict[str, tuple[str, date, date | None]] | None = None) -> dict[str, int]:
    """Download every missing month of every source (a month in the past that is already on disk is
    not fetched again; the current month always is). Returns files fetched per source."""
    raw = settings.data_dir / "events" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    today = date.today()
    counts: dict[str, int] = {}
    with httpx.Client(headers=HEADERS, timeout=90.0, follow_redirects=True) as http:
        http.get(LISTING, headers={"Accept": "text/html,application/xhtml+xml"})  # the cookies the API wants
        for name, (tpl, first, last) in (sources or SOURCES).items():
            counts[name] = 0
            for a, b in months(first, last or today):
                path = raw / f"{name}_{a:%Y-%m}.json"
                if path.exists() and b < today.replace(day=1):
                    continue
                url = tpl.format(a=_ddmmyyyy(a), b=_ddmmyyyy(b))
                for attempt in range(3):
                    try:
                        r = http.get(url, headers={"Referer": LISTING})
                        r.raise_for_status()
                        json.loads(r.text)  # must be JSON, or the block page came back
                        path.write_text(r.text)
                        counts[name] += 1
                        break
                    except (httpx.HTTPError, ValueError) as exc:
                        log.warning("events.fetch.retry", source=name, month=f"{a:%Y-%m}", attempt=attempt, error=str(exc))
                        time.sleep(5.0 * (attempt + 1))
                        http.get(LISTING, headers={"Accept": "text/html,application/xhtml+xml"})
                time.sleep(pause)
    return counts


def _ts(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return from_ist(datetime.strptime(text, NSE_TS))
    except ValueError:
        return None


def _rows(path: Path) -> list[Any]:
    d = json.loads(path.read_text())
    return d.get("data", []) if isinstance(d, dict) else d


def build_tables(settings: Settings) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``results.parquet`` and ``board.parquet`` from the raw files (see the module docstring)."""
    raw = settings.data_dir / "events" / "raw"
    res: list[dict[str, Any]] = []
    for src in ("results", "annual"):
        for p in sorted(raw.glob(f"{src}_*.json")):
            for x in _rows(p):
                res.append({"symbol": x.get("symbol"), "broadcast_ts": _ts(x.get("broadCastDate")), "period_end": x.get("toDate"),
                            "relating_to": x.get("relatingTo"), "audited": x.get("audited"), "consolidated": x.get("consolidated"),
                            "source": src})
    for p in sorted(raw.glob("integrated_*.json")):
        for x in _rows(p):
            res.append({"symbol": x.get("symbol"), "broadcast_ts": _ts(x.get("broadcast_Date")), "period_end": x.get("qe_Date"),
                        "relating_to": x.get("type_Sub"), "audited": x.get("audited"), "consolidated": x.get("consolidated"),
                        "source": "integrated"})
    results = pd.DataFrame(res).dropna(subset=["symbol", "broadcast_ts"])
    if len(results):
        results["broadcast_ist"] = [(pd.Timestamp(t, unit="s") + pd.Timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M:%S") for t in results["broadcast_ts"]]
        results = results.drop_duplicates(["symbol", "broadcast_ts", "consolidated"]).sort_values(["symbol", "broadcast_ts"]).reset_index(drop=True)
    bd: list[dict[str, Any]] = []
    for p in sorted(raw.glob("board_*.json")):
        for x in _rows(p):
            purpose = x.get("bm_purpose") or ""
            if "result" not in purpose.lower():
                continue
            try:
                day = datetime.strptime(x["bm_date"], "%d-%b-%Y").date().isoformat()
            except (KeyError, ValueError):
                continue
            bd.append({"symbol": x.get("bm_symbol"), "meeting_day": day, "purpose": purpose, "intimated_ts": _ts(x.get("bm_timestamp"))})
    board = pd.DataFrame(bd).dropna(subset=["symbol"]).drop_duplicates(["symbol", "meeting_day"]).reset_index(drop=True)
    out = settings.data_dir / "events"
    results.to_parquet(out / "results.parquet", index=False)
    board.to_parquet(out / "board.parquet", index=False)
    return results, board




# -- part two: the study ------------------------------------------------------------------------------

HALVES = (("2021-04-01", "2023-06-30"), ("2023-07-01", "2025-09-30"))
Z_MIN = 2.0
HOLD = 5
KS = (1, 2, 3, 5, 10, 20)
SIGMA_N = 60
RUNUP_N = 5
CLOSE_HM = "15:30"
OPEN_HM = "09:15"


def event_sessions(results: pd.DataFrame, sessions: list[str]) -> pd.DataFrame:
    """One row per (symbol, quarter): the earliest broadcast of the quarter's filings, its event
    session E (the first session whose close follows it) and whether it came during market hours."""
    r = results.copy()
    r["period_end"] = r["period_end"].astype(str).str.upper()
    r = r.sort_values("broadcast_ts").drop_duplicates(["symbol", "period_end"], keep="first")
    sess = sorted(sessions)
    idx = pd.Index(sess)
    rows = []
    for x in r.itertuples():
        ist = x.broadcast_ist
        day, hm = ist[:10], ist[11:16]
        if day in idx and hm < CLOSE_HM:
            e, in_hours = day, hm >= OPEN_HM
        else:
            pos = idx.searchsorted(day, side="right")
            if pos >= len(sess):
                continue
            e, in_hours = sess[pos], False
        rows.append({"symbol": x.symbol, "period_end": x.period_end, "broadcast_ist": ist, "E": e, "in_hours": in_hours, "source": x.source})
    out = pd.DataFrame(rows).sort_values(["symbol", "E"])
    # a revision or a second filing inside the same few sessions is the same event
    keep = []
    last: dict[str, str] = {}
    for x in out.itertuples():
        prev = last.get(x.symbol)
        if prev is not None and idx.get_loc(x.E) - idx.get_loc(prev) < 3:
            keep.append(False)
            continue
        keep.append(True)
        last[x.symbol] = x.E
    return out[pd.Series(keep, index=out.index)].reset_index(drop=True)


def _weekly_stats(s: pd.Series) -> dict[str, Any]:
    """``s`` in bps, indexed by the event session; the unit of inference is the week of E."""
    import math

    s = s.dropna()
    if s.empty:
        return {"n": 0}
    wk = s.groupby(["{}-W{:02d}".format(*date.fromisoformat(d).isocalendar()[:2]) for d in s.index]).sum()
    sd = wk.std(ddof=1)
    t = float(wk.mean() / (sd / math.sqrt(len(wk)))) if sd > 0 and len(wk) > 1 else None
    return {"n": len(s), "avg_bps": round(float(s.mean()), 2), "median_bps": round(float(s.median()), 2), "t_by_week": round(t, 2) if t else None,
            "weeks": len(wk), "weeks_positive": round(float((wk > 0).mean()) * 100, 1),
            "halves": [round(float(s[(s.index >= a) & (s.index <= b)].mean()), 2) if ((s.index >= a) & (s.index <= b)).any() else None for a, b in HALVES],
            "by_year_bps": s.groupby(s.index.str[:4]).mean().round(2).to_dict(), "hit_rate": round(float((s > 0).mean()) * 100, 1)}


def event_table(settings: Settings, ev: pd.DataFrame, end: date) -> pd.DataFrame:
    """The reaction, its z, the run-up, the drifts and the raw forward returns for every event of an
    F&O member (point-in-time), E ≤ ``end`` − 20 sessions so every drift is observable inside the window."""
    import numpy as np

    from ..market.fo_universe import load as load_members
    from .history import HistoryStore
    from .regimes import _daily

    store = HistoryStore(settings.data_dir / "history")
    fm, _ = load_members(settings.data_dir)
    nifty = _daily(store, "NIFTY").set_index("day")
    sessions = [d for d in nifty.index if d <= end.isoformat()]
    n_c = nifty["c"].reindex(sessions)
    n_ret = n_c.pct_change()
    cutoff = sessions[-max(KS) - 1]
    iv_p = settings.data_dir / "oi_daily" / "_optsignals.parquet"
    iv = pd.read_parquet(iv_p, columns=["symbol", "day", "iv_c", "iv_p"]) if iv_p.exists() else None
    iv_atm = iv.assign(iv=(iv["iv_c"] + iv["iv_p"]) / 2).set_index(["symbol", "day"])["iv"] if iv is not None else None
    rows = []
    for sym, g in ev.groupby("symbol"):
        try:
            d = _daily(store, sym)
        except FileNotFoundError:
            continue
        if len(d) < SIGMA_N + 30:
            continue
        c = d.set_index("day")["c"].reindex(sessions)
        o = d.set_index("day")["o"].reindex(sessions)
        ret = c.pct_change()
        adj = ret - n_ret
        sigma = adj.rolling(SIGMA_N, min_periods=40).std().shift(1)
        pos_of = {s: i for i, s in enumerate(sessions)}
        cv, ov, nv, adjv, retv = c.to_numpy(float), o.to_numpy(float), n_c.to_numpy(float), adj.to_numpy(float), ret.to_numpy(float)
        for x in g.itertuples():
            e = x.E
            if e not in pos_of or e > cutoff or e < "2021-04-01":
                continue
            i = pos_of[e]
            if not (fm.covers(date.fromisoformat(e)) and fm.member(sym, date.fromisoformat(e))):
                continue
            r_e, sg = adjv[i], sigma.iloc[i]
            if not (np.isfinite(r_e) and np.isfinite(sg)) or sg <= 0 or abs(retv[i]) > 0.5:
                continue
            row: dict[str, Any] = {"symbol": sym, "E": e, "in_hours": bool(x.in_hours), "broadcast_ist": x.broadcast_ist, "period_end": x.period_end,
                                   "r_e": float(r_e), "raw_e": float(retv[i]), "gap_e": float(ov[i] / cv[i - 1] - 1), "sigma": float(sg), "z": float(r_e / sg),
                                   "runup": float(np.nansum(adjv[i - RUNUP_N:i]))}
            for k in KS:
                j = i + k
                if j < len(cv) and np.isfinite(cv[j]):
                    row[f"drift_{k}"] = float((cv[j] / cv[i] - 1) - (nv[j] / nv[i] - 1))
                    row[f"raw_{k}"] = float(cv[j] / cv[i] - 1)
                else:
                    row[f"drift_{k}"] = row[f"raw_{k}"] = float("nan")
            if iv_atm is not None and (sym, sessions[i - 1]) in iv_atm.index:
                v = float(iv_atm.loc[(sym, sessions[i - 1])])
                row["implied_1d"] = v * (1 / 252) ** 0.5 if np.isfinite(v) else float("nan")
            else:
                row["implied_1d"] = float("nan")
            rows.append(row)
    return pd.DataFrame(rows)


def study(settings: Settings, end: date = date(2025, 9, 30), label: str = "") -> dict[str, Any]:
    """§7 as written. Writes ``obtests/events-<id>.json`` and the event table beside it."""
    import uuid

    import numpy as np

    from ..config import Segment
    from ..domain import Instrument, InstrumentKind
    from ..risk.costs import CostModel

    if end >= HOLDOUT_START:
        raise ValueError(f"{end} is inside the holdout (from {HOLDOUT_START})")
    t0 = time.time()
    root = settings.data_dir / "events"
    results = pd.read_parquet(root / "results.parquet")
    board = pd.read_parquet(root / "board.parquet") if (root / "board.parquet").exists() else pd.DataFrame()
    from .history import HistoryStore
    from .regimes import _daily

    sessions = [d for d in _daily(HistoryStore(settings.data_dir / "history"), "NIFTY")["day"] if d <= end.isoformat()]
    ev = event_sessions(results[results["broadcast_ist"] < f"{end.isoformat()} 23:59:59"], sessions)
    t = event_table(settings, ev, end)
    inst = Instrument("BT", "BT", Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=1)
    cost_bps = CostModel(settings).round_trip(inst, 1000.0, 1000.0, 500).total / 500_000 * 1e4 + 10.0
    t["side"] = np.sign(t["z"])
    sel = t[t["z"].abs() >= Z_MIN].copy()
    for k in KS:
        sel[f"gross_{k}"] = sel["side"] * sel[f"raw_{k}"] * 1e4
        sel[f"net_{k}"] = sel[f"gross_{k}"] - cost_bps
        sel[f"adj_{k}"] = sel["side"] * sel[f"drift_{k}"] * 1e4
    si = sel.set_index("E")
    primary = _weekly_stats(si[f"net_{HOLD}"])
    primary["passes"] = bool(primary.get("t_by_week") and primary["t_by_week"] >= 2.0 and primary["avg_bps"] > 0
                             and primary["weeks_positive"] >= 55 and all(h is not None and h > 0 for h in primary["halves"]))
    # the cross-check: board-meeting result dates of members with no filing within 3 sessions
    missing = None
    if len(board) and len(t):
        from ..market.fo_universe import load as load_members

        fm, _ = load_members(settings.data_dir)
        have = {(sym, e) for sym, e in zip(t["symbol"], t["E"], strict=True)}
        idx = pd.Index(sessions)
        b = board[(board["meeting_day"] >= "2021-04-01") & (board["meeting_day"] <= end.isoformat())]
        n_member = miss = 0
        for x in b.itertuples():
            d = date.fromisoformat(x.meeting_day)
            if not (fm.covers(d) and fm.member(x.symbol, d)):
                continue  # not in F&O on that day: no event was expected
            n_member += 1
            p = idx.searchsorted(x.meeting_day)
            near = {sessions[j] for j in range(max(0, p - 1), min(len(sessions), p + 4))}
            if not any((x.symbol, e) in have for e in near):
                miss += 1
        missing = {"board_result_dates_of_members": n_member, "without_a_filing_nearby": miss}
    bins = {"1-2": t[(t["z"].abs() >= 1) & (t["z"].abs() < 2)], "2-3": t[(t["z"].abs() >= 2) & (t["z"].abs() < 3)], ">3": t[t["z"].abs() >= 3]}
    out: dict[str, Any] = {
        "id": f"events-{uuid.uuid4().hex[:10]}", "kind": "events", "label": label, "end": end.isoformat(), "cost_bps_round_trip": round(cost_bps, 2),
        "filings": len(results), "events_all_members": len(t), "events_selected_abs_z_ge_2": len(sel),
        "events_in_hours_share": round(float(t["in_hours"].mean()) * 100, 1) if len(t) else None,
        "reaction_abs_bps": {"all": round(float(t["raw_e"].abs().mean()) * 1e4, 1) if len(t) else None, "selected": round(float(sel["raw_e"].abs().mean()) * 1e4, 1) if len(sel) else None},
        "primary_net_hold5": primary,
        "gross_hold5": _weekly_stats(si[f"gross_{HOLD}"]), "market_adjusted_hold5": _weekly_stats(si[f"adj_{HOLD}"]),
        "by_hold_net": {str(k): _weekly_stats(si[f"net_{k}"]) for k in KS},
        "by_hold_market_adjusted": {str(k): round(float(si[f"adj_{k}"].mean()), 2) for k in KS},
        "long_leg_net": _weekly_stats(si.loc[si["side"] > 0, f"net_{HOLD}"]), "short_leg_net": _weekly_stats(si.loc[si["side"] < 0, f"net_{HOLD}"]),
        "in_hours_net": _weekly_stats(si.loc[si["in_hours"], f"net_{HOLD}"]), "after_hours_net": _weekly_stats(si.loc[~si["in_hours"], f"net_{HOLD}"]),
        "by_abs_z_bin_market_adjusted_drift_bps": {name: {str(k): round(float((np.sign(g["z"]) * g[f"drift_{k}"]).mean() * 1e4), 2) for k in KS} | {"n": len(g)}
                                                   for name, g in bins.items()},
        "mirror_fade_net": _weekly_stats(-si[f"gross_{HOLD}"] - cost_bps),
        "runup_signed_bps": {"selected": round(float((sel["side"] * sel["runup"]).mean() * 1e4), 2) if len(sel) else None,
                             "all": round(float((t["side"] * t["runup"]).mean() * 1e4), 2) if len(t) else None},
        "implied_vs_realised": None, "coverage_cross_check": missing, "seconds": round(time.time() - t0, 1),
    }
    iv_ok = t[np.isfinite(t["implied_1d"]) & (t["implied_1d"] > 0)]
    if len(iv_ok):
        ratio = iv_ok["raw_e"].abs() / iv_ok["implied_1d"]
        out["implied_vs_realised"] = {"n": len(iv_ok), "mean_abs_move_bps": round(float(iv_ok["raw_e"].abs().mean()) * 1e4, 1),
                                      "mean_implied_1d_bps": round(float(iv_ok["implied_1d"].mean()) * 1e4, 1),
                                      "realised_over_implied_median": round(float(ratio.median()), 2), "share_realised_above_implied": round(float((ratio > 1).mean()) * 100, 1)}
    reg_p = settings.data_dir / "oi_daily" / "_regimes.parquet"
    if reg_p.exists() and len(sel):
        reg = pd.read_parquet(reg_p)["regime"].reindex(si.index)
        out["by_regime_at_E_post_hoc"] = {str(int(k)): {"n": len(g), "net_bps": round(float(g[f"net_{HOLD}"].mean()), 2)}
                                          for k, g in si.groupby(reg.values) if k == k}
    obt = settings.data_dir / "obtests"
    obt.mkdir(exist_ok=True)
    (obt / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    t.to_parquet(obt / f"{out['id']}-events.parquet", index=False)
    return out




def execution_check(settings: Settings, events_parquet: Path, start: str = "2024-01-01", cost_bps: float = 14.62) -> dict[str, Any]:
    """§7's execution check: select **and** enter on E's 15:25 bar, exit on E+k's 15:25 bar, every price from
    the 5m cache (it is not bonus/split adjusted while the daily cache is, so the two are never mixed); the
    daily-close rule on the same events beside it. Only the stocks with 5m data (today's F&O list)."""
    import math

    import numpy as np

    from ..market.session import ist_at
    from .history import HistoryStore
    from .regimes import _daily

    store = HistoryStore(settings.data_dir / "history")
    t = pd.read_parquet(events_parquet)
    t = t[t["E"] >= start]
    sessions = list(_daily(store, "NIFTY")["day"])
    pos = {d: i for i, d in enumerate(sessions)}

    def last_px(c5: pd.Series, day: str) -> float:
        ts = ist_at(date.fromisoformat(day), "15:25")
        if ts in c5.index:
            return float(c5.loc[ts])
        w = c5[(c5.index >= ist_at(date.fromisoformat(day), "15:00")) & (c5.index < ts + 300)]
        return float(w.iloc[-1]) if len(w) else float("nan")

    n5 = store.load("NIFTY", "5m").set_index("ts")["c"]
    cache: dict[str, pd.Series | None] = {}
    rows = []
    for x in t.itertuples():
        if x.symbol not in cache:
            m5 = store.load(x.symbol, "5m")
            cache[x.symbol] = m5.set_index("ts")["c"] if len(m5) else None
        c5 = cache[x.symbol]
        if c5 is None:
            continue
        i = pos[x.E]
        p_prev, p_e, n_prev, n_e = last_px(c5, sessions[i - 1]), last_px(c5, x.E), last_px(n5, sessions[i - 1]), last_px(n5, x.E)
        if not all(np.isfinite(v) for v in (p_prev, p_e, n_prev, n_e)) or abs(p_e / p_prev - 1) > 0.5:
            continue
        z = ((p_e / p_prev - 1) - (n_e / n_prev - 1)) / x.sigma
        d = _daily(store, x.symbol).set_index("day")["c"]
        row: dict[str, Any] = {"E": x.E, "symbol": x.symbol, "z_close": x.z, "z_1525": z}
        ok = True
        for k in KS:
            j = i + k
            if j >= len(sessions):
                ok = False
                break
            p_x = last_px(c5, sessions[j])
            if not np.isfinite(p_x) or abs(p_x / p_e - 1) > 0.6:
                ok = False
                break
            row[f"i_{k}"] = np.sign(z) * (p_x / p_e - 1) * 1e4 - cost_bps
            row[f"d_{k}"] = np.sign(x.z) * (d.loc[sessions[j]] / d.loc[x.E] - 1) * 1e4 - cost_bps if sessions[j] in d.index and x.E in d.index else float("nan")
        if ok:
            rows.append(row)
    r = pd.DataFrame(rows)
    if r.empty:
        return {"n": 0}

    def wk(sr: pd.Series) -> dict[str, Any]:
        sr = sr.dropna()
        w = sr.groupby(["{}-W{:02d}".format(*date.fromisoformat(d).isocalendar()[:2]) for d in sr.index]).sum()
        sd = w.std(ddof=1)
        return {"n": len(sr), "avg_bps": round(float(sr.mean()), 1), "median_bps": round(float(sr.median()), 1),
                "t_by_week": round(float(w.mean() / (sd / math.sqrt(len(w)))), 2) if sd > 0 and len(w) > 1 else None,
                "weeks_positive": round(float((w > 0).mean()) * 100, 1)}

    sel_c, sel_i = r[r["z_close"].abs() >= Z_MIN].set_index("E"), r[r["z_1525"].abs() >= Z_MIN].set_index("E")
    return {"events_with_5m": len(r), "selected_at_close": len(sel_c), "selected_at_1525": len(sel_i),
            "z_corr": round(float(r[["z_close", "z_1525"]].corr().iloc[0, 1]), 3),
            "same_sign_pct": round(float((np.sign(r["z_close"]) == np.sign(r["z_1525"])).mean()) * 100, 1),
            "by_hold": {str(k): {"daily_rule": wk(sel_c[f"d_{k}"]), "select_and_trade_at_1525": wk(sel_i[f"i_{k}"])} for k in KS}}


__all__ = ["SOURCES", "build_tables", "event_sessions", "event_table", "execution_check", "fetch", "months", "study"]
