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
    #: ``daily_halt or drawdown_halt`` — kept as fields because every reader (pages, gateway,
    #: ledger rows) reads these two; they are DERIVED by ``_sync_halt`` and never set directly
    halted: bool = False
    halt_reason: str = ""
    updated_ts: float = field(default_factory=time.time)
    #: The two breakers have different lifetimes, so each has its own slot (operator, 2026-09-26:
    #: "10% daily-loss halt for each separate wallet, which resets every morning. The 15% drawdown
    #: halt ... stays on day after day until someone resets that particular wallet"). One shared
    #: flag let the daily halt hide the drawdown one: ``check_breakers`` returned early on any
    #: halt and ``rollover`` cleared a DAILY_LOSS reason — so a book that crossed 15% while
    #: halted for the day woke up tradeable (audit probe: -10.5% then -7% more, halted=False
    #: next morning).
    daily_halt: str = ""
    drawdown_halt: str = ""

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

    #: the slots are rebuilt from ``halt_reason`` on load and never stored: the stored row keeps the
    #: exact shape the previous build reads (``cls(**d)``), so a rollback boots on a ledger this build
    #: wrote (audit, 2026-09-26: "unexpected keyword argument 'daily_halt'" at boot)
    _NOT_STORED = ("daily_halt", "drawdown_halt")

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Wallet:
        known = {f for f in cls.__dataclass_fields__ if f not in cls._NOT_STORED}
        w = cls(**{k: v for k, v in d.items() if k in known})
        if w.halted:
            # "DRAWDOWN 16.20% · DAILY_LOSS -10.40%", or one of the two, or an older row's lone reason
            for part in (p.strip() for p in (w.halt_reason or "").split("·")):
                if part.startswith("DRAWDOWN"):
                    w.drawdown_halt = part
                elif part:
                    w.daily_halt = part
            if not (w.daily_halt or w.drawdown_halt):
                w.daily_halt = "DAILY_LOSS"
        w._sync_halt()
        return w

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        for k in self._NOT_STORED:
            d.pop(k, None)
        return d

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

    def rollover(self, now: float) -> dict[str, Any] | None:
        """A new IST day for this wallet, decided by the day the WALLET last recorded — not by a
        clock the process started with — so a restart after midnight rolls it over as surely as
        a process that ran through midnight. Idempotent: called at boot and on every housekeeping
        tick. Today's opening balance is the last closing balance; only the daily breaker clears.
        Returns the day that closed ``{day, open, close, pnl, dailyHalt, drawdownHalt}``, or None."""
        d = ist_day(now).isoformat()
        if d == self.day:
            return None
        closed = {
            "day": self.day, "open": round(self.day_start_balance, 2), "close": round(self.balance, 2),
            "pnl": round(self.balance - self.day_start_balance, 2), "dailyHalt": self.daily_halt,
            "drawdownHalt": self.drawdown_halt,
        }
        self.day = d
        self.day_start_balance = self.balance
        self.daily_halt = ""
        self._sync_halt()
        self.updated_ts = now
        return closed

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
        """Trip either breaker on a breached limit — BOTH are evaluated every time, whatever is
        already halted, so the drawdown halt is recorded even on a day the daily one tripped
        first. Returns the reasons newly tripped by this call (to alert once), else None."""
        new = []
        if not self.daily_halt and self.day_pnl_pct <= -limits.daily_loss_limit_pct:
            self.daily_halt = f"DAILY_LOSS {self.day_pnl_pct:.2f}%"
            new.append(self.daily_halt)
        if not self.drawdown_halt and self.drawdown_pct >= limits.max_drawdown_pct:
            self.drawdown_halt = f"DRAWDOWN {self.drawdown_pct:.2f}%"
            new.append(self.drawdown_halt)
        if not new:
            return None
        self._sync_halt()
        self.updated_ts = now
        return " · ".join(new)

    def _sync_halt(self) -> None:
        """``halted``/``halt_reason`` from the two slots — the drawdown (the one that persists)
        named first."""
        reasons = [r for r in (self.drawdown_halt, self.daily_halt) if r]
        self.halted, self.halt_reason = bool(reasons), " · ".join(reasons)
