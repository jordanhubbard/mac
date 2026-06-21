"""Tests for coding-agent CLI preference (Claude Code / Codex / Cursor).

These verify that mac decides — from the same environment the executor runs in —
which coding-agent CLI is available and authenticated (so the work runs against
a cheaper subscription/seat instead of the metered LLM gateway), builds the
right non-interactive invocation, honors the disable/pin/override knobs, wires
the messaging MCP server only where supported, and never leaks a credential
into the legible decision.
"""

import json

from mac.coding_agent import (
    CodingAgentChoice,
    coding_agent_argv,
    mcp_config_document,
    messaging_mcp_enabled,
    resolve_coding_agent,
    supports_per_invocation_mcp,
)


def _which(*available):
    """Fake shutil.which: resolves only the named binaries to a fake path."""
    names = set(available)
    return lambda name: ("/usr/local/bin/%s" % name) if name in names else None


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def test_no_agent_on_path_falls_back_to_gateway(tmp_path):
    choice = resolve_coding_agent(env={}, home=tmp_path, which=_which())
    assert choice.available is False
    assert choice.agent == ""
    assert any("gateway" in line for line in choice.rationale)


def test_claude_via_anthropic_api_key(tmp_path):
    choice = resolve_coding_agent(
        env={"ANTHROPIC_API_KEY": "sk-secret"}, home=tmp_path, which=_which("claude")
    )
    assert choice.agent == "claude"
    assert choice.available is True
    assert choice.auth_source == "ANTHROPIC_API_KEY"
    assert choice.binary == "/usr/local/bin/claude"


def test_claude_via_claude_json_primary_key(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({"primary_key": "sk-xyz"}), encoding="utf-8")
    choice = resolve_coding_agent(env={}, home=tmp_path, which=_which("claude"))
    assert choice.agent == "claude"
    assert choice.auth_source == "~/.claude.json:primary_key"


def test_claude_on_path_but_unauthed_is_skipped(tmp_path):
    # No ANTHROPIC_API_KEY, and ~/.claude.json absent / no primary_key.
    (tmp_path / ".claude.json").write_text(json.dumps({"other": "x"}), encoding="utf-8")
    choice = resolve_coding_agent(env={}, home=tmp_path, which=_which("claude"))
    assert choice.available is False
    assert choice.agent == ""


def test_codex_via_auth_json(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-codex"}), encoding="utf-8")
    choice = resolve_coding_agent(env={}, home=tmp_path, which=_which("codex"))
    assert choice.agent == "codex"
    assert choice.auth_source == "~/.codex/auth.json"


def test_codex_empty_auth_json_is_skipped(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text("", encoding="utf-8")
    choice = resolve_coding_agent(env={}, home=tmp_path, which=_which("codex"))
    assert choice.available is False


def test_cursor_via_cursor_dir_and_cursor_agent_binary(tmp_path):
    (tmp_path / ".cursor").mkdir()
    choice = resolve_coding_agent(env={}, home=tmp_path, which=_which("cursor-agent"))
    assert choice.agent == "cursor"
    assert choice.binary == "/usr/local/bin/cursor-agent"
    assert choice.auth_source == "~/.cursor"


def test_priority_claude_beats_codex_and_cursor(tmp_path):
    # All three installed + authed; claude must win.
    (tmp_path / ".claude.json").write_text(json.dumps({"primary_key": "k"}), encoding="utf-8")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "k"}), encoding="utf-8")
    (tmp_path / ".cursor").mkdir()
    choice = resolve_coding_agent(
        env={}, home=tmp_path, which=_which("claude", "codex", "cursor-agent")
    )
    assert choice.agent == "claude"


def test_codex_chosen_when_claude_unauthed(tmp_path):
    # claude installed but unauthed; codex authed -> codex wins.
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "k"}), encoding="utf-8")
    choice = resolve_coding_agent(env={}, home=tmp_path, which=_which("claude", "codex"))
    assert choice.agent == "codex"


# --------------------------------------------------------------------------- #
# Knobs: disable / pin
# --------------------------------------------------------------------------- #


def test_preference_disabled_forces_gateway(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({"primary_key": "k"}), encoding="utf-8")
    choice = resolve_coding_agent(
        env={"MAC_PREFER_CODING_AGENT": "0"}, home=tmp_path, which=_which("claude")
    )
    assert choice.available is False
    assert any("disabled" in line for line in choice.rationale)


