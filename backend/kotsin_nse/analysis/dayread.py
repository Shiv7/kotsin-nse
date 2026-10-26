"""One session on the 5-minute chart, told the way the operator reads it (2026-10-26, NIFTY):
"09:20 took support at S1, 09:45 higher high, 09:55 through the previous day's CPR, up to 10:05,
retracement to 11:30, 12:50 CPR retest held, 13:05 bullish again" — and for every leg between
swings, who was positioning: the front future's OI change (long build-up, short covering, short
build-up, long unwinding) and the calls and puts added or shed at the strikes around price, with
the 15- and 30-minute frames beside it.

Everything is a function of plain series, so a past day comes from the archive (``from_archive``)
and today from the engine's stores (``from_engine``). It is a reading, not a signal: it describes
what happened in the operator's vocabulary. Which of these readings predicts anything is a
research question (docs/research/, HANDOFF §3.4), not something this module claims.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Any

from ..structure.engine import SWING_SIDE, Bar5, _swings, resample_5m
from ..structure.levels import daily_levels
from .buildup import _at
from .buildup import label as oi_label

#: a touch counts within this fraction of a normal day's range of the level (the levels page uses the same)
TOL_ADR = 0.1
#: a close this far beyond a level is a break (half the touch tolerance: a close just past it is a test, not a break)
BREAK_FRAC = 0.5
#: a leg's OI change under this (%) is flat — the 15-minute display threshold of ``buildup.INTRADAY`` is 0.1
LEG_FLAT_PCT = 0.25
#: a single OI point this far (%) from both neighbours is a bad frame, not a position change
#: (NIFTY future 2026-10-26 12:40: +617,825 then −491,465 one minute later)
SPIKE_PCT = 1.0
#: strikes this many steps either side of price are "around price"
NEAR_STEPS = 2.5
WORDS = {"LB": "long build-up", "SC": "short covering", "SB": "short build-up", "LU": "long unwinding", "FLAT": "OI flat"}
LEVELS = ("S2", "S1", "PDL", "PDC", "R1", "PDH", "R2")


def hm(ts: float) -> str:
    s = int(ts) + 19800  # IST is a fixed +05:30; no DST, no zoneinfo needed
    return f"{s % 86400 // 3600:02d}:{s % 3600 // 60:02d}"


@dataclass(slots=True)
class Band:
    """A level, or a band such as the CPR (``lo`` = BC, ``hi`` = TC)."""

    name: str
    lo: float
    hi: float


@dataclass(slots=True)
class Event:
    ts: int
    kind: str  # SUPPORT | RESISTANCE | BREAK_UP | BREAK_DOWN | HH | HL | LH | LL | BOS_UP | BOS_DOWN
    text: str
    price: float
    level: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "time": hm(self.ts)}


@dataclass(slots=True)
class Leg:
    t0: int
    t1: int
    p0: float
    p1: float
    closed: bool  # False for the leg still running at the last bar

    @property
    def up(self) -> bool:
        return self.p1 >= self.p0


def bands(levels: dict[str, float]) -> list[Band]:
    out = []
    if "BC" in levels and "TC" in levels:
        out.append(Band("CPR", min(levels["BC"], levels["TC"]), max(levels["BC"], levels["TC"])))
    out += [Band(n, levels[n], levels[n]) for n in LEVELS if n in levels]
    return out


def cpr(prev_h: float, prev_l: float, prev_c: float) -> dict[str, float]:
    """The previous session's floor pivots, CPR and extremes, by name."""
    return {lv.name: round(lv.price, 2) for lv in daily_levels(prev_h, prev_l, prev_c)}


