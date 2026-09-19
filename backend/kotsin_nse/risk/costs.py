"""What a trade actually costs. One model, used by the paper filler, the live accounting and the
backtester — so a strategy can never look profitable in research and unprofitable live because two
cost models disagreed.

This is the most important file in the risk package, for a measured reason. On the NSE cash book at
₹33,000 a position the **round trip cost 0.299%**, of which **81% was flat brokerage** (₹40/order
× 2). Break-even needed roughly ₹1.32 lakh of position. The best exit rule anyone found returned
+0.118%/trade gross — i.e. every stop, trail and target variant tested was strictly worse than
"flat at the close", and the book still lost money because the *entries* had to clear a fixed cost
that does not scale down. Any strategy evaluated without this model is being evaluated on a number
that cannot be realised.

Rates are configuration, not constants: they change by circular, differ by segment, and 5paisa's
brokerage is a ``min(flat, percent)`` slab. Every default is stated in ``config.Settings`` with its
source so a wrong number is visible rather than buried.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..domain import Instrument, InstrumentKind, OrderSide


@dataclass(frozen=True, slots=True)
class Charges:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst

    def __add__(self, other: Charges) -> Charges:
        return Charges(
            brokerage=self.brokerage + other.brokerage,
            stt=self.stt + other.stt,
            exchange=self.exchange + other.exchange,
            sebi=self.sebi + other.sebi,
            stamp=self.stamp + other.stamp,
            gst=self.gst + other.gst,
        )

    def to_json(self) -> dict[str, float]:
        return {
            "brokerage": round(self.brokerage, 2),
            "stt": round(self.stt, 2),
            "exchange": round(self.exchange, 2),
            "sebi": round(self.sebi, 2),
            "stamp": round(self.stamp, 2),
            "gst": round(self.gst, 2),
            "total": round(self.total, 2),
        }


class CostModel:
    def __init__(self, s: Settings) -> None:
        self.s = s

    def turnover(self, instrument: Instrument, price: float, qty: int) -> float:
        """For an option this is **premium turnover**, which is what every option charge is levied
        on — not the notional of the underlying exposure."""
        return price * qty * instrument.multiplier

    def leg(
        self, instrument: Instrument, side: OrderSide, price: float, qty: int
    ) -> Charges:
        s = self.s
        t = self.turnover(instrument, price, qty)
        if t <= 0 or qty <= 0:
            return Charges()

        brokerage = s.cost_brokerage_per_order_inr
        if s.cost_brokerage_pct > 0:
            brokerage = min(brokerage, t * s.cost_brokerage_pct / 100)

        kind = instrument.kind
        if kind is InstrumentKind.OPTION:
            exch_pct = s.cost_exchange_txn_pct_option
            stt_pct = s.cost_stt_pct_sell_option_premium
        elif kind is InstrumentKind.FUTURE:
            exch_pct = s.cost_exchange_txn_pct_future
            stt_pct = s.cost_stt_pct_sell_future
        else:
            exch_pct = s.cost_exchange_txn_pct_equity
            stt_pct = s.cost_stt_pct_sell_equity

        stt = t * stt_pct / 100 if side is OrderSide.SELL else 0.0
        exchange = t * exch_pct / 100
        sebi = t * s.cost_sebi_pct / 100
        stamp = t * s.cost_stamp_pct_buy / 100 if side is OrderSide.BUY else 0.0
        gst = (brokerage + exchange + sebi) * s.cost_gst_pct / 100
        return Charges(
            brokerage=brokerage, stt=stt, exchange=exchange, sebi=sebi, stamp=stamp, gst=gst
        )

    def round_trip(self, instrument: Instrument, entry: float, exit_price: float, qty: int) -> Charges:
        return self.leg(instrument, OrderSide.BUY, entry, qty) + self.leg(
            instrument, OrderSide.SELL, exit_price, qty
        )

    def round_trip_pct(self, instrument: Instrument, price: float, qty: int) -> float:
        """Round-trip cost as a percentage of turnover, assuming a flat exit.

        The number to look at before believing any edge. At ₹33,000 on NSE cash it is ~0.30%; at
        ₹1.3 lakh it is ~0.10%. That difference is the whole argument for larger, fewer positions.
        """
        t = self.turnover(instrument, price, qty)
        if t <= 0:
            return 0.0
        return self.round_trip(instrument, price, price, qty).total / t * 100

    def breakeven_move_pct(self, instrument: Instrument, price: float, qty: int) -> float:
        """How far the instrument must move, in percent, just to pay for itself."""
        return self.round_trip_pct(instrument, price, qty)

    def slippage(self, price: float, qty: int, instrument: Instrument, *, bps: float | None = None) -> float:
        b = self.s.slippage_bps_default if bps is None else bps
        return self.turnover(instrument, price, qty) * b / 1e4
