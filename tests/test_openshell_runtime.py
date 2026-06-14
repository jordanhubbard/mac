from __future__ import annotations

from mac.openshell_runtime import (
    DEFAULT_REQUIRED_AGENT_NAMES,
    base_agent_name,
    openshell_required_for_identity,
    openshell_required_for_local_agent,
    truthy,
)


def test_base_agent_name_normalizes_ids_hosts_and_empty_values():
    assert base_agent_name("agent_alpha") == "alpha"
    assert base_agent_name("Alpha.EXAMPLE.com") == "alpha"
    assert base_agent_name("") == ""


def test_truthy_normalizes_common_env_values():
    assert truthy(" On ") is True
    assert truthy("false") is False
    assert truthy(None) is False


def test_default_required_set_is_empty_in_this_snapshot():
    # de-personalized: no hardcoded fleet agent names; required-ness comes from
    # MAC_OPENSHELL_REQUIRED, per-agent resources, or an explicit required set.
    assert DEFAULT_REQUIRED_AGENT_NAMES == frozenset()


def test_required_for_identity_explicit_and_resources_override():
    # explicit wins outright
    assert openshell_required_for_identity(agent_id="agent_alpha", explicit="1") is True
    assert openshell_required_for_identity(agent_id="agent_alpha", explicit="0") is False
    # resources.openshell_required wins over name matching
    assert openshell_required_for_identity(agent_id="agent_x", resources={"openshell_required": "true"}) is True
    assert openshell_required_for_identity(agent_id="agent_x", resources={"openshell_required": "false"}) is False


def test_required_for_identity_matches_against_an_explicit_required_set():
    # name matching works against a caller-supplied set (not hardcoded names)
    assert openshell_required_for_identity(agent_id="agent_alpha", required_agent_names={"alpha"}) is True
    assert openshell_required_for_identity(agent_id="agent_beta", required_agent_names={"alpha"}) is False
    # with the empty default set, an agent name alone never forces sandboxing
    assert openshell_required_for_identity(agent_id="agent_alpha") is False


def test_required_for_local_agent_reads_env_precedence():
    assert openshell_required_for_local_agent({"MAC_OPENSHELL_REQUIRED": "1"}) is True
    assert openshell_required_for_local_agent({"MAC_OPENSHELL_REQUIRED": "0"}) is False
    # no override + empty default set -> not required by name alone
    assert openshell_required_for_local_agent({"MAC_AGENT_ID": "agent_alpha"}) is False
