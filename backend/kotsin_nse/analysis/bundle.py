"""Everything the engine knows about one stock, assembled in reading order for the Analysis page
and the LLM summary: big picture, positioning, build-up across timeframes, levels (pivots, zones,
Fibonacci), structure and profile, today's reaction, microstructure, options, hedged setups.

Runs on the event loop (it reads the engine's live dicts); each part is light (~ms)."""

from __future__ import annotations

from datetime import date
from typing import Any

from ..market.session import ist_day, session_open_ts
from ..profile.service import from_store as profile_from_store
from ..structure.service import from_store as structure_from_store
from ..structure.service import levels_from_store
from .bigpicture import big_picture
from .buildup import mtf
from .hedge import setups
from .positioning import positioning

FLOW_BUCKET_MIN = 15


def _flow_buckets(minutes: list[Any]) -> list[dict[str, Any]]:
    by: dict[int, dict[str, float]] = {}
    for m in minutes:
        k = m.ts - m.ts % (FLOW_BUCKET_MIN * 60)
        b = by.setdefault(k, {"buy": 0.0, "sell": 0.0, "ofi": 0.0, "qi": 0.0, "qn": 0})
        b["buy"] += m.buy
        b["sell"] += m.sell
        b["ofi"] += m.ofi
        if m.book_n:
            b["qi"] += m.qi_sum / m.book_n
            b["qn"] += 1
    out = []
    for k in sorted(by):
        b = by[k]
        tot = b["buy"] + b["sell"]
        out.append({"ts": k, "buy": b["buy"], "sell": b["sell"], "delta_pct": round((b["buy"] - b["sell"]) / tot * 100, 1) if tot else None,
                    "ofi": b["ofi"], "queue_imbalance": round(b["qi"] / b["qn"], 3) if b["qn"] else None})
    return out


def _reaction(zones: list[dict[str, Any]], o: float, hi: float, lo: float, last: float) -> list[dict[str, Any]]:
    """Each pivot zone today's range reached: rejected (touched, price back on the near side),
    broken (price now beyond it) or holding (price inside it now)."""
    out = []
    for z in zones:
        if z["high"] < lo or z["low"] > hi:
            continue
        if z["low"] <= last <= z["high"]:
            status = "inside now"
        elif z["low"] > o:  # above the open: a resistance the day ran into
            status = "broken through, holding above" if last > z["high"] else "rejected"
        elif z["high"] < o:
            status = "broken through, holding below" if last < z["low"] else "held as support"
        else:
            status = "opened in it, " + ("left upward" if last > z["high"] else "left downward")
        out.append({"zone": f"{z['low']:,.2f}–{z['high']:,.2f}", "members": z["members"], "status": status})
    return out


