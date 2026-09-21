#!/usr/bin/env python
"""DEV ONLY — drive a complete paper session with synthetic market data, then serve the UI.

This exists so the whole flow can be exercised without a broker session: signal → instrument
selection → sizing → gateway → paper fill → position → exit → trade → ledger → API → UI.

It is **not** a simulator of the market. It feeds hand-built bars through the real engine so every
stage downstream of the feed runs its production code path. What it does NOT exercise is the only
thing it cannot: the 5paisa socket, the scrip master and real order placement. Those need
credentials, and nothing here should be read as evidence that they work.

    uv run python ../scripts/dev-paper-session.py          # seed and serve on :8501
    uv run python ../scripts/dev-paper-session.py --seed-only

Run it from `backend/`.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from kotsin_nse.api.routes import build_app  # noqa: E402
from kotsin_nse.bars.unified import BarSource, UnifiedBar  # noqa: E402
from kotsin_nse.config import Segment, Settings  # noqa: E402
from kotsin_nse.domain import Instrument, InstrumentKind, OptionType  # noqa: E402
from kotsin_nse.engine import Engine  # noqa: E402
from kotsin_nse.exec.gateway import Mode  # noqa: E402
from kotsin_nse.exec.paper import BookSnapshot  # noqa: E402
from kotsin_nse.instrument.select import Quote  # noqa: E402
from kotsin_nse.log import configure_logging  # noqa: E402
from kotsin_nse.market.session import IST, from_ist  # noqa: E402

BOOK = [
    ("RELIANCE", Segment.NSE_EQ, 1480.0, 250),
    ("TCS", Segment.NSE_EQ, 3120.0, 175),
    ("INFY", Segment.NSE_EQ, 1890.0, 400),
    ("CRUDEOIL", Segment.MCX_FO, 5820.0, 100),
]
GRID = [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in (15, 45)][:13]


def ts_at(day: date, hm: str) -> int:
    from datetime import datetime

    return int(from_ist(datetime.fromisoformat(f"{day.isoformat()} {hm}:00").replace(tzinfo=IST)))


def trading_days(n: int, end: date) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def build_series(symbol: str, base: float, days: list[date], rng: random.Random):
    """A drifting series with a decisive volume-backed expansion every few sessions — the shape
    FUDKII is built to find. Not a market model; a fixture that reaches the gates."""
    intraday: list[UnifiedBar] = []
    dailies: list[UnifiedBar] = []
    price = base
    for di, day in enumerate(days):
        day_open = price
        fire = di >= 22 and di % 4 == 2
        closes, vols = [], []
        for bi in range(len(GRID)):
            if fire and bi == 8:
                price *= 1.035          # the expansion bar
                vols.append(9000.0)
            elif fire and bi == 9:
                price *= 1.012
                vols.append(4200.0)
            else:
                price *= 1 + rng.gauss(0, 0.0016) - 0.0002
                vols.append(rng.uniform(800, 1250))
            closes.append(price)
        prev = day_open
        for bi, (c, v) in enumerate(zip(closes, vols, strict=True)):
            hi, lo = max(prev, c) * 1.0015, min(prev, c) * 0.9985
            intraday.append(
                UnifiedBar(
                    symbol=symbol, scrip_code=symbol, tf="30m", ts=ts_at(day, GRID[bi]),
                    open=prev, high=hi, low=lo, close=c, volume=v,
                    source=BarSource.REST, complete=True,
                    oi=1_400_000 + di * 900, oi_change_pct=180.0 if fire else 40.0,
                )
            )
            prev = c
        dailies.append(
            UnifiedBar(
                symbol=symbol, scrip_code=symbol, tf="1d", ts=ts_at(day, "09:15"),
                open=day_open, high=max(closes) * 1.004, low=min(closes) * 0.996,
                close=closes[-1], volume=sum(vols) * 90,
                source=BarSource.REST, complete=True,
            )
        )
    return intraday, dailies


async def seed(engine: Engine) -> dict[str, int]:
    rng = random.Random(11)
    days = trading_days(40, date.today())
    await engine.set_mode(Mode.PAPER)

    series: dict[str, list[UnifiedBar]] = {}
    for symbol, segment, base, lot in BOOK:
        kind = InstrumentKind.FUTURE if segment is Segment.MCX_FO else InstrumentKind.EQUITY
        engine.underlyings[symbol] = Instrument(
            scrip_code=symbol, symbol=symbol, segment=segment, kind=kind,
            lot_size=1 if kind is InstrumentKind.EQUITY else lot,
            multiplier=1, underlying=symbol,
        )
        intraday, dailies = build_series(symbol, base, days, rng)
        engine.store.seed(symbol, "1d", dailies)
        series[symbol] = intraday
        engine.ltps[symbol] = intraday[-1].close

    # The chain would come from the scrip master; without credentials there is none, so selection
    # is stubbed with one synthetic contract per underlying. Everything AFTER this point is the
    # real path: sizing, exposure, the gateway, the paper matcher, the ledger.
    contracts: dict[str, Instrument] = {}

    async def fake_select(underlying: Instrument, sig):  # noqa: ANN001
        spot = sig.entry
        lot = 250 if underlying.segment is not Segment.MCX_FO else 100
        strike = round(spot * (1.02 if sig.direction.value == "BULLISH" else 0.98), -1)
        code = f"{underlying.symbol}-{int(strike)}{sig.direction.option_type.value}"
        inst = contracts.setdefault(code, Instrument(
            scrip_code=code, symbol=underlying.symbol, segment=Segment.NSE_FO,
            kind=InstrumentKind.OPTION,
            name=f"{underlying.symbol} 25 SEP 2026 {sig.direction.option_type.value} {strike:.0f}",
            lot_size=lot, multiplier=1, expiry="2026-09-25", strike=strike,
            option_type=sig.direction.option_type, underlying=underlying.symbol,
        ))
        premium = round(max(6.0, spot * 0.019), 2)
        now = time.time()
        engine.quotes[code] = Quote(ltp=premium, bid=premium - 0.35, ask=premium + 0.35, ts=now)
        engine.ltps[code] = premium
        engine.books[code] = BookSnapshot(
            code,
            bids=[(premium - 0.35, lot * 40), (premium - 0.7, lot * 80)],
            asks=[(premium + 0.35, lot * 40), (premium + 0.7, lot * 80)],
            ts=now,
        )

        class Sel:
            ok, instrument, premium_, reason, anchor, spread_pct = True, inst, premium, "ok", 0.0, 1.7

        Sel.premium = premium
        return Sel

    engine._select_instrument = fake_select  # type: ignore[method-assign]

    # Walk the series forward one bar at a time. Seeding only up to `i` is what keeps this honest:
    # ctx.bars() must never be able to see a bar that has not happened.
    decided = 0
    for symbol, bars in series.items():
        for i in range(25, len(bars)):
            engine.store.seed(symbol, "30m", bars[: i + 1])
            await engine._decide(bars[i])
            decided += 1
            if engine.positions:
                await drift_open_positions(engine)
    await drift_open_positions(engine, force_close=True)

    counts = await engine.ledger.counts()
    counts["bars_decided"] = decided
    return counts


async def drift_open_positions(engine: Engine, *, force_close: bool = False) -> None:
    """Move the option premium so real exits fire: some reach T1, some stop out."""
    for pos in list(engine.positions.values()):
        if pos.status != "OPEN":
            continue
        code = pos.instrument.scrip_code
        ltp = engine.ltps.get(code, pos.entry)
        seed_val = sum(ord(c) for c in pos.id) % 3
        step = {0: 1.12, 1: 0.93, 2: 1.04}[seed_val]
        ltp = round(max(0.5, ltp * step), 2) if not force_close else round(pos.option_sl * 0.98, 2)
        engine.ltps[code] = ltp
        now = time.time()
        engine.quotes[code] = Quote(ltp=ltp, bid=ltp - 0.3, ask=ltp + 0.3, ts=now)
        engine.books[code] = BookSnapshot(
            code, bids=[(ltp - 0.3, 100000)], asks=[(ltp + 0.3, 100000)], ts=now
        )
    await engine._manage_positions()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8501)
    ap.add_argument("--data-dir", default="./data-dev")
    ap.add_argument("--seed-only", action="store_true")
    args = ap.parse_args()

    data = Path(args.data_dir).resolve()
    if data.exists():
        for f in data.glob("*.db*"):
            f.unlink()
    data.mkdir(parents=True, exist_ok=True)
    # The calendar is read from the data dir, so a dev dir without it warns "no holiday list" and
    # treats every weekday as a trading day — true, but misleading here. Use the real one.
    real_holidays = Path(__file__).resolve().parents[1] / "backend" / "data" / "holidays.txt"
    if real_holidays.exists():
        (data / "holidays.txt").write_text(real_holidays.read_text())

    settings = Settings(
        _env_file=None,
        data_dir=data,
        db_url=f"sqlite+aiosqlite:///{data}/dev.db",
        api_port=args.port,
        engine_enabled=False,
        feed_enabled=False,
    )
    configure_logging("INFO")
    engine = Engine(settings)
    await engine.start()

    counts = await seed(engine)
    print("\n  ── seeded ──────────────────────────────")
    for k, v in counts.items():
        print(f"  {k:<16} {v}")
    wallets = {w.strategy: round(w.balance) for w in engine.wallets.values()}
    print(f"  wallets          {wallets}")
    print(f"  open positions   {len([p for p in engine.positions.values() if p.status == 'OPEN'])}")
    print("  ────────────────────────────────────────\n")
    if args.seed_only:
        await engine.stop()
        return

    print(f"  UI + API  →  http://127.0.0.1:{args.port}\n")
    config = uvicorn.Config(build_app(engine), host="127.0.0.1", port=args.port,
                            log_level="warning", access_log=False)
    try:
        await uvicorn.Server(config).serve()
    finally:
        await engine.stop()
    _ = math


if __name__ == "__main__":
    asyncio.run(main())
