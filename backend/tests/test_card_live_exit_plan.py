"""The card's exit plan follows the LIVE position (operator, 2026-09-29: "does the SL get updated
live?"): the stop as it stands now, the rungs already taken, the lots left — the ledger row only
changes at a fill, a target placed or cancelled, or an exit."""

from __future__ import annotations

from datetime import date

import pytest

from kotsin_nse.domain import Direction
from kotsin_nse.engine import IN_TREND_BOOKS

from .test_wide_stop_shadow import _rt_y_trigger


@pytest.mark.asyncio
async def test_an_open_positions_exit_plan_shows_the_stop_as_it_stands_now(settings):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
        y = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y")
        card = next(c for c in (await e.book_cards("FUDKII_RT_Y", date.today()))["cards"] if c["signalId"] == sig.signal_id)
        before = card["exitPlan"]["rows"]
        assert any(r["kind"] == "stop" and r["at"].startswith(f"option stop {y.option_sl:.2f}") for r in before)

        lot = y.instrument.lot_size
        y.option_targets = (21.0, 23.0, 25.0)  # the option's own ladder (the test's contract has none of its own)
        y.targets_hit, y.qty_remaining, y.ratchet_sl = 1, y.qty - lot, y.entry  # T1 taken: a lot out, the stop at breakeven
        card = next(c for c in (await e.book_cards("FUDKII_RT_Y", date.today()))["cards"] if c["signalId"] == sig.signal_id)
        rows = card["exitPlan"]["rows"]
        assert rows[0]["action"] == "taken" and rows[0]["at"] == "T1 21.00 ✓", "T1 already taken"
        assert [r["action"] for r in rows[1:3]] == ["1 lot", "the rest"] and rows[2]["qty"] == y.qty - 2 * lot
        stop = next(r for r in rows if r["kind"] == "stop")
        assert stop["at"].startswith(f"stop now {max(y.option_sl, y.entry):.2f}") and stop["qty"] == y.qty - lot, "the live stop, on the lots left"
    finally:
        await e.stop()
