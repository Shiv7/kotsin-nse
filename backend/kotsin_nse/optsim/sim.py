"""The structure simulator: one ``Book`` = a structure, an entry calendar, the exits, a management rule and the costs;
one ``state`` = the open position with its daily path and the closed cycles. The same ``step`` advances a backtest
over the bhavcopy history and a paper book forward one session at a time, so the forward record is exactly the test
rerun on new days.

The calendar (``Entry``): monthly or weekly series, a days-to-expiry window, an optional weekday, the first days of the
month, never on an expiry day, never inside ``no_entry_dte`` days of expiry. The exits (``Exit``): at expiry (the
bhavcopy's settlement closes), at a days-to-expiry (``exit_dte`` — "two days before expiry"), a loss budget in points
or as a multiple of the credit (the Batman's "stop = the centre"), a profit target as a fraction of the credit. The
management (``manage``): ``none``; ``convert_to_fly`` (an iron condor whose short strike the future closed beyond:
the untested side is closed and re-sold at the tested short's premium with a hedge at the same width — the
condor-to-fly rule); ``balance_premium`` (a strangle whose untested short has decayed under ``balance_ratio`` of the
tested short: roll it in to the strike priced like the tested short — the weekly playbook's rule). Costs: slippage as
a percentage of each leg's premium each way, brokerage per order, STT on sold premium, exchange and GST — the
practitioners' 0.5–0.8 % slippage is the honest assumption on settlement prices.

Everything is in index points per one unit of the structure; rupees use ``Costs.lot``.
"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field
from typing import Any

from .chain import Day, Store, dte
from .structures import BUILDERS, Leg


@dataclass(slots=True)
class Costs:
    slippage_pct: float = 0.5  # of each leg's premium, each way
    brokerage_inr: float = 20.0  # per order
    stt_pct_sell: float = 0.15  # of premium turnover on the sell side
    exchange_pct: float = 0.03503
    gst_pct: float = 18.0
    lot: int = 65

    def leg_inr(self, qty_lots: int, px: float, selling: bool) -> float:
        t = abs(qty_lots) * self.lot * px
        stt = t * self.stt_pct_sell / 100 if selling else 0.0
        exch = t * self.exchange_pct / 100
        return self.brokerage_inr + stt + exch + (self.brokerage_inr + exch) * self.gst_pct / 100

    def slip_pts(self, legs: list[Leg], prices: dict[int, float]) -> float:
        """Slippage over a round trip, in points per unit: each leg's entry and exit premium × the percentage."""
        return sum(abs(x.qty) * (x.entry + prices.get(i, x.entry)) * self.slippage_pct / 100 for i, x in enumerate(legs))

    def round_trip_inr(self, legs: list[Leg], prices: dict[int, float]) -> float:
        total = 0.0
        for i, x in enumerate(legs):
            sold_first = x.qty < 0
            total += self.leg_inr(x.qty, x.entry, selling=sold_first)
            total += self.leg_inr(x.qty, prices.get(i, x.entry), selling=not sold_first)
        return total


@dataclass(slots=True)
class Entry:
    series: str = "monthly"  # monthly | weekly
    min_dte: int = 30
    max_dte: int = 45
    weekday: int | None = None  # 0 = Monday … 4 = Friday; None = any
    first_days_of_month: int | None = None  # enter only on calendar days 1..N
    no_entry_dte: int = 2  # never open a series this close to its expiry

    def expiry_for(self, d: Day) -> str | None:
        date = datetime.date.fromisoformat(d.day)
        if self.weekday is not None and date.weekday() != self.weekday:
            return None
        if self.first_days_of_month is not None and date.day > self.first_days_of_month:
            return None
        series = d.monthlies if self.series == "monthly" else d.weeklies
        for e in series:
            n = dte(e, d.day)
            if n <= self.no_entry_dte or n < self.min_dte:
                continue
            if n <= self.max_dte:
                return e
        return None


@dataclass(slots=True)
class Exit:
    exit_dte: int = 0  # close at the first session with this many days or fewer to expiry (0 = the expiry's settlement)
    loss_budget_pts: float | None = None
    loss_budget_credit_mult: float | None = None  # budget = mult × the credit
    target_frac: float | None = None  # take profit at this fraction of the credit

    def budget(self, credit: float) -> float | None:
        if self.loss_budget_pts is not None:
            return self.loss_budget_pts
        if self.loss_budget_credit_mult is not None and credit > 0:
            return self.loss_budget_credit_mult * credit
        return None


