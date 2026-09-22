"""The strategy catalogue is a port plan, so its claims have to stay true."""

from __future__ import annotations

from kotsin_nse.strategy.catalog import BOOKS, BY_KEY, LIVE_KEYS
from kotsin_nse.strategy.keys import ALL_KEYS


def test_live_books_are_exactly_the_ones_this_repo_implements():
    """The catalogue may not advertise a book the engine cannot compute.

    StrategyKey is the single registry of what runs here; if a book is marked live without a key,
    the page promises state that no code produces.
    """
    assert set(LIVE_KEYS) == {k.value for k in ALL_KEYS}


def test_every_unported_book_says_what_it_still_needs():
    for b in BOOKS:
        if b.status == "not_ported":
            assert b.need, f"{b.key} is unported but names nothing it needs"
            assert b.source, f"{b.key} is unported but cites no source to port from"


def test_status_is_only_ever_one_of_two_values():
    assert {b.status for b in BOOKS} <= {"live", "not_ported"}


def test_keys_are_unique_and_addressable():
    keys = [b.key for b in BOOKS]
    assert len(keys) == len(set(keys))
    assert set(BY_KEY) == set(keys)


def test_a_book_with_no_deployed_config_does_not_pretend_to_have_one():
    """MERE lived as a backtest script; inventing parameters for it would be research, not a port."""
    mere = BY_KEY["MERE"]
    assert mere.params == {}
    assert "no deployed config" in " ".join(mere.need).lower()


def test_serialisation_carries_the_params_source_only_when_there_are_params():
    for b in BOOKS:
        row = b.to_json()
        assert bool(row["paramsSource"]) == bool(b.params)
        assert set(row) >= {"key", "label", "status", "params", "have", "need", "source"}
