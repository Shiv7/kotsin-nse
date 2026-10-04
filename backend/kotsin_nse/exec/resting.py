"""Paper limit orders: where to rest one, when it fills, when to reprice, when to give up.

Operator, 2026-09-26: "put limit orders at entry price of that OTM and check if Bid-Ask has that
number, if yes, wait, if not then place order on the mid of bid-ask in favour of the trade" / "check
orderbook at the time of entry and exit both and place limit order and note the signal … limit-order
placed … and order executed time stamp". The Sep 1–25 replay paid ~3.5% of the premium per round
trip to the spread and charges — buying at the ask and selling at the bid was most of it.

* **Entry** — BUY LIMIT at the OTM's price at the signal (the selection premium) when the book still
  straddles it (bid ≤ ref ≤ ask); otherwise at the book's mid. A buy never starts at the ask, and is
  never placed, moved or taken above the signal price + ``entry_cap_pct`` (operator, 2026-09-26:
  "Cap the 30-second buy price … no more than 3% above the signal price of the option"). For the
  first ``entry_hold_s`` it does not move (operator: the limit moves to the mid "after 30s wait");
  after that a ref that has left the book is repriced to the new mid, under the cap. Unfilled at the
  deadline → the trigger is recorded as missed.
* **Exit** — SELL LIMIT at the mid, walked toward the bid on every reprice, and crossed (sold at the
  bid through the book) at a deadline set by the exit's urgency, so an exit is never left working.
* **Urgent stops** — operator, 2026-10-03: "when the stoploss is hit and sustained ... if there are
  already buyers at the price we want to sell, why place order at the mid? ... if the momentum is very
  high, then stoploss if not executed quickly and waited will attract high losses ... place order
  smartly not blindly". A stop sells into the bid AT ONCE (through the depth, for every lot) when the
  stock has broken its stop (SL-EQ), the option's mid has fallen ``exit_fast_fall_pct`` % or more in
  ``exit_fast_window_s``, or the book is ``exit_tight_ticks`` ticks wide or less; a calm option stop on
  a wide book still rests at the mid and walks. Live paper 30 Sep - 1 Oct, 43 stop exits: +₹23,224
  after the depth (DMART: bid 107.10 when its stock stop fired, crossed at 94.97 fifteen seconds
  later); +₹5,370 without DMART; the 25 Sep - 1 Oct replay, whose books are built from 1-minute
  candles and cannot show a 15-second collapse, −₹5,209. Insurance against the collapse, about even
  otherwise. Trail, target and 15:20 exits keep the walk (it beat the bid there).
* **Momentum (entry)** — operator, 2026-09-26: "if it is racing ahead, take a call in 30th second and
  get in quickly", measured on "the option's move not the stock/equity's move". The run is the
  OPTION's mid against the signal price, in % of that price. One look at ``entry_race_check_s``: a
  run of ``entry_race_pct`` or more takes the ask — only while the ask is within the cap. Between
  30 s and the deadline the limit follows the mid under the cap (the "trade at the current price"
  call, made passively); at the deadline it is missed — the Sep 1-25 replay lost ₹0.9-2.4 L a month
  buying the ask at 60 s. In % and not in ATR: the option's own intraday ATR is not known at the
  trigger (its candles are fetched only once the strike is chosen), and the cap is itself in %.
* **Target sells placed in advance** — operator, 2026-09-26: "upon approaching the target … why not
  place order in advance? … first come first serve has our name too and in case it is a
  touch-and-fall case, we at least make profit on lot 1". Every book whose target is a touch rests
  the next rung's SELL at the rung (risk/exits.py ``ExitEngine.resting_target``) from the moment
  it holds the contract. It fills on a TOUCH — the best bid at or over the limit, or a print at or
  over it (``touch_fills``); that is optimistic: a real order at the rung waits behind the queue
  already there, and a print AT the level may not reach it. Any other exit cancels it first.
* **Fill rule (paper)** — a resting buy fills when the best ask comes down to its limit, or a NEW trade
  prints below it; a sell when the best bid comes up to its limit, or a new trade prints above it. A
  last price that has not changed since the order last looked is an old print, not a trade through
  it. It fills at the limit, whole. Queue position and partial fills are NOT modelled: a real resting order
  at the touch waits behind everyone already there, so these fills are optimistic by that queue.

Pure: book and clock in, prices and verdicts out — the engine owns the orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain import ExitReason, OrderIntent

#: exits that must get out — a breached stop or its hard floor
STOP_REASONS = frozenset({ExitReason.SL_EQ, ExitReason.SL_OP})
#: exits the clock or the operator forces — the shortest fuse
URGENT_REASONS = frozenset({ExitReason.EOD, ExitReason.HALT, ExitReason.DAILY_LOSS, ExitReason.MANUAL})


@dataclass(frozen=True, slots=True)
class LimitPolicy:
    """The paper limit-order policy. Seconds throughout."""

    enabled: bool = True
    #: an entry rests at most this long, then the trigger is recorded as missed
    entry_wait_s: float = 60.0
    #: at the deadline, take the ask if it is within this % of the signal price; None = cancel (missed)
    entry_chase_pct: float | None = None
    #: how often a resting entry re-reads the book (a ref that has left it is repriced to the mid)
    entry_recheck_s: float = 5.0
    #: the entry does not move for this long — at the signal price (or the mid if the book had left it)
    entry_hold_s: float = 30.0
    #: a BUY is never placed, repriced or taken above the signal price + this %; None = no cap
    entry_cap_pct: float | None = 3.0
    #: momentum: one look at this many seconds — the option's mid up ``entry_race_pct`` % or more on the
    #: signal price takes the ask, if the ask is within the cap ("racing"); None = no early call
    entry_race_check_s: float = 30.0
    #: OFF by default: the Sep 1-25 replay (fresh purses, net) lost ₹5k-16k a month with it at +1 %
    entry_race_pct: float | None = None
    #: operator, 2026-09-26 (under test): "wait for 30s for the limit order to fill, if not, then fill
    #: order at the least price within 3% cap as soon as possible" — after ``entry_hold_s`` the entry
    #: takes the ask the first time the ask is within the cap (the lowest price it can be bought at
    #: now), until the deadline; the resting limit is not raised meanwhile (raised to the cap it would
    #: fill AT the cap when the ask dips under it). False = the limit follows the mid, missed at the deadline
    entry_cross_after_hold: bool = False
    #: how often a resting exit steps from the mid toward the bid
    exit_reprice_s: float = 5.0
    #: an exit is crossed (sold at the bid) after this long: stops, then force-flat/halt/daily-loss/manual, then the rest
    exit_cross_stop_s: float = 15.0
    exit_cross_urgent_s: float = 10.0
    exit_cross_other_s: float = 45.0
    #: rest the next target sell in advance for the books whose target is a touch
    rest_targets: bool = True
    #: a stop sells into the bid the moment its rule fires, every stop (``urgent_stop``; operator,
    #: 2026-10-04: the simulated stop must not cross its level and then fill at a later, worse price for
    #: want of a stop order). The engine's stop is a software stop — a 1 s read of the level, then an
    #: order — so what it can execute at the trigger is the bid, walked through the depth for the lots:
    #: that is the fill. Resting a limit at the mid first was a bet on the spread taken at the moment
    #: the thesis had failed: on the 43 stops that rested 26 Sep – 3 Oct it lost ₹30,712 against
    #: selling at the bid at the trigger (won on 15, lost on 24; DMART −₹7,692 / −₹5,458 / −₹5,458 in
    #: one 15 s rest). False = only the urgent stops below sell at once; a calm stop rests and walks.
    exit_stops_at_bid: bool = True
    #: a stop that is urgent sells into the bid at once instead of walking from the mid (``urgent_stop``)
    exit_urgent_stops: bool = True
    #: ... urgent when the option's mid fell this many % or more over ``exit_fast_window_s``
    exit_fast_fall_pct: float = 2.0
    exit_fast_window_s: float = 30.0
    #: ... or when the book is this many ticks wide or less: resting at the mid buys at most a tick
    exit_tight_ticks: int = 2

    def exit_deadline(self, reason: ExitReason) -> float:
        if reason in URGENT_REASONS:
            return self.exit_cross_urgent_s
        if reason in STOP_REASONS:
            return self.exit_cross_stop_s
        return self.exit_cross_other_s

    def to_json(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "entryWaitS": self.entry_wait_s, "entryRecheckS": self.entry_recheck_s,
            "entryChasePct": self.entry_chase_pct, "entryHoldS": self.entry_hold_s, "entryCapPct": self.entry_cap_pct,
            "entryRaceCheckS": self.entry_race_check_s, "entryRacePct": self.entry_race_pct,
            "entryCrossAfterHold": self.entry_cross_after_hold,
            "exitRepriceS": self.exit_reprice_s, "exitCrossStopS": self.exit_cross_stop_s,
            "exitCrossUrgentS": self.exit_cross_urgent_s, "exitCrossOtherS": self.exit_cross_other_s,
            "restTargets": self.rest_targets,
            "exitStopsAtBid": self.exit_stops_at_bid,
            "exitUrgentStops": self.exit_urgent_stops, "exitFastFallPct": self.exit_fast_fall_pct,
            "exitFastWindowS": self.exit_fast_window_s, "exitTightTicks": self.exit_tight_ticks,
        }


def _tick(px: float, tick: float) -> float:
    return round(round(px / tick) * tick, 4) if tick > 0 else round(px, 2)


def entry_cap(ref: float | None, cap_pct: float | None, tick: float = 0.05) -> float | None:
    """The highest a BUY may pay: the signal price + ``cap_pct`` %, on the tick at or below it."""
    if ref is None or ref <= 0 or cap_pct is None:
        return None
    t = tick if tick > 0 else 0.05
    return round(int(ref * (1 + cap_pct / 100) / t + 1e-9) * t, 4)


def entry_limit(ref: float | None, bid: float | None, ask: float | None, tick: float = 0.05,
                cap: float | None = None) -> tuple[float | None, str]:
    """Where a BUY rests: at ``ref`` if the book still straddles it, else at the mid — never above
    ``cap``."""
    if bid and ask and bid > 0 and ask >= bid:
        if ref is not None and bid <= ref <= ask:
            px, why = _tick(ref, tick), "at the signal price, inside the book"
        else:
            px, why = _tick((bid + ask) / 2, tick), "at the mid — the signal price has left the book"
    elif ref is not None and ref > 0:
        px, why = _tick(ref, tick), "at the signal price — no two-sided book to read"
    else:
        return None, "no price to rest at"
    if cap is not None and px > cap:
        return cap, f"{why}, capped at {cap:g} (signal price + the cap)"
    return px, why


def option_run_pct(ref: float | None, bid: float | None, ask: float | None, ltp: float | None) -> float | None:
    """The option's move since the signal: its mid (else its last price) against the signal price, %."""
    if ref is None or ref <= 0:
        return None
    px = (bid + ask) / 2 if (bid and ask and bid > 0 and ask >= bid) else (ltp if ltp and ltp > 0 else None)
    return None if px is None else (px / ref - 1) * 100


