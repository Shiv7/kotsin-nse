"""The option-buying replay: fills at the recorded ask and bid after the latency, real charges, the
rewards adding up to the net P&L, no look-ahead, stale quotes untradable, the forced exit."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from kotsin_nse.config import Settings
from kotsin_nse.market.session import ist_at
from kotsin_nse.risk.costs import CostModel
from kotsin_nse.sim.day import Option, Quotes, RootDay
from kotsin_nse.sim.env import BUY_CALL, EXIT, HOLD, OptionBuyEnv
from kotsin_nse.sim.policies import Flat, Oracle, Random
from kotsin_nse.sim.run import episode

DAY = date(2026, 10, 23)
T0 = float(ist_at(DAY, "09:15"))


def _day(drift: float = 0.01, gap_after: float | None = None) -> RootDay:
    ts = np.arange(T0, T0 + 3 * 3600, 1.0)
    mid = 1180 + drift * (ts - T0)
    keep = ts < (gap_after or np.inf)
    cash = Quotes(ts, mid, mid - 0.05, mid + 0.05, np.cumsum(np.full(len(ts), 100.0)))
    ce_mid = np.maximum(mid - 1180, 0) + 10
    pe_mid = np.maximum(1180 - mid, 0) + 10
    opt = lambda m, code, kind: Option(code, 1180.0, kind, "2026-10-27", 500,  # noqa: E731
                                       Quotes(ts[keep], m[keep], m[keep] - 0.1, m[keep] + 0.1, np.zeros(int(keep.sum()))))
    bars = pd.DataFrame({"ts": ts[::60], "o": mid[::60], "h": mid[::60] + 0.2, "l": mid[::60] - 0.2, "c": mid[::60], "v": 1000.0})
    return RootDay("X", DAY, cash, None, [opt(ce_mid, "C", "CE"), opt(pe_mid, "P", "PE")], pd.DataFrame(), pd.DataFrame(), bars)


def _env(rd: RootDay, **kw) -> OptionBuyEnv:
    return OptionBuyEnv(rd, CostModel(Settings(_env_file=None)), start="09:30", end="10:30", **kw)


def test_a_call_bought_at_the_ask_and_sold_at_the_bid_nets_exactly_and_the_rewards_add_up():
    env = _env(_day())
    _obs, info = env.reset()
    assert info["can"][BUY_CALL]
    rewards = [env.step(BUY_CALL)[1]]
    for _ in range(59):
        rewards.append(env.step(HOLD)[1])
    rewards.append(env.step(EXIT)[1])
    (tr,) = env.trades
    entry_t, exit_t = float(ist_at(DAY, "09:30")) + 1, float(ist_at(DAY, "09:30")) + 60 * 5 + 1
    mid = lambda t: 1180 + 0.01 * (t - T0)  # noqa: E731
    assert tr.entry == pytest.approx(mid(entry_t) - 1180 + 10 + 0.1) and tr.exit == pytest.approx(mid(exit_t) - 1180 + 10 - 0.1)
    assert tr.net == pytest.approx((tr.exit - tr.entry) * 500 - tr.charges)
    assert sum(rewards) == pytest.approx(tr.net)
    assert tr.charges > 80, "₹40 an order each way at least"


def test_the_observation_never_reads_past_the_decision():
    """Two copies of the same day, the second poisoned after 10:00 with absurd values BEFORE anything
    is read (so no cache can hide it): the observations at 10:00 must be identical."""
    cut = float(ist_at(DAY, "10:00"))
    clean, poisoned = _day(), _day()
    for q in [poisoned.cash] + [o.q for o in poisoned.options]:
        late = q.ts > cut
        q.ltp[late], q.bid[late], q.ask[late] = 1e9, 1e9, 2e9
    poisoned.bars.loc[poisoned.bars["ts"] + 60 > cut, ["h", "l", "c", "v"]] = 1e9
    a, b = _env(clean), _env(poisoned)
    a.reset(start="10:00")
    b.reset(start="10:00")
    assert np.allclose(a.observe(), b.observe())
    assert not np.allclose(a.observe(), _env_at(_day(drift=0.02), "10:00").observe()), "the test can tell days apart"


def _env_at(rd: RootDay, at: str) -> OptionBuyEnv:
    e = _env(rd)
    e.reset(start=at)
    return e


def test_no_fresh_quote_no_entry_and_the_last_bid_marks_a_forced_exit():
    rd = _day(gap_after=float(ist_at(DAY, "09:40")))
    env = _env(rd)
    env.reset(start="09:39")
    env.step(BUY_CALL)
    for _ in range(40):
        env.step(HOLD)
    assert env.can()[BUY_CALL] is False, "the option's quotes stopped at 09:40"
    env2 = _env(_day(), max_hold_s=120)
    env2.reset()
    env2.step(BUY_CALL)
    for _ in range(30):
        env2.step(HOLD)
    assert env2.pos is None and env2.trades[0].why == "max hold"


def test_baselines_flat_is_zero_random_pays_the_spread_and_the_oracle_rides_the_drift():
    assert episode(_env(_day()), Flat())["net"] == 0
    r = episode(_env(_day(drift=0.0)), Random(p_enter=0.05, hold_s=120, seed=1))
    assert r["trades"] > 0 and r["net"] < 0 and r["reward_sum"] == pytest.approx(r["net"], abs=0.01)
    o = episode(_env(_day(drift=0.01)), Oracle(edge_bps=5, hold_s=300))
    assert o["trades"] > 0 and o["net"] > 0
