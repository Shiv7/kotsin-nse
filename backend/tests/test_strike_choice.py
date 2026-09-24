"""Which strike to buy, decided on its own terms (operator, 2026-09-24).

The chooser used to borrow the confluence target: the strike nearest T1. That coupled two
questions that are not the same one, and on a distant wall it put the strike where nothing trades
— KAYNES that morning anchored on 3800 against a 3523 spot and every candidate came back
one-sided, so the trigger was lost. The strike is now placed against the nearest RAW classic pivot
ahead, and chosen from the span between spot and that pivot by how much actually trades there.

The exit ladder is untouched: targets and stops still come from the confluence engine.
"""

from __future__ import annotations

import time

import pytest

from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Direction, Instrument, InstrumentKind, OptionType
from kotsin_nse.instrument.select import (
    Quote,
    rank_by_liquidity,
    select_option,
    strike_anchor_from_pivots,
)


def _opt(strike: float, ot: OptionType = OptionType.CE) -> Instrument:
    return Instrument(
        scrip_code=f"{int(strike)}{ot.value}", symbol="X", segment=Segment.NSE_FO,
        kind=InstrumentKind.OPTION, lot_size=100, tick_size=0.05, multiplier=1,
        expiry="2026-09-29", strike=strike, option_type=ot, underlying="X",
    )


# -- the anchor ----------------------------------------------------------------------------------


def test_the_anchor_is_the_nearest_raw_pivot_ahead_and_skips_one_sitting_on_spot():
    pivots = [95.0, 100.2, 104.0, 110.0, 120.0]
    # bullish: 100.2 is 0.2 above spot and no target at all -> the next one ahead
    assert strike_anchor_from_pivots(prices=pivots, spot=100.0, direction=Direction.BULLISH,
                                     min_distance=1.0) == 104.0
    # with no floor, the nearest ahead wins
    assert strike_anchor_from_pivots(prices=pivots, spot=100.0, direction=Direction.BULLISH,
                                     min_distance=0.0) == 100.2
    # "ahead" is strict: a level below spot is never a bullish anchor, however close
    assert strike_anchor_from_pivots(prices=[99.9], spot=100.0, direction=Direction.BULLISH,
                                     min_distance=0.0) is None
    # bearish looks the other way
    assert strike_anchor_from_pivots(prices=pivots, spot=100.0, direction=Direction.BEARISH,
                                     min_distance=1.0) == 95.0
    # nothing ahead at all
    assert strike_anchor_from_pivots(prices=[90.0], spot=100.0, direction=Direction.BULLISH,
                                     min_distance=1.0) is None
    # every level inside the floor: the furthest of them is still the best available
    assert strike_anchor_from_pivots(prices=[100.1, 100.4], spot=100.0,
                                     direction=Direction.BULLISH, min_distance=5.0) == 100.4


def test_the_anchor_is_not_the_confluence_target_and_does_not_touch_it():
    """KAYNES: spot 3523.10, confluence T1 3800 (5.5 strikes out, nothing trading there), but the
    nearest raw pivot ahead is far closer. The exit ladder still says 3800."""
    pivots = [3480.0, 3555.0, 3610.0, 3800.0]
    anchor = strike_anchor_from_pivots(prices=pivots, spot=3523.10,
                                       direction=Direction.BULLISH, min_distance=12.0)
    assert anchor == 3555.0, "the strike aims at the next level, not the exit target"
    assert anchor != 3800.0


# -- the ranking ---------------------------------------------------------------------------------


def test_the_strike_is_the_one_that_actually_trades_not_merely_the_nearest():
    a, b, c = _opt(3550), _opt(3600), _opt(3650)
    liq = {a.scrip_code: (100.0, 500.0), b.scrip_code: (9_000.0, 40_000.0), c.scrip_code: (50.0, 90.0)}
    assert rank_by_liquidity([a, b, c], liq, anchor=3555.0)[0] is b, "best on both wins"
    # Neither number decides alone. When they disagree exactly, rank-sum ties — "best on volume
    # and worst on OI" is not better than "second on both" — and the tie breaks toward the anchor.
    # That is the intended shape: a combined rank, not a winner-takes-all on either metric.
    split = {a.scrip_code: (9_999.0, 10.0), b.scrip_code: (900.0, 900.0), c.scrip_code: (10.0, 9_999.0)}
    order = rank_by_liquidity([a, b, c], split, anchor=3555.0)
    assert order[0] is a and order[-1] is c, "all tie on rank; nearest the anchor comes first"
    # no data ranks last on that metric rather than being dropped
    assert set(rank_by_liquidity([a, b, c], {}, anchor=3555.0)) == {a, b, c}
    # ties break toward the anchor
    flat = dict.fromkeys((a.scrip_code, b.scrip_code, c.scrip_code), (1.0, 1.0))
    assert rank_by_liquidity([a, b, c], flat, anchor=3650.0)[0] is c


