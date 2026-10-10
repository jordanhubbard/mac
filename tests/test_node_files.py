"""mac.node_files: the one installer of a node's managed files (task_8955351b)."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

import pytest

from mac import node_files
from mac.node_files import NodeFilesError


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    for src_rel, _, _ in node_files.MANAGED["hub"]:
        path = root / src_rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("v1 %s\n" % src_rel)
    return root


def _modes(home: Path) -> dict:
    return {p.name: p.stat().st_mode & 0o777 for p in (home / "bin").iterdir()}


def test_install_is_atomic_moded_and_then_a_no_op(source, tmp_path):
    home = tmp_path / "home"
    first = node_files.apply(source, home, "worker")
    assert set(first["changed"]) == {dest for _, dest, _ in node_files.MANAGED["worker"]}
    assert _modes(home) == {
        "mac-agent-service": 0o700,
        "mac-agent-startup-self-test": 0o700,
        "mac-task-executor": 0o700,
        "mac-task-executor.py": 0o600,
        "mac-crash-observer": 0o755,
    }
    second = node_files.apply(source, home, "worker")
    assert second["changed"] == []
    assert set(second["actions"].values()) == {"unchanged"}


def test_a_changed_source_or_mode_is_updated(source, tmp_path):
    home = tmp_path / "home"
    node_files.apply(source, home, "worker")
    (source / "deploy/bin/mac-agent-service").write_text("v2\n")
    (home / "bin/mac-task-executor").chmod(0o755)
    result = node_files.apply(source, home, "worker")
    assert result["actions"]["bin/mac-agent-service"] == "updated"
    assert result["actions"]["bin/mac-task-executor"] == "updated"
    assert (home / "bin/mac-agent-service").read_text() == "v2\n"
    assert (home / "bin/mac-task-executor").stat().st_mode & 0o777 == 0o700


def test_files_no_longer_declared_are_removed_with_a_backup(source, tmp_path, monkeypatch):
    home = tmp_path / "home"
    node_files.apply(source, home, "hub")
    hub_only = "bin/mac-service"
    # The hub stops declaring mac-service: it was ours, so it goes.
    monkeypatch.setitem(node_files.MANAGED, "hub", node_files.MANAGED["worker"])
    result = node_files.apply(source, home, "hub")
    assert result["actions"][hub_only] == "removed"
    assert not (home / hub_only).exists()
    assert (Path(result["backup"]) / hub_only).read_text() == "v1 deploy/bin/mac-service\n"
    state = json.loads((home / "managed-files.json").read_text())
    assert hub_only not in state["files"]
    # And a second run does nothing.
    assert node_files.apply(source, home, "hub")["changed"] == []


def test_a_hand_edited_retired_file_is_kept_and_reported(source, tmp_path, monkeypatch):
    home = tmp_path / "home"
    node_files.apply(source, home, "hub")
    (home / "bin/mac-service").write_text("local fix\n")
    monkeypatch.setitem(node_files.MANAGED, "hub", node_files.MANAGED["worker"])
    result = node_files.apply(source, home, "hub")
    assert result["actions"]["bin/mac-service"] == "kept_modified"
    assert (home / "bin/mac-service").read_text() == "local fix\n"


def test_unrecorded_files_are_never_touched(source, tmp_path):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    (home / "bin/opencode").write_text("someone else's\n")
    node_files.apply(source, home, "worker")
    assert (home / "bin/opencode").read_text() == "someone else's\n"


def test_plan_changes_nothing_and_a_missing_source_file_fails(source, tmp_path):
    home = tmp_path / "home"
    planned = node_files.plan(source, home, "worker")
    assert set(planned["actions"].values()) == {"install"}
    assert not home.exists()
    (source / "deploy/bin/mac-task-executor").unlink()
    with pytest.raises(NodeFilesError, match="has no deploy/bin/mac-task-executor"):
        node_files.apply(source, home, "worker")


def test_one_installer_per_host(source, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    with open(home / node_files.LOCK_NAME, "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        with pytest.raises(NodeFilesError, match="another installer"):
            node_files.apply(source, home, "worker")


def test_cli_prints_one_json_line(source, tmp_path, capsys):
    home = tmp_path / "home"
    assert node_files.main(["--source", str(source), "--role", "worker", "--home", str(home)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["role"] == "worker"
    assert node_files.main(["--source", str(tmp_path / "nope"), "--role", "worker", "--home", str(home)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "error"
