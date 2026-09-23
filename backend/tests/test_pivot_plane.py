"""The pivot data plane wired into the engine — offline, no broker, no start()."""

from datetime import date, datetime, timedelta

from kotsin_nse.bars.daily import MIN_DAILY_BARS, is_official
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.engine import Engine
from kotsin_nse.market.session import ist_today


def _bar(sym: str, code: str, day: date, close: float, *, source=BarSource.REST, complete=True) -> UnifiedBar:
    ts = datetime(day.year, day.month, day.day, 9, 15).timestamp()
    return UnifiedBar(symbol=sym, scrip_code=code, tf="1d", ts=ts, open=close, high=close * 1.01,
                      low=close * 0.99, close=close, volume=1e6, source=source, complete=complete)


def _official_series(sym: str, code: str, *, n: int = MIN_DAILY_BARS + 10) -> list[UnifiedBar]:
    today = ist_today()
    out, d, i = [], today - timedelta(days=1), 0
    while len(out) < n:
        if d.weekday() < 5:
            out.append(_bar(sym, code, d, 1000 + (i % 7) * 3.0))
            i += 1
        d -= timedelta(days=1)
    return list(reversed(out))


def test_zones_are_not_served_or_cached_from_a_tick_built_previous_session(settings, equity):
    e = Engine(settings)
    e.underlyings[equity.symbol] = equity
    series = _official_series(equity.symbol, equity.scrip_code)
    # yesterday's bar as the aggregator would have left it overnight: last print, not official
    series[-1] = _bar(equity.symbol, equity.scrip_code, ist_today() - timedelta(days=1), 1003.0,
                      source=BarSource.LIVE, complete=True)
    e.store.seed(equity.symbol, "1d", series)

    assert e.zones_for(equity.symbol) == []
    assert equity.symbol not in e._zone_cache, "a refusal must not be cached until the band changes"
    assert e.daily_audit().unofficial == [equity.symbol]

    # the official candle lands (what _seed_daily / the repair loop do) → real zones, now cached
    yday = (ist_today() - timedelta(days=1)).isoformat()
    e._seed_daily(equity, [{"dt": f"{yday}T09:15:00", "o": 1003.0, "h": 1013.0, "l": 993.0, "c": 1003.0, "v": 1e6}])
    zones = e.zones_for(equity.symbol)
    assert zones and equity.symbol in e._zone_cache
    assert e.daily_audit().ready
    assert is_official(e.store.bars(equity.symbol, "1d")[-1])


def test_a_short_series_yields_no_zones_and_no_cache_entry(settings, equity):
    e = Engine(settings)
    e.underlyings[equity.symbol] = equity
    e.store.seed(equity.symbol, "1d", _official_series(equity.symbol, equity.scrip_code, n=5))
    assert e.zones_for(equity.symbol) == [] and equity.symbol not in e._zone_cache
    assert e.daily_audit().short == [equity.symbol]


def test_the_disk_cache_seeds_the_store_before_rest_is_asked(settings, equity):
    e = Engine(settings)
    e.underlyings[equity.symbol] = equity
    series = _official_series(equity.symbol, equity.scrip_code)
    assert e.daily_cache.save(equity.symbol, series) == len(series)

    fresh = Engine(settings)  # a new process, same data_dir
    fresh.underlyings[equity.symbol] = equity
    assert fresh.store.bars(equity.symbol, "1d") == []
    fresh._seed_daily_from_cache([equity])
    held = fresh.store.bars(equity.symbol, "1d")
    assert len(held) == len(series) and all(is_official(b) for b in held)
    assert fresh.zones_for(equity.symbol), "levels are real straight from the cache"


def test_seed_daily_writes_the_cache_and_voids_the_zone_cache(settings, equity):
    e = Engine(settings)
    e.underlyings[equity.symbol] = equity
    e.store.seed(equity.symbol, "1d", _official_series(equity.symbol, equity.scrip_code))
    assert e.zones_for(equity.symbol) and equity.symbol in e._zone_cache
    e._daily_failed.add(equity.symbol)
    day = (ist_today() - timedelta(days=1)).isoformat()
    e._seed_daily(equity, [{"dt": f"{day}T09:15:00", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}])
    assert equity.symbol not in e._zone_cache and equity.symbol not in e._daily_failed
    assert e.daily_cache.path(equity.symbol).exists()


def test_expected_legs_is_empty_without_groups(settings):
    e = Engine(settings)
    assert e._expected_legs([]) == []
