"""Option buying on a replayed day: every ``step_s`` seconds the agent sees the market as it stood
and either stays as it is, buys the at-the-money call, buys the at-the-money put, or exits.

Fills are the recorded quotes ``latency_s`` after the decision: a buy pays the ASK, a sale gets the
BID — the spread is paid in full, as a market order would. Charges per leg are the engine's cost
model (``risk/costs.py``: ₹40 an order, STT on the sell premium, exchange, SEBI, stamp, GST). An
option is tradable only if its quote is fresher than ``max_quote_age_s`` and has a real bid and ask.
A position is closed by force after ``max_hold_s`` or at ``end``.

Reward = the change in the position's value marked at the BID, less charges, so the rewards of an
episode add up to its net P&L in rupees: entering costs (ask − bid) × qty + the buy charges at once.

Observation (all from data at or before the decision, closed minutes only for flow) — ``FEATURES``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..config import Segment
from ..domain import Instrument, InstrumentKind, OrderSide
from ..market.session import ist_at
from ..risk.costs import CostModel
from .day import Option, RootDay

HOLD, BUY_CALL, BUY_PUT, EXIT = 0, 1, 2, 3
ACTIONS = ("hold", "buy_call", "buy_put", "exit")

FEATURES = (
    "ret_1m_bps", "ret_5m_bps", "ret_15m_bps", "vwap_dist_bps", "range_pos", "tod",
    "delta_share_5m", "ofi_5m_per_depth", "queue_imbalance", "micro_bps", "fut_oi_chg_15m_pct",
    "spread_call_pct", "spread_put_pct", "pos_side", "pos_pnl_pct", "pos_age_min",
)


@dataclass(slots=True)
class Position:
    opt: Option
    qty: int
    entry: float
    entry_ts: float
    mark: float
    charges: float


@dataclass(slots=True)
class Trade:
    symbol: str
    kind: str
    strike: float
    qty: int
    entry_ts: float
    entry: float
    exit_ts: float
    exit: float
    gross: float
    charges: float
    net: float
    why: str
    spread_paid: float


@dataclass(slots=True)
class OptionBuyEnv:
    day: RootDay
    costs: CostModel
    step_s: float = 5.0
    latency_s: float = 1.0
    max_hold_s: float = 900.0
    max_quote_age_s: float = 15.0
    start: str = "09:20"
    end: str = "15:10"
    lots: int = 1
    t: float = 0.0
    t_end: float = 0.0
    pos: Position | None = None
    trades: list[Trade] = field(default_factory=list)
    _inst: dict[str, Instrument] = field(default_factory=dict)

    # -- market reads ------------------------------------------------------------------------------

    def _quote(self, o: Option, t: float) -> tuple[float, float] | None:
        i = o.q.at(t)
        if i < 0 or t - o.q.ts[i] > self.max_quote_age_s:
            return None
        b, a = float(o.q.bid[i]), float(o.q.ask[i])
        return (b, a) if b > 0 and a > b else None

    def _atm(self, kind: str, t: float) -> Option | None:
        mid = self.day.mid(t)
        if mid is None:
            return None
        live = [o for o in self.day.options if o.kind == kind and self._quote(o, t) is not None]
        if not live:
            return None
        near = min(o.expiry for o in live)
        return min((o for o in live if o.expiry == near), key=lambda o: abs(o.strike - mid))

    def _leg(self, o: Option, side: OrderSide, price: float, qty: int) -> float:
        inst = self._inst.setdefault(o.code, Instrument(o.code, self.day.symbol, Segment.NSE_FO, InstrumentKind.OPTION, lot_size=o.lot))
        return self.costs.leg(inst, side, price, qty).total

    # -- observation --------------------------------------------------------------------------------

    def observe(self) -> np.ndarray:
        t, d = self.t, self.day
        mid = d.mid(t) or 0.0

        def ret(mins: int) -> float:
            p0 = d.mid(t - mins * 60)
            return (mid / p0 - 1) * 1e4 if p0 and mid else 0.0

        a = d.arrays()
        vwap = rp = 0.0
        if "bar_end" in a:
            k = int(np.searchsorted(a["bar_end"], t, side="right")) - 1  # the last bar closed by t
            if k >= 0 and mid:
                if a["cum_v"][k] > 0:
                    vwap = (mid / (a["cum_pv"][k] / a["cum_v"][k]) - 1) * 1e4
                hi, lo = a["run_hi"][k], a["run_lo"][k]
                rp = (mid - lo) / (hi - lo) if hi > lo else 0.5

        def window(key: str, n: int) -> slice:
            if f"{key}_end" not in a:
                return slice(0, 0)
            k = int(np.searchsorted(a[f"{key}_end"], t, side="right"))  # minutes closed by t
            return slice(max(0, k - n), k)

        w = window("f", 5)
        g = lambda c: a[f"f_{c}"][w] if f"f_{c}" in a else np.zeros(0)  # noqa: E731
        buy, sell = float(np.nansum(g("buy"))), float(np.nansum(g("sell")))
        bd, ad = g("bid_depth"), g("ask_depth")
        depth = (np.nanmean(bd) + np.nanmean(ad)) / 2 if len(bd) and np.isfinite(bd).any() and np.isfinite(ad).any() else 0.0
        qi_last = g("qi")[-1] if len(g("qi")) else np.nan
        mb_last = g("micro_bps")[-1] if len(g("micro_bps")) else np.nan
        wf = window("ff", 16)
        oi = a["ff_oi"][wf] if "ff_oi" in a else np.zeros(0)
        oi = oi[np.isfinite(oi)]
        sp = []
        for kind in ("CE", "PE"):
            o = self._atm(kind, t)
            q = self._quote(o, t) if o else None
            sp.append((q[1] - q[0]) / ((q[0] + q[1]) / 2) * 100 if q else 5.0)
        p = self.pos
        obs = [
            ret(1), ret(5), ret(15), vwap, rp, (t - ist_at(d.day, "09:15")) / (375 * 60),
            (buy - sell) / (buy + sell) if buy + sell else 0.0,
            float(np.nansum(g("ofi"))) / depth if depth and depth == depth else 0.0,
            float(qi_last) if qi_last == qi_last else 0.0,
            float(mb_last) if mb_last == mb_last else 0.0,
            (oi[-1] / oi[0] - 1) * 100 if len(oi) > 1 and oi[0] else 0.0,
            sp[0], sp[1],
            0.0 if p is None else (1.0 if p.opt.kind == "CE" else -1.0),
            0.0 if p is None else (p.mark / p.entry - 1) * 100,
            0.0 if p is None else (t - p.entry_ts) / 60,
        ]
        return np.nan_to_num(np.asarray(obs, dtype=float))

    def can(self) -> list[bool]:
        """Which actions are possible now (a mask for the agent)."""
        flat = self.pos is None
        return [True, flat and self._atm("CE", self.t) is not None, flat and self._atm("PE", self.t) is not None, not flat]

    # -- episode ------------------------------------------------------------------------------------

    def reset(self, start: str | None = None) -> tuple[np.ndarray, dict[str, Any]]:
        self.t = float(ist_at(self.day.day, start or self.start))
        self.t_end = float(ist_at(self.day.day, self.end))
        self.pos, self.trades = None, []
        return self.observe(), {"can": self.can()}

    def _close(self, t: float, why: str) -> float:
        p = self.pos
        assert p is not None
        q = self._quote(p.opt, t)
        bid = q[0] if q else p.mark  # no fresh quote: the last known bid (and the trade says so)
        ch = self._leg(p.opt, OrderSide.SELL, bid, p.qty)
        r = (bid - p.mark) * p.qty - ch
        gross = (bid - p.entry) * p.qty
        i = p.opt.q.at(p.entry_ts)
        self.trades.append(Trade(self.day.symbol, p.opt.kind, p.opt.strike, p.qty, p.entry_ts, p.entry, t, bid, gross,
                                 p.charges + ch, gross - p.charges - ch, why if q else why + " (stale quote)",
                                 (p.entry - float(p.opt.q.bid[i])) * p.qty if i >= 0 else 0.0))
        self.pos = None
        return r

    def step(self, action: int) -> tuple[np.ndarray, float, bool, dict[str, Any]]:
        fill_t = self.t + self.latency_s
        reward = 0.0
        if action in (BUY_CALL, BUY_PUT) and self.pos is None:
            o = self._atm("CE" if action == BUY_CALL else "PE", self.t)
            q = self._quote(o, fill_t) if o else None
            if o and q:
                qty = o.lot * self.lots
                ch = self._leg(o, OrderSide.BUY, q[1], qty)
                self.pos = Position(o, qty, q[1], fill_t, q[0], ch)
                reward -= (q[1] - q[0]) * qty + ch  # marked at the bid at once: the spread and the charges
        elif action == EXIT and self.pos is not None:
            reward += self._close(fill_t, "agent")
        self.t += self.step_s
        if self.pos is not None:
            if self.t - self.pos.entry_ts >= self.max_hold_s:
                reward += self._close(self.t, "max hold")
            else:
                q = self._quote(self.pos.opt, self.t)
                if q:
                    reward += (q[0] - self.pos.mark) * self.pos.qty
                    self.pos.mark = q[0]
        done = self.t >= self.t_end
        if done and self.pos is not None:
            reward += self._close(self.t, "end of session")
        return self.observe(), reward, done, {"can": self.can()}

    def summary(self) -> dict[str, Any]:
        tr = self.trades
        if not tr:
            return {"trades": 0, "net": 0.0}
        net = [x.net for x in tr]
        return {
            "trades": len(tr), "net": round(sum(net), 2), "gross": round(sum(x.gross for x in tr), 2),
            "charges": round(sum(x.charges for x in tr), 2), "spread_paid": round(sum(x.spread_paid for x in tr), 2),
            "win_rate": round(100 * sum(n > 0 for n in net) / len(net), 1), "avg_net": round(sum(net) / len(net), 2),
            "avg_hold_min": round(sum((x.exit_ts - x.entry_ts) for x in tr) / len(tr) / 60, 2),
            "best": round(max(net), 2), "worst": round(min(net), 2),
            "stale_exits": sum("stale" in x.why for x in tr),
        }


__all__ = ["ACTIONS", "BUY_CALL", "BUY_PUT", "EXIT", "FEATURES", "HOLD", "OptionBuyEnv", "Trade", "math"]
