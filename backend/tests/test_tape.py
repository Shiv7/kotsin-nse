"""The tick tape: what goes on it, when it lapses, how it is kept, and a replay on it."""

import time
from datetime import datetime

import pandas as pd
import pytest

from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Direction, Instrument, InstrumentKind, Position, PosSide
from kotsin_nse.engine import Engine
from kotsin_nse.instrument.select import Quote
from kotsin_nse.main import build_parser, show_tape
from kotsin_nse.market.session import IST
from kotsin_nse.ops import tape as tp
from kotsin_nse.ops.archive import DailyArchive
from kotsin_nse.ops.tape import (
    AFTER_CLOSE_S,
    CANDIDATE_TTL_S,
    MAX_WATCHED,
    Series,
    Tape,
    Tick,
    read_day,
)
from kotsin_nse.research.tape_replay import replay_on_tape
from kotsin_nse.risk.exits import ExitEngine
from kotsin_nse.risk.limits import RT_X_LIMITS

DAY = "2026-09-24"


def ist_ts(day: str, hm: str) -> float:
    return datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=IST).timestamp()


def q(ts: float, px: float, spread: float = 0.05) -> Quote:
    return Quote(ltp=px, bid=round(px - spread, 2), ask=round(px + spread, 2), ts=ts)


def test_the_watched_codes_and_their_legs_are_written_once_a_second_on_change(tmp_path):
    a = DailyArchive(tmp_path / "archive")
    legs = {"RELIANCE": [("2885", "equity"), ("5001", "future")]}
    t = Tape(a, legs_for=lambda s: legs.get(s, []))
    now = ist_ts(DAY, "10:00")
    quotes = {"45678": q(now, 10.0), "2885": q(now, 1300.0), "5001": q(now, 1302.0)}
    t.follow("RELIANCE", ["45678"], now=now)
    assert t.sample(now, quotes, held=[]) == 3
    assert t.sample(now + 1, quotes, held=[]) == 0, "an unchanged quote is not written again"
    quotes["45678"] = q(now + 2, 10.5)
    assert t.sample(now + 2, quotes, held=[]) == 1
    a.flush()
    df = read_day(tmp_path / "archive", DAY)
    assert set(df.scrip_code) == {"45678", "2885", "5001"} and len(df) == 4
    assert not df.held.any() and set(df.role) == {"option", "equity", "future"}
    s = tp.series(df, "45678")
    assert s.at(now - 1) is None and s.at(now + 1).ltp == 10.0 and s.at(now + 2).ltp == 10.5
    assert s.at(now + 500).ltp == 10.5, "the reader forward-fills"
    assert [r["role"] for r in tp.summary(df)] == ["equity", "future", "option"]
    assert tp.days(tmp_path / "archive") == [DAY]
    assert t.stats()["watched"] == 1 and t.stats()["rows"] == 4
    # a quote that never printed is not a row, and an unknown symbol has no legs
    t.follow("NOBODY", ["1"], now=now)
    assert t.sample(now + 3, quotes, held=[]) == 0


