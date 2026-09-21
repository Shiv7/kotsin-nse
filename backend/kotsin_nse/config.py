"""Typed, closed configuration.

Rule R1 (``docs/LEARNINGS.md``): a config key nothing reads is a bug that hides — and the NSE stack
lost weeks to exactly that (``fudkii.trigger.bb.period`` read into a field and only ever logged;
``fukaa.trigger.volume.multiplier`` set in properties while the code read three *other* keys;
``retest.v2.*`` whose configured and read key sets did not overlap at all). So here:

* every setting is a declared field on :class:`Settings` — there is no other place one can come from;
* an unknown key in ``.env`` fails validation (``extra="forbid"``);
* an unknown ``KN_*`` variable in the process environment fails at boot
  (:func:`assert_no_unknown_env`) — pydantic-settings ignores those by default, which is how a typo
  like ``KN_SEGMENT=NSE_EQ`` would otherwise run silently on the default;
* ``None`` means OFF for every cap, and the boot banner prints it (R4 — no ``top.n=999`` sentinels).

Trading mode (SHADOW / PAPER / LIVE_CAPPED / LIVE) is deliberately **not** here. It is a row in the
control table (R9), because CAN2 spent eight weeks paper-trading after a restart dropped
``CAN2_LIVE=true`` and nothing alerted.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "KN_"


class Segment(StrEnum):
    """A tradeable book. The pair (exch, exch_type) is the 5paisa wire identity."""

    NSE_EQ = "NSE_EQ"
    NSE_FO = "NSE_FO"
    NSE_IDX = "NSE_IDX"
    MCX_FO = "MCX_FO"

    @property
    def exch(self) -> str:
        return "M" if self is Segment.MCX_FO else "N"

    @property
    def exch_type(self) -> str:
        if self is Segment.NSE_EQ:
            return "C"
        if self is Segment.NSE_IDX:
            return "C"  # indices ride the cash feed with 999920xx codes
        return "D"

    @property
    def scripmaster_key(self) -> str:
        """Segment name in ``ScripMaster/segment/{key}``."""
        return {
            Segment.NSE_EQ: "nse_eq",
            Segment.NSE_FO: "nse_fo",
            Segment.NSE_IDX: "nse_eq",
            Segment.MCX_FO: "mcx_fo",
        }[self]


class Endpoints(BaseModel, frozen=True):
    rest: str
    ws: str
    scripmaster: str


# Verified 2026-09-20 against the running NSE stack (optionProducerJava WebSocketManager:245,
# FivePaisaBrokerService:38, scripFinder ScripIngestionServiceImpl:212).
FIVEPAISA = Endpoints(
    rest="https://Openapi.5paisa.com/VendorsAPI/Service1.svc/",
    ws="wss://openfeed.5paisa.com/Feeds/api/chat?Value1=",
    scripmaster="https://openapi.5paisa.com/VendorsAPI/Service1.svc/ScripMaster/segment/",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    env: str = "dev"
    log_level: str = "INFO"

    # ---- broker credentials -------------------------------------------------------------------
    # NEVER hardcoded. The old stack committed all six of these plus the TOTP seed to git in three
    # separate files; they must be rotated and supplied through .env (git-ignored) only.
    fp_client_code: str | None = None  # numeric login id (this is an identifier, not a secret, but it is not published here either)
    fp_user_id: SecretStr | None = None
    fp_password: SecretStr | None = None
    fp_app_key: SecretStr | None = None  # "Key" in the request head
    fp_encrypt_key: SecretStr | None = None
    fp_pin: SecretStr | None = None
    fp_totp_secret: SecretStr | None = None  # base32 seed; the engine derives the code itself
    fp_app_source: int = 23312
    fp_app_name: str | None = None
    fp_public_ip: str | None = None  # RMS rejects orders when this is wrong; auto-detected if unset

    # ---- universe -----------------------------------------------------------------------------
    segments: str = "NSE_EQ"  # comma-separated Segment names
    universe_file: Path | None = None  # optional explicit symbol list, one per line
    max_universe: int | None = 200  # None = no cap (R4: a cap is None-able, never 999)
    calibration_file: Path | None = None  # per-scrip CAN2 params; hot-reloaded, not boot-only

    # ---- storage ------------------------------------------------------------------------------
    data_dir: Path = Path("./data")
    db_url: str = "sqlite+aiosqlite:///./data/kotsin_nse.db"
    archive_enabled: bool = True

    # ---- service ------------------------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8500
    engine_enabled: bool = True  # false → API only (tests and UI work with no broker session)
    feed_enabled: bool = True  # false → REST-polled bars only, no WebSocket

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # ---- paper accounting ---------------------------------------------------------------------
    paper_initial_inr: float = 1_000_000.0  # per-strategy wallet, matching the old 10-lakh default
    backfill_days: int = 30  # daily/intraday history seeded from REST at boot
    #: The 30m decision waits this long for the exchange's own candle before deciding on the live
    #: build. Measured: REST serves a bucket while it is still forming, so this is a ceiling, not a
    #: typical wait. Past it the decision proceeds on the live bar and is counted.
    decision_reconcile_timeout_s: float = 12.0
    #: Periodic REST sweep of the finer frames (fidelity metric + chart accuracy). One call per
    #: symbol per swept timeframe per interval.
    bar_sweep_interval_s: float = 300.0
    #: scripFinder's strike shortlist: ±band around the previous close, N per side, nearest expiry
    universe_band_pct: float = 12.0
    universe_strikes_per_side: int = 5
    universe_include_indices: bool = True
    #: An open position whose contract has not quoted for this long is NOT evaluated for exits —
    #: a stop checked against a price from minutes ago is worse than one not checked at all. A
    #: forced exit (force-flat or halt) overrides this and proceeds on the last known price.
    position_quote_max_age_s: float = 60.0

    # ---- review committee -----------------------------------------------------------------------
    # Post-mortems on the algo's own signals and trades, by Claude, with an experiment loop that
    # grades every proposal by running the backtester. Advisory: nothing here reaches the decision
    # path. Off until a key is present (``ANTHROPIC_API_KEY`` in the environment also works).
    anthropic_api_key: SecretStr | None = None
    committee_model: str = "claude-opus-5"
    #: review every closed trade automatically (five Claude calls each), within the daily cap
    committee_auto_review: bool = False
    #: cost guard: a run is one case (5 calls) or one cohort (4)
    committee_max_runs_per_day: int = 30
    #: decision-frame bars after the signal shown to a case review ("what happened next")
    committee_path_bars: int = 16
    #: cap the symbols an experiment backtests (None = every symbol in the history cache)
    committee_experiment_symbols: int | None = None

    # ---- LIVE_CAPPED caps ----------------------------------------------------------------------
    # Mode is state (R9); these only bound what an *armed* engine may do.
    live_segments: str = "NSE_EQ"
    live_max_qty_rupees: float = 25_000.0  # notional per order
    live_max_positions: int = 2
    live_max_orders_per_day: int = 6
    live_daily_loss_inr: float = 2_000.0
    live_entry_cutoff_ist: str = "15:10"

    # ---- cost model ----------------------------------------------------------------------------
    # Measured on this book, not assumed: at ₹33,000/position the NSE cash round trip was 0.299%,
    # of which 81% was flat brokerage (₹40/order × 2) — see kotsin-box/SESSION-PRIMER.md. These are
    # the numbers the backtester and the paper filler both use, so a strategy cannot look profitable
    # in research and unprofitable live because two cost models disagreed.
    #: Flat charge per order. **This is the measured value on this account**, not a published
    #: tariff: ₹40/order × 2 accounted for 81% of a 0.299% round trip at ₹33,000.
    cost_brokerage_per_order_inr: float = 40.0
    #: Percentage slab, applied as ``min(flat, pct × turnover)`` when > 0. Left at 0 by default so
    #: the flat charge is what the model uses — a slab of 0.03% would quietly replace ₹40 with
    #: ₹9.90 at ₹33,000 and make every small position look three times cheaper than it is.
    cost_brokerage_pct: float = 0.0
    cost_stt_pct_sell_equity: float = 0.025
    cost_stt_pct_sell_option_premium: float = 0.0625
    cost_stt_pct_sell_future: float = 0.02
    cost_exchange_txn_pct_equity: float = 0.00297
    cost_exchange_txn_pct_option: float = 0.05
    cost_exchange_txn_pct_future: float = 0.00173
    cost_sebi_pct: float = 0.0001
    cost_stamp_pct_buy: float = 0.003
    cost_gst_pct: float = 18.0
    slippage_bps_default: float = 5.0

    @field_validator("max_universe", "committee_experiment_symbols", mode="before")
    @classmethod
    def _uncapped_is_blank(cls, v: object) -> object:
        """``KN_MAX_UNIVERSE=`` means no cap.

        The field documents ``None = no cap`` but every value arrives from the environment as a
        string, so ``None`` had no spelling: blank failed int parsing and the only way to take the
        whole list was a number chosen to exceed it — the "never 999" this rule exists to forbid.
        """
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("segments", "live_segments")
    @classmethod
    def _segments_valid(cls, v: str) -> str:
        names = [s.strip().upper() for s in v.split(",") if s.strip()]
        if not names:
            raise ValueError("must list at least one segment")
        for n in names:
            if n not in Segment.__members__:
                raise ValueError(f"unknown segment {n!r} — known: {', '.join(Segment.__members__)}")
        return ",".join(names)

    @field_validator("live_entry_cutoff_ist")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        hh, _, mm = v.partition(":")
        if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
            raise ValueError(f"expected HH:MM, got {v!r}")
        return v

    @property
    def segment_list(self) -> list[Segment]:
        return [Segment[s] for s in self.segments.split(",")]

    @property
    def live_segment_list(self) -> list[Segment]:
        return [Segment[s] for s in self.live_segments.split(",")]

    @property
    def endpoints(self) -> Endpoints:
        return FIVEPAISA

    @property
    def has_credentials(self) -> bool:
        """True when a broker session can be established. Market data needs it too — 5paisa has no
        anonymous feed, unlike Delta — so PAPER mode still requires a login."""
        return all(
            v is not None
            for v in (
                self.fp_client_code,
                self.fp_app_key,
                self.fp_encrypt_key,
                self.fp_user_id,
                self.fp_pin,
                self.fp_totp_secret,
            )
        )


class UnknownConfigKeys(RuntimeError):
    """Raised at boot when the environment carries a ``KN_*`` variable no field declares."""


def known_env_keys() -> set[str]:
    return {ENV_PREFIX + name.upper() for name in Settings.model_fields}


def assert_no_unknown_env(environ: Mapping[str, str] | None = None) -> None:
    env = os.environ if environ is None else environ
    known = known_env_keys()
    unknown = sorted(k for k in env if k.upper().startswith(ENV_PREFIX) and k.upper() not in known)
    if unknown:
        raise UnknownConfigKeys(
            f"unknown config keys: {', '.join(unknown)} — known keys: {', '.join(sorted(known))}"
        )
