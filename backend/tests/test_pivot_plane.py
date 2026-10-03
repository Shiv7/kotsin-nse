"""The pivot data plane wired into the engine — offline, no broker, no start()."""

from datetime import date, datetime, timedelta

from kotsin_nse.bars.daily import MIN_DAILY_BARS, is_official
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.engine import Engine
from kotsin_nse.market.session import from_ist, ist_today


def _bar(sym: str, code: str, day: date, close: float, *, source=BarSource.REST, complete=True) -> UnifiedBar:
    # 5paisa's END-OF-DAY daily row is stamped 00:00 IST; the 09:15 stamp is its provisional one,
    # which sets no levels (bars/zones.py)
    ts = from_ist(datetime(day.year, day.month, day.day, 0, 0))
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
    e._seed_daily(equity, [{"dt": f"{yday}T00:00:00", "o": 1003.0, "h": 1013.0, "l": 993.0, "c": 1003.0, "v": 1e6}])
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


def test_a_provisional_previous_session_sets_no_levels_until_the_end_of_day_candle_lands(settings, equity):
    """5paisa serves an NSE session twice: the provisional row stamped 09:15 (its high and low can
    still be wrong) and the end-of-day row stamped 00:00. Only the latter sets levels."""
    e = Engine(settings)
    e.underlyings[equity.symbol] = equity
    series = _official_series(equity.symbol, equity.scrip_code)
    yday = ist_today() - timedelta(days=1)
    while yday.weekday() >= 5:
        yday -= timedelta(days=1)
    series = [b for b in series if b.ts < from_ist(datetime(yday.year, yday.month, yday.day))]
    series.append(UnifiedBar(symbol=equity.symbol, scrip_code=equity.scrip_code, tf="1d",
                             ts=from_ist(datetime(yday.year, yday.month, yday.day, 9, 15)), open=1000.0, high=1010.0,
                             low=990.0, close=1003.0, volume=1e6, source=BarSource.REST, complete=True))
    e.store.seed(equity.symbol, "1d", series)
    assert e.zones_for(equity.symbol) == [] and e.zone_refusals[equity.symbol].startswith("provisional")


def _provisional_engine(settings, equity) -> tuple[Engine, date]:
    e = Engine(settings)
    e.underlyings[equity.symbol] = equity
    yday = ist_today() - timedelta(days=1)
    while yday.weekday() >= 5:
        yday -= timedelta(days=1)
    series = [b for b in _official_series(equity.symbol, equity.scrip_code) if b.ts < from_ist(datetime(yday.year, yday.month, yday.day))]
    series.append(UnifiedBar(symbol=equity.symbol, scrip_code=equity.scrip_code, tf="1d",
                             ts=from_ist(datetime(yday.year, yday.month, yday.day, 9, 15)), open=1000.0, high=1010.0,
                             low=990.0, close=1003.0, volume=1e6, source=BarSource.REST, complete=True))
    e.store.seed(equity.symbol, "1d", series)
    e._daily_due = e._legs_due = False
    return e, yday


async def test_a_provisional_name_is_re_asked_on_a_backoff_until_the_end_of_day_candle_lands(settings, equity):
    """Review, 2026-10-03: asked once a session, a name still provisional at 08:30 stayed without
    levels until 15:45. It is re-asked every PROVISIONAL_RETRY_S until the 00:00 candle lands."""
    e, yday = _provisional_engine(settings, equity)
    asked: list[str] = []
    stamp = ["09:15:00"]

    async def candles(inst, tf, start, end):
        asked.append(inst.symbol)
        return [{"dt": f"{yday.isoformat()}T{stamp[0]}", "o": 1000.0, "h": 1010.0, "l": 990.0, "c": 1003.0, "v": 1e6}]

    e.rest.candles = candles  # type: ignore[method-assign]
    await e._pivot_repair()
    assert asked == [equity.symbol] and e.daily_audit().provisional == [equity.symbol], "the broker still has the provisional one"
    await e._pivot_repair()
    assert asked == [equity.symbol], "not again within the backoff"
    e._daily_provisional_asked[equity.symbol] = 0.0
    stamp[0] = "00:00:00"  # the end-of-day candle has landed
    await e._pivot_repair()
    assert asked == [equity.symbol, equity.symbol] and e.daily_audit().ok == [equity.symbol]
    assert e.zones_for(equity.symbol), "levels again"


def test_the_zones_line_alarms_on_a_provisional_candle_once_the_session_is_under_way(settings, equity):
    e, _ = _provisional_engine(settings, equity)
    e.booting = False
    tue = date(2026, 10, 6)

    def at(hm: str) -> float:
        return from_ist(datetime(tue.year, tue.month, tue.day, *map(int, hm.split(":"))))

    a = e.daily_audit()
    early, late = e._zones_check(a, now=at("09:00")), e._zones_check(a, now=at("10:30"))
    assert early.ok and "provisional candle: 1" in early.detail
    assert not late.ok and equity.symbol in late.detail and "provisional after 09:20" in late.detail


def test_a_name_the_zones_refuse_has_no_pivots_either(settings):
    """Gate B's "key level ahead" and the counter legs read the same build as the zones (review,
    2026-10-03): a daily series on another price basis (a corporate action) gave zones nothing but
    still fed its adjusted levels to the gate."""
    from dataclasses import replace

    from .test_stage2_3_bars_and_zones import _daily, _thirty, _weekdays

    today = ist_today()
    days = _weekdays(40, today)
    intraday = [b for d in days for b in _thirty(d, 1000.0)]
    dailies = [_daily(d, 1000.0) for d in days]
    e = Engine(settings)
    e.store.seed("X", "1d", dailies)
    e.store.seed("X", "30m", intraday)
    assert "1d.R1" in {p.label for p in e._pivot_points("X")}
    adjusted = [replace(b, open=b.open * 0.374, high=b.high * 0.374, low=b.low * 0.374, close=b.close * 0.374) for b in dailies]
    e2 = Engine(settings)
    e2.store.seed("X", "1d", adjusted)
    e2.store.seed("X", "30m", intraday)
    assert e2._pivot_points("X") == [] and e2.zones_for("X") == [] and e2.zone_refusals["X"].startswith("basis")
