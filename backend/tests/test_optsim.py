"""The option simulator (kotsin_nse.optsim) on a synthetic, flat-volatility bhavcopy: strikes by delta, the Batman held
to expiry and stopped, the condor converted to a fly, the weekly strangle balanced, the risk layer and the regime row."""

from __future__ import annotations

import datetime

import pandas as pd

from kotsin_nse.fno.chain import black76
from kotsin_nse.optsim import regime, risk
from kotsin_nse.optsim.chain import Store, dte
from kotsin_nse.optsim.sim import Book, Costs, Entry, Exit, new_state, step, summary
from kotsin_nse.optsim.structures import batman, batman_winged, iron_condor, strangle

MONTHLY = "2026-02-24"
WEEKLIES = ["2026-01-15", "2026-01-22", "2026-01-29", "2026-02-05", "2026-02-12", "2026-02-19"]


def bdays(start: str, end: str) -> list[str]:
    d, e = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    out = []
    while d <= e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def make_day(day: str, f: float, iv: float = 0.15) -> pd.DataFrame:
    rows = [{"symbol": "NIFTY", "kind": "FUTIDX", "expiry": MONTHLY, "strike": 0.0, "option_type": "", "close": f, "oi": 1e6, "volume": 1e4}]
    for e in [MONTHLY, *WEEKLIES]:
        n = dte(e, day)
        if n < 0:
            continue
        t = max(n, 0) / 365 + 1e-6
        for k in range(20000, 28001, 100):
            for kind in ("CE", "PE"):
                px = black76(f, k, t, iv, kind == "CE")
                rows.append({"symbol": "NIFTY", "kind": "OPTIDX", "expiry": e, "strike": float(k), "option_type": kind, "close": round(max(px, 0.05), 2), "oi": 1e5, "volume": 1e3})
    return pd.DataFrame(rows)


class FrameStore(Store):
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.symbol = "NIFTY"
        self._frames = frames
        self._cache = {}
        self.keep = 1000
        self.root = None  # type: ignore[assignment]

    @property
    def days(self) -> list[str]:
        return sorted(self._frames)

    def get(self, day: str):
        if day not in self._cache:
            self._cache[day] = Store.from_frame(day, "NIFTY", self._frames[day])
        return self._cache[day]


def path_store(path: dict[str, tuple[float, float]], start: str, end: str, f0: float = 24000.0) -> FrameStore:
    """A store over the business days ``start`` → ``end``: the future flat at ``f0`` unless ``path`` gives (F, iv) for a day
    and after (the last given level carries forward)."""
    frames = {}
    f, iv = f0, 0.15
    for day in bdays(start, end):
        if day in path:
            f, iv = path[day]
        frames[day] = make_day(day, f, iv)
    return FrameStore(frames)


def run(store: FrameStore, book: Book) -> dict:
    st = new_state(book)
    events = []
    for day in store.days:
        events += step(st, store, day, book)
    st["events_all"] = events
    return st


def test_strikes_by_delta_and_the_batman_legs():
    d = path_store({}, "2026-01-13", "2026-01-13").get("2026-01-13")
    assert d.monthlies == [MONTHLY] and WEEKLIES[0] in d.weeklies and d.future(MONTHLY) == 24000.0 and d.atm(MONTHLY) == 24000.0
    kp, kc = d.strike_by_delta(MONTHLY, "PE", 0.16), d.strike_by_delta(MONTHLY, "CE", 0.16)
    assert kp < 24000 < kc and abs(abs(d.delta(MONTHLY, kp, "PE")) - 0.16) < 0.03 and abs(d.delta(MONTHLY, kc, "CE") - 0.16) < 0.03
    assert abs(d.iv(MONTHLY, kp, "PE") - 0.15) < 0.002  # the flat vol is recovered from the closes
    legs = batman(d, MONTHLY)
    assert legs is not None and [x.qty for x in legs] == [-2, 1, -2, 1] and [x.kind for x in legs] == ["PE", "PE", "CE", "CE"]
    assert legs[1].strike > legs[0].strike and legs[3].strike < legs[2].strike  # the bought legs sit nearer the money
    assert -sum(x.qty * x.entry for x in legs) > 0
    w = batman_winged(d, MONTHLY)
    assert w is not None and len(w) == 6 and all(x.role == "wing" and x.qty == 1 for x in w[4:]) and w[4].strike < legs[0].strike and w[5].strike > legs[2].strike
    c = iron_condor(d, MONTHLY)
    assert c is not None and [x.qty for x in c] == [-1, 1, -1, 1] and c[1].strike < c[0].strike < 24000 < c[2].strike < c[3].strike
    s = strangle(d, MONTHLY, delta=0.10)
    assert s is not None and s[0].strike < 24000 < s[1].strike


def test_the_batman_held_to_expiry_collects_the_credit_on_a_flat_path():
    store = path_store({}, "2026-01-13", MONTHLY)
    book = Book("hold", "batman", {}, Entry("monthly", 30, 45), Exit(0), "none", costs=Costs(slippage_pct=0.5, brokerage_inr=20.0, lot=65))
    st = run(store, book)
    assert st["opens"] == [] and len(st["cycles"]) == 1
    c = st["cycles"][0]
    assert c["entry"] == "2026-01-13" and c["expiry"] == MONTHLY and c["how"] == "expiry" and c["dte"] == 42
    assert abs(c["pnl_pts"] - c["credit"]) < 0.5  # every leg expired worthless: the credit is the profit
    assert c["slip_pts"] > 0 and c["charges_inr"] > 0 and c["net_inr"] < c["credit"] * 65 and c["mtm_min"] <= 0 <= c["mtm_max"]
    assert c["days"] == len(store.days) - 1 and "DT0" in c["dt_flags"] and "DT1" in c["dt_flags"]
    s = summary(st)
    assert s["cycles"] == 1 and s["wins"] == 1 and s["stops"] == 0
    assert any("open batman" in e for e in st["events_all"]) and st["events_all"][-1].startswith(f"{MONTHLY} close expiry")


