"""The focus list: the underlyings whose options the market actually trades, from the last
session's NSE bhavcopy — the ones that get the deep treatment (the front future's book, ATM ± N
strikes with their books).

Ranked by option premium turnover, Σ close × volume over every stock option of the root (the
bhavcopy's volume unit is the same for every row of one file, so the ranking does not depend on
it). A fixed list for the day: changing it intraday would break each name's recorded book.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import pandas as pd


def focus_roots(
    bhav: pd.DataFrame, top_n: int, indices: Iterable[str] = (), *, renames: Mapping[str, str] | None = None
) -> list[str]:
    """The ``top_n`` stock roots by option premium turnover, then ``indices`` (in that order)."""
    ren = dict(renames or {})
    opt = bhav[bhav["kind"] == "OPTSTK"]
    turnover = (opt["close"].fillna(0) * opt["volume"].fillna(0)).groupby(opt["symbol"].map(lambda s: ren.get(s, s))).sum()
    stocks = [s for s in turnover.sort_values(ascending=False).index if turnover[s] > 0][:top_n]
    return [*stocks, *[i for i in indices if i not in stocks]]


__all__ = ["focus_roots"]
