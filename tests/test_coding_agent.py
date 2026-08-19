"""Tests for coding-agent CLI preference (Claude Code / Codex / Cursor).

These verify that mac decides — from the same environment the executor runs in —
which coding-agent CLI is available and authenticated (so the work runs against
a cheaper subscription/seat instead of the metered LLM gateway), builds the
right non-interactive invocation, honors the disable/pin/override knobs, and
never leaks a credential into the legible decision.
"""

import json

from mac.coding_agent import (
    CodingAgentChoice,
    coding_agent_argv,
    mcp_config_document,
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


def test_no_agent_on_path_fails_closed(tmp_path):
    choice = resolve_coding_agent(env={}, home=tmp_path, which=_which())
    assert choice.available is False
    assert choice.agent == ""
    assert any("fail closed" in line for line in choice.rationale)


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


def test_verification_falls_through_failed_claude_to_codex(tmp_path):
    seen = []
    choice = resolve_coding_agent(
        env={"ANTHROPIC_API_KEY": "anthropic", "OPENAI_API_KEY": "openai"},
        home=tmp_path,
        which=_which("claude", "codex"),
        accept=lambda candidate: (
            seen.append(candidate.agent) or candidate.agent == "codex"
        ),
    )

    assert seen == ["claude", "codex"]
    assert choice.agent == "codex"
    assert choice.available is True
    assert any("claude: route verification failed" in line for line in choice.rationale)


def test_verify_all_checks_fallbacks_after_selecting_first_working_route(tmp_path):
    seen = []
    choice = resolve_coding_agent(
        env={
            "ANTHROPIC_API_KEY": "anthropic",
            "OPENAI_API_KEY": "openai",
            "CURSOR_API_KEY": "cursor",
        },
        home=tmp_path,
        which=_which("claude", "codex", "cursor-agent"),
        accept=lambda candidate: seen.append(candidate.agent) or candidate.agent != "claude",
        verify_all=True,
    )

    assert seen == ["claude", "codex", "cursor"]
    assert choice.agent == "codex"
    assert choice.available is True
    assert any(
        "codex: route verified; continuing fallback verification" in line
        for line in choice.rationale
    )


def test_cursor_auth_token_precedes_api_key_in_route_identity(tmp_path):
    choice = resolve_coding_agent(
        env={
            "CURSOR_AUTH_TOKEN": "browser-login-token",
            "CURSOR_API_KEY": "generated-api-key",
        },
        home=tmp_path,
        which=_which("cursor-agent"),
    )
    assert choice.agent == "cursor"
    assert choice.auth_source == "CURSOR_AUTH_TOKEN"
    assert choice.auth_kind == "bearer_env"


def test_verification_does_not_fall_through_explicit_agent_pin(tmp_path):
    seen = []
    choice = resolve_coding_agent(
        env={
            "MAC_CODING_AGENT": "claude",
            "ANTHROPIC_API_KEY": "anthropic",
            "OPENAI_API_KEY": "openai",
        },
        home=tmp_path,
        which=_which("claude", "codex"),
        accept=lambda candidate: seen.append(candidate.agent) or False,
    )

    assert seen == ["claude"]
    assert choice.available is False
    assert choice.agent == ""


def test_codex_chosen_when_claude_unauthed(tmp_path):
    # claude installed but unauthed; codex authed -> codex wins.
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "k"}), encoding="utf-8")
    choice = resolve_coding_agent(env={}, home=tmp_path, which=_which("claude", "codex"))
    assert choice.agent == "codex"


