"""Every charge rate in one file the operator edits: ``<data_dir>/charges.toml``.

Brokerage and statutory rates change by circular and by plan, so they are data, not code
(operator, 2026-09-26: "create a separate area where all charges are parked, that can be changed
anytime since the charges change often"). The rates are PER PRODUCT — Zerodha's table (the
operator's reference, https://zerodha.com/charges/, read 2026-09-26) has a different line for cash
intraday, cash delivery, F&O futures, F&O options, currency and commodity futures and options, and
one set of numbers for all of them costed options with the futures' STT and a cash brokerage.

The file is written on first boot from ``PRODUCT_DEFAULTS``; after that it wins, and it is re-read
when it changes — no restart — at most once a second. A file that fails to parse or carries a bad
value keeps the LAST GOOD rates in force and says why on /charges.

Every percentage is of the leg's TURNOVER (price × total quantity), charged once per leg — four
lots are one turnover, not four. Brokerage is per EXECUTED ORDER by default (Zerodha's rule: one
order for 4 lots is one ₹20), or per lot if ``[brokerage] basis = "lot"``.
"""

from __future__ import annotations

import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings

FILE_NAME = "charges.toml"
#: The engine (and any tool) reads ``<data_dir>/charges.toml``. The test suite switches this off
#: (tests/conftest.py): deploy.sh runs the tests INSIDE the live folder, and a test building its own
#: Settings must never create, read or overwrite the operator's file. An explicit ``path`` is always
#: honoured, so the file's own tests pass one.
AUTO_FILE = True

SOURCE = "https://zerodha.com/charges/ (read 2026-09-26)"

#: product → what each field means is in FIELDS. Zerodha's published rates, 2026-09-26.
PRODUCT_DEFAULTS: dict[str, dict[str, float]] = {
    "equity_intraday": {"brokerage_flat_inr": 20, "brokerage_pct": 0.03, "stt_sell_pct": 0.025, "stt_buy_pct": 0,
                        "exchange_pct": 0.00307, "stamp_buy_pct": 0.003, "sebi_pct": 0.0001},
    "equity_delivery": {"brokerage_flat_inr": 0, "brokerage_pct": 0, "stt_sell_pct": 0.1, "stt_buy_pct": 0.1,
                        "exchange_pct": 0.00307, "stamp_buy_pct": 0.015, "sebi_pct": 0.0001},
    "fo_futures": {"brokerage_flat_inr": 20, "brokerage_pct": 0.03, "stt_sell_pct": 0.05, "stt_buy_pct": 0,
                   "exchange_pct": 0.00183, "stamp_buy_pct": 0.002, "sebi_pct": 0.0001},
    "fo_options": {"brokerage_flat_inr": 20, "brokerage_pct": 0, "stt_sell_pct": 0.15, "stt_buy_pct": 0,
                   "exchange_pct": 0.03553, "stamp_buy_pct": 0.003, "sebi_pct": 0.0001},
    "currency_futures": {"brokerage_flat_inr": 20, "brokerage_pct": 0.03, "stt_sell_pct": 0, "stt_buy_pct": 0,
                         "exchange_pct": 0.00035, "stamp_buy_pct": 0.0001, "sebi_pct": 0.0001},
    "currency_options": {"brokerage_flat_inr": 20, "brokerage_pct": 0, "stt_sell_pct": 0, "stt_buy_pct": 0,
                         "exchange_pct": 0.0311, "stamp_buy_pct": 0.0001, "sebi_pct": 0.0001},
    "commodity_futures": {"brokerage_flat_inr": 20, "brokerage_pct": 0.03, "stt_sell_pct": 0.01, "stt_buy_pct": 0,
                          "exchange_pct": 0.0021, "stamp_buy_pct": 0.002, "sebi_pct": 0.0001},
    "commodity_options": {"brokerage_flat_inr": 20, "brokerage_pct": 0, "stt_sell_pct": 0.05, "stt_buy_pct": 0,
                          "exchange_pct": 0.0418, "stamp_buy_pct": 0.003, "sebi_pct": 0.0001},
}
PRODUCT_TITLES = {
    "equity_intraday": "Equity intraday (NSE cash)", "equity_delivery": "Equity delivery (NSE cash, overnight)",
    "fo_futures": "F&O futures (NSE)", "fo_options": "F&O options (NSE)",
    "currency_futures": "Currency futures (not traded here)", "currency_options": "Currency options (not traded here)",
    "commodity_futures": "Commodity futures (MCX)", "commodity_options": "Commodity options (MCX)",
}
#: field → (unit, meaning)
FIELDS: dict[str, tuple[str, str]] = {
    "brokerage_flat_inr": ("₹ per executed order (or per lot, see basis)", "Brokerage."),
    "brokerage_pct": ("% of turnover", "If above 0, brokerage = min(flat, this % of turnover). 0 = the flat charge only."),
    "stt_sell_pct": ("% of turnover", "STT/CTT on the SELL leg (options: on premium)."),
    "stt_buy_pct": ("% of turnover", "STT on the BUY leg (delivery only)."),
    "exchange_pct": ("% of turnover", "Exchange transaction charge (NSE, or MCX for commodities)."),
    "stamp_buy_pct": ("% of turnover", "Stamp duty on the BUY leg."),
    "sebi_pct": ("% of turnover", "SEBI turnover fee (₹10 / crore = 0.0001%)."),
}
DEFAULT_GLOBALS: dict[str, Any] = {"gst_pct": 18.0, "basis": "order", "slippage_bps_default": 5.0}

