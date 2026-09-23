from datetime import date

import pytest

from kotsin_nse.market.iv import (
    IvHistory,
    IvPoint,
    atm_iv,
    bs_price,
    implied_vol,
    ladder_tolerance_pct,
    merge_points,
    regime_for_name,
    seed_points,
    years_to_expiry,
)
from kotsin_nse.market.volatility import Band, regime_for_equity


def test_implied_vol_inverts_the_price_it_was_given():
    for sigma in (0.12, 0.25, 0.60):
        px = bs_price(1000.0, 1010.0, 10 / 365, sigma, call=True)
        assert implied_vol(px, 1000.0, 1010.0, 10 / 365, call=True) == pytest.approx(sigma, abs=1e-4)
        px = bs_price(1000.0, 990.0, 10 / 365, sigma, call=False)
        assert implied_vol(px, 1000.0, 990.0, 10 / 365, call=False) == pytest.approx(sigma, abs=1e-4)


def test_no_sigma_prices_an_option_at_intrinsic_or_a_bad_print():
    assert implied_vol(0.0, 100.0, 100.0, 0.02, call=True) is None
    assert implied_vol(25.0, 100.0, 75.0, 0.02, call=True) is None, "at intrinsic"
    assert implied_vol(90.0, 100.0, 100.0, 0.02, call=True) is None, "above any volatility"
    assert implied_vol(1.0, 100.0, 100.0, 0.0, call=True) is None, "expired"


def test_canbk_at_0945_has_an_atm_iv_near_22_pct_against_an_index_vix_of_10_7():
    """CE125 1.56, PE125 1.23, spot 124.94, six days to the 29-Sep expiry (2026-09-23)."""
    iv = atm_iv(124.94, 125.0, years_to_expiry("2026-09-29", date(2026, 9, 23)), 1.56, 1.23)
    assert iv is not None and 0.20 < iv < 0.24
    assert atm_iv(124.94, 125.0, 6 / 365, None, 1.23) is not None, "one side is enough"
    assert atm_iv(124.94, 125.0, 6 / 365, None, None) is None


def test_the_ladder_tolerance_is_the_parents_noise_through_delta():
    # CANBK: k 0.20 (COMPLACENT), ATR30 0.402, delta 0.50, premium 1.56 -> 2.58 %
    assert ladder_tolerance_pct(0.20, 0.402, 0.50, 1.56) == pytest.approx(2.58, abs=0.01)
    assert ladder_tolerance_pct(0.20, 0.402, 0.50, 0.0) == 0.0


def test_seeded_history_bands_a_name_on_its_own_median_and_live_points_win(tmp_path):
    h = IvHistory(tmp_path / "iv")
    days = [date(2026, 9, d) for d in (1, 2, 3, 4, 7, 8, 9, 10, 11, 14, 15, 16)]
    assert h.seed("canbk", [IvPoint(d, 0.20) for d in days]) == 12
    med, n = h.median_before("CANBK", date(2026, 9, 23))
    assert med == pytest.approx(0.20) and n == 12
    r = regime_for_name(0.30, med, n, fallback=regime_for_equity(10.7))
    assert r.source == "stock_iv" and r.band is Band.HIGH and r.k == 0.55
    r = regime_for_name(0.20, med, n, fallback=regime_for_equity(10.7))
    assert r.band is Band.NEUTRAL
    # too little history -> the fallback, whatever the IV says
    thin = IvHistory(tmp_path / "iv2")
    thin.seed("X", [IvPoint(d, 0.2) for d in days[:5]])
    med, n = thin.median_before("X", date(2026, 9, 23))
    assert med is None and regime_for_name(0.9, med, n, fallback=regime_for_equity(10.7)).source == "india_vix"
    # a live point for a day replaces the seeded one and re-seeding cannot overwrite it
    h.record("CANBK", date(2026, 9, 16), 0.50)
    assert h.seed("CANBK", [IvPoint(date(2026, 9, 16), 0.20)]) == 0
    assert [p.iv for p in h.points("CANBK") if p.day == date(2026, 9, 16)] == [0.50]
    # round trip
    h.save_all()
    again = IvHistory(tmp_path / "iv")
    assert [p.iv for p in again.points("CANBK")][-1] == 0.50


