"""The 5-read and the strike walls, recorded live (record-only) — reproduced against the research that defined them.

Operator, 2026-10-19: "yes - the engine should start recording the 5-read and strike-wall state live, for research
only, so CT-B1 and later B2 can use them". The fixture (``fixtures/read5_research_rows.json``) holds the raw inputs of
six research-scored triggers — the quote rows the research read touched, the legs' OI prints, their broker 1m volumes
and VOL baseline bars — extracted once with the research's own helpers (staging/next_record5/measure/
extract_read5_fixture.py), and the research's own outputs. They are fed through the engine's recorder
(``ops/optrec.py``) as feed frames and read with ``ops/read5rec.py`` exactly as the live engine reads them; every
read, vote, split, lead, the first 3 of 5 and its label must come out as in ``score5_all.csv``.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind, OptionType
from kotsin_nse.market.session import IST
from kotsin_nse.ops import read5rec as RR
from kotsin_nse.ops.optrec import OptionRecorder
from kotsin_nse.research import read5 as R

FIX = json.loads((Path(__file__).parent / "fixtures" / "read5_research_rows.json").read_text())
TRIG = {f"{t['day']}|{t['symbol']}|{t['start']}": t for t in FIX["triggers"]}
PREV = {"2026-10-15": "2026-10-14", "2026-10-19": "2026-10-16"}


def _ms(t: float) -> str:
    return f"/Date({int(t * 1000)})/"


def contracts_of(t: dict[str, Any]) -> RR.Contracts:
    legs = {k: Instrument(v["code"], t["symbol"], Segment.NSE_FO, InstrumentKind.OPTION, expiry=v["expiry"],
                          strike=float(v["strike"]), option_type=OptionType(k[:2]), underlying=t["symbol"])
            for k, v in t["legs"].items()}
    fut = Instrument(t["fut"]["code"], t["symbol"], Segment.NSE_FO, InstrumentKind.FUTURE, expiry=t["fut"]["expiry"])
    eq = Instrument(t["eq"], t["symbol"], Segment.NSE_EQ, InstrumentKind.EQUITY)
    return RR.Contracts(symbol=t["symbol"], expiry=next(iter(t["legs"].values()))["expiry"], legs=legs, fut=fut, eq=eq)  # type: ignore[arg-type]


def replay(t: dict[str, Any]) -> OptionRecorder:
    """The research's inputs as the feed would have carried them: quote frames at their tape times, OI frames at
    their archive times, one volume frame a minute (the broker's running total), the baseline bars as archived."""
    rec = OptionRecorder(silence_s=None)
    day = date.fromisoformat(t["day"])
    rec.roll(day)
    c = contracts_of(t)
    rec.set_watch({**{v["code"]: "opt" for v in t["legs"].values()}, t["fut"]["code"]: "fut", t["eq"]: "eq"})
    rec.oi_prev.update(t["oi_prev"])
    frames: list[tuple[float, str, dict[str, Any]]] = []
    for code, rows in t["tape"].items():
        for ts, ltp, bid, ask in rows:
            frames.append((ts, "mf", {"Token": code, "LastRate": ltp, "BidRate": bid, "OffRate": ask}))
    for code, rows in t["oi"].items():
        for ts, oi in rows:
            frames.append((ts, "oi", {"Token": code, "OpenInterest": oi}))
    yday = t["c0"] - 86_400
    for code, v in t["vol"].items():
        if not v["ok"]:
            continue
        # the first frame says no trade today (its last trade is yesterday's): the reference is 0, covered from here
        frames.append((t["c0"] - 600, "mf", {"Token": code, "LastRate": 0, "TotalQty": 0, "TickDt": _ms(yday)}))
        cum = 0.0
        for m, vol in v["minutes"]:
            cum += vol
            frames.append((m + 30, "mf", {"Token": code, "LastRate": 0, "TotalQty": cum, "TickDt": _ms(m + 30)}))
    frames.sort(key=lambda f: f[0])
    for ts, ch, row in frames:
        rec.on_frame(ch, row, ts)
    bars = []
    for code, v in t["vol"].items():
        for d, b, vol in v["baseline"]:
            a = R.at_ist(d, b)
            bars.append({"scrip_code": code, "bucket_ts": a, "v": vol if vol is not None else 0.0, "verified": vol is not None, "from_ts": None})
    rec.load_bars(bars)
    assert rec.errors == 0, rec.last_error
    assert c.legs
    return rec


def run_read(t: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    rec = replay(t)
    c = contracts_of(t)
    sessions = [PREV[t["day"]], t["day"]]
    record, X, b30 = RR.trigger_read(rec, c, signal_id="sid", symbol=t["symbol"], direction=t["direction"], c0=t["c0"],
                                     s=t["s"], spot=t["S"], atr=t["atr"], atr_src=t["atr_src"], sessions=sessions,
                                     now=t["s"] + 10)
    stages = [record["stage0"]] + [RR.follow_stage(rec, c, s=t["s"], m=m, B=X, b30=b30, atr=t["atr"]) for m in range(1, 17)]
    return record, stages, RR.first_calls(stages, t["direction"])


def close(a: Any, b: Any, tol: float = 1e-9) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(float(a), float(b), rel_tol=tol, abs_tol=tol)


def hms(x: float) -> str:
    return datetime.fromtimestamp(x, IST).strftime("%H:%M:%S")


# -- the arithmetic, on the research's own snapshots ------------------------------------------------------------------

KEYS15 = ["2026-10-15|EICHERMOT|10:15", "2026-10-15|NATIONALUM|09:15", "2026-10-15|LICI|09:15", "2026-10-15|TRENT|09:15"]


def _nan(v: Any) -> float:
    return math.nan if v is None else float(v)


def _snap(d: dict[str, Any]) -> dict[str, Any]:
    return {"stock": _nan(d["stock"]), **{k: {f: _nan(v[f]) for f in ("px", "oi", "iv")} for k, v in d.items() if k != "stock"}}


@pytest.mark.parametrize("key", KEYS15)
def test_each_reads_arithmetic_on_the_research_snapshots(key):
    """``read5.reads`` / ``decide`` / ``first_n`` / ``label`` fed the research's own snapshot of every stage (each
    leg's price, OI and IV at the moment and at the baseline, the window volumes and baselines): every read's CE and
    PE value, the stock move, every vote and lead of all 17 stages as the research timeline, to 1e-12; then the first
    3 of 5 and its label as ``score5_all.csv``."""
    t = TRIG[key]
    timeline = []
    for st, exp in zip(t["expected"]["research_stages"], t["expected"]["stages"], strict=True):
        vol = {k: (_nan(v[0]), _nan(v[1])) for k, v in st["vol"].items()}
        r, mv, votes = R.reads(_snap(st["X"]), _snap(st["B"]), vol, t["atr"])
        for read in ("OI", "VOL", "IV", "PREMIUM"):
            for i, side in enumerate(("CE", "PE")):
                assert close(r[read][i], exp[f"{read}_{side}"], 1e-12), (key, st["m"], read, side, r[read][i], exp[f"{read}_{side}"])
        assert close(mv, exp["stock_atr"], 1e-12)
        assert votes == {k: exp[f"vote_{k}"] for k in R.READS}, (key, st["m"])
        assert (R.decide(votes)[3] or "") == exp["lead"]
        timeline.append((votes, R.stage_row(r, mv, votes)))
    sc = t["expected"]["score5"]
    w, a, n = R.split(timeline[0][0], t["direction"])
    assert (f"{len(w)}-{len(a)}", ", ".join(w), ", ".join(a), ", ".join(n)) == (
        sc["t_split"], sc["t_reads_with"] or "", sc["t_reads_against"] or "", sc["t_reads_no_vote"] or "")
    i, call, _ = R.first_n([v for v, _ in timeline], 3)
    lab = R.label(timeline[i][1], call)
    assert (call, i, lab["vote_split"], lab["reads_for"], lab["label"], lab["weak_because"], lab["thin_for"]) == (
        sc["f3_call"], int(sc["f3_minute"]), sc["f3_split"], sc["f3_reads_for"], sc["f3_label"], sc["f3_weak_because"] or "",
        sc["f3_thin"] or "")


# -- the research rows through the live recorder -------------------------------------------------------------------

#: IV only: the research tape is the union of both engines' 1 Hz samplers, so a quote's time there can be the other
#: engine's sample of the same change, up to a second later than the change itself (EICHERMOT 10:15 CE1 at the candle
#: start: 10:14:51.44 here, 10:14:52.00 on the union) — a sub-second time to expiry: 55 of the 136 IV reads of these
#: four triggers differ by 1.1e-6 … 1.2e-5 vol points. Twice that second also moves the future the IV is matched with
#: across its 60 s freshness line (EICHERMOT +1 min CE2: the union's 10:45:55.000 row of 89744 is matched with a
#: future row of the same second; here the price is 10:45:54.145 and the future's last row is 10:44:38, a last trade
#: — IV −0.034 against −0.081; +5 min PE2: the union's 10:49:40.000 row, matched with the future's 10:49:40.000 row
#: (mid 7,216.75); here 10:49:39.276 and the future's 10:49:37.049 (mid 7,215.50) — the PE side +0.156 against +0.190,
#: which turns that minute's IV vote from BEAR to BULL, and its lead from none to Bullish). Of the 340 votes of these
#: four triggers that one differs; it moves no first 2, 3 or 4 of 5 (all at the trigger) and no label.
IV_TOL = 1e-4
IV_TOL_FUTURE = 0.05
#: the one vote the union tape's second sampler changes (see above)
UNION_VOTE = {("2026-10-15|EICHERMOT|10:15", 5): {"IV": ("BULL", "BEAR")}}
UNION_LEAD = {("2026-10-15|EICHERMOT|10:15", 5): ("Bullish", "")}


@pytest.mark.parametrize("key", KEYS15)
def test_every_stage_of_a_research_trigger_reads_the_same_through_the_recorder(key):
    """All 17 stages (the trigger and +1 … +16 min) read live: the research's quote rows, OI prints and minute volumes
    fed to the recorder as feed frames, read as of each moment — OI, VOL, PREMIUM and the stock move to 1e-9, IV to
    ``IV_TOL``; every vote and the lead identical (oct15/multi/live_2026-10-15.pkl, VOL from broker 1m candles)."""
    t = TRIG[key]
    _, stages, _ = run_read(t)
    wide = []
    for got, exp in zip(stages, t["expected"]["stages"], strict=True):
        assert got["t"] == exp["time"], (key, got["m"])
        for read in ("OI", "VOL", "IV", "PREMIUM"):
            for i, side in enumerate(("CE", "PE")):
                a, b = got["r"][read][i], exp[f"{read}_{side}"]
                if read == "IV" and a is not None and b is not None and not close(a, b, IV_TOL):
                    wide.append((got["m"], side))
                    assert abs(a - b) <= IV_TOL_FUTURE, (key, got["m"], side, a, b)
                    continue
                assert close(a, b, IV_TOL if read == "IV" else 1e-9), (key, got["m"], read, side, a, b)
        assert close(got["mv"], exp["stock_atr"], 1e-12), (key, got["m"])
        diff = {k: (got["votes"][k], exp[f"vote_{k}"]) for k in R.READS if got["votes"][k] != exp[f"vote_{k}"]}
        assert diff == UNION_VOTE.get((key, got["m"]), {}), (key, got["m"], diff)
        assert (got["lead"], exp["lead"]) == UNION_LEAD.get((key, got["m"]), (exp["lead"], exp["lead"])), (key, got["m"])
    assert wide == ([(1, "CE"), (5, "PE")] if key.endswith("EICHERMOT|10:15") else []), wide


@pytest.mark.parametrize("key", KEYS15)
def test_the_score5_row_at_the_trigger_and_the_first_3_of_5(key):
    """buckets_oct19/score5_all.csv: the trigger's split against the engine's direction, with / against / no-vote, the
    lead, the reads' values; the first 3 of 5 — its call, time, minute, split, reads, label, why weak, thin votes."""
    t = TRIG[key]
    sc = t["expected"]["score5"]
    record, _, calls = run_read(t)
    assert record["direction"] == sc["engine_direction"]
    assert record["split"] == sc["t_split"]
    assert ", ".join(record["with"]) == (sc["t_reads_with"] or "")
    assert ", ".join(record["against"]) == (sc["t_reads_against"] or "")
    assert ", ".join(record["noVote"]) == (sc["t_reads_no_vote"] or "")
    assert record["lead"] == sc["t_lead"] and record["leadVsEngine"] == sc["t_lead_vs_engine"]
    for k in R.READS:
        assert record["votes"][k] == sc[f"t_vote_{k}"]
    for k in ("OI", "VOL", "IV", "PREMIUM"):
        tol = IV_TOL if k == "IV" else 1e-6
        assert close(record["reads"][k]["ce"], sc[f"t_{k}_CE"], tol) and close(record["reads"][k]["pe"], sc[f"t_{k}_PE"], tol)
    assert close(record["reads"]["STOCK"]["move"], sc["t_stock_atr"], 1e-6)
    assert record["basis"] == "5-read" and not record["missingReads"]
    f3 = calls["f3"]
    assert f3["call"] == sc["f3_call"] and f3["time"] == sc["f3_time"] and f3["minute"] == int(sc["f3_minute"])
    assert f3["split"] == sc["f3_split"] and f3["readsFor"] == sc["f3_reads_for"]
    assert f3["readsAgainst"] == (sc["f3_reads_against"] or "")
    assert f3["label"] == sc["f3_label"] and f3["weakBecause"] == (sc["f3_weak_because"] or "")
    assert f3["thin"] == (sc["f3_thin"] or "")
    assert f3["vsEngine"] == ("with engine" if sc["f3_call"] == sc["engine_direction"] else "against engine")
    exp = t["expected"]
    assert close(calls["f3"]["x"], exp["f3_x"]) and calls["f3"]["call"] == exp["f3_trend"]
    assert (calls["f4"] or {}).get("call", "") == exp["f4_trend"]
    assert close((calls["f4"] or {}).get("x"), exp["f4_x"])
    assert close(calls["f2"]["x"], exp["entry_x"]) and calls["f2"]["call"] == exp["entry_trend"]


def test_iv76_reproduces_the_research_legs():
    """``iv76`` on the research's own inputs (the price, the future matched to its time, the time to expiry from that
    moment): every leg's IV at the signal, the candle start and the 09:15 first print as ``ivlegs_2026-10-15.parquet``'s
    ``iv_new``, to 1e-9."""
    n = 0
    for key in KEYS15:
        t = TRIG[key]
        for e in t["expected"]["ivlegs"]:
            leg = t["legs"][e["leg"]]
            if e["px"] is None or e["F_new"] is None:
                assert e["iv_new"] is None
                continue
            iv, why = R.iv76(e["px"], e["F_new"], float(leg["strike"]), R.tte_years(leg["expiry"], e["t_px"]), e["leg"][:2])
            assert close(iv if why == "ok" else None, e["iv_new"]), (key, e)
            n += 1
    assert n >= 40


def test_the_recorders_time_matched_iv_at_the_signal_matches_the_research_legs():
    """Through the recorder, every leg's price, its price time, the future matched to it and the IV at the signal as
    ``ivlegs`` — exactly where the union tape has no second sampler's row in between, else to ``IV_TOL``."""
    n = 0
    for key in KEYS15:
        t = TRIG[key]
        record, _, _ = run_read(t)
        for e in t["expected"]["ivlegs"]:
            if e["snap"] != "signal":
                continue
            leg = record["legs"][e["leg"]]["X"]
            assert close(leg["px"], e["px"]) and close(leg["F"], e["F_new"]), (key, e)
            assert abs(leg["pxT"] - e["t_px"]) < 1.0 and close(leg["iv"], e["iv_new"], IV_TOL), (key, e["leg"], leg["iv"], e["iv_new"])
            n += 1
    assert n == 16


# -- missing legs: what the record says, and why ---------------------------------------------------------------------


def test_canbk_19_oct_three_legs_never_priced_only_oi_and_stock_read():
    """19 Oct CANBK 09:15: CE1, CE2 and PE1 were on neither engine's price feed — no price at the signal. The CE pair
    has no priced leg, so IV and PREMIUM cannot vote (the PE pair's PE2 alone would do for its side); VOL is not
    recorded (the provisional 4-read). score5: split 1-1, reads missing VOL, IV, PREMIUM, legs CE1,CE2,PE1 unpriced;
    every vote of all 17 stages as the research's."""
    t = TRIG["2026-10-19|CANBK|09:15"]
    sc = t["expected"]["score5"]
    record, stages, _ = run_read(t)
    assert record["split"] == sc["t_split"] == "1-1"
    assert ", ".join(k for k in ("OI", "VOL", "IV", "PREMIUM") if k in record["missingReads"]) == sc["reads_nan_at_trigger"]
    assert ",".join(record["legsNoPriceAtSignal"]) == sc["legs_no_price_at_signal"] == "CE1,CE2,PE1"
    assert record["basis"].startswith("provisional")
    miss = record["reads"]
    assert "CE1 not priced at 09:45" in miss["PREMIUM"]["missing"] and "volume not recorded" in miss["VOL"]["missing"]
    assert miss["IV"]["vote"] == "" and miss["PREMIUM"]["vote"] == "" and miss["VOL"]["vote"] == ""
    for got, exp in zip(stages, t["expected"]["stages"], strict=True):
        assert got["votes"] == {k: exp[f"vote_{k}"] for k in R.READS}, got["m"]


def test_pnb_19_oct_no_leg_ever_printed_only_stock_reads():
    """19 Oct PNB 09:15: no leg was on either engine's feed (quotes 114-118 against a 123.88 close): no price, and
    three of the four with no OI print — only STOCK reads (score5: split 1-0, OI/VOL/IV/PREMIUM missing)."""
    t = TRIG["2026-10-19|PNB|09:15"]
    sc = t["expected"]["score5"]
    record, _, _ = run_read(t)
    assert record["split"] == sc["t_split"] == "1-0"
    assert record["missingReads"] == ["OI", "VOL", "IV", "PREMIUM"]
    assert ",".join(record["legsNoPriceAtSignal"]) == sc["legs_no_price_at_signal"]
    assert "no OI print" in record["reads"]["OI"]["missing"]
    assert record["votes"]["STOCK"] == sc["t_vote_STOCK"]


# -- the walls: wall_state.csv, 9 Oct ABCAPITAL ----------------------------------------------------------------------


@pytest.mark.parametrize("side", ["PE", "CE"])
def test_the_wall_state_rows_of_9_oct_abcapital(side):
    """opt_table/wall_state.csv at the trigger: PE 370 — OI 2,070,800 (+37,200, +1.829268 %), premium 7.5 → 6.325
    (−15.6667 %), SHORT BUILD-UP (writers adding) → Bullish, oiChangePct 184.615 against six non-zero windows
    (significant); CE 400 — OI −0.215 %: FLAT → Neutral with no premium, oiChangePct −60."""
    w = next(x for x in FIX["walls"]["walls"] if x["side"] == side)
    e = w["expected"]
    deltas = R.oi_deltas([(a if a is not None else math.nan, b if b is not None else math.nan) for _, _, a, b in w["windows"]])
    got = R.wall_read(side, w["oi1"], w["oi0"], w["p1"] if w["p1"] is not None else math.nan,
                      w["p0"] if w["p0"] is not None else math.nan, deltas)
    assert close(got["oi"], e["oi"]) and close(got["oiChg"], e["oi_chg"]) and close(got["doi"], e["doi"])
    assert close(got["pxChg"] if R.isnum(got["pxChg"]) else None, e["px_chg"])
    assert close(got["oicp"], e["oicp"]) and got["oicpBasis"] == e["oicp_basis"] and got["nBase"] == e["n_base"]
    assert ("yes" if got["significant"] else "no") == e["significant"]
    assert (got["state"], got["why"], got["interp"]) == (e["state"], e["why"], e["interp"])


def test_the_walls_overall_of_9_oct_abcapital_is_bullish():
    reads = {}
    for w in FIX["walls"]["walls"]:
        deltas = R.oi_deltas([(a if a is not None else math.nan, b if b is not None else math.nan) for _, _, a, b in w["windows"]])
        reads[w["side"]] = R.wall_read(w["side"], w["oi1"], w["oi0"], w["p1"] if w["p1"] is not None else math.nan,
                                       w["p0"] if w["p0"] is not None else math.nan, deltas)["interp"]
    assert R.wall_overall(reads["CE"], reads["PE"]) == FIX["walls"]["overall"] == "Bullish"


# -- the engine's own path: catalogue, ATR as logged, the ledger and the archive --------------------------------------


@pytest.mark.parametrize("key", ["2026-10-15|LICI|09:15", "2026-10-15|EICHERMOT|10:15"])
async def test_the_engine_records_the_research_row_through_its_own_glue(settings, key, tmp_path):
    """``Engine._read5_record`` end to end on a research trigger: the legs from the catalogue (today's expiry rule), the
    ATR30 the trigger was logged with (``regime.breadth``), the ledger's ``read5.trigger`` / ``read5.follow`` and the
    archive's ``read5`` row — the score5 split, lead and first 3 of 5 with its label."""
    import pandas as pd

    from kotsin_nse.domain import Direction
    from kotsin_nse.engine import Engine
    from kotsin_nse.instrument.catalogue import Catalogue
    from kotsin_nse.strategy.base import Signal
    from kotsin_nse.strategy.keys import StrategyKey

    t = TRIG[key]
    sc = t["expected"]["score5"]
    e = Engine(settings)
    await e.start()
    try:
        c = contracts_of(t)
        cat = Catalogue()
        for inst in (*c.legs.values(), c.fut, c.eq):
            assert inst is not None
            cat.by_code[inst.scrip_code] = inst
        cat.options_by_symbol[t["symbol"]] = [i for i in c.legs.values() if i is not None]
        cat.futures_by_symbol[t["symbol"]] = [c.fut]  # type: ignore[list-item]
        cat.equity_by_symbol[t["symbol"]] = c.eq  # type: ignore[assignment]
        e.catalogue_loader.catalogue = cat
        e.underlyings[t["symbol"]] = c.eq  # type: ignore[assignment]
        e.recorder = replay(t)
        sig = Signal(strategy=StrategyKey.FUDKII, symbol=t["symbol"], direction=Direction.BULLISH if t["direction"] == "Bullish" else Direction.BEARISH,
                     ts=int(t["c0"]), entry=t["S"], stop=t["S"], targets=(), grade="A", rr=1.0, reason="research row")
        e._breadth_at[sig.signal_id] = {"atr30": t["atr"]}

        async def no_wait(until: float) -> None:
            return None

        e._read5_wait = no_wait  # type: ignore[method-assign]
        await e._read5_record(sig, t["s"])
        assert e.read5_stats["errors"] == 0, e.read5_stats
        evs = [ev for ev in await e.ledger.rows_between("events", 0, 4e9) if str(ev["kind"]).startswith("read5.")]
        trig, follow = evs
        assert trig["atrSrc"] == "regime.breadth" and trig["split"] == sc["t_split"] and trig["lead"] == sc["t_lead"]
        assert ", ".join(trig["with"]) == (sc["t_reads_with"] or "") and trig["expiry"] == "2026-10-27"
        assert follow["f3"]["call"] == sc["f3_call"] and follow["f3"]["time"] == sc["f3_time"] and follow["f3"]["label"] == sc["f3_label"]
        assert follow["complete"] and follow["wallsAtF3"] is not None
        e.archive.flush()
        row = pd.read_parquet(Path(settings.data_dir) / "archive" / "read5" / f"{t['day']}.parquet").iloc[0]
        assert (row.t_split, row.f3_call, row.f3_time, row.f3_label, row.follow_state) == (
            sc["t_split"], sc["f3_call"], sc["f3_time"], sc["f3_label"], "complete")
    finally:
        await e.stop()
