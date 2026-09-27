"""The FUDKII rescan: every signal of the day, including the ones the live path never saw.

2026-09-25: nothing ran before 10:58, and after the 11:32 restart the live path skipped a handful
of bars the exchange had not confirmed in time. The rescan runs FUDKII on the exchange's own 30m
candles for exactly those bars, records what it finds as MISSED_* with the reason, and leaves every
bar the live path decided alone.

Pinned to a fixed past session so no assertion depends on the wall clock (the book-cards test
failed twice around midnight for exactly that reason).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import ClassVar

import pytest

import kotsin_nse.engine as engine_mod
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.domain import Direction
from kotsin_nse.market.session import IST
from kotsin_nse.strategy.base import Outcome, Signal
from kotsin_nse.strategy.keys import StrategyKey

DAY = date(2026, 9, 24)
T915 = int(datetime(2026, 9, 24, 9, 15, tzinfo=IST).timestamp())
NOW = datetime(2026, 9, 24, 15, 40, tzinfo=IST).timestamp()


def _bar(ts: int, symbol: str = "RELIANCE", source: BarSource = BarSource.REST) -> UnifiedBar:
    return UnifiedBar(symbol=symbol, scrip_code="2885", tf="30m", ts=ts, open=99.0, high=101.0,
                      low=98.5, close=100.0, volume=1e5, source=source, complete=True)


def _fires_on(targets: dict[tuple[str, int], Direction]):
    """A FUDKII stand-in that fires exactly on the given (symbol, bar ts)."""

    class FakeFudkii:
        seen: ClassVar[list[tuple[str, int, int]]] = []

        def __init__(self, cfg=None) -> None:
            self.cfg = cfg

        def on_bar(self, ctx, bar):
            # what the strategy may read: bars up to and including its own, never after
            FakeFudkii.seen.append((bar.symbol, bar.ts, max(b.ts for b in ctx.bars(bar.symbol, "30m", 60))))
            d = targets.get((bar.symbol, bar.ts))
            if d is None:
                return Outcome()
            return Outcome(signals=[Signal(strategy=StrategyKey.FUDKII, symbol=bar.symbol, direction=d, ts=bar.ts,
                                           entry=100.0, stop=101.5 if d is Direction.BEARISH else 98.5,
                                           targets=(97.0,) if d is Direction.BEARISH else (103.0,),
                                           grade="A", reason="ST flip + band break")])

    return FakeFudkii


@pytest.fixture
async def eng(settings, equity, monkeypatch):
    monkeypatch.setattr(engine_mod, "ist_today", lambda: DAY)
    monkeypatch.setattr(engine_mod.time, "time", lambda: NOW)
    e = engine_mod.Engine(settings)
    await e.ledger.init()
    e.underlyings = {"RELIANCE": equity, "TCS": equity}
    e.store.seed("RELIANCE", "30m", [_bar(T915 - 1800 * k) for k in range(60, 0, -1)] + [_bar(T915 + 1800 * k) for k in range(6)])
    e.store.seed("TCS", "30m", [_bar(T915 + 1800 * k, "TCS") for k in range(6)])
    yield e
    await e.ledger.close()


async def test_the_rescan_records_what_the_live_path_never_saw_and_leaves_its_decisions_alone(eng, monkeypatch):
    fake = _fires_on({
        ("RELIANCE", T915): Direction.BEARISH,             # closed while the engine was down
        ("RELIANCE", T915 + 1800 * 4): Direction.BULLISH,  # after going live, decision skipped
        ("TCS", T915 + 1800 * 2): Direction.BEARISH,       # decided live: must NOT be second-guessed
    })
    monkeypatch.setattr(engine_mod, "Fudkii", fake)
    eng._live_from_ts = T915 + 1800 * 3 + 60
    eng._decided = {("TCS", T915 + 1800 * k) for k in range(6)}

    got = await eng.scan_fudkii()
    assert got["signals_found"] == 2 and got["recorded_as_missed"] == 2 and got["rows_added_to_alerts"] == 2
    assert all(seen_ts == bar_ts for _, bar_ts, seen_ts in fake.seen), "no bar after the one being decided"
    assert not any(sym == "TCS" for sym, _, _ in fake.seen), "bars the live path decided are not rescanned"

    rows = await eng.fudkii_today()
    assert [(r["symbol"], r["bar_ts"], r["decision"]) for r in rows] == [
        ("RELIANCE", T915, "MISSED_ENGINE_DOWN"),
        ("RELIANCE", T915 + 1800 * 4, "MISSED_UNCONFIRMED_BAR"),
    ]
    assert rows[0]["fired_ts"] == T915 + 1800, "stamped at its bar's close, not when the rescan ran"
    assert rows[0]["entry"] == 100.0 and rows[0]["targets"] == [97.0] and "not traded" in rows[0]["decision_reason"]

    entries = [a for a in eng.alerts.feed("FUDKII_RT") if a["kind"] == "ENTRY"]
    assert len(entries) == 2 and all(a["evidence"]["replayed"] for a in entries)
    assert all("not traded" in a["card"]["skipped"][0]["reason"] for a in entries)

    again = await eng.scan_fudkii()
    assert again["signals_found"] == 0 and again["recorded_as_missed"] == 0, "a scanned bar is not scanned twice"
    assert len(await eng.fudkii_today()) == 2


async def test_a_live_signal_whose_row_was_lost_comes_back_without_being_called_missed(eng, monkeypatch):
    monkeypatch.setattr(engine_mod, "Fudkii", _fires_on({("RELIANCE", T915 + 1800): Direction.BEARISH}))
    sig = Signal(strategy=StrategyKey.FUDKII, symbol="RELIANCE", direction=Direction.BEARISH, ts=T915 + 1800,
                 entry=100.0, stop=101.5, targets=(97.0,), grade="A")
    await eng.ledger.insert_signal(sig.to_json(), "PAPER_FILLED", "filled", created_ts=T915 + 3600 + 3)

    got = await eng.scan_fudkii()
    assert got["recorded_as_missed"] == 0 and got["rows_added_to_alerts"] == 1
    rows = await eng.fudkii_today()
    assert [r["decision"] for r in rows] == ["PAPER_FILLED"], "the live decision stands"
    entry = next(a for a in eng.alerts.feed("FUDKII_RT") if a["kind"] == "ENTRY")
    assert "skipped" not in (entry["card"] or {}), "it traded; the row must not say otherwise"


async def test_an_unconfirmed_bar_is_asked_for_a_bounded_number_of_times_then_left(eng, monkeypatch):
    monkeypatch.setattr(engine_mod, "Fudkii", _fires_on({}))
    eng.store.replace_closed(_bar(T915 + 1800 * 5, source=BarSource.PARTIAL))
    assert eng.store.last("RELIANCE", "30m").source is BarSource.PARTIAL
    asked: list[int] = []

    class Reconciler:
        async def reconcile_bar(self, bar, *, timeout_s):
            asked.append(bar.ts)

            class C:
                found = False
            return C()

    eng.reconciler = Reconciler()
    eng.reconciler_ready = True
    monkeypatch.setattr(type(eng.s), "has_credentials", property(lambda self: True))
    for _ in range(5):
        got = await eng.scan_fudkii(max_confirm_attempts=3)
    assert asked == [T915 + 1800 * 5] * 3, "three tries at the exchange, then no more"
    assert got["bars_unconfirmed"] == 1, "and it is reported, never silently treated as a bar"


async def test_bars_decided_before_a_restart_stay_decided_after_it(settings, equity, monkeypatch):
    monkeypatch.setattr(engine_mod, "ist_today", lambda: DAY)
    first = engine_mod.Engine(settings)
    first._decided = {("RELIANCE", T915), ("RELIANCE", T915 + 1800)}
    first._save_decided()

    second = engine_mod.Engine(settings)
    second._load_decided()
    assert second._decided == {("RELIANCE", T915), ("RELIANCE", T915 + 1800)}

    monkeypatch.setattr(engine_mod, "ist_today", lambda: date(2026, 9, 25))
    third = engine_mod.Engine(settings)
    third._load_decided()
    assert third._decided == set(), "yesterday's decisions are not today's"