def level_events(bars: Sequence[Bar5], lvls: Sequence[Band], tol: float) -> list[Event]:
    """Tests, rejections and breaks of each level over closed 5m bars. A level fires once per
    visit: after an event it is re-armed only when a close is more than two tolerances away."""
    out: list[Event] = []
    armed = {b.name: True for b in lvls}
    brk = tol * BREAK_FRAC
    for i, b in enumerate(bars):
        prev_c = bars[i - 1].c if i else b.o
        for lv in lvls:
            if not armed[lv.name]:
                if b.c > lv.hi + 2 * tol or b.c < lv.lo - 2 * tol:
                    armed[lv.name] = True
                continue
            where = f"{lv.name} {lv.lo:.2f}" if lv.lo == lv.hi else f"{lv.name} {lv.lo:.2f}–{lv.hi:.2f}"
            if prev_c <= lv.hi and b.c > lv.hi + brk:
                out.append(Event(b.ts, "BREAK_UP", f"closed above {where}", b.c, lv.name))
            elif prev_c >= lv.lo and b.c < lv.lo - brk:
                out.append(Event(b.ts, "BREAK_DOWN", f"closed below {where}", b.c, lv.name))
            elif prev_c > lv.hi and b.l <= lv.hi + tol and b.c >= lv.lo:
                how = "swept and reclaimed" if b.l < lv.lo - tol else "held as support"
                out.append(Event(b.ts, "SUPPORT", f"{where} {how} (low {b.l:.2f})", b.l, lv.name))
            elif prev_c < lv.lo and b.h >= lv.lo - tol and b.c <= lv.hi:
                how = "swept and rejected" if b.h > lv.hi + tol else "rejected as resistance"
                out.append(Event(b.ts, "RESISTANCE", f"{where} {how} (high {b.h:.2f})", b.h, lv.name))
            else:
                continue
            armed[lv.name] = False
    return out


def structure_events(bars: Sequence[Bar5], side: int = SWING_SIDE) -> tuple[list[Event], list[Leg]]:
    """Each swing as it becomes known (``side`` bars later), labelled against the previous swing of
    its kind, the first close beyond a swing as a break of structure, and the legs between swings."""
    swings = _swings(bars, side)
    out: list[Event] = []
    last: dict[str, float] = {}
    for s in swings:
        known = bars[min(s.i + side, len(bars) - 1)].ts
        prev = last.get(s.kind)
        if s.kind == "HIGH":
            kind = "HH" if prev is None or s.price > prev else "LH"
            word = "higher high" if kind == "HH" else "lower high"
        else:
            kind = "HL" if prev is None or s.price > prev else "LL"
            word = "higher low" if kind == "HL" else "lower low"
        vs = f" (previous {prev:.2f})" if prev is not None else ""
        out.append(Event(known, kind, f"{word} {s.price:.2f} at {hm(s.ts)}{vs}", s.price))
        last[s.kind] = s.price
        for b in bars[s.i + 1 :]:
            if (s.kind == "HIGH" and b.c > s.price) or (s.kind == "LOW" and b.c < s.price):
                k = "BOS_UP" if s.kind == "HIGH" else "BOS_DOWN"
                out.append(Event(b.ts, k, f"close {b.c:.2f} {'above' if k == 'BOS_UP' else 'below'} the swing {s.kind.lower()} {s.price:.2f}", b.c))
                break
    # legs: alternate highs and lows; of two same-kind swings in a row keep the more extreme
    kept: list[Any] = []
    for s in swings:
        if kept and kept[-1].kind == s.kind:
            better = s.price > kept[-1].price if s.kind == "HIGH" else s.price < kept[-1].price
            if better:
                kept[-1] = s
            continue
        kept.append(s)
    legs = [Leg(a.ts, b.ts, a.price, b.price, True) for a, b in pairwise(kept)]
    if kept and bars:
        # the first leg runs from the open to the first swing (the opening drive is never a swing: it has no bars before it)
        legs.insert(0, Leg(bars[0].ts, kept[0].ts, bars[0].o, kept[0].price, True))
        legs.append(Leg(kept[-1].ts, bars[-1].ts, kept[-1].price, bars[-1].c, False))
    # one BOS per bar: keep the one against the most extreme swing
    seen: dict[tuple[int, str], Event] = {}
    for e in out:
        if e.kind in ("BOS_UP", "BOS_DOWN"):
            seen.setdefault((e.ts, e.kind), e)
    out = [e for e in out if e.kind not in ("BOS_UP", "BOS_DOWN")] + list(seen.values())
    out.sort(key=lambda e: (e.ts, e.kind))
    return out, legs


