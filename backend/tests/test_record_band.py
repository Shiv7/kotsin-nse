"""The 5-read's legs and expiry, and the record band (KN_RECORD_BAND) — RECORD-ONLY (operator, 2026-10-19).

Legs (research inputs14.build): CE1 / CE2 the first and second listed call strike strictly above the trigger's close,
PE1 / PE2 the first and second put strictly below it, on the nearest expiry at least 2 calendar days after the day —
from TODAY's date, never a pick a long-running process made at boot. The band: both types of every listed strike
within K strikes of any price each NSE name showed today, on that expiry, sticky for the day, capped.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind, OptionType
from kotsin_nse.engine import Engine
from kotsin_nse.instrument.catalogue import Catalogue
from kotsin_nse.instrument.recband import (
    RootBand,
    band,
    capped,
    legs_expiry,
    nearest_first,
    strikes_near,
)
from kotsin_nse.instrument.universe import ScripGroup
from kotsin_nse.ops import read5rec as RR
from kotsin_nse.research import read5 as R


def opt(code: str, root: str, expiry: str, strike: float, cp: str) -> Instrument:
    return Instrument(code, root, Segment.NSE_FO, InstrumentKind.OPTION, expiry=expiry, strike=strike,
                      option_type=OptionType(cp), underlying=root, lot_size=100)


def chain_cat(root: str, strikes: list[float], expiries: list[str], *, fut_expiries: tuple[str, ...] = ("2026-10-27", "2026-11-24")) -> Catalogue:
    cat = Catalogue()
    n = 0
    for e in expiries:
        for k in strikes:
            for cp in ("CE", "PE"):
                n += 1
                i = opt(f"{root}{e[5:7]}{e[8:]}{k:g}{cp}", root, e, k, cp)
                cat.by_code[i.scrip_code] = i
                cat.options_by_symbol[root].append(i)
    for j, e in enumerate(fut_expiries):
        f = Instrument(f"{root}F{j}", root, Segment.NSE_FO, InstrumentKind.FUTURE, expiry=e, underlying=root)
        cat.by_code[f.scrip_code] = f
        cat.futures_by_symbol[root].append(f)
    eq = Instrument(f"{root}EQ", root, Segment.NSE_EQ, InstrumentKind.EQUITY, underlying=root)
    cat.by_code[eq.scrip_code] = eq
    cat.equity_by_symbol[root] = eq
    return cat


# -- legs --------------------------------------------------------------------------------------------------------


def _legs(cat: Catalogue, root: str, spot: float, today: date) -> dict[str, float | None]:
    c, why = RR.contracts(cat, root, spot, today, cat.equity(root))
    assert c is not None, why
    return {k: (v.strike if v is not None else None) for k, v in c.legs.items()}


def test_the_legs_are_the_two_listed_strikes_each_side_of_the_close_whatever_the_direction():
    """Uniform steps of 10 / 50 / 100: CE1/CE2 above, PE1/PE2 below. A bullish and a bearish trigger at the same close
    have the same four legs; only the with / against split flips."""
    for step, spot in ((10.0, 1234.5), (50.0, 25_012.0), (100.0, 55_476.85)):
        strikes = [round(spot / step) * step + step * i for i in range(-6, 7)]
        cat = chain_cat("X", strikes, ["2026-10-27"])
        legs = _legs(cat, "X", spot, date(2026, 10, 19))
        above = sorted(k for k in strikes if k > spot)
        below = sorted((k for k in strikes if k < spot), reverse=True)
        assert legs == {"CE1": above[0], "CE2": above[1], "PE1": below[0], "PE2": below[1]}
    votes = {"OI": "BULL", "VOL": "BULL", "IV": "SKIP", "PREMIUM": "", "STOCK": "BEAR"}
    assert R.split(votes, "Bullish") == (["OI", "VOL"], ["STOCK"], ["IV", "PREMIUM"])
    assert R.split(votes, "Bearish") == (["STOCK"], ["OI", "VOL"], ["IV", "PREMIUM"])


def test_uneven_steps_and_a_close_exactly_on_a_strike():
    """CANBK lists 117 / 117.5 / 118 / 119 / 120; BANKBARODA steps 2.5. A close exactly on a strike belongs to neither
    side (strictly above / below)."""
    cat = chain_cat("CANBK", [115.0, 116.0, 117.0, 117.5, 118.0, 119.0, 120.0, 121.0], ["2026-10-27"])
    assert _legs(cat, "CANBK", 117.6, date(2026, 10, 19)) == {"CE1": 118.0, "CE2": 119.0, "PE1": 117.5, "PE2": 117.0}
    assert _legs(cat, "CANBK", 118.0, date(2026, 10, 19)) == {"CE1": 119.0, "CE2": 120.0, "PE1": 117.5, "PE2": 117.0}
    cat = chain_cat("BANKBARODA", [240.0, 242.5, 245.0, 247.5, 250.0], ["2026-10-27"])
    assert _legs(cat, "BANKBARODA", 245.0, date(2026, 10, 19)) == {"CE1": 247.5, "CE2": 250.0, "PE1": 242.5, "PE2": 240.0}


def test_fewer_than_two_strikes_on_a_side_leaves_the_leg_unlisted_and_the_trigger_unscored():
    cat = chain_cat("X", [100.0, 110.0, 120.0], ["2026-10-27"])
    c, _ = RR.contracts(cat, "X", 115.0, date(2026, 10, 19), cat.equity("X"))
    assert c is not None and c.legs["CE2"] is None and c.legs["CE1"] is not None and c.legs["PE2"] is not None
    assert RR.contracts(Catalogue(), "CRUDEOIL", 5000.0, date(2026, 10, 19), None) == (None, "no NSE option chain (MCX / no options listed)")


@pytest.mark.parametrize(("today", "want"), [
    ("2026-10-25", "2026-10-27"),   # 2 days before the expiry: kept
    ("2026-10-26", "2026-11-24"),   # 1 day before: rolled
    ("2026-10-27", "2026-11-24"),   # the expiry day: the next
])
def test_the_legs_expiry_rolls_two_days_out(today, want):
    cat = chain_cat("X", [100.0, 110.0, 120.0], ["2026-10-27", "2026-11-24"])
    assert legs_expiry(cat, "X", date.fromisoformat(today)) == want
    assert R.legs_expiry(["2026-10-27", "2026-11-24"], date.fromisoformat(today)) == want


def test_the_nifty_weekly_of_19_oct_as_the_research_legs_read_it():
    """NIFTY's 19 Oct weekly (20 Oct a holiday): read on 16 Oct → 19 Oct; read on 19 Oct itself → 27 Oct (xc19)."""
    cat = chain_cat("NIFTY", [25_000.0, 25_050.0, 25_100.0], ["2026-10-13", "2026-10-19", "2026-10-27", "2026-11-03"])
    assert legs_expiry(cat, "NIFTY", date(2026, 10, 16)) == "2026-10-19"
    assert legs_expiry(cat, "NIFTY", date(2026, 10, 19)) == "2026-10-27"


