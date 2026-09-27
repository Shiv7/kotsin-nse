"""The charges file (operator, 2026-09-26): "create a separate area where all charges are parked,
that can be changed anytime since the charges change often" — per product, on Zerodha's table."""

from __future__ import annotations

import os
import time

import pytest

from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Instrument, InstrumentKind, OptionType, OrderSide
from kotsin_nse.risk.charge_rates import ChargeRates
from kotsin_nse.risk.costs import CostModel

OPT = Instrument("1", "X", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=500, strike=100.0, option_type=OptionType.CE, underlying="X")
FUT = Instrument("2", "X", Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=500, underlying="X")
MCX_OPT = Instrument("3", "GOLDM", Segment.MCX_FO, InstrumentKind.OPTION, lot_size=10, strike=100.0, option_type=OptionType.CE, underlying="GOLDM")


def _bump(path, text):
    path.write_text(text, encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))  # a distinct mtime, whatever the filesystem's resolution


def _costs(tmp_path) -> tuple[CostModel, object]:
    s = Settings(_env_file=None, data_dir=tmp_path)
    costs = CostModel(s)
    costs.rates = ChargeRates(s, path=tmp_path / "charges.toml")
    return costs, tmp_path / "charges.toml"


def test_first_boot_writes_every_product_with_zerodhas_rates(tmp_path):
    costs, path = _costs(tmp_path)
    text = path.read_text()
    for section in ("[fo_options]", "[fo_futures]", "[equity_intraday]", "[equity_delivery]", "[commodity_options]", "[currency_options]"):
        assert section in text
    ch = costs.leg(OPT, OrderSide.SELL, 20.0, 2000)          # ₹40,000 of premium
    assert ch.brokerage == 20.0 and ch.stt == pytest.approx(60.0) and ch.exchange == pytest.approx(14.212)
    assert costs.leg(OPT, OrderSide.BUY, 20.0, 2000).stamp == pytest.approx(1.2)
    assert costs.leg(FUT, OrderSide.SELL, 1500.0, 500).stt == pytest.approx(750000 * 0.05 / 100)
    assert costs.leg(MCX_OPT, OrderSide.SELL, 100.0, 10).exchange == pytest.approx(1000 * 0.0418 / 100), "MCX options on their own line"


def test_an_edit_applies_without_a_restart_and_the_lot_basis_multiplies_by_lots(tmp_path):
    costs, path = _costs(tmp_path)
    t = path.read_text()
    start = t.index("[fo_options]")
    _bump(path, t[:start] + t[start:].replace("brokerage_flat_inr = 20", "brokerage_flat_inr = 15", 1))
    costs.rates._checked = 0.0  # skip the once-a-second throttle
    assert costs.leg(OPT, OrderSide.BUY, 20.0, 2000).brokerage == 15.0, "an edit applies without a restart"
    assert costs.leg(FUT, OrderSide.BUY, 1500.0, 500).brokerage == 20.0, "only the product edited"
    _bump(path, path.read_text().replace('basis = "order"', 'basis = "lot"'))
    costs.rates._checked = 0.0
    assert costs.leg(OPT, OrderSide.BUY, 20.0, 2000).brokerage == 60.0, "4 lots x ₹15 on the lot basis"


def test_a_bad_edit_keeps_the_last_good_rates_and_says_why(tmp_path):
    costs, path = _costs(tmp_path)
    good = path.read_text()
    _bump(path, good.replace("gst_pct = 18", "gst_pct = eighteen"))
    costs.rates._checked = 0.0
    assert costs.rates.current()["gst_pct"] == 18.0 and costs.rates.status()["error"]
    _bump(path, good.replace('basis = "order"', 'basis = "unit"'))
    costs.rates._checked = 0.0
    assert costs.rates.current()["basis"] == "order" and "basis" in costs.rates.status()["error"]


def test_the_old_single_rate_file_is_kept_and_replaced(tmp_path):
    old = tmp_path / "charges.toml"
    old.write_text("[brokerage]\nper_order_inr = 40\n[stt]\nsell_option_premium_pct = 0.0625\n", encoding="utf-8")
    rates = ChargeRates(Settings(_env_file=None, data_dir=tmp_path), path=old)
    assert "[fo_options]" in old.read_text() and rates.migrated_from and "per_order_inr = 40" in open(rates.migrated_from).read()


def test_a_cost_stress_multiplies_the_brokerage_in_force(tmp_path):
    s = Settings(_env_file=None, data_dir=tmp_path)
    ChargeRates(s, path=tmp_path / "charges.toml")
    stressed = ChargeRates(s, path=tmp_path / "charges.toml", overrides={"brokerage_mult": 2.0})
    assert stressed.product("fo_options")["brokerage_flat_inr"] == 40.0
    assert "brokerage_flat_inr = 20" in (tmp_path / "charges.toml").read_text(), "a stress never writes the file"


def test_the_suite_never_touches_the_operators_file(tmp_path):
    """No explicit path → no file: deploy.sh runs these tests inside the live folder."""
    CostModel(Settings(_env_file=None, data_dir=tmp_path))
    assert not (tmp_path / "charges.toml").exists()


@pytest.mark.asyncio
async def test_the_charges_page_shows_every_product_and_worked_round_trips(settings):
    import httpx

    from kotsin_nse.api.routes import build_app
    from kotsin_nse.engine import Engine

    engine = Engine(settings)
    engine.costs.rates = ChargeRates(settings, path=settings.data_dir / "charges.toml")
    app = build_app(engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        page = await c.get("/charges")
        j = (await c.get("/api/charges")).json()
    assert page.status_code == 200 and "<h1>Charges</h1>" in page.text and "F&amp;O options (NSE)" in page.text
    assert "per executed order" in page.text and "zerodha.com/charges" in page.text
    assert {p["product"] for p in j["products"]} >= {"fo_options", "fo_futures", "equity_intraday", "commodity_options"}
    opt = next(e for e in j["examples"] if e["product"] == "fo_options")
    assert opt["charges"]["brokerage"] == 40.0 and opt["charges"]["stt"] == pytest.approx(60.0)
