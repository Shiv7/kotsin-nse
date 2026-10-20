"""The option recorder (``ops/optrec.py``) — RECORD-ONLY: the as-of quotes, OI and per-minute option volume the 5-read
reads, and what it archives. Every read is as of a moment; a stretch it cannot vouch for says so.
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind, OptionType
from kotsin_nse.ops import read5rec as RR
from kotsin_nse.ops.archive import DailyArchive
from kotsin_nse.ops.optrec import OptionRecorder, VolumeTape, broker_ts, read_volume_day
from kotsin_nse.research import read5 as R

DAY = "2026-10-19"
D = date(2026, 10, 19)


def at(hms: str, day: str = DAY) -> float:
    whole, _, frac = hms.partition(".")
    return R.at_ist(day, whole) + (float(f"0.{frac}") if frac else 0.0)


def dt(t: float) -> str:
    return f"/Date({int(t * 1000)})/"


def mf(code: str, ltp: float, bid: float = 0.0, ask: float = 0.0, **kw: Any) -> dict[str, Any]:
    return {"Token": code, "LastRate": ltp, "BidRate": bid, "OffRate": ask, **kw}


def recorder(watch: dict[str, str] | None = None, **kw: Any) -> OptionRecorder:
    rec = OptionRecorder(**kw)
    rec.roll(D)
    rec.set_watch(watch or {"O1": "opt", "O2": "opt", "F": "fut", "E": "eq"})
    return rec


# -- volume ------------------------------------------------------------------------------------------------------


def test_the_running_total_becomes_minute_volume_by_exchange_time_and_the_first_frame_is_only_a_reference():
    rec = recorder(silence_s=None)
    rec.on_frame("mf", mf("O1", 10.0, TotalQty=5_000, TickDt=dt(at("09:20:10"))), at("09:20:11"))
    assert rec.volume("O1", at("09:20:00"), at("09:30:00")) == (math.nan, rec.volume("O1", at("09:20:00"), at("09:30:00"))[1])
    rec.on_frame("mf", mf("O1", 10.5, TotalQty=5_750, TickDt=dt(at("09:21:59.5"))), at("09:22:00.2"))
    rec.on_frame("mf", mf("O1", 10.5, TotalQty=6_000, TickDt=dt(at("09:22:30"))), at("09:22:31"))
    v, why = rec.volume("O1", at("09:21:00"), at("09:23:00"))
    assert (v, why) == (1_000.0, "")
    assert rec.volume("O1", at("09:22:00"), at("09:23:00"))[0] == 250.0, "the 750 traded at 09:21:59.5 is 09:21's"
    assert rec.volume("O1", at("09:22:00"), at("09:22:59"))[0] == 0.0, "a minute counts only once complete"
    v, why = rec.volume("O1", at("09:15:00"), at("09:23:00"))
    assert math.isnan(v) and "recorded only from 09:20:11" in why


def test_a_frame_whose_last_trade_is_yesterdays_says_nothing_traded_today():
    """The pre-open snapshot carries the last trade's TickDt — yesterday's — and yesterday's running total: today's
    reference is 0, covered from that frame, so the first trade's whole quantity is counted."""
    rec = recorder(silence_s=None)
    rec.on_frame("mf", mf("O1", 12.0, TotalQty=88_000, TickDt=dt(at("15:29:58", "2026-10-16"))), at("09:07:30"))
    rec.on_frame("mf", mf("O1", 12.5, TotalQty=1_500, TickDt=dt(at("09:15:03"))), at("09:15:03.4"))
    assert rec.volume("O1", at("09:15:00"), at("09:16:00")) == (1_500.0, "")


def test_a_running_total_that_goes_back_restarts_the_coverage():
    rec = recorder()
    rec.on_frame("mf", mf("O1", 10.0, TotalQty=5_000, TickDt=dt(at("09:30:00"))), at("09:14:00"))
    rec.on_frame("mf", mf("O1", 10.0, TotalQty=4_000, TickDt=dt(at("09:40:00"))), at("09:40:01"))
    assert rec.volume_restarts == 1
    v, why = rec.volume("O1", at("09:30:00"), at("09:45:00"))
    assert math.isnan(v) and "09:40:01" in why


def test_a_reconnect_or_a_silence_in_the_session_leaves_the_bars_it_touches_unverified():
    rec = recorder()
    rec.on_frame("mf", mf("O1", 10.0, TotalQty=0, TickDt=dt(at("15:29:00", "2026-10-16"))), at("09:10:00"))
    for i in range(21):
        rec.on_frame("mf", mf("O2", 5.0 + i / 100), at("09:10:00") + 30 * i)
    rec.on_frame("mf", mf("O2", 5.5), at("09:22:30"))  # 150 s of nothing in the session (the Mac asleep)
    assert len(rec.gaps) == 1
    assert rec.gaps and rec.gaps[-1][:2] == (at("09:20:00"), at("09:22:30"))
    v, why = rec.volume("O1", at("09:15:00"), at("09:45:00"))
    assert math.isnan(v) and "feed gap 09:20:00-09:22:30" in why
    assert rec.covered("O1", at("09:45:00"), at("10:15:00"))[0]
    rec.note_reconnect(at("10:00:00"))
    assert not rec.covered("O1", at("09:45:00"), at("10:15:00"))[0]
    quiet = recorder(silence_s=None)
    quiet.on_frame("mf", mf("O2", 5.0), at("09:20:00"))
    quiet.on_frame("mf", mf("O2", 5.1), at("09:30:00"))
    assert not quiet.gaps, "a replay of sparse rows switches the silence rule off"


