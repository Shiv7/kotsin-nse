"""The FUDKII-RT card: walls both sides, dual-trigger stop, geometric odds."""

from __future__ import annotations

from kotsin_nse.alerts import rtcard
from kotsin_nse.bars.pivots import WALL_MIN_STRENGTH, Zone


def _zones() -> list[Zone]:
    return [
        Zone(price=90.0, strength=9.2, members=["1d.S1", "1wk.S2"]),   # support below
        Zone(price=97.0, strength=2.0, members=["1mo.FIB_S1"]),        # weak, just below
        Zone(price=104.0, strength=15.2, members=["1d.R1", "1wk.R1", "1mo.PIVOT"]),  # wall above
    ]


def test_a_bullish_trade_finds_the_wall_above_and_the_support_below():
    z = _zones()
    ahead = rtcard.find_wall(z, 100.0, atr=2.0, ahead=True, bullish=True)
    behind = rtcard.find_wall(z, 100.0, atr=2.0, ahead=False, bullish=True)
    assert ahead is not None and ahead.price == 104.0 and ahead.side == "AHEAD"
    assert ahead.qualifies and ahead.grade == "FORTRESS"
    assert ahead.timeframes == ["1d", "1mo", "1wk"]
    assert ahead.dist_atr == 2.0
    # The nearest zone below is the weak one — nearest, not strongest, is the honest answer.
    assert behind is not None and behind.price == 97.0 and behind.side == "BEHIND"
    assert not behind.qualifies and behind.grade == "WEAK"


def test_the_sides_swap_for_a_bearish_trade():
    z = _zones()
    ahead = rtcard.find_wall(z, 100.0, atr=2.0, ahead=True, bullish=False)
    behind = rtcard.find_wall(z, 100.0, atr=2.0, ahead=False, bullish=False)
    assert ahead.price == 97.0, "a bearish trade travels down, so the wall ahead is below"
    assert behind.price == 104.0, "and the structure behind it is above"


def test_a_single_daily_level_is_not_a_wall_but_a_daily_plus_a_weekly_is():
    """WALL_MIN_STRENGTH = 5.2 with TF_WEIGHT 1d=4.0, 1wk=3.2 — the threshold sits between them."""
    lone = rtcard.find_wall(
        [Zone(price=104.0, strength=4.0, members=["1d.R1"])], 100.0, atr=2.0, ahead=True, bullish=True
    )
    pair = rtcard.find_wall(
        [Zone(price=104.0, strength=7.2, members=["1d.R1", "1wk.R1"])],
        100.0, atr=2.0, ahead=True, bullish=True,
    )
    assert not lone.qualifies
    assert pair.qualifies
    assert lone.strength < WALL_MIN_STRENGTH < pair.strength


def test_a_seven_r_setup_is_a_twelve_percent_chance_not_a_good_one():
    """Gambler's ruin: P(target first) = risk / (risk + reward). High R:R IS low probability."""
    o = rtcard.hit_probability(entry=100.0, stop=99.0, target=108.0)
    assert o["pT1"] == 11.1
    o2 = rtcard.hit_probability(entry=100.0, stop=95.0, target=105.0)
    assert o2["pT1"] == 50.0, "symmetric barriers are a coin flip"
    assert "ignores theta" in o["note"]


def test_the_stop_fires_on_whichever_side_is_reached_first():
    st = rtcard.dual_stop(
        bullish=True, equity_entry=100.0, equity_stop=96.0,
        option_entry=5.0, option_ltp=5.0, equity_ltp=100.0, delta=0.5, basis="pivot",
    )
    # 4.00 of adverse equity move x 0.5 delta = 2.00 off the premium.
    assert st["optionStop"] == 3.0
    assert st["equityStop"] == 96.0
    assert st["constantForSession"] is True, "a pivot stop does not move all session"
    assert st["deltaRefreshS"] == 10.0
    # equity is 4% above its stop; the option is 40% above its own — equity triggers first.
    assert st["equityDistPct"] == 4.0
    assert st["optionDistPct"] == 40.0
    assert st["triggersFirst"] == "EQUITY"


def test_a_bearish_stop_measures_distance_the_other_way():
    st = rtcard.dual_stop(
        bullish=False, equity_entry=100.0, equity_stop=104.0,
        option_entry=5.0, option_ltp=5.0, equity_ltp=100.0, delta=0.5, basis="pivot",
    )
    assert st["optionStop"] == 3.0
    assert st["equityDistPct"] == 4.0, "price must RISE 4% to stop a bearish trade"


def test_the_option_ladder_is_linear_in_delta_and_says_so():
    lad = rtcard.option_ladder(option_entry=5.0, equity_entry=100.0, targets=[104.0, 110.0], delta=0.5)
    assert lad[0]["option"] == 7.0   # 4.00 x 0.5
    assert lad[1]["option"] == 10.0  # 10.00 x 0.5
    assert lad[0]["optionGainPct"] == 40.0


