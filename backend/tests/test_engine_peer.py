"""Two engines side by side (operator, 2026-10-03): "A = phase34 ... as phase 34, B = phase35 ... as phase 35
and include their strategies in http://127.0.0.1:8500/alerts". Each engine names itself from
data/engine.json and reads its twin's pages through /api/peer — GET only, loopback only, one hop."""

from __future__ import annotations

import json

import httpx
import pytest

from kotsin_nse.api import peer
from kotsin_nse.api.routes import build_app
from kotsin_nse.config import Settings
from kotsin_nse.engine import Engine


def _settings(tmp_path, name: str) -> Settings:
    d = tmp_path / name
    d.mkdir()
    return Settings(_env_file=None, data_dir=d, db_url=f"sqlite+aiosqlite:///{d}/t.db", engine_enabled=False)


async def _app(tmp_path, name: str, ident: dict | None):
    s = _settings(tmp_path, name)
    if ident is not None:
        (s.data_dir / peer.FILE).write_text(json.dumps(ident))
    e = Engine(s)
    await e.ledger.init()
    return build_app(e)


@pytest.mark.asyncio
async def test_each_engine_names_itself_and_reads_its_twins_books(tmp_path, monkeypatch):
    a = await _app(tmp_path, "a", {"name": "phase 34", "peer": {"name": "phase 35", "url": "http://127.0.0.1:8501"}})
    b = await _app(tmp_path, "b", {"name": "phase 35", "peer": {"name": "phase 34", "url": "http://127.0.0.1:8500"}})
    seen: list[httpx.URL] = []

    class Twin(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            assert request.method == "GET" and request.url.host == "127.0.0.1" and request.url.port == 8501
            return await httpx.ASGITransport(app=b).handle_async_request(request)

    monkeypatch.setattr(peer, "_client", lambda: httpx.AsyncClient(transport=Twin(), timeout=5))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url="http://t") as c:
        assert (await c.get("/api/engine")).json() == {"name": "phase 34", "peer": {"name": "phase 35"}}
        twin = await c.get("/api/peer/engine")
        assert twin.status_code == 200 and twin.json()["name"] == "phase 35", "the twin's own answer, through this engine"
        alerts = await c.get("/api/peer/alerts", params={"book": "FUDKII_RT_X"})
        assert alerts.status_code == 200 and "alerts" in alerts.json()
        assert seen[-1].path == "/api/alerts" and seen[-1].params["book"] == "FUDKII_RT_X", "the query is forwarded"
        assert (await c.get("/api/peer/peer/engine")).status_code == 400, "never a twin's twin"
        assert (await c.post("/api/peer/books/FUDKII_RT_X/take", json={})).status_code == 405, "read-only: no POST"


@pytest.mark.asyncio
async def test_one_engine_alone_has_no_twin_and_a_remote_twin_is_refused(tmp_path):
    for name, ident in (("solo", None), ("far", {"name": "x", "peer": {"name": "y", "url": "http://10.0.0.5:8501"}}),
                        ("https", {"name": "x", "peer": {"name": "y", "url": "https://127.0.0.1:8501"}})):
        app = await _app(tmp_path, name, ident)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/api/engine")).json()["peer"] is None, name
            assert (await c.get("/api/peer/alerts")).status_code == 404, name


@pytest.mark.asyncio
async def test_a_twin_that_is_down_reads_as_not_answering(tmp_path, monkeypatch):
    app = await _app(tmp_path, "a", {"name": "phase 34", "peer": {"name": "phase 35", "url": "http://127.0.0.1:8501"}})

    class Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(peer, "_client", lambda: httpx.AsyncClient(transport=Down(), timeout=5))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/peer/alerts")
        assert r.status_code == 502 and "phase 35 is not answering" in r.json()["detail"]