def test_closed_bars_carry_volume_and_verification_and_feed_the_baseline_and_the_archive(tmp_path):
    rec = recorder(silence_s=None)
    rec.on_frame("mf", mf("O1", 10.0, TotalQty=0, TickDt=dt(at("15:29:00", "2026-10-16"))), at("09:08:00"))
    cum = 0
    for i in range(30):
        cum += 100 * (i + 1)
        t = at("09:15:00") + 60 * i + 20
        rec.on_frame("mf", mf("O1", 10.0 + i / 100, TotalQty=cum, TickDt=dt(t)), t + 0.3)
    rec.on_frame("mf", mf("O2", 3.0, TotalQty=10, TickDt=dt(at("09:31:00"))), at("09:31:00.5"))  # covered from 09:31
    assert rec.close_bars(at("09:46:00")) == 0, "not before 90 s past the bar's end"
    assert rec.close_bars(at("09:46:31")) == 2
    rows = {r["scrip_code"]: r for r in rec.take_bar_rows()}
    assert rows["O1"]["v"] == float(sum(100 * (i + 1) for i in range(30))) and rows["O1"]["verified"] is True
    assert rows["O2"]["verified"] is False and rows["O2"]["bucket_ts"] == int(at("09:15:00"))
    assert rec.bar30("O1", DAY, "09:15", at("10:00:00")) == (46_500.0, "")
    v, why = rec.bar30("O2", DAY, "09:15", at("10:00:00"))
    assert math.isnan(v) and "not verified" in why and "09:31:00" in why
    a = DailyArchive(tmp_path / "archive")
    for r in rows.values():
        a.record5("option_volume_30m", float(r["bucket_ts"]), r)
    a.flush()
    back = pd.read_parquet(tmp_path / "archive" / "option_volume_30m" / f"{DAY}.parquet")
    assert set(back.scrip_code) == {"O1", "O2"} and list(back.columns)[:3] == ["scrip_code", "bucket_ts", "v"]
    fresh = recorder()
    assert fresh.load_bars(back.to_dict("records")) == 2, "a restart reads today's closed bars back"
    assert fresh.bar30("O1", DAY, "09:15", at("10:00:00")) == (46_500.0, "")


def test_minute_rows_are_additive_part_files(tmp_path):
    rec = recorder(silence_s=None)
    rec.on_frame("mf", mf("O1", 10.0, TotalQty=0, TickDt=dt(at("15:29:00", "2026-10-16"))), at("09:08:00"))
    rec.on_frame("mf", mf("O1", 10.0, TotalQty=300, TickDt=dt(at("09:20:10"))), at("09:20:10.5"))
    rec.on_frame("mf", mf("O1", 10.0, TotalQty=400, TickDt=dt(at("09:21:30"))), at("09:21:30.5"))
    assert rec.take_minute_rows(at("09:22:00")) == [], "a minute waits two minutes past its end"
    rows = rec.take_minute_rows(at("09:23:01"))
    assert rows == [("O1", int(at("09:20:00")), 300.0, 300.0)]
    rec.on_frame("mf", mf("O1", 10.0, TotalQty=550, TickDt=dt(at("09:20:50"))), at("09:24:00"))  # a late frame
    late = rec.take_minute_rows(at("09:30:00"))
    assert late == [("O1", int(at("09:20:00")), 150.0, 550.0), ("O1", int(at("09:21:00")), 100.0, 400.0)]
    assert rec.late_minutes == 1 and rec.volume("O1", at("09:20:00"), at("09:21:00"))[0] == 450.0
    tape = VolumeTape(tmp_path / "option_volume")
    tape.write(rows, now=at("09:23:02"))
    tape.write(late, now=at("09:30:01"))
    day = read_volume_day(tmp_path / "option_volume", DAY)
    assert day.to_dict("records") == [{"scrip_code": "O1", "ts": int(at("09:20:00")), "v": 450.0, "cum": 550.0},
                                      {"scrip_code": "O1", "ts": int(at("09:21:00")), "v": 100.0, "cum": 400.0}]


# -- quotes and OI as of a moment --------------------------------------------------------------------------------


def test_a_quote_as_of_a_moment_its_freshness_and_its_last_trade_run():
    rec = recorder()
    rec.on_frame("mf", mf("O1", 10.0, 9.9, 10.1), at("10:00:00"))
    rec.on_frame("mf", mf("O1", 10.0, 9.8, 10.2), at("10:00:30"))  # a quote change: the last trade is 10:00:00's
    rec.on_frame("mf", mf("O1", 10.0, 9.8, 10.2), at("10:00:40"))  # nothing changed: no entry, the quote ages
    q = rec.quote("O1", at("10:01:20"))
    assert q is not None and q["eff"] == at("10:00:30") and q["fresh"] and q["mid"] == 10.0 and q["run"] == at("10:00:00")
    assert R.opt_px(q) == (10.0, "mid")
    q = rec.quote("O1", at("10:01:31"))
    assert q is not None and not q["fresh"] and R.opt_px(q) == (10.0, "ltp"), "61 s since the change: the last trade"
    rec.on_frame("mf", mf("O1", 10.4, 0.0, 10.5), at("10:02:00"))
    assert R.opt_px(rec.quote("O1", at("10:02:01"))) == (10.4, "ltp"), "one-sided: the last trade"
    assert rec.quote("O1", at("09:59:59")) is None
    rec.on_frame("mf", mf("O1", 0.0, 9.0, 11.0), at("10:03:00"))
    assert rec.quote("O1", at("10:03:01"))["ltp"] == 10.4, "a frame with no last price is not a quote (the quote table's rule)"


def test_two_frames_in_one_second_keep_the_later_and_the_first_print_after_0915():
    rec = recorder()
    rec.on_frame("mf", mf("O1", 7.0, 6.9, 7.1), at("09:14:58"))
    rec.on_frame("mf", mf("O1", 7.0, 6.95, 7.1), at("09:15:00.2"))
    rec.on_frame("mf", mf("O1", 7.3, 7.2, 7.4), at("09:15:00.7"))
    assert len(rec._ring["O1"].t) == 2
    t, ltp, run = rec.first_after_open("O1")
    assert (t, ltp, run) == (at("09:15:00.7"), 7.3, at("09:15:00.7"))


