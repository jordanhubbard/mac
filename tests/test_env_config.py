"""Tests for the typed MAC_* config accessors (env-config-registry foundation)."""

from __future__ import annotations

from mac.env_config import (
    env_bool,
    env_int,
    env_str,
    resolve_env_chain,
    resolve_hub_agent,
)


def test_env_str():
    e = {"A": "  hi  ", "B": "  "}
    assert env_str("A", environ=e) == "hi"
    assert env_str("B", "def", environ=e) == "def"
    assert env_str("MISSING", "def", environ=e) == "def"


def test_env_bool():
    for token in ("1", "true", "TRUE", "yes", "on"):
        assert env_bool("F", environ={"F": token}) is True
    for token in ("0", "false", "no", "off"):
        assert env_bool("F", True, environ={"F": token}) is False
    assert env_bool("F", True, environ={}) is True          # unset -> default
    assert env_bool("F", True, environ={"F": "garbage"}) is True  # invalid -> default


def test_env_int_and_clamp():
    assert env_int("N", 5, environ={}) == 5
    assert env_int("N", 5, environ={"N": "12"}) == 12
    assert env_int("N", 5, environ={"N": "nope"}) == 5
    assert env_int("N", 5, minimum=0, maximum=10, environ={"N": "99"}) == 10
    assert env_int("N", 5, minimum=3, environ={"N": "1"}) == 3


def test_resolve_env_chain_priority():
    e = {"SECOND": "b", "THIRD": "c"}
    assert resolve_env_chain("FIRST", "SECOND", "THIRD", environ=e) == "b"
    assert resolve_env_chain("X", "Y", default="fallback", environ=e) == "fallback"
    # blank values are skipped
    assert resolve_env_chain("BLANK", "SECOND", environ={"BLANK": "  ", "SECOND": "b"}) == "b"


def test_resolve_hub_agent_ignores_removed_beads_var():
    # The removed beads subsystem's var must NOT resolve a hub agent.
    e = {"MAC_BEADS_BRIDGE_HUB_AGENT": "old-beads-agent"}
    assert resolve_hub_agent("MAC_SHARED_SERVICES_MANAGER_AGENT", environ=e) == ""
    assert resolve_hub_agent("MAC_REVIEW_TICK_HUB_AGENT", environ=e) == ""
    # A current var resolves normally.
    e2 = {"MAC_REVIEW_TICK_HUB_AGENT": "rocky"}
    assert resolve_hub_agent("MAC_REVIEW_TICK_HUB_AGENT", environ=e2) == "rocky"