def exit_limit(bid: float | None, ask: float | None, elapsed: float, deadline: float, tick: float = 0.05) -> float | None:
    """Where a SELL rests after ``elapsed`` seconds: the mid at first, walked in a straight line to
    the bid by the deadline."""
    if not bid or bid <= 0:
        return None
    if not ask or ask < bid:
        return _tick(bid, tick)
    mid = (bid + ask) / 2
    frac = min(1.0, max(0.0, elapsed / deadline)) if deadline > 0 else 1.0
    return _tick(mid - (mid - bid) * frac, tick)


def urgent_stop(reason: ExitReason, bid: float | None, ask: float | None, run_pct: float | None, policy: LimitPolicy,
                tick: float = 0.05) -> str | None:
    """Why a stop sells into the bid at once rather than resting at the mid, or None (rest and walk).
    ``run_pct`` is the option's mid now against ``exit_fast_window_s`` ago, % (None = not known)."""
    if reason not in STOP_REASONS:
        return None
    if policy.exit_stops_at_bid:
        return "a stop sells at its trigger"
    if not policy.exit_urgent_stops:
        return None
    if reason is ExitReason.SL_EQ:
        return "the stock has broken its stop"
    if run_pct is not None and run_pct <= -policy.exit_fast_fall_pct:
        return f"the option is falling fast ({run_pct:+.1f}% in {policy.exit_fast_window_s:g} s)"
    if bid and ask and bid > 0 and ask >= bid and ask - bid <= policy.exit_tight_ticks * tick + 1e-9:
        return f"a tight book ({bid:g} / {ask:g})"
    return None