def test_the_ring_trims_to_its_window_and_a_future_reads_its_minute_state_beyond_it():
    rec = recorder(ring_s=600)
    t0 = at("10:00:00")
    for i in range(1500):
        rec.on_frame("mf", mf("F", 7000 + i * 0.05, 7000 + i * 0.05 - 0.5, 7000 + i * 0.05 + 0.5), t0 + i)
        rec.on_frame("mf", mf("O1", 50 + i * 0.01), t0 + i)
    rec.trim(t0 + 1500)
    ring = rec._ring["F"]
    assert ring.trimmed and len(ring.t) < 1500 and ring.t[ring.lo] <= t0 + 900
    q = rec.quote("F", t0 + 100.5)
    assert q is not None and q["src"] == "minute" and t0 + 59 <= q["eff"] <= t0 + 100.5, "the last state of 10:00 or 10:01"
    assert rec.quote("O1", t0 + 100.5) is None, "an option's history ends at the ring"
    assert rec.quote("F", t0 + 1400)["src"] == "ring"


def test_oi_as_of_a_moment_with_its_basis_and_the_previous_sessions_fallback():
    rec = recorder()
    rec.oi_prev["O1"] = 900.0
    assert rec.oi_at("O1", at("09:30:00")) == (900.0, rec.oi_at("O1", at("09:30:00"))[1], "prevday")
    assert math.isnan(rec.oi_at("O1", at("09:30:00"), allow_prev=False)[0])
    rec.on_frame("oi", {"Token": "O1", "OpenInterest": 1000}, at("09:05:00"))
    rec.on_frame("oi", {"Token": "O1", "OpenInterest": 1000}, at("09:16:00"))  # unchanged: not kept
    rec.on_frame("oi", {"Token": "O1", "OpenInterest": 1200}, at("09:20:00"))
    assert rec.oi_at("O1", at("09:16:00"))[0::2] == (1000.0, "preopen")
    assert rec.oi_at("O1", at("09:25:00"))[0::2] == (1200.0, "session")
    assert rec.oi_recorded("O1", at("09:10:00")) and not rec.oi_recorded("O2", at("10:00:00"))
    rec.on_frame("oi", {"Token": "O1", "OpenInterest": 1300}, at("15:00:00"))
    rec.roll(date(2026, 10, 21))
    assert rec.oi_prev == {"O1": 1300.0} and rec.oi_at("O1", at("09:30:00", "2026-10-21"))[2] == "prevday"


def test_a_pooled_frame_from_an_earlier_day_is_not_todays_quote():
    """The pool keeps a code's last frame however old (the hub's answer to a twin's subscribe): one that first arrived
    yesterday is neither today's quote nor a volume reference."""
    rec = recorder()
    rec.on_frame("mf", mf("O1", 9.0, 8.9, 9.1, TotalQty=50_000, TickDt=dt(at("15:29:00", "2026-10-16"))),
                 at("15:29:01", "2026-10-16"), True)
    assert rec.quote("O1", at("09:20:00")) is None and "O1" not in rec._vol and rec.frames_watched == 0
    rec.on_frame("mf", mf("O1", 9.5, 9.4, 9.6, TotalQty=1_000, TickDt=dt(at("09:15:30"))), at("09:15:31"), True)
    assert rec.quote("O1", at("09:20:00"))["ltp"] == 9.5 and rec.frames_pooled == 1


def test_an_unwatched_code_costs_a_lookup_and_a_fault_never_reaches_the_reader():
    rec = recorder()
    rec.on_frame("mf", mf("ZZZ", 1.0), at("10:00:00"))
    assert "ZZZ" not in rec._ring and rec.frames == 1 and rec.frames_watched == 0
    rec.on_frame("mf", {"Token": "O1", "LastRate": "not a number", "TotalQty": object()}, at("10:00:01"))
    assert rec.errors == 0 or rec.last_error
    assert broker_ts("/Date(-62135596800000)/") is None and broker_ts("/Date(1792038000000+0530)/") == 1792038000.0


def test_the_walls_oi_windows_today_then_the_previous_sessions_grids():
    rec = recorder()
    for hms, oi in (("09:16:00", 1000), ("09:45:00", 1100), ("10:15:00", 1100), ("10:46:00", 1300)):
        rec.on_frame("oi", {"Token": "O1", "OpenInterest": oi}, at(hms))
    prev = pd.DataFrame({"scrip_code": ["O1"] * 3, "ts": [at("09:16:00", "2026-10-16"), at("14:45:00", "2026-10-16"),
                                                           at("15:30:00", "2026-10-16")], "oi": [800.0, 850.0, 950.0]})
    assert rec.set_oi_grid("2026-10-16", prev) == 1
    w = rec.oi_windows("O1", at("10:45:00"), ["2026-10-16", "2026-10-15"])
    assert w[:3] == [(1100.0, 1100.0), (1100.0, 1100.0), (1000.0, 1100.0)], "today's bars before the candle, newest first"
    assert w[3] == (850.0, 950.0) and w[4] == (850.0, 850.0)
    assert R.oi_deltas(w) == [100.0, 100.0, 50.0], "today's 09:15 bar, then 16 Oct's 15:15 and 14:15 bars"
    assert len(w) == 3 + 13 + 13 and all(math.isnan(a) for a, _ in w[-13:])


# -- the reads' missing-data record ------------------------------------------------------------------------------


def _contracts() -> RR.Contracts:
    def o(code: str, k: float, cp: str) -> Instrument:
        return Instrument(code, "X", Segment.NSE_FO, InstrumentKind.OPTION, expiry="2026-10-27", strike=k,
                          option_type=OptionType(cp), underlying="X")
    legs: dict[str, Instrument | None] = {"CE1": o("C1", 110.0, "CE"), "CE2": o("C2", 120.0, "CE"),
                                          "PE1": o("P1", 100.0, "PE"), "PE2": o("P2", 90.0, "PE")}
    fut = Instrument("F", "X", Segment.NSE_FO, InstrumentKind.FUTURE, expiry="2026-10-27")
    eq = Instrument("E", "X", Segment.NSE_EQ, InstrumentKind.EQUITY)
    return RR.Contracts("X", "2026-10-27", legs, fut, eq)


