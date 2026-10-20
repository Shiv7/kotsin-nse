"""The scanner: strength, the board's rules, and a past day replayed from the cache."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from kotsin_nse.market.session import ist_at
from kotsin_nse.research.history import HistoryStore
from kotsin_nse.scan.board import OIRead, build_board
from kotsin_nse.scan.replay import DayReplay
from kotsin_nse.scan.strength import DayState, average_daily_range, strength, vwap


def st(sym="A", o=100.0, h=100.0, lo=100.0, last=100.0, prev=100.0, adr=2.0, vw=None):
    return DayState(sym, o, h, lo, last, prev, adr, vwap=vw)


# -- strength -----------------------------------------------------------------------------------------


def test_one_sided_up_day_scores_its_move_in_average_days():
    assert strength(st(h=104, lo=99.5, last=103)) == pytest.approx(1.5)


def test_a_run_up_that_came_back_scores_nothing():
    assert strength(st(h=104, lo=99.5, last=100)) == 0.0


def test_a_two_sided_day_scores_nothing():
    assert strength(st(h=103, lo=98, last=102.5)) == 0.0  # up 3 is not > 2 × down 2


def test_bearish_is_negative_and_no_adr_means_no_score():
    assert strength(st(h=100.5, lo=96, last=97)) == pytest.approx(-1.5)
    assert strength(st(h=104, lo=99.5, last=103, adr=None)) == 0.0


def test_adr_needs_history_and_uses_the_last_n_sessions():
    assert average_daily_range([10] * 5, [9] * 5) is None
    highs = [12.0] * 10 + [11.0] * 50
    lows = [10.0] * 60
    assert average_daily_range(highs, lows, n=50) == pytest.approx(1.0)


def test_vwap_weights_typical_price_by_volume():
    assert vwap([(11, 9, 10, 100), (21, 19, 20, 300)]) == pytest.approx(17.5)
    assert vwap([(11, 9, 10, 0)]) is None


# -- the board -------------------------------------------------------------------------------------------


def test_picks_take_the_top_three_per_side_with_a_sector_cap_and_no_event_risk():
    states = [
        st("B1", h=110, lo=99.9, last=109),  # 4.5
        st("B2", h=108, lo=99.9, last=107),  # 3.5
        st("B3", h=107, lo=99.9, last=106),  # 3.0 — third from the same sector → capped
        st("B4", h=105, lo=99.9, last=104),  # 2.0 — takes the capped one's place
        st("S1", h=100.1, lo=93, last=94, prev=101),  # bearish, fell (94 − 101)/2 = −3.5 ADR → event risk
        st("S2", h=100.1, lo=97, last=98),  # −1.0
    ]
    sectors = {"B1": "Bank", "B2": "Bank", "B3": "Bank", "B4": "IT", "S1": "IT", "S2": "IT"}
    b = build_board(0, states, sectors)
    assert b.bullish == ["B1", "B2", "B3", "B4"] and b.bearish == ["S1", "S2"]
    assert b.picks == ["B1", "B2", "B4", "S2"]
    rule = {r.symbol: r.rule for r in b.rows}
    assert rule["B3"] == "NOT PICKED · sector cap" and rule["S1"] == "NOT PICKED · event risk"
    assert b.breadth["bullish"] == 4 and b.breadth["bearish"] == 2
    ranks = {r.symbol: r.rank for r in b.rows}
    assert ranks["B1"] == 1 and ranks["S1"] == 1 and ranks["S2"] == 2
    pct = {r.symbol: r.percentile for r in b.rows}
    assert pct["B1"] > pct["B4"] > pct["S2"] > pct["S1"]


def test_oi_build_up_labels_the_four_quadrants():
    assert OIRead(5, 1).build_up == "LONG BUILD-UP"
    assert OIRead(5, -1).build_up == "SHORT BUILD-UP"
    assert OIRead(-5, 1).build_up == "SHORT COVERING"
    assert OIRead(-5, -1).build_up == "LONG UNWINDING"


def test_a_tiny_oi_change_is_no_build_up():
    """TECHM +0.05 % OI was labelled SHORT BUILD-UP on 2026-10-19."""
    assert OIRead(0.05, -0.2).build_up == "NEUTRAL"
    assert OIRead(-0.9, 2.0).build_up == "NEUTRAL"
    assert OIRead(1.0, -0.2).build_up == "SHORT BUILD-UP"


def test_vwap_side_is_reported():
    b = build_board(0, [st("A", h=104, lo=99.9, last=103, vw=102)], {})
    assert b.rows[0].above_vwap is True


# -- a past day, replayed --------------------------------------------------------------------------------


def _store(tmp_path, day: date) -> HistoryStore:
    store = HistoryStore(tmp_path)
    daily = []
    for i in range(60):
        ts = ist_at(day, "09:15") - (60 - i) * 86400
        daily.append({"ts": ts, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1e6})
    store.save("UP", "1d", pd.DataFrame(daily))
    store.save("FLAT", "1d", pd.DataFrame(daily))
    t0 = ist_at(day, "09:15")
    up = [{"ts": t0 + 60 * k, "o": 100 + 0.1 * k, "h": 100.1 + 0.1 * k, "l": 99.95 + 0.1 * k, "c": 100.1 + 0.1 * k, "v": 1000} for k in range(60)]
    flat = [{"ts": t0 + 60 * k, "o": 100, "h": 100.5, "l": 99.5, "c": 100, "v": 1000} for k in range(60)]
    store.save("UP", "1m", pd.DataFrame(up))
    store.save("FLAT", "1m", pd.DataFrame(flat))
    return store


def test_replay_reads_only_bars_closed_by_the_minute_and_scores_from_0920(tmp_path):
    day = date(2026, 10, 16)
    r = DayReplay(_store(tmp_path, day), day)
    assert r.minutes()[0] == ist_at(day, "09:20")
    b = r.board_at(ist_at(day, "09:20"))
    up = next(x for x in b.rows if x.symbol == "UP")
    # bars 09:15..09:19 closed by 09:20 → last close = 100.1 + 0.4 = 100.5, ADR = 2
    assert up.last == pytest.approx(100.5) and up.score == pytest.approx(0.25)
    later = next(x for x in r.board_at(ist_at(day, "10:00")).rows if x.symbol == "UP")
    assert later.score > up.score and later.side == "BULLISH"
    assert next(x for x in b.rows if x.symbol == "FLAT").side == "NONE"


def test_an_unadjusted_split_in_the_adr_window_withholds_the_score(tmp_path):
    day = date(2026, 10, 16)
    store = _store(tmp_path, day)
    d1 = store.load("UP", "1d")
    d1.loc[d1.index[-10:], ["o", "h", "l", "c"]] = d1.loc[d1.index[-10:], ["o", "h", "l", "c"]] / 2
    store.save("UP", "1d", d1)
    r = DayReplay(store, day)
    assert "UP" in r.summary()["without_adr"]


def test_the_days_own_daily_candle_is_never_its_previous_close(tmp_path):
    """NSE stamps a final daily candle at 00:00 of its own day — before 09:15, but not before the day."""
    day = date(2026, 10, 16)
    store = _store(tmp_path, day)
    d1 = store.load("UP", "1d")
    today = pd.DataFrame([{"ts": ist_at(day, "00:00"), "o": 100.0, "h": 120.0, "l": 90.0, "c": 115.0, "v": 1e6}])
    store.save("UP", "1d", pd.concat([d1, today], ignore_index=True))
    r = DayReplay(store, day)
    assert r.days["UP"].prev_close == 100.0 and r.days["UP"].adr == pytest.approx(2.0)


def test_no_pick_symbols_rank_but_never_take_a_pick_slot() -> None:
    """MCX commodities ride the live board but the picks rule was only tested on NSE stocks."""
    states = [st("CRUDEOIL", h=106, lo=99.9, last=105, vw=103), st("A", h=104, lo=99.9, last=103, vw=102)]
    b = build_board(0, states, {"CRUDEOIL": "Commodity"}, no_pick={"CRUDEOIL": "NOT PICKED · MCX"})
    crude = next(r for r in b.rows if r.symbol == "CRUDEOIL")
    assert crude.rank == 1 and not crude.pick and crude.rule == "NOT PICKED · MCX"
    assert b.picks == ["A"]
    assert "Commodity" in {s["sector"] for s in b.sectors}