def test_codex_prefers_openshell_safe_environment_auth(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(
        json.dumps({"tokens": {"refresh_token": "rotating"}}), encoding="utf-8"
    )

    choice = resolve_coding_agent(
        env={"OPENAI_API_KEY": "sandbox-safe-key"},
        home=tmp_path,
        which=_which("codex"),
    )

    assert choice.agent == "codex"
    assert choice.auth_source == "OPENAI_API_KEY"
    # opencode is probed first (see AGENT_PRIORITY) and is absent in this
    # fixture, so its miss is now the first line of the rationale.
    assert choice.rationale == [
        "opencode: not on PATH",
        "claude: not on PATH",
        "codex: configured via OPENAI_API_KEY",
    ]
    assert choice.provider == "openai"
    assert choice.protocol == "responses"
    assert choice.auth_kind == "bearer_env"


def test_codex_mac_router_route_is_explicit_and_secret_free(tmp_path):
    choice = resolve_coding_agent(
        env={
            "OPENAI_API_KEY": "mac-secret-bearer",
            "OPENAI_BASE_URL": "http://user:password@127.0.0.1:8789/v1?token=leak",
        },
        home=tmp_path,
        which=_which("codex"),
    )

    assert choice.provider == "mac-router"
    assert choice.protocol == "responses"
    assert choice.endpoint == "http://127.0.0.1:8789/v1"
    assert choice.model == "*"
    observable = json.dumps(choice.observable())
    assert "mac-secret-bearer" not in observable
    assert "password" not in observable
    assert "token=leak" not in observable


def test_codex_mac_router_explicit_model_overrides_router_wildcard(tmp_path):
    choice = resolve_coding_agent(
        env={
            "OPENAI_API_KEY": "mac-secret-bearer",
            "OPENAI_BASE_URL": "http://127.0.0.1:8789/v1",
            "MAC_CODEX_MODEL": "operator-pinned-model",
        },
        home=tmp_path,
        which=_which("codex"),
    )

    assert choice.provider == "mac-router"
    assert choice.model == "operator-pinned-model"


def test_invalid_or_ipv6_endpoint_is_safely_normalized(tmp_path):
    malformed = resolve_coding_agent(
        env={"OPENAI_API_KEY": "secret", "OPENAI_BASE_URL": "http://host:bad/v1"},
        home=tmp_path,
        which=_which("codex"),
    )
    ipv6 = resolve_coding_agent(
        env={"OPENAI_API_KEY": "secret", "OPENAI_BASE_URL": "http://[::1]:8789/v1"},
        home=tmp_path,
        which=_which("codex"),
    )

    assert malformed.endpoint == "https://api.openai.com/v1"
    assert ipv6.endpoint == "http://[::1]:8789/v1"


def test_task_model_overrides_deployed_default_in_route_and_argv(tmp_path):
    env = {
        "OPENAI_API_KEY": "secret",
        "MAC_CODEX_MODEL": "fleet-default",
        "MAC_TASK_MODEL": "task-pinned",
    }
    choice = resolve_coding_agent(env=env, home=tmp_path, which=_which("codex"))
    argv = coding_agent_argv(choice, "probe", env={})

    assert choice.model == "task-pinned"
    assert argv[argv.index("--model") + 1] == "task-pinned"


def test_codex_nonstandard_env_auth_uses_custom_provider_even_at_openai(tmp_path):
    choice = resolve_coding_agent(
        env={"MAC_CODEX_TOKEN": "secret"},
        home=tmp_path,
        which=_which("codex"),
    )
    argv = coding_agent_argv(choice, "probe", env={})
    joined = " ".join(argv)

    assert 'model_provider="mac-openai"' in joined
    assert 'model_providers.mac-openai.env_key="MAC_CODEX_TOKEN"' in joined
    assert "secret" not in joined


def test_route_fingerprint_changes_with_protocol_or_endpoint(tmp_path):
    base = {"OPENAI_API_KEY": "same-secret"}
    responses = resolve_coding_agent(
        env={**base, "OPENAI_BASE_URL": "https://proxy-a.example/v1"},
        home=tmp_path,
        which=_which("codex"),
    )
    chat = resolve_coding_agent(
        env={
            **base,
            "OPENAI_BASE_URL": "https://proxy-a.example/v1",
            "MAC_CODEX_WIRE_API": "chat",
        },
        home=tmp_path,
        which=_which("codex"),
    )
    other_endpoint = resolve_coding_agent(
        env={**base, "OPENAI_BASE_URL": "https://proxy-b.example/v1"},
        home=tmp_path,
        which=_which("codex"),
    )

    assert len({responses.route_fingerprint(), chat.route_fingerprint(), other_endpoint.route_fingerprint()}) == 3


def test_detect_all_requires_matching_route_verification(tmp_path):
    from mac.coding_agent import detect_all

    env = {"OPENAI_API_KEY": "secret", "OPENAI_BASE_URL": "https://proxy.example/v1"}
    choice = resolve_coding_agent(env=env, home=tmp_path, which=_which("codex"))
    verification = {
        "codex": {
            "schema": "mac.coding_agent.verification.v1",
            "agent": "codex",
            "route_fingerprint": choice.route_fingerprint(),
            "verified": True,
            "checked_at": "2026-07-08T00:00:00+00:00",
        }
    }

    matched = detect_all(env=env, home=tmp_path, which=_which("codex"), verification=verification)
    changed = detect_all(
        env={**env, "OPENAI_BASE_URL": "https://other.example/v1"},
        home=tmp_path,
        which=_which("codex"),
        verification=verification,
    )

    assert matched["codex"]["verified"] is True
    assert changed["codex"]["verified"] is False
    assert changed["codex"]["verification"] == {}
    # available tracks the executable proof, never inventory: a matching probe
    # makes it True; a non-matching (stale-route) probe leaves it False even
    # though the CLI is still installed + credentialed.
    assert matched["codex"]["available"] is True
    assert matched["codex"]["configured"] is True
    assert changed["codex"]["available"] is False
    assert changed["codex"]["configured"] is True


def test_detect_all_available_requires_same_environment_executable_proof(tmp_path):
    """available/verified must be False for an on-PATH, credentialed CLI until a
    matching same-environment probe proves it launches; configured/on_path stay
    True to preserve the inventory signal."""
    from mac.coding_agent import detect_all

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_codex = bin_dir / "codex"
    fake_codex.write_text("#!/bin/sh\nexit 0\n")
    fake_codex.chmod(0o755)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"t": 1}')
    env = {"PATH": str(bin_dir)}

    # No verification supplied -> inventory only, never advertised available.
    unproven = detect_all(env=env, home=home)
    assert unproven["codex"]["on_path"] is True
    assert unproven["codex"]["configured"] is True
    assert unproven["codex"]["available"] is False
    assert unproven["codex"]["verified"] is False
    assert unproven["codex"]["verification_status"] == "unverified"

    # A matching, successful same-environment probe flips available -> True.
    choice = resolve_coding_agent(env=env, home=home)
    verification = {
        "codex": {
            "schema": "mac.coding_agent.verification.v1",
            "agent": "codex",
            "route_fingerprint": choice.route_fingerprint(),
            "verified": True,
            "checked_at": "2026-07-08T00:00:00+00:00",
        }
    }
    proven = detect_all(env=env, home=home, verification=verification)
    assert proven["codex"]["available"] is True
    assert proven["codex"]["verified"] is True
    assert proven["codex"]["configured"] is True

    # A failed probe (verified False) keeps available False while configured
    # remains True.
    failed = detect_all(
        env=env,
        home=home,
        verification={"codex": {**verification["codex"], "verified": False}},
    )
    assert failed["codex"]["available"] is False
    assert failed["codex"]["verified"] is False
    assert failed["codex"]["configured"] is True
    assert failed["codex"]["verification_status"] == "failed"