def _feed_basic(rec: OptionRecorder, c0: float, s: float, *, skip: set[str] = frozenset()) -> None:  # type: ignore[assignment]
    for code, px in (("C1", 2.0), ("C2", 1.0), ("P1", 3.0), ("P2", 1.5)):
        if code in skip:
            continue
        rec.on_frame("mf", mf(code, px, px - 0.05, px + 0.05), c0 - 5)
        rec.on_frame("mf", mf(code, px * 1.1, px * 1.1 - 0.05, px * 1.1 + 0.05), s - 5)
        rec.on_frame("oi", {"Token": code, "OpenInterest": 10_000}, c0 - 30)
        rec.on_frame("oi", {"Token": code, "OpenInterest": 11_000}, s - 30)
    rec.on_frame("mf", mf("F", 105.0, 104.9, 105.1), c0 - 5)
    rec.on_frame("mf", mf("F", 105.5, 105.4, 105.6), s - 5)
    rec.on_frame("mf", mf("E", 104.0), c0 - 5)
    rec.on_frame("mf", mf("E", 104.5), s - 5)


def _read(rec: OptionRecorder, c0: float, s: float) -> dict[str, Any]:
    record, _, _ = RR.trigger_read(rec, _contracts(), signal_id="X-1", symbol="X", direction="Bullish", c0=c0, s=s,
                                   spot=104.5, atr=1.0, atr_src="regime.breadth", sessions=["2026-10-16", DAY], now=s + 10)
    return record


def test_a_leg_priced_at_the_signal_but_not_at_the_candle_start():
    rec = recorder({"C1": "opt", "C2": "opt", "P1": "opt", "P2": "opt", "F": "fut", "E": "eq"}, silence_s=None)
    c0, s = at("10:15:00"), at("10:45:05")
    _feed_basic(rec, c0, s, skip={"C2"})
    rec.on_frame("mf", mf("C2", 1.1, 1.05, 1.15), s - 5)  # first priced after the candle start
    r = _read(rec, c0, s)
    assert r["reads"]["PREMIUM"]["missing"] == ""
    assert r["legs"]["CE2"]["B"]["px"] is None and r["legs"]["CE2"]["X"]["px"] is not None
    assert math.isclose(r["reads"]["PREMIUM"]["ce"], 10.0), "one leg of a pair is enough for PREMIUM (CE1 +10 %)"


def test_one_leg_of_a_pair_still_reads_iv_and_premium_but_not_oi_or_vol():
    rec = recorder({"C1": "opt", "C2": "opt", "P1": "opt", "P2": "opt", "F": "fut", "E": "eq"}, silence_s=None)
    c0, s = at("10:15:00"), at("10:45:05")
    _feed_basic(rec, c0, s, skip={"C2"})
    r = _read(rec, c0, s)
    assert r["reads"]["OI"]["ce"] is None and "CE2 no OI print by 10:45:05" in r["reads"]["OI"]["missing"]
    assert r["reads"]["PREMIUM"]["ce"] is not None and r["reads"]["IV"]["ce"] is not None
    assert "CE2" in r["reads"]["VOL"]["missing"] and r["basis"].startswith("provisional")
    assert r["split"] == "-".join(str(len(r[k])) for k in ("with", "against"))
    assert "CE2" in r["legsNoPriceAtSignal"]


def test_the_previous_sessions_close_is_kept_beside_but_never_read_as_the_candles_oi():
    """No OI print today by a moment means the code was not streaming OI then — every OI subscription prints on
    subscribe (19 Oct: all 7,320 codes' first print came at boot) — so the previous close would read the change since
    yesterday as the candle's. It is kept beside (``oiPrev``), not read, and the read says so (review, 20 Oct)."""
    rec = recorder({"C1": "opt", "C2": "opt", "P1": "opt", "P2": "opt", "F": "fut", "E": "eq"}, silence_s=None)
    c0, s = at("10:15:00"), at("10:45:05")
    _feed_basic(rec, c0, s, skip={"P2"})
    rec.on_frame("mf", mf("P2", 1.5, 1.45, 1.55), c0 - 5)
    rec.on_frame("mf", mf("P2", 1.4, 1.35, 1.45), s - 5)
    rec.oi_prev["P2"] = 7_000.0
    r = _read(rec, c0, s)
    x, b = r["legs"]["PE2"]["X"], r["legs"]["PE2"]["B"]
    assert x["oiBasis"] == b["oiBasis"] == "prevday" and x["oi"] is None and x["oiPrev"] == 7_000.0
    assert r["reads"]["OI"]["pe"] is None and "OI" in r["missingReads"]
    assert "PE2 no OI print by 10:45:05 (previous close only)" in r["reads"]["OI"]["missing"]
    assert "PE2 no OI print by 10:15:00 (previous close only)" in r["reads"]["OI"]["missing"]
    assert r["reads"]["OI"]["ce"] is not None, "the other side reads as before"


def test_reading_the_hub_the_previous_close_says_the_hub_replays_no_oi():
    """Re-check, 20 Oct: the hub answers no OI from its pool, so on the twin a code that already streamed there before
    its recorder started prints only when its OI changes. 'previous close only' would say the code was not streaming;
    the twin's reason names the hub and when the recorder started."""
    rec = recorder({"C1": "opt", "C2": "opt", "P1": "opt", "P2": "opt", "F": "fut", "E": "eq"}, silence_s=None)
    rec.oi_on_subscribe = False  # the engine's hub role (``Engine._rec_owner_sync``)
    c0, s = at("10:15:00"), at("10:45:05")
    _feed_basic(rec, c0, s, skip={"P2"})
    rec.on_frame("mf", mf("P2", 1.5, 1.45, 1.55), c0 - 5)
    rec.on_frame("mf", mf("P2", 1.4, 1.35, 1.45), s - 5)
    rec.oi_prev["P2"] = 7_000.0
    r = _read(rec, c0, s)
    why = "previous close only; reading the hub, which replays no OI: none since this recorder started 10:14:55"
    assert f"PE2 no OI print by 10:45:05 ({why})" in r["reads"]["OI"]["missing"]
    assert r["legs"]["PE2"]["X"]["oi"] is None and r["legs"]["PE2"]["X"]["oiPrev"] == 7_000.0
    rec.started = at("23:50:00", "2026-10-16")
    assert RR.prev_close_only(rec).endswith("since this recorder started 16 Oct 23:50:00")