def test_the_stop_fires_on_a_crash_and_the_held_book_eats_it():
    crash = {"2026-01-16": (21800.0, 0.30)}
    store = path_store(crash, "2026-01-13", MONTHLY)
    stop = run(store, Book("stop", "batman", {}, Entry("monthly", 30, 45), Exit(0, loss_budget_credit_mult=1.0), "none"))
    hold = run(store, Book("hold", "batman", {}, Entry("monthly", 30, 45), Exit(0), "none"))
    cs, ch = stop["cycles"][0], hold["cycles"][0]
    assert cs["how"] == "stop" and cs["exit"] == "2026-01-16" and cs["pnl_pts"] <= -cs["credit"]
    assert ch["how"] == "expiry" and ch["pnl_pts"] < 0  # held through: the sold puts are in the money at expiry; the crash-day mark (vega) was worse than the settlement
    assert ch["F_exit"] == 21800.0 and ch["mtm_min"] <= cs["pnl_pts"]


def test_the_condor_converts_to_a_fly_when_the_future_closes_beyond_the_short_call():
    store = path_store({"2026-02-09": (25300.0, 0.15)}, "2026-02-03", MONTHLY)
    book = Book("fly", "iron_condor", {"short_delta": 0.25, "hedge_delta": 0.17}, Entry("monthly", 15, 40, first_days_of_month=5), Exit(0), "convert_to_fly")
    st = run(store, book)
    c = st["cycles"][0]
    assert c["entry"] == "2026-02-03" and any("convert_to_fly" in e for e in c["events"]) and len(c["events"]) == 1
    legs0 = c["legs"].split()
    assert legs0[0].startswith("-1PE") and legs0[2].startswith("-1CE")
    # no second conversion, the structure ran to expiry
    assert c["how"] == "expiry"
    plain = run(store, Book("plain", "iron_condor", {"short_delta": 0.25, "hedge_delta": 0.17}, Entry("monthly", 15, 40, first_days_of_month=5), Exit(0), "none"))
    assert plain["cycles"][0]["events"] == []


def test_the_weekly_strangle_is_balanced_and_leaves_two_days_before_expiry():
    store = path_store({"2026-01-14": (24700.0, 0.15)}, "2026-01-13", "2026-01-23")
    book = Book("wk", "strangle", {"delta": 0.10}, Entry("weekly", 5, 12, weekday=1), Exit(2), "balance_premium", 0.7, 3)
    st = run(store, book)
    c = st["cycles"][0]
    assert c["entry"] == "2026-01-13" and c["expiry"] == "2026-01-22" and c["dte"] == 9  # this week's Thursday is inside the no-entry window
    assert any("balance: PE" in e for e in c["events"])  # the put decayed under 70 % of the call after the 700-point rise
    assert c["how"] == "dte2" and c["exit"] == "2026-01-20"
    assert len(st["opens"]) == 1 and st["opens"][0]["expiry"] == "2026-01-29" and st["opens"][0]["entered"] == "2026-01-20"  # the next Tuesday opened the next week
    # with a loss budget of twice the credit the same path is stopped on the jump day, the balance event kept on the cycle
    stopped = run(store, Book("wk2", "strangle", {"delta": 0.10}, Entry("weekly", 5, 12, weekday=1), Exit(2, loss_budget_credit_mult=2.0), "balance_premium", 0.7, 3))
    c2 = stopped["cycles"][0]
    assert c2["how"] == "stop" and c2["exit"] == "2026-01-14" and any("balance" in e for e in c2["events"]) and c2["pnl_pts"] <= -2 * c2["credit"]


def test_the_risk_layer_and_the_regime_row():
    mc = risk.monte_carlo_drawdown([100.0] * 10 + [-500.0], n_paths=2000, seed=1)
    assert mc["worst_dd"] <= -500 and mc["p99_dd"] <= mc["p90_dd"] <= mc["median_dd"] <= 0
    assert risk.recorded_drawdown([100, -300, 50, -200]) == -450
    assert risk.budget_check(-3000, 4000)["used_pct"] == 75.0 and not risk.budget_check(-3000, 4000)["halted"] and risk.budget_check(-4000, 4000)["halted"]
    a = risk.allocation(2_000_000, [{"name": "b", "structure": "iron_condor", "share": 0.15}])[0]
    assert a["allocation_inr"] == 300_000 and a["lots"] == 3 and a["monthly_budget_inr"] == 12_000
    rep = risk.report(2_000_000, [{"name": "b", "structure": "iron_condor", "share": 0.15}], {"b": [{"net_inr": 2000}, {"net_inr": -5000}, {"net_inr": 3000}]})
    assert rep["books"][0]["cycles"] == 3 and rep["books"][0]["p99_dd_inr_at_lots"] <= -5000 * 3 and rep["sum_p99_dd_pct"] < 0
    store = path_store({}, "2026-01-13", "2026-01-13")
    r = regime.row(store, "2026-01-13")
    assert r["monthly"] == MONTHLY and abs(r["iv_m"] - 15.0) < 0.2 and r["weekly"] == "2026-01-15" and r["dte_w"] == 2 and abs(r["skew25"]) < 0.2 and r["atm_m"] == 24000.0
