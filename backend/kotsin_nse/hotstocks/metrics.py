"""Daily bars + exchange data → one HotStocks card.

Everything here is derived, never fetched: :mod:`nsepublic` supplies delivery and deals, the
:class:`~kotsin_nse.bars.store.BarStore` supplies a year of daily bars, and this module turns the
pair into the fields a card renders and :mod:`scoring` grades.

The rule throughout is that **an unknown is ``None``, not a zero**. ``deliveryPctLatest = 0`` means
the exchange reported no delivery; ``None`` means we never saw the bhavcopy. A card that cannot
tell those apart is how a stock with no published data reads as a stock nobody wanted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import structlog

from ..bars.unified import UnifiedBar
from .nsepublic import DealRow, DeliveryRow, NsePublicData
from .scoring import FlowInput, OiInput, ScoreInput, compute

log = structlog.get_logger(__name__)

#: Sector label (``hotstocks-sectors.tsv``) → the NSE index that represents it in ``allIndices``.
#: Verified against the live index list on 2026-09-22. A sector with no index is not guessed at:
#: it maps to None and the relative-strength bucket scores its sector half as INLINE.
SECTOR_INDEX: dict[str, str | None] = {
    "Financial Services": "NIFTY FIN SERVICE",
    "Healthcare": "NIFTY HEALTHCARE",
    "Information Technology": "NIFTY IT",
    "Chemicals": "NIFTY CHEMICALS",
    "Capital Goods": "NIFTY CAPITAL MKT",
    "Oil Gas & Consumable Fuels": "NIFTY OIL AND GAS",
    "Fast Moving Consumer Goods": "NIFTY FMCG",
    "Construction Materials": "NIFTY COMMODITIES",
    "Automobile and Auto Components": "NIFTY AUTO",
    "Power": "NIFTY ENERGY",
    "Consumer Durables": "NIFTY CONSUMPTION",
    "Metals & Mining": "NIFTY METAL",
    "Realty": "NIFTY REALTY",
    "Media Entertainment & Publication": "NIFTY MEDIA",
    "Consumer Services": "NIFTY CONSUMPTION",
    "Telecommunication": None,
    "Services": "NIFTY INFRA",
    "Construction": "NIFTY INFRA",
    "Textiles": None,
}

BENCHMARK = "NIFTY 50"
#: A move this far from its benchmark is LEADING/LAGGING rather than INLINE (percentage points).
RS_BAND_PCT = 1.0
#: Delivery at or above this is treated as institutional accumulation rather than day trading.
DELIVERY_INSTITUTIONAL_PCT = 60.0


def load_sectors(path: Path) -> dict[str, str]:
    """``SYMBOL\\tSector`` per line. Missing file is not fatal — every name becomes UNKNOWN."""
    out: dict[str, str] = {}
    if not path.exists():
        log.warning("hotstocks.sectors_missing", path=str(path))
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 2 and parts[0].strip():
            out[parts[0].strip().upper()] = parts[1].strip()
    return out


def _pct(now: float, then: float) -> float:
    return 0.0 if then <= 0 else (now - then) / then * 100.0


def _sma(vals: list[float], n: int) -> float | None:
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(-period, 0):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / period, losses / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 2)


def _label(diff: float) -> str:
    if diff > RS_BAND_PCT:
        return "LEADING"
    if diff < -RS_BAND_PCT:
        return "LAGGING"
    return "INLINE"


@dataclass(slots=True)
class FlowFacts:
    buy_cr: float
    sell_cr: float
    buy_clients: list[str]
    sell_clients: list[str]
    bulk_count: int
    block_count: int
    deal_days: int
    conviction: float
    known: bool

    @property
    def dominant(self) -> str:
        if not self.known:
            return "UNKNOWN"
        if self.buy_cr > self.sell_cr * 1.2:
            return "DEAL_NET_BUY"
        if self.sell_cr > self.buy_cr * 1.2:
            return "DEAL_NET_SELL"
        if self.buy_cr > 0 or self.sell_cr > 0:
            return "ROTATION"
        return "INSUFFICIENT"


def flow_for(symbol: str, deals: list[DealRow], *, deals_known: bool) -> FlowFacts:
    rows = [d for d in deals if d.symbol.upper() == symbol.upper()]
    buy = sum(d.value_cr for d in rows if d.side == "BUY")
    sell = sum(d.value_cr for d in rows if d.side == "SELL")
    total = buy + sell
    return FlowFacts(
        buy_cr=round(buy, 2),
        sell_cr=round(sell, 2),
        buy_clients=sorted({d.client for d in rows if d.side == "BUY" and d.client}),
        sell_clients=sorted({d.client for d in rows if d.side == "SELL" and d.client}),
        bulk_count=sum(1 for d in rows if d.kind == "bulk"),
        block_count=sum(1 for d in rows if d.kind == "block"),
        deal_days=len({d.day for d in rows}),
        # One-sidedness of the flow: 1.0 = entirely one way, 0.0 = perfectly balanced churn.
        conviction=round(abs(buy - sell) / total, 2) if total > 0 else 0.0,
        known=deals_known,
    )


def build(
    *,
    symbol: str,
    scrip_code: str,
    daily: list[UnifiedBar],
    ltp: float | None,
    sector: str,
    nse: NsePublicData,
    oi_5d_pct: float | None,
    fno_eligible: bool,
    zones_entry: tuple[float, float] | None = None,
    suggested_sl: float | None = None,
) -> dict[str, Any] | None:
    """One card. ``None`` when there is not enough history to say anything honest."""
    if len(daily) < 21:
        return None

    closes = [b.close for b in daily]
    vols = [b.volume for b in daily]
    last_close = closes[-1]
    ref = ltp if ltp and ltp > 0 else last_close

    c1 = _pct(ref, closes[-2])
    c5 = _pct(ref, closes[-6]) if len(closes) >= 6 else 0.0
    c20 = _pct(ref, closes[-21]) if len(closes) >= 21 else 0.0

    sma50, sma200 = _sma(closes, 50), _sma(closes, 200)
    above50 = _pct(ref, sma50) if sma50 else None
    above200 = _pct(ref, sma200) if sma200 else None

    if above50 is None or above200 is None:
        trend = "INSUFFICIENT"
    elif above50 > 0 and above200 > 0:
        trend = "UPTREND"
    elif above50 < 0 and above200 < 0:
        trend = "DOWNTREND"
    else:
        trend = "SIDEWAYS"

    window = closes[-252:] if len(closes) >= 252 else closes
    lo52, hi52 = min(window), max(window)
    pos52 = None if hi52 <= lo52 else round((ref - lo52) / (hi52 - lo52) * 100, 1)

    avg5 = _sma(vols, 5) or 0.0
    avg20 = _sma(vols, 20) or 0.0
    vratio = round(avg5 / avg20, 2) if avg20 > 0 else 0.0
    if vratio >= 1.5:
        vregime = "ELEVATED"
    elif 0 < vratio <= 0.7:
        vregime = "DRYING_UP"
    else:
        vregime = "NORMAL"

    if trend == "UPTREND" and c20 > 0:
        regime, regime_conf = "BULLISH_TREND", min(1.0, abs(c20) / 10)
    elif trend == "DOWNTREND" and c20 < 0:
        regime, regime_conf = "BEARISH_TREND", min(1.0, abs(c20) / 10)
    else:
        regime, regime_conf = "RANGE_BOUND", 0.4

    swing = daily[-20:]
    swing_lo, swing_hi = min(b.low for b in swing), max(b.high for b in swing)

    # Relative strength, against the sector's own index and against NIFTY.
    idx_name = SECTOR_INDEX.get(sector)
    sector_pct = nse.indices.get((idx_name or "").upper()) if idx_name else None
    nifty_pct = nse.indices.get(BENCHMARK.upper())
    vs_sector = round(c1 - sector_pct, 2) if sector_pct is not None else 0.0
    vs_nifty = round(c1 - nifty_pct, 2) if nifty_pct is not None else 0.0
    sector_label = _label(vs_sector) if sector_pct is not None else "UNKNOWN"
    nifty_label = _label(vs_nifty) if nifty_pct is not None else "UNKNOWN"

    # Delivery, latest and its 5-session average, both from the published bhavcopy.
    d_latest: DeliveryRow | None = nse.delivery.get(symbol.upper())
    series = [
        rows[symbol.upper()].deliv_pct
        for _, rows in sorted(nse.delivery_by_day.items())
        if symbol.upper() in rows
    ]
    d_pct = d_latest.deliv_pct if d_latest else None
    d_avg5 = round(sum(series) / len(series), 2) if series else None
    if d_pct is not None and d_avg5 is not None and len(series) >= 2:
        d_trend = "RISING" if d_pct > d_avg5 * 1.1 else "FALLING" if d_pct < d_avg5 * 0.9 else "STABLE"
    else:
        d_trend = "UNKNOWN"
    institutional = bool(d_pct is not None and d_pct >= DELIVERY_INSTITUTIONAL_PCT)

    flow = flow_for(symbol, nse.deals, deals_known="deals" not in nse.errors)

    si = ScoreInput(
        change_1d_pct=c1,
        change_5d_pct=c5,
        change_20d_pct=c20,
        weekly_52_position_pct=pos52,
        vs_sector_label=sector_label,
        vs_nifty_label=nifty_label,
        volume_regime=vregime,
        price_regime=regime,
        delivery_pct_latest=d_pct or 0.0,
        delivery_institutional=institutional,
        smart_buy_cr=flow.buy_cr,
        smart_sell_cr=flow.sell_cr,
        smart_buy_clients=flow.buy_clients,
        smart_sell_clients=flow.sell_clients,
        fno_eligible=fno_eligible,
    )
    res = compute(
        si,
        FlowInput(flow.buy_cr, flow.sell_cr, flow.conviction, flow.known),
        OiInput(oi_5d_pct or 0.0, oi_5d_pct is not None),
    )

    turnover_cr = round(avg20 * ref / 1e7, 2)
    tier = "HIGH" if turnover_cr >= 100 else "MED" if turnover_cr >= 25 else "LOW"

    entry_lo, entry_hi = zones_entry or (round(swing_lo, 2), round(ref, 2))

    return {
        "scripCode": scrip_code,
        "symbol": symbol,
        "sector": sector or "UNKNOWN",
        "fnoEligible": fno_eligible,
        "ltpYesterday": round(last_close, 2),
        "ltp": round(ref, 2),
        "change1dPct": round(c1, 2),
        "change5dPct": round(c5, 2),
        "change20dPct": round(c20, 2),
        "vsSectorIndexPct": vs_sector,
        "vsSectorLabel": sector_label,
        "sectorIndex": idx_name,
        "sectorIndexPct": sector_pct,
        "vsNifty50Pct": vs_nifty,
        "vsNiftyLabel": nifty_label,
        "niftyPct": nifty_pct,
        "bulkDealCount": flow.bulk_count,
        "blockDealCount": flow.block_count,
        "dealDays": flow.deal_days,
        "smartBuyCr": flow.buy_cr,
        "smartSellCr": flow.sell_cr,
        "smartBuyClients": flow.buy_clients,
        "smartSellClients": flow.sell_clients,
        "dominantFlow": flow.dominant,
        "conviction": flow.conviction,
        "flowKnown": flow.known,
        "deliveryPctLatest": d_pct,
        "deliveryPctAvg5d": d_avg5,
        "deliveryTrend": d_trend,
        "deliveryInstitutional": institutional,
        "deliveryDay": nse.delivery_day or None,
        "above50dmaPct": None if above50 is None else round(above50, 2),
        "above200dmaPct": None if above200 is None else round(above200, 2),
        "trendState": trend,
        "rsi14": _rsi(closes),
        "weekly52PositionPct": pos52,
        "priceRegime": regime,
        "priceRegimeConfidence": round(regime_conf, 2),
        "volumeRatio5d20d": vratio,
        "volumeRegime": vregime,
        "avgVolume5d": round(avg5),
        "avgVolume20d": round(avg20),
        "avgTurnover20dCr": turnover_cr,
        "liquidityTier": tier,
        "swingLow20d": round(swing_lo, 2),
        "swingHigh20d": round(swing_hi, 2),
        "entryZoneLow": entry_lo,
        "entryZoneHigh": entry_hi,
        "suggestedSlPrice": suggested_sl,
        "oiChangePct5d": oi_5d_pct,
        "v2Score": res.final,
        "v2PreClampScore": res.pre_clamp,
        "v2ScoreWithoutVolume": res.without_volume,
        "v2Tier": res.tier,
        "v2DataConfidence": res.data_confidence,
        "v2Bucket1": res.bucket1,
        "v2Bucket2": res.bucket2,
        "v2Bucket3": res.bucket3,
        "v2Bucket4": res.bucket4,
        "v2Bucket5": res.bucket5,
        "v2Clamps": res.clamps,
    }


def rank(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strongest accumulation first. Ties break on turnover so an illiquid name never leads."""
    return sorted(
        cards,
        key=lambda c: (-(c["v2Score"] or 0), -(c["avgTurnover20dCr"] or 0), c["symbol"]),
    )


def today_ist() -> date:
    import time

    from ..market.session import ist_day

    return ist_day(time.time())
