import asyncio, json, time
from collections import deque
import pytest
from kotsin_nse.alerts.detectors import Alert
from kotsin_nse.api.routes import build_app
from kotsin_nse.engine import Engine

SCR = "/private/tmp/claude-501/-Users-devinakothari-Downloads-kotsincode/5405d416-d2e8-4d9c-a2ea-3ee96d86d293/scratchpad"

@pytest.mark.asyncio
async def test_probe(settings, equity):
    row = json.load(open(SCR + "/row_full.json"))
    e = Engine(settings)
    app = build_app(e)
    import httpx
    for n in (500, 1000, 3000):
        ring = deque(maxlen=5000)
        for i in range(n):
            a = Alert(book="FUDKII_RT", symbol="RELIANCE", scrip_code="2885", tf="1m",
                      ts=1790000000 + i, direction="BULLISH", score=50.0, reason=row["reason"],
                      price=100.0, evidence=dict(row["evidence"]), kind="KEEPALIVE",
                      plan=json.loads(json.dumps(row["plan"])), cta=dict(row["cta"]),
                      company="RELIANCE", exchange="N", bar_close=1790000060,
                      card=json.loads(json.dumps(row["card"])), fired_at=time.time())
            ring.append(a)
        e.alerts.alerts["FUDKII_RT"] = ring
        e.alerts.counts["FUDKII_RT"] = n

        gaps = []
        stop = False
        async def ticker():
            prev = time.perf_counter()
            while not stop:
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - prev)
                prev = now
        t = asyncio.create_task(ticker())
        await asyncio.sleep(0.2)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            t0 = time.perf_counter()
            r = await c.get("/api/alerts")
            dt = (time.perf_counter() - t0) * 1000
        stop = True
        await t
        print(f"\nn={n} status={r.status_code} bytes={len(r.content)/1e6:.3f}MB "
              f"request_ms={dt:.0f} max_loop_stall_ms={max(gaps)*1000:.0f}")
