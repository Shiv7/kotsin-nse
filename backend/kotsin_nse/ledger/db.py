"""SQLite ledger (WAL) over SQLAlchemy async. One writer — the engine.

Columns exist for what we query on; the full record is kept as JSON alongside, so a field added to
a strategy's evidence does not need a migration and nothing is ever silently dropped.

Two tables earn their place beyond the obvious:

* ``rejections`` — every candidate that did **not** become a signal, with the gate that killed it.
  "What did the filter reject, and would it have won?" was unanswerable for most of the old stack;
  the one book that kept a pass-and-fail audit is the one whose config bug was provable in a single
  query.
* ``control`` — the mode row. Mode is state, not an environment variable, and ``LIVE*`` carries an
  ``armed_until``: a restart after expiry boots into PAPER rather than silently staying live.
"""

from __future__ import annotations

import json
import time
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

meta = sa.MetaData()

control = sa.Table(
    "control",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("mode", sa.String, nullable=False),
    sa.Column("armed_until", sa.Float),
    sa.Column("halted", sa.Boolean, nullable=False, default=False),
    sa.Column("halt_reason", sa.String, default=""),
    sa.Column("updated_ts", sa.Float, nullable=False),
)
wallets = sa.Table(
    "wallets",
    meta,
    sa.Column("strategy", sa.String, primary_key=True),
    sa.Column("json", sa.Text, nullable=False),
    sa.Column("updated_ts", sa.Float, nullable=False),
)
signals = sa.Table(
    "signals",
    meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("signal_id", sa.String, unique=True, nullable=False),
    sa.Column("strategy", sa.String, nullable=False),
    sa.Column("symbol", sa.String, nullable=False),
    sa.Column("direction", sa.String, nullable=False),
    sa.Column("ts", sa.Integer, nullable=False),
    sa.Column("grade", sa.String, default=""),
    sa.Column("decision", sa.String, nullable=False),
    sa.Column("decision_reason", sa.String, default=""),
    sa.Column("json", sa.Text, nullable=False),
    sa.Column("created_ts", sa.Float, nullable=False),
    sa.Index("ix_signals_ts", "ts"),
)
rejections = sa.Table(
    "rejections",
    meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("strategy", sa.String, nullable=False),
    sa.Column("symbol", sa.String, nullable=False),
    sa.Column("ts", sa.Integer, nullable=False),
    sa.Column("binding_gate", sa.String, nullable=False),
    sa.Column("json", sa.Text, nullable=False),
    sa.Column("created_ts", sa.Float, nullable=False),
    sa.Index("ix_rejections_gate", "strategy", "binding_gate"),
)
orders = sa.Table(
    "orders",
    meta,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("client_order_id", sa.String, unique=True, nullable=False),
    sa.Column("strategy", sa.String, nullable=False),
    sa.Column("symbol", sa.String, nullable=False),
    sa.Column("scrip_code", sa.String, nullable=False),
    sa.Column("purpose", sa.String, nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("mode", sa.String, nullable=False),
    sa.Column("decision", sa.String, nullable=False),
    sa.Column("ts", sa.Float, nullable=False),
    sa.Column("json", sa.Text, nullable=False),
)
positions = sa.Table(
    "positions",
    meta,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("strategy", sa.String, nullable=False),
    sa.Column("symbol", sa.String, nullable=False),
    sa.Column("scrip_code", sa.String, nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("opened_ts", sa.Float, nullable=False),
    sa.Column("closed_ts", sa.Float),
    sa.Column("json", sa.Text, nullable=False),
)
trades = sa.Table(
    "trades",
    meta,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("position_id", sa.String, nullable=False),
    sa.Column("strategy", sa.String, nullable=False),
    sa.Column("symbol", sa.String, nullable=False),
    sa.Column("underlying", sa.String, nullable=False),
    sa.Column("closed_ts", sa.Float, nullable=False),
    sa.Column("net", sa.Float, nullable=False),
    sa.Column("charges", sa.Float, nullable=False),
    sa.Column("r_multiple", sa.Float, nullable=False),
    sa.Column("exit_reason", sa.String, nullable=False),
    sa.Column("json", sa.Text, nullable=False),
    sa.Index("ix_trades_closed", "closed_ts"),
)
wallet_snapshots = sa.Table(
    "wallet_snapshots",
    meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.Float, nullable=False),
    sa.Column("strategy", sa.String, nullable=False),
    sa.Column("json", sa.Text, nullable=False),
)
events = sa.Table(
    "events",
    meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.Float, nullable=False),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("json", sa.Text, nullable=False),
    sa.Index("ix_events_kind", "kind"),
)
health = sa.Table(
    "health",
    meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.Float, nullable=False),
    sa.Column("json", sa.Text, nullable=False),
)


def _j(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))


