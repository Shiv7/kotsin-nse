"""The power of each tool on its own (pre-registered: docs/research/2026-10-23-prereg-unsupervised.md §4).

Not strategies — no stops, sizes or costs. For every event a race from the close of its bar: does price
go X in the tool's direction before X against it (``race``)? A coin scores 50 %; a bar that touches
both counts ½, and so does a session that ends with neither. Also the signed move after 30 and 60
minutes (5m) or 5 sessions (daily), in ADR.

**Low timeframe** — 5m bars built from the 1m cache (levels from the 1m bars too: the daily cache is
bonus-adjusted, the 1m is not), X = 0.25 ADR, rest of the session:
  * the first touch of each daily (P, TC, BC, R1, S1, R2, S2, PDH, PDL), weekly (WP, WR1, WS1, PWH,
    PWL) and monthly (MP, MR1, MS1, PMH, PML) level from above (a support test → up) or below (→ down),
    tagged with its cluster weight (levels within 0.1 ADR chained; monthly 3, weekly 2, daily 1); each
    with a **placebo** level shifted 0.15–0.35 ADR on the same stock-day;
  * Fibonacci (38.2 / 50 / 61.8 / 78.6) of the previous day's range against 30 / 45 / 56 / 70;
  * the Initial Balance (09:15–10:15): its first 5m close beyond (a break → continuation) and its
    first probe beyond that closes back inside (a sweep → reversal); the same for PDH / PDL;
  * SMC order blocks: the last opposite candle before a ≥ ``OB_DISPLACE`` ATR move, on its first
    retest, against the same retest of an ordinary opposite candle (no displacement);
  * 5m double bottoms / tops at the neckline break (``confirm.doubles``); hammers / shooting stars and
    engulfings at a 12-bar extreme.
**High timeframe** — the daily cache, X = 1 ADR, 10 sessions: weekly / monthly pivot touches (with
placebos), Fibonacci of the 60-session swing (with the non-Fibonacci ratios), sweeps of the
20-session high / low against closes beyond it, daily double bottoms / tops.

**IB role** (descriptive): how often the IB breaks up / down / both / neither, what follows a break
and a failed break, by the IB's size and the CPR's width.
"""

from __future__ import annotations

import hashlib
import json
import time as _time
import uuid
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ..config import Settings
from .confirm import ConfirmParams, atr, cpr, doubles
from .dbcpr import bars_from_1m
from .history import HistoryStore
from .optsignals import HOLDOUT_START, _weekly_t

X_LTF, X_HTF, HTF_BARS = 0.25, 1.0, 10
TOUCH_TOL = 0.05  # ADR
CLUSTER_TOL = 0.10  # ADR
OB_DISPLACE = 1.5  # ATR(5m)
FIB = (0.382, 0.5, 0.618, 0.786)
NONFIB = (0.30, 0.45, 0.56, 0.70)
WEIGHT = {"D": 1, "W": 2, "M": 3}
IB_BARS = 12  # 5m bars, 09:15–10:15
HALVES = {"ltf": ("2024-04-01", "2024-12-31", "2025-01-01", "2025-09-30"), "htf": ("2021-04-01", "2023-06-30", "2023-07-01", "2025-09-30")}


# -- the race -------------------------------------------------------------------------------------------


def race(h: np.ndarray, lo: np.ndarray, c: np.ndarray, j: int, side: int, x: float, end: int | None = None) -> float:
    """1 if price goes ``x`` (price units) ``side``-wards from c[j] before ``x`` the other way, 0 if the
    other way first, ½ for a bar touching both or neither by ``end``."""
    e = c[j]
    hh, ll = h[j + 1 : end], lo[j + 1 : end]
    if side > 0:
        win, lose = hh >= e + x, ll <= e - x
    else:
        win, lose = ll <= e - x, hh >= e + x
    iw = int(np.argmax(win)) if win.any() else 10**9
    il = int(np.argmax(lose)) if lose.any() else 10**9
    if iw == il:
        return 0.5
    return 1.0 if iw < il else 0.0


