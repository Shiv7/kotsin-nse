"""Entry point. One process: engine + API + static UI on one port, plus the history fetch.

``uv run kotsin-nse`` boots the engine, mounts the API and serves ``frontend/dist`` from the same
port, so there is one systemd unit, one log and one thing to restart. The old stack needed five
JVMs, a Kafka, a Mongo, a Redis and a Vite dev server brought up in a specific order — and a
watchdog that relaunched all five inside 30 seconds with heap flags sized for a machine that no
longer existed.

Subcommands:

* ``serve`` (default) — the engine and the UI
* ``fetch-history`` — fill the Parquet cache from the broker's historical endpoint
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
from datetime import date, timedelta
from pathlib import Path

import structlog
import uvicorn

from .api.routes import build_app
from .config import Settings, UnknownConfigKeys, assert_no_unknown_env
from .engine import Engine
from .log import configure_logging

log = structlog.get_logger(__name__)


def load_settings() -> Settings:
    assert_no_unknown_env()
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    (s.data_dir / "logs").mkdir(exist_ok=True)
    return s


# -- serve ----------------------------------------------------------------------------------------


async def serve(settings: Settings) -> None:
    engine = Engine(settings)
    app = build_app(engine)
    await engine.start()

    config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(config)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    pid_file = settings.data_dir / "engine.pid"
    pid_file.write_text(str(os.getpid()))
    log.info("serving", url=f"http://{settings.api_host}:{settings.api_port}", pid=os.getpid())

    serve_task = asyncio.create_task(server.serve())
    await stop.wait()
    log.info("shutdown.begin")
    server.should_exit = True
    with contextlib.suppress(Exception):
        await asyncio.wait_for(serve_task, timeout=10)
    await engine.stop()
    pid_file.unlink(missing_ok=True)
    log.info("shutdown.done")


# -- research ---------------------------------------------------------------------------------------


async def fetch_history(settings: Settings, args: argparse.Namespace) -> None:
    """Fill the Parquet cache. Needs broker credentials; everything downstream of it does not."""
    import httpx

    from .domain import Instrument, InstrumentKind
    from .instrument.catalogue import CatalogueLoader
    from .research.history import HistoryStore, fetch
    from .venue.fivepaisa.auth import Authenticator
    from .venue.fivepaisa.rest import FivePaisaREST

    if not settings.has_credentials:
        print("5paisa credentials required — set KN_FP_* in backend/.env", file=sys.stderr)
        raise SystemExit(2)

    async with httpx.AsyncClient(timeout=60) as http:
        auth = Authenticator(settings, http)
        rest = FivePaisaREST(settings, http, auth)
        catalogue = await CatalogueLoader(settings, rest.scrip_master_csv).ensure()
        if args.all_fo:
            from .config import Segment
            from .instrument.universe import UniverseBuilder, UniversePolicy

            groups = UniverseBuilder(catalogue, UniversePolicy(include_indices=False)).build_underlyings(
                [Segment.NSE_FO], date.today()
            )
            wanted = sorted(r for r, g in groups.items() if g.equity is not None)
        else:
            wanted = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        instruments: list[Instrument] = []
        for sym in wanted:
            inst = catalogue.equity(sym) or catalogue.front_future(sym)
            if inst is None:
                print(f"  ! {sym} not in the scrip master — skipped", file=sys.stderr)
                continue
            instruments.append(inst)
        if not instruments:
            print("nothing to fetch", file=sys.stderr)
            raise SystemExit(1)
        store = HistoryStore(settings.data_dir / "history")
        counts = await fetch(
            rest,
            store,
            instruments,
            tfs=tuple(t.strip() for t in args.tf.split(",") if t.strip()),
            start=date.fromisoformat(args.start),
            end=date.fromisoformat(args.end),
        )
    for key, n in sorted(counts.items()):
        print(f"{key:<24} {n:>7} bars")
    _ = InstrumentKind  # imported for the type union above


def run_playbook(settings: Settings, args: argparse.Namespace) -> None:
    """Sweep / value-area strategies over cached days (research/playbooks.py)."""
    from dataclasses import fields

    from .research.history import HistoryStore
    from .research.obtest import cached_days
    from .research.playbooks import PBParams, run

    days = cached_days(HistoryStore(settings.data_dir / "history"))
    days = [d for d in days if (not args.start or d >= date.fromisoformat(args.start)) and (not args.end or d <= date.fromisoformat(args.end))]
    known = {f.name: f for f in fields(PBParams)}
    over: dict[str, object] = {}
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        if k not in known:
            raise SystemExit(f"unknown parameter {k!r}; known: {', '.join(known)}")
        cur = getattr(PBParams(), k)
        over[k] = v if isinstance(cur, str) else (v.lower() == "true") if isinstance(cur, bool) else type(cur)(v)
    out = run(settings, days, PBParams(**over), label=args.label)  # type: ignore[arg-type]
    sm = out["summary"]
    print(f"{out['id']}  {sm['trades']} trades / {sm['days']} days  win {sm['win_rate']}%  avg {sm['avg_r']} R  total {sm['total_r']} R  "
          f"net ₹{sm['net_inr']:,}  costs ₹{sm['costs_inr']:,}  t(by day) {sm['t_by_day']}  days +{sm['days_positive']}/-{sm['days_negative']}")
    print(f"  setups {sm['by_setup']}\n  exits {sm['by_exit']}")


async def fetch_oi(settings: Settings, args: argparse.Namespace) -> None:
    """NSE's F&O bhavcopy for every weekday in range: the official closing OI of every contract."""
    import httpx

    from .market.fo_bhavcopy import OiDailyStore, backfill

    store = OiDailyStore(settings.data_dir / "oi_daily")
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
        out = await backfill(http, store, date.fromisoformat(args.start), date.fromisoformat(args.end))
    print(f"bhavcopy: {out}  ({len(store.days())} sessions held)")


