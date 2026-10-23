"""Run policies over recorded days and stocks; one row per (policy, stock, day) and a total."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any

from ..config import Settings
from ..risk.costs import CostModel
from .day import coverage, load_day
from .env import OptionBuyEnv
from .policies import Policy


def episode(env: OptionBuyEnv, policy: Policy) -> dict[str, Any]:
    obs, info = env.reset()
    total, done = 0.0, False
    while not done:
        obs, r, done, info = env.step(policy.act(env, obs, info["can"]))
        total += r
    s = env.summary()
    s["reward_sum"] = round(total, 2)  # equals the net P&L: the accounting check
    return s


def run(settings: Settings, days: list[date], roots: list[str] | None, policies: list[Policy], **env_kw: Any) -> dict[str, Any]:
    costs = CostModel(settings)
    rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    cov: list[dict[str, Any]] = []
    for d in days:
        data = load_day(settings.data_dir, d, roots)
        cov += [{"day": d.isoformat(), **coverage(rd)} for rd in data.values()]
        for p in policies:
            for sym, rd in data.items():
                env = OptionBuyEnv(rd, costs, **env_kw)
                s = episode(env, p)
                rows.append({"policy": p.name, "symbol": sym, "day": d.isoformat(), **s})
                trades += [{"policy": p.name, **asdict(t)} for t in env.trades]
    totals: dict[str, dict[str, Any]] = {}
    for r in rows:
        t = totals.setdefault(r["policy"], {"trades": 0, "net": 0.0, "charges": 0.0, "spread_paid": 0.0, "wins": 0})
        t["trades"] += r.get("trades", 0)
        t["net"] += r.get("net", 0.0)
        t["charges"] += r.get("charges", 0.0)
        t["spread_paid"] += r.get("spread_paid", 0.0)
    for name, t in totals.items():
        tr = [x for x in trades if x["policy"] == name]
        t["wins"] = sum(x["net"] > 0 for x in tr)
        t["win_rate"] = round(100 * t["wins"] / len(tr), 1) if tr else None
        t["avg_net"] = round(t["net"] / len(tr), 2) if tr else None
        for k in ("net", "charges", "spread_paid"):
            t[k] = round(t[k], 2)
    return {"rows": rows, "totals": totals, "coverage": cov, "trades": trades}


__all__ = ["episode", "run"]
