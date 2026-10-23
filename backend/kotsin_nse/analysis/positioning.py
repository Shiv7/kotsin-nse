"""Positioning from NSE's daily F&O bhavcopy: per session the futures' close and OI with the
build-up it reads as, the put-call ratio of the nearest expiry, and where its biggest OI sits.

Build-up per session (futures, all months together): price up + OI up = long build-up (LB), price
down + OI up = short build-up (SB), price up + OI down = short covering (SC), price down + OI down =
long unwinding (LU). Under ``FLAT_OI_PCT`` of OI change it is flat."""

from __future__ import annotations

from datetime import date
from typing import Any

from ..market.fo_bhavcopy import OiDailyStore

FLAT_OI_PCT = 0.5
WALLS_SHOWN = 5


def positioning(store: OiDailyStore, symbol: str, before: date, sessions: int = 16) -> dict[str, Any] | None:
    days = [d for d in store.days() if d < before][-sessions:]
    rows: list[dict[str, Any]] = []
    last = None
    prev_close = None
    for d in days:
        x = store.load(d, columns=("symbol", "kind", "expiry", "strike", "option_type", "close", "oi", "oi_change"))
        x = x[x["symbol"] == symbol]
        fut = x[x["kind"].isin(("FUTSTK", "FUTIDX"))].sort_values("expiry")
        if fut.empty:
            continue
        close = float(fut["close"].iloc[0])
        oi, chg = float(fut["oi"].sum()), float(fut["oi_change"].sum())
        opt = x[x["kind"].isin(("OPTSTK", "OPTIDX"))]
        near = sorted(opt["expiry"].unique())[0] if len(opt) else None
        on = opt[opt["expiry"] == near] if near else opt
        ce, pe = on[on["option_type"] == "CE"], on[on["option_type"] == "PE"]
        label = None
        if prev_close is not None and oi - chg > 0:
            oi_pct = chg / (oi - chg) * 100
            up = close >= prev_close
            label = "FLAT" if abs(oi_pct) < FLAT_OI_PCT else ("LB" if up else "SB") if chg > 0 else ("SC" if up else "LU")
        rows.append({
            "day": d.isoformat(), "fut_close": round(close, 2), "fut_oi": oi, "fut_oi_chg": chg,
            "build_up": label, "pcr": round(float(pe["oi"].sum()) / float(ce["oi"].sum()), 2) if ce["oi"].sum() > 0 else None,
            "call_wall": float(ce.loc[ce["oi"].idxmax(), "strike"]) if len(ce) else None,
            "put_wall": float(pe.loc[pe["oi"].idxmax(), "strike"]) if len(pe) else None,
            "expiry": near,
        })
        prev_close = close
        last = (d, ce, pe, near)
    if not rows or last is None:
        return None
    d, ce, pe, near = last
    top = lambda t: [  # noqa: E731
        {"strike": float(r.strike), "oi": float(r.oi), "oi_chg": float(r.oi_change), "close": float(r.close)}
        for r in t.nlargest(WALLS_SHOWN, "oi").itertuples()
    ]
    first, end = rows[0], rows[-1]
    return {
        "sessions": rows,
        "summary": {
            "from": first["day"], "to": end["day"],
            "fut_oi_change_pct": round((end["fut_oi"] / first["fut_oi"] - 1) * 100, 2) if first["fut_oi"] else None,
            "fut_price_change_pct": round((end["fut_close"] / first["fut_close"] - 1) * 100, 2),
            "pcr_from": first["pcr"], "pcr_to": end["pcr"],
        },
        "latest_day": d.isoformat(), "expiry": near, "top_calls": top(ce), "top_puts": top(pe),
    }


__all__ = ["positioning"]