def test_a_candidate_lapses_a_held_contract_stays_and_its_rows_are_flagged_through_a_grace(tmp_path):
    a = DailyArchive(tmp_path / "archive")
    t = Tape(a, legs_for=lambda s: [("2885", "equity")])
    now = ist_ts(DAY, "10:00")
    t.follow("RELIANCE", ["45678"], now=now)
    # the position pins the watch past the candidate ttl and marks the symbol's rows held
    later = now + CANDIDATE_TTL_S + 10
    quotes = {"45678": q(later, 12.0), "2885": q(later, 1300.0)}
    assert t.sample(later, quotes, held=[("45678", "RELIANCE", "option")]) == 2
    w = t.watched(later)["45678"]
    assert w["open"] and w["held"] and t.stats()["held"] == 1
    # it closes → a grace: still on tape, still flagged, no longer open
    t2 = later + 1
    quotes = {"45678": q(t2, 11.0), "2885": q(t2, 1299.0)}
    assert t.sample(t2, quotes, held=[]) == 2
    # `watched` is read at a stated instant, never the wall clock: these timestamps are simulated
    w = t.watched(t2)["45678"]
    assert not w["open"] and w["held"] and t.stats()["held"] == 0
    # past the grace it is gone, legs included
    t3 = t2 + AFTER_CLOSE_S + 1
    quotes = {"45678": q(t3, 10.0), "2885": q(t3, 1298.0)}
    assert t.sample(t3, quotes, held=[]) == 0 and t.watched() == {}
    a.flush()
    df = read_day(tmp_path / "archive", DAY)
    assert len(df) == 4 and df.held.all()
    # a plain candidate lapses after its ttl
    t.follow("RELIANCE", ["999"], now=t3)
    quotes["999"] = q(t3, 1.0)
    assert t.sample(t3, quotes, held=[]) == 2  # the candidate and the equity leg
    assert t.sample(t3 + CANDIDATE_TTL_S + 1, quotes, held=[]) == 0 and t.watched() == {}
    # off: nothing is followed or written
    off = Tape(a, enabled=False)
    off.follow("X", ["1"], now=now)
    assert off.sample(now, {"1": q(now, 1.0)}, held=[("1", "X", "option")]) == 0 and off.watched() == {}


def test_the_second_a_contract_becomes_held_is_always_written(tmp_path):
    """The change key carries the held flag. Without it a contract whose quote froze the instant it
    was bought would end the day with only held=False rows — and the retention would then drop the
    one trade the tape exists to keep."""
    a = DailyArchive(tmp_path / "archive")
    t = Tape(a)
    now = ist_ts(DAY, "10:00")
    frozen = q(now, 10.0)  # the same Quote object: same ts, same prices, for the whole test
    t.follow("RELIANCE", ["45678"], now=now)
    assert t.sample(now, {"45678": frozen}, held=[]) == 1
    assert t.sample(now + 1, {"45678": frozen}, held=[]) == 0
    # it is bought; the quote has not moved, and the row must still be written
    assert t.sample(now + 2, {"45678": frozen}, held=[("45678", "RELIANCE", "option")]) == 1
    assert t.sample(now + 3, {"45678": frozen}, held=[("45678", "RELIANCE", "option")]) == 0
    a.flush()
    df = read_day(tmp_path / "archive", DAY)
    assert list(df.held) == [False, True] and list(df.ts) == [int(now), int(now) + 2]


def test_a_strike_that_has_not_traded_is_still_taped_when_it_has_a_book(tmp_path):
    """An OTM strike with a real two-sided market but no print yet is exactly the contract a
    'should we have picked the other strike' question is about."""
    a = DailyArchive(tmp_path / "archive")
    t = Tape(a)
    now = ist_ts(DAY, "10:00")
    t.follow("RELIANCE", ["45678", "45679"], now=now)
    quotes = {
        "45678": Quote(ltp=0.0, bid=2.4, ask=2.6, ts=now),  # quoted, never traded
        "45679": Quote(ltp=0.0, bid=0.0, ask=0.0, ts=now),  # nothing at all
    }
    assert t.sample(now, quotes, held=[]) == 1
    a.flush()
    df = read_day(tmp_path / "archive", DAY)
    assert list(df.scrip_code) == ["45678"] and df.iloc[0].ltp == 0.0 and df.iloc[0].bid == 2.4