def run_mtf(settings: Settings, args: argparse.Namespace) -> None:
    """Higher-timeframe sweeps timed on a lower timeframe (research/mtf.py), one stock at a time."""
    from dataclasses import fields

    from .research.mtf import MTFParams, run

    known = {f.name for f in fields(MTFParams)}
    over: dict[str, object] = {}
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        if k not in known:
            raise SystemExit(f"unknown parameter {k!r}; known: {', '.join(sorted(known))}")
        cur = getattr(MTFParams(), k)
        over[k] = v if isinstance(cur, str) else (v.lower() == "true") if isinstance(cur, bool) else type(cur)(v)
    syms = [x.strip().upper() for x in args.symbols.split(",") if x.strip()] or None

    def on_symbol(sym: str, got: list, n: int, of: int) -> None:
        if n % 25 == 0 or n == of:
            print(f"  {n}/{of} stocks", flush=True)

    out = run(settings, date.fromisoformat(args.start), date.fromisoformat(args.end), MTFParams(**over),  # type: ignore[arg-type]
              symbols=syms, label=args.label, on_symbol=on_symbol)
    sm = out["summary"]
    print(f"{out['id']}  {sm['trades']} trades ({out['candidates']} candidates)  win {sm['win_rate']}%  avg {sm['avg_r']} R  "
          f"total {sm['total_r']} R  net ₹{sm['net_inr']:,}  costs {sm['costs_r_per_trade']} R/trade  t(by week) {sm['t_by_week']}  "
          f"months +{sm['months_positive']}/-{sm['months_negative']}  held {sm['avg_sessions_held']} sessions")
    print(f"  setups {sm['by_setup']}\n  exits {sm['by_exit']}\n  sides {sm['by_side']}")