def test_a_stale_boot_expiry_is_never_read_and_fut0_stays_the_front_month_on_its_roll_day():
    """A process up since 23 Oct holds ``g.option_expiry`` 27 Oct; on 26 Oct the legs come from November all the same.
    FUT0 is the nearest future expiring on or after the day (research), so on 27 Oct it is still October's."""
    cat = chain_cat("X", [100.0, 110.0, 120.0], ["2026-10-27", "2026-11-24"])
    g = ScripGroup("X", Segment.NSE_EQ, cat.equity("X"), cat.equity("X"), option_expiry="2026-10-27")  # type: ignore[arg-type]
    c, _ = RR.contracts(cat, "X", 105.0, date(2026, 10, 26), g.underlying)
    assert c is not None and c.expiry == "2026-11-24" and {i.expiry for i in c.legs.values() if i} == {"2026-11-24"}
    c, _ = RR.contracts(cat, "X", 105.0, date(2026, 10, 27), g.underlying)
    assert c is not None and c.fut is not None and c.fut.expiry == "2026-10-27"


# -- the band ----------------------------------------------------------------------------------------------------


def test_the_band_is_both_types_within_k_strikes_of_the_price_range():
    cat = chain_cat("PNB", [110.0 + i for i in range(20)], ["2026-10-27"])
    ce = cat.chain("PNB", "2026-10-27", OptionType.CE)
    pe = cat.chain("PNB", "2026-10-27", OptionType.PE)
    got = band(ce, pe, 123.88, 123.88, 3)
    assert sorted({i.strike for i in got}) == [121.0, 122.0, 123.0, 124.0, 125.0, 126.0]
    assert {i.option_type for i in got} == {OptionType.CE, OptionType.PE} and len(got) == 12
    assert [i.strike for i in strikes_near(ce, 124.0, 124.0, 1)] == [123.0, 124.0, 125.0], "a strike on the price is in"