def test_a_day_whose_held_rows_cannot_be_moved_is_kept_not_deleted(tmp_path, monkeypatch):
    a = DailyArchive(tmp_path / "archive", keep_sessions=1, keep_held_sessions=5)
    for day in ("2026-09-01", "2026-09-02"):
        ts = int(ist_ts(day, "10:00"))
        a.quote("1", ts, symbol="X", role="option", ltp=1, bid=1, ask=1, quote_ts=ts, held=True)
    a.flush()
    assert a.days("quotes") == ["2026-09-02"] and a.days("quotes_held") == ["2026-09-01"]

    boom = DailyArchive(tmp_path / "boom", keep_sessions=1, keep_held_sessions=5)
    for day in ("2026-09-01", "2026-09-02"):
        ts = int(ist_ts(day, "10:00"))
        boom.quote("1", ts, symbol="X", role="option", ltp=1, bid=1, ask=1, quote_ts=ts, held=True)
    boom.flush()
    (tmp_path / "boom" / "quotes" / "2026-09-01.parquet").write_bytes(b"not parquet")
    boom.keep_sessions = 0  # rewrite the day that was already pruned away
    boom.quote("1", int(ist_ts("2026-09-01", "10:00")), symbol="X", role="option", ltp=2, bid=2,
               ask=2, quote_ts=1, held=True)
    boom.keep_sessions = 1
    before = boom.files_pruned
    boom.flush()
    assert boom.days("quotes") == ["2026-09-01", "2026-09-02"], "unreadable: kept, not deleted"
    assert boom.set_aside_failed >= 1 and boom.files_pruned == before
    assert boom.stats()["set_aside_failed"] >= 1


def test_prune_keeps_the_newest_sessions_and_moves_the_held_rows_aside(tmp_path):
    a = DailyArchive(tmp_path / "archive", keep_sessions=2, keep_held_sessions=3, keep_research_sessions=3)
    for day in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"):
        ts = int(ist_ts(day, "10:00"))
        a.quote("1", ts, symbol="X", role="option", ltp=1, bid=1, ask=1, quote_ts=ts, held=True)
        a.quote("2", ts, symbol="Y", role="option", ltp=1, bid=1, ask=1, quote_ts=ts, held=False)
        a.oi("1", ts, 100.0, None)
    (tmp_path / "archive" / "quotes").mkdir(parents=True)
    (tmp_path / "archive" / "quotes" / "2026-08-30.tmp.parquet").write_bytes(b"junk")
    a.flush()
    assert a.days("quotes") == ["2026-09-03", "2026-09-04"], "the tape is on its own short window"
    assert a.days("quotes_held") == ["2026-09-01", "2026-09-02"]
    held = pd.read_parquet(tmp_path / "archive" / "quotes_held" / "2026-09-01.parquet")
    assert list(held.scrip_code) == ["1"] and held.held.all()
    assert not (tmp_path / "archive" / "quotes" / "2026-08-30.tmp.parquet").exists()
    assert a.days("oi") == ["2026-09-02", "2026-09-03", "2026-09-04"], "research keeps its own 3"
    assert a.files_pruned == 3 and a.rows_set_aside == 2 and a.errors == 0
    # a day that moved aside is still readable through the same reader, held rows only
    assert list(read_day(tmp_path / "archive", "2026-09-01").scrip_code) == ["1"]
    assert tp.days(tmp_path / "archive") == ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    assert a.stats()["keep_sessions"] == 2 and a.stats()["days"]["quotes_held"] == 2
    # the held stream and the research streams have their own, longer windows
    for day in ("2026-09-05", "2026-09-06", "2026-09-07"):
        ts = int(ist_ts(day, "10:00"))
        a.quote("1", ts, symbol="X", role="option", ltp=1, bid=1, ask=1, quote_ts=ts, held=True)
        a.oi("1", ts, 100.0, None)
    a.flush()
    assert a.days("quotes") == ["2026-09-06", "2026-09-07"]
    assert a.days("quotes_held") == ["2026-09-03", "2026-09-04", "2026-09-05"]
    assert a.days("oi") == ["2026-09-05", "2026-09-06", "2026-09-07"]
    # window off: nothing is ever pruned
    off = DailyArchive(tmp_path / "off")
    for day in ("2026-09-01", "2026-09-02"):
        off.oi("1", int(ist_ts(day, "10:00")), 1.0, None)
    off.flush()
    assert off.prune() == {} and off.files_pruned == 0 and off.days("oi") == ["2026-09-01", "2026-09-02"]