def despike(series: Sequence[tuple[float, float]], pct: float = SPIKE_PCT) -> list[tuple[float, float]]:
    """A point farther than ``pct`` % from the median of itself and its neighbours is replaced by that median."""
    vals = [v for _t, v in series]
    out = []
    for i, (t, v) in enumerate(series):
        a = vals[i - 1] if i else v
        b = vals[i + 1] if i + 1 < len(vals) else v
        med = sorted((a, v, b))[1]
        out.append((t, med if med and abs(v - med) > pct / 100 * med else v))
    return out


def leg_oi(leg: Leg, fut_oi: Sequence[tuple[float, float]], fut_px: Sequence[tuple[float, float]] = ()) -> dict[str, Any] | None:
    """The future's OI over the leg (values at the end of its first and last bar)."""
    t0, t1 = leg.t0 + 300, leg.t1 + 300
    o0, o1 = _at(fut_oi, t0), _at(fut_oi, t1)
    if not o0 or not o1:
        return None
    p0 = _at(fut_px, t0) if fut_px else None
    p1 = _at(fut_px, t1) if fut_px else None
    pc = (p1 / p0 - 1) * 100 if p0 and p1 else (leg.p1 / leg.p0 - 1) * 100
    oc = (o1 / o0 - 1) * 100
    lab = oi_label(pc, oc, LEG_FLAT_PCT)
    return {"oi_from": o0, "oi_to": o1, "oi_chg": o1 - o0, "oi_chg_pct": round(oc, 2), "build_up": lab, "read": WORDS[lab]}


def _lakh(x: float) -> str:
    return f"{x / 1e5:+.1f}L" if abs(x) >= 1e4 else f"{x:+.0f}"


