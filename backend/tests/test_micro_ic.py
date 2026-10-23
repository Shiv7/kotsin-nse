"""The IC test reads a minute's order flow only after the minute closes, and finds a reading that
really leads the next minutes."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from kotsin_nse.market.session import ist_at
from kotsin_nse.research.micro_ic import day_frame, run

DAY = date(2026, 10, 26)


def _write(tmp_path, lead: bool) -> None:
    rng = np.random.default_rng(7)
    t0 = ist_at(DAY, "09:30")
    bars, flow = [], []
    for k in range(30):
        code = str(1000 + k)
        px = 100.0
        for m in range(60):
            ts = t0 + 60 * m
            push = rng.normal()
            # the reading at minute m leads the move over the next minutes (or is pure noise)
            px *= 1 + 0.0005 * (flow[-1]["ofi"] if lead and flow and flow[-1]["code"] == code else rng.normal())
            bars.append({"symbol": f"S{k}", "scrip_code": code, "ts": ts, "c": px, "source": "live"})
            flow.append({"code": code, "ts": ts, "buy": 10.0, "sell": 10.0, "delta": float(rng.normal()), "imbalance": 0.0, "ofi": push,
                         "ofi_deep": push, "qi": push, "micro_bps": 0.0, "wimb": 0.0, "bid_depth": 1.0, "ask_depth": 1.0})
    for kind, rows in (("bars", bars), ("flow", flow)):
        (tmp_path / "archive" / kind).mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(tmp_path / "archive" / kind / f"{DAY}.parquet")


def test_a_reading_that_leads_has_a_high_ic_and_noise_does_not(tmp_path):
    _write(tmp_path, lead=True)
    rows = {(r["signal"], r["horizon_min"]): r for r in run(tmp_path, DAY, DAY)["rows"]}
    assert rows[("qi", 5)]["ic"] > 0.2 and rows[("qi", 5)]["top_minus_bottom_bps"] > 0
    assert abs(rows[("delta_share", 5)]["ic"]) < 0.1, "a reading that is pure noise"


def test_the_forward_return_starts_at_the_close_of_the_readings_own_minute(tmp_path):
    _write(tmp_path, lead=False)
    f = day_frame(tmp_path / "archive", DAY)
    assert f is not None
    bars = pd.read_parquet(tmp_path / "archive" / "bars" / f"{DAY}.parquet")
    c = bars[bars.scrip_code == "1000"].set_index("ts")["c"]
    row = f[(f.code == "1000")].iloc[3]
    assert row["fwd_5"] == c[row["ts"] + 300] / c[row["ts"]] - 1
