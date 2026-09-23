"""Config closure, mode-as-state, and an end-to-end paper trade through the real engine."""

from __future__ import annotations

import time

import pytest

from kotsin_nse.config import Segment, Settings, UnknownConfigKeys, assert_no_unknown_env
from kotsin_nse.domain import Direction, OptionType
from kotsin_nse.engine import Engine
from kotsin_nse.exec.gateway import Mode
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.instrument.select import (
    Quote,
    estimate_delta,
    map_levels_to_option,
    otm_strike_anchor,
    qty_for_budget,
    select_option,
)
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.keys import StrategyKey

from .conftest import ist_ts

# -- config ----------------------------------------------------------------------------------------


def test_unknown_env_key_fails_at_boot():
    """``KN_DELTA_ENVIRONMENT=mainnet`` (typo) must not silently run on the default. pydantic
    ignores unknown *process* env vars by default, which is the wrong trade-off for a namespace
    we own."""
    with pytest.raises(UnknownConfigKeys) as exc:
        assert_no_unknown_env({"KN_SEGMENT": "NSE_EQ"})  # the field is KN_SEGMENTS
    assert "KN_SEGMENT" in str(exc.value)
    assert_no_unknown_env({"KN_SEGMENTS": "NSE_EQ", "PATH": "/bin"})


def test_unknown_dotenv_key_fails_validation(tmp_path):
    env = tmp_path / ".env"
    env.write_text("KN_NOT_A_REAL_KEY=1\n")
    with pytest.raises(Exception):
        Settings(_env_file=str(env))


def test_bad_segment_is_rejected_with_the_known_list():
    with pytest.raises(Exception) as exc:
        Settings(_env_file=None, segments="NSE_CASH")
    assert "NSE_EQ" in str(exc.value)


def test_segment_wire_identity():
    assert (Segment.NSE_EQ.exch, Segment.NSE_EQ.exch_type) == ("N", "C")
    assert (Segment.NSE_FO.exch, Segment.NSE_FO.exch_type) == ("N", "D")
    assert (Segment.MCX_FO.exch, Segment.MCX_FO.exch_type) == ("M", "D")
    assert Segment.MCX_FO.scripmaster_key == "mcx_fo"


def test_credentials_are_absent_by_default():
    """The old repos committed all six credentials plus the TOTP seed in three source files."""
    s = Settings(_env_file=None)
    assert s.has_credentials is False
    assert s.fp_totp_secret is None


def test_secrets_do_not_appear_in_the_repr():
    s = Settings(_env_file=None, fp_pin="1234", fp_totp_secret="ABCD")
    assert "1234" not in repr(s)
    assert "ABCD" not in repr(s)


# -- instrument selection -----------------------------------------------------------------------------


def test_otm_anchor_is_t1_so_the_option_is_atm_at_target():
    assert otm_strike_anchor(spot=1500, target1=1560, direction=Direction.BULLISH) == 1560
    # A target on the wrong side means stale confluence: fall back to ATM rather than pick nonsense.
    assert otm_strike_anchor(spot=1500, target1=1400, direction=Direction.BULLISH) == 1500
    assert otm_strike_anchor(spot=1500, target1=None, direction=Direction.BULLISH) == 1500


def test_select_option_picks_an_otm_strike_near_the_anchor(option):
    from dataclasses import replace

    chain = [replace(option, scrip_code=str(k), strike=float(k)) for k in (1500, 1550, 1600, 1650)]
    now = time.time()
    quotes = {i.scrip_code: Quote(ltp=40.0, bid=39.5, ask=40.5, ts=now) for i in chain}
    sel = select_option(chain=chain, quotes=quotes, spot=1500.0, target1=1560.0,
                        direction=Direction.BULLISH, now=now)
    assert sel.ok
    assert sel.instrument.strike == 1550.0  # nearest OTM strike to the T1 anchor
    assert sel.anchor == 1560.0


def test_select_option_refuses_an_illiquid_or_stale_strike(option):
    from dataclasses import replace

    chain = [replace(option, scrip_code="a", strike=1550.0)]
    now = time.time()
    wide = {"a": Quote(ltp=40.0, bid=20.0, ask=60.0, ts=now)}
    assert not select_option(chain=chain, quotes=wide, spot=1500.0, target1=1560.0,
                             direction=Direction.BULLISH, now=now).ok
    stale = {"a": Quote(ltp=40.0, bid=39.5, ask=40.5, ts=now - 600)}
    sel = select_option(chain=chain, quotes=stale, spot=1500.0, target1=1560.0,
                        direction=Direction.BULLISH, now=now)
    assert not sel.ok and "stale" in sel.reason


