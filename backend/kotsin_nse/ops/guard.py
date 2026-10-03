"""Per-duty error accounting — so an advisory ``except Exception`` stops being where bugs go to hide.

Two defects lived for days behind broad excepts (review, 2026-10-03): the held-quote refresh raised a
TypeError on every pass with a position open (logged as held_quotes.failed, nothing else), and the
backtester failed every symbol at its first signal (a missing method, logged per symbol, 0 trades
saved). Both were programming errors, not market conditions; both were swallowed exactly like a
broker timeout.

``Guards.duty(name)`` wraps one duty (sync or async ``with``):

* every failure is counted per duty, with its last message and time, and shown in ``/api/health``
  (``duty_errors``) — a duty that keeps failing is visible the same day;
* the log line is rate-limited per duty, so a duty failing every 5 s does not drown the log;
* under test (pytest, or ``KN_STRICT_GUARDS=1``) a PROGRAMMING error — ``TypeError``,
  ``AttributeError``, ``NameError`` — is re-raised instead of swallowed: the suite fails where
  production would have hidden it. Market and I/O failures are still swallowed there, as in
  production, so a test of a broker timeout behaves as the engine does.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

import structlog

log = structlog.get_logger(__name__)

PROGRAMMING_ERRORS: tuple[type[BaseException], ...] = (TypeError, AttributeError, NameError)
#: one log line per duty per this many seconds
LOG_EVERY_S = 60.0


def strict() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or os.environ.get("KN_STRICT_GUARDS") == "1"


@dataclass(slots=True)
class DutyStats:
    runs: int = 0
    errors: int = 0
    programming_errors: int = 0
    last_error: str = ""
    last_error_ts: float = 0.0
    last_logged_ts: float = 0.0


@dataclass(slots=True)
class Guards:
    duties: dict[str, DutyStats] = field(default_factory=dict)

    def duty(self, name: str) -> _Duty:
        return _Duty(self, name)

    def record(self, name: str, exc: BaseException | None) -> bool:
        """Count one run of ``name``. Returns True when the exception is to be swallowed."""
        st = self.duties.setdefault(name, DutyStats())
        st.runs += 1
        if exc is None:
            return True
        programming = isinstance(exc, PROGRAMMING_ERRORS)
        st.errors += 1
        st.programming_errors += int(programming)
        st.last_error = f"{type(exc).__name__}: {exc}"[:200]
        now = time.time()
        st.last_error_ts = now
        if programming and strict():
            return False
        if now - st.last_logged_ts >= LOG_EVERY_S:
            st.last_logged_ts = now
            (log.error if programming else log.warning)("duty.failed", duty=name, error=st.last_error, errors=st.errors,
                                                         programming=programming)
        return True

    def snapshot(self, *, since_s: float | None = None) -> dict[str, dict[str, Any]]:
        """Every duty that has failed (within ``since_s`` when given), worst first."""
        now = time.time()
        rows = {
            name: {"runs": st.runs, "errors": st.errors, "programmingErrors": st.programming_errors,
                   "lastError": st.last_error, "lastErrorAgoS": round(now - st.last_error_ts, 1)}
            for name, st in self.duties.items()
            if st.errors and (since_s is None or now - st.last_error_ts <= since_s)
        }
        return dict(sorted(rows.items(), key=lambda kv: (-kv[1]["programmingErrors"], -kv[1]["errors"])))


class _Duty:
    __slots__ = ("guards", "name")

    def __init__(self, guards: Guards, name: str) -> None:
        self.guards = guards
        self.name = name

    def __enter__(self) -> _Duty:
        return self

    def __exit__(self, et: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> bool:
        if exc is not None and not isinstance(exc, Exception):
            return False  # CancelledError, KeyboardInterrupt, SystemExit: never a duty's to swallow
        return self.guards.record(self.name, exc)

    async def __aenter__(self) -> _Duty:
        return self

    async def __aexit__(self, et: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> bool:
        return self.__exit__(et, exc, tb)


__all__ = ["PROGRAMMING_ERRORS", "DutyStats", "Guards", "strict"]