def test_the_backtest_inputs_are_not_on_the_tapes_fifteen_session_window(tmp_path):
    """FUKAA cannot be tested without months of option OI and nothing else records it. A 15-session
    window on ``oi`` would delete that quietly, so the streams carry separate windows."""
    a = DailyArchive(tmp_path / "archive", keep_sessions=15, keep_held_sessions=250,
                     keep_research_sessions=250)
    assert a.keep_for("quotes") == 15
    assert a.keep_for("quotes_held") == 250
    assert [a.keep_for(k) for k in ("bars", "oi", "micro", "option_quotes")] == [250] * 4
    # the same split on the real defaults
    s = Settings(_env_file=None, data_dir=tmp_path)
    e = Engine(s)
    assert e.archive.keep_for("quotes") == 15 and e.archive.keep_for("oi") == 250


def test_the_settings_carry_the_windows_and_the_switch():
    s = Settings(_env_file=None)
    assert s.archive_keep_sessions == 15 and s.archive_keep_held_sessions == 250
    assert s.archive_keep_research_sessions == 250 and s.tape_enabled


def test_the_candidate_ceiling_never_refuses_a_held_contract(tmp_path):
    """The alert ring holds 500 a book. The ceiling is the backstop under the card follow's own
    time box — and an open position must get on tape whatever else is being watched."""
    a = DailyArchive(tmp_path / "archive")
    t = Tape(a)
    now = ist_ts(DAY, "10:00")
    t.follow("X", [str(i) for i in range(MAX_WATCHED + 50)], now=now)
    assert len(t.watched()) == MAX_WATCHED and t.stats()["dropped"] == 50 and t.stats()["capped"]
    quotes = {"45678": q(now, 10.0)}
    assert t.sample(now, quotes, held=[("45678", "RELIANCE", "option")]) == 1
    assert t.watched()["45678"]["held"], "a position is never refused by the ceiling"
    # renewing one already on tape is not a new watch and cannot be refused
    t.follow("X", ["0"], now=now + 10)
    assert t.watched()["0"]["symbol"] == "X" and t.stats()["dropped"] == 50


def test_a_card_stops_being_renewed_half_an_hour_after_it_fired(settings, monkeypatch):
    """Otherwise every contract the ring ever carded sits on the tape until 15:30."""
    from kotsin_nse.alerts.engine import TAPE_CARD_TTL_S

    assert TAPE_CARD_TTL_S == CANDIDATE_TTL_S
    e = Engine(settings)
    now = time.time()
    e.tape.follow("RELIANCE", ["45678"], now=now - TAPE_CARD_TTL_S - 1)
    e.quotes["45678"] = q(now, 10.0)
    assert e.tape.sample(now, e.quotes, held=[]) == 0 and e.tape.watched() == {}


def test_the_engine_pins_the_open_position_and_resolves_its_legs(settings, equity, option):
    e = Engine(settings)
    e.underlyings[equity.symbol] = equity
    fut = Instrument("68781", "RELIANCE", Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=250,
                     expiry="2026-09-29", underlying="RELIANCE")
    e.catalogue_loader.catalogue.futures_by_symbol["RELIANCE"] = [fut]
    now = time.time()
    e.quotes[option.scrip_code] = q(now, 10.0)
    e.quotes[equity.scrip_code] = q(now, 1300.0)
    e.quotes[fut.scrip_code] = q(now, 1302.0)
    e.positions["p1"] = Position(
        id="p1", strategy="FUDKII_RT_X", instrument=option, underlying=equity, side=PosSide.LONG,
        qty=250, entry=10.0, opened_ts=now, signal_id="s", direction=Direction.BULLISH, option_sl=8.0,
    )
    assert e.archive.keep_sessions == 15 and e.tape.enabled
    e._record_tape()
    assert e.tape.stats()["held"] == 1 and e.archive.rows_buffered == 3
    assert e._tape_legs("RELIANCE") == [("2885", "equity"), ("68781", "future")]
    assert e._tape_legs("NOBODY") == []
    assert e.health_snapshot()["tape"]["watched"] == 1


def _pos(option, equity, *, entry: float, stop: float, opened: float) -> Position:
    return Position(
        id="p", strategy="FUDKII_RT_X", instrument=option, underlying=equity, side=PosSide.LONG,
        qty=500, entry=entry, opened_ts=opened, signal_id="s", direction=Direction.BULLISH,
        equity_entry=1300.0, equity_sl=1290.0, equity_targets=(1320.0, 1340.0),
        option_sl=stop, option_targets=(14.0, 18.0, 22.0), option_t1=14.0,
    )