class Ledger:
    def __init__(self, url: str) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url, future=True)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            if self.url.startswith("sqlite"):
                await conn.execute(sa.text("PRAGMA journal_mode=WAL"))
                await conn.execute(sa.text("PRAGMA synchronous=NORMAL"))
            await conn.run_sync(meta.create_all)
            row = (await conn.execute(sa.select(control).where(control.c.id == 1))).first()
            if row is None:
                await conn.execute(
                    control.insert().values(
                        id=1, mode="SHADOW", halted=False, halt_reason="", updated_ts=time.time()
                    )
                )

    async def close(self) -> None:
        await self.engine.dispose()

    # -- control ---------------------------------------------------------------------------------

    async def get_control(self) -> dict[str, Any]:
        async with self.engine.begin() as conn:
            row = (await conn.execute(sa.select(control).where(control.c.id == 1))).mappings().first()
        return dict(row) if row else {}

    async def set_mode(self, mode: str, armed_until: float | None = None) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                control.update()
                .where(control.c.id == 1)
                .values(mode=mode, armed_until=armed_until, updated_ts=time.time())
            )

    async def set_halt(self, halted: bool, reason: str = "") -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                control.update()
                .where(control.c.id == 1)
                .values(halted=halted, halt_reason=reason, updated_ts=time.time())
            )

    # -- wallets ----------------------------------------------------------------------------------

    async def upsert_wallet(self, strategy: str, data: dict[str, Any]) -> None:
        now = time.time()
        async with self.engine.begin() as conn:
            existing = (
                await conn.execute(sa.select(wallets.c.strategy).where(wallets.c.strategy == strategy))
            ).first()
            if existing:
                await conn.execute(
                    wallets.update()
                    .where(wallets.c.strategy == strategy)
                    .values(json=_j(data), updated_ts=now)
                )
            else:
                await conn.execute(
                    wallets.insert().values(strategy=strategy, json=_j(data), updated_ts=now)
                )

    async def load_wallets(self) -> dict[str, dict[str, Any]]:
        async with self.engine.begin() as conn:
            rows = (await conn.execute(sa.select(wallets))).mappings().all()
        return {r["strategy"]: json.loads(r["json"]) for r in rows}

    async def snapshot_wallet(self, strategy: str, data: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                wallet_snapshots.insert().values(ts=time.time(), strategy=strategy, json=_j(data))
            )

    # -- signals ------------------------------------------------------------------------------------

    async def insert_signal(self, sig: dict[str, Any], decision: str, reason: str = "") -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                sqlite_insert(signals)
                .values(
                    signal_id=sig["signal_id"],
                    strategy=sig["strategy"],
                    symbol=sig["symbol"],
                    direction=sig["direction"],
                    ts=sig["ts"],
                    grade=sig.get("grade", ""),
                    decision=decision,
                    decision_reason=reason,
                    json=_j({**sig, "decision": decision, "decision_reason": reason}),
                    created_ts=time.time(),
                )
                .on_conflict_do_nothing(index_elements=[signals.c.signal_id])
            )

    async def insert_rejection(self, rej: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                rejections.insert().values(
                    strategy=rej["strategy"],
                    symbol=rej["symbol"],
                    ts=rej["ts"],
                    binding_gate=rej["binding_gate"],
                    json=_j(rej),
                    created_ts=time.time(),
                )
            )

    # -- orders / positions / trades -------------------------------------------------------------------

    async def insert_order(self, order: dict[str, Any], decision: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                sqlite_insert(orders)
                .values(
                    id=order["id"],
                    client_order_id=order["client_order_id"],
                    strategy=order["strategy"],
                    symbol=order["symbol"],
                    scrip_code=order["scrip_code"],
                    purpose=order["purpose"],
                    status=order["status"],
                    mode=order["mode"],
                    decision=decision,
                    ts=order["ts"],
                    json=_j(order),
                )
                .on_conflict_do_nothing(index_elements=[orders.c.client_order_id])
            )

    async def upsert_position(self, pos: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            existing = (
                await conn.execute(sa.select(positions.c.id).where(positions.c.id == pos["id"]))
            ).first()
            values = {
                "strategy": pos["strategy"],
                "symbol": pos["symbol"],
                "scrip_code": pos["scrip_code"],
                "status": pos["status"],
                "opened_ts": pos["opened_ts"],
                "closed_ts": pos.get("closed_ts"),
                "json": _j(pos),
            }
            if existing:
                await conn.execute(positions.update().where(positions.c.id == pos["id"]).values(**values))
            else:
                await conn.execute(positions.insert().values(id=pos["id"], **values))

    async def load_open_positions(self) -> list[dict[str, Any]]:
        async with self.engine.begin() as conn:
            rows = (
                await conn.execute(sa.select(positions.c.json).where(positions.c.status == "OPEN"))
            ).all()
        return [json.loads(r[0]) for r in rows]

    async def insert_trade(self, trade: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                sqlite_insert(trades)
                .values(
                    id=trade["id"],
                    position_id=trade["position_id"],
                    strategy=trade["strategy"],
                    symbol=trade["symbol"],
                    underlying=trade["underlying"],
                    closed_ts=trade["closed_ts"],
                    net=trade["net"],
                    charges=trade["charges"],
                    r_multiple=trade["r_multiple"],
                    exit_reason=trade["exit_reason"],
                    json=_j(trade),
                )
                .on_conflict_do_nothing(index_elements=[trades.c.id])
            )

    async def known_client_order_ids(self) -> set[str]:
        async with self.engine.begin() as conn:
            rows = (await conn.execute(sa.select(orders.c.client_order_id))).all()
        return {r[0] for r in rows}

    # -- events / health ----------------------------------------------------------------------------

    async def event(self, kind: str, data: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(events.insert().values(ts=time.time(), kind=kind, json=_j(data)))

    async def insert_health(self, data: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(health.insert().values(ts=time.time(), json=_j(data)))

    # -- reads ----------------------------------------------------------------------------------------

    async def recent(
        self, table: sa.Table, limit: int = 100, *, order_col: str = "id", where: Any = None
    ) -> list[dict[str, Any]]:
        stmt = sa.select(table.c.json).order_by(sa.desc(table.c[order_col])).limit(limit)
        if where is not None:
            stmt = stmt.where(where)
        async with self.engine.begin() as conn:
            rows = (await conn.execute(stmt)).all()
        return [json.loads(r[0]) for r in rows]

    async def rows_between(self, name: str, start: float, end: float) -> list[dict[str, Any]]:
        """Every row of one table whose timestamp falls in ``[start, end)``, oldest first, as its
        JSON with the columns the JSON does not carry merged in (a signal's decision, an event's
        kind and time). The trigger-card page reads a whole session this way."""
        table, col, extra = {
            "signals": (signals, "ts", ("decision", "decision_reason")),
            "positions": (positions, "opened_ts", ("status", "closed_ts")),
            "trades": (trades, "closed_ts", ()),
            "orders": (orders, "ts", ("purpose", "status")),
            "events": (events, "ts", ("kind", "ts")),
        }[name]
        cols = [table.c.json, *(table.c[c] for c in extra)]
        stmt = sa.select(*cols).where(table.c[col] >= start, table.c[col] < end).order_by(table.c[col], table.c.id)
        async with self.engine.begin() as conn:
            rows = (await conn.execute(stmt)).all()
        out = []
        for r in rows:
            d = json.loads(r[0])
            for i, c in enumerate(extra, start=1):
                d.setdefault(c, r[i])
            out.append(d)
        return out

    async def gate_histogram(self, strategy: str | None = None) -> list[dict[str, Any]]:
        """Which gate is binding, as a number. The question ``NSE_BB_30`` could never answer."""
        stmt = sa.select(
            rejections.c.strategy,
            rejections.c.binding_gate,
            sa.func.count().label("n"),
        ).group_by(rejections.c.strategy, rejections.c.binding_gate)
        if strategy:
            stmt = stmt.where(rejections.c.strategy == strategy)
        async with self.engine.begin() as conn:
            rows = (await conn.execute(stmt.order_by(sa.desc("n")))).mappings().all()
        return [dict(r) for r in rows]

    async def pnl_by_strategy(self) -> dict[str, dict[str, Any]]:
        """Trades, net, and the last close per book — the 'when did it last fire and what did it
        make' a reviewer asks first."""
        stmt = sa.select(
            trades.c.strategy,
            sa.func.count().label("n"),
            sa.func.sum(trades.c.net).label("net"),
            sa.func.sum(trades.c.charges).label("charges"),
            sa.func.max(trades.c.closed_ts).label("last_closed_ts"),
        ).group_by(trades.c.strategy)
        async with self.engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return {r["strategy"]: dict(r) for r in rows}

    async def last_signal(self, strategy: str) -> dict[str, Any] | None:
        rows = await self.recent(signals, 1, order_col="ts", where=signals.c.strategy == strategy)
        return rows[0] if rows else None

    async def signal(self, signal_id: str) -> dict[str, Any] | None:
        rows = await self.recent(signals, 1, where=signals.c.signal_id == signal_id)
        return rows[0] if rows else None

    async def trade_for_signal(self, signal_id: str) -> dict[str, Any] | None:
        """The closed trade a signal produced, if any. Columns exist for what is queried in bulk;
        the signal id lives in the JSON, and a personal ledger is small enough to scan."""
        for row in await self.recent(trades, 2000):
            if row.get("signal_id") == signal_id:
                return row
        return None

    async def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        async with self.engine.begin() as conn:
            for t in (signals, rejections, orders, positions, trades, events):
                out[t.name] = int(
                    (await conn.execute(sa.select(sa.func.count()).select_from(t))).scalar() or 0
                )
        return out
