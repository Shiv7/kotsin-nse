"""One open-interest reading, computed from the OI LEVEL (operator, 2026-10-02).

The broker's ``oi_change_pct`` field is 0.0 on every frame — 824,031 of 824,031 on 2026-10-01 across
6,493 contracts while the OI itself moved (JSWSTEEL OCT 40,453,425 → 40,321,800) — so a strategy that
read it scored a dead input as a real 0 % for its whole life (FUKAA, 21 Sep - 1 Oct: 227 decisions,
all at 0.0). The change is ours to compute:

* **against the previous session's closing OI** — the last print of the day before, from the
  engine's own OI archive or, running through midnight, the last level it held; failing both, the
  day's first print before the open (OI does not move pre-open). A reference the engine cannot vouch
  for is no reference: the reading is DOUBTFUL, never a guess;
* **on the current month's future** — and in the contract's last three sessions (expiry day
  included) on the current and next month SUMMED: positions roll, and the current month alone reads
  the roll (NIFTY50 average −59 % on the 29 Sep expiry day), not the positioning. Summed, not the
  mean of the two percentages: the next month starts small and its percentage balloons;
* **fresh** — OI updates about once a minute (median 65 s, 90th percentile 88 s on 1 Oct); a level
  older than ``max_age_s`` makes the reading doubtful.

A doubtful reading carries no number and says why. It decides nothing.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: the contract's last sessions (expiry day included) in which the next month is added in
ROLL_SESSIONS = 3
MAX_AGE_S = 300.0
#: the fewest NIFTY50 members a relative reading may stand on
MIN_PEERS = 25


@dataclass(frozen=True, slots=True)
class OiReading:
    change_pct: float | None = None
    #: the futures it was read on, nearest first
    contracts: tuple[str, ...] = ()
    ref_oi: float | None = None
    now_oi: float | None = None
    #: the oldest level it used, in seconds
    age_s: float | None = None
    doubt: str = ""
    #: where the previous close came from: "nse" (the exchange's bhavcopy), "archive" (the engine's
    #: own last print of that session) or "preopen" (today's first print before the open) — joined
    #: with "+" when the roll reads two contracts whose references came from different places
    ref_source: str = ""

    @property
    def ok(self) -> bool:
        return not self.doubt and self.change_pct is not None

    def to_json(self) -> dict[str, object]:
        return {"changePct": None if self.change_pct is None else round(self.change_pct, 3), "contracts": list(self.contracts),
                "refOi": self.ref_oi, "nowOi": self.now_oi, "ageS": None if self.age_s is None else round(self.age_s, 1),
                "doubt": self.doubt, "refSource": self.ref_source}


def read_oi(
    futures: Sequence[str],
    *,
    sessions_left: int,
    levels: Mapping[str, tuple[float, float]],
    refs: Mapping[str, float],
    now: float,
    max_age_s: float = MAX_AGE_S,
    ref_sources: Mapping[str, str] | None = None,
) -> OiReading:
    """``futures``: the unexpired futures' codes, nearest first. ``sessions_left``: sessions to the
    nearest one's expiry, today and the expiry day both counted. ``levels``: code → (OI, when);
    ``refs``: code → the previous session's closing OI; ``ref_sources``: code → where that came from."""
    if not futures:
        return OiReading(doubt="no unexpired future")
    use = list(futures[:2]) if sessions_left <= ROLL_SESSIONS and len(futures) >= 2 else [futures[0]]
    ref_sum = now_sum = 0.0
    oldest = 0.0
    for code in use:
        ref = refs.get(code)
        if ref is None or ref <= 0:
            return OiReading(contracts=tuple(use), doubt=f"no previous-close OI for {code}")
        lv = levels.get(code)
        if lv is None or lv[0] <= 0:
            return OiReading(contracts=tuple(use), doubt=f"no OI print for {code}")
        oldest = max(oldest, now - lv[1])
        ref_sum += ref
        now_sum += lv[0]
    src = "+".join(dict.fromkeys((ref_sources or {}).get(c, "") for c in use if (ref_sources or {}).get(c)))
    if oldest > max_age_s:
        return OiReading(contracts=tuple(use), ref_oi=ref_sum, now_oi=now_sum, age_s=oldest,
                         doubt=f"OI {oldest:.0f}s old (> {max_age_s:.0f}s)", ref_source=src)
    return OiReading(change_pct=(now_sum / ref_sum - 1.0) * 100.0, contracts=tuple(use), ref_oi=ref_sum, now_oi=now_sum,
                     age_s=oldest, ref_source=src)


def relative_z(own: float, peers: Sequence[float], *, min_peers: int = MIN_PEERS) -> tuple[float | None, int]:
    """How far ``own`` stands from the peers' cross-section, in their own spread — so a quiet month,
    when every change is small, still lets a stock that stands out stand out. ``(None, n)`` when
    there are too few peers or no spread."""
    n = len(peers)
    if n < min_peers:
        return None, n
    sd = statistics.pstdev(peers)
    if sd <= 0:
        return None, n
    return (own - statistics.fmean(peers)) / sd, n


def oi_quadrant(price_change_pct: float | None, oi_change_pct: float | None) -> str | None:
    """The price against the OI, both since the previous close: price up on rising OI is a long
    build-up, down on rising OI a short build-up, up on falling OI short covering, down on falling
    OI long unwinding. None when either is unknown or flat."""
    if not price_change_pct or not oi_change_pct:
        return None
    if oi_change_pct > 0:
        return "long build-up" if price_change_pct > 0 else "short build-up"
    return "short covering" if price_change_pct > 0 else "long unwinding"