def test_a_replay_on_the_tape_exits_where_the_policy_did_and_fills_at_the_bid(option, equity):
    t0 = int(ist_ts(DAY, "10:00"))
    # the premium sits at 10, then trades down through the 8.0 stop at 10:05 and stays there
    ticks = [Tick(t0 + i, 10.0, 9.95, 10.05, t0 + i) for i in range(300)]
    ticks += [Tick(t0 + 300 + i, 7.5, 7.45, 7.55, t0 + 300 + i) for i in range(600)]
    eq = Series("2885", [Tick(t0 + i, 1300.0, 1299.95, 1300.05, t0 + i) for i in range(900)])
    pos = _pos(option, equity, entry=10.0, stop=8.0, opened=t0)
    rep = replay_on_tape(ExitEngine(RT_X_LIMITS), pos, Series("45678", ticks), eq, start_ts=t0, end_ts=t0 + 899)
    assert rep.events and pos.status == "CLOSED" and rep.source == "tape"
    first = rep.events[0]
    assert first.ts >= t0 + 300 and first.reason in {"SL-OP", "TRAIL", "SL-EQ"} and first.fill == 7.45
    assert sum(ev.qty for ev in rep.events) == 500 and rep.gross == pytest.approx((7.45 - 10.0) * 500)
    assert rep.peak_mid == 10.0 and rep.seconds == 301 and rep.skipped_stale == 0, "evaluation stops at the exit"

    # never breached → nothing fires; the end of the tape flattens at the last bid
    calm = Series("45678", [Tick(t0 + i, 10.0, 9.95, 10.05, t0 + i) for i in range(120)])
    pos = _pos(option, equity, entry=10.0, stop=8.0, opened=t0)
    rep = replay_on_tape(ExitEngine(RT_X_LIMITS), pos, calm, eq, start_ts=t0, end_ts=t0 + 119)
    assert [ev.reason for ev in rep.events] == ["EOD"] and rep.events[0].fill == 9.95

    # a stale quote is skipped as the live loop skips it
    stale = Series("45678", [Tick(t0 + i, 7.0, 6.95, 7.05, t0 - 600) for i in range(30)])
    pos = _pos(option, equity, entry=10.0, stop=8.0, opened=t0)
    rep = replay_on_tape(ExitEngine(RT_X_LIMITS), pos, stale, None, start_ts=t0, end_ts=t0 + 29, flatten_at_end=False)
    assert rep.events == [] and rep.skipped_stale == 30 and pos.status == "OPEN"


def test_the_cli_reads_the_tape_without_an_engine(tmp_path, capsys):
    s = Settings(_env_file=None, data_dir=tmp_path, engine_enabled=False)
    args = build_parser().parse_args(["tape", "--day", DAY])
    show_tape(s, args)
    assert "nothing on tape" in capsys.readouterr().out
    a = DailyArchive(tmp_path / "archive")
    ts = int(ist_ts(DAY, "10:00"))
    a.quote("45678", ts, symbol="RELIANCE", role="option", ltp=10, bid=9.95, ask=10.05, quote_ts=ts, held=True)
    a.quote("45678", ts + 1, symbol="RELIANCE", role="option", ltp=11, bid=10.95, ask=11.05, quote_ts=ts + 1, held=True)
    a.flush()
    show_tape(s, build_parser().parse_args(["tape", "--day", DAY]))
    out = capsys.readouterr().out
    assert "RELIANCE" in out and "yes" in out and "2 rows on" in out
    show_tape(s, build_parser().parse_args(["tape", "--day", DAY, "--code", "45678"]))
    out = capsys.readouterr().out
    assert "10:00:00" in out and "10:00:01" in out and "2 rows" in out
    show_tape(s, build_parser().parse_args(["tape", "--days"]))
    assert DAY in capsys.readouterr().out
    show_tape(s, build_parser().parse_args(["tape", "--day", DAY, "--symbol", "NOBODY"]))
    assert "nothing on tape" in capsys.readouterr().out


