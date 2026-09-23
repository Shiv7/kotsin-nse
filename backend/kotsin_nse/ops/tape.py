"""The tick tape: the second-by-second quotes the exit engine judged against, kept.

The RT exit policy is a state machine over wall-clock seconds — a 75 s sustain, a 10 s stop
re-projection, a give-back band read against the live mid — and the only record of the prices
it saw was the 1-minute candle the broker serves afterwards. A candle has four prices and no
order; a replay on it has to guess the path inside the minute, and every "why was this stop hit"
question ended at that guess. The tape removes it: while a contract matters, its top of book is
written once a second (on change), beside the equity and the front future the same policy reads.

**What is on tape** (``Tape.sample``, driven by the engine clock at 1 Hz):

* every contract of an open position, until five minutes after it closes (``AFTER_CLOSE_S``),
  so the exit print and the minutes around it are on record;
* the strikes the selector considered for a trigger (``follow`` from ``_select_instrument``),
  for half an hour (``CANDIDATE_TTL_S``) — long enough to see what the rejected strike did;
* the contract of a trigger card fired inside that same half hour (the alert refresh renews it
  every second, so it lapses half an hour after the *last* renewal, not after the fill);
* and for each of those, the underlying's equity and its front future (``legs_for``).

A row is written only when the quote changed since the last row for that code — the broker's tick
time counts as a change, so an actively trading code writes about one row a second and a quiet one
costs a few rows a minute; the reader forward-fills. The ``held`` flag marks the rows of a symbol
that had a position open (through the grace), and it is **part of the change key**: the second a
contract becomes held is always written, so a contract that stops quoting the moment it is bought
still has a held row. The archive moves those rows aside when the day leaves the rolling window
(``ops/archive.py::prune``).

Measured 2026-09-23 (23 carded contracts at 1 Hz, every second written): 0.57 MB for the day.
With the legs and the on-change rule this stays under ~8 MB a session, 15 sessions ≈ 120 MB.
Two things bound it rather than leaving it to that arithmetic: the card follow is time-boxed
(above — the alert ring holds 500 a book and an all-day ring would otherwise pin every contract
it ever carded), and ``MAX_WATCHED`` is a hard ceiling on the *candidate* set. An open position
is never refused by the ceiling: the tape's whole purpose is the contract actually held. The
ceiling bounds watches, not rows: each watched symbol also carries its equity and future legs, so
the per-second row ceiling is nearer 3 x ``MAX_WATCHED`` than ``MAX_WATCHED``.

A quote with no last-traded price is still recorded when it has a side: an OTM strike that has not
traded today has a real two-sided book, and dropping it would answer "what did the strike we passed
over do" with silence.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import structlog

from .archive import DailyArchive

log = structlog.get_logger(__name__)

#: how long a considered-but-not-held contract stays on tape after the selector last looked
CANDIDATE_TTL_S = 1800.0
#: how long a closed position's contract (and its legs) stay on tape after the exit
AFTER_CLOSE_S = 300.0
#: hard ceiling on candidate/card watches — a backstop, ~10x a heavy day (23 on 2026-09-23).
#: Held contracts bypass it.
MAX_WATCHED = 200

ROLE_OPTION = "option"
ROLE_EQUITY = "equity"
ROLE_FUTURE = "future"
ROLE_INDEX = "index"


class QuoteLike(Protocol):
    ltp: float
    bid: float
    ask: float
    ts: float


@dataclass(slots=True)
class Watch:
    symbol: str
    role: str
    #: wall-clock second after which the watch lapses; ``inf`` while the contract is held
    until: float
    #: the contract is, or was until the grace runs out, an open position's
    held: bool = False
    #: wall-clock second the held flag lapses; ``inf`` while the contract is open. Separate from
    #: ``until`` because a candidate follow may keep the watch alive long past the grace.
    held_until: float = math.inf


class Tape:
    def __init__(
        self,
        archive: DailyArchive,
        *,
        enabled: bool = True,
        legs_for: Callable[[str], list[tuple[str, str]]] | None = None,
    ) -> None:
        self.archive = archive
        self.enabled = enabled
        #: the underlying's own legs for a symbol: ``[(scrip_code, role), …]``
        self.legs_for = legs_for or (lambda _symbol: [])
        self._watch: dict[str, Watch] = {}
        #: the last row written per code, so an unchanged quote is not written again
        self._last: dict[str, tuple[float, float, float, float, bool]] = {}
        self.rows = 0
        self.samples = 0
        self.dropped = 0
        self.capped = False
        self.last_sample_ts: float | None = None

    # -- what to follow --------------------------------------------------------------------------

    def follow(self, symbol: str, codes: Iterable[str], *, role: str = ROLE_OPTION, ttl: float = CANDIDATE_TTL_S, now: float) -> None:
        """Keep ``codes`` on tape for ``ttl`` seconds from ``now``. A held contract is never
        shortened by a candidate follow; a candidate renewed later just lives longer."""
        if not self.enabled:
            return
        until = now + ttl
        for code in codes:
            code = str(code)
            w = self._watch.get(code)
            if w is not None:
                if w.until < until:
                    w.until = until
                continue
            if len(self._watch) >= MAX_WATCHED:
                self.dropped += 1
                if not self.capped:
                    self.capped = True
                    log.warning("tape.capped", watched=len(self._watch), cap=MAX_WATCHED)
                continue
            self._watch[code] = Watch(symbol=symbol, role=role, until=until)

    def sample(
        self,
        now: float,
        quotes: Mapping[str, QuoteLike],
        held: Iterable[tuple[str, str, str]],
    ) -> int:
        """Record this second. ``held`` is ``(scrip_code, symbol, role)`` for every open position's
        contract; those pin their watch open and mark their symbol's rows as held."""
        if not self.enabled:
            return 0
        ts = int(now)
        held_symbols: set[str] = set()
        open_codes: set[str] = set()
        for code, symbol, role in held:
            code = str(code)
            open_codes.add(code)
            held_symbols.add(symbol)
            w = self._watch.get(code)
            if w is None:
                self._watch[code] = Watch(symbol=symbol, role=role, until=math.inf, held=True)
            else:
                w.until, w.role, w.held, w.held_until = math.inf, role, True, math.inf
        # a contract that was held and is not any more gets the grace, once; a lapsed watch goes
        for code, w in list(self._watch.items()):
            if code not in open_codes and w.until == math.inf:
                w.until = w.held_until = now + AFTER_CLOSE_S
            if w.until < now:
                self._watch.pop(code, None)
                self._last.pop(code, None)
                continue
            if w.held and now > w.held_until:
                w.held = False
            if w.held:
                held_symbols.add(w.symbol)
        # the legs of every symbol on tape, resolved each second: the front future can change
        # at a roll and the resolver is the engine's own (cached there)
        rows: list[tuple[str, str, str]] = [(code, w.symbol, w.role) for code, w in self._watch.items()]
        seen = {code for code, _, _ in rows}
        for symbol in {w.symbol for w in self._watch.values()}:
            for code, role in self.legs_for(symbol):
                code = str(code)
                if code not in seen:
                    seen.add(code)
                    rows.append((code, symbol, role))
        written = 0
        for code, symbol, role in rows:
            q = quotes.get(code)
            if q is None or (q.ltp <= 0 and q.bid <= 0 and q.ask <= 0):
                continue
            is_held = symbol in held_symbols
            # ``is_held`` is in the key: a contract that stops quoting the instant it is bought
            # would otherwise never write a held row, and the retention would then drop the trade.
            key = (float(q.ts), float(q.ltp), float(q.bid), float(q.ask), is_held)
            if self._last.get(code) == key:
                continue
            self._last[code] = key
            self.archive.quote(
                code,
                ts,
                symbol=symbol,
                role=role,
                ltp=q.ltp,
                bid=q.bid,
                ask=q.ask,
                quote_ts=q.ts,
                held=is_held,
            )
            written += 1
        self.rows += written
        self.samples += 1
        self.last_sample_ts = now
        return written

    def watched(self) -> dict[str, dict[str, Any]]:
        return {
            code: {"symbol": w.symbol, "role": w.role, "held": w.held, "open": w.until == math.inf}
            for code, w in self._watch.items()
        }

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "watched": len(self._watch),
            "held": sum(1 for w in self._watch.values() if w.until == math.inf),
            "rows": self.rows,
            "samples": self.samples,
            "dropped": self.dropped,
            "capped": self.capped,
            "last_sample_ts": self.last_sample_ts,
        }