@dataclass(slots=True)
class Book:
    name: str
    structure: str = "batman"
    params: dict[str, Any] = field(default_factory=dict)
    entry: Entry = field(default_factory=Entry)
    exit: Exit = field(default_factory=Exit)
    manage: str = "none"  # none | convert_to_fly | balance_premium
    balance_ratio: float = 0.7
    max_rolls: int = 3
    costs: Costs = field(default_factory=Costs)
    symbol: str = "NIFTY"
    note: str = ""
    max_open: int = 1  # positions open at once: 2 lets a monthly structure overlap the next month's, as the playbooks deploy

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "structure": self.structure, "params": self.params, "entry": asdict(self.entry), "exit": asdict(self.exit),
                "manage": self.manage, "balance_ratio": self.balance_ratio, "max_rolls": self.max_rolls, "costs": asdict(self.costs), "symbol": self.symbol,
                "note": self.note, "max_open": self.max_open}


def new_state(book: Book) -> dict[str, Any]:
    return {"book": book.name, "opens": [], "cycles": [], "last_day": None, "skipped": 0}


# -- pricing --------------------------------------------------------------------------------------------------


def leg_price(d: Day, leg: Leg) -> float | None:
    """The leg's close today; on its expiry day, its intrinsic value off the future when the file has no close."""
    px = d.close(leg.expiry, leg.strike, leg.kind)
    if px is not None:
        return px
    if dte(leg.expiry, d.day) <= 0:
        f = d.future(leg.expiry)
        if f is None:
            return None
        return max(f - leg.strike, 0.0) if leg.kind == "CE" else max(leg.strike - f, 0.0)
    return None


def prices_now(d: Day, legs: list[Leg]) -> dict[int, float] | None:
    out: dict[int, float] = {}
    for i, leg in enumerate(legs):
        px = leg_price(d, leg)
        if px is None:
            return None
        out[i] = px
    return out


def pnl_pts(legs: list[Leg], prices: dict[int, float]) -> float:
    return sum(x.qty * (prices[i] - x.entry) for i, x in enumerate(legs))


def credit_pts(legs: list[Leg]) -> float:
    return -sum(x.qty * x.entry for x in legs)


# -- management -----------------------------------------------------------------------------------------------


def _tested_side(d: Day, legs: list[Leg], expiry: str) -> str | None:
    """The side whose short strike the future has closed beyond, if any."""
    f = d.future(expiry)
    if f is None:
        return None
    for x in legs:
        if x.qty < 0 and x.role == "sell":
            if x.kind == "CE" and f >= x.strike:
                return "CE"
            if x.kind == "PE" and f <= x.strike:
                return "PE"
    return None


def _realise(legs: list[Leg], idx: list[int], prices: dict[int, float]) -> tuple[list[Leg], float]:
    """Close the legs at ``idx`` at today's prices: returns the remaining legs and the realised points."""
    realised = sum(legs[i].qty * (prices[i] - legs[i].entry) for i in idx)
    keep = [x for i, x in enumerate(legs) if i not in idx]
    return keep, realised


def convert_to_fly(d: Day, open_: dict[str, Any]) -> list[str]:
    """Iron condor → iron fly once the future closes beyond a short strike: the untested short and its hedge are
    closed, a new short on the untested side is sold at the premium of the tested short, its hedge bought at the
    tested side's width. Once per cycle."""
    if open_.get("converted"):
        return []
    legs = [Leg.from_json(x) for x in open_["legs"]]
    expiry = open_["expiry"]
    tested = _tested_side(d, legs, expiry)
    if tested is None:
        return []
    untested = "PE" if tested == "CE" else "CE"
    prices = prices_now(d, legs)
    if prices is None:
        return []
    t_short = next((x for x in legs if x.kind == tested and x.qty < 0), None)
    t_hedge = next((x for x in legs if x.kind == tested and x.qty > 0), None)
    if t_short is None or t_hedge is None:
        return []
    width = abs(t_hedge.strike - t_short.strike)
    t_px = prices[legs.index(t_short)]
    idx = [i for i, x in enumerate(legs) if x.kind == untested]
    keep, realised = _realise(legs, idx, prices)
    k_new = d.strike_by_premium(expiry, untested, t_px, otm=True)  # never in the money: the nearest OTM strike priced like the tested short
    if k_new is None:
        return []
    ks = d.strikes(expiry)
    k_h = min(ks, key=lambda k: abs(k - (k_new + width if untested == "CE" else k_new - width)))
    s = d.close(expiry, k_new, untested)
    h = d.close(expiry, k_h, untested)
    if s is None or h is None:
        return []
    keep += [Leg(expiry, k_new, untested, -1, s, "sell"), Leg(expiry, k_h, untested, 1, h, "hedge")]
    open_["legs"] = [x.to_json() for x in keep]
    open_["realised"] = round(open_.get("realised", 0.0) + realised, 2)
    open_["converted"] = True
    return [f"{d.day} convert_to_fly: {tested} tested, {untested} re-sold at {k_new:.0f} ({s:.1f}), hedge {k_h:.0f}; realised {realised:+.1f}"]