def test_a_name_read_through_its_front_future_has_no_eq_and_is_not_scored():
    """Re-check, 20 Oct: NIFTYNXT50 (and NIFTYFPI) have no cash or index code: the engine's underlying for them is the
    front future, and 19 Oct NIFTYNXT50 09:15 was scored with FUT0 = EQ and STOCK read on the future. score5 does not
    score it ("contracts missing"): EQ is None, so ``_read5_record`` excludes it with the reason."""
    c = _contracts()

    class Cat:
        def expiries(self, symbol: str, on: date) -> list[str]:
            return ["2026-10-27"]

        def chain(self, symbol: str, exp: str, cp: OptionType) -> list[Instrument]:
            return [i for i in c.legs.values() if i is not None and i.option_type is cp]

        def front_future(self, symbol: str, on: date) -> Instrument | None:
            return c.fut

    assert c.fut is not None and c.eq is not None
    got, why = RR.contracts(Cat(), "X", 104.5, D, c.fut)
    assert got is not None and why == "" and got.eq is None and got.fut == c.fut
    other_future = Instrument("F2", "X", Segment.NSE_FO, InstrumentKind.FUTURE, expiry="2026-11-24")
    assert RR.contracts(Cat(), "X", 104.5, D, other_future)[0].eq is None  # type: ignore[union-attr]
    kept, _ = RR.contracts(Cat(), "X", 104.5, D, c.eq)
    assert kept is not None and kept.eq == c.eq and list(kept.legs) == ["CE1", "CE2", "PE1", "PE2"]


def test_a_restart_says_the_baseline_before_it_is_not_on_record():
    """An engine restarted mid-candle: the candle-start prices and OI are before the recorder's first frame. They are
    missing, and the read says the recorder started then — never yesterday's close read as the candle's OI."""
    rec = recorder({"C1": "opt", "C2": "opt", "P1": "opt", "P2": "opt", "F": "fut", "E": "eq"}, silence_s=None)
    c0, s = at("10:15:00"), at("10:45:05")
    restart = at("10:30:00.5")
    for code, px in (("C1", 2.0), ("C2", 1.0), ("P1", 3.0), ("P2", 1.5)):
        rec.oi_prev[code] = 9_000.0
        rec.on_frame("oi", {"Token": code, "OpenInterest": 10_000}, restart)  # the resubscribe's snapshot
        rec.on_frame("mf", mf(code, px, px - 0.05, px + 0.05), restart)
    rec.on_frame("mf", mf("F", 105.0, 104.9, 105.1), restart)
    rec.on_frame("mf", mf("E", 104.0), restart)
    assert rec.started == restart
    r = _read(rec, c0, s)
    assert r["reads"]["OI"]["ce"] is None and r["reads"]["PREMIUM"]["ce"] is None and r["reads"]["IV"]["ce"] is None
    assert "CE1 no OI print by 10:15:00 (recorder started 10:30:00 (restart))" in r["reads"]["OI"]["missing"]
    assert "CE1 not priced at the baseline (recorder started 10:30:00 (restart))" in r["reads"]["PREMIUM"]["missing"]
    assert "recorder started 10:30:00 (restart)" in r["reads"]["IV"]["missing"]
    assert r["legs"]["CE1"]["X"]["oi"] == 10_000.0, "the moment after the restart reads as ever"


def test_an_iv_below_intrinsic_is_missing_with_its_reason():
    rec = recorder({"C1": "opt", "C2": "opt", "P1": "opt", "P2": "opt", "F": "fut", "E": "eq"}, silence_s=None)
    c0, s = at("10:15:00"), at("10:45:05")
    _feed_basic(rec, c0, s, skip={"P1", "P2"})
    for code in ("P1", "P2"):
        rec.on_frame("mf", mf(code, 0.5, 0.45, 0.55), c0 - 5)   # far below the 100 / 90 puts' worth? they are OTM: fine
    rec.on_frame("mf", mf("F", 80.0, 79.9, 80.1), s - 4)       # the future crashes: the puts are now deep ITM
    for code in ("P1", "P2"):
        rec.on_frame("mf", mf(code, 1.0, 0.95, 1.05), s - 3)
    r = _read(rec, c0, s)
    assert r["legs"]["PE1"]["X"]["ivWhy"] == "below_intrinsic" and r["reads"]["IV"]["pe"] is None
    assert "PE1 no IV at 10:45:05 (below_intrinsic)" in r["reads"]["IV"]["missing"]


def test_an_unverified_vol_baseline_bar_is_named():
    rec = recorder({"C1": "opt", "C2": "opt", "P1": "opt", "P2": "opt", "F": "fut", "E": "eq"}, silence_s=None)
    c0, s = at("12:45:00"), at("13:15:05")
    _feed_basic(rec, c0, s)
    for code in ("C1", "C2", "P1", "P2"):
        rec.on_frame("mf", mf(code, 9.0, TotalQty=0, TickDt=dt(at("15:29:00", "2026-10-16"))), at("09:00:00"))
    bars = [{"scrip_code": c, "bucket_ts": at(b), "v": 5000.0, "verified": not (c == "P2" and b == "10:45"), "from_ts": at("09:00:00")}
            for c in ("C1", "C2", "P1", "P2") for b in ("09:15", "09:45", "10:15", "10:45", "11:15", "11:45")]
    rec.load_bars(bars)
    r = _read(rec, c0, s)
    assert r["legs"]["CE1"]["normal"] == 5000.0 and r["reads"]["VOL"]["ce"] is not None
    assert r["reads"]["VOL"]["pe"] is None and f"PE2 baseline bar {DAY} 10:45 not verified" in r["reads"]["VOL"]["missing"]