# -- reading it back --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tick:
    ts: int
    ltp: float
    bid: float
    ask: float
    quote_ts: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else self.ltp

    @property
    def spread_pct(self) -> float | None:
        if self.bid <= 0 or self.ask <= 0:
            return None
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100 if mid > 0 else None


class Series:
    """One code's tape for one day, forward-filled: ``at(ts)`` is the quote the engine held at
    that second — the last row at or before it — or None before the first row."""

    def __init__(self, code: str, ticks: list[Tick]) -> None:
        self.code = code
        self.ticks = sorted(ticks, key=lambda t: t.ts)
        self._ts = [t.ts for t in self.ticks]

    def __len__(self) -> int:
        return len(self.ticks)

    @property
    def first_ts(self) -> int | None:
        return self._ts[0] if self._ts else None

    @property
    def last_ts(self) -> int | None:
        return self._ts[-1] if self._ts else None

    def at(self, ts: float) -> Tick | None:
        i = bisect_right(self._ts, int(ts))
        return self.ticks[i - 1] if i else None


def read_day(root: Path, day: str, *, codes: Iterable[str] | None = None) -> Any:
    """The tape for ``day`` as a DataFrame — ``quotes`` and ``quotes_held`` merged, since a day
    may be in either or, mid-prune, both. Empty frame when there is nothing."""
    import pandas as pd

    frames = []
    for kind in ("quotes", "quotes_held"):
        path = root / kind / f"{day}.parquet"
        if path.exists():
            try:
                frames.append(pd.read_parquet(path))
            except (OSError, ValueError) as exc:
                log.warning("tape.unreadable", path=str(path), error=str(exc)[:120])
    if not frames:
        return pd.DataFrame(columns=["scrip_code", "ts", "symbol", "role", "ltp", "bid", "ask", "quote_ts", "held"])
    df = pd.concat(frames, ignore_index=True).drop_duplicates(["scrip_code", "ts"], keep="last")
    if codes is not None:
        want = {str(c) for c in codes}
        df = df[df["scrip_code"].astype(str).isin(want)]
    return df.sort_values(["scrip_code", "ts"]).reset_index(drop=True)