def balance_premium(d: Day, open_: dict[str, Any], ratio: float, max_rolls: int) -> list[str]:
    """Strangle premium balancing: when the untested short has decayed under ``ratio`` of the tested short's premium,
    roll it in to the strike priced like the tested short (closer to the money)."""
    if open_.get("rolls", 0) >= max_rolls:
        return []
    legs = [Leg.from_json(x) for x in open_["legs"]]
    expiry = open_["expiry"]
    shorts = [i for i, x in enumerate(legs) if x.qty < 0]
    if len(shorts) != 2:
        return []
    prices = prices_now(d, legs)
    if prices is None:
        return []
    a, b = shorts
    hi, lo = (a, b) if prices[a] >= prices[b] else (b, a)
    if prices[lo] >= ratio * prices[hi] or prices[hi] <= 0:
        return []
    kind = legs[lo].kind
    k_new = d.strike_by_premium(expiry, kind, prices[hi])
    if k_new is None or k_new == legs[lo].strike:
        return []
    px = d.close(expiry, k_new, kind)
    if px is None:
        return []
    keep, realised = _realise(legs, [lo], prices)
    keep.append(Leg(expiry, k_new, kind, -1, px, "sell"))
    open_["legs"] = [x.to_json() for x in keep]
    open_["realised"] = round(open_.get("realised", 0.0) + realised, 2)
    open_["rolls"] = open_.get("rolls", 0) + 1
    return [f"{d.day} balance: {kind} {legs[lo].strike:.0f} ({prices[lo]:.1f}) → {k_new:.0f} ({px:.1f}); realised {realised:+.1f}"]


# -- the step -------------------------------------------------------------------------------------------------


def _close(state: dict[str, Any], book: Book, d: Day, prices: dict[int, float], how: str, open_: dict[str, Any]) -> dict[str, Any]:
    legs = [Leg.from_json(x) for x in open_["legs"]]
    gross = pnl_pts(legs, prices) + open_.get("realised", 0.0)
    slip = book.costs.slip_pts(legs, prices) + open_.get("slip_extra", 0.0)
    charges = book.costs.round_trip_inr(legs, prices) + open_.get("charges_extra", 0.0)
    net_inr = (gross - slip) * book.costs.lot - charges
    f = d.future(open_["expiry"])
    row = {"book": book.name, "expiry": open_["expiry"], "entry": open_["entered"], "dte": open_["dte"], "F0": open_["F0"], "legs": open_["legs0"],
           "credit": open_["credit"], "exit": d.day, "how": how, "pnl_pts": round(gross, 1), "slip_pts": round(slip, 1), "charges_inr": round(charges),
           "net_inr": round(net_inr), "mtm_min": open_["mtm_min"], "mtm_max": open_["mtm_max"], "days": len(open_["path"]), "events": open_["events"],
           "F_exit": round(f, 1) if f else None, "dt_flags": open_.get("dt_flags", [])}
    state["cycles"].append(row)
    return row