def test_the_card_text_says_the_split_the_call_and_the_walls_and_a_restart():
    trig = {"signal_id": "X", "s": at("09:45:05"), "split": "3-1", "with": ["OI", "PREMIUM", "STOCK"], "against": ["IV"],
            "noVote": ["VOL"], "basis": "provisional 4-read: VOL not recorded",
            "reads": {"VOL": {"missing": f"CE1 baseline bar {DAY} 09:15 not recorded"}},
            "walls": {"ce": {"strike": 1300.0, "state": "SHORT BUILD-UP", "interp": "Bearish", "oiChg": 2.0, "pxChg": -3.0},
                      "pe": {"strike": 1200.0, "state": "FLAT", "interp": "Neutral", "oiChg": 0.2, "pxChg": None}, "overall": "Bearish"}}
    lines = RR.card_lines(trig, None, now=at("10:30:00"))
    assert lines[0].startswith("5-read at trigger 3-1 with (OI, PREMIUM, STOCK) · against IV · no vote VOL (CE1 baseline bar")
    assert lines[1] == "follow-up interrupted (restart)"
    assert lines[2].startswith("walls CE 1300 SHORT BUILD-UP → Bearish") and lines[2].endswith("overall Bearish")
    follow = {"complete": True, "f3": {"call": "Bullish", "time": "09:47:05", "minute": 2, "label": "Moderate", "vsEngine": "with engine"}}
    assert RR.card_lines(trig, follow, now=at("10:30:00"))[1] == "first 3 of 5: Bullish 09:47:05 (+2 min, Moderate) — with engine"
    assert RR.card_lines({"excluded": "15:15 auction candle"}, None, now=0.0) == ["5-read not taken: 15:15 auction candle"]


def test_the_flat_archive_row_mirrors_score5s_columns(tmp_path: Path):
    rec = recorder({"C1": "opt", "C2": "opt", "P1": "opt", "P2": "opt", "F": "fut", "E": "eq"}, silence_s=None)
    c0, s = at("10:15:00"), at("10:45:05")
    _feed_basic(rec, c0, s)
    r = _read(rec, c0, s)
    row = RR.flat_row(r, None)
    for col in ("t_split", "t_reads_with", "t_vote_OI", "t_OI_CE", "t_stock_atr", "reads_nan_at_trigger", "CE1_code", "wall_overall"):
        assert col in row
    a = DailyArchive(tmp_path)
    a.record5("read5", s, row)
    a.record5("read5", s, RR.flat_row(r, {"complete": True, "f3": None, "wallsAtF3": None}))
    a.flush()
    back = pd.read_parquet(tmp_path / "read5" / f"{DAY}.parquet")
    assert len(back) == 1 and back.f3_label.iloc[0] == "Skip", "the follow-up's row replaces the trigger's"


# -- review, 20 Oct ----------------------------------------------------------------------------------------------


def _open_read(rec: OptionRecorder, s: float) -> dict[str, Any]:
    record, _, _ = RR.trigger_read(rec, _contracts(), signal_id="X-1", symbol="X", direction="Bullish", c0=at("09:15:00"),
                                   s=s, spot=104.5, atr=1.0, atr_src="regime.breadth", sessions=["2026-10-16", DAY], now=s + 10)
    return record


def test_a_first_frame_just_before_the_signal_is_not_the_0915_open():
    """19 Oct MAHABANK CE2: its first frame of the day came at 09:45:05.339, 63 ms before the signal — a subscription
    snapshot, the state then, not the open. A leg not streaming by 09:15 has no opening price, and says why; a leg
    streaming since its boot snapshot keeps the first print at or after 09:15 as before."""
    rec = recorder({"C1": "opt", "C2": "opt", "P1": "opt", "P2": "opt", "F": "fut", "E": "eq"}, silence_s=None)
    s = at("09:45:05.402")
    for code, px in (("C1", 2.0), ("P1", 3.0), ("P2", 1.5)):
        rec.on_frame("mf", mf(code, px, px - 0.05, px + 0.05), at("00:45:10"))  # the boot snapshot
        rec.on_frame("mf", mf(code, px * 1.1, px * 1.1 - 0.05, px * 1.1 + 0.05), at("09:15:20"))
        rec.on_frame("mf", mf(code, px * 1.2, px * 1.2 - 0.05, px * 1.2 + 0.05), s - 5)
    rec.on_frame("mf", mf("C2", 1.8, 1.75, 1.85), at("09:45:05.339"))  # subscribed late: lands 63 ms before s
    for code in ("F", "E"):
        rec.on_frame("mf", mf(code, 104.0, 103.9, 104.1), at("00:45:10"))
        rec.on_frame("mf", mf(code, 104.2, 104.1, 104.3), at("09:15:01"))
    assert rec.first_frame("C2") == at("09:45:05.339") and rec.first_after_open("C2")[0] < s
    r = _open_read(rec, s)
    b2 = r["legs"]["CE2"]["B"]
    assert b2["px"] is None and b2["pxWhy"] == "not streaming at 09:15 (first frame 09:45:05)"
    assert r["legs"]["CE2"]["B"]["ivWhy"] == b2["pxWhy"] and r["legs"]["CE2"]["X"]["px"] is not None
    b1 = r["legs"]["CE1"]["B"]
    assert b1["pxSrc"] == "first print" and math.isclose(b1["px"], 2.2), "streaming since boot: its 09:15:20 print"
    assert math.isclose(r["reads"]["PREMIUM"]["ce"], 100 * (2.4 / 2.2 - 1)), "CE from CE1 alone, never CE2's late snapshot"
    assert r["stock"]["B"] == 104.2


def test_a_wall_strike_not_streaming_at_0915_has_no_opening_premium():
    rec = recorder({"W1": "opt", "W2": "opt"}, silence_s=None)
    c = _contracts()
    w_early = Instrument("W1", "X", Segment.NSE_FO, InstrumentKind.OPTION, expiry="2026-10-27", strike=130.0,
                         option_type=OptionType.CE, underlying="X")
    w_late = Instrument("W2", "X", Segment.NSE_FO, InstrumentKind.OPTION, expiry="2026-10-27", strike=140.0,
                        option_type=OptionType.CE, underlying="X")
    for code in ("W1", "W2"):
        rec.on_frame("oi", {"Token": code, "OpenInterest": 50_000}, at("00:45:00"))
        rec.on_frame("oi", {"Token": code, "OpenInterest": 52_000}, at("09:40:00"))
    rec.on_frame("mf", mf("W1", 1.0, 0.95, 1.05), at("09:14:00"))  # the band's candidates, a minute before the bar
    rec.on_frame("mf", mf("W1", 1.1, 1.05, 1.15), at("09:15:30"))
    rec.on_frame("mf", mf("W1", 0.9, 0.85, 0.95), at("09:45:00"))
    rec.on_frame("mf", mf("W2", 2.0, 1.95, 2.05), at("09:20:00"))  # first streamed at 09:20
    rec.on_frame("mf", mf("W2", 1.8, 1.75, 1.85), at("09:45:00"))
    x = at("09:45:05")
    early = RR.wall_at(rec, "CE", w_early, x, at("09:15:00"), [])
    late = RR.wall_at(rec, "CE", w_late, x, at("09:15:00"), [])
    assert early["px0Src"] == "first print" and math.isclose(early["pxChg"], 100 * (0.9 / 1.1 - 1))
    assert late["pxChg"] is None and late["px0Src"] == "not streaming at 09:15 (first frame 09:20:00)"
    assert c.legs  # the contracts helper stays as the other tests use it


