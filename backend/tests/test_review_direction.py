"""The review card's direction block (operator, 2026-10-09: "focus is on choosing the right direction of trade. often the
direction is not right and often we trade a trigger that should have been skipped"): each read at the decision marked with
or against the trade by a threshold already in use, the right-direction label from the minute bars, and the candidate
skip rules as a hook that shows nothing until the research writes its file."""

from __future__ import annotations

import json

from kotsin_nse.api.review_direction import (
    SKIP_RULES_FILE,
    candidate_skip,
    direction_read,
    first_touch,
    load_skip_rules,
    trigger_close_ts,
)

BAR = 1_791_517_500.0  # the session's first 30m bar, 9 Oct 2026 09:15 IST
CLOSE = BAR + 1800  # its close, 09:45


def bars(path: list[tuple[float, float]], start: float = CLOSE, o: list[float] | None = None) -> list[dict]:
    return [{"ts": start + 60 * i, "o": (o[i] if o else (lo + hi) / 2), "h": hi, "l": lo, "c": (lo + hi) / 2} for i, (lo, hi) in enumerate(path)]


def test_the_label_is_the_side_reached_first():
    up = first_touch(bars([(99.9, 100.2)] * 3 + [(100.1, 100.6)]), start=CLOSE, end=CLOSE + 3600, close=100.0, atr=1.0)
    assert up["first"] == "up" and up["ts"] == CLOSE + 180
    down = first_touch(bars([(99.6, 100.2), (99.4, 100.1)]), start=CLOSE, end=CLOSE + 3600, close=100.0, atr=1.0)
    assert down["first"] == "down" and down["ts"] == CLOSE + 60
    assert first_touch(bars([(99.8, 100.2)] * 30), start=CLOSE, end=CLOSE + 3600, close=100.0, atr=1.0)["first"] == "none"
    both = first_touch(bars([(99.4, 100.6)]), start=CLOSE, end=CLOSE + 3600, close=100.0, atr=1.0)
    assert both["first"] == "ambiguous", "both inside one minute, the open beyond neither: the 1 s tape would decide"
    gap_up = first_touch(bars([(99.4, 100.7)], o=[100.6]), start=CLOSE, end=CLOSE + 3600, close=100.0, atr=1.0)
    assert gap_up["first"] == "up" and gap_up["how"] == "the minute opened beyond the level"
    assert first_touch([], start=CLOSE, end=CLOSE + 60, close=100.0, atr=1.0)["first"] is None


def test_the_closing_bar_closes_at_the_close():
    assert trigger_close_ts(BAR) == CLOSE
    b1515 = BAR + 6 * 3600  # 15:15
    assert trigger_close_ts(b1515) == BAR + 6.25 * 3600, "the 15:15 bar closes at 15:30"


def _card(**kw) -> dict:
    c = {"direction": "BULLISH", "book": "FUDKII_RT_Y", "grade": "C", "stock": {"atr": 1.0},
         "checks": [{"key": "market", "share": 0.38}, {"key": "entry", "chaseAtr": 0.42},
                    {"key": "oi", "status": "bad", "quadrant": "short build-up", "detail": "OI +2 % with the bar down: short build-up — against"}],
         "levels": {"ahead": {"label": "1d.R1", "price": 100.2, "atr": 0.2}}}
    c.update(kw)
    return c


SIGNAL = {"ts": BAR, "direction": "BULLISH", "evidence": {"close": 100.0, "atr": 1.0}}
BREADTH = {"atr30": 1.0, "volSurgeT": 0.8, "volSurgeT1": 0.7, "gapDatr": 0.35, "openBar": True, "relVol": 1.2, "direction": "BULLISH"}
NIFTY = [{"ts": BAR - 1800 + 60 * i, "o": 22000.0 - i, "h": 22001.0 - i, "l": 21999.0 - i, "c": 22000.0 - i} for i in range(60)]


def test_every_read_against_an_in_trend_trade_and_the_wrong_direction():
    d = direction_read(_card(), signal=SIGNAL, breadth=BREADTH, route={"route": "COUNTER"}, bars=bars([(99.6, 100.1), (99.3, 99.9)]),
                       nifty_bars=NIFTY, book_side="trend")
    assert set(d["againstKeys"]) == {"route", "breadth", "level", "volume", "gap", "chase", "nifty", "oi"}
    assert d["label"]["first"] == "down" and d["label"]["tradeRight"] is False and d["label"]["triggerRight"] is False
    assert "the wrong direction for this trade" in d["label"]["text"]
    assert d["skip"] is None, "no rules file: the hook shows nothing"
    assert d["facts"]["triggerCloseMin"] == 30 and d["facts"]["breadthWith"] == 0.38


def test_a_fade_reads_the_route_and_the_gap_the_other_way():
    d = direction_read(_card(direction="BEARISH", book="FUDKII_CT_Y"), signal=SIGNAL, breadth=BREADTH, route={"route": "COUNTER"},
                       bars=bars([(99.6, 100.1), (99.3, 99.9)]), nifty_bars=NIFTY, book_side="counter-trend")
    stance = {r["key"]: r["stance"] for r in d["reads"]}
    assert stance["route"] == "with" and stance["gap"] == "with", "routed to the fade, a gap CT-Y fades"
    assert d["label"]["tradeRight"] is True and d["label"]["triggerRight"] is False
    switched = direction_read(_card(direction="BEARISH", book="FUDKII_CT_Y"), signal=SIGNAL, breadth={**BREADTH, "relVol": 7.9},
                              route={"route": "IN_TREND"}, bars=[], nifty_bars=[], book_side="counter-trend")
    assert {r["key"]: r["stance"] for r in switched["reads"]}["gap"] == "against", "on heavy volume the gap switch follows the gap"
    assert switched["label"]["first"] is None


def test_the_skip_rules_hook_reads_a_file_only_when_one_exists(tmp_path):
    assert load_skip_rules(tmp_path) is None and load_skip_rules(None) is None
    (tmp_path / SKIP_RULES_FILE).write_text("not json")
    assert load_skip_rules(tmp_path) is None
    rules = {"name": "test", "rules": [{"id": "R1", "label": "late", "all": [{"fact": "triggerCloseMin", "op": ">=", "value": 30}]},
                                       {"id": "R2", "label": "breadth", "all": [{"fact": "breadthWith", "op": "<", "value": 0.3}]},
                                       {"id": "R3", "label": "no such fact", "all": [{"fact": "nope", "op": "==", "value": 1}]}]}
    (tmp_path / SKIP_RULES_FILE).write_text(json.dumps(rules))
    loaded = load_skip_rules(tmp_path)
    assert loaded == rules
    v = candidate_skip({"triggerCloseMin": 60, "breadthWith": 0.38}, loaded)
    assert [r["fired"] for r in v["rules"]] == [True, False, None] and v["skipped"] is True
    d = direction_read(_card(), signal=SIGNAL, breadth=BREADTH, route=None, bars=[], nifty_bars=[], book_side="trend", skip_rules=loaded)
    assert d["skip"]["skipped"] is True and d["reads"][0]["stance"] == "unknown"
