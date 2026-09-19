"""Core records shared by risk / exec / ledger / api.

Strategies never import this — they emit ``strategy.base.Signal`` on the *underlying*, and everything
from instrument selection onwards speaks these types.

Two identity rules are encoded here because breaking either cost the old stack real money:

* **P18 — never pass a symbol where a scrip code is expected.** :class:`Instrument` carries both and
  is the only thing handed around; nothing takes a bare string. PIVOTBOSS never fired for its whole
  life because a universe function returned symbols into a lookup keyed by numeric codes, and CAN2's
  OI gate read ``NO_DATA`` for 39 of 40 scrips for the same reason.
* **The trade is on an option; the levels are on the underlying.** A FUDKII position holds a CE/PE
  but its stop and targets are derived from pivots on the equity. Both ladders are stored
  (``equity_sl`` / ``option_sl``, ``equity_targets`` / ``option_targets``) so an exit can never be
  attributed to the wrong instrument — an ``SL-OP`` tag on a cash trade was a real mislabel.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from .config import Segment


class OptionType(StrEnum):
    CE = "CE"
    PE = "PE"
    FUT = "XX"  # 5paisa's ScripType for a future leg
    NONE = ""


class InstrumentKind(StrEnum):
    EQUITY = "EQUITY"
    FUTURE = "FUTURE"
    OPTION = "OPTION"
    INDEX = "INDEX"


@dataclass(frozen=True, slots=True)
class Instrument:
    """One tradeable or quotable thing, as the broker identifies it.

    ``scrip_code`` is the numeric 5paisa token and is the ONLY key used for orders, positions and
    feed subscriptions. ``symbol`` is the human root (``RELIANCE``) and is the only key used for
    joining an option or future back to its underlying — the old OI gate learned the hard way that
    stored future scrip codes expire every month while the symbol does not.
    """

    scrip_code: str
    symbol: str
    segment: Segment
    kind: InstrumentKind
    name: str = ""
    lot_size: int = 1
    tick_size: float = 0.05
    #: rupees per price point per unit of quantity. MCX ALUMINI is quoted per kg on a 1,000 kg
    #: contract, so notional = price x qty x 1000. Sizing that assumes 1 was wrong by that factor.
    multiplier: int = 1
    expiry: str = ""  # YYYY-MM-DD, "" for cash
    strike: float = 0.0
    option_type: OptionType = OptionType.NONE
    underlying: str = ""  # symbol of the underlying; == symbol for cash

    @property
    def exch(self) -> str:
        return self.segment.exch

    @property
    def exch_type(self) -> str:
        return self.segment.exch_type

    @property
    def is_option(self) -> bool:
        return self.option_type in (OptionType.CE, OptionType.PE)

    @property
    def qty_step(self) -> int:
        """Order quantity must be a multiple of this. Cash trades in shares; everything else in lots."""
        return 1 if self.kind is InstrumentKind.EQUITY else max(1, self.lot_size)

    def notional(self, price: float, qty: int) -> float:
        return price * qty * self.multiplier

    def round_price(self, px: float) -> float:
        if self.tick_size <= 0:
            return px
        return round(round(px / self.tick_size) * self.tick_size, 4)


class PosSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Purpose(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class ExitReason(StrEnum):
    SL_OP = "SL-OP"  # the option's own stop was hit
    SL_EQ = "SL-EQ"  # the underlying breached the equity stop
    TARGET = "TARGET"  # a T1..T4 rung was taken
    TRAIL = "TRAIL"  # the ratchet gave back its allowance
    TIME_STOP = "TIME_STOP"
    EOD = "EOD"  # segment force-flat
    HALT = "HALT"
    DAILY_LOSS = "DAILY_LOSS"
    MANUAL = "MANUAL"
    END = "END"  # a backtest range ended with the position open


class Direction(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.BULLISH else -1

    @property
    def option_type(self) -> OptionType:
        return OptionType.CE if self is Direction.BULLISH else OptionType.PE


@dataclass(slots=True)
class Level:
    price: float
    qty: int


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def to_json(obj: Any) -> dict[str, Any]:
    return asdict(obj)


@dataclass(slots=True)
class OrderIntent:
    strategy: str
    instrument: Instrument
    side: OrderSide
    qty: int
    purpose: Purpose
    signal_id: str
    #: idempotency key. ``{signal_id}|{purpose}|{position_id}`` — the shape that finally stopped the
    #: duplicate-close bug in the old executor (564 closes, 0 duplicates after it landed).
    client_order_id: str
    reason: str = ""
    position_id: str | None = None
    ref_price: float | None = None  # price the decision was taken at
    limit_price: float | None = None
    ts: float = field(default_factory=time.time)

    @property
    def is_entry(self) -> bool:
        return self.purpose is Purpose.ENTRY


@dataclass(slots=True)
class Fill:
    price: float
    qty: int
    ts: float
    charges: float = 0.0
    slippage_bps: float | None = None
    book_age_ms: int | None = None
    levels: int = 0


@dataclass(slots=True)
class Order:
    id: str
    client_order_id: str
    strategy: str
    scrip_code: str
    symbol: str
    side: OrderSide
    purpose: Purpose
    qty: int
    mode: str
    status: str  # FILLED | SHADOW | REJECTED | SUBMITTED
    signal_id: str
    position_id: str | None
    reason: str
    ts: float
    avg_price: float | None = None
    filled: int = 0
    charges: float = 0.0
    slippage_bps: float | None = None
    broker_order_id: str = ""
    note: str = ""


@dataclass(slots=True)
class Position:
    id: str
    strategy: str
    instrument: Instrument  # what is actually held (usually a CE/PE)
    underlying: Instrument  # what the levels are computed on
    side: PosSide
    qty: int
    entry: float  # option premium paid
    opened_ts: float
    signal_id: str
    direction: Direction
    #: levels on the underlying, from the confluence engine
    equity_entry: float = 0.0
    equity_sl: float = 0.0
    equity_targets: tuple[float, ...] = ()
    #: the same ladder mapped onto the option
    option_sl: float = 0.0
    option_targets: tuple[float, ...] = ()
    initial_option_sl: float = 0.0
    #: |entry - initial option stop|; the denominator of every R figure
    r_unit: float = 0.0
    peak_r: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    charges: float = 0.0
    targets_hit: int = 0
    qty_remaining: int = 0
    status: str = "OPEN"
    bars_held: int = 0
    closed_ts: float | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl: float | None = None
    grade: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.qty_remaining == 0:
            self.qty_remaining = self.qty
        if self.initial_option_sl == 0.0:
            self.initial_option_sl = self.option_sl
        if self.r_unit == 0.0:
            self.r_unit = abs(self.entry - self.initial_option_sl)

    @property
    def dir_sign(self) -> int:
        """A bought option is always long premium, whichever way the underlying signal pointed."""
        return 1 if self.side is PosSide.LONG else -1

    def unrealized(self, ltp: float) -> float:
        return (
            (ltp - self.entry)
            * self.dir_sign
            * self.qty_remaining
            * self.instrument.multiplier
        )

    def r_now(self, ltp: float) -> float:
        return (ltp - self.entry) * self.dir_sign / self.r_unit if self.r_unit > 0 else 0.0


@dataclass(slots=True)
class Trade:
    id: str
    position_id: str
    strategy: str
    scrip_code: str
    symbol: str
    underlying: str
    instrument_kind: str
    side: PosSide
    qty: int
    entry: float
    exit: float
    gross: float
    charges: float
    net: float
    r_multiple: float
    mfe_r: float
    mae_r: float
    exit_reason: str
    opened_ts: float
    closed_ts: float
    duration_s: float
    signal_id: str
    grade: str = ""
    equity_entry: float = 0.0
    equity_sl: float = 0.0
    equity_targets: tuple[float, ...] = ()
    r_unit: float = 0.0
    multiplier: int = 1
    evidence: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class ExitDecision:
    position_id: str
    reason: ExitReason
    ref_price: float
    qty: int  # partial exits carry less than qty_remaining
    note: str = ""
