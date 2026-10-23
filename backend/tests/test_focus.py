"""The focus list and the strikes it gets: the most traded option roots, ATM ± N around the live price."""

from __future__ import annotations

import pandas as pd

from kotsin_nse.instrument.focus import focus_roots


def test_the_most_traded_option_roots_then_the_indices():
    rows = [("OPTSTK", "AAA", 10.0, 1000), ("OPTSTK", "AAA", 5.0, 100), ("OPTSTK", "BBB", 1.0, 50_000),
            ("OPTSTK", "OLDNAME", 2.0, 3000), ("FUTSTK", "CCC", 900.0, 10**7), ("OPTIDX", "NIFTY", 100.0, 10**6)]
    bhav = pd.DataFrame(rows, columns=["kind", "symbol", "close", "volume"])
    assert focus_roots(bhav, 2, ["NIFTY", "BANKNIFTY"]) == ["BBB", "AAA", "NIFTY", "BANKNIFTY"]
    assert focus_roots(bhav, 3, [], renames={"OLDNAME": "NEWNAME"})[2] == "NEWNAME"
