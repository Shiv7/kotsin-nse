"""The sheet's exit ladder, replayed on 1-minute candles. Rules as the sheet's headers and Devina's answers of 21 Oct:

* 4 lots bought at the entry price K at 09:45;
* before arming: stop at -25 % of K, on a touch;
* arming: the price reaching K + 5 %;
* targets: the option's raw pivots above the entry, ascending, rounded to the tick; one lot sells at each of the first
  three touched (a target below the arm line is still a target - default, flagged);
* after arming, the remaining lots trail the peak: a 5-minute candle (09:15 grid, built from the 1m bars) that CLOSES
  3 % or more under the peak exits at that close; a drop to 5 % under the peak inside a candle exits at once;
* everything left goes at 15:19.

Pessimistic where a minute is ambiguous: in one candle a stop counts before a target, and a 5 % dip before a new peak.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

ARM_PCT, STOP_PCT, DIP_CLOSE_PCT, DIP_HARD_PCT = 0.05, 0.25, 0.03, 0.05
LOTS, MAX_TARGETS = 4, 3
LAST_HM = "15:19"


@dataclass
class Fill:
    hm: str
    price: float
    lots: int
    why: str


@dataclass
class Result:
    fills: list[Fill] = field(default_factory=list)
    armed_hm: str | None = None
    peak: float = 0.0
    peak_hm: str | None = None
    targets: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def lots_out(self) -> int:
        return sum(f.lots for f in self.fills)

    def revenue(self, lot: int) -> float:
        return sum(f.price * f.lots * lot for f in self.fills)


def replay(bars: pd.DataFrame, entry: float, pivots_above: list[float], entry_hm: str = "09:45", *, lenient: bool = False,
           dip_close: float = DIP_CLOSE_PCT, dip_hard: float = DIP_HARD_PCT) -> Result:
    """``bars``: one day's 1m candles with columns hm, o, h, l, c (ascending). ``lenient``: the -5 % hard dip is read on
    the minute's CLOSE (a sustained print, not a wick) and never in the minute that armed - the order of ticks inside a
    minute is unknowable from 1m bars; the default assumes the worst order."""
    r = Result(targets=pivots_above[:MAX_TARGETS])
    arm, stop = entry * (1 + ARM_PCT), entry * (1 - STOP_PCT)
    left = LOTS
    hit = 0  # targets taken
    armed = False
    peak = entry
    cands = bars[bars["hm"] >= entry_hm].sort_values("hm")
    # 5-minute buckets on the 09:15 grid: minutes 15-19 -> one candle, etc.
    cands = cands.assign(b5=cands["hm"].map(lambda s: (int(s[:2]) * 60 + int(s[3:]) - 555) // 5))
    for b5, grp in cands.groupby("b5", sort=True):
        c5_close = float(grp["c"].iloc[-1])
        c5_last_hm = str(grp["hm"].iloc[-1])
        for _, m in grp.iterrows():
            if left == 0:
                break
            hm, h, lo = str(m["hm"]), float(m["h"]), float(m["l"])
            if hm > LAST_HM:
                break
            if not armed:
                if lo <= stop:  # stop first: the pessimistic order
                    r.fills.append(Fill(hm, stop, left, "stop -25%"))
                    left = 0
                    break
                if h >= arm:
                    armed, r.armed_hm = True, hm
                    peak, r.peak_hm = max(peak, h), hm
            if armed:
                # a hard dip inside the minute counts before this minute's new high
                dip_px = float(m["c"]) if lenient else lo
                if lenient and hm == r.armed_hm:
                    dip_px = peak  # the arming minute: its high is taken to come last
                if dip_px <= peak * (1 - dip_hard) and left:
                    r.fills.append(Fill(hm, round(peak * (1 - dip_hard), 2), left, f"-5% from peak {peak}"))
                    left = 0
                    break
                if h > peak:
                    peak, r.peak_hm = h, hm
            while left and hit < len(r.targets) and h >= r.targets[hit]:
                r.fills.append(Fill(hm, r.targets[hit], 1, f"T{hit + 1}"))
                hit += 1
                left -= 1
        if left == 0:
            break
        if armed and c5_close <= peak * (1 - dip_close) and c5_last_hm <= LAST_HM:
            r.fills.append(Fill(c5_last_hm, c5_close, left, f"5m close -3% from peak {peak}"))
            left = 0
            break
    if left:
        last = cands[cands["hm"] <= LAST_HM].iloc[-1]
        r.fills.append(Fill(str(last["hm"]), float(last["c"]), left, "15:19 exit"))
    r.peak = peak
    if r.targets and r.targets[0] < arm:
        r.notes.append(f"T1 {r.targets[0]} sits below the +5% arm {arm:.2f}: taken on touch (default)")
    return r
