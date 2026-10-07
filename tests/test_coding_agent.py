"""opencode is MAC's only coding CLI: detection, knobs, route status, argv.

The router route itself (generated config, sandbox env, preflight token) is
covered by tests/test_opencode_router.py.
"""

import json

import pytest

from mac import coding_agent as ca
from mac.coding_agent import coding_agent_argv, resolve_coding_agent, route_status

_HUB = {"MAC_HUB_URL": "http://hub.example:8789", "MAC_WORKER_TOKEN": "mac_worker_secret"}


def _which(*available):
    """Fake shutil.which: resolves only the named binaries to a fake path."""
    names = set(available)
    return lambda name: ("/usr/local/bin/%s" % name) if name in names else None


def _fake_opencode(directory):
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "opencode"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return binary


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def test_missing_binary_fails_closed(tmp_path):
    choice = resolve_coding_agent(env=_HUB, home=tmp_path, which=_which())
    assert choice.available is False
    assert choice.agent == ""
    assert "opencode: not on PATH" in choice.rationale


@pytest.mark.parametrize(
    "env, reason",
    [
        ({"MAC_WORKER_TOKEN": "t"}, "no hub URL"),
        ({"MAC_HUB_URL": "http://hub.example:8789"}, "no inference token or worker token"),
    ],
)
def test_router_route_needs_a_hub_url_and_a_hub_credential(tmp_path, env, reason):
    choice = resolve_coding_agent(env=env, home=tmp_path, which=_which("opencode"))
    assert choice.available is False
    assert any(reason in line for line in choice.rationale)


def test_no_host_provider_credential_makes_the_route_available(tmp_path):
    # The old direct-provider auth file is not a route any more.
    auth = tmp_path / ".local" / "share" / "opencode"
    auth.mkdir(parents=True)
    (auth / "auth.json").write_text('{"nvidia": {"type": "api", "key": "x"}}')
    choice = resolve_coding_agent(
        env={"OPENCODE_API_KEY": "k"}, home=tmp_path, which=_which("opencode")
    )
    assert choice.available is False


def test_other_installed_clis_are_never_selected(tmp_path):
    env = {**_HUB, "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k"}
    choice = resolve_coding_agent(
        env=env, home=tmp_path, which=_which("claude", "codex", "cursor-agent", "pi")
    )
    assert choice.available is False and choice.agent == ""


def test_route_identity_is_the_hub_router(tmp_path):
    choice = resolve_coding_agent(env=_HUB, home=tmp_path, which=_which("opencode"))
    assert choice.available
    assert choice.agent == "opencode"
    assert choice.binary == "/usr/local/bin/opencode"
    assert choice.auth_source == "MAC_INFERENCE_TOKEN"
    assert choice.provider == "mac-router"
    assert choice.endpoint == "http://hub.example:8789/v1"


def test_ipv6_hub_endpoint_is_normalized(tmp_path):
    choice = resolve_coding_agent(
        env={"MAC_HUB_URL": "http://[::1]:8789/", "MAC_INFERENCE_TOKEN": "t"},
        home=tmp_path,
        which=_which("opencode"),
    )
    assert choice.endpoint == "http://[::1]:8789/v1"


def test_route_fingerprint_changes_with_endpoint_or_model(tmp_path):
    which = _which("opencode")
    base = resolve_coding_agent(env=_HUB, home=tmp_path, which=which)
    other_hub = resolve_coding_agent(
        env={**_HUB, "MAC_HUB_URL": "http://other.example:8789"}, home=tmp_path, which=which
    )
    other_model = resolve_coding_agent(
        env={**_HUB, "MAC_TASK_MODEL": "kimi-k2"}, home=tmp_path, which=which
    )
    fingerprints = {
        base.route_fingerprint(),
        other_hub.route_fingerprint(),
        other_model.route_fingerprint(),
    }
    assert len(fingerprints) == 3


def test_observable_is_secret_free(tmp_path):
    choice = resolve_coding_agent(env=_HUB, home=tmp_path, which=_which("opencode"))
    blob = json.dumps(choice.observable())
    assert "mac_worker_secret" not in blob
    assert choice.observable()["auth_source"] == "MAC_INFERENCE_TOKEN"
    assert choice.observable()["schema"] == "mac.coding_agent.choice.v2"


# --------------------------------------------------------------------------- #
# Knobs
# --------------------------------------------------------------------------- #


