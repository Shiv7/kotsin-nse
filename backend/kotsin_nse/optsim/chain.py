"""The index option chain as NSE's daily bhavcopy settles it: one ``Day`` per session and index root, the futures by
expiry, the options by expiry, strike and type, with Black-76 implied volatility and delta on the matching future.
Settlement closes, not bids and asks: a simulator on them is an upper bound on what a live book sees (``sim.Costs``
charges slippage for that). The store reads ``data-live/oi_daily/<day>.parquet`` (``OiDailyStore``), the file the
engine fetches from NSE each session."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from ..fno.chain import black76, greeks76, implied_vol

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
INDEX_KINDS = ("OPTIDX", "FUTIDX")


def dte(expiry: str, day: str) -> int:
    return (datetime.date.fromisoformat(expiry) - datetime.date.fromisoformat(day)).days


def years(expiry: str, day: str) -> float:
    return max(dte(expiry, day), 0) / 365.0 + 1e-6


@dataclass(slots=True)
class Day:
    day: str
    symbol: str
    futures: dict[str, float]  # expiry -> close
    options: pd.DataFrame  # expiry, strike, option_type, close, oi, volume
    _ivs: dict[tuple[str, float, str], float | None] = field(default_factory=dict)

    @property
    def expiries(self) -> list[str]:
        return sorted(self.options["expiry"].unique())

    @property
    def monthlies(self) -> list[str]:
        """The expiries that have a future: the monthly series (the weeklies have none)."""
        return sorted(e for e in self.expiries if e in self.futures)

    @property
    def weeklies(self) -> list[str]:
        return sorted(e for e in self.expiries if e not in self.futures)

    def future(self, expiry: str) -> float | None:
        """The matching future's close, else the front future's (the nearest listed)."""
        if expiry in self.futures:
            return self.futures[expiry]
        if not self.futures:
            return None
        return self.futures[min(self.futures)]

    def strikes(self, expiry: str) -> list[float]:
        return sorted(float(k) for k in self.options[self.options["expiry"] == expiry]["strike"].unique())

    def close(self, expiry: str, strike: float, kind: str) -> float | None:
        r = self.options[(self.options["expiry"] == expiry) & (self.options["strike"] == strike) & (self.options["option_type"] == kind)]
        return float(r["close"].iloc[0]) if len(r) else None

    def iv(self, expiry: str, strike: float, kind: str) -> float | None:
        key = (expiry, strike, kind)
        if key not in self._ivs:
            f, px = self.future(expiry), self.close(expiry, strike, kind)
            self._ivs[key] = implied_vol(px, f, strike, years(expiry, self.day), kind == "CE") if f and px else None
        return self._ivs[key]

    def delta(self, expiry: str, strike: float, kind: str) -> float | None:
        iv, f = self.iv(expiry, strike, kind), self.future(expiry)
        if iv is None or not f:
            return None
        g = greeks76(f, strike, years(expiry, self.day), iv, kind == "CE")
        return g["delta"] if g else None

    def atm(self, expiry: str) -> float | None:
        f, ks = self.future(expiry), self.strikes(expiry)
        return min(ks, key=lambda k: abs(k - f)) if f and ks else None

    def strike_by_delta(self, expiry: str, kind: str, target: float, *, otm: bool = True) -> float | None:
        """The strike whose |delta| is nearest ``target``; out of the money only by default (calls above the future,
        puts below), since every playbook sells and hedges out of the money."""
        f = self.future(expiry)
        if not f:
            return None
        best, bk = 9.0, None
        for k in self.strikes(expiry):
            if otm and ((kind == "CE" and k <= f) or (kind == "PE" and k >= f)):
                continue
            d = self.delta(expiry, k, kind)
            if d is None:
                continue
            if abs(abs(d) - target) < best:
                best, bk = abs(abs(d) - target), k
        return bk

    def strike_by_premium(self, expiry: str, kind: str, premium: float, *, otm: bool = True) -> float | None:
        """The out-of-the-money strike whose close is nearest ``premium`` (the premium-balancing roll)."""
        f = self.future(expiry)
        if not f:
            return None
        best, bk = 1e18, None
        for k in self.strikes(expiry):
            if otm and ((kind == "CE" and k <= f) or (kind == "PE" and k >= f)):
                continue
            px = self.close(expiry, k, kind)
            if px is None:
                continue
            if abs(px - premium) < best:
                best, bk = abs(px - premium), k
        return bk

    def straddle(self, expiry: str) -> tuple[float, float, float] | None:
        """(ATM strike, straddle close, its implied vol in %) for the expiry."""
        k = self.atm(expiry)
        if k is None:
            return None
        ce, pe = self.close(expiry, k, "CE"), self.close(expiry, k, "PE")
        if ce is None or pe is None:
            return None
        ivs = [v for v in (self.iv(expiry, k, "CE"), self.iv(expiry, k, "PE")) if v is not None]
        return k, ce + pe, (sum(ivs) / len(ivs) * 100) if ivs else float("nan")

    def model_price(self, expiry: str, strike: float, kind: str, iv: float) -> float | None:
        f = self.future(expiry)
        return black76(f, strike, years(expiry, self.day), iv, kind == "CE") if f else None


class Store:
    """The bhavcopy days on disk, one index root, loaded on demand and cached (the last ``keep`` days)."""

    def __init__(self, root: Path, symbol: str = "NIFTY", keep: int = 800) -> None:
        self.root = Path(root)
        self.symbol = symbol
        self.keep = keep
        self._cache: dict[str, Day] = {}

    @property
    def days(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("2*.parquet"))

    def has(self, day: str) -> bool:
        return (self.root / f"{day}.parquet").exists()

    def get(self, day: str) -> Day:
        if day not in self._cache:
            if len(self._cache) >= self.keep:
                self._cache.pop(next(iter(self._cache)))
            t = pq.read_table(self.root / f"{day}.parquet", columns=["symbol", "kind", "expiry", "strike", "option_type", "close", "oi", "volume"],
                              filters=[("symbol", "==", self.symbol), ("kind", "in", list(INDEX_KINDS))]).to_pandas()
            t["expiry"] = t["expiry"].astype(str)
            fut = {str(r.expiry): float(r.close) for r in t[t["kind"] == "FUTIDX"].itertuples() if r.close and r.close > 0}
            opt = t[t["kind"] == "OPTIDX"][["expiry", "strike", "option_type", "close", "oi", "volume"]].copy()
            opt["strike"] = opt["strike"].astype(float)
            self._cache[day] = Day(day, self.symbol, fut, opt.reset_index(drop=True))
        return self._cache[day]

    @staticmethod
    def from_frame(day: str, symbol: str, frame: pd.DataFrame) -> Day:
        """A ``Day`` from an in-memory bhavcopy-shaped frame (tests)."""
        t = frame[(frame["symbol"] == symbol) & (frame["kind"].isin(INDEX_KINDS))].copy()
        t["expiry"] = t["expiry"].astype(str)
        fut = {str(r.expiry): float(r.close) for r in t[t["kind"] == "FUTIDX"].itertuples()}
        opt = t[t["kind"] == "OPTIDX"][["expiry", "strike", "option_type", "close", "oi", "volume"]].copy()
        opt["strike"] = opt["strike"].astype(float)
        return Day(day, symbol, fut, opt.reset_index(drop=True))

    def next_days(self, after: str, n: int) -> list[str]:
        ds = self.days
        return [d for d in ds if d > after][:n]

    def monthly_expiries(self, days: list[str] | None = None) -> list[str]:
        """Every monthly expiry seen in the store (the expiries with a future), sorted."""
        out: set[str] = set()
        for d in (days or self.days)[::5]:
            out |= set(self.get(d).monthlies)
        return sorted(out)


__all__ = ["IST", "Day", "Store", "dte", "np", "years"]
