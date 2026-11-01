"""Positioning from NSE's daily F&O bhavcopy: per session the futures' close and OI with the
build-up it reads as, the put-call ratio of the nearest expiry, and where its biggest OI sits.

Build-up per session (futures, all months together): price up + OI up = long build-up (LB), price
down + OI up = short build-up (SB), price up + OI down = short covering (SC), price down + OI down =
long unwinding (LU). Under ``FLAT_OI_PCT`` of OI change it is flat. "Price up" is the near contract
against its own previous close; "OI up" is today's summed OI against yesterday's summed OI as
listed yesterday (both as ``research/oi_daily.futures_table`` reads them).

Futures OI is given two ways: ``fut_oi`` sums every month (it does not jump at a rollover, so the
build-up is read from it) and ``fut_oi_front`` is the nearest month alone — the basis the live feed
and the F&O panel use. They differ by the next months' OI (RELIANCE 22 Oct: 166.6M vs 90.9M)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from ..market.fo_bhavcopy import OiDailyStore

FLAT_OI_PCT = 0.5
WALLS_SHOWN = 5


def positioning(
    store: OiDailyStore, symbol: str, before: date, sessions: int = 16, *, is_holiday: Callable[[date], bool] | None = None
) -> dict[str, Any] | None:
    """``is_holiday`` names the weekdays in the window the exchange was shut, so a gap in the rows
    reads as a holiday and not as missing data."""
    days = [d for d in store.days() if d < before][-sessions:]
    rows: list[dict[str, Any]] = []
    last = None
    prev_oi: float | None = None  # yesterday's summed futures OI, as listed yesterday
    prev_close_by_expiry: dict[Any, float] = {}  # each contract's own previous close
    for d in days:
        x = store.load(d, columns=("symbol", "kind", "expiry", "strike", "option_type", "close", "oi", "oi_change"))
        x = x[x["symbol"] == symbol]
        fut = x[x["kind"].isin(("FUTSTK", "FUTIDX"))].sort_values("expiry")
        if fut.empty:
            continue
        near_fut = fut["expiry"].iloc[0]
        close = float(fut["close"].iloc[0])
        oi, chg = float(fut["oi"].sum()), float(fut["oi_change"].sum())
        front_oi, front_chg = float(fut["oi"].iloc[0]), float(fut["oi_change"].iloc[0])
        opt = x[x["kind"].isin(("OPTSTK", "OPTIDX"))]
        # the nearest expiry still ahead of this session: on expiry day the dying expiry's PCR and
        # walls are its unwind, not a positioning read (the research rolls 5 sessions before)
        exps = sorted(e for e in opt["expiry"].unique() if e > d) or sorted(opt["expiry"].unique())
        near = exps[0] if exps else None
        on = opt[opt["expiry"] == near] if near else opt
        ce, pe = on[on["option_type"] == "CE"], on[on["option_type"] == "PE"]
        label = None
        # price: the near contract against ITS OWN previous close (``research/oi_daily`` does the
        # same). Comparing the nearest close across the roll read every post-expiry day as "up" by
        # the next month's premium. OI: yesterday's summed OI as listed yesterday — reconstructing
        # it from today's rows' changes misses the contract that expired, +66 % on the day after.
        prev_close = prev_close_by_expiry.get(near_fut)
        if prev_close is not None and prev_oi:
            oi_pct = (oi / prev_oi - 1) * 100
            up = close >= prev_close
            label = "FLAT" if abs(oi_pct) < FLAT_OI_PCT else ("LB" if up else "SB") if oi > prev_oi else ("SC" if up else "LU")
        ce_above = ce[ce["strike"] >= close] if len(ce) else ce
        pe_below = pe[pe["strike"] <= close] if len(pe) else pe
        rows.append({
            "day": d.isoformat(), "fut_close": round(close, 2), "fut_oi": oi, "fut_oi_chg": chg,
            "fut_oi_front": front_oi, "fut_oi_front_chg": front_chg, "front_expiry": str(near_fut),
            "build_up": label, "pcr": round(float(pe["oi"].sum()) / float(ce["oi"].sum()), 2) if ce["oi"].sum() > 0 else None,
            # walls are one-sided: the biggest call OI at or above the price, the biggest put OI at or below it
            "call_wall": float(ce_above.loc[ce_above["oi"].idxmax(), "strike"]) if len(ce_above) and ce_above["oi"].max() > 0 else None,
            "put_wall": float(pe_below.loc[pe_below["oi"].idxmax(), "strike"]) if len(pe_below) and pe_below["oi"].max() > 0 else None,
            "expiry": near,
        })
        prev_oi = oi
        prev_close_by_expiry = {e: float(c) for e, c in zip(fut["expiry"], fut["close"], strict=True)}
        last = (d, ce, pe, near)
    if not rows or last is None:
        return None
    d, ce, pe, near = last
    top = lambda t: [  # noqa: E731
        {"strike": float(r.strike), "oi": float(r.oi), "oi_chg": float(r.oi_change), "close": float(r.close)}
        for r in t.nlargest(WALLS_SHOWN, "oi").itertuples()
    ]
    first, end = rows[0], rows[-1]
    holidays = []
    if is_holiday is not None:
        d0 = date.fromisoformat(first["day"])
        while d0 < before:
            if d0.weekday() < 5 and is_holiday(d0):
                holidays.append(d0.isoformat())
            d0 += timedelta(days=1)
    return {
        "holidays": holidays,
        "sessions": rows,
        "summary": {
            "from": first["day"], "to": end["day"],
            "fut_oi_change_pct": round((end["fut_oi"] / first["fut_oi"] - 1) * 100, 2) if first["fut_oi"] else None,
            "basis": "fut_oi = all months (rollover-neutral); fut_oi_front = nearest month, as the live feed",
            "fut_price_change_pct": round((end["fut_close"] / first["fut_close"] - 1) * 100, 2),
            "pcr_from": first["pcr"], "pcr_to": end["pcr"],
        },
        "latest_day": d.isoformat(), "expiry": near, "top_calls": top(ce), "top_puts": top(pe),
    }


__all__ = ["positioning"]
