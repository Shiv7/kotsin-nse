"""The review log: every case, cohort and experiment, with hypotheses that carry a status.

TradingAgents' memory log with Trading-R1's deferred grading, re-aimed: here a decision is a
*hypothesis about the algo* and the grade comes from running the backtester on it. Resolved
hypotheses (confirmed / refuted, with the measured delta) and recent same-symbol lessons are
injected into the next run's prompt — point-in-time: only resolved entries, never pending ones.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..domain import new_id

HYP_STATUSES = ("pending", "running", "confirmed", "refuted", "inconclusive", "error")


class ReviewLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[dict[str, Any]] = []
        if path.exists():
            try:
                self.entries = json.loads(path.read_text())
            except (OSError, ValueError):
                self.entries = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, default=str))
        tmp.replace(self.path)

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        entry.setdefault("id", new_id("rev"))
        entry.setdefault("ts", time.time())
        for h in entry.get("hypotheses") or []:
            h.setdefault("id", new_id("hyp"))
            h.setdefault("status", "pending")
            h["review_id"] = entry["id"]
        self.entries.append(entry)
        self._save()
        return entry

    def get(self, entry_id: str) -> dict[str, Any] | None:
        return next((e for e in self.entries if e.get("id") == entry_id), None)

    def hypotheses(self) -> list[dict[str, Any]]:
        out = []
        for e in self.entries:
            for h in e.get("hypotheses") or []:
                out.append(
                    {
                        **h,
                        "review_id": e["id"],
                        "review_kind": e.get("kind"),
                        "review_ts": e.get("ts"),
                        "subject": e.get("subject"),
                    }
                )
        return out

    def find_hypothesis(self, hyp_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        for e in self.entries:
            for h in e.get("hypotheses") or []:
                if h.get("id") == hyp_id:
                    return e, h
        return None

    def update_hypothesis(self, hyp_id: str, **fields: Any) -> dict[str, Any] | None:
        found = self.find_hypothesis(hyp_id)
        if found is None:
            return None
        _, h = found
        h.update(fields)
        self._save()
        return h

    def past_context(
        self, *, symbol: str | None = None, strategy: str | None = None, n_same: int = 3, n_cross: int = 4
    ) -> str:
        parts: list[str] = []
        resolved = [
            h for h in reversed(self.hypotheses()) if h.get("status") in ("confirmed", "refuted", "inconclusive")
        ][:n_cross]
        if resolved:
            parts.append("Hypotheses already tested (do not propose these again unchanged):")
            for h in resolved:
                r = h.get("result") or {}
                parts.append(
                    f"- {h['status'].upper()}: {h.get('title')} — changes {h.get('changes')} → "
                    f"avg R {r.get('baseline_avg_r')} → {r.get('patched_avg_r')} "
                    f"(Δ {r.get('delta_avg_r')}, p {r.get('p_value')}, n {r.get('patched_n')}). "
                    f"{h.get('reflection') or ''}"
                )
        cases = [
            e
            for e in reversed(self.entries)
            if e.get("kind") == "case"
            and e.get("lesson")
            and (symbol is None or e.get("symbol") == symbol)
            and (strategy is None or e.get("strategy") == strategy)
        ][:n_same]
        if cases:
            parts.append(f"Past case lessons{f' on {symbol}' if symbol else ''} (most recent first):")
            for e in cases:
                parts.append(
                    f"- {e.get('failure_mode')} (confidence {e.get('confidence', 0):.2f}): {e['lesson']}"
                )
        return "\n".join(parts)

    def stats(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        by_mode: dict[str, int] = {}
        for e in self.entries:
            by_kind[e.get("kind", "?")] = by_kind.get(e.get("kind", "?"), 0) + 1
            if e.get("failure_mode"):
                by_mode[e["failure_mode"]] = by_mode.get(e["failure_mode"], 0) + 1
        hyps = self.hypotheses()
        by_status = {s: sum(1 for h in hyps if h.get("status") == s) for s in HYP_STATUSES}
        confs = [e["confidence"] for e in self.entries if e.get("confidence") is not None]
        return {
            "entries": len(self.entries),
            "by_kind": by_kind,
            "by_failure_mode": dict(sorted(by_mode.items(), key=lambda kv: -kv[1])),
            "hypotheses": len(hyps),
            "hypotheses_by_status": {k: v for k, v in by_status.items() if v},
            "mean_confidence": round(sum(confs) / len(confs), 2) if confs else None,
        }


__all__ = ["HYP_STATUSES", "ReviewLog"]
