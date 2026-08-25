from __future__ import annotations

from pathlib import Path

from mac.openshell_runtime import (
    DEFAULT_REQUIRED_AGENT_NAMES,
    SANDBOX_BASE_PATH,
    apply_openshell_requirement,
    base_agent_name,
    openshell_required_for_identity,
    openshell_required_for_local_agent,
    truthy,
)

ROOT = Path(__file__).resolve().parents[1]
CONTAINERFILE = ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile"


def test_base_agent_name_normalizes_ids_hosts_and_empty_values():
    assert base_agent_name("agent_alpha") == "alpha"
    assert base_agent_name("Alpha.EXAMPLE.com") == "alpha"
    assert base_agent_name("") == ""


def test_truthy_normalizes_common_env_values():
    assert truthy(" On ") is True
    assert truthy("false") is False
    assert truthy(None) is False


def test_default_required_set_is_empty_no_hardcoded_fleet():
    # The fleet roster is no longer baked into source; required-ness is data-driven.
    assert DEFAULT_REQUIRED_AGENT_NAMES == frozenset()


def test_sandbox_base_path_prefers_image_runtime():
    assert SANDBOX_BASE_PATH.split(":") == [
        "/opt/mac-venv/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]


def test_mac_cli_is_linked_into_the_agent_shell_path():
    """Coding-agent shell tools retain /usr/local/bin but may replace image PATH."""
    text = CONTAINERFILE.read_text(encoding="utf-8")

    assert "ln -sfn /opt/mac-venv/bin/mac /usr/local/bin/mac" in text
    assert "command -v mac" in text
    assert "mac --version" in text


def test_required_for_identity_explicit_and_resources_override():
    # explicit wins outright
    assert openshell_required_for_identity(agent_id="agent_alpha", explicit="1") is True
    assert openshell_required_for_identity(agent_id="agent_alpha", explicit="0") is False
    # resources.openshell_required is the data-driven signal
    assert (
        openshell_required_for_identity(
            agent_id="agent_x", resources={"openshell_required": "true"}
        )
        is True
    )
    assert (
        openshell_required_for_identity(
            agent_id="agent_x", resources={"openshell_required": "false"}
        )
        is False
    )


def test_required_for_identity_matches_only_an_explicit_required_set():
    # name matching works against a caller-supplied set, not a hardcoded one
    assert (
        openshell_required_for_identity(agent_id="agent_alpha", required_agent_names={"alpha"})
        is True
    )
    assert (
        openshell_required_for_identity(agent_id="agent_beta", required_agent_names={"alpha"})
        is False
    )
    # with the empty default set, an agent name alone never forces sandboxing
    assert openshell_required_for_identity(agent_id="agent_alpha") is False


def test_required_for_local_agent_reads_env_precedence():
    assert openshell_required_for_local_agent({"MAC_OPENSHELL_REQUIRED": "1"}) is True
    assert openshell_required_for_local_agent({"MAC_OPENSHELL_REQUIRED": "0"}) is False
    # no override + empty default set -> not required by name alone
    assert openshell_required_for_local_agent({"MAC_AGENT_ID": "agent_alpha"}) is False


def test_apply_openshell_requirement_stamps_env_from_resources():
    env: dict[str, str] = {}
    assert apply_openshell_requirement({"openshell_required": True}, env) is True
    assert env["MAC_OPENSHELL_REQUIRED"] == "1"

    env = {}
    assert apply_openshell_requirement({"openshell_required": "false"}, env) is False
    assert env["MAC_OPENSHELL_REQUIRED"] == "0"


def test_apply_openshell_requirement_preserves_existing_env_override():
    env = {"MAC_OPENSHELL_REQUIRED": "1"}
    # an operator/deploy override wins; a DB "false" must not silently downgrade it
    assert apply_openshell_requirement({"openshell_required": False}, env) is None
    assert env["MAC_OPENSHELL_REQUIRED"] == "1"


def test_apply_openshell_requirement_noop_when_resource_absent():
    env: dict[str, str] = {}
    assert apply_openshell_requirement(None, env) is None
    assert apply_openshell_requirement({}, env) is None
    assert apply_openshell_requirement({"hardware": {}}, env) is None
    assert "MAC_OPENSHELL_REQUIRED" not in env


def test_apply_then_local_agent_round_trips():
    # the env this helper writes is exactly what the executor's gate reads
    env: dict[str, str] = {}
    apply_openshell_requirement({"openshell_required": True}, env)
    assert openshell_required_for_local_agent(env) is True