def step(state: dict[str, Any], store: Store, day: str, book: Book) -> list[str]:
    """Advance the book by one bhavcopy session: manage, mark and maybe close each open position, then open a new
    one if the calendar says so and fewer than ``book.max_open`` are running. Returns the events of the day."""
    d = store.get(day)
    events: list[str] = []
    if "open" in state:  # a state written before positions could overlap
        state["opens"] = [state["open"]] if state.get("open") else []
        state.pop("open")
    still: list[dict[str, Any]] = []
    for open_ in state.get("opens", []):
        ev: list[str] = []
        if book.manage == "convert_to_fly":
            ev += convert_to_fly(d, open_)
        elif book.manage == "balance_premium":
            ev += balance_premium(d, open_, book.balance_ratio, book.max_rolls)
        open_["events"] += ev
        events += ev
        legs = [Leg.from_json(x) for x in open_["legs"]]
        prices = prices_now(d, legs)
        if prices is None:
            events.append(f"{day} no prices for a leg of {open_['expiry']}; held")
            still.append(open_)
            continue
        pnl = pnl_pts(legs, prices) + open_.get("realised", 0.0)
        open_["path"].append([day, round(pnl, 1)])
        open_["mtm_min"] = round(min(open_["mtm_min"], pnl), 1)
        open_["mtm_max"] = round(max(open_["mtm_max"], pnl), 1)
        n = dte(open_["expiry"], day)
        if n <= 1:
            open_.setdefault("dt_flags", []).append(f"DT{n}")
        budget = book.exit.budget(open_["credit"])
        how = None
        if budget is not None and pnl <= -budget:
            how = "stop"
        elif book.exit.target_frac is not None and open_["credit"] > 0 and pnl >= book.exit.target_frac * open_["credit"]:
            how = "target"
        elif n <= book.exit.exit_dte:
            how = "expiry" if n <= 0 else f"dte{n}"
        if how:
            row = _close(state, book, d, prices, how, open_)
            events.append(f"{day} close {how} {open_['expiry']}: {row['pnl_pts']:+.1f} pts, net ₹{row['net_inr']:+,}")
        else:
            still.append(open_)
    state["opens"] = still
    if len(state["opens"]) < book.max_open:
        expiry = book.entry.expiry_for(d)
        taken = {o["expiry"] for o in state["opens"]} | {c["expiry"] for c in state["cycles"][-4:]}
        if expiry is not None and expiry not in taken:
            legs = BUILDERS[book.structure](d, expiry, **book.params)
            if legs is None:
                state["skipped"] += 1
                events.append(f"{day} could not build {book.structure} on {expiry}")
            else:
                credit = credit_pts(legs)
                f = d.future(expiry)
                open_ = {"expiry": expiry, "entered": day, "dte": dte(expiry, day), "F0": round(f, 1) if f else None,
                         "legs": [x.to_json() for x in legs], "legs0": " ".join(f"{x.qty:+d}{x.kind}{x.strike:.0f}@{x.entry:.1f}" for x in legs),
                         "credit": round(credit, 1), "path": [], "mtm_min": 0.0, "mtm_max": 0.0, "events": [], "realised": 0.0}
                state["opens"].append(open_)
                events.append(f"{day} open {book.structure} {expiry} ({dte(expiry, day)} d): credit {credit:+.1f} pts; {open_['legs0']}")
    state["last_day"] = day
    return events


def backtest(store: Store, book: Book, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    state = new_state(book)
    for day in store.days:
        if (start and day < start) or (end and day > end):
            continue
        step(state, store, day, book)
    return state


def summary(state: dict[str, Any], lot: int = 65) -> dict[str, Any]:
    c = state["cycles"]
    if not c:
        return {"cycles": 0}
    pts = [x["pnl_pts"] for x in c]
    net = [x["net_inr"] for x in c]
    return {"cycles": len(c), "wins": sum(1 for p in pts if p > 0), "sum_pts": round(sum(pts), 1), "mean_pts": round(sum(pts) / len(pts), 1),
            "worst_pts": round(min(pts), 1), "best_pts": round(max(pts), 1), "sum_net_inr": round(sum(net)), "mean_net_inr": round(sum(net) / len(net)),
            "worst_net_inr": round(min(net)), "stops": sum(1 for x in c if x["how"] == "stop"), "targets": sum(1 for x in c if x["how"] == "target"),
            "mean_credit": round(sum(x["credit"] for x in c) / len(c), 1), "worst_mtm": round(min(x["mtm_min"] for x in c), 1),
            "events": sum(len(x["events"]) for x in c)}


__all__ = ["Book", "Costs", "Entry", "Exit", "backtest", "credit_pts", "new_state", "pnl_pts", "prices_now", "step", "summary"]