def test_the_band_is_sticky_it_only_grows():
    rb = RootBand("X", "2026-10-27", 100.0, 100.0)
    assert rb.widen(101.0) and (rb.lo, rb.hi) == (100.0, 101.0)
    assert not rb.widen(100.5) and not rb.widen(0.0)
    assert rb.widen(98.0) and (rb.lo, rb.hi) == (98.0, 101.0)


def test_the_cap_counts_only_codes_the_engine_does_not_hold_and_admits_the_nearest_first():
    a, b, c, d = (opt(f"C{i}", "X", "2026-10-27", k, "CE") for i, k in enumerate((100.0, 105.0, 110.0, 120.0)))
    order = nearest_first([d, c, a, b], {"X": 104.0})
    assert [i.strike for i in order] == [105.0, 100.0, 110.0, 120.0]
    admitted, hit = capped(order, already_extra=8, cap=10, engine_has={"C0"})
    assert [i.scrip_code for i in admitted] == ["C1", "C0", "C2"] and hit, "C0 the engine holds costs nothing"


class FakeFeed:
    """The feed's record-set surface as the engine's band sync uses it."""

    def __init__(self, engine_mf: set[str] = frozenset(), engine_oi: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.desired = {"mf": set(engine_mf), "oi": set(engine_oi), "md": set()}
        self.record: dict[str, dict[str, Instrument]] = {"mf": {}, "oi": {}, "md": {}}
        self.record_tap_errors = 0
        self.unsubs: list[tuple[str, list[str]]] = []

    def is_record(self, ch: str, code: str) -> bool:
        return code in self.record[ch]

    def is_subscribed(self, ch: str, code: str) -> bool:
        return code in self.desired[ch]

    def record_instruments(self, ch: str) -> dict[str, Instrument]:
        return dict(self.record[ch])

    def record_counts(self) -> dict[str, dict[str, int]]:
        return {ch: {"record": len(r), "extra": len(set(r) - self.desired[ch]), "socket": len(set(r) | self.desired[ch])}
                for ch, r in self.record.items()}

    async def subscribe_record(self, ch: str, insts: list[Instrument]) -> list[Instrument]:
        fresh = [i for i in insts if i.scrip_code not in self.record[ch]]
        self.record[ch].update({i.scrip_code: i for i in fresh})
        return fresh

    async def unsubscribe_record(self, ch: str, insts: list[Instrument]) -> list[Instrument]:
        self.unsubs.append((ch, [i.scrip_code for i in insts]))
        for i in insts:
            self.record[ch].pop(i.scrip_code, None)
        return insts


def _engine_with_band(settings: Any, **over: Any) -> tuple[Engine, FakeFeed, Catalogue]:
    e = Engine(settings.model_copy(update={"record_band": True, **over}))
    cat = chain_cat("PNB", [110.0 + i for i in range(20)], ["2026-10-27"])
    crude = Instrument("CRUDE", "CRUDEOIL", Segment.MCX_FO, InstrumentKind.FUTURE, expiry="2026-11-19", underlying="CRUDEOIL")
    e.catalogue_loader.catalogue = cat
    e.groups = {"PNB": ScripGroup("PNB", Segment.NSE_EQ, cat.equity("PNB"), cat.equity("PNB")),  # type: ignore[arg-type]
                "CRUDEOIL": ScripGroup("CRUDEOIL", Segment.MCX_FO, crude, None)}
    feed = FakeFeed()
    e.feed = feed  # type: ignore[assignment]
    return e, feed, cat


def _at(day: str, hms: str) -> float:
    return R.at_ist(day, hms)


async def test_the_band_follows_the_live_price_from_the_pre_open_and_skips_mcx(settings):
    """09:07 on 19 Oct: PNB's pre-open frame put 123.88 on the feed — calls and puts 121-126 (K = 3) go on the record
    set, prices and OI; CRUDEOIL (MCX) gets nothing. At 09:16 the price touches 127.2: the band grows to the chain's
    last strike, 129, and keeps 121 (sticky)."""
    e, feed, _ = _engine_with_band(settings)
    e.ltps["PNBEQ"] = 123.88
    e.ltps["CRUDE"] = 5000.0
    await e._rec_sync(_at("2026-10-19", "09:07:05"))
    strikes = sorted({i.strike for i in feed.record["mf"].values()})
    assert strikes == [121.0, 122.0, 123.0, 124.0, 125.0, 126.0] and len(feed.record["oi"]) == 12
    assert all(i.underlying == "PNB" for i in feed.record["mf"].values())
    e.ltps["PNBEQ"] = 127.2
    await e._rec_sync(_at("2026-10-19", "09:16:00"))
    assert sorted({i.strike for i in feed.record["mf"].values()}) == [121.0 + i for i in range(9)]
    h = e.record5_health()
    assert h["band"]["on"] and h["band"]["roots"] == 1 and h["subscriptions"]["mf"]["record"] == 18


async def test_the_band_waits_for_0907_and_stops_at_1530_and_is_off_by_default(settings):
    e, feed, _ = _engine_with_band(settings)
    e.ltps["PNBEQ"] = 123.88
    await e._rec_sync(_at("2026-10-19", "09:06:59"))
    await e._rec_sync(_at("2026-10-19", "15:30:00"))
    assert not feed.record["mf"] and not feed.record["oi"]
    off, feed_off, _ = _engine_with_band(settings, record_band=False)
    off.ltps["PNBEQ"] = 123.88
    await off._rec_sync(_at("2026-10-19", "10:00:00"))
    assert not feed_off.record["mf"], "KN_RECORD_BAND off (the default): no record subscription at all"
    assert Engine(settings).s.record_band is False


async def test_the_cap_stops_the_band_growing_and_codes_the_engine_holds_cost_nothing(settings):
    e, feed, _ = _engine_with_band(settings, record5_max_extra_mf=5, record5_max_extra_oi=1000, record5_walls_mf=False)
    feed.desired["mf"] = {"PNB1027124CE", "PNB1027124PE"}  # the engine's own base strikes
    e.ltps["PNBEQ"] = 123.88
    await e._rec_sync(_at("2026-10-19", "09:30:00"))
    counts = feed.record_counts()["mf"]
    assert counts["extra"] == 5 and counts["record"] == 7 and e._rec_capped["mf"]
    assert len(feed.record["oi"]) == 12 and not e._rec_capped["oi"]


async def test_a_new_day_prunes_to_its_own_band_and_the_feed_keeps_what_the_engine_holds(settings):
    e, feed, _ = _engine_with_band(settings, record5_walls_mf=False)
    e.ltps["PNBEQ"] = 123.88
    await e._rec_sync(_at("2026-10-19", "10:00:00"))
    assert len(feed.record["mf"]) == 12
    e.ltps["PNBEQ"] = 114.2
    await e._rec_sync(_at("2026-10-21", "09:07:30"))
    assert sorted({i.strike for i in feed.record["mf"].values()}) == [112.0, 113.0, 114.0, 115.0, 116.0, 117.0]
    pruned = {c for ch, codes in feed.unsubs if ch == "mf" for c in codes}
    assert pruned and all(feed.record_instruments("mf").get(c) is None for c in pruned)


async def test_the_wall_candidates_join_the_record_price_channel_once_a_bar(settings):
    """At each 30m bar's start: the largest recorded OI call at or above the price and put at or below it go on the
    record price channel (their premium is read 30 minutes later) — once per bar."""
    e, feed, cat = _engine_with_band(settings, record5_band_mf=True)
    e.ltps["PNBEQ"] = 120.0
    rec = e.recorder
    assert rec is not None
    t = _at("2026-10-19", "09:44:00")
    rec.roll(date(2026, 10, 19))
    rec.set_watch({i.scrip_code: "opt" for i in cat.options_by_symbol["PNB"]})
    far_ce = "PNB1027129CE"
    far_pe = "PNB1027111PE"
    for code, oi in ((far_ce, 9e6), (far_pe, 8e6), ("PNB1027121CE", 1e5)):
        rec.on_frame("oi", {"Token": code, "OpenInterest": oi}, t)
    await e._rec_sync(_at("2026-10-19", "09:45:03"))
    assert far_ce in feed.record["mf"] and far_pe in feed.record["mf"]
    assert far_ce not in feed.record["oi"], "a wall candidate is a price the band lacks; its OI is the engine's band"
    n = len(feed.record["mf"])
    e.ltps["PNBEQ"] = 120.0
    await e._rec_sync(_at("2026-10-19", "09:50:00"))
    assert len(feed.record["mf"]) == n


def test_the_band_constant_and_expiry_rule_match_the_design():
    from kotsin_nse.config import Settings
    s = Settings(_env_file=None)
    assert (s.record5_band_strikes, s.record5_max_extra_mf, s.record5_max_extra_oi, s.record_band, s.record5_enabled) == (3, 2000, 1000, False, True)


async def test_the_recorders_day_is_seeded_from_the_engines_own_archive(settings):
    """At boot (and each day): what the recorder watches (every option of the legs' expiry, the front future, the
    cash code), the previous session's last OI print per option, the previous session's 30m volume bars, and the two
    previous sessions' OI at the grid the walls' oiChangePct reads — from the engine's own archive."""
    import pandas as pd

    e, _feed, _cat = _engine_with_band(settings, record_band=False)
    arch = settings.data_dir / "archive"
    (arch / "oi").mkdir(parents=True)
    (arch / "option_volume_30m").mkdir(parents=True)
    code = "PNB1027124CE"
    pd.DataFrame({"scrip_code": [code, code, "PNBF0"], "ts": [R.at_ist("2026-10-16", "10:00"), R.at_ist("2026-10-16", "15:29"),
                                                         R.at_ist("2026-10-16", "15:29")], "oi": [1000.0, 1500.0, 9.0],
                  "change_pct": [None, None, None]}).to_parquet(arch / "oi" / "2026-10-16.parquet", index=False)
    pd.DataFrame({"scrip_code": [code], "ts": [R.at_ist("2026-10-15", "15:00")], "oi": [800.0], "change_pct": [None]}).to_parquet(
        arch / "oi" / "2026-10-15.parquet", index=False)
    pd.DataFrame({"scrip_code": [code], "bucket_ts": [int(R.at_ist("2026-10-16", "14:45"))], "v": [12_000.0], "verified": [True],
                  "from_ts": [R.at_ist("2026-10-16", "09:07")]}).to_parquet(arch / "option_volume_30m" / "2026-10-16.parquet", index=False)
    await e._rec_prepare(date(2026, 10, 19))
    rec = e.recorder
    assert rec is not None and rec.day == date(2026, 10, 19)
    assert rec.watch[code] == "opt" and rec.watch["PNBF0"] == "fut" and rec.watch["PNBEQ"] == "eq" and "CRUDE" not in rec.watch
    assert rec.oi_prev == {code: 1500.0}, "the previous session's LAST print, options only"
    assert rec.bar30(code, "2026-10-16", "14:45", R.at_ist("2026-10-19", "10:00")) == (12_000.0, "")
    g16, g15 = rec.oi_grid_prev[(code, "2026-10-16")], rec.oi_grid_prev[(code, "2026-10-15")]
    assert g16[0] != g16[0] and g16[1] != g16[1] and g16[2] == 1000.0 and g16[-1] == 1500.0 and g15[-1] == 800.0