def test_support_behind_raises_confidence_and_an_obstacle_ahead_lowers_it():
    strong_behind = rtcard.find_wall(
        [Zone(price=98.0, strength=15.2, members=["1d.S1", "1wk.S1", "1mo.S1"])],
        100.0, atr=2.0, ahead=False, bullish=True,
    )
    strong_ahead = rtcard.find_wall(
        [Zone(price=102.0, strength=15.2, members=["1d.R1", "1wk.R1", "1mo.R1"])],
        100.0, atr=2.0, ahead=True, bullish=True,
    )
    supported = rtcard.confidence(wall_ahead=None, wall_behind=strong_behind, surge=2.0, p_t1=50.0)
    blocked = rtcard.confidence(wall_ahead=strong_ahead, wall_behind=None, surge=2.0, p_t1=50.0)
    assert supported["score"] > blocked["score"]
    assert any("support" in c["factor"] for c in supported["components"])
    assert any(c["points"] < 0 for c in blocked["components"])


def test_dte_counts_days_to_expiry():
    from datetime import date

    assert rtcard.dte("2026-09-29", today=date(2026, 9, 22)) == 7
    assert rtcard.dte("", today=date(2026, 9, 22)) is None


def test_the_option_stop_triggers_on_adverse_movement_before_the_equity_does():
    """The point of the delta leg: a CE bleeds as spot falls, a PE bleeds as spot rises, so the
    option level can be reached while the equity is still short of its own."""
    # Bullish CE. Equity entered 100, stop 96, premium 5.00, delta 0.5 -> option stop 3.00.
    # Spot has slipped to 97 (not yet stopped) but the premium has collapsed to 2.80.
    st = rtcard.dual_stop(
        bullish=True, equity_entry=100.0, equity_stop=96.0,
        option_entry=5.0, option_ltp=2.80, equity_ltp=97.0, delta=0.5, basis="pivot",
    )
    assert st["optionStop"] == 3.0
    assert st["equityHit"] is False, "97 has not reached the 96 equity stop"
    assert st["optionHit"] is True, "but 2.80 is through the 3.00 option stop"
    assert st["triggered"] is True
    assert st["triggeredBy"] == "OPTION"

    # Bearish PE, the mirror: spot rising is the adverse direction.
    st2 = rtcard.dual_stop(
        bullish=False, equity_entry=100.0, equity_stop=104.0,
        option_entry=5.0, option_ltp=2.80, equity_ltp=103.0, delta=0.5, basis="pivot",
    )
    assert st2["equityHit"] is False
    assert st2["optionHit"] is True
    assert st2["triggeredBy"] == "OPTION"


def test_neither_side_hit_reports_no_trigger():
    st = rtcard.dual_stop(
        bullish=True, equity_entry=100.0, equity_stop=96.0,
        option_entry=5.0, option_ltp=5.4, equity_ltp=100.8, delta=0.5, basis="pivot",
    )
    assert st["triggered"] is False and st["triggeredBy"] is None


def test_the_card_projects_the_option_levels_the_engine_itself_would_set():
    """Not a second copy of the formula: map_levels_to_option is the engine's own."""
    from kotsin_nse.instrument.select import map_levels_to_option

    stop, targets = map_levels_to_option(
        equity_entry=100.0, equity_stop=96.0, equity_targets=(104.0, 110.0),
        option_premium=5.0, delta=0.5,
    )
    card_stop = rtcard.dual_stop(
        bullish=True, equity_entry=100.0, equity_stop=96.0, option_entry=5.0,
        option_ltp=5.0, equity_ltp=100.0, delta=0.5, basis="pivot",
    )["optionStop"]
    card_ladder = [r["option"] for r in rtcard.option_ladder(
        option_entry=5.0, equity_entry=100.0, targets=[104.0, 110.0], delta=0.5
    )]
    assert card_stop == stop
    assert card_ladder == list(targets)


def test_sizing_takes_the_lower_of_the_capital_cap_and_the_lot_cap():
    """Rs 1,00,000 or 4 lots, whichever binds first — and the card says which one did."""
    # Cheap premium: 4 lots cost 40,000, so the LOT cap binds, not the capital one.
    cheap = rtcard.size(option_premium=20.0, lot_size=500, open_trades=0)
    assert cheap["lots"] == 4 and cheap["binding"] == "lot cap"
    assert cheap["capital"] == 40_000
    assert cheap["unusedReturnedToWallet"] == 60_000, "unspent capital is not reserved"

    # Rich premium: one lot is 35,000, so capital allows 2 and the CAPITAL cap binds.
    rich = rtcard.size(option_premium=70.0, lot_size=500, open_trades=0)
    assert rich["lots"] == 2 and rich["binding"] == "capital cap"
    assert rich["capital"] == 70_000
    assert rich["unusedReturnedToWallet"] == 30_000


def test_a_lot_too_expensive_for_the_cap_is_declined_not_part_filled():
    out = rtcard.size(option_premium=300.0, lot_size=500, open_trades=0)  # 1,50,000 a lot
    assert out["rejected"] and out["lots"] == 0
    assert "declined rather than part-filled" in out["reason"]


def test_concurrency_is_capped_at_thirty_open_trades():
    assert rtcard.size(option_premium=20.0, lot_size=500, open_trades=29)["lots"] == 4
    full = rtcard.size(option_premium=20.0, lot_size=500, open_trades=30)
    assert full["rejected"] and full["binding"] == "concurrency"
    assert full["slotsLeft"] == 0


def test_a_contract_that_is_not_quoting_cannot_be_sized():
    out = rtcard.size(option_premium=None, lot_size=500, open_trades=0)
    assert out["rejected"] and out["binding"] == "no premium"