def test_detect_all_prefers_matching_task_sandbox_inventory_over_host_path(tmp_path):
    from mac.coding_agent import detect_all

    env = {"CURSOR_API_KEY": "cursor-secret"}
    choice = resolve_coding_agent(
        env=env,
        home=tmp_path,
        which=lambda name: name if name == "cursor-agent" else None,
    )
    verification = {
        "cursor": {
            "schema": "mac.coding_agent.verification.v1",
            "agent": "cursor",
            "binary": "cursor-agent",
            "binary_status": "present",
            "route_fingerprint": choice.route_fingerprint(),
            "verified": True,
            "checked_at": "2026-07-28T00:00:00+00:00",
        }
    }

    status = detect_all(
        env=env,
        home=tmp_path,
        which=_which(),
        host_which=_which(),
        verification=verification,
    )["cursor"]

    assert status["on_path"] is True
    assert status["configured"] is True
    assert status["verified"] is True
    assert status["binary_status"] == "present"
    assert status["host_on_path"] is False
    assert status["route_fingerprint"] == choice.route_fingerprint()


def test_detect_all_reports_missing_sandbox_binary_even_when_host_has_it(tmp_path):
    from mac.coding_agent import detect_all

    env = {"CURSOR_API_KEY": "cursor-secret"}
    choice = resolve_coding_agent(
        env=env,
        home=tmp_path,
        which=lambda name: name if name == "cursor-agent" else None,
    )
    verification = {
        "cursor": {
            "schema": "mac.coding_agent.verification.v1",
            "agent": "cursor",
            "binary": "cursor-agent",
            "binary_status": "missing",
            "route_fingerprint": choice.route_fingerprint(),
            "verified": False,
            "checked_at": "2026-07-28T00:00:00+00:00",
            "failure_class": "agent_binary_missing",
        }
    }

    status = detect_all(
        env=env,
        home=tmp_path,
        which=_which("cursor-agent"),
        host_which=_which("cursor-agent"),
        verification=verification,
    )["cursor"]

    assert status["on_path"] is False
    assert status["configured"] is False
    assert status["verified"] is False
    assert status["binary_status"] == "missing"
    assert status["host_on_path"] is True
    assert status["verification_status"] == "failed"


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
    assert "--skip-git-repo-check" in argv
    assert argv[-1] == "fix bug"