def test_the_future_a_few_ms_after_the_options_snapshot_stands_in_for_its_iv():
    """19 Oct HDFCBANK CE1: the option's price time is its subscription snapshot (09:03:22.552), the future's first
    frame of the same burst came 48 ms later — no future at or before the option's price time. The future's first
    quote within 5 s after stands in, said in its source; never one after the moment read."""
    rec = recorder({"C1": "opt", "F": "fut"}, silence_s=None)
    c = _contracts()
    rec.on_frame("mf", mf("C1", 12.0), at("09:03:22.552"))
    rec.on_frame("mf", mf("F", 104.0, 103.9, 104.1), at("09:03:22.600"))
    snap = RR.leg_snap(rec, c, c.legs["CE1"], at("09:45:05"))  # type: ignore[arg-type]
    assert snap["pxSrc"] == "ltp" and snap["pxT"] == at("09:03:22.552")
    assert snap["FSrc"] == "mid first after (+0.048 s)" and snap["iv"] is not None and math.isfinite(snap["iv"])
    F, _src, _ft = RR.fwd_at(rec, c.fut, at("09:03:22.552"))
    assert math.isnan(F), "without the stand-in (the snapshot's own future read) nothing changes"
    early = RR.leg_snap(rec, c, c.legs["CE1"], at("09:03:22.560"))  # type: ignore[arg-type]
    assert early["ivWhy"] == "no future price at the option's price time", "never a future after the moment read"


def test_the_iv_stand_in_survives_the_futures_ring_being_trimmed_and_compacted():
    """Re-check, 20 Oct: the stand-in read the ring, else each minute's LAST state. Once the future's ring was compacted
    past the option's 09:03:22 price time, a future with six frames in 09:03 had no state before 09:03:50 — an 11:00
    read found no future. Each minute's first state is kept beside its last: the 09:45 and 11:00 reads agree."""
    rec = recorder({"C1": "opt", "F": "fut"}, silence_s=None)
    c = _contracts()
    rec.on_frame("mf", mf("C1", 12.0), at("09:03:22.552"))  # the option's carried pre-open price, never traded again
    for k, t in enumerate(("09:03:22.600", "09:03:28", "09:03:35", "09:03:41", "09:03:47", "09:03:50")):
        rec.on_frame("mf", mf("F", 104.0 + k * 0.1, 103.9 + k * 0.1, 104.1 + k * 0.1), at(t))
    t, k = at("09:04:00"), 0
    while t < at("11:00:00"):  # the future streams; housekeeping trims every 30 s
        k += 1
        rec.on_frame("mf", mf("F", 104.0 + (k % 50) * 0.05, 103.9 + (k % 50) * 0.05, 104.1 + (k % 50) * 0.05), t)
        if k % 30 == 0:
            rec.trim(t)
        t += 1.0
    assert rec._ring["F"].trimmed and rec._ring["F"].t[0] > at("09:30:00"), "compacted past the option's price time"
    got = [RR.leg_snap(rec, c, c.legs["CE1"], at(x)) for x in ("09:45:05", "11:00:00")]  # type: ignore[arg-type]
    for snap in got:
        assert snap["FSrc"] == "mid@minute first after (+0.048 s)" and snap["F"] == 104.0, snap
        assert math.isfinite(snap["iv"])
    assert got[0]["iv"] == got[1]["iv"]
    q = rec.quote("F", at("09:03:38"))
    assert q is not None and q["eff"] == at("09:03:22.600") and q["src"] == "minute", "inside a minute: its first state"
    assert rec.quote("F", at("09:03:51"))["eff"] == at("09:03:50"), "after the minute's last: the last"
    assert rec.first_after("F", at("09:03:30"), 5.0) is None, "inside a minute the next state is past 5 s: none"


def test_stats_keep_todays_bar_counts_without_walking_the_bars():
    """Every /api/health call reads ``stats()`` on the trading loop (review, 20 Oct: 39-67 ms a call walking 87.5k
    bars): the counts are kept as bars close and load, and match a full walk."""
    import time as _time

    rec = recorder(silence_s=None)
    bars = [{"scrip_code": f"O{i}", "bucket_ts": int(at(b, d)), "v": 100.0, "verified": (i % 3 != 0), "from_ts": None}
            for i in range(3500) for d in ("2026-10-16", DAY) for b in ("09:15", "09:45", "10:15", "10:45", "11:15", "11:45",
                                                                         "12:15", "12:45", "13:15", "13:45", "14:15", "14:45", "15:15")][:91_000]
    rec.merge_bars(RR_parse(bars))
    walk_today = [v for k, v in rec._b30.items() if k[1] == DAY]
    st = rec.stats()
    assert st["bars_closed_today"] == len(walk_today) > 0
    assert st["bars_verified_today"] == sum(1 for v in walk_today if v[1])
    t0 = _time.perf_counter()
    for _ in range(20):
        rec.stats()
    assert (_time.perf_counter() - t0) / 20 < 0.005, "well under the 39 ms a walk took"
    rec.on_frame("mf", mf("O1", 1.0, TotalQty=0, TickDt=dt(at("15:29:00", "2026-10-16"))), at("09:08:00"))
    rec.on_frame("mf", mf("O1", 1.0, TotalQty=10, TickDt=dt(at("15:20:00"))), at("15:20:01"))
    before = rec.stats()["bars_closed_today"]
    rec.close_bars(at("16:00:00"))
    walk_today = [v for k, v in rec._b30.items() if k[1] == DAY]
    assert rec.stats()["bars_closed_today"] == len(walk_today) and len(walk_today) >= before
    assert rec.stats()["bars_verified_today"] == sum(1 for v in walk_today if v[1])
    rec.roll(date(2026, 10, 20))
    assert rec.stats()["bars_closed_today"] == 0 and rec.stats()["bars_verified_today"] == 0


