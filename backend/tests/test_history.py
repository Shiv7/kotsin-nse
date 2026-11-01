"""The Parquet history cache: a re-fetched bar replaces the cached one at the same timestamp — the
fresh row wins every time, not whichever duplicate an unstable sort happened to leave last."""

from __future__ import annotations

from datetime import datetime, timedelta

from kotsin_nse.market.session import ist_naive_to_ts
from kotsin_nse.research.history import HistoryStore


def _dt(minute: int) -> str:
    return (datetime(2025, 6, 2, 9, 15) + timedelta(minutes=minute)).strftime("%Y-%m-%d %H:%M:%S")


def _rows(n: int, close: float, first: int = 0) -> list[dict]:
    return [{"dt": _dt(first + i), "o": 100.0, "h": 101.0, "l": 99.0, "c": close, "v": 10.0} for i in range(n)]


def test_a_refetched_overlapping_bar_replaces_the_cached_one(tmp_path):
    store = HistoryStore(tmp_path)
    store.merge("X", "1m", _rows(60, close=1.0))
    # the tail is fetched again (the padded window overlaps) with revised prints, plus one new bar
    store.merge("X", "1m", _rows(41, close=2.0, first=20))
    df = store.load("X", "1m")
    assert len(df) == 61 and df["ts"].is_unique and df["ts"].is_monotonic_increasing
    cut = int(ist_naive_to_ts(_dt(20)))
    assert (df.loc[df["ts"] < cut, "c"] == 1.0).all()
    assert (df.loc[df["ts"] >= cut, "c"] == 2.0).all(), "a re-fetched bar must replace the cached one"