def run_fib(settings: Settings, args: argparse.Namespace) -> None:
    """Trend pullbacks at Fibonacci + pivot + order-block confluence (research/fibob.py)."""
    from dataclasses import fields

    from .research.fibob import FibParams, run

    known = {f.name for f in fields(FibParams)}
    over: dict[str, object] = {}
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        if k not in known:
            raise SystemExit(f"unknown parameter {k!r}; known: {', '.join(sorted(known))}")
        cur = getattr(FibParams(), k)
        over[k] = v if isinstance(cur, str) else (v.lower() == "true") if isinstance(cur, bool) else type(cur)(v)
    syms = [x.strip().upper() for x in args.symbols.split(",") if x.strip()] or None
    out = run(settings, date.fromisoformat(args.start), date.fromisoformat(args.end), FibParams(**over),  # type: ignore[arg-type]
              symbols=syms, label=args.label)
    sm = out["summary"]
    print(f"{out['id']}  {sm['trades']} trades ({out['candidates']} candidates)  win {sm['win_rate']}%  avg {sm['avg_r']} R  "
          f"total {sm['total_r']} R  net ₹{sm['net_inr']:,}  costs {sm['costs_r_per_trade']} R/trade  t(by week) {sm['t_by_week']}  "
          f"months +{sm['months_positive']}/-{sm['months_negative']}  held {sm['avg_sessions_held']} sessions")
    print(f"  funnel {out['funnel']}")
    print(f"  confluence {sm['by_confluence']}\n  exits {sm['by_exit']}\n  sides {sm['by_side']}\n  OI {sm['by_oi_align']}")


def run_swing(settings: Settings, args: argparse.Namespace) -> None:
    """Higher-timeframe reversals with a structure trail (research/swing.py)."""
    from dataclasses import fields

    from .research.swing import SwingParams, run

    known = {f.name for f in fields(SwingParams)}
    over: dict[str, object] = {}
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        if k not in known:
            raise SystemExit(f"unknown parameter {k!r}; known: {', '.join(sorted(known))}")
        cur = getattr(SwingParams(), k)
        over[k] = v if isinstance(cur, str) else (v.lower() == "true") if isinstance(cur, bool) else type(cur)(v)
    syms = [x.strip().upper() for x in args.symbols.split(",") if x.strip()] or None
    out = run(settings, date.fromisoformat(args.start), date.fromisoformat(args.end), SwingParams(**over),  # type: ignore[arg-type]
              symbols=syms, label=args.label)
    sm = out["summary"]
    print(f"{out['id']}  {sm['trades']} trades ({out['candidates']} candidates)  win {sm['win_rate']}%  avg {sm['avg_r']} R  "
          f"median {sm.get('median_r')} R  5R+ {sm.get('five_r_plus')}  best {sm.get('best_r')} R  total {sm['total_r']} R  "
          f"net ₹{sm['net_inr']:,}  t(by week) {sm['t_by_week']}  months +{sm['months_positive']}/-{sm['months_negative']}  "
          f"held {sm['avg_sessions_held']} sessions")
    print(f"  funnel {out['funnel']}\n  setups {sm['by_setup']}\n  exits {sm['by_exit']}\n  sides {sm['by_side']}")


