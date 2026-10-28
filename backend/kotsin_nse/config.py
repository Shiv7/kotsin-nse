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
from typing import Literal

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

    # ---- the Analysis page's written summary (analysis/llm.py) -------------------------------
    #: Anthropic API key (KN_ANTHROPIC_API_KEY in .env); without it the summary says so
    anthropic_api_key: SecretStr | None = None
    fp_totp_secret: SecretStr | None = None  # base32 seed; the engine derives the code itself
    fp_app_source: int = 23312
    fp_app_name: str | None = None
    fp_public_ip: str | None = None  # RMS rejects orders when this is wrong; auto-detected if unset

    # ---- universe -----------------------------------------------------------------------------
    segments: str = "NSE_EQ"  # comma-separated Segment names
    universe_file: Path | None = None  # optional explicit symbol list, one per line
    max_universe: int | None = 200  # None = no cap (R4: a cap is None-able, never 999)

    # ---- storage ------------------------------------------------------------------------------
    data_dir: Path = Path("./data")
    db_url: str = "sqlite+aiosqlite:///./data/kotsin_nse.db"
    #: keep the live session: per-IST-day Parquet of 1m bars and OI for every subscribed code
    #: (``ops/archive.py``). The REST history has no OI, so what is not archived is gone.
    archive_enabled: bool = True
    #: also keep every quote frame of the underlyings, futures and option strikes: the raw
    #: material for volume-at-price, intraday option premiums, and judging a source's resolution
    archive_ticks: bool = True
    #: one depth snapshot per contract at most this often (underlyings + front futures)
    archive_depth_every_s: float = 5.0
    #: how many levels of each side that snapshot keeps. 5paisa's MarketDepthService sends 5 (every
    #: NSE frame on 2026-10-23 had at most 5); its 20-level feed is a separate gateway this account
    #: is refused on ("Unauthorized exception", 2026-10-23)
    archive_depth_levels: int = 5
    archive_flush_s: float = 300.0

    # ---- service ------------------------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8500
    engine_enabled: bool = True  # false → API only (tests and UI work with no broker session)
    feed_enabled: bool = True  # false → REST-polled bars only, no WebSocket
    #: who streams market data (``feed/``). Orders always go to 5paisa; data may later come from a
    #: vendor. Only sources with a tested adapter are accepted.
    data_source: Literal["fivepaisa"] = "fivepaisa"

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # ---- bars -----------------------------------------------------------------------------------
    backfill_days: int = 30  # daily/intraday history seeded from REST at boot
    #: How long a closed 30m bar waits for the exchange's own candle before the live build stands.
    #: Measured: REST serves a bucket while it is still forming, so this is a ceiling, not a
    #: typical wait.
    bar_reconcile_timeout_s: float = 12.0
    #: Periodic REST sweep of the finer frames (fidelity metric + chart accuracy). One call per
    #: symbol per swept timeframe per interval.
    bar_sweep_interval_s: float = 300.0
    #: scripFinder's strike shortlist: ±band around the previous close, N per side, nearest expiry
    universe_band_pct: float = 12.0
    universe_strikes_per_side: int = 5
    universe_include_indices: bool = True
    #: how many futures per root to stream (None = every unexpired month); OI history of the rest
    #: comes from the daily bhavcopy
    universe_futures_months: int | None = 1
    #: order books on the option strikes too (each book is a heavy stream on 5paisa)
    universe_option_depth: bool = False
    #: order books at all; False leaves only quotes and OI. Measured 2026-10-19: with no books at
    #: all near futures rose only 2.45 → 3.11 frames/min — 5paisa itself caps the futures rate
    universe_depth: bool = True
    #: the front futures' books too (their order flow is noise at ~3 frames/min anyway)
    universe_future_depth: bool = False
    #: the focus list (``instrument/focus.py``): the most traded option underlyings of the last
    #: session plus these indices get the front future's book and ATM ± ``focus_strikes_each_side``
    #: strikes with their books; everything else keeps quotes, the cash book and OI
    focus_top_n: int = 10
    focus_indices: str = "NIFTY,BANKNIFTY"
    focus_strikes_each_side: int = 4
    #: re-centre a root's strikes on its live future once it has moved a strike from the centre
    strike_recenter: bool = True

    # ---- the strategy (strategy/) --------------------------------------------------------------
    #: run the CPR retest sequence (``strategy/cpr_retest.py``) on every closed 5m bar of the cash
    #: equities. Orders go through the gateway in whatever mode the control row says — SHADOW by
    #: default, where the book is scored here and nothing is placed.
    strategy_enabled: bool = True
    #: the exit rule the shadow book trades (the other two are scored alongside as "virtual")
    strategy_rule: Literal["wall_then_trail", "fixed_wall", "fixed_1to2"] = "wall_then_trail"
    #: ``none`` = every confirmed retest; else tag names joined by ``+`` that must all hold at the
    #: entry (``cpr_retest.daily_tags`` / ``intraday_tags``), e.g. ``st60_al+dma20_al``
    strategy_filter: str = "none"
    #: shadow size per trade, rupees of notional (the research's bps are size-free; this is for ₹)
    strategy_notional_inr: float = 25_000.0
    #: the previous session's close the CPR is built on. ``official`` = the exchange's close (the broker's
    #: daily candle; for an F&O stock the closing auction's price since 2026-08-03). ``last`` = a stock's last
    #: continuous trade before the 15:15 auction (the close of its 14:45 30m bar), which is what a chart drawn
    #: from ticks shows: AXISBANK 2026-10-26 closed 1220.0 in the auction against 1225.0 last traded, and the
    #: owner's Kite CPR implied 1224.97. An index has no auction; ``last`` falls back to its official close.
    strategy_pdc: Literal["official", "last"] = "official"
    #: ``exchange`` = a closed 5m bar waits (bounded) for the exchange's candle before the strategy reads
    #: it; ``feed`` = the socket's build decides at once
    strategy_bars: Literal["exchange", "feed"] = "exchange"
    #: how long a decision may wait for the exchange's 5m candle (217 symbols close in the same second)
    strategy_bar_timeout_s: float = 25.0
    #: round numbers as walls. The 29 Oct 2026 read of the first fifteen shadow trades: five of the eight first
    #: walls under 0.8R were round numbers, and the trail to them gave AXISBANK's 96 bps and PIIND's 232 away.
    #: On the dev window dropping them moves the mean by nothing (+3.2 gross either way): the walls are the
    #: levels on the chart, nothing else.
    strategy_walls_round: bool = False
    #: no entry once the stock has travelled this many ADRs from the open (0 = off). Dev window, 3,101 entries:
    #: the 604 past 0.6 ADR made −1.3 bps gross, the rest +4.4 (RECLTD, PREMIERENE, HINDUNILVR on 28 Oct).
    strategy_max_move_adr: float = 0.6
    #: with ``strategy_pdc = last``, the previous day's high and low from continuous trades too (its 30m bars
    #: before 15:15). The 27 Oct 2026 closing auction printed PREMIERENE at 862 against a last trade of 835 and
    #: that print became PDH and R1 on the 28th. ``daily`` = the daily candle's high/low, auction print included.
    strategy_levels_hl: Literal["continuous", "daily"] = "continuous"

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
    # the numbers the paper filler uses, so a strategy cannot look profitable on paper and
    # unprofitable live because two cost models disagreed.
    #: Flat charge per order. **This is the measured value on this account**, not a published
    #: tariff: ₹40/order × 2 accounted for 81% of a 0.299% round trip at ₹33,000.
    cost_brokerage_per_order_inr: float = 40.0
    #: Percentage slab, applied as ``min(flat, pct × turnover)`` when > 0. Left at 0 by default so
    #: the flat charge is what the model uses — a slab of 0.03% would quietly replace ₹40 with
    #: ₹9.90 at ₹33,000 and make every small position look three times cheaper than it is.
    cost_brokerage_pct: float = 0.0
    cost_stt_pct_sell_equity: float = 0.025
    #: overnight (delivery) cash equity pays STT on BOTH legs at this rate; the intraday rate
    #: above applies to the sell leg only.
    cost_stt_pct_delivery_equity: float = 0.1
    cost_stt_pct_sell_option_premium: float = 0.1  # 0.1 % of premium on sale since 2024-10-01 (was 0.0625)
    cost_stt_pct_sell_future: float = 0.02
    cost_exchange_txn_pct_equity: float = 0.00297
    cost_exchange_txn_pct_option: float = 0.05
    cost_exchange_txn_pct_future: float = 0.00173
    cost_sebi_pct: float = 0.0001
    cost_stamp_pct_buy: float = 0.003
    cost_gst_pct: float = 18.0
    slippage_bps_default: float = 5.0

    @field_validator("max_universe", "universe_futures_months", mode="before")
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