_HEADER = f"""# Kotsin charges — every rate the engine uses to cost a trade, per product.
# Source of the defaults: {SOURCE}
# Edit a value and save: the engine re-reads this file within a second, no restart.
# A typo keeps the previous values in force and shows the error at http://127.0.0.1:8500/charges
# Percentages are in percent (0.15 means 0.15%) and are charged ONCE on each leg's total turnover
# (price x total quantity) — 4 lots is one turnover, not four.
"""


@dataclass(slots=True)
class ChargeRates:
    """The rates in force, the file they came from, and whether the last read worked."""

    s: Settings
    path: Path | None = None
    #: applied on top of the file on every read — ``brokerage_mult`` (a cost-stress replay)
    #: multiplies every product's flat brokerage; a stressed Settings copy would be overridden by the file
    overrides: dict[str, float] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)
    loaded_at: float | None = None
    mtime: float | None = None
    error: str = ""
    migrated_from: str = ""
    _checked: float = 0.0

    def __post_init__(self) -> None:
        self.values = self._apply(_defaults())
        if self.path is None and AUTO_FILE and self.s.data_dir is not None:
            self.path = Path(self.s.data_dir) / FILE_NAME
        if self.path is not None:
            try:
                if self.path.exists() and _is_old_format(self.path):
                    # the first file (one set of rates for everything) — keep it, write the per-product one
                    backup = self.path.with_name(f"{FILE_NAME}.single-rate-{time.strftime('%Y%m%d-%H%M%S')}")
                    self.path.rename(backup)
                    self.migrated_from = str(backup)
                if not self.path.exists():
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.path.write_text(render_file(_defaults()), encoding="utf-8")
            except OSError as exc:
                self.error = f"could not write {self.path}: {exc}"
            self._reload()

    def current(self) -> dict[str, Any]:
        """The rates to cost a trade with — the file's, re-read if it changed (checked ≤ 1/s)."""
        now = time.monotonic()
        if self.path is not None and now - self._checked >= 1.0:
            self._checked = now
            try:
                mtime = self.path.stat().st_mtime
            except OSError:
                mtime = None
            if mtime is not None and mtime != self.mtime:
                self._reload()
        return self.values

    def product(self, name: str) -> dict[str, float]:
        return self.current()["products"][name]

    def _apply(self, v: dict[str, Any]) -> dict[str, Any]:
        mult = float(self.overrides.get("brokerage_mult", 1.0))
        if mult == 1.0:
            return v
        out = {**v, "products": {k: dict(p) for k, p in v["products"].items()}}
        for p in out["products"].values():
            p["brokerage_flat_inr"] *= mult
        return out

    def _reload(self) -> None:
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime
            data = tomllib.loads(self.path.read_text(encoding="utf-8"))
            fresh = _defaults()
            for name, prod in fresh["products"].items():
                sec = data.get(name) or {}
                for key in prod:
                    if key in sec:
                        v = float(sec[key])
                        if v < 0 or v != v:
                            raise ValueError(f"[{name}] {key} = {sec[key]!r} must be a number ≥ 0")
                        prod[key] = v
            glob = data.get("all") or {}
            if "gst_pct" in glob:
                g = float(glob["gst_pct"])
                if g < 0 or g != g:
                    raise ValueError(f"[all] gst_pct = {glob['gst_pct']!r} must be a number ≥ 0")
                fresh["gst_pct"] = g
            if "slippage_bps_default" in glob:
                fresh["slippage_bps_default"] = float(glob["slippage_bps_default"])
            basis = str((data.get("brokerage") or {}).get("basis", fresh["basis"])).strip().lower()
            if basis not in ("order", "lot"):
                raise ValueError(f'[brokerage] basis = {basis!r} must be "order" or "lot"')
            fresh["basis"] = basis
        except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"[:300]
            self.mtime = None  # try again on the next change
            return
        self.values, self.mtime, self.loaded_at, self.error = self._apply(fresh), mtime, time.time(), ""

    def status(self) -> dict[str, Any]:
        v = self.current()
        return {
            "path": str(self.path) if self.path else None, "loadedAt": self.loaded_at, "error": self.error,
            "source": SOURCE, "basis": v["basis"], "gstPct": v["gst_pct"], "slippageBps": v["slippage_bps_default"],
            "migratedFrom": self.migrated_from,
            "products": [{"product": k, "title": PRODUCT_TITLES[k], **p} for k, p in v["products"].items()],
            "fields": [{"key": k, "unit": u, "about": a} for k, (u, a) in FIELDS.items()],
        }


def _defaults() -> dict[str, Any]:
    return {**DEFAULT_GLOBALS, "products": {k: dict(v) for k, v in PRODUCT_DEFAULTS.items()}}


def _is_old_format(path: Path) -> bool:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return "fo_options" not in data and ("stt" in data or "exchange" in data)


def render_file(values: dict[str, Any]) -> str:
    """The file as written on first boot: every product, each field with its unit and meaning."""
    out = [_HEADER, "[brokerage]",
           '# "order" = the flat brokerage once per executed order, whatever the lots (Zerodha); "lot" = per lot',
           f'basis = "{values["basis"]}"', "", "[all]",
           "# GST, % of (brokerage + exchange charge + SEBI fee)", f"gst_pct = {values['gst_pct']:g}",
           "# ONLY for a paper fill with no order book at all: the LTP moved this much against us (basis points).",
           "# Every other fill trades against the book — a limit at the signal price or the mid, or the bid/ask when crossed.",
           f"slippage_bps_default = {values['slippage_bps_default']:g}"]
    for name, prod in values["products"].items():
        out += ["", f"[{name}]  # {PRODUCT_TITLES[name]}"]
        for key, v in prod.items():
            unit, about = FIELDS[key]
            out.append(f"{key} = {v:g}  # {unit} — {about}")
    return "\n".join(out) + "\n"
