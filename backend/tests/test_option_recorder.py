"""The option recorder (``ops/optrec.py``) — RECORD-ONLY: the as-of quotes, OI and per-minute option volume the 5-read
reads, and what it archives. Every read is as of a moment; a stretch it cannot vouch for says so.
"""

from __future__ import annotations

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


def test_the_previous_sessions_oi_stands_in_and_says_so():
    rec = recorder({"C1": "opt", "C2": "opt", "P1": "opt", "P2": "opt", "F": "fut", "E": "eq"}, silence_s=None)
    c0, s = at("10:15:00"), at("10:45:05")
    _feed_basic(rec, c0, s, skip={"P2"})
    rec.on_frame("mf", mf("P2", 1.5, 1.45, 1.55), c0 - 5)
    rec.on_frame("mf", mf("P2", 1.4, 1.35, 1.45), s - 5)
    rec.oi_prev["P2"] = 7_000.0
    r = _read(rec, c0, s)
    assert r["legs"]["PE2"]["X"]["oiBasis"] == "prevday" and r["legs"]["PE2"]["B"]["oiBasis"] == "prevday"
    assert r["reads"]["OI"]["pe"] is not None


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
