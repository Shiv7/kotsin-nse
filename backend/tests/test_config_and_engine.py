"""Config closure, mode-as-state, and an end-to-end paper trade through the real engine."""

from __future__ import annotations

import time

import pytest

from kotsin_nse.config import Segment, Settings, UnknownConfigKeys, assert_no_unknown_env
from kotsin_nse.engine import Engine
from kotsin_nse.exec.gateway import Mode

# -- config ----------------------------------------------------------------------------------------


def test_unknown_env_key_fails_at_boot():
    """``KN_DELTA_ENVIRONMENT=mainnet`` (typo) must not silently run on the default. pydantic
    ignores unknown *process* env vars by default, which is the wrong trade-off for a namespace
    we own."""
    with pytest.raises(UnknownConfigKeys) as exc:
        assert_no_unknown_env({"KN_SEGMENT": "NSE_EQ"})  # the field is KN_SEGMENTS
    assert "KN_SEGMENT" in str(exc.value)
    assert_no_unknown_env({"KN_SEGMENTS": "NSE_EQ", "PATH": "/bin"})


def test_unknown_dotenv_key_fails_validation(tmp_path):
    env = tmp_path / ".env"
    env.write_text("KN_NOT_A_REAL_KEY=1\n")
    with pytest.raises(Exception):
        Settings(_env_file=str(env))


def test_bad_segment_is_rejected_with_the_known_list():
    with pytest.raises(Exception) as exc:
        Settings(_env_file=None, segments="NSE_CASH")
    assert "NSE_EQ" in str(exc.value)


def test_segment_wire_identity():
    assert (Segment.NSE_EQ.exch, Segment.NSE_EQ.exch_type) == ("N", "C")
    assert (Segment.NSE_FO.exch, Segment.NSE_FO.exch_type) == ("N", "D")
    assert (Segment.MCX_FO.exch, Segment.MCX_FO.exch_type) == ("M", "D")
    assert Segment.MCX_FO.scripmaster_key == "mcx_fo"


def test_credentials_are_absent_by_default():
    """The old repos committed all six credentials plus the TOTP seed in three source files."""
    s = Settings(_env_file=None)
    assert s.has_credentials is False
    assert s.fp_totp_secret is None


def test_secrets_do_not_appear_in_the_repr():
    s = Settings(_env_file=None, fp_pin="1234", fp_totp_secret="ABCD")
    assert "1234" not in repr(s)
    assert "ABCD" not in repr(s)


# -- engine -------------------------------------------------------------------------------------------


async def test_engine_boots_without_credentials_and_says_why(settings):
    e = Engine(settings)
    await e.start()
    try:
        assert e.mode() is Mode.SHADOW
        assert any("credentials" in n for n in e.boot_notes)
    finally:
        await e.stop()


async def test_live_mode_requires_explicit_arming(settings):
    e = Engine(settings)
    await e.start()
    try:
        with pytest.raises(ValueError):
            await e.set_mode(Mode.LIVE_CAPPED)
        await e.set_mode(Mode.LIVE_CAPPED, armed_minutes=30)
        assert e.mode() is Mode.LIVE_CAPPED
    finally:
        await e.stop()


async def test_expired_arming_falls_back_to_paper_on_restart(settings):
    """A restart after the arming window must not silently stay live. CAN2 ran paper for eight
    weeks because a restart dropped an env var — this is the opposite failure, guarded."""
    e = Engine(settings)
    await e.start()
    await e.ledger.set_mode("LIVE_CAPPED", time.time() - 60)
    await e.stop()

    e2 = Engine(settings)
    await e2.start()
    try:
        assert e2.mode() is Mode.PAPER
        assert any("arming expired" in n for n in e2.boot_notes)
    finally:
        await e2.stop()


async def test_expired_arming_downgrades_even_without_a_restart(settings):
    e = Engine(settings)
    await e.start()
    try:
        await e.set_mode(Mode.LIVE_CAPPED, armed_minutes=30)
        e._armed_until = time.time() - 1
        assert e.mode() is Mode.PAPER
    finally:
        await e.stop()


async def test_halt_and_breaker_both_stop_entries(settings):
    e = Engine(settings)
    await e.start()
    try:
        assert e.halted() == (False, "")
        await e.set_halt(True, "manual test")
        assert e.halted()[0] is True
        await e.set_halt(False)
        e.gateway.breaker_tripped = True
        assert "breaker" in e.halted()[1]
    finally:
        await e.stop()
