"""pi as a coding-agent route.

pi (@earendil-works/pi-coding-agent) is the coding agent NVIDIA's own OpenShell
worker runs; its policy and provider profile live in horde-next-ops under
packages/openshell-worker/assets. Adding it here gives the fleet a second
route that authenticates from a portable credential rather than an expiring
OAuth session.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mac.cli_credentials import KNOWN_CLIS, detect_local_credentials
from mac.coding_agent import AGENT_PRIORITY, coding_agent_argv, resolve_coding_agent


def _which(found: dict):
    return lambda name: found.get(name)


def _home(tmp_path: Path, *, auth: str | None = None) -> Path:
    if auth is not None:
        d = tmp_path / ".pi" / "agent"
        d.mkdir(parents=True, exist_ok=True)
        (d / "auth.json").write_text(auth, encoding="utf-8")
    return tmp_path


def test_pi_is_a_known_route():
    assert "pi" in AGENT_PRIORITY
    assert "pi" in KNOWN_CLIS


def test_an_empty_auth_file_is_not_a_credential(tmp_path):
    """pi writes ~/.pi/agent/auth.json as `{}` the first time it runs ANYTHING.

    `pi auth check` alone creates it. So testing for the file's existence -- the
    way the claude and opencode detectors reasonably test for theirs -- would
    report every node that has ever invoked pi as configured, which is the
    absent-reads-as-present mistake this codebase keeps making.
    """
    choice = resolve_coding_agent(
        env={"MAC_CODING_AGENT": "pi"},
        home=_home(tmp_path, auth="{}"),
        which=_which({"pi": "/usr/local/bin/pi"}),
    )
    assert not choice.available
    reason = " ".join(choice.rationale)
    assert "empty" in reason and "not a credential" in reason, reason


def test_a_populated_auth_file_is_a_credential(tmp_path):
    choice = resolve_coding_agent(
        env={"MAC_CODING_AGENT": "pi"},
        home=_home(tmp_path, auth='{"anthropic": {"type": "api_key"}}'),
        which=_which({"pi": "/usr/local/bin/pi"}),
    )
    assert choice.available
    assert choice.auth_source == "~/.pi/agent/auth.json"
    assert choice.auth_kind == "api_key_file"


@pytest.mark.parametrize(
    "var", ["INFERENCE_HUB_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"]
)
def test_a_provider_key_in_the_environment_is_enough(tmp_path, var):
    choice = resolve_coding_agent(
        env={"MAC_CODING_AGENT": "pi", var: "secret"},
        home=_home(tmp_path),
        which=_which({"pi": "/usr/local/bin/pi"}),
    )
    assert choice.available
    assert choice.auth_source == var
    assert choice.auth_kind == "bearer_env"


def test_argv_is_non_interactive(tmp_path):
    """Bare `pi` opens a TUI; a task run under it would hang forever."""
    choice = resolve_coding_agent(
        env={"MAC_CODING_AGENT": "pi", "ANTHROPIC_API_KEY": "x"},
        home=_home(tmp_path),
        which=_which({"pi": "/usr/local/bin/pi"}),
    )
    argv = coding_agent_argv(choice, "do the thing", env={})
    assert argv[:4] == ["/usr/local/bin/pi", "--print", "--mode", "text"]
    assert argv[-1] == "do the thing"


def test_model_pin_passes_through_provider_qualified(tmp_path):
    """pi's --model takes "provider/id", so a MAC_TASK_MODEL pin is verbatim."""
    choice = resolve_coding_agent(
        env={"MAC_CODING_AGENT": "pi", "ANTHROPIC_API_KEY": "x"},
        home=_home(tmp_path),
        which=_which({"pi": "/usr/local/bin/pi"}),
    )
    model = "inference-hub/nvidia/nemotron-3-nano-30b-a3b"
    argv = coding_agent_argv(choice, "p", env={"MAC_TASK_MODEL": model})
    assert argv[4:6] == ["--model", model]


def test_pi_does_not_borrow_another_agents_identity(tmp_path):
    choice = resolve_coding_agent(
        env={"MAC_CODING_AGENT": "pi", "ANTHROPIC_API_KEY": "x"},
        home=_home(tmp_path),
        which=_which({"pi": "/usr/local/bin/pi"}),
    )
    assert choice.provider == "pi"
    assert choice.protocol == "pi-print"
    assert "cursor" not in (choice.endpoint or "")


def test_credential_sync_refuses_to_ship_the_empty_auth_file(tmp_path):
    """Shipping `{}` would make an unconfigured worker look provisioned."""
    home = _home(tmp_path, auth="{}")
    source = detect_local_credentials(clis=["pi"], home=home, environ={})["pi"]
    assert source.files == {}
    assert not source.present

    populated = detect_local_credentials(
        clis=["pi"], home=_home(tmp_path, auth='{"anthropic": {}}'), environ={}
    )["pi"]
    assert ".pi/agent/auth.json" in populated.files