def run_obtest(settings: Settings, args: argparse.Namespace) -> None:
    """The order-block playbook over cached days, one day at a time (light on the CPU)."""
    from dataclasses import fields

    from .research.history import HistoryStore
    from .research.obtest import Params, cached_days, run

    days = cached_days(HistoryStore(settings.data_dir / "history"))
    if args.start:
        days = [d for d in days if d >= date.fromisoformat(args.start)]
    if args.end:
        days = [d for d in days if d <= date.fromisoformat(args.end)]
    if not days:
        print("no cached 1m days in range — run fetch-history --all-fo --tf 1m,1d", file=sys.stderr)
        raise SystemExit(1)
    overrides: dict[str, object] = {}
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        f = {f.name: f for f in fields(Params)}.get(k)
        if f is None:
            raise SystemExit(f"unknown parameter {k!r}; known: {', '.join(x.name for x in fields(Params))}")
        overrides[k] = v if f.type == "str" else (v.lower() == "true") if f.type == "bool" else type(getattr(Params(), k))(v)
    p = Params(**overrides)  # type: ignore[arg-type]

    def on_day(d: date, got: list, i: int, n: int) -> None:
        r = sum(t.r for t in got)
        print(f"  {d}  {len(got):>2} trades  {r:+6.2f} R   ({i}/{n})", flush=True)

    out = run(settings, days, p, label=args.label, on_day=on_day)
    sm = out["summary"]
    print(f"\n{out['id']}  {sm['trades']} trades over {sm['days']} days  win {sm['win_rate']}%  avg {sm['avg_r']} R  "
          f"total {sm['total_r']} R  net ₹{sm['net_inr']:,.0f} (costs ₹{sm['costs_inr']:,.0f})  t(by day) {sm['t_by_day']}")
    print(f"exits: {sm['by_exit']}\nsides: {sm['by_side']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kotsin-nse", description="NSE/MCX trading engine on 5paisa")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("serve", help="run the engine, the API and the UI (default)")

    today = date.today()
    f = sub.add_parser("fetch-history", help="fill the Parquet history cache from the broker")
    f.add_argument("--symbols", default="", help="comma-separated underlying symbols")
    f.add_argument("--all-fo", dest="all_fo", action="store_true", help="every NSE stock with F&O (no indices)")
    f.add_argument("--tf", default="30m,1d", help="comma-separated: 1m 3m 5m 10m 15m 30m 60m 1d")
    f.add_argument("--start", default=(today - timedelta(days=365)).isoformat())
    f.add_argument("--end", default=today.isoformat())

    o = sub.add_parser("ob-backtest", help="the order-block playbook over cached 1m days")
    o.add_argument("--start", default="")
    o.add_argument("--end", default="")
    o.add_argument("--label", default="")
    o.add_argument("--set", action="append", help="a parameter, e.g. --set min_rr=2 --set side_rule=both")
    pb = sub.add_parser("pb-backtest", help="sweep / value-area strategies over cached 1m days")
    pb.add_argument("--start", default="")
    pb.add_argument("--end", default="")
    pb.add_argument("--label", default="")
    pb.add_argument("--set", action="append", help="e.g. --set strategy=VA_REENTRY --set min_stop_adr=0.2")
    fb = sub.add_parser("fib-backtest", help="trend pullbacks at Fibonacci + pivot + order-block confluence (1h → 15m)")
    fb.add_argument("--start", required=True)
    fb.add_argument("--end", required=True)
    fb.add_argument("--symbols", default="")
    fb.add_argument("--label", default="")
    fb.add_argument("--set", action="append", help="e.g. --set zone=ote --set cpr_narrow=true --set oi_filter=WITH")

    sw = sub.add_parser("swing-backtest", help="higher-timeframe reversals with a structure trail")
    sw.add_argument("--start", required=True)
    sw.add_argument("--end", required=True)
    sw.add_argument("--symbols", default="")
    sw.add_argument("--label", default="")
    sw.add_argument("--set", action="append", help="e.g. --set entry=1h --set use_bias=false --set sides=long")

    oi = sub.add_parser("fetch-oi", help="NSE's daily F&O bhavcopy (official OI of every contract)")
    oi.add_argument("--start", required=True)
    oi.add_argument("--end", default=today.isoformat())

    m = sub.add_parser("mtf-backtest", help="1h/4h sweeps timed on 15m/5m, over the 5m cache")
    m.add_argument("--start", required=True)
    m.add_argument("--end", required=True)
    m.add_argument("--symbols", default="")
    m.add_argument("--label", default="")
    m.add_argument("--set", action="append", help="e.g. --set htf=240m --set ltf=5m --set daily_filter=true")
    return p


def cli() -> None:
    args = build_parser().parse_args()
    try:
        settings = load_settings()
    except UnknownConfigKeys as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    configure_logging(settings.log_level, json_output=settings.env == "prod")
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)

    command = args.command or "serve"
    if command == "serve":
        asyncio.run(serve(settings))
    elif command == "fetch-history":
        asyncio.run(fetch_history(settings, args))
    elif command == "ob-backtest":
        run_obtest(settings, args)
    elif command == "pb-backtest":
        run_playbook(settings, args)
    elif command == "swing-backtest":
        run_swing(settings, args)
    elif command == "fib-backtest":
        run_fib(settings, args)
    elif command == "fetch-oi":
        asyncio.run(fetch_oi(settings, args))
    elif command == "mtf-backtest":
        run_mtf(settings, args)
    else:  # pragma: no cover - argparse rejects anything else
        raise SystemExit(f"unknown command {command}")


if __name__ == "__main__":
    cli()
