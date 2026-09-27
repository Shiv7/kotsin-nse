"""Operator, 2026-09-27: "whenever the system detects that the entire premium is at risk, we need to
kick-in a rule to protect it" — the option stop priced properly, and a cap on the premium one lot can
lose (both switchable per book, off by default)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kotsin_nse.instrument.pricing import value_at, years_left
from kotsin_nse.risk.exits import ExitEngine
from kotsin_nse.risk.limits import RT_X_LIMITS

from .test_exits_rt import _own, _view

IST = timezone(timedelta(hours=5, minutes=30))
MON_0945 = datetime(2026, 9, 21, 9, 45, tzinfo=IST).timestamp()


def test_the_option_is_priced_at_the_stock_stop_not_drawn_to_zero():
    """SUNPHARMA 1900 CE, 21 Sep 09:45: the straight line read 9.10 − 28.9 × 0.42 = −3.04 (nothing left);
    priced with its own implied volatility and 8 days to go it is worth 2.55 at the stop."""
    v = value_at(option_price=9.10, spot=1880.4, target_spot=1851.5, strike=1900, expiry="2026-09-29", now=MON_0945, call=True)
    assert v == pytest.approx(2.55, abs=0.01)
    assert years_left("2026-09-29", MON_0945) == pytest.approx((8 * 86_400 + 5.75 * 3600) / (365 * 86_400))
    assert value_at(option_price=9.10, spot=1880.4, target_spot=1851.5, strike=1900, expiry="2026-09-20", now=MON_0945, call=True) is None


def test_the_cap_holds_the_stop_through_every_reprojection():
    e = ExitEngine(replace(RT_X_LIMITS, max_premium_loss_pct=40.0))
    pos = _own(equity_sl=950.0, option_sl=5.0)  # 50 points × δ≈0.30 from a 20.00 premium: the line says 5.00
    e.evaluate(pos, _view(option_ltp=19.0, option_mid=19.0, underlying_ltp=995.0, now=MON_0945))
    assert pos.option_sl == 12.0, "never more than 40 % below the 20.00 paid"
    plain = _own(equity_sl=950.0, option_sl=5.0)
    ExitEngine(RT_X_LIMITS).evaluate(plain, _view(option_ltp=19.0, option_mid=19.0, underlying_ltp=995.0, now=MON_0945))
    assert plain.option_sl < 12.0, "without the cap the straight line stands"


def test_the_priced_stop_replaces_the_straight_line_in_the_reprojection():
    e = ExitEngine(replace(RT_X_LIMITS, priced_option_stop=True))
    pos = _own(equity_sl=950.0, option_sl=5.0)
    e.evaluate(pos, _view(option_ltp=20.0, option_mid=20.0, underlying_ltp=1000.0, now=MON_0945))
    want = value_at(option_price=20.0, spot=1000.0, target_spot=950.0, strike=pos.instrument.strike, expiry="2026-09-29", now=MON_0945, call=True)
    assert want is not None and pos.option_sl == pytest.approx(round(want, 2), abs=0.01)


def test_a_capped_stop_never_goes_below_a_rung_the_trade_has_claimed():
    e = ExitEngine(replace(RT_X_LIMITS, max_premium_loss_pct=40.0))
    pos = _own(equity_sl=950.0, option_sl=5.0)
    pos.ratchet_sl = 20.0  # breakeven after T1
    e.evaluate(pos, _view(option_ltp=25.0, option_mid=25.0, underlying_ltp=1010.0, now=MON_0945))
    assert pos.option_sl == 20.0


@pytest.mark.asyncio
async def test_the_stop_is_protected_at_the_fill_for_a_book_that_never_reprojects(settings):
    """FUDKII's own stop is set once, at the fill: the cap and the pricing must apply there."""
    from kotsin_nse.engine import Engine
    from kotsin_nse.risk.limits import RiskLimits

    e = Engine(settings)
    pos = _own(strategy="FUDKII", equity_sl=950.0, option_sl=0.05)
    e._protect_option_stop(pos, RiskLimits(max_lots=4, max_premium_loss_pct=40.0), "FUDKII", MON_0945)
    assert pos.option_sl == pos.initial_option_sl == 12.0 and pos.r_unit == 8.0
    off = _own(strategy="FUDKII", equity_sl=950.0, option_sl=0.05)
    e._protect_option_stop(off, RiskLimits(max_lots=4), "FUDKII", MON_0945)
    assert off.option_sl == 0.05, "off by default: nothing changes"


def test_only_rt_n_prices_and_caps_its_stop():
    """Operator, 2026-09-27: "fudkii-rt-n use only priced+35% logic and fudkii-rt-x and fudkii-rt-y,
    fudkii-ct-x and fudkii-ct-y be as is"."""
    from kotsin_nse.config import Settings
    from kotsin_nse.engine import Engine
    from kotsin_nse.risk.limits import CT_X_LIMITS, CT_Y_LIMITS, RT_N_LIMITS, RT_Y_LIMITS

    assert RT_N_LIMITS.priced_option_stop and RT_N_LIMITS.max_premium_loss_pct == 35.0
    for lim in (RT_X_LIMITS, RT_Y_LIMITS, CT_X_LIMITS, CT_Y_LIMITS):
        assert not lim.priced_option_stop and lim.max_premium_loss_pct is None
    e = Engine(Settings(_env_file=None))
    assert not e.limits.priced_option_stop and e.limits.max_premium_loss_pct is None, "the parent as it is"
    assert e.limits_for("FUDKII_RT_MCX").max_premium_loss_pct is None and e.limits_for("FUDKII_RT_Y_W1").max_premium_loss_pct is None
