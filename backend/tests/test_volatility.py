"""Volatility regime -> pivot-cluster width."""

from __future__ import annotations

from kotsin_nse.market.volatility import (
    CLUSTER_K,
    FALLBACK_K,
    Band,
    band_from_realised,
    band_from_vix,
    regime_for_commodity,
    regime_for_equity,
)


def test_the_vix_bands_are_the_old_stacks_thresholds():
    assert band_from_vix(9.0) is Band.COMPLACENT
    assert band_from_vix(11.0) is Band.LOW
    assert band_from_vix(13.9) is Band.LOW
    assert band_from_vix(14.0) is Band.NEUTRAL
    assert band_from_vix(18.0) is Band.ELEVATED
    assert band_from_vix(22.0) is Band.HIGH
    assert band_from_vix(28.0) is Band.EXTREME
    assert band_from_vix(45.0) is Band.EXTREME


def test_more_volatility_widens_the_cluster_and_neutral_is_the_old_flat_value():
    ks = [CLUSTER_K[b] for b in
          (Band.COMPLACENT, Band.LOW, Band.NEUTRAL, Band.ELEVATED, Band.HIGH, Band.EXTREME)]
    assert ks == sorted(ks), "k must rise monotonically with volatility"
    assert CLUSTER_K[Band.NEUTRAL] == 0.30, "the old stack's flat CLUSTER_ATR_MULTIPLE"
    # A second-order correction, not a restatement of volatility: ATR already scales ~linearly
    # with VIX, so the k range stays near 3x rather than the ~3.6x VIX range itself.
    assert 3.0 <= CLUSTER_K[Band.EXTREME] / CLUSTER_K[Band.COMPLACENT] <= 4.0
    # Convex: the top step is larger than the bottom one.
    assert (CLUSTER_K[Band.EXTREME] - CLUSTER_K[Band.HIGH]) > (
        CLUSTER_K[Band.LOW] - CLUSTER_K[Band.COMPLACENT]
    )


def test_a_missing_vix_print_falls_back_to_the_old_flat_value_and_says_so():
    r = regime_for_equity(None)
    assert r.k == FALLBACK_K and r.source == "fallback"
    assert "no India VIX print" in r.detail
    assert regime_for_equity(0.0).source == "fallback"


def test_an_equity_regime_reports_the_print_that_produced_it():
    r = regime_for_equity(24.5)
    assert r.band is Band.HIGH and r.k == 0.55
    assert r.source == "india_vix" and r.value == 24.5
    assert "24.50" in r.detail


def test_commodities_band_on_their_own_realised_vol_not_on_india_vix():
    """An equity-index implied vol says nothing about crude."""
    assert band_from_realised(2.0) is Band.EXTREME
    assert band_from_realised(1.0) is Band.NEUTRAL
    assert band_from_realised(0.5) is Band.COMPLACENT

    calm = [1.0] * 20 + [0.6]          # today is 0.6x its own median
    wild = [1.0] * 20 + [1.9]          # today is 1.9x
    assert regime_for_commodity(calm).band is Band.COMPLACENT
    assert regime_for_commodity(wild).band is Band.EXTREME
    assert regime_for_commodity(wild).source == "realised"
    assert "own 20-session median" in regime_for_commodity(wild).detail


def test_a_commodity_without_enough_history_falls_back_rather_than_guessing():
    r = regime_for_commodity([1.0, 1.1])
    assert r.source == "fallback" and r.k == FALLBACK_K
    assert "not enough sessions" in r.detail


def test_the_cluster_width_in_rupees_widens_with_both_atr_and_the_band():
    """k x ATR: ATR carries the first-order move, k the implied-realised gap on top of it."""
    calm_px, calm_atr = 1200.0, 1200 * 0.0029   # VIX 16-ish 30m move
    wild_atr = 1200 * 0.0057                    # VIX 32-ish
    calm = regime_for_equity(16.0).k * calm_atr
    wild = regime_for_equity(32.0).k * wild_atr
    assert wild / calm > 4.0, "ATR doubles and k rises 2.5x -> the zone is ~5x wider in rupees"
    assert calm / calm_px * 100 < 0.15, "a calm tape clusters tightly in percentage terms"
