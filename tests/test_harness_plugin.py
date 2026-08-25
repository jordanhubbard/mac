"""Agent Plugins installer (ADR 0023): one package, thin harness pointers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mac.harness_plugin import (
    RECEIPT_SCHEMA,
    find_skills_root,
    install,
    is_mac_source_tree,
    peer_must_not_nudge,
    status,
    uninstall,
)
from mac.mcp_server import server_command
from mac.models import ValidationError

REPO = Path(__file__).resolve().parents[1]


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    skill = root / "mac-cli"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# mac-cli\n", encoding="utf-8")
    return root


def _homes(tmp_path: Path, *names: str) -> Path:
    home = tmp_path / "home"
    mapping = {
        "claude": home / ".claude",
        "cursor": home / ".cursor",
        "codex": home / ".codex",
        "opencode": home / ".config" / "opencode",
        "pi": home / ".pi",
    }
    for name in names:
        mapping[name].mkdir(parents=True)
    return home


def test_server_command_is_the_existing_mcp_verb():
    assert server_command() == ["mac", "admin", "mcp", "serve"]


def test_peer_must_not_nudge_is_hub_only():
    assert peer_must_not_nudge() == "hub-only"


def test_mac_source_tree_is_refused(tmp_path):
    assert is_mac_source_tree(REPO)
    other = tmp_path / "other"
    other.mkdir()
    assert is_mac_source_tree(other) is False


def test_global_install_wires_detected_harnesses_only(tmp_path):
    home = _homes(tmp_path, "claude", "cursor")
    plugin = tmp_path / "plugin"
    receipt = install(
        user_home=home,
        plugin_root=plugin,
        skills_root=_skills(tmp_path),
    )

    assert receipt["schema"] == RECEIPT_SCHEMA
    assert receipt["harnesses"]["claude"] == "installed"
    assert receipt["harnesses"]["cursor"] == "installed"
    assert receipt["harnesses"]["codex"] == "skipped"
    assert (plugin / "plugin.json").is_file()
    assert json.loads((plugin / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["mac"][
        "args"
    ] == ["admin", "mcp", "serve"]
    assert (home / ".claude" / "skills" / "mac-cli").is_symlink()
    cursor_mcp = json.loads((home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert cursor_mcp["mcpServers"]["mac"]["command"] == "mac"
    assert not (home / ".codex" / "mcp.json").exists()


def test_repo_install_refuses_the_mac_tree(tmp_path):
    with pytest.raises(ValidationError, match="mac source tree"):
        install(
            scope="repo",
            repo=REPO,
            user_home=_homes(tmp_path),
            plugin_root=tmp_path / "plugin",
            skills_root=_skills(tmp_path),
        )


def test_repo_install_writes_only_repo_pointers(tmp_path):
    home = _homes(tmp_path, "cursor", "claude")
    repo = tmp_path / "app"
    repo.mkdir()
    plugin = tmp_path / "plugin"
    receipt = install(
        scope="repo",
        repo=repo,
        user_home=home,
        plugin_root=plugin,
        skills_root=_skills(tmp_path),
    )

    assert receipt["harnesses"]["cursor"] == "installed"
    assert receipt["harnesses"]["opencode"] == "installed"
    assert receipt["harnesses"]["claude"] == "skipped"
    assert (repo / ".cursor" / "mcp.json").is_file()
    assert (repo / ".agents" / "skills" / "mac-cli").is_symlink()
    assert not (home / ".cursor" / "mcp.json").exists()
    assert not (home / ".claude" / "skills" / "mac-cli").exists()


def test_uninstall_drops_mac_mcp_and_keeps_the_rest(tmp_path):
    home = _homes(tmp_path, "cursor")
    mcp = home / ".cursor" / "mcp.json"
    mcp.write_text(
        json.dumps({"mcpServers": {"other": {"command": "keep-me"}}}),
        encoding="utf-8",
    )
    plugin = tmp_path / "plugin"
    install(
        user_home=home,
        plugin_root=plugin,
        skills_root=_skills(tmp_path),
    )
    result = uninstall(user_home=home, plugin_root=plugin)

    assert result["removed"] is True
    assert not plugin.exists()
    leftover = json.loads(mcp.read_text(encoding="utf-8"))
    assert "mac" not in leftover["mcpServers"]
    assert leftover["mcpServers"]["other"]["command"] == "keep-me"


def test_status_reports_missing_and_installed(tmp_path):
    plugin = tmp_path / "plugin"
    missing = status(plugin_root=plugin)
    assert missing["installed"] is False
    install(
        user_home=_homes(tmp_path, "pi"),
        plugin_root=plugin,
        skills_root=_skills(tmp_path),
    )
    present = status(plugin_root=plugin)
    assert present["installed"] is True
    assert present["stale"] is False


def test_find_skills_root_sees_this_checkout():
    assert (find_skills_root(REPO) / "mac-cli" / "SKILL.md").is_file()
