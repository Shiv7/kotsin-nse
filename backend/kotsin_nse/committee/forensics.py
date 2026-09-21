"""Deterministic diagnostics over a cohort of trades — the tables the committee reads.

The model never sees raw trades. It sees these numbers, computed by tested code and keyed so they
can be cited (``by_stop_pct.<0.25.avg_r``), and argues about which of them explain the result.
Trading-R1's first imperative — input quality — applied to post-mortems: a model that reads 481
rows finds patterns that are not there; a model that reads 481 rows bucketed nine ways with
day-clustered t-statistics can only point at a bucket, and the bucket can be checked.

Two trade shapes exist and both are normalised into :class:`TradeRec` first, so a bucket means
the same thing whichever produced it: the backtester's ``BtTrade`` (underlying levels, the 481)
and the ledger's ``Trade`` (option premiums, with the underlying levels alongside).
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import accumulate
from typing import Any

from ..market.session import ist_day, ist_hm
from ..research.stats import day_clustered_mean, max_drawdown, profit_factor

DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
STOP_PCT_EDGES: tuple[tuple[float | None, str], ...] = (
    (0.25, "<0.25"),
    (0.5, "0.25-0.5"),
    (1.0, "0.5-1"),
    (2.0, "1-2"),
    (None, ">=2"),
)
RR_EDGES: tuple[tuple[float | None, str], ...] = (
    (1.0, "<1"),
    (1.5, "1-1.5"),
    (2.5, "1.5-2.5"),
    (None, ">=2.5"),
)
HELD_EDGES: tuple[tuple[float | None, str], ...] = (
    (1, "1"),
    (4, "2-4"),
    (12, "5-12"),
    (None, ">12"),
)
STOP_REASONS = ("SL-EQ", "SL-OP")
#: a symbol bucket needs this many trades before it is worth a row
MIN_SYMBOL_N = 3


@dataclass(slots=True)
class TradeRec:
    strategy: str
    symbol: str
    direction: str
    day: str
    entry_ts: int
    exit_ts: int
    entry: float  # underlying at entry
    stop: float  # underlying stop
    target1: float | None
    r: float
    net: float
    gross: float
    charges: float
    mfe_r: float
    mae_r: float
    exit_reason: str
    grade: str
    bars_held: int
    source: str

    @property
    def stop_pct(self) -> float | None:
        return abs(self.entry - self.stop) / self.entry * 100 if self.entry else None

    @property
    def rr(self) -> float | None:
        if self.target1 is None or not self.entry:
            return None
        risk = abs(self.entry - self.stop)
        return abs(self.target1 - self.entry) / risk if risk > 0 else None


def from_backtest(d: Mapping[str, Any]) -> TradeRec:
    return TradeRec(
        strategy=str(d["strategy"]),
        symbol=str(d["symbol"]),
        direction=str(d.get("direction") or ""),
        day=str(d["day"]),
        entry_ts=int(d["entry_ts"]),
        exit_ts=int(d["exit_ts"]),
        entry=float(d["entry"]),
        stop=float(d["stop"]),
        target1=float(d["target1"]) if d.get("target1") is not None else None,
        r=float(d["r_multiple"]),
        net=float(d["net"]),
        gross=float(d["gross"]),
        charges=float(d["charges"]),
        mfe_r=float(d.get("mfe_r") or 0.0),
        mae_r=float(d.get("mae_r") or 0.0),
        exit_reason=str(d["exit_reason"]),
        grade=str(d.get("grade") or ""),
        bars_held=int(d.get("bars_held") or 1),
        source="backtest",
    )


def from_ledger(d: Mapping[str, Any]) -> TradeRec:
    """A ledger trade carries the option premiums in ``entry``/``exit``; the underlying levels
    the decision was made on are ``equity_entry`` / ``equity_sl`` / ``equity_targets``. The
    direction is not stored on the trade — it follows from which side of the entry the stop is."""
    entry = float(d.get("equity_entry") or 0.0)
    stop = float(d.get("equity_sl") or 0.0)
    targets = d.get("equity_targets") or []
    opened, closed = float(d["opened_ts"]), float(d["closed_ts"])
    return TradeRec(
        strategy=str(d["strategy"]),
        symbol=str(d.get("underlying") or d["symbol"]),
        direction="BULLISH" if stop < entry else "BEARISH",
        day=ist_day(opened).isoformat(),
        entry_ts=int(opened),
        exit_ts=int(closed),
        entry=entry,
        stop=stop,
        target1=float(targets[0]) if targets else None,
        r=float(d["r_multiple"]),
        net=float(d["net"]),
        gross=float(d["gross"]),
        charges=float(d["charges"]),
        mfe_r=float(d.get("mfe_r") or 0.0),
        mae_r=float(d.get("mae_r") or 0.0),
        exit_reason=str(d["exit_reason"]),
        grade=str(d.get("grade") or ""),
        # the ledger keeps the hold as seconds; the decision frame is 30 minutes
        bars_held=max(1, round(float(d.get("duration_s") or 0.0) / 1800)),
        source="ledger",
    )


# -- buckets ------------------------------------------------------------------------------------


def _edge_label(
    v: float | None,
    edges: Sequence[tuple[float | None, str]],
    missing: str,
    *,
    inclusive: bool = False,
) -> str:
    """``inclusive`` edges are upper bounds that belong to their bucket (bars held 1 → "1");
    exclusive ones are the conventional half-open percentage bins (0.25 → "0.25-0.5")."""
    if v is None:
        return missing
    for edge, label in edges:
        if edge is None or (v <= edge if inclusive else v < edge):
            return label
    return edges[-1][1]


def _bucket(label: str, rows: Sequence[TradeRec], total_loss: float) -> dict[str, Any]:
    if not rows:
        return {"label": label, "n": 0}
    cm = day_clustered_mean([t.r for t in rows], [t.day for t in rows])
    net = sum(t.net for t in rows)
    loss = sum(t.net for t in rows if t.net < 0)
    return {
        "label": label,
        "n": cm.n,
        "n_days": cm.n_days,
        "avg_r": round(cm.mean, 3),
        "t": round(cm.t_stat, 2) if cm.t_stat is not None else None,
        "win_rate": round(sum(1 for t in rows if t.net > 0) / len(rows) * 100, 1),
        "net": round(net, 2),
        "loss_share": round(loss / total_loss * 100, 1) if total_loss < 0 else 0.0,
        "too_small": cm.too_small,
    }


def _dim(
    rows: Iterable[TradeRec],
    key: Callable[[TradeRec], str],
    total_loss: float,
    *,
    order: Sequence[str] | None = None,
    min_n: int = 1,
    sort_by_net: bool = False,
) -> list[dict[str, Any]]:
    groups: dict[str, list[TradeRec]] = defaultdict(list)
    for t in rows:
        groups[key(t)].append(t)
    labels = list(order) if order else sorted(groups)
    out = [_bucket(lab, groups.get(lab, []), total_loss) for lab in labels if len(groups.get(lab, [])) >= min_n]
    if sort_by_net:
        out.sort(key=lambda b: b["net"])
    return out


def forensics(rows: Sequence[TradeRec]) -> dict[str, Any]:
    """Every number the committee may cite, computed once."""
    if not rows:
        return {"cohort": {"n": 0}, "dims": {}}
    total_loss = sum(t.net for t in rows if t.net < 0)
    cm = day_clustered_mean([t.r for t in rows], [t.day for t in rows])
    nets_in_order = [t.net for t in sorted(rows, key=lambda t: t.exit_ts)]
    gross = sum(t.gross for t in rows)
    charges = sum(t.charges for t in rows)
    first_bar_stops = [t for t in rows if t.bars_held <= 1 and t.exit_reason in STOP_REASONS]
    gave_back = [t for t in rows if t.mfe_r >= 1.0 and t.r <= 0]
    captures = [t.r / t.mfe_r for t in rows if t.mfe_r >= 0.25]
    stop_pcts = [p for t in rows if (p := t.stop_pct) is not None]
    cohort: dict[str, Any] = {
        "n": cm.n,
        "n_days": cm.n_days,
        "avg_r": round(cm.mean, 3),
        "avg_r_stderr": round(cm.stderr, 4) if cm.stderr else None,
        "avg_r_t": round(cm.t_stat, 2) if cm.t_stat is not None else None,
        "too_small": cm.too_small,
        "win_rate": round(sum(1 for t in rows if t.net > 0) / len(rows) * 100, 1),
        "net": round(sum(t.net for t in rows), 2),
        "gross": round(gross, 2),
        "charges": round(charges, 2),
        "charges_share_of_gross": round(charges / abs(gross) * 100, 1) if gross else None,
        "profit_factor": profit_factor(nets_in_order),
        "max_drawdown": round(max_drawdown(list(accumulate(nets_in_order))), 2),
        "stop_hit_rate": round(
            sum(1 for t in rows if t.exit_reason in STOP_REASONS) / len(rows) * 100, 1
        ),
        "first_bar_stop_rate": round(len(first_bar_stops) / len(rows) * 100, 1),
        "first_bar_stop_avg_r": round(
            sum(t.r for t in first_bar_stops) / len(first_bar_stops), 3
        )
        if first_bar_stops
        else None,
        "give_back_rate": round(len(gave_back) / len(rows) * 100, 1),
        "mfe_capture_median": round(statistics.median(captures), 3) if captures else None,
        "t1_hit_rate": round(
            sum(1 for t in rows if t.exit_reason == "TARGET") / len(rows) * 100, 1
        ),
        "eod_rate": round(sum(1 for t in rows if t.exit_reason == "EOD") / len(rows) * 100, 1),
        "avg_mfe_r": round(sum(t.mfe_r for t in rows) / len(rows), 3),
        "avg_mae_r": round(sum(t.mae_r for t in rows) / len(rows), 3),
        "median_stop_pct": round(statistics.median(stop_pcts), 3) if stop_pcts else None,
        "median_bars_held": statistics.median(t.bars_held for t in rows),
        "sources": dict(Counter(t.source for t in rows)),
        "first_day": min(t.day for t in rows),
        "last_day": max(t.day for t in rows),
    }
    dims: dict[str, list[dict[str, Any]]] = {
        "strategy": _dim(rows, lambda t: t.strategy, total_loss),
        "grade": _dim(rows, lambda t: t.grade or "?", total_loss),
        "direction": _dim(rows, lambda t: t.direction or "?", total_loss),
        "exit_reason": _dim(rows, lambda t: t.exit_reason, total_loss),
        "stop_pct": _dim(
            rows,
            lambda t: _edge_label(t.stop_pct, STOP_PCT_EDGES, "?"),
            total_loss,
            order=[lab for _, lab in STOP_PCT_EDGES],
        ),
        "rr": _dim(
            rows,
            lambda t: _edge_label(t.rr, RR_EDGES, "no_t1"),
            total_loss,
            order=[*(lab for _, lab in RR_EDGES), "no_t1"],
        ),
        "bars_held": _dim(
            rows,
            lambda t: _edge_label(t.bars_held, HELD_EDGES, "?", inclusive=True),
            total_loss,
            order=[lab for _, lab in HELD_EDGES],
        ),
        "hour_ist": _dim(rows, lambda t: ist_hm(t.entry_ts)[:2], total_loss),
        "dow": _dim(
            rows,
            lambda t: DOW[ist_day(t.entry_ts).weekday()],
            total_loss,
            order=list(DOW[:6]),
        ),
        "month": _dim(rows, lambda t: t.day[:7], total_loss),
        "symbol": _dim(
            rows, lambda t: t.symbol, total_loss, min_n=MIN_SYMBOL_N, sort_by_net=True
        ),
    }
    return {"cohort": cohort, "dims": dims}


def flat(f: Mapping[str, Any]) -> dict[str, Any]:
    """The citable key space: ``cohort.<k>`` and ``by_<dim>.<label>.<metric>``."""
    out: dict[str, Any] = {f"cohort.{k}": v for k, v in f.get("cohort", {}).items()}
    for dim, buckets in f.get("dims", {}).items():
        for b in buckets:
            for k, v in b.items():
                if k != "label":
                    out[f"by_{dim}.{b['label']}.{k}"] = v
    return out


def _fmt(v: Any) -> str:
    if v is None:
        return "missing"
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:,.0f}"
    return str(v)


def render(f: Mapping[str, Any], *, title: str = "") -> str:
    lines = [f"COHORT {title}".rstrip(), ""]
    for k, v in f.get("cohort", {}).items():
        lines.append(f"cohort.{k}: {_fmt(v)}")
    lines.append("")
    lines.append("Buckets: n, days, avg_r (day-clustered mean R), t, win%, net ₹, loss_share% (of the cohort's losing ₹); too_small = <30 trades or <10 days")
    for dim, buckets in f.get("dims", {}).items():
        rows = buckets
        if dim == "symbol" and len(rows) > 10:
            rows = [*rows[:5], {"label": "…", "n": 0}, *rows[-5:]]
        lines.append("")
        for b in rows:
            if b.get("n", 0) == 0:
                lines.append(f"by_{dim}.{b['label']}: (none)" if b["label"] != "…" else "  …")
                continue
            lines.append(
                f"by_{dim}.{b['label']}: n={b['n']} days={b['n_days']} avg_r={_fmt(b['avg_r'])} "
                f"t={_fmt(b['t'])} win={b['win_rate']}% net={b['net']:,.0f} "
                f"loss_share={b['loss_share']}%{' too_small' if b['too_small'] else ''}"
            )
    return "\n".join(lines)


__all__ = [
    "TradeRec",
    "flat",
    "forensics",
    "from_backtest",
    "from_ledger",
    "render",
]
