"""The call journal's scorer: fills, stop-before-target, the time marks, the close, the summary."""

from __future__ import annotations

from datetime import date

import pytest

from kotsin_nse.journal import close_reason, new_call, score, summary
from kotsin_nse.market.session import ist_at

D = date(2026, 10, 23)
T0 = ist_at(D, "11:05")


def _bars(path: list[tuple[float, float, float, float]], start: int = T0) -> list[tuple[float, float, float, float, float]]:
    return [(start + 60 * i, o, h, lw, c) for i, (o, h, lw, c) in enumerate(path)]


def _call(**kw):
    base = {"symbol": "eternal", "side": "LONG", "kind": "entry", "price": 100.0, "entry": None, "stop": 99.0, "target": 102.0,
            "tags": ["5m double bottom"], "note": "", "ts": T0 + 20}
    base.update(kw)
    return new_call(**base)


def test_a_call_is_validated_and_defaults_its_risk():
    c = _call()
    assert (c["symbol"], c["entry"], c["risk"], c["risk_is_default"], c["day"]) == ("ETERNAL", 100.0, 1.0, False, "2026-10-23")
    assert _call(stop=None)["risk"] == pytest.approx(0.5) and _call(stop=None)["risk_is_default"]
    with pytest.raises(ValueError):
        _call(stop=101.0)
    with pytest.raises(ValueError):
        _call(side="SHORT", target=103.0)
    with pytest.raises(ValueError):
        _call(price=None)


def test_target_hit_scores_its_r_and_closes_the_call():
    c = _call()
    out = score(c, _bars([(100, 100.5, 99.6, 100.2), (100.2, 101.0, 100.0, 100.9), (100.9, 102.1, 100.8, 101.9)]), now=T0 + 600)
    assert out["filled_ts"] == T0 and out["hit"] == "target" and out["hit_ts"] == T0 + 120
    assert out["r_now"] == 2.0 and out["final"] and out["mfe_r"] == pytest.approx(2.1) and out["mae_r"] == pytest.approx(-0.4)
    assert close_reason(out) == "target"


def test_a_bar_touching_both_is_the_stop():
    out = score(_call(), _bars([(100, 100.2, 99.9, 100.1), (100.1, 102.5, 98.9, 101.0)]), now=T0 + 600)
    assert out["hit"] == "stop" and out["r_now"] == -1.0 and out["final"]


def test_a_short_scores_in_its_own_direction():
    c = _call(side="SHORT", stop=101.0, target=98.0)
    out = score(c, _bars([(100, 100.3, 99.5, 99.6), (99.6, 99.7, 98.4, 98.5)]), now=T0 + 600)
    assert out["hit"] is None and out["r_now"] == pytest.approx(1.5) and out["mfe_r"] == pytest.approx(1.6) and not out["final"]


def test_a_level_never_reached_is_not_filled():
    c = _call(entry=99.0, stop=98.0, target=101.0)
    out = score(c, _bars([(100, 100.5, 99.5, 100.2), (100.2, 100.8, 99.9, 100.5)]), now=T0 + 600)
    assert out["filled_ts"] is None and out["r_now"] is None and not out["final"]
    assert close_reason(out) == "not filled"


def test_the_time_marks_and_the_close():
    c = _call(stop=90.0, target=120.0)  # neither hit: the marks and the close decide
    path = [(100, 100.4, 99.8, 100.0 + 0.1 * i) for i in range(70)]
    out = score(c, _bars(path), now=T0 + 70 * 60)
    assert out["r_15m"] == pytest.approx(1.5 / 10, abs=0.011) and out["r_30m"] == pytest.approx(3.0 / 10, abs=0.011) and out["r_60m"] == pytest.approx(6.0 / 10, abs=0.011)
    assert not out["final"] and out["r_close"] is None
    eod = ist_at(D, "15:25")
    out = score(c, [*_bars(path), (eod, 106.0, 106.5, 105.5, 106.0)], now=eod + 120)
    assert out["final"] and out["r_close"] == pytest.approx(0.6) and close_reason(out) == "close"


def test_the_summary_keeps_kinds_and_tags_apart():
    a, b = _call(), _call(kind="alert", tags=["CPR retest"])
    a["status"], a["outcome"] = "CLOSED", {"filled_ts": T0, "hit": "target", "r_now": 2.0, "mfe_r": 2.1, "mae_r": -0.4}
    b["status"], b["outcome"] = "CLOSED", {"filled_ts": T0, "hit": "stop", "r_now": -1.0, "mfe_r": 0.3, "mae_r": -1.0}
    c = _call()
    c["status"] = "CANCELLED"
    s = summary([a, b, c])
    assert s["all"]["calls"] == 2 and s["all"]["targets"] == 1 and s["all"]["stops"] == 1 and s["all"]["avg_r"] == 0.5
    assert s["by_kind"]["entry"]["sum_r"] == 2.0 and s["by_kind"]["alert"]["sum_r"] == -1.0
    assert s["by_tag"]["CPR retest"]["win_rate"] == 0.0 and s["by_tag"]["5m double bottom"]["win_rate"] == 100.0
