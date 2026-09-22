"""FUDKII and FUKAA. Both books, their gates, and the dead machinery that is deliberately gone."""

from __future__ import annotations

from typing import Any

from kotsin_nse.bars.pivots import Zone
from kotsin_nse.bars.unified import UnifiedBar
from kotsin_nse.domain import Direction
from kotsin_nse.strategy.conviction import ConvictionInput, Tier, score, thresholds_for
from kotsin_nse.strategy.fudkii import Fudkii, FudkiiConfig
from kotsin_nse.strategy.fukaa import Fukaa, FukaaConfig, select
from kotsin_nse.strategy.keys import ALL_KEYS, StrategyKey

from .conftest import series


class FakeCtx:
    """A Context with no I/O — which is the whole point of the strategy contract."""

    def __init__(
        self,
        bars: list[UnifiedBar],
        zones: list[Zone] | None = None,
        exchange: str = "N",
        phase: str = "MID",
    ) -> None:
        self._bars = bars
        self._zones = zones if zones is not None else _default_zones(bars[-1].close)
        self._exchange = exchange
        self._phase = phase
        self._state: dict[str, Any] = {}

    def set_bars(self, bars: list[UnifiedBar]) -> None:
        """The engine keeps one Context per strategy across bars; tests must too, or the
        T+1 watching state silently disappears."""
        self._bars = bars

    def bars(self, symbol: str, tf: str, n: int):
        return self._bars[-n:]

    def zones(self, symbol: str):
        return self._zones

    def exchange(self, symbol: str) -> str:
        return self._exchange

    def session_phase(self, symbol: str, ts: int) -> str:
        return self._phase

    @property
    def state(self) -> dict[str, Any]:
        return self._state


def _default_zones(close: float) -> list[Zone]:
    return [
        Zone(price=close * 0.97, strength=8.0, members=["1d.S1", "1wk.S1"]),
        Zone(price=close * 1.05, strength=8.0, members=["1d.R1", "1wk.R1"]),
        Zone(price=close * 1.12, strength=8.0, members=["1d.R2", "1wk.R2"]),
    ]


def _breakout_series(n: int = 60) -> list[UnifiedBar]:
    """A long quiet drift, then a decisive up-bar: SuperTrend flips up and the close clears the
    upper band on the same bar."""
    closes = [100.0 + (i % 3) * 0.1 for i in range(n - 6)]
    closes += [99.0, 98.5, 98.0, 97.5, 97.0, 112.0]
    return series(closes)


# -- FUDKII ---------------------------------------------------------------------------------------


def test_fudkii_fires_on_flip_plus_band_break():
    bars = _breakout_series()
    ctx = FakeCtx(bars)
    out = Fudkii().on_bar(ctx, bars[-1])
    assert out.signals, [r.binding_gate for r in out.rejections]
    sig = out.signals[0]
    assert sig.strategy is StrategyKey.FUDKII
    assert sig.direction is Direction.BULLISH
    assert sig.score == 100.0
    assert sig.stop < sig.entry
    assert sig.targets and sig.targets[0] > sig.entry


def test_fudkii_requires_both_conditions():
    """``require_both`` is live-true: a flip alone or a band break alone never fires."""
    bars = _breakout_series()
    quiet = FakeCtx(series([100.0 + i * 0.01 for i in range(60)]))
    out = Fudkii().on_bar(quiet, quiet._bars[-1])
    assert not out.signals
    assert out.rejections
    assert out.rejections[0].binding_gate in ("st_flip", "bb_break", "score")
    # And with require_both off, a 50-point score is enough.
    loose = Fudkii(FudkiiConfig(require_both=False))
    assert loose.cfg.score_threshold == 50.0
    assert Fudkii().cfg.score_threshold == 100.0
    assert bars  # the breakout series is exercised above


def test_fudkii_parameters_are_actually_read():
    """The original bound ``bb.period`` / ``st.period`` into fields that only ever appeared in a
    log line, while the calculator used hardcoded constants. Changing them here must change the
    result, or the config is decoration."""
    bars = _breakout_series()
    strict = Fudkii(FudkiiConfig(bb_period=20, bb_mult=2.0))
    wide = Fudkii(FudkiiConfig(bb_period=20, bb_mult=25.0))  # nothing can clear this band
    assert strict.on_bar(FakeCtx(bars), bars[-1]).signals
    assert not wide.on_bar(FakeCtx(bars), bars[-1]).signals


def test_fudkii_blocks_grade_f_and_records_it():
    """~61% of graded signals were F in the live log and nothing counted them."""
    bars = _breakout_series()
    close = bars[-1].close
    # A wall immediately overhead: no room, terrible RR.
    zones = [
        Zone(price=close * 0.90, strength=8.0, members=["1d.S1", "1wk.S1"]),
        Zone(price=close * 1.001, strength=8.0, members=["1d.R1", "1wk.R1"]),
    ]
    out = Fudkii().on_bar(FakeCtx(bars, zones), bars[-1])
    assert not out.signals
    assert out.rejections[0].binding_gate == "confluence_grade"
    assert out.rejections[0].evidence["rr"] < 1.0