def test_codex_custom_route_is_rendered_without_credential_value():
    choice = CodingAgentChoice(
        agent="codex",
        available=True,
        binary="/b/codex",
        auth_source="MAC_CODEX_TOKEN",
        provider="mac-router",
        protocol="responses",
        auth_kind="bearer_env",
        endpoint="https://hub.example/v1",
    )
    argv = coding_agent_argv(
        choice,
        "fix bug",
        env={"MAC_CODEX_TOKEN": "super-secret"},
    )

    joined = " ".join(argv)
    assert argv[0] == "/b/codex"
    assert "model_provider" in joined
    assert "mac-router" in joined
    assert "wire_api" in joined and "responses" in joined
    assert "MAC_CODEX_TOKEN" in joined
    assert "env_http_headers" in joined
    assert "X-MAC-Task-ID" in joined and "MAC_TASK_ID" in joined
    assert "X-MAC-Agent-ID" in joined and "MAC_AGENT_ID" in joined
    assert "X-MAC-Lease-ID" in joined and "MAC_LEASE_ID" in joined
    assert "super-secret" not in joined
    assert "exec" in argv


def test_codex_non_mac_custom_route_does_not_receive_mac_context_headers():
    choice = CodingAgentChoice(
        agent="codex",
        available=True,
        binary="/b/codex",
        auth_source="EXAMPLE_TOKEN",
        provider="example",
        protocol="responses",
        auth_kind="bearer_env",
        endpoint="https://provider.example/v1",
    )

    joined = " ".join(coding_agent_argv(choice, "fix bug", env={}))

    assert "model_provider" in joined and "example" in joined
    assert "env_http_headers" not in joined
    assert "X-MAC-Task-ID" not in joined


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
    assert choice.observable()["schema"] == "mac.coding_agent.choice.v2"


def test_detect_all_default_which_finds_binaries_on_path(tmp_path):
    """Regression: detect_all() with no explicit `which` must actually probe
    PATH (v1 passed which=None into the detectors, the None(name) call raised,
    was swallowed, and every CLI reported 'not on PATH' fleet-wide)."""
    from mac.coding_agent import detect_all

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_codex = bin_dir / "codex"
    fake_codex.write_text("#!/bin/sh\nexit 0\n")
    fake_codex.chmod(0o755)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"t": 1}')

    status = detect_all(env={"PATH": str(bin_dir)}, home=home)
    assert status["codex"]["on_path"] is True
    # Inventory (installed + credentialed) is reported via configured; but with
    # no same-environment executable proof supplied, available/verified stay
    # False so a host-only binary is never advertised as runnable.
    assert status["codex"]["configured"] is True
    assert status["codex"]["available"] is False
    assert status["codex"]["verified"] is False
    # claude may exist on the host PATH the service-augmented lookup scans, but
    # this temp home carries no claude credential, so it can never be configured
    # or advertised available regardless of on_path.
    assert status["claude"]["configured"] is False
    assert status["claude"]["available"] is False


def test_detect_all_augments_service_path_with_user_bins(tmp_path):
    """A minimal supervisor PATH still finds CLIs in ~/.local/bin."""
    from mac.coding_agent import detect_all

    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    fake_claude = local_bin / "claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n")
    fake_claude.chmod(0o755)

    status = detect_all(env={"PATH": "/usr/bin:/bin"}, home=home)
    assert status["claude"]["on_path"] is True


def test_resolver_augments_service_path_like_inventory(tmp_path):
    """Runner selection must see a CLI that heartbeat inventory advertises."""
    from mac.coding_agent import detect_all

    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    fake_codex = local_bin / "codex"
    fake_codex.write_text("#!/bin/sh\nexit 0\n")
    fake_codex.chmod(0o755)
    env = {
        "PATH": "/usr/bin:/bin",
        "OPENAI_API_KEY": "test-only",
        "OPENAI_BASE_URL": "http://hub.example:8789/v1",
        "MAC_OPENAI_PROTOCOL": "responses",
    }

    status = detect_all(env=env, home=home)
    choice = resolve_coding_agent(env=env, home=home)

    # Inventory sees the CLI (configured); executable-proof (available) requires
    # a matching verification report, absent here. Resolver selection is
    # inventory-based and still picks codex.
    assert status["codex"]["configured"] is True
    assert status["codex"]["available"] is False
    assert choice.available is True
    assert choice.agent == "codex"
    assert choice.binary == str(fake_codex)