def test_levels_map_onto_the_option_monotonically():
    stop, targets = map_levels_to_option(
        equity_entry=1500.0, equity_stop=1470.0, equity_targets=(1560.0, 1600.0),
        option_premium=40.0, delta=0.4,
    )
    assert stop == 40.0 - 30 * 0.4
    assert targets == (40.0 + 60 * 0.4, 40.0 + 100 * 0.4)
    assert list(targets) == sorted(targets)


def test_option_stop_never_goes_to_or_below_zero():
    stop, _ = map_levels_to_option(
        equity_entry=1500.0, equity_stop=1000.0, equity_targets=(), option_premium=5.0, delta=1.0
    )
    assert stop > 0


def test_delta_estimate_decays_away_from_the_money():
    atm = estimate_delta(spot=1500, strike=1500, option_type=OptionType.CE)
    otm = estimate_delta(spot=1500, strike=1700, option_type=OptionType.CE)
    itm = estimate_delta(spot=1500, strike=1300, option_type=OptionType.CE)
    assert itm > atm > otm
    assert 0 < otm < 1 and 0 < itm <= 1


def test_qty_for_budget_uses_lots_and_the_multiplier(option, mcx_future, equity):
    assert qty_for_budget(option, 50.0, 30_000, max_lots=None) == 500  # 2 lots of 250
    assert qty_for_budget(option, 50.0, 30_000, max_lots=1) == 250
    # ALUMINI: 349.45 × 1 × 1000 = ₹349,450 per lot, so ₹100,000 buys nothing.
    assert qty_for_budget(mcx_future, 349.45, 100_000, max_lots=4) == 0
    assert qty_for_budget(equity, 330.0, 33_000, max_lots=None) == 100


# -- engine -------------------------------------------------------------------------------------------


async def test_engine_boots_without_credentials_and_says_why(settings):
    e = Engine(settings)
    await e.start()
    try:
        assert e.mode() is Mode.SHADOW
        assert any("credentials" in n for n in e.boot_notes)
        assert set(e.wallets) == {"FUDKII", "FUKAA", "FUDKII_RT_X", "FUDKII_RT_MCX", "FUDKII_RT_N", "FUDKII_RT_Y", "FUDKII_CT_X", "FUDKII_CT_Y"}
        assert e.wallets["FUDKII"].balance == settings.paper_initial_inr
    finally:
        await e.stop()


async def test_live_mode_requires_explicit_arming(settings):
    e = Engine(settings)
    await e.start()
    try:
        with pytest.raises(ValueError):
            await e.set_mode(Mode.LIVE_CAPPED)
        await e.set_mode(Mode.LIVE_CAPPED, armed_minutes=30)
        assert e.mode() is Mode.LIVE_CAPPED
    finally:
        await e.stop()


async def test_expired_arming_falls_back_to_paper_on_restart(settings):
    """A restart after the arming window must not silently stay live. CAN2 ran paper for eight
    weeks because a restart dropped an env var — this is the opposite failure, guarded."""
    e = Engine(settings)
    await e.start()
    await e.ledger.set_mode("LIVE_CAPPED", time.time() - 60)
    await e.stop()

    e2 = Engine(settings)
    await e2.start()
    try:
        assert e2.mode() is Mode.PAPER
        assert any("arming expired" in n for n in e2.boot_notes)
    finally:
        await e2.stop()


async def test_expired_arming_downgrades_even_without_a_restart(settings):
    e = Engine(settings)
    await e.start()
    try:
        await e.set_mode(Mode.LIVE_CAPPED, armed_minutes=30)
        e._armed_until = time.time() - 1
        assert e.mode() is Mode.PAPER
    finally:
        await e.stop()


async def test_halt_and_reconcile_freeze_both_stop_entries(settings):
    e = Engine(settings)
    await e.start()
    try:
        assert e.halted() == (False, "")
        await e.set_halt(True, "manual test")
        assert e.halted()[0] is True
        await e.set_halt(False)
        e.gateway.breaker_tripped = True
        assert "breaker" in e.halted()[1]
    finally:
        await e.stop()


