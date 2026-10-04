"""The equity stop mode (operator, 2026-10-04): the STOCK stop is the thesis trigger, the option only the
instrument sold. One strategic stop path in ``ExitEngine.evaluate`` / ``evaluate_stale``; the option-side
stop rules (hard floor, sustained option stop, plain touch, the stock stop as the option's confirmation) are
inert; the rising line, targets, trail and backstops are unchanged. The default is the option mode every
book trades with; the stop-rule mirrors run the equity mode (test_stop_mirrors.py)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from kotsin_nse.domain import Direction, ExitReason, Position, PosSide
from kotsin_nse.engine import _position_from_json, _position_json
from kotsin_nse.risk.exits import ExitEngine, MarketView, replay_gap
from kotsin_nse.risk.limits import RT_X_LIMITS, RT_Y_LIMITS, RiskLimits
from tests.test_limit_orders import OPT, UND

EQ = replace(RT_Y_LIMITS, stop_mode="equity")
BASE_EQ = replace(RiskLimits(max_lots=4), stop_mode="equity")


def _pos(*, entry=12.0, option_sl=9.0, eq_entry=187.0, eq_sl=185.0, direction=Direction.BULLISH, strategy="FUDKII_RT_Y", targets=()) -> Position:
    return Position(id="p1", strategy=strategy, instrument=OPT, underlying=UND, side=PosSide.LONG, qty=2000, entry=entry, opened_ts=1000.0,
                    signal_id="s1", direction=direction, equity_entry=eq_entry, equity_sl=eq_sl, option_sl=option_sl, option_targets=targets, qty_remaining=2000)


def _view(now: float, und: float | None, *, ltp=11.0, bid=10.9, ask=11.1, quote_ok=True) -> MarketView:
    mid = (bid + ask) / 2 if (bid and ask) else ltp
    return MarketView(option_ltp=ltp, underlying_ltp=und, now=now, bars_held=0, past_force_flat=False,
                      option_mid=mid if quote_ok else None, spread_pct=(ask - bid) / mid if (bid and ask and quote_ok) else None,
                      quote_ok=quote_ok, option_bid=bid if quote_ok else None)


def _run(eng, pos, prices, *, start=1001.0, **kw):
    """One read a second along ``prices`` (the stock); the first decision, with the second it came on."""
    for k, u in enumerate(prices):
        d = eng.evaluate(pos, _view(start + k, u, **kw))
        if d is not None:
            return d, k
    return None, len(prices)


def test_a_touch_and_reverse_stands_down_and_leaves_the_trade_alone():
    eng, pos = ExitEngine(EQ), _pos()
    # inside, a 0.02 % touch for 8 s, back inside: nothing sells, the clock and the integral are cleared
    d, _ = _run(eng, pos, [185.3, 185.1, 184.96, 184.96, 184.97, 184.96, 184.96, 184.97, 184.96, 184.96, 185.2, 185.4])
    assert d is None and pos.breach_since is None and pos.stop_area == 0.0


def test_a_decisive_breach_sells_on_the_read_that_saw_it():
    eng, pos = ExitEngine(EQ), _pos()
    d, k = _run(eng, pos, [186.0, 185.5, 184.80])  # 0.108 % through
    assert d is not None and d.reason is ExitReason.SL_EQ and k == 2
    assert d.level == 185.0 and d.trigger_price == 184.80 and d.trigger_on == "underlying" and "decisive" in d.note
    assert d.ref_price == 11.0, "the option's last trade is a reference, never the fill"


def test_a_fast_fall_sells_at_a_marginal_breach():
    eng, pos = ExitEngine(EQ), _pos()
    # 185.70 → 184.98 in 30 s is −0.39 %: through by only 0.01 % but falling fast
    d, k = _run(eng, pos, [185.70] * 30 + [184.98])
    assert d is not None and "fast" in d.note and k == 30


def test_a_marginal_breach_confirms_by_magnitude_times_time_and_at_60_s_at_the_latest():
    # 0.05 % through: the integral reaches 1.0 %·s after 20 reads, well before the 60 s clock
    eng, pos = ExitEngine(EQ), _pos()
    d, k = _run(eng, pos, [185.2] + [184.907] * 70)  # (185 − 184.907) / 184.907 = 0.0503 %
    assert d is not None and "persistent" in d.note and 18 <= k <= 22, f"sold on read {k}"
    # 0.01 % through: the integral would need 100 s; the 60 s clock sells first
    eng, pos = ExitEngine(EQ), _pos()
    d, k = _run(eng, pos, [185.2] + [184.982] * 70)
    assert d is not None and "still through after 60 s" in d.note and k == 61


def test_a_breach_that_recovers_then_breaches_again_starts_its_confirmation_afresh():
    eng, pos = ExitEngine(EQ), _pos()
    prices = [185.2] + [184.98] * 40 + [185.2] * 5 + [184.98] * 40
    d, k = _run(eng, pos, prices)
    assert d is None or k >= 1 + 40 + 5 + 60, "the second breach is not credited with the first one's 40 s"


def test_the_premium_cap_sells_whatever_the_stock_says_and_only_on_a_fresh_quote():
    eng, pos = ExitEngine(EQ), _pos(entry=12.0)
    d = eng.evaluate(pos, _view(1001.0, 186.5, ltp=9.1, bid=8.95, ask=9.2))  # bid 8.95 ≤ 9.00 (75 % of 12), stock inside
    assert d is not None and d.reason is ExitReason.SL_OP and "premium cap" in d.note and d.level == 9.0 and d.trigger_on == "option bid"
    eng, pos = ExitEngine(EQ), _pos(entry=12.0)
    assert eng.evaluate_stale(pos, _view(1001.0, 186.5, ltp=8.5, bid=None, ask=None, quote_ok=False)) is None, "a stale quote prices nothing"


def test_a_stale_option_quote_still_lets_the_stock_decide():
    eng, pos = ExitEngine(EQ), _pos()
    for k, u in enumerate([186.0, 184.7]):
        d = eng.evaluate_stale(pos, _view(1001.0 + k, u, quote_ok=False))
    assert d is not None and d.reason is ExitReason.SL_EQ and "decisive" in d.note


def test_the_option_side_stop_rules_are_inert_under_the_equity_mode():
    """The option mid far below the option stop, through the 9 % hard floor, for minutes: no exit while the
    stock stays inside. The same reads under the option mode sell at once (the hard floor)."""
    eng, pos = ExitEngine(EQ), _pos(option_sl=11.0)  # hard floor 10.01; the 25 % cap at 9.00 is not reached
    for k in range(120):
        assert eng.evaluate(pos, _view(1001.0 + k, 186.0, ltp=9.5, bid=9.4, ask=9.6)) is None
    opt, pos2 = ExitEngine(RT_Y_LIMITS), _pos(option_sl=11.0)
    d = opt.evaluate(pos2, _view(1001.0, 186.0, ltp=9.5, bid=9.4, ask=9.6))
    assert d is not None and d.reason is ExitReason.SL_OP and "hard floor" in d.note


def test_the_first_stock_print_after_entry_already_through_sells_at_once():
    eng, pos = ExitEngine(EQ), _pos()
    d = eng.evaluate(pos, _view(1001.0, 184.99))  # 0.005 % through, but the first print the position sees
    assert d is not None and "first stock print" in d.note


def test_the_base_books_trail_still_works_through_the_option_stop_level():
    """Under the equity mode the plain touch of ``option_sl`` is inert while the level is the thesis stop,
    and active once ``_trail`` has raised it (breakeven after T1): that is profit protection, not the stop."""
    eng, pos = ExitEngine(BASE_EQ), _pos(strategy="FUDKII", option_sl=9.5, targets=(13.0, 14.0))
    assert eng.evaluate(pos, _view(1001.0, 186.0, ltp=9.3, bid=9.2, ask=9.4)) is None, "the option under its (inert) stop, above the cap: nothing"
    pos = _pos(strategy="FUDKII", option_sl=9.5, targets=(13.0, 14.0))
    pos.targets_hit = 1
    eng.evaluate(pos, _view(1001.0, 188.0, ltp=12.6, bid=12.5, ask=12.7))  # T1 paid, +5 %: the trail lifts the level (breakeven, then peak − 40 % of the gain)
    assert pos.option_sl >= 12.0
    d = eng.evaluate(pos, _view(1002.0, 188.0, ltp=11.9, bid=11.8, ask=12.0))
    assert d is not None and d.reason is ExitReason.SL_OP and "≤ stop" in d.note


def test_the_option_mode_is_untouched_and_the_feed_gap_replay_is_the_option_modes():
    eng, pos = ExitEngine(RT_X_LIMITS), _pos()
    d = eng.evaluate(pos, _view(1001.0, 184.0))  # the stock through: the option mode's "equity confirmed, no grace"
    assert d is not None and d.reason is ExitReason.SL_EQ and "no grace" in d.note
    assert replay_gap(_pos(), [(1000.0, 8.0), (1060.0, 8.0)], EQ) is None


def test_the_confirmation_state_survives_a_restart():
    pos = _pos()
    pos.breach_since, pos.stop_area = 1234.0, 0.42
    back = _position_from_json(_position_json(pos))
    assert back.breach_since == 1234.0 and back.stop_area == pytest.approx(0.42)
