"""NSE's F&O bhavcopy: the official end-of-day open interest of every contract (operator, 2026-10-03).

5paisa gives us the OI LEVEL live and nothing else: its ``OIChangePercent`` is 0.0 on every frame
(5,931 of 5,931 archived on 2026-10-02, 824,031 of 824,031 on 1 Oct), its historical candles carry
no OI, so a session the engine did not record is an OI close nobody can recover from the broker. The
exchange publishes that close itself, every evening, for every contract:

  ``nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip``

One row per contract (33,250 on 2026-10-01): ``FinInstrmId`` is NSE's contract token, and it IS
5paisa's ``ScripCode`` — 33,200 of 33,250 joined on it with not one symbol or expiry disagreeing
(the 50 others had left the scrip master). ``OpnIntrst`` is the official closing OI and
``ChngInOpnIntrst`` the official change from the session before (JSWSTEEL OCT, 1 Oct: 39,903,300,
−448,200 — the number the broker's change field reports as 0).

So this file is the previous close an OI change is measured from whenever the engine's own archive
does not have it, and the daily OI history the backtests never had.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import structlog

log = structlog.get_logger(__name__)

URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"

#: NSE answers a 403 and an HTML challenge to anything that does not look like a browser.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/zip,*/*",
}

#: the exchange's instrument types, in the names the rest of the engine uses
KINDS = {"STF": "FUTSTK", "IDF": "FUTIDX", "STO": "OPTSTK", "IDO": "OPTIDX"}

COLUMNS = ("scrip_code", "symbol", "kind", "expiry", "strike", "option_type",
           "close", "settle", "oi", "oi_change", "volume")


def url_for(day: date) -> str:
    return URL.format(yyyymmdd=day.strftime("%Y%m%d"))


def parse(blob: bytes) -> pd.DataFrame:
    """The zipped CSV → one row per contract, ``COLUMNS``. Raises ``ValueError`` on anything that is
    not a bhavcopy (NSE serves an HTML page with a 200 on some failures)."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
            if name is None:
                raise ValueError("no CSV in the archive")
            raw = pd.read_csv(z.open(name), low_memory=False)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a zip: {exc}") from exc
    need = {"FinInstrmId", "TckrSymb", "FinInstrmTp", "XpryDt", "OpnIntrst", "ChngInOpnIntrst"}
    if not need <= set(raw.columns):
        raise ValueError(f"not an F&O bhavcopy: missing {sorted(need - set(raw.columns))}")
    raw = raw[raw.FinInstrmTp.isin(KINDS)]
    out = pd.DataFrame({
        "scrip_code": raw.FinInstrmId.astype("int64").astype(str),
        "symbol": raw.TckrSymb.astype(str),
        "kind": raw.FinInstrmTp.map(KINDS),
        "expiry": raw.XpryDt.astype(str),
        "strike": pd.to_numeric(raw.get("StrkPric"), errors="coerce"),
        "option_type": raw.get("OptnTp").fillna("").astype(str) if "OptnTp" in raw else "",
        "close": pd.to_numeric(raw.get("ClsPric"), errors="coerce"),
        "settle": pd.to_numeric(raw.get("SttlmPric"), errors="coerce"),
        "oi": pd.to_numeric(raw.OpnIntrst, errors="coerce").fillna(0.0).astype(float),
        "oi_change": pd.to_numeric(raw.ChngInOpnIntrst, errors="coerce").fillna(0.0).astype(float),
        "volume": pd.to_numeric(raw.get("TtlTradgVol"), errors="coerce").fillna(0.0).astype(float),
    })
    return out.reset_index(drop=True)


class OiDailyStore:
    """One Parquet file per session, ``<root>/<YYYY-MM-DD>.parquet``, written from the bhavcopy.

    Deliberately a store of the exchange's own numbers and nothing else: the engine's archive is the
    other source of a previous close, and the two are never merged into one file, so it is always
    known which one a reference came from."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, day: date) -> Path:
        return self.root / f"{day.isoformat()}.parquet"

    def has(self, day: date) -> bool:
        return self.path(day).exists()

    def days(self) -> list[date]:
        if not self.root.exists():
            return []
        out = []
        for p in self.root.glob("*.parquet"):
            try:
                out.append(date.fromisoformat(p.stem))
            except ValueError:
                continue
        return sorted(out)

    def write(self, day: date, df: pd.DataFrame) -> Path:
        """Every future, and every option that holds OI or traded: a strike with neither has no
        close to measure from and no price that day — 21,541 of 37,268 rows on a session, two
        thirds of the file. Kept, the rest is also the daily option-price history 5paisa lacks."""
        self.root.mkdir(parents=True, exist_ok=True)
        df = df[(df.oi > 0) | (df.volume > 0) | df.kind.isin(("FUTSTK", "FUTIDX"))]
        tmp = self.path(day).with_suffix(".tmp")
        df.to_parquet(tmp, index=False, compression="zstd")  # ~33k contracts a session; zstd keeps a year small
        tmp.replace(self.path(day))
        return self.path(day)

    def load(self, day: date, columns: Iterable[str] | None = None) -> pd.DataFrame:
        p = self.path(day)
        if not p.exists():
            return pd.DataFrame(columns=list(columns or COLUMNS))
        return pd.read_parquet(p, columns=list(columns) if columns else None)

    def closes(self, day: date, codes: Iterable[str]) -> dict[str, float]:
        """The official closing OI of ``codes`` on ``day`` — only contracts with OI above zero (a
        contract nobody holds has nothing to measure a change from)."""
        want = {str(c) for c in codes}
        if not want or not self.has(day):
            return {}
        df = self.load(day, columns=("scrip_code", "oi"))
        df = df[df.scrip_code.isin(want) & (df.oi > 0)]
        return {str(c): float(v) for c, v in zip(df.scrip_code, df.oi, strict=True)}


async def fetch_day(http: Any, day: date, *, wait_s: float = 45.0) -> pd.DataFrame | None:
    """The bhavcopy of ``day``, or None when NSE has none (a holiday, a weekend, not yet published
    — it appears in the evening) or answers with something that is not one."""
    try:
        r = await http.get(url_for(day), headers=HEADERS, timeout=wait_s)
    except Exception as exc:  # noqa: BLE001 - no file is no file; the caller decides what that means
        log.warning("fo_bhavcopy.fetch_failed", day=day.isoformat(), error=str(exc)[:120])
        return None
    if r.status_code != 200:
        log.info("fo_bhavcopy.unavailable", day=day.isoformat(), status=r.status_code)
        return None
    try:
        return parse(r.content)
    except ValueError as exc:
        log.warning("fo_bhavcopy.unreadable", day=day.isoformat(), error=str(exc)[:120])
        return None


async def backfill(
    http: Any, store: OiDailyStore, start: date, end: date, *, pace_s: float = 0.4, refetch: bool = False,
) -> dict[str, int]:
    """Every weekday in ``[start, end]`` the store does not hold yet. NSE's own calendar decides
    which of them were sessions: a holiday simply has no file."""
    out = {"fetched": 0, "held": 0, "missing": 0}
    d = start
    while d <= end:
        if d.weekday() < 5:
            if store.has(d) and not refetch:
                out["held"] += 1
            else:
                df = await fetch_day(http, d)
                if df is None or df.empty:
                    out["missing"] += 1
                else:
                    store.write(d, df)
                    out["fetched"] += 1
                await asyncio.sleep(pace_s)
        d += timedelta(days=1)
    return out


__all__ = ["COLUMNS", "KINDS", "OiDailyStore", "backfill", "fetch_day", "parse", "url_for"]