def test_force_off_disables(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({"primary_key": "k"}), encoding="utf-8")
    choice = resolve_coding_agent(
        env={"MAC_CODING_AGENT": "off"}, home=tmp_path, which=_which("claude")
    )
    assert choice.available is False


def test_force_pins_to_codex_even_when_claude_available(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({"primary_key": "k"}), encoding="utf-8")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "k"}), encoding="utf-8")
    choice = resolve_coding_agent(
        env={"MAC_CODING_AGENT": "codex"}, home=tmp_path, which=_which("claude", "codex")
    )
    assert choice.agent == "codex"


def test_force_pin_to_unavailable_falls_back(tmp_path):
    # Pin to cursor, but cursor isn't installed -> no fall-through to claude.
    (tmp_path / ".claude.json").write_text(json.dumps({"primary_key": "k"}), encoding="utf-8")
    choice = resolve_coding_agent(
        env={"MAC_CODING_AGENT": "cursor"}, home=tmp_path, which=_which("claude")
    )
    assert choice.available is False
    assert choice.agent == ""


# --------------------------------------------------------------------------- #
# argv construction
# --------------------------------------------------------------------------- #


def test_claude_default_argv_is_headless_and_skips_permissions():
    choice = CodingAgentChoice(agent="claude", available=True, binary="/b/claude")
    argv = coding_agent_argv(choice, "do the thing", env={})
    assert argv[0] == "/b/claude"
    assert "--dangerously-skip-permissions" in argv
    assert "-p" in argv
    assert argv[-1] == "do the thing"


def test_codex_default_argv_uses_exec_and_bypass():
    choice = CodingAgentChoice(agent="codex", available=True, binary="/b/codex")
    argv = coding_agent_argv(choice, "fix bug", env={})
    assert argv[:2] == ["/b/codex", "exec"]
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert argv[-1] == "fix bug"


def test_cursor_default_argv():
    choice = CodingAgentChoice(agent="cursor", available=True, binary="/b/cursor-agent")
    argv = coding_agent_argv(choice, "refactor", env={})
    assert argv[0] == "/b/cursor-agent"
    assert argv[-1] == "refactor"


def test_command_override_is_used_verbatim_with_prompt_appended():
    choice = CodingAgentChoice(agent="claude", available=True, binary="/b/claude")
    argv = coding_agent_argv(
        choice, "the prompt", env={"MAC_CODING_AGENT_CLAUDE_CMD": "claude --print --model opus"}
    )
    assert argv == ["claude", "--print", "--model", "opus", "the prompt"]


def test_mcp_config_injected_only_for_claude():
    claude = CodingAgentChoice(agent="claude", available=True, binary="/b/claude")
    argv = coding_agent_argv(claude, "p", env={}, mcp_config_path="/tmp/mcp.json")
    assert "--mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == "/tmp/mcp.json"
    # ...but not for codex (no clean per-invocation MCP flag).
    codex = CodingAgentChoice(agent="codex", available=True, binary="/b/codex")
    argv2 = coding_agent_argv(codex, "p", env={}, mcp_config_path="/tmp/mcp.json")
    assert "--mcp-config" not in argv2


# --------------------------------------------------------------------------- #
# MCP + observability
# --------------------------------------------------------------------------- #


def test_supports_per_invocation_mcp():
    assert supports_per_invocation_mcp("claude") is True
    assert supports_per_invocation_mcp("codex") is False
    assert supports_per_invocation_mcp("cursor") is False


def test_messaging_mcp_default_on_and_overridable():
    assert messaging_mcp_enabled({}) is True
    assert messaging_mcp_enabled({"MAC_CODING_AGENT_MESSAGING_MCP": "0"}) is False


def test_mcp_config_document_shape():
    doc = mcp_config_document(["/py", "-m", "hermes_cli.main", "mcp", "serve"], name="hermes")
    server = doc["mcpServers"]["hermes"]
    assert server["command"] == "/py"
    assert server["args"] == ["-m", "hermes_cli.main", "mcp", "serve"]


def test_observable_is_secret_free(tmp_path):
    choice = resolve_coding_agent(
        env={"ANTHROPIC_API_KEY": "sk-super-secret"}, home=tmp_path, which=_which("claude")
    )
    blob = json.dumps(choice.observable())
    assert "sk-super-secret" not in blob
    # Only the env var *name* is recorded, never the value.
    assert choice.observable()["auth_source"] == "ANTHROPIC_API_KEY"
    assert choice.observable()["schema"] == "mac.coding_agent.choice.v1"
