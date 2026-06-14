from __future__ import annotations

from mac.openshell_runtime import (
    base_agent_name,
    openshell_required_for_identity,
    openshell_required_for_local_agent,
    truthy,
)


def test_base_agent_name_normalizes_ids_hosts_and_empty_values():
    assert base_agent_name("agent_rocky") == "rocky"
    assert base_agent_name("Rocky.EXAMPLE.com") == "rocky"
    assert base_agent_name("") == ""


def test_truthy_normalizes_common_env_values():
    assert truthy(" On ") is True
    assert truthy("false") is False
    assert truthy(None) is False


def test_openshell_required_for_identity_uses_explicit_and_resources():
    assert openshell_required_for_identity(agent_id="agent_rocky") is True
    assert openshell_required_for_identity(agent_id="agent_rocky", explicit="0") is False
    assert openshell_required_for_identity(agent_id="agent_boris") is False
    assert openshell_required_for_identity(
        agent_id="agent_boris",
        resources={"openshell_required": "true"},
    ) is True
    assert openshell_required_for_identity(
        agent_id="agent_rocky",
        resources={"openshell_required": "false"},
    ) is False
    assert openshell_required_for_identity(
        agent_id="agent_boris",
        resources={"hostname": "natasha.internal"},
    ) is True


def test_openshell_required_for_local_agent_reads_env_precedence():
    env = {"MAC_AGENT_ID": "agent_rocky"}
    assert openshell_required_for_local_agent(env) is True
    env = {"MAC_AGENT_ID": "agent_rocky", "MAC_OPENSHELL_REQUIRED": "0"}
    assert openshell_required_for_local_agent(env) is False
    env = {"MAC_WORKER_AGENT_NAME": "natasha.example.com"}
    assert openshell_required_for_local_agent(env) is True
    assert openshell_required_for_local_agent({}, fallback_name="agent_rocky") is True
