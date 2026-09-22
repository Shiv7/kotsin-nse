"""HotStocks: the score is a port, and an unknown must never read as a zero."""

from __future__ import annotations

from typing import ClassVar

from kotsin_nse.hotstocks import scoring
from kotsin_nse.hotstocks.metrics import flow_for
from kotsin_nse.hotstocks.nsepublic import parse_bhavcopy, parse_deals, parse_indices

BHAV = (
    "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, "
    "CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
    "RELIANCE, EQ, 21-Sep-2026, 1226.40, 1230.00, 1250.00, 1225.00, 1246.00, 1246.40, 1240.00, "
    "8273311, 102000.50, 250000, 4136655, 50.00\n"
    "SOMEBOND, N2, 21-Sep-2026, 100.00, 100.00, 101.00, 99.00, 100.50, 100.50, 100.00, "
    "500, 0.50, 5, 250, 50.00\n"
)


def test_bhavcopy_keeps_only_eq_and_survives_the_space_padded_header():
    """NSE pads both header and cells; reading them raw filters every row away."""
    rows = parse_bhavcopy(BHAV)
    assert set(rows) == {"RELIANCE"}
    assert rows["RELIANCE"].deliv_pct == 50.0
    assert rows["RELIANCE"].traded_qty == 8_273_311


def test_deals_and_indices_parse_the_shapes_the_exchange_actually_returns():
    deals = parse_deals(
        {"data": [{"BD_DT_DATE": "17-SEP-2026", "BD_SYMBOL": "ASIANPAINT",
                   "BD_CLIENT_NAME": "JALAJ A DANI", "BD_BUY_SELL": "SELL",
                   "BD_QTY_TRD": 164000, "BD_TP_WATP": 2440}]},
        "block",
    )
    assert len(deals) == 1
    assert round(deals[0].value_cr, 2) == 40.02  # 164000 × 2440 / 1e7
    assert parse_indices({"data": [{"indexSymbol": "NIFTY 50", "percentChange": 0.06}]}) == {
        "NIFTY 50": 0.06
    }


def test_a_name_with_no_disclosed_deals_scores_zero_flow_not_negative_flow():
    """'The exchange published nothing' and 'institutions sold it' must not collide."""
    m = scoring.ScoreInput()
    unknown = scoring.FlowInput(known=False)
    assert scoring.score_flow(unknown, m) == 0

    # ...and a *known* empty book is also zero, but for a different reason: net flow of 0.
    known_empty = scoring.FlowInput(buy_cr=0, sell_cr=0, conviction=0, known=True)
    assert scoring.score_flow(known_empty, m) == 0

    # A known distribution is what actually scores negative.
    selling = scoring.FlowInput(buy_cr=0, sell_cr=60, conviction=1.0, known=True)
    assert scoring.score_flow(selling, m) == -20


def test_bucket_caps_and_rotation_match_the_java_engine():
    m = scoring.ScoreInput(change_1d_pct=50, change_5d_pct=50, change_20d_pct=50,
                           weekly_52_position_pct=90)
    # 5 + 10 + 5 capped by boundedLinear, +5 for the 52w bonus = 25 = BUCKET2_CAP
    assert scoring.score_price(m) == scoring.BUCKET2_CAP

    # conviction < 0.30 halves the tier: net 60 -> tier 20 -> 10
    churn = scoring.FlowInput(buy_cr=60, sell_cr=0, conviction=0.2, known=True)
    assert scoring.score_flow(churn, scoring.ScoreInput()) == 10
    # [0.30, 0.50) takes three quarters: 20 -> 15
    part = scoring.FlowInput(buy_cr=60, sell_cr=0, conviction=0.4, known=True)
    assert scoring.score_flow(part, scoring.ScoreInput()) == 15


def test_oi_bucket_is_skipped_for_a_cash_only_name_and_when_oi_is_unknown():
    m = scoring.ScoreInput(change_5d_pct=5, fno_eligible=False)
    res = scoring.compute(m, scoring.FlowInput(known=True), scoring.OiInput(10.0, True))
    assert res.bucket3 == 0, "a cash-only name has no open interest to score"

    fno = scoring.ScoreInput(change_5d_pct=5, fno_eligible=True)
    assert scoring.score_oi(scoring.OiInput(available=False), fno, scoring.FlowInput()) == 0
    assert scoring.score_oi(scoring.OiInput(10.0, True), fno, scoring.FlowInput()) == 15


def test_a_falling_knife_cannot_be_ranked_bullish_however_good_its_momentum():
    m = scoring.ScoreInput(change_1d_pct=10, change_5d_pct=-6, change_20d_pct=10)
    res = scoring.compute(m, scoring.FlowInput(known=True), scoring.OiInput())
    assert "FALLING_KNIFE" in res.clamps
    assert res.final <= -30


def test_flow_conviction_is_one_sidedness_not_size():
    from kotsin_nse.hotstocks.nsepublic import DealRow

    one_way = [DealRow("d", "X", "FUND A", "BUY", 100, 1e5, "bulk")]
    both = [*one_way, DealRow("d", "X", "FUND B", "SELL", 100, 1e5, "bulk")]
    assert flow_for("X", one_way, deals_known=True).conviction == 1.0
    assert flow_for("X", both, deals_known=True).conviction == 0.0
    assert flow_for("X", both, deals_known=True).dominant == "ROTATION"
    assert flow_for("X", [], deals_known=False).dominant == "UNKNOWN"


def test_the_live_book_renders_a_position_not_only_an_empty_one():
    """The first version of this passed CI and 500'd the moment a trade opened.

    It read ``p.symbol`` off the ``Position`` model, where the symbol actually lives on
    ``underlying``. Every test had an empty book, so the loop body never ran. This one has a
    position in it, which is the whole point.
    """
    from kotsin_nse.hotstocks.service import HotStocksService

    class _Wallet:
        strategy, balance, deployed, realized_pnl = "FUDKII", 1_011_250.0, 0.0, 11_359.16
        trades, wins, losses = 1, 1, 0

    class _Engine:
        wallets: ClassVar[dict] = {"FUDKII": _Wallet()}

        @staticmethod
        def mode():
            from kotsin_nse.exec.gateway import Mode

            return Mode.PAPER

    svc = HotStocksService.__new__(HotStocksService)
    svc.engine = _Engine()

    view = {
        "symbol": "BLUESTARCO",
        "scrip_code": "59121",
        "strategy": "FUDKII",
        "instrument": {"name": "BLUESTARCO 29 SEP 2026 PE 1500.00"},
        "entry": 15.26,
        "option_sl": 15.7,
        "initial_option_sl": 14.0,
        "qty_remaining": 6500,
        "ltp": 16.0,
        "r_now": 0.587,
        "grade": "A",
        "direction": "BEARISH",
        "opened_ist": "2026-09-22 10:15:04",
        "option_targets": [21.89],
        "equity_entry": 1536.9,
        "equity_sl": 1541.3,
    }

    book = svc.live_book([view])
    p = book["positions"][0]
    assert p["symbol"] == "BLUESTARCO"
    assert p["contract"] == "BLUESTARCO 29 SEP 2026 PE 1500.00"
    assert p["unrealizedPct"] == 4.85  # (16.00 - 15.26) / 15.26
    assert p["slPct"] == 2.88  # the trail has moved above entry
    assert book["wallets"][0]["realizedPnl"] == 11_359.16
    assert book["mode"] == "PAPER"