def test_seed_points_pair_the_option_close_with_the_parents_close_that_day():
    rows = [{"dt": "2026-09-15T09:15:00", "c": 2.0}, {"dt": "2026-09-16T09:15:00", "c": 0.0}, {"dt": "2026-09-17T09:15:00", "c": 2.5}]
    spots = {"2026-09-15": 100.0, "2026-09-17": 100.0}
    ce = seed_points(rows, spots, strike=101.0, call=True, expiry="2026-09-29")
    assert [p.day.day for p in ce] == [15, 17], "the zero print and the day without a parent close are skipped"
    pe = seed_points([{"dt": "2026-09-15T09:15:00", "c": 3.0}], spots, strike=101.0, call=False, expiry="2026-09-29")
    merged = merge_points(ce, pe)
    assert len(merged) == 2 and merged[0].day.day == 15


def test_the_engine_tolerance_uses_the_names_own_regime_when_it_has_one(settings, equity):
    import time

    from kotsin_nse.bars.unified import BarSource, UnifiedBar
    from kotsin_nse.domain import OptionType
    from kotsin_nse.engine import Engine
    from kotsin_nse.instrument.legs import OPTION_CLUSTER_TOL_PCT
    from kotsin_nse.market.volatility import INDIA_VIX_SCRIP

    e = Engine(settings)
    e.underlyings[equity.symbol] = equity
    # no ATR yet: the flat percent, on the fallback regime
    tol, reg = e.option_ladder_tolerance(equity.symbol, 125.0, OptionType.CE, 1.56)
    assert tol == OPTION_CLUSTER_TOL_PCT and reg.source == "fallback"
    base = time.time() - 40 * 1800
    e.store.seed(equity.symbol, "30m", [
        UnifiedBar(symbol=equity.symbol, scrip_code=equity.scrip_code, tf="30m", ts=base + i * 1800, open=124.9,
                   high=125.1, low=124.7, close=124.9, volume=1000, source=BarSource.REST, complete=True)
        for i in range(40)
    ])  # ATR30 = 0.40
    e.ltps[equity.scrip_code] = 124.94
    e.ltps[INDIA_VIX_SCRIP] = 10.27
    from kotsin_nse.bars.indicators import atr
    from kotsin_nse.instrument.select import estimate_delta

    tol_vix, reg = e.option_ladder_tolerance(equity.symbol, 125.0, OptionType.CE, 1.56)
    a = atr(e.store.bars(equity.symbol, "30m", 60), 14)
    d = abs(estimate_delta(spot=124.94, strike=125.0, option_type=OptionType.CE))
    assert reg.source == "india_vix" and reg.k == 0.20
    assert tol_vix == pytest.approx(ladder_tolerance_pct(0.20, a, d, 1.56), rel=1e-6) and 2.0 < tol_vix < 3.0
    # the name's own history + a live IV well above its median -> its own band, a wider k
    e.iv_history.seed(equity.symbol, [IvPoint(date(2026, 9, d), 0.20) for d in (1, 2, 3, 4, 7, 8, 9, 10, 11, 14, 15, 16)])
    e.stock_iv[equity.symbol] = (0.30, time.time())
    tol_own, reg = e.option_ladder_tolerance(equity.symbol, 125.0, OptionType.CE, 1.56)
    assert reg.source == "stock_iv" and reg.band is Band.HIGH and tol_own == pytest.approx(tol_vix * 0.55 / 0.20, rel=1e-3)
    snap = e.stock_iv_snapshot(equity.symbol)
    assert snap["atmIv"] == 0.3 and snap["medianIv"] == 0.2 and snap["sessions"] == 12 and snap["clusterK"] == 0.55