def race_call(run_pct: float | None, ask: float | None, cap: float | None, policy: LimitPolicy) -> tuple[bool, str]:
    """The one look at ``entry_race_check_s``: ``(True, why)`` take the ask now, else ``(False, why)``
    keep resting. Racing needs the run AND an ask within the cap."""
    if run_pct is None:
        return False, "no option price to read"
    at = f"option {run_pct:+.1f}% at {policy.entry_race_check_s:g} s"
    if policy.entry_race_pct is None or run_pct < policy.entry_race_pct:
        return False, f"{at} — not racing, resting to {policy.entry_wait_s:g} s"
    if not ask or ask <= 0:
        return False, f"{at} — racing, but no ask to take"
    if cap is not None and ask > cap:
        return False, f"{at} — racing, but the ask {ask:g} is over the cap {cap:g}"
    return True, f"racing: {at}, ask {ask:g} within the cap"


def touch_fills(limit: float, bid: float | None, ltp: float | None) -> bool:
    """A target sell resting in advance fills on a TOUCH: the bid at or over it, or a print at or over
    it. Optimistic — no queue: a real order at the level stands behind everyone already there."""
    return bool((bid and bid >= limit) or (ltp and ltp >= limit))


def fills(buy: bool, limit: float, bid: float | None, ask: float | None, ltp: float | None) -> bool:
    """Would a resting limit have been hit? The touch crossing it, or a trade printing through it."""
    if buy:
        return bool((ask and 0 < ask <= limit) or (ltp and 0 < ltp < limit))
    return bool((bid and bid >= limit) or (ltp and ltp > limit))