def test_only_strikes_between_spot_and_the_anchor_compete():
    chain = [_opt(s) for s in (3550, 3600, 3650, 3700, 3900)]
    q = {i.scrip_code: Quote(ltp=20.0, bid=19.9, ask=20.1, ts=time.time()) for i in chain}
    # 3900 is past the anchor and must not be chosen however liquid it looks
    liq = {i.scrip_code: (100.0, 100.0) for i in chain}
    liq[chain[-1].scrip_code] = (10_000_000.0, 10_000_000.0)
    sel = select_option(chain=chain, quotes=q, spot=3523.1, target1=3800.0,
                        direction=Direction.BULLISH, now=time.time(),
                        strike_anchor=3650.0, liquidity=liq)
    assert sel.ok and sel.instrument.strike in (3550, 3600, 3650)
    assert sel.anchor == 3650.0


def test_without_an_anchor_the_old_behaviour_is_exactly_preserved():
    """No pivots, no liquidity: the selector must still choose nearest-to-target as it always did."""
    chain = [_opt(s) for s in (3550, 3600, 3650, 3700)]
    q = {i.scrip_code: Quote(ltp=20.0, bid=19.9, ask=20.1, ts=time.time()) for i in chain}
    sel = select_option(chain=chain, quotes=q, spot=3523.1, target1=3700.0,
                        direction=Direction.BULLISH, now=time.time())
    assert sel.ok and sel.instrument.strike == 3700.0 and sel.anchor == 3700.0


def test_an_unavailable_first_choice_falls_through_to_the_next_suitable_otm():
    """'if the desired strike is not present, log it and take the nearest possible OTM' — the
    reason string names what was refused, which is what the log line carries."""
    best, second = _opt(3600), _opt(3550)
    chain = [second, best]
    liq = {best.scrip_code: (9_999.0, 9_999.0), second.scrip_code: (10.0, 10.0)}
    now = time.time()
    quotes = {
        best.scrip_code: Quote(ltp=20.0, bid=0.0, ask=20.1, ts=now),   # one-sided: not tradeable
        second.scrip_code: Quote(ltp=30.0, bid=29.9, ask=30.1, ts=now),
    }
    sel = select_option(chain=chain, quotes=quotes, spot=3523.1, target1=3800.0,
                        direction=Direction.BULLISH, now=now, strike_anchor=3650.0, liquidity=liq)
    assert sel.ok and sel.instrument is second, "the most liquid was untradeable; the next stands in"
    assert rank_by_liquidity(chain, liq, 3650.0)[0] is best, "and it WAS the first choice"


# -- the wider OI band, and what is deliberately untouched ----------------------------------------


def test_open_interest_is_subscribed_wider_than_the_traded_shortlist():
    s = Settings(_env_file=None)
    assert s.universe_oi_strikes_per_side == 12 > s.universe_strikes_per_side == 5
    assert s.strike_anchor_min_atr == 0.35


@pytest.mark.asyncio
async def test_the_exit_ladder_is_untouched_by_any_of_this(settings, equity):
    """The operator's condition: the strike logic must not move the targets or the stop, for any
    book or twin. Those come from the confluence engine and nothing here writes to them."""
    import inspect

    from kotsin_nse.engine import Engine

    src = inspect.getsource(Engine._select_instrument) + inspect.getsource(Engine._strike_span)
    for forbidden in ("sig.targets =", "sig.stop =", "option_targets =", "option_sl ="):
        assert forbidden not in src, f"strike selection must not write {forbidden}"
    e = Engine(settings)
    # and the anchor is a read: no pivots loaded -> None, never an exception, never a mutation
    assert e.strike_anchor("NOSUCH", 100.0, Direction.BULLISH) is None
