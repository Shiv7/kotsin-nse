"""The nightly step for the option system: fetch the bhavcopy of any recent session not yet on disk (NSE publishes it
in the evening; the engine fetches the previous session's at its day start), advance every paper book over the new
sessions, refresh the volatility regime, and print where each book stands. Run by ``scripts/paper-options.sh`` after
18:30 on a session day or any morning; safe to run twice (a day already stepped is skipped)."""

from __future__ import annotations

import asyncio
import datetime

import httpx

from ..config import Settings
from ..market.fo_bhavcopy import OiDailyStore, fetch_day
from . import paper, regime
from .sim import summary


async def fetch_missing(settings: Settings, *, back_days: int = 6) -> list[str]:
    store = OiDailyStore(settings.data_dir / "oi_daily")
    today = datetime.date.today()
    got: list[str] = []
    async with httpx.AsyncClient(timeout=30) as http:
        for i in range(back_days, -1, -1):
            d = today - datetime.timedelta(days=i)
            if d.weekday() >= 5 or store.has(d):
                continue
            if d == today and datetime.datetime.now().hour < 18:
                continue  # today's file appears in the evening
            df = await fetch_day(http, d)
            if df is not None and not df.empty:
                store.write(d, df)
                got.append(d.isoformat())
    return got


def main() -> None:
    settings = Settings(engine_enabled=False)
    got = asyncio.run(fetch_missing(settings))
    print("bhavcopies fetched:", got or "nothing new")
    store, proot, rroot = paper.paths(settings)
    ev = paper.advance(store, proot)
    for name, events in ev.items():
        for e in events:
            print(f"   {name}: {e}")
        st = paper.load_state(proot, paper.BOOKS[name])
        opens = [(o["expiry"], o["credit"], o["path"][-1][1] if o["path"] else 0.0) for o in st.get("opens", [])]
        print(f"== {name}: {summary(st)} open {opens}")
    r = regime.latest(store, rroot)
    if r:
        print("regime:", {k: r.get(k) for k in ("day", "F", "iv_m", "iv_m_pct", "iv_w", "term_w_minus_m", "skew25", "straddle_m_pct", "dte_m", "dte_w")})


if __name__ == "__main__":
    main()
