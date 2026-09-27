"""What is this option worth if the underlying stands at its stop?

The option stop has been the equity stop drawn through a stand-in delta as a straight line
(``estimate_delta``). An OTM option's delta falls as the underlying falls, so the line overstates the
loss, and when distance × delta reaches the premium it says the option is worth nothing at the stop
(SUNPHARMA 1900 CE, 21 Sep: 9.10 premium, 28.9 points to the stop × δ 0.42 = 12.14). Priced with the
option's own implied volatility and the time left (market/iv.py's Black–Scholes), it is worth 2.55
there — a 72 % loss, not 100 %.
"""

from __future__ import annotations

from datetime import datetime

from ..market.iv import bs_price, implied_vol
from ..market.session import IST

_YEAR_S = 365.0 * 86_400


def years_left(expiry: str, now: float) -> float:
    """Time to the 15:30 IST close of the expiry day, in years — to the second (market/iv.py's
    ``years_to_expiry`` counts whole days, which in expiry week is most of the time value)."""
    try:
        d = datetime.fromisoformat(str(expiry)[:10])
    except ValueError:
        return 0.0
    close = datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST).timestamp()
    return max(0.0, (close - now) / _YEAR_S)


def value_at(*, option_price: float, spot: float, target_spot: float, strike: float, expiry: str, now: float, call: bool) -> float | None:
    """The option's value if the underlying stood at ``target_spot`` now: its own implied volatility
    (backed out of ``option_price`` at ``spot``) and the time left held. None when it cannot be priced."""
    years = years_left(expiry, now)
    vol = implied_vol(option_price, spot, strike, years, call=call)
    if vol is None:
        return None
    return bs_price(target_spot, strike, years, vol, call=call)