def series(df: Any, code: str) -> Series:
    rows = df[df["scrip_code"].astype(str) == str(code)]
    ticks = [
        Tick(ts=int(r.ts), ltp=float(r.ltp), bid=float(r.bid), ask=float(r.ask), quote_ts=float(r.quote_ts))
        for r in rows.itertuples(index=False)
    ]
    return Series(str(code), ticks)


def days(root: Path) -> list[str]:
    """Every day with any tape, oldest first."""
    out: set[str] = set()
    for kind in ("quotes", "quotes_held"):
        d = root / kind
        if d.exists():
            out |= {p.stem for p in d.glob("*.parquet") if len(p.stem) == 10 and p.stem[4] == "-"}
    return sorted(out)


def summary(df: Any) -> list[dict[str, Any]]:
    """Per code: symbol, role, rows, first/last second, whether held. For the CLI and the API."""
    if df.empty:
        return []
    out = []
    for code, g in df.groupby("scrip_code", sort=True):
        out.append(
            {
                "scrip_code": str(code),
                "symbol": str(g["symbol"].iloc[0]),
                "role": str(g["role"].iloc[0]),
                "rows": len(g),
                "first_ts": int(g["ts"].min()),
                "last_ts": int(g["ts"].max()),
                "held": bool(g["held"].astype(bool).any()),
            }
        )
    out.sort(key=lambda r: (r["symbol"], r["role"], r["scrip_code"]))
    return out


__all__ = [
    "AFTER_CLOSE_S",
    "CANDIDATE_TTL_S",
    "MAX_WATCHED",
    "ROLE_EQUITY",
    "ROLE_FUTURE",
    "ROLE_INDEX",
    "ROLE_OPTION",
    "Series",
    "Tape",
    "Tick",
    "Watch",
    "days",
    "read_day",
    "series",
    "summary",
]