def _fwd(c: np.ndarray, j: int, n: int, side: int, x_unit: float) -> float:
    k = min(j + n, len(c) - 1)
    return float(side * (c[k] - c[j]) / x_unit) if k > j else np.nan


def _first_touch(pc: np.ndarray, h: np.ndarray, lo: np.ndarray, lvl: float, tol: float, start: int) -> tuple[int | None, int | None]:
    """First bar (≥ start) that comes down to ``lvl`` from above, and first that comes up from below."""
    above = np.flatnonzero((pc[start:] > lvl) & (lo[start:] <= lvl + tol))
    below = np.flatnonzero((pc[start:] < lvl) & (h[start:] >= lvl - tol))
    return (int(above[0]) + start if len(above) else None), (int(below[0]) + start if len(below) else None)


def _seed(*parts: Any) -> np.random.Generator:
    return np.random.default_rng(int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:8], 16))


# -- levels ---------------------------------------------------------------------------------------------


def _wk(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _period_levels(daily: pd.DataFrame, key: Any, prefix: str, frame: str) -> dict[Any, dict[str, tuple[float, str]]]:
    """For every day: the previous complete period's P, R1, S1, high, low."""
    groups = daily.groupby(key)
    agg = groups.agg(H=("H", "max"), L=("L", "min"), C=("C", "last"))
    keys = list(agg.index)
    out: dict[Any, dict[str, tuple[float, str]]] = {}
    for n, k in enumerate(keys[1:], 1):
        r = agg.loc[keys[n - 1]]
        p = (r.H + r.L + r.C) / 3
        out[k] = {f"{prefix}P": (p, frame), f"{prefix}R1": (2 * p - r.L, frame), f"{prefix}S1": (2 * p - r.H, frame),
                  f"P{prefix}H": (r.H, frame), f"P{prefix}L": (r.L, frame)}
    return out


def _cluster_weight(levels: dict[str, tuple[float, str]], tol: float) -> dict[str, int]:
    items = sorted(levels.items(), key=lambda kv: kv[1][0])
    out: dict[str, int] = {}
    chain: list[tuple[str, tuple[float, str]]] = []
    for kv in items:
        if chain and kv[1][0] - chain[-1][1][0] > tol:
            w = sum(WEIGHT[f] for _n, (_p, f) in chain)
            out.update({n: w for n, _ in chain})
            chain = []
        chain.append(kv)
    if chain:
        w = sum(WEIGHT[f] for _n, (_p, f) in chain)
        out.update({n: w for n, _ in chain})
    return out


# -- low timeframe --------------------------------------------------------------------------------------


def ltf_events(sym: str, m1: pd.DataFrame, start: date, end: date, member: Any = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every low-timeframe event of one stock, and its IB rows."""
    if m1.empty:
        return [], []
    m1 = m1.sort_values("ts").drop_duplicates("ts")
    t = pd.to_datetime(m1["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    mins = (t.dt.hour * 60 + t.dt.minute).to_numpy()
    m1 = m1[(mins >= 9 * 60 + 15) & (mins < 15 * 60 + 14)]
    if m1.empty:
        return [], []
    b5 = bars_from_1m(m1, 5)
    b5["day"] = [(pd.Timestamp(x, unit="s") + pd.Timedelta(hours=5, minutes=30)).date() for x in b5["ts"]]
    daily = b5.groupby("day").agg(O=("o", "first"), H=("h", "max"), L=("l", "min"), C=("c", "last"))
    days = list(daily.index)
    O1, H1, L1, C1 = (daily[x].to_numpy(float) for x in ("O", "H", "L", "C"))
    adr_pct = pd.Series((H1 - L1) / C1).shift(1).rolling(20, min_periods=15).mean().to_numpy()
    jump = np.r_[False, np.abs(O1[1:] / C1[:-1] - 1) > 0.2]
    last_jump = -10_000
    ok_day = np.zeros(len(days), bool)
    for i in range(len(days)):
        if jump[i]:
            last_jump = i
        ok_day[i] = i - last_jump > 25  # a monthly level spans the jump for ~a month
    wk = _period_levels(daily.assign(k=[_wk(d) for d in days]), "k", "W", "W")
    mo = _period_levels(daily.assign(k=[d.isoformat()[:7] for d in days]), "k", "M", "M")
    A5 = atr(b5["h"].to_numpy(float), b5["l"].to_numpy(float), b5["c"].to_numpy(float))
    H5, L5, C5, O5 = (b5[x].to_numpy(float) for x in ("h", "l", "c", "o"))
    ev: list[dict[str, Any]] = []
    ib_rows: list[dict[str, Any]] = []
    starts = np.flatnonzero(np.r_[True, b5["day"].to_numpy()[1:] != b5["day"].to_numpy()[:-1]])
    bounds = list(zip(starts, np.r_[starts[1:], len(b5)], strict=True))
    for di, d in enumerate(days):
        if di < 2 or not (start <= d <= end) or not ok_day[di] or np.isnan(adr_pct[di]) or (member is not None and not member(sym, d)):
            continue
        a0, a1 = bounds[di]
        if a1 - a0 < 60:
            continue
        h, lo, c, o, a5 = H5[a0:a1], L5[a0:a1], C5[a0:a1], O5[a0:a1], A5[a0:a1]
        pc = np.r_[O1[di], c[:-1]]
        ad = adr_pct[di] * C1[di - 1]
        x = X_LTF * ad
        tol = TOUCH_TOL * ad
        base = {"symbol": sym, "day": d.isoformat(), "tf": "ltf"}

        def add(family: str, name: str, j: int | None, side: int, placebo: bool = False, *, _b: dict[str, Any] = base, _h: np.ndarray = h,
                _lo: np.ndarray = lo, _c: np.ndarray = c, _x: float = x, _ad: float = ad, **tags: Any) -> None:
            if j is None or j >= len(_c) - 2:
                return
            ev.append({**_b, "family": family, "name": name, "side": side, "placebo": placebo, "bar": j,
                       "race": race(_h, _lo, _c, j, side, _x), "fwd30": _fwd(_c, j, 6, side, _ad), "fwd60": _fwd(_c, j, 12, side, _ad), **tags})

        # pivots, weekly, monthly — with clusters and placebos
        lv = {k: (v, "D") for k, v in cpr(H1[di - 1], L1[di - 1], C1[di - 1]).items()}
        lv.update(wk.get(_wk(d), {}))
        lv.update(mo.get(d.isoformat()[:7], {}))
        cw = _cluster_weight(lv, CLUSTER_TOL * ad)
        rng = _seed(sym, d)
        for name, (price, frame) in lv.items():
            ja, jb = _first_touch(pc, h, lo, price, tol, 1)
            w = cw.get(name, 1)
            tag = {"frame": frame, "cluster": "alone" if w == WEIGHT[frame] else ("weight 2-3" if w <= 3 else "weight 4+")}
            add("level", name, ja, +1, **tag)
            add("level", name, jb, -1, **tag)
            fake = price + rng.choice((-1, 1)) * rng.uniform(0.15, 0.35) * ad
            ja, jb = _first_touch(pc, h, lo, fake, tol, 1)
            add("level", name, ja, +1, placebo=True, **tag)
            add("level", name, jb, -1, placebo=True, **tag)
        # Fibonacci of the previous day's range vs non-Fibonacci ratios
        up = C1[di - 1] >= O1[di - 1]
        rngd = H1[di - 1] - L1[di - 1]
        for ratios, fam in ((FIB, "fib"), (NONFIB, "nonfib")):
            for r in ratios:
                price = H1[di - 1] - r * rngd if up else L1[di - 1] + r * rngd
                ja, jb = _first_touch(pc, h, lo, price, tol, 1)
                add(fam, f"{r:.3f}", ja, +1)
                add(fam, f"{r:.3f}", jb, -1)
        # Initial Balance, PDH/PDL: break (continuation) vs sweep (reversal)
        ibh, ibl = h[:IB_BARS].max(), lo[:IB_BARS].min()
        for nm, lvl, s in (("IBH", ibh, 1), ("IBL", ibl, -1), ("PDH", H1[di - 1], 1), ("PDL", L1[di - 1], -1)):
            st = IB_BARS if nm.startswith("IB") else 1
            beyond = s * (c[st:] - lvl) > 0
            probe = (h[st:] > lvl) if s > 0 else (lo[st:] < lvl)
            jb_ = int(np.argmax(beyond)) + st if beyond.any() else None
            sweep = probe & ~beyond
            js = int(np.argmax(sweep)) + st if sweep.any() else None
            if js is not None and (jb_ is None or js < jb_) and s * (pc[js] - lvl) < 0:
                add("sweep", nm, js, -s)
            if jb_ is not None and s * (pc[jb_] - lvl) <= 0:
                add("break", nm, jb_, s)
        # SMC order blocks vs ordinary opposite candles, on the first retest
        for i in range(1, len(c) - 5):
            for s in (1, -1):
                if s * (c[i] - o[i]) >= 0:
                    continue  # needs a candle against the move that follows
                edge = h[i] if s > 0 else lo[i]
                disp = (c[i + 1 : i + 4].max() - h[i]) if s > 0 else (lo[i] - c[i + 1 : i + 4].min())
                kind = "order block" if disp >= OB_DISPLACE * a5[i] else ("plain candle" if disp < 0.3 * a5[i] else None)
                if kind is None:
                    continue
                for j in range(i + 4, len(c) - 2):
                    if s * (pc[j] - edge) > 0 and ((lo[j] <= edge) if s > 0 else (h[j] >= edge)):
                        add("smc", kind, j, s)
                        break
        # double bottoms / tops at the neckline break
        cp = ConfirmParams()
        for s in (1, -1):
            hh, ll, cc = (h, lo, c) if s > 0 else (-lo, -h, -c)
            for p in doubles(hh, ll, cc, a5, cp, np.ones(len(c), bool)):
                add("double", "bottom" if s > 0 else "top", p["k"], s)
        # reversal candles at a 12-bar extreme
        body = np.abs(c - o)
        lw, uw = np.minimum(o, c) - lo, h - np.maximum(o, c)
        for j in range(12, len(c) - 2):
            low12, high12 = lo[j] <= lo[j - 12 : j].min(), h[j] >= h[j - 12 : j].max()
            if low12 and lw[j] >= 2 * body[j] and uw[j] <= body[j] and body[j] > 0:
                add("candle", "hammer", j, 1)
            if high12 and uw[j] >= 2 * body[j] and lw[j] <= body[j] and body[j] > 0:
                add("candle", "shooting star", j, -1)
            if low12 and c[j] > o[j] and c[j - 1] < o[j - 1] and o[j] <= c[j - 1] and c[j] >= o[j - 1]:
                add("candle", "bull engulfing", j, 1)
            if high12 and c[j] < o[j] and c[j - 1] > o[j - 1] and o[j] >= c[j - 1] and c[j] <= o[j - 1]:
                add("candle", "bear engulfing", j, -1)
        # IB role, described
        after_h, after_l = h[IB_BARS:], lo[IB_BARS:]
        up_i = int(np.argmax(after_h > ibh)) if (after_h > ibh).any() else None
        dn_i = int(np.argmax(after_l < ibl)) if (after_l < ibl).any() else None
        ib = ibh - ibl
        lvc = cpr(H1[di - 1], L1[di - 1], C1[di - 1])
        ib_rows.append({**base, "ib_adr": ib / ad, "cpr_w_adr": (lvc["TC"] - lvc["BC"]) / ad,
                        "broke_up": up_i is not None, "broke_dn": dn_i is not None,
                        "first": "up" if up_i is not None and (dn_i is None or up_i < dn_i) else ("down" if dn_i is not None else "none"),
                        "ext_up_ib": max(0.0, (after_h.max() - ibh) / ib) if ib > 0 else np.nan,
                        "ext_dn_ib": max(0.0, (ibl - after_l.min()) / ib) if ib > 0 else np.nan,
                        "close_vs_ib": "above" if c[-1] > ibh else ("below" if c[-1] < ibl else "inside"),
                        "open_in_ib_pos": (o[0] - ibl) / ib if ib > 0 else np.nan})
    return ev, ib_rows


# -- high timeframe -------------------------------------------------------------------------------------


def htf_events(sym: str, d1: pd.DataFrame, start: date, end: date, member: Any = None) -> list[dict[str, Any]]:
    if d1.empty or len(d1) < 100:
        return []
    d1 = d1.sort_values("ts")
    d1["day"] = [(pd.Timestamp(x, unit="s") + pd.Timedelta(hours=5, minutes=30)).date() for x in d1["ts"]]
    d1 = d1.drop_duplicates("day", keep="last").reset_index(drop=True)
    days = list(d1["day"])
    H, L, C = (d1[x].to_numpy(float) for x in ("h", "l", "c"))
    adr = pd.Series(H - L).shift(1).rolling(20, min_periods=15).mean().to_numpy()
    daily = pd.DataFrame({"H": H, "L": L, "C": C}, index=days)
    wk = _period_levels(daily.assign(k=[_wk(d) for d in days]), "k", "W", "W")
    mo = _period_levels(daily.assign(k=[d.isoformat()[:7] for d in days]), "k", "M", "M")
    pc = np.r_[np.nan, C[:-1]]
    ev: list[dict[str, Any]] = []

    def add(family: str, name: str, j: int, side: int, placebo: bool = False, **tags: Any) -> None:
        if j + 1 >= len(C):
            return
        ev.append({"symbol": sym, "day": days[j].isoformat(), "tf": "htf", "family": family, "name": name, "side": side, "placebo": placebo,
                   "bar": j, "race": race(H, L, C, j, side, X_HTF * adr[j], j + 1 + HTF_BARS), "fwd5": _fwd(C, j, 5, side, adr[j]), **tags})

    ok = [i for i, d in enumerate(days) if start <= d <= end and i >= 60 and not np.isnan(adr[i]) and (member is None or member(sym, d))]
    okset = set(ok)
    # weekly / monthly pivots, first touch each period, with placebos
    for table, frame in ((wk, "W"), (mo, "M")):
        seen: set[tuple[Any, str, int, bool]] = set()
        for i in ok:
            d = days[i]
            k = _wk(d) if frame == "W" else d.isoformat()[:7]
            rng = _seed(sym, k, frame)
            for name, (price, _f) in table.get(k, {}).items():
                fake = price + rng.choice((-1, 1)) * rng.uniform(0.15, 0.35) * adr[i]
                for lvl, plc in ((price, False), (fake, True)):
                    tol = TOUCH_TOL * adr[i]
                    for side, hit in ((1, pc[i] > lvl and L[i] <= lvl + tol), (-1, pc[i] < lvl and H[i] >= lvl - tol)):
                        if hit and (k, name, side, plc) not in seen:
                            seen.add((k, name, side, plc))
                            add("level", name, i, side, plc, frame=frame)
    # Fibonacci of the 60-session swing vs non-Fibonacci ratios; sweeps of the 20-session range
    for i in ok:
        hi_i, lo_i = i - 60 + int(np.argmax(H[i - 60 : i])), i - 60 + int(np.argmin(L[i - 60 : i]))
        sh, sl = H[hi_i], L[lo_i]
        up = hi_i > lo_i
        for ratios, fam in ((FIB, "fib"), (NONFIB, "nonfib")):
            for r in ratios:
                lvl = sh - r * (sh - sl) if up else sl + r * (sh - sl)
                side = 1 if up else -1
                if (side > 0 and pc[i] > lvl and L[i] <= lvl) or (side < 0 and pc[i] < lvl and H[i] >= lvl):
                    add(fam, f"{r:.3f}", i, side)
        h20, l20 = H[i - 20 : i].max(), L[i - 20 : i].min()
        if L[i] < l20:
            add("sweep" if C[i] > l20 else "break", "20-day low", i, 1 if C[i] > l20 else -1)
        if H[i] > h20:
            add("sweep" if C[i] < h20 else "break", "20-day high", i, -1 if C[i] < h20 else 1)
    a = atr(H, L, C)
    cp = ConfirmParams(max_wait=10, min_gap=5)
    for s in (1, -1):
        hh, ll, cc = (H, L, C) if s > 0 else (-L, -H, -C)
        for p in doubles(hh, ll, cc, a, cp, np.ones(len(C), bool)):
            if p["k"] in okset:
                add("double", "bottom" if s > 0 else "top", p["k"], s)
    return ev


# -- read-out -------------------------------------------------------------------------------------------


def summarize(ev: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for (tf, fam, name), g in ev.groupby(["tf", "family", "name"]):
        real, fake = g[~g["placebo"]], g[g["placebo"]]
        h = HALVES[tf]
        by_day = real.groupby("day")["race"].mean()
        r = {"tf": tf, "family": fam, "name": name, "n": len(real), "race": round(float(real["race"].mean()) * 100, 1)}
        if len(fake):
            diff = by_day - fake.groupby("day")["race"].mean().reindex(by_day.index)
            t, _w = _weekly_t(diff.dropna())
            r.update({"placebo": round(float(fake["race"].mean()) * 100, 1), "edge_pts": round((real["race"].mean() - fake["race"].mean()) * 100, 1),
                      "t": round(t, 2) if t else None})
        else:
            t, _w = _weekly_t(by_day - 0.5)
            r.update({"placebo": 50.0, "edge_pts": round((real["race"].mean() - 0.5) * 100, 1), "t": round(t, 2) if t else None})
        r["halves"] = [round(float(real.loc[(real["day"] >= h[0]) & (real["day"] <= h[1]), "race"].mean()) * 100, 1),
                       round(float(real.loc[(real["day"] >= h[2]) & (real["day"] <= h[3]), "race"].mean()) * 100, 1)]
        fwd = "fwd60" if tf == "ltf" else "fwd5"
        r["fwd_adr"] = round(float(real[fwd].mean()), 3)
        r["power"] = bool(r["t"] is not None and abs(r["t"]) >= 3)
        rows.append(r)
    return rows


def run(settings: Settings, label: str = "", on_symbol: Any = None) -> dict[str, Any]:
    from ..market.fo_universe import load as load_members

    t0 = _time.time()
    fm, _ = load_members(settings.data_dir)
    store = HistoryStore(settings.data_dir / "history")
    ltf_end, htf_end = date(2025, 9, 30), date(2025, 9, 30)
    assert ltf_end < HOLDOUT_START and htf_end < HOLDOUT_START
    ev: list[dict[str, Any]] = []
    ib: list[dict[str, Any]] = []
    names = [s for s in store.symbols("1m") if s not in ("NIFTY", "BANKNIFTY")]
    for n, sym in enumerate(names, 1):
        e, i = ltf_events(sym, store.load(sym, "1m"), date(2024, 4, 1), ltf_end, fm.member)
        ev += e
        ib += i
        ev += htf_events(sym, store.load(sym, "1d"), date(2021, 4, 1), htf_end, fm.member)
        if on_symbol and (n % 25 == 0 or n == len(names)):
            on_symbol(sym, n, len(names))
    E = pd.DataFrame(ev)
    root = settings.data_dir / "obtests"
    rid = f"power-{uuid.uuid4().hex[:10]}"
    E.to_parquet(root / f"{rid}-events.parquet")
    IB = pd.DataFrame(ib)
    IB.to_parquet(root / f"{rid}-ib.parquet")
    out = {"id": rid, "kind": "power", "label": label, "events": len(E), "table": summarize(E), "ib": ib_role(IB),
           "seconds": round(_time.time() - t0, 1)}
    (root / f"{rid}.json").write_text(json.dumps(out, default=float))
    return out


def ib_role(ib: pd.DataFrame) -> dict[str, Any]:
    if ib.empty:
        return {}
    both = ib["broke_up"] & ib["broke_dn"]
    out: dict[str, Any] = {
        "days": len(ib),
        "broke_up_only": round(float((ib["broke_up"] & ~ib["broke_dn"]).mean()) * 100, 1),
        "broke_down_only": round(float((ib["broke_dn"] & ~ib["broke_up"]).mean()) * 100, 1),
        "broke_both": round(float(both.mean()) * 100, 1), "broke_neither": round(float((~ib["broke_up"] & ~ib["broke_dn"]).mean()) * 100, 1),
        "ib_size_adr_median": round(float(ib["ib_adr"].median()), 2),
    }
    first_up = ib[ib["first"] == "up"]
    out["after_first_break_up"] = {"n": len(first_up), "later_broke_down_too": round(float(first_up["broke_dn"].mean()) * 100, 1),
                                   "closed_above_ibh": round(float((first_up["close_vs_ib"] == "above").mean()) * 100, 1),
                                   "closed_inside": round(float((first_up["close_vs_ib"] == "inside").mean()) * 100, 1),
                                   "extension_ib_median": round(float(first_up["ext_up_ib"].median()), 2),
                                   "reached_1x_ib_extension": round(float((first_up["ext_up_ib"] >= 1).mean()) * 100, 1)}
    first_dn = ib[ib["first"] == "down"]
    out["after_first_break_down"] = {"n": len(first_dn), "later_broke_up_too": round(float(first_dn["broke_up"].mean()) * 100, 1),
                                     "closed_below_ibl": round(float((first_dn["close_vs_ib"] == "below").mean()) * 100, 1),
                                     "closed_inside": round(float((first_dn["close_vs_ib"] == "inside").mean()) * 100, 1),
                                     "extension_ib_median": round(float(first_dn["ext_dn_ib"].median()), 2),
                                     "reached_1x_ib_extension": round(float((first_dn["ext_dn_ib"] >= 1).mean()) * 100, 1)}
    q = pd.qcut(ib["ib_adr"], 4, labels=["smallest IB", "small", "large", "largest IB"])
    out["by_ib_size"] = {str(k): {"n": len(g), "broke_neither": round(float((~g["broke_up"] & ~g["broke_dn"]).mean()) * 100, 1),
                                  "broke_both": round(float((g["broke_up"] & g["broke_dn"]).mean()) * 100, 1),
                                  "closed_outside_ib": round(float((g["close_vs_ib"] != "inside").mean()) * 100, 1),
                                  "max_extension_ib_median": round(float(np.maximum(g["ext_up_ib"], g["ext_dn_ib"]).median()), 2)}
                         for k, g in ib.groupby(q, observed=True)}
    w = pd.qcut(ib["cpr_w_adr"], 3, labels=["narrow CPR", "mid CPR", "wide CPR"])
    out["by_cpr_width"] = {str(k): {"n": len(g), "broke_neither": round(float((~g["broke_up"] & ~g["broke_dn"]).mean()) * 100, 1),
                                    "closed_outside_ib": round(float((g["close_vs_ib"] != "inside").mean()) * 100, 1),
                                    "max_extension_ib_median": round(float(np.maximum(g["ext_up_ib"], g["ext_dn_ib"]).median()), 2)}
                           for k, g in ib.groupby(w, observed=True)}
    return out


__all__ = ["htf_events", "ib_role", "ltf_events", "race", "run", "summarize"]
