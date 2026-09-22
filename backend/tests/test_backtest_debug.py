"""A backtest trade keeps its decision (signal, FUKAA verdict) and the debugger can explain it."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from kotsin_nse.domain import Direction
from kotsin_nse.research.backtest import (
    Backtester,
    BacktestParams,
    fukaa_funnel,
    fukaa_verdict,
    load_run,
    save,
)
from kotsin_nse.research.debug import trade_view
from kotsin_nse.research.history import HistoryStore
from kotsin_nse.strategy.base import Outcome, Rejection, Signal
from kotsin_nse.strategy.gates import GateResult
from kotsin_nse.strategy.keys import StrategyKey

from .conftest import ist_ts

DAY0 = "2026-01-05"


def _history(root: Path, symbol: str = "AAA", trading_days: int = 70) -> HistoryStore:
    store = HistoryStore(root)
    rows30, rows1d = [], []
    d = date.fromisoformat(DAY0)
    price, n = 1000.0, 0
    while n < trading_days:
        if d.weekday() < 5:
            n += 1
            o0, hi, lo = price, price, price
            for i in range(13):
                ts = int(ist_ts(d.isoformat(), "09:15")) + i * 1800
                drift = ((n * 7 + i * 3) % 11 - 5) * 0.4
                o, c = price, price + drift
                h, l_ = max(o, c) + 1.5, min(o, c) - 1.5
                rows30.append({"ts": ts, "o": o, "h": h, "l": l_, "c": c, "v": 1000.0})
                hi, lo, price = max(hi, h), min(lo, l_), c
            rows1d.append({"ts": int(ist_ts(d.isoformat(), "00:00")), "o": o0, "h": hi, "l": lo, "c": price, "v": 13000.0})
        d += timedelta(days=1)
    store.save(symbol, "30m", pd.DataFrame(rows30))
    store.save(symbol, "1d", pd.DataFrame(rows1d))
    return store


def _gate(name: str, passed: bool, *, value: float | None = 1.0, missing: bool = False) -> GateResult:
    return GateResult(name=name, passed=passed, required=True, value=value, threshold=0.5, missing=missing)


def _fudkii(b, direction=Direction.BULLISH) -> Signal:
    up = direction is Direction.BULLISH
    return Signal(
        strategy=StrategyKey.FUDKII, symbol=b.symbol, direction=direction, ts=b.ts, entry=b.close,
        stop=b.close * (0.99 if up else 1.01), targets=(b.close * (1.02 if up else 0.98),), grade="A", rr=2.0,
        gates=(_gate("st_flip", True), _gate("bb_break", True)),
        evidence={"atr": 4.0, "bars_since_flip": 0.0},
        context={"indicators": {"bb_upper": b.close - 1, "bb_middle": b.close - 5, "bb_lower": b.close - 9,
                                "st_value": b.close - 3, "st_trend": 1, "bars_in_trend": 1, "atr": 4.0, "params": {}},
                 "confluence": {"stop": b.close * 0.99, "stop_zone": "1d:S1", "targets": [b.close * 1.02],
                                "target_zones": ["1d:R1"], "grade": "A", "rr": 2.0, "fortress": 6.0, "room_ratio": 1.0, "note": ""},
                 "zones": [{"price": round(b.close * 0.99, 2), "strength": 4.0, "wall": False, "members": ["1d:S1"]},
                           {"price": round(b.close * 1.02, 2), "strength": 6.0, "wall": True, "members": ["1d:R1"]}]},
    )


def test_fukaa_verdict_and_funnel():
    class B:  # the only fields the helpers read
        symbol, ts, close = "AAA", 1_000, 100.0

    sig = _fudkii(B())
    watching = Rejection(strategy=StrategyKey.FUKAA, symbol="AAA", ts=1_000, direction=Direction.BULLISH,
                         binding_gate="volume_surge", gates=(_gate("volume_surge", False, value=1.2),),
                         evidence={"surge_used": 1.2}, note="WATCHING — 35 min to confirm")
    out = Outcome(signals=[sig], rejections=[watching])
    v = fukaa_verdict(out, sig)
    assert v is not None and v["verdict"] == "WATCHING" and v["binding_gate"] == "volume_surge"
    rejected = Rejection(strategy=StrategyKey.FUKAA, symbol="AAA", ts=1_000, direction=Direction.BULLISH,
                         binding_gate="ref_oi", gates=(_gate("ref_oi", False, value=None, missing=True),), evidence={}, note="")
    assert fukaa_verdict(Outcome(signals=[sig], rejections=[rejected]), sig)["verdict"] == "REJECTED"
    assert fukaa_verdict(Outcome(signals=[sig]), sig) is None
    fk = Signal(strategy=StrategyKey.FUKAA, symbol="AAA", direction=Direction.BULLISH, ts=1_000, entry=100.0, stop=99.0)
    assert fukaa_verdict(Outcome(signals=[sig, fk]), fk)["verdict"] == "TAKEN"

    class T:
        def __init__(self, f):
            self.fukaa = f

    funnel = fukaa_funnel([T(v), T({"verdict": "REJECTED", "binding_gate": "ref_oi"}), T(None), T({"verdict": "TAKEN"})])
    assert funnel == {"triggers": 4, "taken": 1, "watching": 1, "rejected": 1, "not_evaluated": 1, "by_gate": {"ref_oi": 1}}


def test_backtest_keeps_the_decision_and_the_debugger_explains_it(settings, tmp_path, monkeypatch):
    store = _history(tmp_path / "history")

    def decide(fudkii, fukaa, ctx, fukaa_ctx, b):
        sig = _fudkii(b)
        rej = Rejection(strategy=StrategyKey.FUKAA, symbol=b.symbol, ts=b.ts, direction=Direction.BULLISH,
                        binding_gate="ref_oi", gates=(_gate("ref_oi", False, value=None, missing=True),),
                        evidence={"surge_used": 4.5}, note="")
        return Outcome(signals=[sig], rejections=[rej])

    monkeypatch.setattr(Backtester, "_decide", staticmethod(decide))
    result = Backtester(settings, BacktestParams()).run(store, ["AAA"])
    assert result.trades and all(t.signal and t.signal["strategy"] == "FUDKII" for t in result.trades)
    assert all(t.fukaa and t.fukaa["verdict"] == "REJECTED" for t in result.trades)
    summary = result.summary()
    assert summary["fukaa_on_triggers"]["rejected"] == len(result.trades)
    assert summary["fukaa_on_triggers"]["by_gate"] == {"ref_oi": len(result.trades)}

    path = save(result, tmp_path / "backtests")
    raw = json.loads(path.read_text())
    assert "signal" not in raw["trades"][0] and len(raw["details"]) == len(raw["trades"])
    assert raw["details"][0]["signal"]["context"]["zones"]
    run = load_run(path)
    assert run is load_run(path)  # cached by mtime

    idx = len(result.trades) // 2  # a trade with history before it; trade 0 sits at the cache's edge
    view = trade_view(path, idx, store, before=30, after=8)
    assert view["count"] == len(result.trades) and view["tf"] == "30m" and view["index"] == idx
    assert view["bars"] and view["bars"][0]["bb_upper"] is not None  # warm-up computed before the window
    roles = [r for b in view["bars"] for r in b["roles"]]
    assert "decision" in roles and "entry" in roles and "exit" in roles
    assert view["levels"]["stop_zone"] == "1d:S1" and view["levels"]["stop_dist_atr"] is not None
    assert view["path"]["path.window_bars"] > 0 and view["fukaa"]["verdict"] == "REJECTED"
    assert view["signal"]["gates"][0]["name"] == "st_flip" and len(view["zones"]) == 2
    import pytest

    with pytest.raises(KeyError):
        trade_view(path, 10_000, store)