async def test_end_to_end_paper_trade(settings, monkeypatch):
    """A FUDKII signal becomes a sized, filled, recorded position — through the real gateway,
    the real cost model and the real ledger."""
    from dataclasses import replace

    from .test_strategies import _breakout_series, _default_zones

    e = Engine(settings)
    await e.start()
    try:
        await e.set_mode(Mode.PAPER)
        bars = _breakout_series()
        for b in bars:
            b.volume, b.oi, b.oi_change_pct = 1000.0, 1_000_000, 200.0
        bars[-1].volume = 9000.0
        e.store.seed("RELIANCE", "30m", bars)

        from kotsin_nse.domain import Instrument, InstrumentKind

        underlying = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY,
                                underlying="RELIANCE")
        e.underlyings = {"RELIANCE": underlying}
        e.ltps["2885"] = bars[-1].close
        monkeypatch.setattr(e, "zones_for", lambda _s: _default_zones(bars[-1].close))

        chain_option = Instrument(
            "45678", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION,
            name="RELIANCE 25 SEP 2026 CE 120", lot_size=250, multiplier=1,
            expiry="2026-12-25", strike=bars[-1].close * 1.04, option_type=OptionType.CE,
            underlying="RELIANCE",
        )
        now = time.time()
        e.quotes[chain_option.scrip_code] = Quote(ltp=40.0, bid=39.5, ask=40.5, ts=now)
        e.ltps[chain_option.scrip_code] = 40.0
        e.books[chain_option.scrip_code] = BookSnapshot(
            chain_option.scrip_code, bids=[(39.5, 5000)], asks=[(40.5, 5000)], ts=now
        )

        class FakeSelection:
            ok, instrument, premium, reason, anchor, spread_pct = (
                True, chain_option, 40.0, "ok", 0.0, 2.5,
            )

        async def fake_select(_u, _s):
            return FakeSelection()

        monkeypatch.setattr(e, "_select_instrument", fake_select)

        await e._decide(bars[-1])

        assert e.positions, "expected a position"
        pos = next(iter(e.positions.values()))
        assert pos.strategy in ("FUDKII", "FUKAA")
        assert pos.qty % 250 == 0
        assert pos.option_sl < pos.entry
        assert pos.equity_sl < pos.equity_entry
        assert e.wallets[pos.strategy].deployed > 0

        counts = await e.ledger.counts()
        assert counts["signals"] >= 1
        assert counts["orders"] >= 1
        assert counts["positions"] >= 1
        assert replace  # imported for clarity in this test's setup
    finally:
        await e.stop()


async def test_signals_and_rejections_are_both_persisted(settings, monkeypatch):
    from .test_strategies import _breakout_series

    e = Engine(settings)
    await e.start()
    try:
        quiet = [*_breakout_series()[:-1], _breakout_series()[-2]]
        e.store.seed("RELIANCE", "30m", quiet)
        monkeypatch.setattr(e, "zones_for", lambda _s: [])
        await e._decide(quiet[-1])
        counts = await e.ledger.counts()
        assert counts["rejections"] >= 1
        hist = await e.ledger.gate_histogram()
        assert hist and hist[0]["n"] >= 1
    finally:
        await e.stop()


def test_strategy_key_is_the_only_registry():
    assert [k.value for k in StrategyKey] == [
        "FUDKII", "FUKAA", "FUDKII_RT_X", "FUDKII_RT_MCX", "FUDKII_RT_N", "FUDKII_RT_Y", "FUDKII_CT_X", "FUDKII_CT_Y",
    ]
    assert Signal(
        strategy=StrategyKey.FUDKII, symbol="RELIANCE", direction=Direction.BULLISH,
        ts=int(ist_ts("2026-09-18", "11:00")), entry=100.0, stop=98.0,
    ).signal_id.startswith("FUDKII-RELIANCE-")


async def test_a_stale_option_quote_suspends_exit_evaluation(settings, monkeypatch):
    """An illiquid strike can stop ticking. Evaluating a stop against a price from minutes ago is
    worse than not evaluating it — but staleness must never trap a position past the force-flat."""
    from kotsin_nse.domain import (
        Direction,
        Instrument,
        InstrumentKind,
        OptionType,
        Position,
        PosSide,
    )
    from kotsin_nse.instrument.select import Quote

    e = Engine(settings)
    await e.start()
    try:
        opt = Instrument(
            "45678", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION,
            lot_size=250, strike=1500.0, option_type=OptionType.CE, underlying="RELIANCE",
        )
        und = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE")
        pos = Position(
            id="p1", strategy="FUDKII", instrument=opt, underlying=und, side=PosSide.LONG,
            qty=250, entry=50.0, opened_ts=time.time(), signal_id="s1", direction=Direction.BULLISH,
            equity_entry=1500.0, equity_sl=1450.0, option_sl=40.0, option_targets=(70.0,),
        )
        await e.set_mode(Mode.PAPER)  # SHADOW places nothing, so nothing could close
        e.positions[pos.id] = pos
        e.ltps[opt.scrip_code] = 35.0  # below the stop: a live quote would exit immediately

        stale_ts = time.time() - settings.position_quote_max_age_s - 10
        e.quotes[opt.scrip_code] = Quote(ltp=35.0, bid=34.5, ask=35.5, ts=stale_ts)
        monkeypatch.setattr("kotsin_nse.engine.past_force_flat", lambda *_a: False)
        await e._manage_positions()
        assert pos.status == "OPEN", "a stale quote must not trigger an exit"
        assert pos.id in e._stale_positions
        assert e.health_snapshot()["positions_stale_quote"] == 1

        # The force-flat overrides staleness: never trap a position at the end of the session.
        monkeypatch.setattr("kotsin_nse.engine.past_force_flat", lambda *_a: True)
        await e._manage_positions()
        assert pos.status != "OPEN" or pos.id not in e.positions
    finally:
        await e.stop()


