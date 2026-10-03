"""Stage 0 of the data-pipeline plan (2026-10-03): the plumbing defects that were costing the paper
books, each pinned by the scenario that showed it."""

from __future__ import annotations

from dataclasses import replace

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.market.iv import bs_price
from kotsin_nse.risk.exits import ExitEngine
from kotsin_nse.risk.limits import RT_MCX_LIMITS, RT_X_LIMITS

from .test_exits_rt import _own, _pos, _view

# -- the option stop re-projection ------------------------------------------------------------------


def _path(e: ExitEngine, pos, spots: list[float], *, vol: float = 0.30, years: float = 21 / 365):
    """Walk the stock along ``spots`` with the option priced off one volatility, re-projecting every
    step; returns [(spot, mid, option_sl)]."""
    out = []
    for k, s in enumerate(spots):
        mid = round(bs_price(s, pos.instrument.strike, years, vol, call=True), 2)
        e._reproject_stop(pos, _view(option_ltp=mid, option_mid=mid, underlying_ltp=s, now=2_000.0 + 15 * k))
        out.append((s, mid, pos.option_sl))
    return out


def test_an_adverse_move_does_not_raise_the_option_stop_toward_the_entry():
    """CE 1010, stock 1000 → its 985 stop. The old line (entry premium − entry distance × the delta
    at TODAY's spot) rose as the delta fell: it crossed the option's own price at 988 and stopped the
    option out three points before the stock reached its stop."""
    e = ExitEngine(replace(RT_X_LIMITS, min_stop_ticks=0))
    entry_mid = round(bs_price(1000.0, 1010.0, 21 / 365, 0.30, call=True), 2)
    pos = _own(entry=entry_mid, equity_entry=1000.0, equity_sl=985.0, option_sl=0.0)
    steps = _path(e, pos, [1000.0, 997.0, 994.0, 991.0, 988.0, 986.0])
    first = steps[0][2]
    for s, mid, sl in steps:
        assert sl == first, f"the delta-line stop moved with the spot ({s}): {sl} != {first}"
        assert mid > sl, f"the option was stopped at {s}, before the stock reached its 985 stop"


def test_a_rally_does_not_move_the_delta_line_stop():
    e = ExitEngine(replace(RT_X_LIMITS, min_stop_ticks=0))
    entry_mid = round(bs_price(1000.0, 1010.0, 21 / 365, 0.30, call=True), 2)
    pos = _own(entry=entry_mid, equity_entry=1000.0, equity_sl=985.0, option_sl=0.0)
    (_, _, before), (_, _, after) = _path(e, pos, [1000.0, 1035.0])
    assert after == before, "a favourable move must neither loosen nor tighten it — only the ratchet does"


def test_the_option_stop_still_fires_on_what_only_the_option_suffers():
    """Held at the fill's level, the line does not follow the option's own price down: a volatility
    crush with the stock unmoved still reaches it."""
    e = ExitEngine(RT_X_LIMITS)
    pos = _pos()  # 20.00 paid, the line at 17.00
    e._reproject_stop(pos, _view(option_mid=12.0, option_ltp=12.0, underlying_ltp=1000.0, now=5_000.0))
    assert pos.option_sl == 17.0


def test_a_stale_quote_does_not_reprice_a_priced_stop():
    e = ExitEngine(replace(RT_X_LIMITS, priced_option_stop=True))
    pos = _pos(option_sl=17.0)
    e._reproject_stop(pos, _view(option_mid=12.0, option_ltp=12.0, underlying_ltp=1000.0, quote_ok=False))
    assert pos.option_sl == 17.0


def test_a_future_keeps_its_own_stop_and_is_never_reprojected_at_half_the_distance():
    """MCX books trade the future: delta 1 at the fill. estimate_delta's 0.5 for a strike-less
    contract put a 5600 entry's 5560 stop at 5580."""
    fut = Instrument("477176", "CRUDEOIL", Segment.MCX_FO, InstrumentKind.FUTURE, lot_size=100, tick_size=1.0,
                     expiry="2026-10-19", underlying="CRUDEOIL")
    pos = _pos(instrument=fut, underlying=fut, entry=5600.0, equity_entry=5600.0, equity_sl=5560.0, option_sl=5560.0,
               option_targets=(5650.0,), strategy="FUDKII_RT_MCX")
    e = ExitEngine(RT_MCX_LIMITS)
    e._reproject_stop(pos, _view(option_ltp=5590.0, option_mid=5590.0, underlying_ltp=5590.0, now=9_000.0))
    assert pos.option_sl == 5560.0


