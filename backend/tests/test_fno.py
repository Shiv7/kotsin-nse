"""Futures and options read together: the 64-state table, Black-76 IV and the chain read."""

from __future__ import annotations

import pytest

from kotsin_nse.fno.chain import OptQuote, StrikeRow, black76, implied_vol, read_chain
from kotsin_nse.fno.positioning import ROWS, Leg, read

# -- the 64-state table -------------------------------------------------------------------------------


def test_the_table_is_numbered_as_the_operator_wrote_it():
    assert len(ROWS) == 64 and len(set(ROWS)) == 64
    assert ROWS[0] == ("LB", "LB", "SB")  # row 1
    assert ROWS[12] == ("LB", "LU", "SC")  # row 13
    assert ROWS[38] == ("SB", "LB", "LB")  # row 39, the worked example
    assert ROWS[63] == ("LU", "SB", "LB")  # row 64


@pytest.mark.parametrize(
    ("legs", "row", "score", "bias", "premium"),
    [
        # row 1: futures LB, calls LB, puts SB → +4 Strong Bull, Normal
        (((1, 5), (2, 5), (-2, 5)), 1, 4.0, "Strong Bull", "Normal"),
        # row 39: futures SB, calls LB, puts LB → −2 Bear, both premiums up = IV↑ big move
        (((-1, 5), (2, 5), (2, 5)), 39, -2.0, "Bear", "IV↑ big move"),
        # row 13: futures LB, calls LU, puts SC → +1 Mild Bull, calls fall while futures rise
        (((1, 5), (-2, -5), (2, -5)), 13, 1.0, "Mild Bull", "Divergence"),
        # row 45: futures SB, calls LU, puts SC → −3 Strong Bear
        (((-1, 5), (-2, -5), (2, -5)), 45, -3.0, "Strong Bear", "Normal"),
    ],
)
def test_reading_matches_the_table(legs, row, score, bias, premium):
    (fp, fo), (cp, co), (pp, po) = legs
    r = read(Leg(fp, fo, fo * 1.0), Leg(cp, co, co * 1.0), Leg(pp, po, po * 1.0))
    assert (r.row, r.score, r.bias, r.premium) == (row, score, bias, premium)
    assert r.weak == ()


def test_a_leg_whose_oi_barely_moved_is_flagged_weak():
    r = read(Leg(0.4, 10, 0.05), Leg(2, 5, 3.0), Leg(-2, 5, None))
    assert r.weak == ("futures", "puts")


# -- pricing --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("call", [True, False])
def test_implied_vol_recovers_the_vol_it_was_priced_with(call):
    price = black76(9500, 9400, 20 / 365, 0.35, call)
    assert implied_vol(price, 9500, 9400, 20 / 365, call) == pytest.approx(0.35, abs=1e-6)


def test_no_iv_from_a_premium_at_or_below_intrinsic():
    assert implied_vol(99.0, 9500, 9400, 20 / 365, True) is None
    assert implied_vol(None, 9500, 9400, 20 / 365, True) is None


# -- the chain ------------------------------------------------------------------------------------------


def _q(ltp, prev, oi, ref):
    return OptQuote(ltp=ltp, prev_close=prev, oi=oi, oi_ref=ref)


def test_chain_reads_walls_pcr_straddle_and_legs():
    rows = [
        StrikeRow(9400, _q(160, 140, 1_000, 900), _q(60, 70, 4_000, 3_000)),  # puts written below
        StrikeRow(9500, _q(100, 90, 2_000, 1_500), _q(100, 110, 2_000, 2_000)),
        StrikeRow(9600, _q(60, 50, 5_000, 4_800), _q(160, 170, 500, 600)),  # call wall above
    ]
    c = read_chain(rows, future=9500, future_prev=9450, t=20 / 365, t_prev=21 / 365)
    v = c.view
    assert v["atm"] == 9500 and v["straddle"] == 200 and v["straddle_prev"] == 200
    assert v["implied_move_pct"] == pytest.approx(200 / 9500 * 100)
    assert v["call_wall"] == {"strike": 9600, "value": 5_000}
    assert v["put_wall"] == {"strike": 9400, "value": 4_000}
    assert v["put_add"] == {"strike": 9400, "value": 1_000}
    assert v["pcr_oi"] == pytest.approx(6_500 / 8_000)
    assert v["pcr_oi_chg"] == pytest.approx(900 / 800)
    assert v["iv"] is not None and 0 < v["iv"] < 100
    # calls: premiums up, OI up → long build-up; puts: premiums down, OI up → short build-up
    assert c.calls is not None and c.calls.state == "LB"
    assert c.puts is not None and c.puts.state == "SB"


def test_a_chain_without_references_still_reads_prices_but_no_oi_change():
    rows = [StrikeRow(100, _q(5, 4, 10, None), _q(5, 6, 10, None))]
    c = read_chain(rows, future=100, future_prev=None, t=0.01, t_prev=None)
    assert c.view["pcr_oi_chg"] is None and c.view["call_add"] is None
    assert c.calls is not None and c.calls.oi_chg_pct is None and c.calls.weak


def test_a_tiny_contract_count_is_weak_whatever_its_percentage():
    """GOLD 2026-10-20: calls +7 contracts (+1.2 % of a thin chain) against 16,739 in the future."""
    assert not Leg(-22.3, 7, 1.23).weak
    assert Leg(-22.3, 7, 1.23, min_contracts=167).weak
    assert not Leg(-16.8, 2_323, 15.5, min_contracts=143).weak


def test_black76_greeks_match_parity_and_finite_differences():
    import math

    from kotsin_nse.fno.chain import RISK_FREE, black76, greeks76

    f, k, t, s = 1193.2, 1190.0, 4 / 365, 0.225
    c, p = greeks76(f, k, t, s, True), greeks76(f, k, t, s, False)
    assert c is not None and p is not None
    assert c["delta"] - p["delta"] == pytest.approx(math.exp(-RISK_FREE * t))  # put-call parity
    assert c["gamma"] == pytest.approx(p["gamma"]) and c["vega"] == pytest.approx(p["vega"])
    h = 0.01
    assert c["delta"] == pytest.approx((black76(f + h, k, t, s, True) - black76(f - h, k, t, s, True)) / (2 * h), rel=1e-4)
    assert c["vega"] == pytest.approx(black76(f, k, t, s + 0.005, True) - black76(f, k, t, s - 0.005, True), rel=1e-3)
    dt = 0.001 / 365  # theta is the instantaneous decay per day: a tiny central step, not a whole day
    assert c["theta"] == pytest.approx((black76(f, k, t - dt, s, True) - black76(f, k, t + dt, s, True)) / (2 * dt) / 365, rel=1e-3)
    assert greeks76(f, k, 0.0, s, True) is None
