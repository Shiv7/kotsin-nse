"""Which stocks had stock futures on a given day: the F&O universe as it was, not as it is.

The history cache holds today's ~217 F&O stocks back to 2021, so a backtest that trades every one of
them on every day also trades names NSE had not admitted yet. Those names are not random: a stock is
admitted after it has grown, which is the move the backtest then "catches". In the swing run
``swing-266ee719c5``, the 2024–25 trades in stocks with futures on the day made −1.8 R (40 trades)
and the 18 in stocks not yet admitted (HINDZINC, ANGELONE, DMART, IRFC…) made +22.5 R.

Built from NSE's own bhavcopies (``oi_daily_legacy/`` before 2024, ``oi_daily/`` from 2024): a stock
is a member on every session its FUTSTK rows appear. On a day with no file (a holiday) the latest
earlier session stands, which is also all anyone knew that morning. Old tickers are mapped to today's
through NSE's symbol-change list (``MCDOWELL-N`` → ``UNITDSPR``), so a renamed stock is not mistaken
for a newcomer.
"""

from __future__ import annotations

import bisect
import csv
import io
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

SYMBOL_CHANGES_URL = "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"
SYMBOL_CHANGES_FILE = "nse_symbolchange.csv"

#: how long after the last file the latest session still answers (a long weekend, tonight's file)
STALE_DAYS = 7


def parse_symbol_changes(text: str) -> dict[str, str]:
    """NSE's ``symbolchange.csv`` → {old ticker: new ticker}."""
    out: dict[str, str] = {}
    rows = csv.reader(io.StringIO(text))
    next(rows, None)
    for row in rows:
        if len(row) >= 3 and row[1].strip() and row[2].strip():
            out[row[1].strip().upper()] = row[2].strip().upper()
    return out


def canonical(symbol: str, changes: dict[str, str]) -> str:
    """Today's ticker for ``symbol``, following renames of renames (a cycle stops where it closes)."""
    seen = {symbol}
    while symbol in changes and changes[symbol] not in seen:
        symbol = changes[symbol]
        seen.add(symbol)
    return symbol


@dataclass(frozen=True, slots=True)
class FoMembership:
    sessions: tuple[date, ...]
    members: tuple[frozenset[str], ...]  # aligned with ``sessions``, today's tickers
    changes: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_sessions(cls, by_day: dict[date, set[str]], changes: dict[str, str] | None = None) -> FoMembership:
        days = sorted(by_day)
        ch = changes or {}
        return cls(tuple(days), tuple(frozenset(canonical(s, ch) for s in by_day[d]) for d in days), tuple(ch.items()))

    def _name(self, symbol: str) -> str:
        """A caller's ticker may itself be an old one (the history cache keeps ICICIPRULI after NSE
        renamed it ICICILIFE on 2026-10-23), so it is mapped forward the same way."""
        return canonical(symbol, dict(self.changes)) if self.changes else symbol

    def covers(self, day: date) -> bool:
        return bool(self.sessions) and self.sessions[0] <= day <= self.sessions[-1] + timedelta(days=STALE_DAYS)

    def member(self, symbol: str, day: date) -> bool:
        """True when the latest session on or before ``day`` lists stock futures of ``symbol``.
        A day the files do not cover raises: an unknown is never read as a no or a yes."""
        if not self.covers(day):
            raise ValueError(f"F&O membership covers {self.first} … {self.last}, not {day}")
        return self._name(symbol) in self.members[bisect.bisect_right(self.sessions, day) - 1]

    @property
    def first(self) -> date | None:
        return self.sessions[0] if self.sessions else None

    @property
    def last(self) -> date | None:
        return self.sessions[-1] if self.sessions else None

    def intervals(self, symbol: str) -> list[tuple[date, date]]:
        """The runs of consecutive sessions ``symbol`` was a member, first and last session of each."""
        symbol = self._name(symbol)
        out: list[tuple[date, date]] = []
        run: date | None = None
        prev: date | None = None
        for d, m in zip(self.sessions, self.members, strict=True):
            if symbol in m:
                run = run or d
                prev = d
            elif run is not None and prev is not None:
                out.append((run, prev))
                run = None
        if run is not None and prev is not None:
            out.append((run, prev))
        return out


def load(data_dir: Path) -> tuple[FoMembership, int]:
    """Membership from every bhavcopy held, and how many renames were applied (0 when NSE's
    symbol-change list has not been fetched: renamed stocks then look new before their rename)."""
    by_day: dict[date, set[str]] = {}
    for sub in ("oi_daily_legacy", "oi_daily"):  # the newer file wins a day both hold
        root = data_dir / sub
        if not root.exists():
            continue
        for p in sorted(root.glob("*.parquet")):
            try:
                d = date.fromisoformat(p.stem)
            except ValueError:
                continue
            df = pd.read_parquet(p, columns=["symbol", "kind"])
            by_day[d] = set(df.loc[df.kind == "FUTSTK", "symbol"].astype(str))
    f = data_dir / SYMBOL_CHANGES_FILE
    changes = parse_symbol_changes(f.read_text(errors="replace")) if f.exists() else {}
    fm = FoMembership.from_sessions(by_day, changes)
    used = {s for m in by_day.values() for s in m if s in changes}
    return fm, len(used)


__all__ = ["SYMBOL_CHANGES_FILE", "SYMBOL_CHANGES_URL", "FoMembership", "canonical", "load", "parse_symbol_changes"]