def test_the_replay_counts_decision_bars_and_force_flats_the_way_the_live_loop_does(option, equity):
    """``bars_held`` is +1 per CLOSED 30m bar of the underlying live, not per elapsed half hour,
    and the session's own force-flat applies without being asked for."""
    seen: list[tuple[str, int, bool]] = []

    class Spy(ExitEngine):
        def evaluate(self, pos, view):
            seen.append((datetime.fromtimestamp(view.now, IST).strftime("%H:%M"),
                         view.bars_held, view.past_force_flat))
            return None

    opened = ist_ts(DAY, "09:47")  # inside the 09:45 bucket
    ticks = [Tick(int(ist_ts(DAY, hm)), 10.0, 9.95, 10.05, int(ist_ts(DAY, hm)))
             for hm in ("09:47", "10:14", "10:15", "15:19", "15:20")]
    pos = _pos(option, equity, entry=10.0, stop=8.0, opened=opened)
    rep = replay_on_tape(Spy(RT_X_LIMITS), pos, Series("45678", ticks), None,
                         start_ts=opened, end_ts=ist_ts(DAY, "15:20"), flatten_at_end=False)
    at = dict((t, b) for t, b, _ in seen)
    assert at["09:47"] == 0 and at["10:14"] == 0, "the 09:45 bar has not closed yet"
    assert at["10:15"] == 1 and at["15:20"] == 11
    flat = dict((t, f) for t, _, f in seen)
    assert not flat["15:19"] and flat["15:20"], "NSE force-flat is 15:20, and the replay knows it"
    # every second with a quote is counted; the ones whose forward-filled quote had gone
    # stale are not evaluated, exactly as the live loop declines to evaluate them
    assert len(seen) == rep.seconds - rep.skipped_stale and rep.skipped_stale > 0


def test_the_replay_refuses_a_position_it_would_silently_return_nothing_for(option, equity):
    t0 = int(ist_ts(DAY, "10:00"))
    ticks = [Tick(t0 + i, 10.0, 9.95, 10.05, t0 + i) for i in range(60)]
    pos = _pos(option, equity, entry=10.0, stop=8.0, opened=t0)
    rep = replay_on_tape(ExitEngine(RT_X_LIMITS), pos, Series("45678", ticks), None,
                         start_ts=t0, end_ts=t0 + 59)
    assert pos.status == "CLOSED" and rep.events[-1].reason == "EOD"
    # the same Position again would look like "nothing happened"; it is refused instead
    with pytest.raises(ValueError, match="replay a fresh position"):
        replay_on_tape(ExitEngine(RT_X_LIMITS), pos, Series("45678", ticks), None,
                       start_ts=t0, end_ts=t0 + 59)


def test_the_end_of_tape_flatten_is_stamped_where_the_quote_actually_was(option, equity):
    """Forward-filling a quote from hours earlier onto end_ts would read as a fill that never was."""
    t0 = int(ist_ts(DAY, "10:00"))
    ticks = [Tick(t0 + i, 10.0, 9.95, 10.05, t0 + i) for i in range(10)]
    pos = _pos(option, equity, entry=10.0, stop=8.0, opened=t0)
    rep = replay_on_tape(ExitEngine(RT_X_LIMITS), pos, Series("45678", ticks), None,
                         start_ts=t0, end_ts=t0 + 4000, quote_max_age_s=1e9)
    ev = rep.events[-1]
    assert ev.reason == "EOD" and ev.ts == t0 + 9 and "3991s after the last quote" in ev.note