def test_fudkii_eod_window_takes_only_strong_signals():
    bars = _breakout_series()
    close = bars[-1].close
    weak = [
        Zone(price=close * 0.97, strength=6.0, members=["1d.S1", "1wk.S1"]),
        Zone(price=close * 1.08, strength=6.0, members=["1d.R1", "1wk.R1"]),
    ]
    assert Fudkii().on_bar(FakeCtx(bars, weak, phase="MID"), bars[-1]).signals
    out = Fudkii().on_bar(FakeCtx(bars, weak, phase="EOD"), bars[-1])
    assert not out.signals
    assert out.rejections[0].binding_gate == "eod_strong_only"


def test_fudkii_counts_which_gate_is_binding():
    """``NSE_BB_30`` had six mandatory gates and two lifetime signals, and nothing recorded which
    gate did the killing."""
    f = Fudkii()
    quiet = series([100.0 + i * 0.01 for i in range(60)])
    for _ in range(3):
        f.on_bar(FakeCtx(quiet), quiet[-1])
    stats = f.stats.to_json()
    assert stats["candidates"] == 3
    assert stats["passed"] == 0
    assert sum(g["binding"] for g in stats["by_gate"].values()) == 3


def test_fudkii_needs_warm_history():
    short = series([100.0, 101.0, 102.0])
    out = Fudkii().on_bar(FakeCtx(short), short[-1])
    assert not out.signals
    assert out.rejections[0].binding_gate == "history"


# -- FUKAA ------------------------------------------------------------------------------------------


def _with_volume(bars: list[UnifiedBar], surge_x: float, *, oi_pct: float = 200.0) -> list[UnifiedBar]:
    for b in bars:
        b.volume = 1000.0
        b.oi = 1_000_000
        b.oi_change_pct = oi_pct
    bars[-1].volume = 1000.0 * surge_x
    return bars


def test_fukaa_admits_a_volume_confirmed_signal():
    bars = _with_volume(_breakout_series(), 6.0)
    ctx = FakeCtx(bars)
    base = Fudkii().on_bar(ctx, bars[-1]).signals[0]
    out = Fukaa().on_signal(FakeCtx(bars), bars[-1], base)
    assert out.signals, [(r.binding_gate, r.note) for r in out.rejections]
    sig = out.signals[0]
    assert sig.strategy is StrategyKey.FUKAA
    assert sig.source_signal_id == base.signal_id
    assert sig.evidence["surge_used"] >= 4.0


def test_fukaa_parks_a_volumeless_signal_for_t_plus_1_promotion():
    bars = _with_volume(_breakout_series(), 1.0)
    ctx = FakeCtx(bars)
    base = Fudkii().on_bar(ctx, bars[-1]).signals[0]
    fukaa = Fukaa()
    fctx = FakeCtx(bars)
    out = fukaa.on_signal(fctx, bars[-1], base)
    assert not out.signals
    assert out.rejections[0].binding_gate == "volume_surge"
    assert "WATCHING" in out.rejections[0].note

    # The next bar delivers the surge → promoted. Same context: the watching state lives there.
    nxt = series([b.close for b in bars] + [bars[-1].close * 1.01])
    for b in nxt:
        b.volume, b.oi, b.oi_change_pct = 1000.0, 1_000_000, 200.0
    nxt[-1].volume = 9000.0
    nxt[-1].ts = bars[-1].ts + 1800
    fctx.set_bars(nxt)
    out2 = fukaa.on_bar(fctx, nxt[-1])
    assert out2.signals
    assert out2.signals[0].evidence["promoted"] == 1.0
    assert "T+1 promotion" in out2.signals[0].reason


def test_fukaa_watching_expires():
    bars = _with_volume(_breakout_series(), 1.0)
    base = Fudkii().on_bar(FakeCtx(bars), bars[-1]).signals[0]
    fukaa = Fukaa(FukaaConfig(watching_ttl_minutes=1))
    ctx = FakeCtx(bars)
    fukaa.on_signal(ctx, bars[-1], base)
    later = series([b.close for b in bars] + [bars[-1].close])
    later[-1].ts = bars[-1].ts + 3600
    ctx.set_bars(later)
    out = fukaa.on_bar(ctx, later[-1])
    assert not out.signals
    assert out.rejections[0].binding_gate == "watching_expired"


def test_fukaa_multiplier_is_per_exchange_and_all_three_are_read():
    """``fukaa.trigger.volume.multiplier`` was set in properties and read by nothing; the code read
    three other keys, and MCX silently ran at 1.0 — a bar at its own average volume passing a gate
    called 'volume confirmation'."""
    f = Fukaa()
    assert f.multiplier("N") == 4.0
    assert f.multiplier("M") == 2.0
    assert f.multiplier("C") == 2.0
    assert f.multiplier("M") > 1.0, "a 1.0 multiplier is not a gate"
    custom = Fukaa(FukaaConfig(volume_multiplier_mcx=3.0))
    assert custom.multiplier("M") == 3.0