def RR_parse(rows: list[dict[str, Any]]) -> dict[Any, Any]:
    from kotsin_nse.ops.optrec import parse_bars

    return parse_bars(rows)


def test_the_history_is_built_off_the_loop_and_merged_the_same_as_before():
    """``parse_bars`` / ``oi_grid`` run in the worker thread; ``merge_bars`` keeps a bar already held, as
    ``load_bars`` always did."""
    from kotsin_nse.ops.optrec import oi_grid, parse_bars

    rows = [{"scrip_code": "O1", "bucket_ts": int(at("09:15")), "v": 5.0, "verified": True, "from_ts": at("09:00")},
            {"scrip_code": "O1", "bucket_ts": int(at("09:15")), "v": 7.0, "verified": False, "from_ts": float("nan")}]
    parsed = parse_bars(rows)
    assert parsed == {("O1", DAY, "09:15"): (5.0, True, at("09:00"))}, "the first row of a key stands"
    rec = recorder()
    rec._put_bar(("O1", DAY, "09:15"), (9.0, False, None))
    assert rec.merge_bars(parsed) == 0 and rec._b30[("O1", DAY, "09:15")] == (9.0, False, None)
    prev = pd.DataFrame({"scrip_code": ["O1", "O1"], "ts": [at("09:16:00", "2026-10-16"), at("15:30:00", "2026-10-16")],
                         "oi": [800.0, 950.0]})
    old = recorder()
    old.set_oi_grid("2026-10-16", prev)
    assert oi_grid("2026-10-16", prev) == old.oi_grid_prev


def test_a_gap_seen_twice_is_one_gap():
    rec = recorder()
    rec.note_gap(at("10:00:00"), at("10:00:20"), "hub broker socket down")
    rec.note_gap(at("09:59:58"), at("10:00:21"), "feed reconnect")
    rec.note_gap(at("11:00:00"), at("11:00:05"), "feed reconnect")
    assert rec.gaps == [(at("09:59:58"), at("10:00:21"), "hub broker socket down"), (at("11:00:00"), at("11:00:05"), "feed reconnect")]


def test_the_minute_volumes_are_written_by_the_sockets_owner_alone_and_never_below_the_disk_floor(tmp_path):
    rows = [("O1", int(at("09:20:00")), 300.0, 300.0)]
    twin = VolumeTape(tmp_path / "twin")
    twin.enabled = False
    assert twin.write(rows) == 0 and twin.rows_skipped == 1 and not (tmp_path / "twin").exists()
    full = VolumeTape(tmp_path / "full", min_free_gb=1e9)  # more free space than any disk has
    assert full.write(rows) == 0 and full.rows_skipped == 1 and "GB free" in full.skipped_why
    owner = VolumeTape(tmp_path / "owner")
    assert owner.write(rows, now=at("09:23:02")) == 1 and owner.bytes_written > 0 and owner.stats()["enabled"]
    assert VolumeTape(tmp_path / "x").keep_days == 30


def test_the_detail_file_keeps_the_whole_record_and_the_ledger_a_compact_copy(tmp_path):
    from kotsin_nse.ops.optrec import DetailLog, read_detail_day

    rec = recorder({"C1": "opt", "C2": "opt", "P1": "opt", "P2": "opt", "F": "fut", "E": "eq"}, silence_s=None)
    c0, s = at("10:15:00"), at("10:45:05")
    _feed_basic(rec, c0, s)
    r = _read(rec, c0, s)
    r["walls"] = {"spot": 104.5, "pickedAt": "10:45:15", "moment": "10:45:05", "overall": "Bullish",
                  "ce": {"strike": 130.0, "code": "W1", "state": "SHORT BUILD-UP", "interp": "Bearish", "oiChg": 2.0,
                         "pxChg": -3.0, "oicp": 160.0, "significant": True, "oi0": 1.0, "oi1": 2.0, "p0": 1.0, "p1": 0.9,
                         "window": "10:15:05-10:45:05"}, "pe": None}
    small = RR.compact_trigger(r)
    assert len(json.dumps(small)) < len(json.dumps(r)) / 2 and len(json.dumps(small)) < 3_000
    assert small["split"] == r["split"] and small["reads"] == r["reads"] and small["legs"]["CE1"] == {"code": "C1", "strike": 110.0, "type": "CE"}
    assert small["walls"]["ce"]["state"] == "SHORT BUILD-UP" and "p0" not in small["walls"]["ce"] and "stage0" not in small
    assert RR.card_lines(small, None, now=s + 60) == RR.card_lines(r, None, now=s + 60), "the card reads the same"
    follow = {"signal_id": "X-1", "symbol": "X", "f2": None, "f3": None, "f4": None, "rows": [r["stage0"]] * 16,
              "wallsAtF3": None, "complete": True, "stoppedAt": "11:01:05"}
    assert RR.compact_follow(follow) == {"signal_id": "X-1", "symbol": "X", "f2": None, "f3": None, "f4": None,
                                         "complete": True, "stoppedAt": "11:01:05", "wallsAtF3": None, "stages": 16}
    log = DetailLog(tmp_path / "read5_detail", keep_days=2)
    (tmp_path / "read5_detail").mkdir()
    (tmp_path / "read5_detail" / "2026-10-10.jsonl").write_text("{}\n")
    assert log.write("read5.trigger", s, r) and log.write("read5.follow", s, follow)
    back = read_detail_day(tmp_path / "read5_detail", DAY)
    assert [b["kind"] for b in back] == ["read5.trigger", "read5.follow"] and back[1]["record"]["rows"][0]["m"] == 0
    assert not (tmp_path / "read5_detail" / "2026-10-10.jsonl").exists(), "older than keep_days: pruned"
