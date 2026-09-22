"""One backtest trade, explained: the bars around it, the strategy's own indicator lines, the
zones the confluence engine saw, the levels, what the price did next, and FUKAA's verdict on the
same trigger.

Nothing here is re-derived from the market. The signal, gates, zones and FUKAA verdict are what
the backtester stored at the decision (``BtTrade.signal`` / ``.fukaa``); the indicator lines come
from the strategy's own ``bollinger`` / ``supertrend`` with the run's own config, so a line that
disagrees with the stored trigger values is a bug, not a rendering choice; the path is the
committee's ``path_metrics`` over the bars after the decision, the same function a post-mortem
uses. The API serves this; it does not compute it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..bars.indicators import atr, bollinger, supertrend
from ..bars.unified import BarSource, UnifiedBar
from ..committee.evidence import BarRow, path_metrics
from .backtest import load_run
from .history import HistoryStore

DEFAULT_BEFORE = 80
DEFAULT_AFTER = 16


def _bars(store: HistoryStore, symbol: str, tf: str) -> list[UnifiedBar]:
    df = store.load(symbol, tf)
    return [
        UnifiedBar(
            symbol=symbol,
            scrip_code=symbol,
            tf=tf,
            ts=int(r.ts),
            open=float(r.o),
            high=float(r.h),
            low=float(r.l),
            close=float(r.c),
            volume=float(r.v),
            source=BarSource.REST,
            complete=True,
        )
        for r in df.itertuples()
    ]


def trade_view(
    run_path: Path,
    index: int,
    store: HistoryStore,
    *,
    before: int = DEFAULT_BEFORE,
    after: int = DEFAULT_AFTER,
) -> dict[str, Any]:
    run = load_run(run_path)
    trades = run.get("trades") or []
    if not 0 <= index < len(trades):
        raise KeyError(f"{run_path.stem} has {len(trades)} trades; no index {index}")
    trade = trades[index]
    details = run.get("details") or []
    detail = details[index] if index < len(details) else {}
    signal = detail.get("signal") or None
    fukaa = detail.get("fukaa") or None
    params = (run.get("summary") or {}).get("params") or {}
    fcfg = params.get("fudkii") or {}
    tf = str(params.get("decision_tf") or "30m")

    symbol = str(trade["symbol"])
    entry_ts, exit_ts = int(trade["entry_ts"]), int(trade["exit_ts"])
    decision_ts = int(signal["ts"]) if signal else entry_ts
    bars = _bars(store, symbol, tf)
    if not bars:
        raise KeyError(f"no cached {tf} history for {symbol}")
    i_dec = next((i for i, b in enumerate(bars) if b.ts >= decision_ts), len(bars) - 1)
    i_exit = next((i for i, b in enumerate(bars) if b.ts >= exit_ts), len(bars) - 1)
    lo = max(0, i_dec - before)
    hi = min(len(bars), max(i_exit, i_dec) + after + 1)

    # the strategy's own lines, with the run's own config — computed over the window plus the
    # warm-up before it, so the first visible bar already has a value
    bb_period = int(fcfg.get("bb_period", 20))
    bb_mult = float(fcfg.get("bb_mult", 2.0))
    st_period = int(fcfg.get("st_atr_period", 7))
    st_mult = float(fcfg.get("st_mult", 3.0))
    warm = max(bb_period, st_period) + 5
    calc = bars[max(0, lo - warm) : hi]
    offset = lo - max(0, lo - warm)
    closes = [b.close for b in calc]
    st_pts = supertrend(calc, st_period, st_mult)
    rows: list[dict[str, Any]] = []
    for j, b in enumerate(calc):
        if j < offset:
            continue
        bb = bollinger(closes[: j + 1], bb_period, bb_mult)
        p = st_pts[j]
        rows.append(
            {
                "ts": b.ts,
                "o": b.open,
                "h": b.high,
                "l": b.low,
                "c": b.close,
                "v": b.volume,
                "bb_upper": round(bb.upper, 4) if bb else None,
                "bb_middle": round(bb.middle, 4) if bb else None,
                "bb_lower": round(bb.lower, 4) if bb else None,
                "st_value": round(p.value, 4) if p else None,
                "st_trend": p.trend if p else None,
                # a trade stopped on its fill bar enters and exits on the same candle
                "roles": [
                    r
                    for r, hit in (
                        ("decision", b.ts == bars[i_dec].ts),
                        ("entry", b.ts == entry_ts),
                        ("exit", b.ts == bars[i_exit].ts),
                    )
                    if hit
                ],
            }
        )

    ctx = dict((signal or {}).get("context") or {})
    conf = dict(ctx.get("confluence") or {})
    zones = list(ctx.get("zones") or [])
    bullish = str(trade.get("direction")) == "BULLISH"
    entry, stop = float(trade["entry"]), float(trade["stop"])
    t1 = float(trade["target1"]) if trade.get("target1") is not None else None
    targets = [float(x) for x in (conf.get("targets") or ([t1] if t1 is not None else []))]
    after_rows = [BarRow(b.ts, b.open, b.high, b.low, b.close, b.volume) for b in bars[i_dec + 1 : hi]]
    path = path_metrics(bullish=bullish, entry=entry, stop=stop, t1=t1, bars=after_rows)
    hist_for_atr = bars[max(0, i_dec - 30) : i_dec + 1]
    a = atr(hist_for_atr, 14) if len(hist_for_atr) > 14 else None
    a = float(a) if a else None
    risk = abs(entry - stop)

    return {
        "run_id": run_path.stem,
        "index": index,
        "count": len(trades),
        "tf": tf,
        "trade": trade,
        "signal": signal,
        "fukaa": fukaa,
        "bars": rows,
        "indicator_params": {"bb_period": bb_period, "bb_mult": bb_mult, "st_atr_period": st_period, "st_mult": st_mult},
        "zones": zones,
        "levels": {
            "decision_ts": decision_ts,
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
            "entry": entry,
            "stop": stop,
            "stop_at_exit": trade.get("stop_at_exit"),
            "targets": targets,
            "exit": float(trade["exit"]),
            "risk": round(risk, 4),
            "stop_dist_pct": round(risk / entry * 100, 3) if entry else None,
            "stop_dist_atr": round(risk / a, 3) if a else None,
            "atr": round(a, 4) if a else None,
            "t1_dist_atr": round(abs(t1 - entry) / a, 3) if (t1 is not None and a) else None,
            "stop_zone": conf.get("stop_zone"),
            "target_zones": conf.get("target_zones"),
            "rr": conf.get("rr"),
            "grade": trade.get("grade"),
            "fortress": conf.get("fortress"),
            "room_ratio": conf.get("room_ratio"),
            "note": conf.get("note"),
        },
        "path": path,
    }


__all__ = ["DEFAULT_AFTER", "DEFAULT_BEFORE", "trade_view"]