@dataclass(slots=True)
class Resting:
    """One working limit order and its audit trail."""

    intent: OrderIntent
    kind: str  # "entry" | "exit" | "target" (a target sell resting in advance)
    limit: float
    placed_ts: float
    deadline_s: float
    signal_ts: float
    ref: float | None = None
    why: str = ""
    book_at_place: tuple[float | None, float | None] = (None, None)
    reprices: list[tuple[float, float]] = field(default_factory=list)
    last_check: float = 0.0
    #: what the engine needs when it resolves: the entry's plan, or the exit's position and decision
    ctx: Any = None
    #: the gateway's order record, resolved (filled / cancelled) when the order is
    order: Any = None
    #: the last traded price the order has already seen — only a print after it can fill the order
    ltp_seen: float | None = None
    #: the momentum rule's early look has been taken (once, at ``entry_race_check_s``)
    race_checked: bool = False
    #: the momentum rule's reads — ``{atS, runPct, note}`` — for the order's trail
    momentum: list[dict[str, Any]] = field(default_factory=list)
    #: the five levels a side, price and quantity, when the order was placed (None = no depth book)
    depth_at_place: dict[str, Any] | None = None
    #: a stop's trigger record (``Engine._stop_record``): level, breaching read, bid and walk price then
    stop: dict[str, Any] | None = None

    def audit(self, **more: Any) -> dict[str, Any]:
        bid, ask = self.book_at_place
        return {
            "kind": self.kind, "signalTs": self.signal_ts, "placedTs": self.placed_ts, "limit": self.limit, "ref": self.ref,
            "why": self.why, "bookAtPlace": {"bid": bid, "ask": ask}, "deadlineS": self.deadline_s,
            "reprices": [[round(t, 3), p] for t, p in self.reprices],
            **({"momentum": list(self.momentum)} if self.momentum else {}),
            **({"depthAtPlace": self.depth_at_place} if self.depth_at_place else {}),
            **({"stop": self.stop} if self.stop else {}), **more,
        }