def build(engine: Any, symbol: str, now: float) -> dict[str, Any] | None:
    sym = symbol.upper()
    g = engine.groups.get(sym)
    if g is None:
        return None
    today = ist_day(now)
    daily = [(b.ts, b.open, b.high, b.low, b.close, b.volume) for b in engine.store.bars(sym, "1d")]
    m1 = [b for b in engine.store.bars(sym, "1m") if ist_day(b.ts) == today and b.ts + 60 <= now]
    last = m1[-1].close if m1 else None
    out: dict[str, Any] = {"symbol": sym, "ts": now, "sector": engine.scanner.sectors.get(sym, "")}

    out["big_picture"] = big_picture(daily, today, last)
    pos = positioning(engine.oi_daily, sym, today)
    out["positioning"] = pos

    lv = levels_from_store(engine.store, sym, now)
    out["levels"] = lv
    st = structure_from_store(engine.store, sym, now)
    if st is not None:
        s = st.to_json()
        out["structure"] = {
            "setup": s.get("setup"), "zones": [{k: z[k] for k in ("side", "low", "high", "state", "bos_kind", "retests")} for z in s.get("zones", [])],
            "sweeps": s.get("sweeps", [])[-3:], "liquidity": s.get("liquidity", [])[:12], "opening_range": s.get("opening_range"),
        }
    pr = profile_from_store(engine.store, sym, now, session_open_ts(g.segment, today))
    if pr:
        mk, vp = pr.get("market") or {}, pr.get("volume") or {}
        out["profile"] = {
            "market": {k: mk.get(k) for k in ("day_type", "poc", "vah", "val", "ib_high", "ib_low", "poor_high", "poor_low")},
            "volume": {k: vp.get(k) for k in ("poc", "vah", "val", "hvn", "lvn")},
        }

    if m1:
        o, hi, lo = m1[0].open, max(b.high for b in m1), min(b.low for b in m1)
        pv = sum((b.high + b.low + b.close) / 3 * b.volume for b in m1)
        vol = sum(b.volume for b in m1)
        bars15: dict[int, list[Any]] = {}
        for b in m1:
            bars15.setdefault(int(b.ts - b.ts % 900), []).append(b)
        out["today"] = {
            "open": o, "high": hi, "low": lo, "last": last, "vwap": round(pv / vol, 2) if vol else None,
            "gap_pct": round((o / out["big_picture"]["prev_close"] - 1) * 100, 2) if out.get("big_picture") else None,
            "bars_15m": [{"ts": k, "o": v[0].open, "h": max(x.high for x in v), "l": min(x.low for x in v), "c": v[-1].close,
                          "v": sum(x.volume for x in v)} for k, v in sorted(bars15.items())],
            "zone_reaction": _reaction(lv["clusters"], o, hi, lo, last) if lv else [],
            "flow_cash": _flow_buckets(engine.flow.minutes(g.underlying.scrip_code)),
        }
    fut = g.futures[0] if g.futures else None
    if fut is not None:
        out["today"] = out.get("today", {})
        out["today"]["flow_future"] = _flow_buckets(engine.flow.minutes(fut.scrip_code))
        fmins = engine.flow.minutes(fut.scrip_code)
        out["buildup"] = mtf(
            [(b.ts + 60, b.close) for b in m1],
            [(m.ts + 60, m.oi) for m in fmins if m.oi],
            now,
            prev_close=out["big_picture"]["prev_close"] if out.get("big_picture") else None,
            oi_ref=engine.oi_ref.get(fut.scrip_code),
            sessions=(pos or {}).get("sessions", []),
        )
    out["micro"] = {
        "cash": engine.micro.snapshot(g.underlying.scrip_code),
        "future": engine.micro.snapshot(fut.scrip_code) if fut else None,
    }
    fno = engine.fno_snapshot(sym, now)
    out["options"] = fno
    if fno and fno.get("expiries"):
        e = fno["expiries"][0]
        f_px = (e.get("future") or {}).get("ltp")
        zones = (lv or {}).get("clusters", [])
        res = [z for z in zones if f_px and z["low"] > f_px]
        sup = [z for z in zones if f_px and z["high"] < f_px]
        lot = g.options[0].lot_size if g.options else (fut.lot_size if fut else 1)
        out["hedge"] = setups(
            e.get("rows") or [], f_px or 0.0, ((fno.get("reading") or {}).get("bias") or "Neutral"), lot,
            (e.get("call_wall") or {}).get("strike"), (e.get("put_wall") or {}).get("strike"),
            min((z["low"] for z in res), default=None), max((z["high"] for z in sup), default=None),
        ) if f_px else None
    return out


def compact(b: dict[str, Any]) -> dict[str, Any]:
    """The bundle without what an LLM does not need (long series, raw strike rows beyond ±4)."""
    c = dict(b)
    if c.get("micro"):
        c["micro"] = {k: ({kk: vv for kk, vv in (v or {}).items() if kk not in ("walls",)} if v else None) for k, v in c["micro"].items()}
    if c.get("levels"):
        c["levels"] = {"last": c["levels"].get("last"), "adr": c["levels"].get("adr"), "clusters": c["levels"].get("clusters"),
                       "pivots": c["levels"].get("levels")}
    return c


__all__ = ["build", "compact", "date"]