async def test_a_fresh_quote_is_evaluated_normally(settings, monkeypatch):
    from kotsin_nse.domain import (
        Direction,
        Instrument,
        InstrumentKind,
        OptionType,
        Position,
        PosSide,
    )
    from kotsin_nse.instrument.select import Quote

    e = Engine(settings)
    await e.start()
    try:
        opt = Instrument(
            "45678", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION,
            lot_size=250, strike=1500.0, option_type=OptionType.CE, underlying="RELIANCE",
        )
        und = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE")
        pos = Position(
            id="p2", strategy="FUDKII", instrument=opt, underlying=und, side=PosSide.LONG,
            qty=250, entry=50.0, opened_ts=time.time(), signal_id="s2", direction=Direction.BULLISH,
            equity_entry=1500.0, equity_sl=1450.0, option_sl=40.0, option_targets=(70.0,),
        )
        await e.set_mode(Mode.PAPER)
        e.positions[pos.id] = pos
        e.ltps[opt.scrip_code] = 35.0
        e.quotes[opt.scrip_code] = Quote(ltp=35.0, bid=34.5, ask=35.5, ts=time.time())
        monkeypatch.setattr("kotsin_nse.engine.past_force_flat", lambda *_a: False)
        await e._manage_positions()
        assert pos.id not in e._stale_positions
        assert pos.status != "OPEN" or pos.id not in e.positions, "the stop should have fired"
    finally:
        await e.stop()


async def test_shadow_mode_reports_an_exit_once_instead_of_failing_every_second(settings, monkeypatch, caplog):
    """A position carried into SHADOW can never close, because SHADOW places nothing. That is the
    mode working — it must be said once at info, not logged as an error on every 1 s tick."""
    from kotsin_nse.domain import (
        Direction,
        Instrument,
        InstrumentKind,
        OptionType,
        Position,
        PosSide,
    )
    from kotsin_nse.instrument.select import Quote

    e = Engine(settings)
    await e.start()
    try:
        opt = Instrument(
            "45678", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION,
            lot_size=250, strike=1500.0, option_type=OptionType.CE, underlying="RELIANCE",
        )
        und = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE")
        pos = Position(
            id="p3", strategy="FUDKII", instrument=opt, underlying=und, side=PosSide.LONG,
            qty=250, entry=50.0, opened_ts=time.time(), signal_id="s3", direction=Direction.BULLISH,
            equity_entry=1500.0, equity_sl=1450.0, option_sl=40.0, option_targets=(70.0,),
        )
        e.positions[pos.id] = pos
        e.ltps[opt.scrip_code] = 35.0
        e.quotes[opt.scrip_code] = Quote(ltp=35.0, bid=34.5, ask=35.5, ts=time.time())
        monkeypatch.setattr("kotsin_nse.engine.past_force_flat", lambda *_a: False)

        for _ in range(5):
            await e._manage_positions()
        assert pos.status == "OPEN"
        assert e._shadow_exits == {pos.id}, "reported once, not once per tick"
    finally:
        await e.stop()


async def test_a_wallet_reset_starts_the_purse_over_unless_the_book_is_in_a_trade(settings):
    from kotsin_nse.config import Segment
    from kotsin_nse.domain import Direction, Instrument, InstrumentKind, Position, PosSide

    e = Engine(settings)
    await e.start()
    try:
        w = e.wallets["FUDKII_RT_X"]
        w.balance, w.realized_pnl, w.trades = 1_008_095.0, 8_353.0, 2
        fresh = await e.reset_wallet("FUDKII_RT_X")
        assert (fresh.initial, fresh.balance, fresh.peak, fresh.realized_pnl, fresh.trades) == (settings.paper_initial_inr,) * 3 + (0.0, 0)
        assert e.wallets["FUDKII_RT_X"] is fresh
        assert (await e.reset_wallet("FUDKII_RT_MCX")).initial == 3_000_000.0, "a book with its own opening capital keeps it"
        assert (await e.reset_wallet("FUDKII", 2_000_000)).balance == 2_000_000.0
        opt = Instrument("1", "X", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=1, underlying="X")
        e.positions["o"] = Position(id="o", strategy="FUDKII", instrument=opt, underlying=opt, side=PosSide.LONG, qty=1,
                                    entry=1.0, opened_ts=1.0, signal_id="s", direction=Direction.BULLISH)
        with pytest.raises(RuntimeError):
            await e.reset_wallet("FUDKII")
        with pytest.raises(KeyError):
            await e.reset_wallet("NOPE")
    finally:
        await e.stop()