def leg_options(
    leg: Leg, spot: float, opt_oi: dict[tuple[str, float], Sequence[tuple[float, float]]], step: float | None
) -> dict[str, Any] | None:
    """OI added or shed at the strikes around ``spot`` over the leg, each side's net, and what it says."""
    if not opt_oi or not step:
        return None
    t0, t1 = leg.t0 + 300, leg.t1 + 300
    rows = []
    for (kind, k), s in opt_oi.items():
        if abs(k - spot) > NEAR_STEPS * step:
            continue
        a, b = _at(s, t0), _at(s, t1)
        if a is None or b is None:
            continue
        rows.append({"kind": kind, "strike": k, "chg": b - a, "oi": b})
    if not rows:
        return None
    ce = [r for r in rows if r["kind"] == "CE"]
    pe = [r for r in rows if r["kind"] == "PE"]
    calls_net = sum(r["chg"] for r in ce)
    puts_net = sum(r["chg"] for r in pe)
    top = sorted(rows, key=lambda r: -abs(r["chg"]))[:4]
    moves = [f"{int(r['strike'])}{r['kind']} {_lakh(r['chg'])}" for r in top if r["chg"]]
    if puts_net > 0 and calls_net <= 0:
        read = "puts added, calls shed: support being built under price"
    elif calls_net > 0 and puts_net <= 0:
        read = "calls added, puts shed: a lid being built over price"
    elif calls_net > 0 and puts_net > 0:
        read = "both sides added: a range being written"
    elif calls_net < 0 and puts_net < 0:
        read = "both sides shed: positions coming off"
    else:
        read = "no option positioning to speak of"
    ce_oi, pe_oi = sum(r["oi"] for r in ce), sum(r["oi"] for r in pe)
    return {"calls_net": calls_net, "puts_net": puts_net, "moves": moves, "read": read, "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None}


def _resample(bars: Sequence[Bar5], n: int) -> list[Bar5]:
    out: list[Bar5] = []
    for i in range(0, len(bars), n):
        g = bars[i : i + n]
        out.append(Bar5(g[0].ts, g[0].o, max(b.h for b in g), min(b.l for b in g), g[-1].c, sum(b.v for b in g)))
    return out


def htf(bars: Sequence[Bar5], levels: dict[str, float]) -> dict[str, Any]:
    """The 15-, 30- and 60-minute frames of the same session: swing labels, candle count, where it closed."""
    out: dict[str, Any] = {}
    b15 = _resample(bars, 3)
    ev, _legs = structure_events(b15, side=1)
    labels = [e for e in ev if e.kind in ("HH", "HL", "LH", "LL")]
    kinds = [e.kind for e in labels[-2:]]
    trend = "up" if kinds and all(k in ("HH", "HL") for k in kinds) else "down" if kinds and all(k in ("LH", "LL") for k in kinds) else "mixed"
    out["15m"] = {"swings": [{"time": hm(e.ts), "kind": e.kind, "price": e.price} for e in labels], "trend": trend}
    for name, n in (("30m", 6), ("1h", 12)):
        bb = _resample(bars, n)
        green = sum(1 for b in bb if b.c > b.o)
        out[name] = {"candles": len(bb), "green": green, "red": len(bb) - green,
                     "bars": [{"time": hm(b.ts), "o": b.o, "h": b.h, "l": b.l, "c": b.c} for b in bb]}
    if bars:
        c = bars[-1].c
        pdh, pdl = levels.get("PDH"), levels.get("PDL")
        tc, bc = levels.get("TC"), levels.get("BC")
        pos = "above the previous day's high" if pdh and c > pdh else "below the previous day's low" if pdl and c < pdl else "inside the previous day's range"
        vs_cpr = "above the CPR" if tc and c > tc else "below the CPR" if bc and c < bc else "inside the CPR"
        out["close"] = {"price": c, "position": pos, "vs_cpr": vs_cpr}
    return out


def read_day(
    symbol: str,
    day: date,
    bars: Sequence[Bar5],
    prev_day: tuple[float, float, float] | None,
    adr: float | None,
    *,
    fut_oi: Sequence[tuple[float, float]] = (),
    fut_px: Sequence[tuple[float, float]] = (),
    opt_oi: dict[tuple[str, float], Sequence[tuple[float, float]]] | None = None,
    strike_step: float | None = None,
    expiry: str | None = None,
) -> dict[str, Any] | None:
    if not bars:
        return None
    levels = cpr(*prev_day) if prev_day else {}
    last = bars[-1].c
    tol = (adr or last * 0.01) * TOL_ADR
    events = level_events(bars, bands(levels), tol) if levels else []
    s_events, legs = structure_events(bars)
    events = sorted(events + s_events, key=lambda e: (e.ts, e.kind))
    oi = despike(list(fut_oi)) if fut_oi else []
    leg_rows = []
    for lg in legs:
        o = leg_oi(lg, oi, fut_px) if oi else None
        opts = leg_options(lg, lg.p1, opt_oi or {}, strike_step)
        pts = lg.p1 - lg.p0
        leg_rows.append({
            "from": hm(lg.t0), "to": hm(lg.t1), "closed": lg.closed, "dir": "up" if lg.up else "down",
            "p0": lg.p0, "p1": lg.p1, "pts": round(pts, 2), "pct": round(pts / lg.p0 * 100, 2),
            "futures": o, "options": opts,
        })
    o, hi, lo = bars[0].o, max(b.h for b in bars), min(b.l for b in bars)
    pdc = levels.get("PDC")
    gap = round((o / pdc - 1) * 100, 2) if pdc else None
    out = {
        "symbol": symbol, "day": day.isoformat(), "bars": len(bars), "expiry": expiry,
        "open": o, "high": hi, "low": lo, "close": last, "gap_pct": gap, "levels": levels, "tolerance": round(tol, 2),
        "events": [e.to_json() for e in events], "legs": leg_rows, "htf": htf(bars, levels),
    }
    out["summary"] = summary(out)
    return out


#: a leg smaller than this (%) is left out of the prose (it stays in ``legs``): 5m swings come every few bars
MIN_LEG_PCT = 0.3


def summary(r: dict[str, Any]) -> list[str]:
    """The day in plain sentences: the open; every level tested, held or broken; the swings only
    where the structure turns (the first higher high after lower highs, the first lower low after
    higher lows) and the breaks of structure; the legs worth a sentence with who was positioning;
    the close and the higher frames."""
    lines: list[str] = []
    lv = r["levels"]
    if r.get("gap_pct") is not None:
        lines.append(f"Opened {r['open']:.2f}, {r['gap_pct']:+.2f}% against the previous close {lv['PDC']:.2f}; CPR {lv['BC']:.2f}–{lv['TC']:.2f}, S1 {lv['S1']:.2f}, R1 {lv['R1']:.2f}.")
    trend: str | None = None  # "up" after HH/HL, "down" after LH/LL
    for e in r["events"]:
        k = e["kind"]
        if k in ("SUPPORT", "RESISTANCE", "BREAK_UP", "BREAK_DOWN", "BOS_UP", "BOS_DOWN"):
            lines.append(f"{e['time']} {e['text']}.")
        elif k in ("HH", "HL", "LH", "LL"):
            now = "up" if k in ("HH", "HL") else "down"
            if trend != now:
                lines.append(f"{e['time']} {e['text']}{' — structure turns ' + now if trend else ''}.")
            trend = now
    for lg in r["legs"]:
        if abs(lg["pct"]) < MIN_LEG_PCT:
            continue
        f, op = lg.get("futures"), lg.get("options")
        who = f"future OI {f['oi_chg_pct']:+.2f}% → {f['read']}" if f else "no futures OI"
        opt = f"; {', '.join(op['moves'])} → {op['read']}" if op and op["moves"] else ""
        tail = "" if lg["closed"] else " (still running at the last bar)"
        lines.append(f"Leg {lg['from']}→{lg['to']} {lg['dir']} {lg['pts']:+.2f} pts ({lg['pct']:+.2f}%): {who}{opt}{tail}.")
    h = r["htf"]
    if "close" in h:
        c = h["close"]
        lines.append(f"Closed {c['price']:.2f}, {c['position']}, {c['vs_cpr']}; 15m structure {h['15m']['trend']}, 30m candles {h['30m']['green']} green / {h['30m']['red']} red.")
    ups = [lg["futures"]["build_up"] for lg in r["legs"]
           if lg.get("futures") and lg["dir"] == "up" and abs(lg["pct"]) >= MIN_LEG_PCT and lg["futures"]["build_up"] != "FLAT"]
    if ups and ups[0] == "SC" and "LB" in ups[1:]:
        lines.append("The first rally was short covering; fresh longs came in on the later up-legs.")
    elif ups and all(x == "SC" for x in ups):
        lines.append("Every up-leg was short covering: no fresh longs were added all day.")
    elif ups and all(x == "LB" for x in ups):
        lines.append("Every up-leg was a long build-up: fresh longs all day.")
    return lines


# -- sources ---------------------------------------------------------------------------------------


def _strike_step(keys: Sequence[tuple[str, float]]) -> float | None:
    ks = sorted({k for _kind, k in keys})
    diffs = [b - a for a, b in pairwise(ks) if b > a]
    return min(diffs) if diffs else None


def from_archive(data_dir: Path, symbol: str, day: date, segment_open: str = "09:15", segment_close: str = "15:30") -> dict[str, Any] | None:
    """A past session from the day's archive files: the underlying's 1m bars, the engine's own
    minute-by-minute futures OI (``archive/fno``), every tracked strike's OI (``archive/oi``) mapped
    through that day's scrip master, and the previous session from the daily cache."""
    import pandas as pd

    from ..market.session import ist_at
    from ..research.history import HistoryStore
    from ..scan.replay import guarded_adr

    sym = symbol.upper()
    bars_p = data_dir / "archive" / "bars" / f"{day.isoformat()}.parquet"
    if not bars_p.exists():
        return None
    b = pd.read_parquet(bars_p)
    b = b[b["symbol"] == sym]
    if b.empty:
        return None
    code = b["scrip_code"].astype(str).mode().iloc[0]
    b = b[b["scrip_code"].astype(str) == code]
    open_ts, close_ts = ist_at(day, segment_open), ist_at(day, segment_close)
    rows = b[(b["ts"] >= open_ts) & (b["ts"] < close_ts)].sort_values("ts")
    bars = resample_5m(list(rows[["ts", "o", "h", "l", "c", "v"]].itertuples(index=False, name=None)), open_ts)
    d1 = HistoryStore(data_dir / "history").load(sym, "1d")
    before = d1[d1["ts"] < ist_at(day, "00:00")].sort_values("ts") if not d1.empty else d1
    prev = (float(before["h"].iloc[-1]), float(before["l"].iloc[-1]), float(before["c"].iloc[-1])) if len(before) else None
    adr = guarded_adr(before["h"].tolist(), before["l"].tolist(), before["c"].tolist()) if len(before) else None
    fut_oi: list[tuple[float, float]] = []
    fut_px: list[tuple[float, float]] = []
    fno_p = data_dir / "archive" / "fno" / f"{day.isoformat()}.parquet"
    if fno_p.exists():
        f = pd.read_parquet(fno_p, columns=["symbol", "ts", "fut_ltp", "fut_oi"])
        f = f[f["symbol"] == sym].dropna(subset=["fut_oi"]).sort_values("ts")
        fut_oi = [(float(t) + 60, float(v)) for t, v in zip(f["ts"], f["fut_oi"], strict=False)]
        fut_px = [(float(t) + 60, float(v)) for t, v in zip(f["ts"], f["fut_ltp"], strict=False) if v == v and v]
    opt_oi: dict[tuple[str, float], list[tuple[float, float]]] = {}
    expiry = None
    oi_p = data_dir / "archive" / "oi" / f"{day.isoformat()}.parquet"
    masters = sorted(p for p in (data_dir / "scripmaster").glob("nse_fo-*.csv") if p.stem[7:] <= day.isoformat())
    if oi_p.exists() and masters:
        sm = pd.read_csv(masters[-1], usecols=["ScripCode", "SymbolRoot", "ScripType", "StrikeRate", "Expiry"], dtype=str)
        sm = sm[(sm["SymbolRoot"] == sym) & sm["ScripType"].isin(["CE", "PE"])]
        oi = pd.read_parquet(oi_p)
        oi["scrip_code"] = oi["scrip_code"].astype(str)
        m = oi.merge(sm, left_on="scrip_code", right_on="ScripCode")
        m = m[(m["ts"] >= open_ts) & (m["ts"] <= close_ts + 600)]
        if not m.empty:
            expiry = str(m.groupby("Expiry").size().idxmax())
            m = m[m["Expiry"] == expiry].sort_values("ts")
            for (kind, k), g in m.groupby(["ScripType", "StrikeRate"]):
                opt_oi[(str(kind), float(k))] = [(float(t), float(v)) for t, v in zip(g["ts"], g["oi"], strict=False)]
    return read_day(sym, day, bars, prev, adr, fut_oi=fut_oi, fut_px=fut_px, opt_oi=opt_oi, strike_step=_strike_step(list(opt_oi)), expiry=expiry)


def from_engine(engine: Any, symbol: str, now: float) -> dict[str, Any] | None:
    """Today, from the engine's bar store, flow minutes (the future's OI) and option OI history."""
    from ..market.session import ist_day, session_open_ts
    from ..scan.replay import guarded_adr

    sym = symbol.upper()
    g = engine.groups.get(sym)
    if g is None:
        return None
    today = ist_day(now)
    open_ts = int(session_open_ts(g.segment, today))
    last_closed = open_ts + ((int(now) - open_ts) // 300) * 300
    m1 = [b for b in engine.store.bars(sym, "1m") if ist_day(b.ts) == today and b.ts < last_closed]
    if not m1:
        return None
    bars = resample_5m([(int(b.ts), b.open, b.high, b.low, b.close, b.volume) for b in m1], open_ts)
    prior = [b for b in engine.store.bars(sym, "1d") if ist_day(b.ts) < today]
    prev = (prior[-1].high, prior[-1].low, prior[-1].close) if prior else None
    adr = guarded_adr([b.high for b in prior], [b.low for b in prior], [b.close for b in prior]) if prior else None
    fut = g.futures[0] if g.futures else None
    fut_oi = [(m.ts + 60, m.oi) for m in engine.flow.minutes(fut.scrip_code) if m.oi] if fut else []
    near = g.option_expiries[0] if g.option_expiries else None
    opt_oi = {(o.option_type.value, o.strike): list(engine.oi_hist.get(o.scrip_code, ())) for o in g.options if o.expiry == near}
    opt_oi = {k: v for k, v in opt_oi.items() if v}
    return read_day(sym, today, bars, prev, adr, fut_oi=fut_oi, opt_oi=opt_oi, strike_step=_strike_step(list(opt_oi)), expiry=near)


__all__ = ["Band", "Event", "Leg", "bands", "cpr", "despike", "from_archive", "from_engine", "htf", "leg_oi", "leg_options", "level_events", "read_day", "structure_events", "summary"]
