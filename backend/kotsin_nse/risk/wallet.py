"""Per-strategy wallet: balance, day accounting, breakers.

One wallet per strategy key, exactly as the old stack had (``wallet:entity:strategy-wallet-FUDKII``),
but persisted through the ledger rather than Redis, and created from the :class:`StrategyKey` enum
so a wallet cannot outlive its producer. The old executor kept funding — and reporting — a wallet
for a strategy whose code had been deleted six weeks earlier.

The trading day is **IST**, not UTC: ``day_pnl`` must reset when the Indian session rolls, not at
05:30 IST. This is one of the two places outside ``market.session`` that needs a calendar day, and
it takes it from there rather than computing one.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..market.session import ist_day
from .limits import RiskLimits


@dataclass(slots=True)
class Wallet:
    strategy: str
    initial: float
    balance: float
    peak: float
    day_start_balance: float
    day: str
    realized_pnl: float = 0.0
    charges_paid: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    deployed: float = 0.0  # premium currently at risk in open positions
    halted: bool = False
    halt_reason: str = ""
    updated_ts: float = field(default_factory=time.time)

    @classmethod
    def new(cls, strategy: str, initial: float, now: float | None = None) -> Wallet:
        now = now or time.time()
        return cls(
            strategy=strategy,
            initial=initial,
            balance=initial,
            peak=initial,
            day_start_balance=initial,
            day=ist_day(now).isoformat(),
            updated_ts=now,
        )

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Wallet:
        return cls(**d)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    # -- derived ---------------------------------------------------------------------------------

    @property
    def available(self) -> float:
        return max(0.0, self.balance - self.deployed)

    @property
    def day_pnl(self) -> float:
        return self.balance - self.day_start_balance

    @property
    def day_pnl_pct(self) -> float:
        return self.day_pnl / self.day_start_balance * 100 if self.day_start_balance else 0.0

    @property
    def drawdown_pct(self) -> float:
        return (self.peak - self.balance) / self.peak * 100 if self.peak else 0.0

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.trades if self.trades else None

    # -- mutations -------------------------------------------------------------------------------

    def rollover(self, now: float) -> bool:
        d = ist_day(now).isoformat()
        if d == self.day:
            return False
        self.day = d
        self.day_start_balance = self.balance
        if self.halted and self.halt_reason.startswith("DAILY_LOSS"):
            self.halted, self.halt_reason = False, ""
        self.updated_ts = now
        return True

    def reserve(self, amount: float, now: float) -> bool:
        """Ask whether this much can be deployed, and deploy it if so. A question asked BEFORE
        an order. For money already spent, use ``commit``."""
        if amount > self.available:
            return False
        self.deployed += amount
        self.updated_ts = now
        return True

    def commit(self, amount: float, now: float) -> float:
        """Record what a fill actually cost, whatever was available. Returns the overdraw, 0.0
        when there was none.

        The sizer budgets against the *quoted* premium (``sizing.py``: ``budget = min(position
        budget, available)``) but the wallet is charged the *fill* price, and a fill lands at or
        above the quote — the paper matcher walks the ask, and a live fill slips. So on the last
        entries of a nearly-full wallet the cost exceeds ``available`` by the slippage, and the
        entry path was calling ``reserve`` and discarding its ``False``: the position opened, the
        money was never marked deployed, ``available`` stayed overstated, and the NEXT entry sized
        against money already spent. Harmless at three positions a book; not at thirty.
        """
        over = max(0.0, amount - self.available)
        self.deployed += amount
        self.updated_ts = now
        return over

    def release(self, amount: float, now: float) -> None:
        self.deployed = max(0.0, self.deployed - amount)
        self.updated_ts = now

    def apply_charges(self, amount: float, now: float) -> None:
        self.balance -= amount
        self.charges_paid += amount
        self.updated_ts = now

    def apply_close(self, net_pnl: float, now: float) -> None:
        self.balance += net_pnl
        self.realized_pnl += net_pnl
        self.trades += 1
        if net_pnl > 0:
            self.wins += 1
        elif net_pnl < 0:
            self.losses += 1
        self.peak = max(self.peak, self.balance)
        self.updated_ts = now

    def check_breakers(self, limits: RiskLimits, now: float) -> str | None:
        """Trip a halt on a breached limit. Returns the reason only when *newly* tripped, so the
        caller alerts once rather than on every tick."""
        if self.halted:
            return None
        if self.day_pnl_pct <= -limits.daily_loss_limit_pct:
            self.halted, self.halt_reason = True, f"DAILY_LOSS {self.day_pnl_pct:.2f}%"
        elif self.drawdown_pct >= limits.max_drawdown_pct:
            self.halted, self.halt_reason = True, f"DRAWDOWN {self.drawdown_pct:.2f}%"
        else:
            return None
        self.updated_ts = now
        return self.halt_reason