def test_preference_disabled_fails_closed(tmp_path):
    env = {**_HUB, "MAC_PREFER_CODING_AGENT": "0"}
    choice = resolve_coding_agent(env=env, home=tmp_path, which=_which("opencode"))
    assert choice.available is False
    assert any("MAC_PREFER_CODING_AGENT" in line for line in choice.rationale)


@pytest.mark.parametrize("value", ["off", "none", "0"])
def test_force_disable_values_turn_the_route_off(tmp_path, value):
    env = {**_HUB, "MAC_CODING_AGENT": value}
    choice = resolve_coding_agent(env=env, home=tmp_path, which=_which("opencode"))
    assert choice.available is False


def test_force_opencode_is_the_default(tmp_path):
    env = {**_HUB, "MAC_CODING_AGENT": "opencode"}
    choice = resolve_coding_agent(env=env, home=tmp_path, which=_which("opencode"))
    assert choice.agent == "opencode"


@pytest.mark.parametrize("value", ["codex", "auto"])
def test_any_other_pin_is_ignored_and_said_so(tmp_path, value):
    env = {**_HUB, "MAC_CODING_AGENT": value}
    choice = resolve_coding_agent(env=env, home=tmp_path, which=_which("opencode", value))
    assert choice.agent == "opencode"
    assert any("is not supported" in line for line in choice.rationale)


def test_rejected_verification_means_no_route(tmp_path):
    seen = []

    def _reject(choice):
        seen.append(choice.agent)
        return False

    choice = resolve_coding_agent(env=_HUB, home=tmp_path, which=_which("opencode"), accept=_reject)
    assert seen == ["opencode"]
    assert choice.available is False
    assert any("verification failed" in line for line in choice.rationale)


def test_verifier_crash_means_no_route(tmp_path):
    def _boom(choice):
        raise RuntimeError("probe exploded")

    choice = resolve_coding_agent(env=_HUB, home=tmp_path, which=_which("opencode"), accept=_boom)
    assert choice.available is False
    assert any("verifier raised RuntimeError" in line for line in choice.rationale)


def test_accepted_verification_selects_the_route(tmp_path):
    choice = resolve_coding_agent(
        env=_HUB, home=tmp_path, which=_which("opencode"), accept=lambda c: True
    )
    assert choice.available is True


# --------------------------------------------------------------------------- #
# argv
# --------------------------------------------------------------------------- #


def test_argv_uses_run_and_auto_approval(tmp_path):
    choice = resolve_coding_agent(env=_HUB, home=tmp_path, which=_which("opencode"))
    argv = coding_agent_argv(choice, "do the task", env={})
    assert argv[:3] == ["/usr/local/bin/opencode", "run", "--auto"]
    assert argv[-1] == "do the task"


def test_argv_requires_an_available_opencode_choice():
    with pytest.raises(ValueError):
        coding_agent_argv(ca.CodingAgentChoice(agent="", available=False), "p", env={})
    with pytest.raises(ValueError):
        coding_agent_argv(
            ca.CodingAgentChoice(agent="cursor", available=True, binary="cursor"), "p", env={}
        )


def test_claude_is_selected_by_name_and_routes_through_messages(tmp_path):
    env = {**_HUB, "MAC_CODING_AGENT": "claude"}
    choice = resolve_coding_agent(env=env, home=tmp_path, which=_which("opencode", "claude"))
    assert (choice.agent, choice.available) == ("claude", True)
    assert choice.protocol == "anthropic-messages"
    assert choice.endpoint.endswith("/v1/messages")
    assert choice.model == ca.DEFAULT_CLAUDE_MODEL
    # Without the binary there is no route, and nothing falls back to opencode.
    missing = resolve_coding_agent(env=env, home=tmp_path, which=_which("opencode"))
    assert missing.available is False
    assert any("claude: not on PATH" in line for line in missing.rationale)


def test_claude_model_honours_only_claude_pins():
    assert ca.claude_model({"MAC_TASK_MODEL": "gpt-5.6-sol"}) == ca.DEFAULT_CLAUDE_MODEL
    assert ca.claude_model({"MAC_TASK_MODEL": "machub/claude-sonnet-4-6"}) == "claude-sonnet-4-6"
    assert ca.claude_model({"MAC_CLAUDE_MODEL": "claude-haiku-4-5-20251001"}) == (
        "claude-haiku-4-5-20251001"
    )