# -- the held-quote refresh, the depth cap, the card's slot volume, the decided set --------------


@pytest.mark.asyncio
async def test_the_held_quote_refresh_runs_with_the_real_session_check(settings, equity, option, monkeypatch):
    """``is_open`` was called without its calendar: TypeError on every pass with a position open,
    swallowed as held_quotes.failed — no held quote was ever refreshed. Unpatched here."""
    import time as _time
    from datetime import datetime

    from kotsin_nse.domain import Direction, Position, PosSide
    from kotsin_nse.engine import Engine
    from kotsin_nse.market.session import IST

    e = Engine(settings)
    e.positions["p"] = Position(id="p", strategy="FUDKII_RT_X", instrument=option, underlying=equity, side=PosSide.LONG,
                                qty=250, entry=10.0, opened_ts=_time.time(), signal_id="s", direction=Direction.BULLISH)
    asked: list[str] = []

    async def market_feed(insts):
        asked.extend(i.scrip_code for i in insts)
        return {i.scrip_code: {"ltp": 9.9, "bid": 0.0, "ask": 0.0, "bid_qty": 0, "ask_qty": 0, "ts": _time.time()} for i in insts}

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(e.rest, "market_feed", market_feed)
    monkeypatch.setattr(e.feed, "subscribe", nothing)
    tuesday_1100 = datetime(2026, 10, 6, 11, 0, tzinfo=IST).timestamp()
    assert await e._refresh_held_quotes(tuesday_1100) == 1 and asked == [option.scrip_code]


@pytest.mark.asyncio
async def test_over_the_depth_cap_a_held_contract_keeps_its_book(settings, equity, option):
    from kotsin_nse.domain import Direction, Position, PosSide
    from kotsin_nse.engine import Engine

    e = Engine(settings.model_copy(update={"depth_max_subscriptions": 2}))
    cat = e.catalogue_loader.catalogue
    held = replace(option, scrip_code="99999")
    for inst in (equity, option, held, replace(option, scrip_code="10"), replace(option, scrip_code="11")):
        cat.by_code[inst.scrip_code] = inst
    subscribed: list[str] = []

    class Feed:
        async def subscribe(self, ch, insts):
            subscribed.extend(i.scrip_code for i in insts)

        async def unsubscribe(self, ch, insts):
            pass

    e.feed = Feed()
    e.positions["p"] = Position(id="p", strategy="FUDKII_RT_X", instrument=held, underlying=equity, side=PosSide.LONG,
                                qty=250, entry=10.0, opened_ts=1.0, signal_id="s", direction=Direction.BULLISH)
    e.depth_wanted = lambda: {"10", "11", "99999"}  # type: ignore[method-assign]
    await e._sync_depth()
    assert "99999" in subscribed and len(subscribed) == 2, f"the held contract lost its book: {subscribed}"


def test_the_card_reads_volume_against_the_same_time_slot():
    from kotsin_nse.alerts.rtcard import same_slot_volume
    from kotsin_nse.bars.unified import UnifiedBar
    from kotsin_nse.market.session import from_ist

    def bar(day: int, hm: str, v: float) -> UnifiedBar:
        from datetime import datetime

        h, m = map(int, hm.split(":"))
        ts = int(from_ist(datetime(2026, 9, day, h, m)))
        return UnifiedBar("X", "1", "30m", ts, 1, 1, 1, 1, v)

    history = [bar(d, "10:15", 100.0) for d in (22, 23, 24)] + [bar(d, "14:45", 900.0) for d in (22, 23, 24)]
    now = bar(25, "10:15", 250.0)
    got = same_slot_volume([*history, now], now)
    assert got is not None and got["ratio"] == 2.5 and got["slot"] == "10:15" and got["sessions"] == 3


def test_the_decided_set_is_written_from_a_copy(settings):
    from kotsin_nse.engine import Engine
    from kotsin_nse.market.session import ist_today

    e = Engine(settings)
    import time as _time

    key = ("RELIANCE", int(_time.time()))
    e._decided.add(key)
    snapshot = list(e._decided)
    e._decided.add(("TCS", key[1]))  # the loop moves on while the thread writes
    e._save_decided(snapshot)
    import json

    saved = json.loads(e._decided_path().read_text())
    assert saved["day"] == ist_today().isoformat() and [tuple(k) for k in saved["keys"]] == [key]


def test_a_cost_key_set_in_the_env_is_reported_as_ignored(settings):
    from kotsin_nse.config import Settings

    s = Settings(**{**settings.model_dump(), "cost_brokerage_per_order_inr": 40.0})
    assert "cost_brokerage_per_order_inr" in s.model_fields_set
