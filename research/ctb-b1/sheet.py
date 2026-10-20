"""The 13 traded rows of FUDKII_CT_Buckets_14-19Oct2026 (sheet B1_GAP-FADE), as the sheet states them, and the
option's own raw pivots (classic P, TC, BC, R1-R4, S1-S4 of its previous day, week and month)."""

from __future__ import annotations

import glob
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from kotsin_nse.bars.pivots import classic_pivots

DATA = Path(__file__).resolve().parent / "data"
TICK = 0.05
LEVELS = ("pivot", "tc", "bc", "r1", "r2", "r3", "r4", "s1", "s2", "s3", "s4")


@dataclass(frozen=True)
class Row:
    xl_row: int
    day: str
    symbol: str
    option: str  # file tag
    entry: float  # K
    lot_sheet: int  # X as the sheet has it
    lot: int  # the exchange's lot (scrip master, 20 Oct)
    t: tuple[float, float, float]  # N, P, R as entered (0 = none)
    peak: float  # T
    brokerage: float  # AA as entered
    sheet_net: float  # AG as the sheet computes it


ROWS = [
    Row(3, "2026-10-14", "JSWENERGY", "JSWENERGY_490CE", 8.55, 1075, 1075, (11.5, 0, 0), 13.3, 60, 17029.29),
    Row(4, "2026-10-15", "ENRIN", "ENRIN_3000PE", 60, 1076, 175, (71.38, 95.08, 0), 97.8, 80, 124197.09),
    Row(5, "2026-10-15", "GVT&D", "GVTandD_4100CE", 75.7, 125, 125, (0, 0, 0), 89.8, 40, 5568.84),
    Row(7, "2026-10-15", "AXISBANK", "AXISBANK_1300CE", 17.45, 625, 625, (18.7, 0, 0), 20.6, 60, 5359.40),
    Row(8, "2026-10-16", "GODREJCP", "GODREJCP_850CE", 13.55, 500, 500, (15.75, 0, 0), 16.8, 60, 5084.12),
    Row(9, "2026-10-16", "SWIGGY", "SWIGGY_230PE", 5.4, 1825, 1825, (5.9, 0, 0), 5.9, 60, 2524.78),
    Row(10, "2026-10-16", "VOLTAS", "VOLTAS_1020PE", 14.75, 375, 375, (15.88, 18, 18.1), 18.5, 100, 3926.82),
    Row(11, "2026-10-19", "PNB", "PNB_122PE", 1.3, 8000, 8000, (0, 0, 0), 1.9, 40, 17215.45),
    Row(12, "2026-10-19", "AXISBANK", "AXISBANK_1240CE", 13.65, 625, 625, (0, 0, 0), 15.3, 40, 2855.36),
    Row(13, "2026-10-19", "PREMIERENE", "PREMIERENE_920PE", 13, 625, 650, (0, 0, 0), 17, 40, 8597.01),
    Row(14, "2026-10-19", "SBICARD", "SBICARD_600CE", 4.8, 800, 800, (0, 0, 0), 5.5, 40, 1630.48),
    Row(15, "2026-10-19", "UNIONBANK", "UNIONBANK_180CE", 3.39, 4425, 4425, (4.05, 0, 0), 4.2, 60, 11787.21),
    Row(16, "2026-10-19", "UNITDSPR", "UNITDSPR_1400CE", 8.85, 400, 400, (10.57, 0, 0), 10.9, 0, 2722.07),
]


def load(tag: str, interval: str) -> pd.DataFrame:
    df = pd.read_parquet(glob.glob(str(DATA / f"{tag}_*_{interval}.parquet"))[0])
    df["day"] = df["dt"].str[:10]
    return df


def _hlc(rows: pd.DataFrame) -> tuple[float, float, float] | None:
    if rows.empty:
        return None
    return float(rows["h"].max()), float(rows["l"].min()), float(rows.sort_values("dt")["c"].iloc[-1])


def prior_periods(daily: pd.DataFrame, day: str) -> dict[str, tuple[float, float, float] | None]:
    """The option's previous day, previous calendar week (Mon-Fri) and previous calendar month, before ``day``."""
    d = date.fromisoformat(day)
    before = daily[daily["day"] < day].sort_values("day")
    monday = d - timedelta(days=d.weekday())
    prev_monday = monday - timedelta(days=7)
    first = d.replace(day=1)
    prev_first = (first - timedelta(days=1)).replace(day=1)
    return {
        "1d": _hlc(before.tail(1)),
        "1wk": _hlc(before[(before["day"] >= prev_monday.isoformat()) & (before["day"] < monday.isoformat())]),
        "1mo": _hlc(before[(before["day"] >= prev_first.isoformat()) & (before["day"] < first.isoformat())]),
    }


def raw_pivots(daily: pd.DataFrame, day: str) -> list[tuple[float, str]]:
    """Every raw pivot of the option for ``day``, ascending: (price, "1d.r1")."""
    out: list[tuple[float, str]] = []
    for tf, hlc in prior_periods(daily, day).items():
        lv = classic_pivots(*hlc) if hlc else None
        if lv is None:
            continue
        out += [(getattr(lv, name), f"{tf}.{name}") for name in LEVELS]
    return sorted(out)


def to_tick(x: float, tick: float = TICK) -> float:
    return round(round(x / tick) * tick, 2)