def test_the_post_close_grace_is_not_extended_by_a_card_that_keeps_renewing(tmp_path):
    """The trigger card renews its follow every second. That may keep the contract on tape; it
    must not keep flagging its rows held for half an hour after the position closed."""
    a = DailyArchive(tmp_path / "archive")
    t = Tape(a)
    now = ist_ts(DAY, "10:00")
    px = 10.0
    t.sample(now, {"45678": q(now, px)}, held=[("45678", "RELIANCE", "option")])
    assert t.watched(now)["45678"]["held"]
    closed = now + 1
    t.sample(closed, {"45678": q(closed, px)}, held=[])
    for i in range(1, 5):  # the card goes on renewing through and past the grace
        at = closed + i * 100
        t.follow("RELIANCE", ["45678"], now=at)
        t.sample(at, {"45678": q(at, px)}, held=[])
    assert "45678" in t.watched(), "still on tape: the card is still live"
    assert not t.watched(closed + 400)["45678"]["held"], "but no longer flagged held"
    a.flush()
    df = read_day(tmp_path / "archive", DAY)
    held_ts = sorted(int(r.ts) for r in df.itertuples() if r.held)
    # open, then exactly AFTER_CLOSE_S of grace from the close — and not a second more, however
    # long the card goes on renewing the watch
    assert held_ts == [int(now), int(closed), int(closed) + 100, int(closed) + 200, int(closed) + 300]
    assert int(closed) + 300 == int(closed + AFTER_CLOSE_S)
    assert sorted(int(r.ts) for r in df.itertuples() if not r.held) == [int(closed) + 400]


def test_set_aside_keeps_the_whole_of_a_traded_contract_not_only_its_held_rows(tmp_path):
    """The tape is change-compressed and the reader forward-fills, so the row in force at entry is
    usually one written before the position existed. Dropping it would leave the start of the hold
    with nothing to forward-fill from once the day left the tape's own window."""
    a = DailyArchive(tmp_path / "archive", keep_sessions=1, keep_held_sessions=9)
    old = int(ist_ts("2026-09-01", "10:00"))
    a.quote("45678", old, symbol="RELIANCE", role="option", ltp=9, bid=8.95, ask=9.05, quote_ts=old, held=False)
    a.quote("45678", old + 60, symbol="RELIANCE", role="option", ltp=10, bid=9.95, ask=10.05, quote_ts=old + 60, held=True)
    a.quote("2885", old, symbol="RELIANCE", role="equity", ltp=1300, bid=1299, ask=1301, quote_ts=old, held=False)
    a.quote("99999", old, symbol="TCS", role="option", ltp=5, bid=4.95, ask=5.05, quote_ts=old, held=False)
    a.quote("1", int(ist_ts("2026-09-02", "10:00")), symbol="X", role="option", ltp=1, bid=1, ask=1,
            quote_ts=int(ist_ts("2026-09-02", "10:00")), held=False)
    a.flush()
    kept = read_day(tmp_path / "archive", "2026-09-01")
    assert sorted(set(kept.scrip_code)) == ["45678"], "the traded contract, both its rows"
    assert sorted(int(t) for t in kept.ts) == [old, old + 60]
    assert tp.series(kept, "45678").at(old + 30).ltp == 9.0, "the pre-entry row still forward-fills"


def test_a_flush_in_flight_does_not_lose_its_day_to_another_thread(tmp_path):
    """`Engine.stop` flushes while the housekeeping loop may still be inside its own flush, and
    both share the buffer and `_write`'s per-day temp path."""
    import threading

    a = DailyArchive(tmp_path / "archive", keep_sessions=5, keep_held_sessions=5, keep_research_sessions=5)
    ts = int(ist_ts(DAY, "10:00"))
    for i in range(400):
        a.quote(str(i), ts + i, symbol="X", role="option", ltp=1, bid=1, ask=1, quote_ts=ts, held=True)
    out: list[int] = []
    threads = [threading.Thread(target=lambda: out.append(a.flush())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(out) == 400 and a.errors == 0 and a.rows_buffered == 0
    assert len(read_day(tmp_path / "archive", DAY)) == 400
    # the sweep leaves a temp file for the newest day alone — it may be a live _write
    live = tmp_path / "archive" / "quotes" / f"{DAY}.tmp.parquet"
    stale = tmp_path / "archive" / "quotes" / "2020-01-01.tmp.parquet"
    live.write_bytes(b"in flight")
    stale.write_bytes(b"orphan")
    a.prune()
    assert live.exists() and not stale.exists()