def test_claude_argv_loads_only_mac_settings_and_takes_the_prompt_last():
    choice = ca.CodingAgentChoice(agent="claude", available=True, binary="/usr/local/bin/claude")
    argv = coding_agent_argv(choice, "PROMPT", env={"MAC_CLAUDE_MAX_TURNS": "50"}, session_id="s-1")
    assert argv[:2] == ["/usr/local/bin/claude", "-p"]
    assert argv[argv.index("--settings") + 1] == ca.CLAUDE_SETTINGS_FILE
    # A repository's own .claude settings and hooks never load.
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--max-turns") + 1] == "50"
    assert argv[argv.index("--session-id") + 1] == "s-1"
    assert argv[-1] == "PROMPT"
    resumed = coding_agent_argv(choice, "NEXT", env={}, session_id="s-1", resume="s-1")
    assert resumed[resumed.index("--resume") + 1] == "s-1"
    assert "--session-id" not in resumed


# --------------------------------------------------------------------------- #
# Route status (worker heartbeat)
# --------------------------------------------------------------------------- #


def _verification(choice, **overrides):
    return {
        "schema": "mac.coding_agent.verification.v1",
        "agent": "opencode",
        "route_fingerprint": choice.route_fingerprint(),
        "verified": True,
        "checked_at": "2026-07-08T00:00:00+00:00",
        **overrides,
    }


def test_route_status_requires_matching_route_verification(tmp_path):
    which = _which("opencode")
    choice = resolve_coding_agent(env=_HUB, home=tmp_path, which=which)
    verification = _verification(choice)

    matched = route_status(env=_HUB, home=tmp_path, which=which, verification=verification)
    changed = route_status(
        env={**_HUB, "MAC_HUB_URL": "http://other.example:8789"},
        home=tmp_path,
        which=which,
        verification=verification,
    )
    assert matched["verified"] is True and matched["available"] is True
    assert changed["verified"] is False and changed["available"] is False
    assert changed["configured"] is True
    assert changed["verification"] == {}


def test_route_status_available_requires_executable_proof(tmp_path):
    bin_dir = tmp_path / "bin"
    _fake_opencode(bin_dir)
    env = {**_HUB, "PATH": str(bin_dir)}
    home = tmp_path / "home"

    unproven = route_status(env=env, home=home)
    assert unproven["on_path"] is True
    assert unproven["configured"] is True
    assert unproven["available"] is False
    assert unproven["verification_status"] == "unverified"

    choice = resolve_coding_agent(env=env, home=home)
    proven = route_status(env=env, home=home, verification=_verification(choice))
    assert proven["available"] is True and proven["verified"] is True

    failed = route_status(env=env, home=home, verification=_verification(choice, verified=False))
    assert failed["available"] is False
    assert failed["configured"] is True
    assert failed["verification_status"] == "failed"


def test_route_status_prefers_matching_task_sandbox_inventory_over_host_path(tmp_path):
    choice = resolve_coding_agent(
        env=_HUB, home=tmp_path, which=lambda name: name if name == "opencode" else None
    )
    status = route_status(
        env=_HUB,
        home=tmp_path,
        which=_which(),
        host_which=_which(),
        verification=_verification(choice, binary="opencode", binary_status="present"),
    )
    assert status["on_path"] is True
    assert status["verified"] is True
    assert status["host_on_path"] is False
    assert status["route_fingerprint"] == choice.route_fingerprint()


def test_route_status_reports_missing_sandbox_binary_even_when_host_has_it(tmp_path):
    choice = resolve_coding_agent(
        env=_HUB, home=tmp_path, which=lambda name: name if name == "opencode" else None
    )
    status = route_status(
        env=_HUB,
        home=tmp_path,
        which=_which("opencode"),
        host_which=_which("opencode"),
        verification=_verification(
            choice,
            binary="opencode",
            binary_status="missing",
            verified=False,
            failure_class="agent_binary_missing",
        ),
    )
    assert status["on_path"] is False
    assert status["configured"] is False
    assert status["binary_status"] == "missing"
    assert status["host_on_path"] is True
    assert status["verification_status"] == "failed"


def test_service_path_finds_the_official_install_location(tmp_path):
    """opencode's installer writes ~/.opencode/bin, which is on no default PATH."""
    home = tmp_path / "home"
    binary = _fake_opencode(home / ".opencode" / "bin")
    env = {**_HUB, "PATH": "/usr/bin:/bin"}

    assert route_status(env=env, home=home)["on_path"] is True
    choice = resolve_coding_agent(env=env, home=home)
    assert choice.available is True
    assert choice.binary == str(binary)
