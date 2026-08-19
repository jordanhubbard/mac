"""opencode as a coding-agent route.

Added on 2026-08-19, when every node reported all three existing CLIs
unavailable at once -- claude with no credential, codex denied by sandbox
policy, cursor returning resource_exhausted -- and the fleet completed one task
in twenty-four hours. opencode does not share those failure modes: it
authenticates from an on-disk credential rather than a per-node env var.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mac.cli_credentials import KNOWN_CLIS, detect_local_credentials
from mac.coding_agent import (
    AGENT_PRIORITY,
    coding_agent_argv,
    resolve_coding_agent,
)


def _which(found: dict):
    return lambda name: found.get(name)


def _home(tmp_path: Path, *, auth: bool = False, config: bool = False) -> Path:
    if auth:
        d = tmp_path / ".local" / "share" / "opencode"
        d.mkdir(parents=True, exist_ok=True)
        (d / "auth.json").write_text('{"nvidia": {"type": "api", "key": "x"}}')
    if config:
        d = tmp_path / ".config" / "opencode"
        d.mkdir(parents=True, exist_ok=True)
        (d / "opencode.json").write_text('{"model": "p/m"}')
    return tmp_path


def test_opencode_is_a_known_route():
    assert "opencode" in AGENT_PRIORITY
    assert "opencode" in KNOWN_CLIS


def test_opencode_is_first():
    """First on credential durability, not model quality.

    opencode authenticates from a portable on-disk credential that can be
    copied to a worker and does not expire. claude and codex rely on OAuth
    sessions that time out, and Anthropic offers no API-key passthrough, so a
    headless worker cannot be provisioned with them at all today.
    """
    assert AGENT_PRIORITY[0] == "opencode"
    order = list(AGENT_PRIORITY)
    for later in ("claude", "codex", "cursor"):
        assert order.index("opencode") < order.index(later)


def test_auth_json_makes_opencode_available(tmp_path):
    choice = resolve_coding_agent(
        env={"MAC_CODING_AGENT": "opencode"},
        home=_home(tmp_path, auth=True, config=True),
        which=_which({"opencode": "/usr/bin/opencode"}),
    )
    assert choice.agent == "opencode"
    assert choice.available
    assert choice.auth_source == "~/.local/share/opencode/auth.json"


def test_config_without_credentials_names_the_missing_directory(tmp_path):
    """The copy-the-wrong-directory mistake must not read as a bare failure.

    ~/.config/opencode is the directory named after the tool, so it is the one
    a person copies to a worker. The credential is in ~/.local/share/opencode.
    A node in that state has a CLI on PATH that looks configured and fails at
    the provider, so the reason has to say which directory is missing.
    """
    choice = resolve_coding_agent(
        env={"MAC_CODING_AGENT": "opencode"},
        home=_home(tmp_path, auth=False, config=True),
        which=_which({"opencode": "/usr/bin/opencode"}),
    )
    assert not choice.available
    reason = " ".join(choice.rationale) if hasattr(choice, "rationale") else ""
    assert "auth.json" in reason and "separate directory" in reason, reason


def test_argv_uses_run_not_the_bare_default(tmp_path):
    """`opencode` with no subcommand starts a TUI and would hang forever."""
    choice = resolve_coding_agent(
        env={"MAC_CODING_AGENT": "opencode"},
        home=_home(tmp_path, auth=True),
        which=_which({"opencode": "/usr/bin/opencode"}),
    )
    argv = coding_agent_argv(choice, "do the thing", env={})
    assert argv[:2] == ["/usr/bin/opencode", "run"]
    assert argv[-1] == "do the thing"


def test_model_pin_is_passed_through(tmp_path):
    choice = resolve_coding_agent(
        env={"MAC_CODING_AGENT": "opencode"},
        home=_home(tmp_path, auth=True),
        which=_which({"opencode": "/usr/bin/opencode"}),
    )
    model = "nvidia-inference/switchyard/openai/gpt-5.3-codex"
    argv = coding_agent_argv(choice, "p", env={"MAC_TASK_MODEL": model})
    assert argv[2:4] == ["--model", model]


def test_credential_sync_ships_both_trees(tmp_path):
    """A worker needs the credential AND the config, from two directories."""
    home = _home(tmp_path, auth=True, config=True)
    source = detect_local_credentials(clis=["opencode"], home=home, environ={})["opencode"]
    assert set(source.files) == {
        ".local/share/opencode/auth.json",
        ".config/opencode/opencode.json",
    }
    assert source.present


def test_credential_sync_never_ships_node_modules(tmp_path):
    """~/.config/opencode carries a 61MB node_modules tree on the workstation."""
    home = _home(tmp_path, auth=True, config=True)
    junk = home / ".config" / "opencode" / "node_modules" / "pkg"
    junk.mkdir(parents=True)
    (junk / "index.js").write_text("x" * 1000)
    source = detect_local_credentials(clis=["opencode"], home=home, environ={})["opencode"]
    assert not any("node_modules" in rel for rel in source.files)


def test_missing_binary_is_not_available(tmp_path):
    choice = resolve_coding_agent(
        env={"MAC_CODING_AGENT": "opencode"},
        home=_home(tmp_path, auth=True),
        which=_which({}),
    )
    assert not choice.available
