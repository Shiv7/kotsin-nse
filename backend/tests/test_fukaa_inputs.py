"""FUKAA's inputs, fixed (operator, 2026-10-02: "first lets fix the input feed, parameters, values and
ensure it is fresh and accurate"). The audit of its 229 live decisions found the OI change dead (the
broker's field is 0.0 on every frame, scored as a real 0 %), momentum dead (an ATR(14) handed 10 bars),
the volume window shifted by a dropped 15:15 bar, and a T+1 promotion priced at the wrong entry."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import date, datetime
from datetime import time as dt_time

import pandas as pd
import pytest

from kotsin_nse.bars.oi_read import OiReading, oi_quadrant, read_oi, relative_z
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.bars.volume_read import VolumeReading
from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.engine import Engine
from kotsin_nse.instrument.catalogue import Catalogue
from kotsin_nse.market.session import from_ist
from kotsin_nse.strategy.fudkii import Fudkii
from kotsin_nse.strategy.fukaa import Fukaa, FukaaConfig
from tests.test_strategies import FakeCtx, _breakout_series, _with_volume

NOW = 1_790_828_100.0


# -- the OI reading (pure) -------------------------------------------------------------------------

def test_the_current_month_alone_outside_the_roll_and_summed_inside_it():
    levels = {"N": (1_100_000.0, NOW - 30), "X": (300_000.0, NOW - 40)}
    refs = {"N": 1_000_000.0, "X": 100_000.0}
    far = read_oi(["N", "X"], sessions_left=12, levels=levels, refs=refs, now=NOW)
    assert far.ok and far.contracts == ("N",) and far.change_pct == pytest.approx(10.0)
    roll = read_oi(["N", "X"], sessions_left=3, levels=levels, refs=refs, now=NOW)
    # summed: 1.4m against 1.1m — not the mean of +10 % and +200 % (the small next month balloons)
    assert roll.contracts == ("N", "X") and roll.change_pct == pytest.approx((1_400_000 / 1_100_000 - 1) * 100)
    assert roll.age_s == pytest.approx(40.0)


def test_a_reading_the_engine_cannot_vouch_for_is_doubtful_never_zero():
    levels = {"N": (1_000_000.0, NOW - 30)}
    assert read_oi(["N"], sessions_left=12, levels=levels, refs={}, now=NOW).doubt.startswith("no previous-close OI")
    assert read_oi(["N"], sessions_left=12, levels={}, refs={"N": 1.0}, now=NOW).doubt.startswith("no OI print")
    stale = read_oi(["N"], sessions_left=12, levels={"N": (1_000_000.0, NOW - 900)}, refs={"N": 990_000.0}, now=NOW)
    assert not stale.ok and stale.change_pct is None and "old" in stale.doubt
    assert read_oi([], sessions_left=0, levels={}, refs={}, now=NOW).doubt == "no unexpired future"


def test_relative_z_stands_out_in_a_quiet_month_and_needs_enough_peers():
    quiet = [0.1 * ((k % 5) - 2) for k in range(40)]  # every change within ±0.2 %
    z, n = relative_z(1.0, quiet)
    assert n == 40 and z is not None and z > 5, "a +1 % build-up stands out when nobody moves"
    assert relative_z(1.0, quiet[:10]) == (None, 10)
    assert relative_z(1.0, [0.5] * 30) == (None, 30), "no spread, no z"


def test_the_price_oi_quadrant():
    assert oi_quadrant(+1.2, +3.0) == "long build-up"
    assert oi_quadrant(-1.2, +3.0) == "short build-up"
    assert oi_quadrant(+1.2, -3.0) == "short covering"
    assert oi_quadrant(-1.2, -3.0) == "long unwinding"
    assert oi_quadrant(None, 3.0) is None and oi_quadrant(1.2, None) is None and oi_quadrant(0.0, 3.0) is None


# -- the engine's OI levels and references ------------------------------------------------------------

def _fut(code: str, expiry: str) -> Instrument:
    return Instrument(code, f"JSWSTEEL {expiry}", Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=675, expiry=expiry,
                      underlying="JSWSTEEL")


NEAR, NEXT = _fut("48900", "2099-11-26"), _fut("61619", "2099-12-31")


def _engine(settings) -> Engine:
    e = Engine(settings)
    cat = Catalogue()
    cat.futures_by_symbol["JSWSTEEL"] = [NEAR, NEXT]
    e.catalogue_loader.catalogue = cat
    e._future_to_underlying = {NEAR.scrip_code: "JSWSTEEL", NEXT.scrip_code: "JSWSTEEL"}
    e.underlyings["JSWSTEEL"] = Instrument("11723", "JSWSTEEL", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="JSWSTEEL")
    return e


def test_the_reference_is_the_previous_close_from_the_archive_else_the_pre_open_print(settings):
    e = _engine(settings)
    today = date(2026, 10, 5)
    prev = e.calendar.previous_trading_day(today)
    oi_dir = settings.data_dir / "archive" / "oi"
    oi_dir.mkdir(parents=True, exist_ok=True)
    t = lambda d, hm: from_ist(datetime.combine(d, dt_time(*map(int, hm.split(":")))))  # noqa: E731
    pd.DataFrame([{"scrip_code": "48900", "ts": t(prev, "10:00"), "oi": 40_000_000.0, "change_pct": 0.0},
                  {"scrip_code": "48900", "ts": t(prev, "15:29"), "oi": 40_351_500.0, "change_pct": 0.0}]).to_parquet(oi_dir / f"{prev}.parquet")
    pd.DataFrame([{"scrip_code": "61619", "ts": t(today, "09:05"), "oi": 564_300.0, "change_pct": 0.0},
                  {"scrip_code": "61619", "ts": t(today, "10:00"), "oi": 570_000.0, "change_pct": 0.0}]).to_parquet(oi_dir / f"{today}.parquet")
    assert e._seed_oi_reference(today=today) == 2
    assert e._oi_ref == {"48900": 40_351_500.0, "61619": 564_300.0}, "yesterday's last print; else today's pre-open one"


def test_an_engine_running_through_midnight_rolls_its_reference(settings):
    e = _engine(settings)
    d1, d2 = date(2026, 10, 1), date(2026, 10, 5)
    t1 = from_ist(datetime.combine(d1, dt_time(15, 29)))
    e._note_oi("48900", 40_321_800.0, t1)
    e._note_oi("48900", 40_500_000.0, from_ist(datetime.combine(d2, dt_time(9, 16))))
    assert e._oi_ref["48900"] == 40_321_800.0, "the last level of the day before is today's reference"


@pytest.mark.asyncio
async def test_a_stocks_bars_carry_the_front_months_computed_change_never_the_broker_zero(settings, monkeypatch):
    import kotsin_nse.engine as engine_mod

    # a trading day's session: a reading counts only a level received during today's session
    day = date(2026, 10, 6)
    monkeypatch.setattr(engine_mod, "ist_today", lambda: day)
    e = _engine(settings)
    e.aggregator.track(e.underlyings["JSWSTEEL"])
    e._oi_ref = {"48900": 40_000_000.0, "61619": 500_000.0}
    e._oi_ref_day = day
    now = from_ist(datetime.combine(day, dt_time(10, 0)))
    await e._on_oi({"scrip_code": "61619", "open_interest": 600_000, "oi_change_pct": 0.0, "recv_ts": now})
    st = e.aggregator.state["11723"]
    assert st.oi_change_pct is None and st.oi in (None, 0), "the next month never stamps the stock's bars"
    await e._on_oi({"scrip_code": "48900", "open_interest": 40_400_000, "oi_change_pct": 0.0, "recv_ts": now})
    assert st.oi == 40_400_000 and st.oi_change_pct == pytest.approx(1.0), "computed against the previous close, not 0.0"
    r = e.oi_reading("JSWSTEEL", now=now)
    assert r.ok and r.contracts == ("48900",) and r.change_pct == pytest.approx(1.0)


# -- FUKAA on its inputs ------------------------------------------------------------------------------

class _Ctx(FakeCtx):
    def __init__(self, bars, *, volume: VolumeReading | None = None, oi: OiReading | None = None, **kw):
        super().__init__(bars, **kw)
        self._vol, self._oi = volume, oi

    def volume_reading(self, symbol, ts):
        return self._vol if self._vol is not None else super().volume_reading(symbol, ts)

    def oi_reading(self, symbol):
        return self._oi if self._oi is not None else super().oi_reading(symbol)


def _base(bars):
    return Fudkii().on_bar(FakeCtx(bars), bars[-1]).signals[0]


def test_momentum_is_read_on_an_atr_that_has_its_bars():
    bars = _with_volume(_breakout_series(), 6.0)
    assert len(bars) > 15
    out = Fukaa().on_signal(_Ctx(bars), bars[-1], _base(bars))
    ev = (out.signals[0] if out.signals else out.rejections[0]).evidence
    assert ev["momentum_score"] > 0 and ev["atr"] > 0 and "price_over_atr" in ev


def test_a_doubtful_volume_reading_decides_nothing_and_says_why():
    bars = _with_volume(_breakout_series(), 6.0)
    doubt = VolumeReading(doubt="missing bar 29 Sep 15:15", kind="missing")
    out = Fukaa().on_signal(_Ctx(bars, volume=doubt), bars[-1], _base(bars))
    assert not out.signals
    gate = next(g for g in out.rejections[0].gates if g.name == "volume_surge")
    assert gate.missing and not gate.passed and "missing bar 29 Sep 15:15" in gate.note


def test_a_missing_oi_reading_scores_nothing_not_a_real_zero():
    bars = _with_volume(_breakout_series(), 6.0)
    out = Fukaa().on_signal(_Ctx(bars, oi=OiReading(doubt="no previous-close OI for 48900")), bars[-1], _base(bars))
    rej = out.rejections[0]
    assert rej.evidence["oi_score"] == 0.0 and "oi_change_pct" not in rej.evidence
    assert next(g for g in rej.gates if g.name == "ref_oi").missing and "no previous-close OI" in rej.note


def test_a_promotion_is_priced_at_the_bar_it_enters_on_and_never_through_the_stop():
    bars = _with_volume(_breakout_series(), 1.0)
    base = _base(bars)
    fukaa, ctx = Fukaa(), _Ctx(bars)
    fukaa.on_signal(ctx, bars[-1], base)  # parked
    nxt = [replace(b) for b in bars] + [replace(bars[-1], ts=bars[-1].ts + 1800, open=bars[-1].close, close=bars[-1].close * 1.01,
                                                high=bars[-1].close * 1.011, volume=9000.0)]
    ctx.set_bars(nxt)
    sig = fukaa.on_bar(ctx, nxt[-1]).signals[0]
    t1 = base.targets[0]
    assert sig.entry == nxt[-1].close and sig.rr == pytest.approx(abs(t1 - sig.entry) / (sig.entry - base.stop))
    assert sig.rr < base.rr, "entering higher, it has less room than the trigger did"
    # through the stop on T+1: refused
    fukaa2, ctx2 = Fukaa(), _Ctx(bars)
    fukaa2.on_signal(ctx2, bars[-1], base)
    down = [replace(b) for b in bars] + [replace(bars[-1], ts=bars[-1].ts + 1800, close=base.stop - 1.0, low=base.stop - 2.0,
                                                 volume=9000.0)]
    ctx2.set_bars(down)
    out = fukaa2.on_bar(ctx2, down[-1])
    assert not out.signals and out.rejections[0].binding_gate == "stop_side"


@pytest.mark.asyncio
async def test_in_shadow_fukaa_records_its_signal_and_never_trades(settings):
    assert FukaaConfig().shadow is True
    e = Engine(settings)
    await e.ledger.init()
    bars = _with_volume(_breakout_series(), 6.0)
    sig = Fukaa().on_signal(_Ctx(bars), bars[-1], _base(bars)).signals[0]
    await e._fukaa_shadow(sig)
    rows = [r for r in await e.ledger.rows_between("signals", 0, time.time() + 86_400 * 3650) if r["signal_id"] == sig.signal_id]
    assert rows and rows[-1]["decision"] == "SHADOW" and not e.positions
    ev = [r for r in await e.ledger.rows_between("events", 0, time.time() + 60) if r.get("kind") == "fukaa.shadow"]
    assert ev and ev[-1]["signal_id"] == sig.signal_id and "evidence" in ev[-1]


@pytest.mark.asyncio
async def test_the_shadow_record_says_whether_the_market_and_the_oi_were_with_it(settings):
    """Operator, 2026-10-02: "look at direction as well and then decide whether it is aligned with
    the direction of the alert, or its counter-trend". Logged with every shadow signal — never a gate."""
    e = Engine(settings)
    await e.ledger.init()
    bars = _with_volume(_breakout_series(), 6.0)
    sig = Fukaa().on_signal(_Ctx(bars), bars[-1], _base(bars)).signals[0]
    e.store.seed(sig.symbol, "1d", [UnifiedBar(symbol=sig.symbol, scrip_code="0", tf="1d", ts=sig.ts - 3 * 86_400, open=99.0,
                                               high=101.0, low=98.0, close=100.0, volume=1e6, source=BarSource.REST, complete=True)])
    e._breadth_at[sig.source_signal_id] = {"share": 0.31, "names": 180}  # logged at the parent trigger
    await e._fukaa_shadow(sig)
    al = [r for r in await e.ledger.rows_between("events", 0, time.time() + 60) if r.get("kind") == "fukaa.shadow"][-1]["alignment"]
    assert al["breadth"] == 0.31 and al["withMarket"] is False, "a bull alert with 31 % of the market agreeing is counter-trend"
    assert al["priceChangePct"] == pytest.approx((sig.entry / 100.0 - 1) * 100, abs=1e-3)
    assert al["oiQuadrant"] == "long build-up" and al["oiAgrees"] is True
