"""mypy, gated on NEW errors only (run from backend/: `uv run python ../scripts/mypy_gate.py`).

mypy would have caught three of the defects the 2026-10-03 review found — the backtester's missing
Context methods (0 trades for a day), is_open called without its calendar (no held quote ever
refreshed), a field read off a bar that never had it — but the codebase carries ~76 older errors, so
a plain `mypy` step would be red from the first run and ignored. This records them in
backend/mypy-baseline.txt (by file, code and message — not line, so editing a file does not move
them) and fails only when a run has an error the baseline does not.

    --update   rewrite the baseline from the current run (after fixing errors, to lock the gain in)
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

BASELINE = Path("mypy-baseline.txt")
LINE = re.compile(r"^(?P<path>[^:]+):\d+: error: (?P<msg>.*)$")


def run() -> Counter[str]:
    out = subprocess.run(
        [sys.executable, "-m", "mypy", "kotsin_nse", "--ignore-missing-imports", "--no-error-summary",
         "--hide-error-context", "--no-color-output"],
        capture_output=True, text=True, check=False,
    ).stdout
    keys: Counter[str] = Counter()
    for line in out.splitlines():
        m = LINE.match(line.strip())
        if m:
            keys[f"{m['path']}: {m['msg']}"] += 1
    return keys


def main() -> int:
    now = run()
    if "--update" in sys.argv:
        BASELINE.write_text("".join(f"{k}\t{n}\n" for k, n in sorted(now.items())), encoding="utf-8")
        print(f"baseline written: {sum(now.values())} errors in {len({k.split(':')[0] for k in now})} files")
        return 0
    held: Counter[str] = Counter()
    if BASELINE.exists():
        for line in BASELINE.read_text(encoding="utf-8").splitlines():
            key, _, n = line.rpartition("\t")
            held[key] = int(n)
    new = {k: n - held.get(k, 0) for k, n in now.items() if n > held.get(k, 0)}
    fixed = sum(max(0, n - now.get(k, 0)) for k, n in held.items())
    if new:
        print(f"{sum(new.values())} NEW mypy error(s) — fix them (or, if intended, `--update`):")
        for k, n in sorted(new.items()):
            print(f"  {k}" + (f"  (×{n})" if n > 1 else ""))
        return 1
    print(f"mypy: no new errors ({sum(now.values())} held in the baseline" + (f", {fixed} fixed since — run --update" if fixed else "") + ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
