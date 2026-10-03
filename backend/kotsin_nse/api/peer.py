"""Which engine this is, and a read-only window onto its twin.

Operator, 2026-10-03: two paper engines run side by side — "A = phase34 ... as phase 34, B = phase35 ...
as phase 35 and include their strategies in http://127.0.0.1:8500/alerts". Each engine reads its name and
its twin from ``<data_dir>/engine.json``::

    {"name": "phase 34", "peer": {"name": "phase 35", "url": "http://127.0.0.1:8501"}}

A file in the engine's own data folder rather than a ``KN_*`` key, because the two engines share one
``.env`` and the settings refuse unknown keys — a key added there would stop a rolled-back engine from
booting; the old code simply ignores the file. Read on every request, so a rename needs no restart.

``/api/peer/<path>`` forwards a GET to the twin's ``/api/<path>``: the alerts page shows the twin's books
from the same page. Read-only by construction (GET only, no body), loopback only (the twin is on this
machine), never a peer of a peer (two engines naming each other would otherwise forward forever)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

log = structlog.get_logger(__name__)

FILE = "engine.json"
PEER_TIMEOUT_S = 5.0
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True, slots=True)
class Identity:
    name: str = ""
    peer_name: str = ""
    peer_url: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "peer": {"name": self.peer_name} if self.peer_url else None}


def read_identity(data_dir: Path) -> Identity:
    """The engine's name and its twin; blank when the file is absent or unreadable (one engine)."""
    try:
        raw = json.loads((data_dir / FILE).read_text())
    except (OSError, ValueError):
        return Identity()
    peer = raw.get("peer") or {}
    url = str(peer.get("url") or "").rstrip("/")
    if url:
        parts = urlsplit(url)
        if parts.scheme != "http" or parts.hostname not in _LOOPBACK:
            log.warning("engine.peer_refused", url=url, why="the twin must be http on this machine")
            url = ""
    return Identity(name=str(raw.get("name") or ""), peer_name=str(peer.get("name") or "") if url else "", peer_url=url)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=PEER_TIMEOUT_S)


def register(api: APIRouter, data_dir: Path) -> None:
    @api.get("/engine")
    async def engine_identity() -> dict[str, Any]:
        return read_identity(data_dir).to_json()

    @api.get("/peer/{path:path}")
    async def peer(path: str, request: Request) -> JSONResponse:
        ident = read_identity(data_dir)
        if not ident.peer_url:
            raise HTTPException(404, "this engine has no twin (data/engine.json)")
        if path.split("/", 1)[0] == "peer":
            raise HTTPException(400, "a twin's twin is not forwarded")
        url = f"{ident.peer_url}/api/{path}"
        try:
            async with _client() as client:
                r = await client.get(url, params=list(request.query_params.multi_items()), headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"{ident.peer_name or 'the twin'} is not answering ({exc.__class__.__name__})") from exc
        try:
            body = r.json()
        except ValueError:
            raise HTTPException(502, f"{ident.peer_name or 'the twin'} answered {r.status_code} without JSON") from None
        return JSONResponse(body, status_code=r.status_code)


__all__ = ["FILE", "Identity", "read_identity", "register"]
