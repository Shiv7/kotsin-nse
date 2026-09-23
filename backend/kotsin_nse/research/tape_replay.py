"""Replay an exit policy on the tick tape — the same seconds the engine lived through.

The backtest harnesses stand minute candles in for ticks: four prices per minute and a guessed
path between them. On a session that was taped (``ops/tape.py``) that guess is unnecessary: the
option's top of book and the underlying's last print exist for every second the engine
evaluated, so the policy can be re-run on exactly what it saw. This is the reader side of the
tape — one function, the live ``ExitEngine``, no engine process.

What is assumed, stated: an exit fills at the **bid** on tape (top of book; the depth ladder the
paper matcher walks is not taped) or a tick under the last print when there is no bid; a second
whose quote is older than ``quote_max_age_s`` is skipped, as the live loop skips it, unless the
exit is forced. Anything left at ``end_ts`` is flattened there when ``flatten_at_end`` is set.

What is NOT modelled, so a study does not mistake it for fidelity: the paper matcher's walk down
the depth ladder (only the top of book is taped, so a large exit fills better here than live), a
breaker that tripped during the session (``halted`` / ``daily_loss_hit`` are always false), and the
option's own 1-minute close, which ``ExitEngine`` accumulates per position id — pass a **fresh**
``ExitEngine`` per replay or that state leaks between runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain import ExitDecision, ExitReason, Position
from ..market.session import TF_SECONDS, bucket_start, past_force_flat
from ..ops.tape import Series
from ..risk.exits import ExitEngine, MarketView, apply_exit

#: the frame ``Position.bars_held`` counts live — one per closed decision bar of the underlying
DECISION_TF = "30m"


@dataclass(slots=True)
class TapeEvent:
    ts: int
    reason: str
    qty: int
    fill: float
    note: str

    def to_json(self) -> dict[str, Any]:
        return {"ts": self.ts, "reason": self.reason, "qty": self.qty, "fill": self.fill, "note": self.note}


@dataclass(slots=True)
class TapeReplay:
    events: list[TapeEvent] = field(default_factory=list)
    gross: float = 0.0
    peak_mid: float = 0.0
    seconds: int = 0
    skipped_stale: int = 0
    source: str = "tape"


def replay_on_tape(
    engine: ExitEngine,
    pos: Position,
    option: Series,
    underlying: Series | None,
    *,
    start_ts: float,
    end_ts: float,
    quote_max_age_s: float = 60.0,
    force_flat_from: float | None = None,
    flatten_at_end: bool = True,
) -> TapeReplay:
    """Run ``engine`` over ``pos`` from ``start_ts`` to ``end_ts`` at one second, on the tape.

    ``pos`` is mutated exactly as the live loop mutates it, so pass a position built for this run.
    ``force_flat_from`` overrides the session's own force-flat instant; left unset, the segment's
    real one applies, which is what the live loop does.
    """
    if pos.status != "OPEN" or pos.qty_remaining <= 0:
        raise ValueError(f"{pos.id} is {pos.status} with {pos.qty_remaining} left — replay a fresh position")
    out = TapeReplay()
    tick = pos.instrument.tick_size or 0.05
    segment = pos.underlying.segment
    tf_s = TF_SECONDS[DECISION_TF]
    opened_bucket = bucket_start(segment, pos.opened_ts, DECISION_TF)
    for ts in range(int(start_ts), int(end_ts) + 1):
        q = option.at(ts)
        if q is None:
            continue
        out.seconds += 1
        forced = (ts >= force_flat_from) if force_flat_from is not None else past_force_flat(segment, ts)
        quote_ok = (ts - q.quote_ts) <= quote_max_age_s
        if not quote_ok and not forced:
            out.skipped_stale += 1
            continue
        out.peak_mid = max(out.peak_mid, q.mid)
        u = underlying.at(ts) if underlying is not None else None
        view = MarketView(
            option_ltp=q.ltp,
            underlying_ltp=u.ltp if u is not None else None,
            now=float(ts),
            # live: +1 per CLOSED decision bar of the underlying, not per elapsed 30 minutes
            bars_held=max(0, int((bucket_start(segment, ts, DECISION_TF) - opened_bucket) // tf_s)),
            past_force_flat=forced,
            option_mid=q.mid,
            spread_pct=(q.spread_pct / 100) if q.spread_pct is not None else None,
            quote_ok=quote_ok,
        )
        decision = engine.evaluate(pos, view)
        if decision is None:
            continue
        fill = q.bid if q.bid > 0 else max(tick, round(q.ltp - tick, 2))
        out.gross += apply_exit(pos, decision, fill_price=fill, charges=0.0, now=float(ts))
        out.events.append(TapeEvent(ts, decision.reason.value, decision.qty, fill, decision.note[:120]))
        if pos.qty_remaining <= 0:
            break
    if flatten_at_end and pos.qty_remaining > 0 and option.last_ts is not None:
        # The last row on tape, not a forward-fill of it: stamping a quote from hours earlier at
        # end_ts would read as a fill that never existed.
        last_ts = min(int(end_ts), option.last_ts)
        q = option.at(last_ts)
        if q is not None:
            fill = q.bid if q.bid > 0 else max(tick, round(q.ltp - tick, 2))
            qty = pos.qty_remaining
            note = f"end of tape ({int(end_ts) - last_ts}s after the last quote)"
            d = ExitDecision(pos.id, ExitReason.EOD, fill, qty, note)
            out.gross += apply_exit(pos, d, fill_price=fill, charges=0.0, now=float(last_ts))
            out.events.append(TapeEvent(last_ts, d.reason.value, qty, fill, note))
    return out


__all__ = ["TapeEvent", "TapeReplay", "replay_on_tape"]