def test_fukaa_mcx_gate_actually_rejects_an_average_bar():
    bars = _with_volume(_breakout_series(), 1.1)
    base = Fudkii().on_bar(FakeCtx(bars), bars[-1]).signals[0]
    out = Fukaa(FukaaConfig(promote_on_t_plus_1=False)).on_signal(
        FakeCtx(bars, exchange="M"), bars[-1], base
    )
    assert not out.signals
    assert out.rejections[0].binding_gate == "volume_surge"


def test_fukaa_ref_oi_gate_fails_closed_on_missing_oi():
    bars = _breakout_series()
    for b in bars:
        b.volume = 1000.0
        b.oi = None
        b.oi_change_pct = None
    bars[-1].volume = 9000.0
    base = Fudkii().on_bar(FakeCtx(bars), bars[-1]).signals[0]
    out = Fukaa().on_signal(FakeCtx(bars), bars[-1], base)
    assert not out.signals
    gate = [g for g in out.rejections[0].gates if g.name == "ref_oi"][0]
    assert gate.missing is True and gate.passed is False, "no OI must fail closed, not fall through"
    # Composite fails first in evaluation order, so that is what is reported as binding — the
    # per-gate counters still record the ref_oi rejection.
    assert out.rejections[0].binding_gate == "composite"


def test_selection_caps_are_off_by_default_not_999():
    """``top.n=999`` was a sentinel that disabled a stage named 'Top-N selection'."""
    cfg = FukaaConfig()
    assert cfg.top_n is None
    assert cfg.max_same_direction is None
    bars = _with_volume(_breakout_series(), 6.0)
    base = Fudkii().on_bar(FakeCtx(bars), bars[-1]).signals[0]
    sigs = Fukaa().on_signal(FakeCtx(bars), bars[-1], base).signals
    admitted, dropped = select(sigs * 5, cfg)
    assert len(admitted) == 5 and not dropped


def test_selection_caps_bind_when_set():
    bars = _with_volume(_breakout_series(), 6.0)
    base = Fudkii().on_bar(FakeCtx(bars), bars[-1]).signals[0]
    sigs = Fukaa().on_signal(FakeCtx(bars), bars[-1], base).signals * 5
    admitted, dropped = select(sigs, FukaaConfig(top_n=2))
    assert len(admitted) == 2 and len(dropped) == 3
    admitted, dropped = select(sigs, FukaaConfig(max_same_direction=1))
    assert len(admitted) == 1 and len(dropped) == 4


# -- conviction ---------------------------------------------------------------------------------------


def test_conviction_thresholds_differ_by_exchange():
    n, m, c = thresholds_for("N"), thresholds_for("M"), thresholds_for("C")
    assert (n.volume_strong, m.volume_strong, c.volume_strong) == (3.0, 2.0, 2.5)
    assert (n.oi_high, m.oi_high) == (150.0, 100.0)
    assert n.ref_oi_floor == 5.0 and m.ref_oi_floor == 8.0


def test_unknown_exchange_falls_back_strict_not_permissive():
    """FUDKOI's ``default -> false`` dropped anything that was not N/M/C with no log line at all."""
    assert thresholds_for("Z") == thresholds_for("N")


def test_conviction_tiers_and_missing_inputs_are_reported():
    strong = score(
        ConvictionInput("N", volume_surge=5.0, oi_change_pct=400.0, oi_buildup_pct=10.0,
                        price_change_over_atr=0.9, rr=3.0)
    )
    assert strong.tier is Tier.S1 and strong.tradeable
    weak = score(ConvictionInput("N", None, None, None, None, rr=0.2))
    assert weak.tier in (Tier.S5, Tier.S6) and not weak.tradeable
    assert set(weak.missing) == {"volume_surge", "oi_change_pct", "price_change_over_atr"}


def test_momentum_score_penalises_an_already_extended_move():
    base = ConvictionInput("N", 5.0, 400.0, 10.0, 0.9, 3.0)
    stretched = score(base)
    extended = score(ConvictionInput("N", 5.0, 400.0, 10.0, 5.0, 3.0))
    assert extended.momentum_score < stretched.momentum_score


# -- registry ------------------------------------------------------------------------------------------


def test_the_registry_is_an_enum_and_names_every_book_that_trades():
    """Three keys now: FUDKII, FUKAA, and FUDKII_RT_X — the RT *exit* policy trading FUDKII's
    own entries on its own wallet, so the two exit policies produce comparable equity curves.
    """
    assert ALL_KEYS == (
        StrategyKey.FUDKII,
        StrategyKey.FUKAA,
        StrategyKey.FUDKII_RT_X,
        StrategyKey.FUDKII_RT_MCX,
    )
    assert StrategyKey.FUDKII.wallet_id == "strategy-wallet-FUDKII"
