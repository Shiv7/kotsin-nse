"""SQLite ledger (WAL) over SQLAlchemy async. One writer — the engine.

Columns exist for what we query on; the full record is kept as JSON alongside, so a new field does
not need a migration and nothing is ever silently dropped.

One table earns its place beyond the obvious:

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
#: the owner's call journal — a discretionary call logged at the moment it is seen, scored by the
#: engine from the bars that follow (``journal.py``). Columns for what the page filters on; the record
#: itself is the JSON.
journal = sa.Table(
    "journal",
    meta,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("ts", sa.Float, nullable=False),
    sa.Column("day", sa.String, nullable=False),
    sa.Column("symbol", sa.String, nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("json", sa.Text, nullable=False),
    sa.Index("ix_journal_day", "day"),
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
            now = time.time()
            await conn.execute(
                events.insert().values(ts=now, kind=kind, json=_j({"kind": kind, "ts": now, **data}))
            )

    async def insert_health(self, data: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(health.insert().values(ts=time.time(), json=_j(data)))

    # -- the call journal ----------------------------------------------------------------------------

    async def upsert_journal(self, row: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            values = {"ts": row["ts"], "day": row["day"], "symbol": row["symbol"], "status": row["status"], "json": _j(row)}
            existing = (await conn.execute(sa.select(journal.c.id).where(journal.c.id == row["id"]))).first()
            if existing:
                await conn.execute(journal.update().where(journal.c.id == row["id"]).values(**values))
            else:
                await conn.execute(journal.insert().values(id=row["id"], **values))

    async def journal_rows(self, limit: int = 500, *, day: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        stmt = sa.select(journal.c.json).order_by(sa.desc(journal.c.ts)).limit(limit)
        if day:
            stmt = stmt.where(journal.c.day == day)
        if status:
            stmt = stmt.where(journal.c.status == status)
        async with self.engine.begin() as conn:
            rows = (await conn.execute(stmt)).all()
        return [json.loads(r[0]) for r in rows]

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

    async def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        async with self.engine.begin() as conn:
            for t in (orders, positions, trades, events, journal):
                out[t.name] = int(
                    (await conn.execute(sa.select(sa.func.count()).select_from(t))).scalar() or 0
                )
        return out
