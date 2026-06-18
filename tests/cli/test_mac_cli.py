"""Tests for the `mac` CLI (mac.cli:main).

Each test exercises a CLI subcommand end-to-end against a real in-file
SQLite database (tmp_path), confirming the command emits valid JSON and
round-trips through the same ControlPlane layer the HTTP API uses.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys

import pytest

from mac.cli import main


def _run(tmp_path, *args):
    """Run `mac --db <tmp> <args>` and return (rc, parsed_output)."""
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", str(tmp_path / "mac.db"), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_mac_cli_init_creates_db(tmp_path):
    rc, result = _run(tmp_path, "init")
    assert rc == 0
    assert result["status"] == "initialized"


def test_print_serializes_list_of_dictish():
    # Regression: hub-mode list commands (e.g. `mac project list`) return
    # list[_Dictish]; _print only unwrapped a single top-level to_dict object,
    # so a list crashed with "Object of type _Dictish is not JSON serializable".
    from mac.cli import _print
    from mac.dispatch import _Dictish

    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        _print([_Dictish({"project": "ova"}), _Dictish({"project": "widget"})])
    finally:
        sys.stdout = old
    assert json.loads(out.getvalue()) == [{"project": "ova"}, {"project": "widget"}]


# ---------------------------------------------------------------------------
# tenant
# ---------------------------------------------------------------------------


def test_mac_cli_tenant_register_and_list(tmp_path):
    # name is a positional arg
    rc, tenant = _run(tmp_path, "tenant", "register", "acme")
    assert rc == 0
    assert tenant["name"] == "acme"
    assert tenant["id"].startswith("tenant_")

    rc, tenants = _run(tmp_path, "tenant", "list")
    assert rc == 0
    assert any(t["id"] == tenant["id"] for t in tenants)


# ---------------------------------------------------------------------------
# machine + agent
# ---------------------------------------------------------------------------


def test_mac_cli_machine_register(tmp_path):
    # hostname is a positional arg
    rc, machine = _run(tmp_path, "machine", "register", "host-1")
    assert rc == 0
    assert machine["hostname"] == "host-1"


def test_mac_cli_agent_register_and_list(tmp_path):
    rc, machine = _run(tmp_path, "machine", "register", "host-1")
    assert rc == 0

    # machine_id and name are positional; capabilities is a flag
    rc, agent = _run(
        tmp_path,
        "agent",
        "register",
        machine["id"],
        "worker-1",
        "--capabilities",
        "python,deploy",
    )
    assert rc == 0
    assert agent["name"] == "worker-1"
    assert "python" in agent["capabilities"]

    rc, agents = _run(tmp_path, "agent", "list")
    assert rc == 0
    assert any(a["id"] == agent["id"] for a in agents)


# ---------------------------------------------------------------------------
# task
# ---------------------------------------------------------------------------


def test_mac_cli_task_create_and_list(tmp_path):
    # title is a positional arg
    rc, task = _run(tmp_path, "task", "create", "Do something")
    assert rc == 0
    assert task["title"] == "Do something"
    assert task["id"].startswith("task_")

    rc, tasks = _run(tmp_path, "task", "list")
    assert rc == 0
    assert any(t["id"] == task["id"] for t in tasks)


class _FakeProc:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def test_default_project_from_cwd_uses_git_toplevel(monkeypatch):
    from mac.cli import _default_project_from_cwd

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _FakeProc(0, "/home/u/Src/myrepo\n"))
    assert _default_project_from_cwd() == "myrepo"


def test_default_project_from_cwd_falls_back_to_cwd_basename(monkeypatch):
    from mac.cli import _default_project_from_cwd

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _FakeProc(128, ""))
    monkeypatch.setattr("os.getcwd", lambda: "/tmp/some-project")
    assert _default_project_from_cwd() == "some-project"


def test_task_create_defaults_project_from_cwd(tmp_path, monkeypatch):
    # bd parity: no --project tags the task with the working directory's project.
    monkeypatch.setattr("mac.cli._default_project_from_cwd", lambda: "inferred-proj")
    rc, task = _run(tmp_path, "task", "create", "Auto project")
    assert rc == 0
    assert task["project"] == "inferred-proj"


def test_task_create_explicit_project_overrides_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr("mac.cli._default_project_from_cwd", lambda: "inferred-proj")
    rc, task = _run(tmp_path, "task", "create", "Explicit", "--project", "chosen")
    assert rc == 0
    assert task["project"] == "chosen"


def test_task_create_empty_project_means_none(tmp_path, monkeypatch):
    # Explicit --project '' opts out of the cwd default (never silently inferred).
    monkeypatch.setattr("mac.cli._default_project_from_cwd", lambda: "inferred-proj")
    rc, task = _run(tmp_path, "task", "create", "No project", "--project", "")
    assert rc == 0
    assert task["project"] is None


def test_task_list_scopes_to_cwd_project(tmp_path, monkeypatch):
    _run(tmp_path, "task", "create", "A", "--project", "alpha")
    _run(tmp_path, "task", "create", "B", "--project", "beta")
    monkeypatch.setattr("mac.cli._default_project_from_cwd", lambda: "alpha")
    rc, scoped = _run(tmp_path, "task", "list")
    assert rc == 0 and {t["project"] for t in scoped} == {"alpha"}
    rc, everything = _run(tmp_path, "task", "list", "--all")
    assert {"alpha", "beta"} <= {t["project"] for t in everything}
    rc, chosen = _run(tmp_path, "task", "list", "--project", "beta")
    assert {t["project"] for t in chosen} == {"beta"}


def test_task_ready_scopes_to_cwd_project(tmp_path, monkeypatch):
    _run(tmp_path, "task", "create", "RA", "--project", "alpha")
    _run(tmp_path, "task", "create", "RB", "--project", "beta")
    monkeypatch.setattr("mac.cli._default_project_from_cwd", lambda: "alpha")
    rc, ready = _run(tmp_path, "task", "ready")
    assert rc == 0 and ready and all(t["project"] == "alpha" for t in ready)
    rc, everything = _run(tmp_path, "task", "ready", "--all")
    assert {"alpha", "beta"} <= {t["project"] for t in everything}


def test_task_search_scopes_to_cwd_project(tmp_path, monkeypatch):
    _run(tmp_path, "task", "create", "searchable alpha", "--project", "alpha")
    _run(tmp_path, "task", "create", "searchable beta", "--project", "beta")
    monkeypatch.setattr("mac.cli._default_project_from_cwd", lambda: "alpha")
    rc, hits = _run(tmp_path, "task", "search", "searchable")
    assert rc == 0 and hits and all(t["project"] == "alpha" for t in hits)
    rc, everything = _run(tmp_path, "task", "search", "searchable", "--all")
    assert {"alpha", "beta"} <= {t["project"] for t in everything}


def test_task_create_emits_ticket_mirror(tmp_path, monkeypatch):
    import mac.tickets_mirror as tm

    d = tmp_path / ".tickets"
    d.mkdir()
    monkeypatch.setattr(tm, "tickets_dir", lambda: d)
    monkeypatch.delenv("MAC_NO_TICKET_MIRROR", raising=False)
    rc, task = _run(tmp_path, "task", "create", "Mirror me", "--project", "p")
    assert rc == 0
    files = list(d.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "# Mirror me" in text
    assert ("id: %s" % task["id"]) in text


def test_task_create_no_ticket_skips_mirror(tmp_path, monkeypatch):
    import mac.tickets_mirror as tm

    d = tmp_path / ".tickets"
    d.mkdir()
    monkeypatch.setattr(tm, "tickets_dir", lambda: d)
    rc, _task = _run(tmp_path, "task", "create", "No mirror", "--project", "p", "--no-ticket")
    assert rc == 0
    assert list(d.glob("*.md")) == []


def test_memory_remember_list_forget_round_trip(tmp_path):
    rc, remembered = _run(
        tmp_path,
        "memory",
        "remember",
        "rule",
        "keep the hub memory path routable",
        "--project",
        "mac",
        "--actor",
        "tester",
    )
    assert rc == 0
    assert remembered["record_type"] == "beads_memory:rule"
    assert remembered["subject_id"] == "mac"

    rc, listed = _run(tmp_path, "memory", "list", "--project", "mac")
    assert rc == 0
    assert listed == [
        {
            "key": "rule",
            "content": "keep the hub memory path routable",
            "created_at": remembered["created_at"],
            "id": remembered["id"],
        }
    ]

    rc, forgotten = _run(tmp_path, "memory", "forget", "rule", "--project", "mac")
    assert rc == 0
    assert forgotten == {"deleted": 1, "key": "rule", "project": "mac"}
    rc, listed = _run(tmp_path, "memory", "list", "--project", "mac")
    assert rc == 0
    assert listed == []


def test_mac_cli_task_show(tmp_path):
    rc, task = _run(tmp_path, "task", "create", "Show me")
    assert rc == 0

    rc, detail = _run(tmp_path, "task", "show", task["id"])
    assert rc == 0
    # task show returns {task: {...}, evidence: [], history: [], ...}
    assert detail["task"]["id"] == task["id"]
    assert detail["task"]["title"] == "Show me"


def test_mac_cli_task_ready_requires_completed_dependencies(tmp_path):
    rc, parent = _run(tmp_path, "task", "create", "Parent")
    assert rc == 0
    rc, child = _run(tmp_path, "task", "create", "Child")
    assert rc == 0
    rc, cancelled_parent = _run(tmp_path, "task", "close", parent["id"], "--cancelled")
    assert rc == 0
    assert cancelled_parent["state"] == "cancelled"

    db_path = tmp_path / "mac.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET dependencies = ? WHERE id = ?",
            (json.dumps([parent["id"]]), child["id"]),
        )

    rc, ready = _run(tmp_path, "task", "ready")
    assert rc == 0
    assert child["id"] not in {task["id"] for task in ready}

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE tasks SET state = ? WHERE id = ?", ("completed", parent["id"]))

    rc, ready = _run(tmp_path, "task", "ready")
    assert rc == 0
    assert child["id"] in {task["id"] for task in ready}


def test_mac_cli_task_create_description_file(tmp_path):
    """Multi-line description with shell metacharacters round-trips via
    --description-file without any shell-quoting hazards."""
    body = (
        "Step 1: append a line\n"
        "  printf -- '- foo bar (baz) {x}' >> file.md\n"
        "Step 2: git commit -m \"msg with $var and `backticks`\"\n"
        "Step 3: push to refs/heads/branch\n"
    )
    desc_file = tmp_path / "desc.txt"
    desc_file.write_text(body, encoding="utf-8")
    metadata_file = tmp_path / "meta.json"
    metadata_file.write_text('{"publication_target": "git://main"}', encoding="utf-8")

    rc, task = _run(
        tmp_path,
        "task", "create", "Mechanical task",
        "--description-file", str(desc_file),
        "--metadata-file", str(metadata_file),
    )
    assert rc == 0
    assert task["title"] == "Mechanical task"
    assert task["description"] == body
    assert task["metadata"]["publication_target"] == "git://main"


def test_mac_cli_task_create_metadata_invalid_json_errors_cleanly(tmp_path):
    """A malformed --metadata value must raise SystemExit, not crash with
    a raw json.JSONDecodeError traceback."""
    import pytest
    with pytest.raises(SystemExit) as exc:
        _run(
            tmp_path,
            "task", "create", "x",
            "--metadata", "{not: valid json",
        )
    assert "invalid JSON" in str(exc.value)


# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------


def test_mac_cli_runtime_delta_lifecycle(tmp_path):
    rc, runtime = _run(
        tmp_path,
        "runtime",
        "create",
        "cli-runtime",
        "--manifest",
        '{"image":"python:3.12@sha256:abc123"}',
        "--created-by",
        "ops",
    )
    assert rc == 0
    rc, machine = _run(tmp_path, "machine", "register", "cli-host")
    assert rc == 0
    rc, agent = _run(tmp_path, "agent", "register", machine["id"], "cli-worker")
    assert rc == 0
    rc, task = _run(tmp_path, "task", "create", "CLI dependency delta")
    assert rc == 0

    rc, delta = _run(
        tmp_path,
        "runtime",
        "delta",
        "propose",
        task["id"],
        agent["id"],
        "--package-manager",
        "pip",
        "--commands",
        '["python -m venv .venv","./.venv/bin/pip install rich==13.7.1"]',
        "--dependencies",
        '["rich==13.7.1"]',
        "--reason",
        "cli task-local dependency",
        "--base-runtime",
        runtime["id"],
        "--lockfile-path",
        "requirements.txt",
        "--lockfile-digest",
        "sha256:" + "f" * 64,
    )
    assert rc == 0
    assert delta["status"] == "proposed"

    rc, validated = _run(tmp_path, "runtime", "delta", "validate", delta["id"])
    assert rc == 0
    assert validated["status"] == "validated"
    rc, promoted = _run(tmp_path, "runtime", "delta", "promote", delta["id"])
    assert rc == 0
    assert promoted["status"] == "promoted"


# ---------------------------------------------------------------------------
# project
# ---------------------------------------------------------------------------


def test_mac_cli_project_create_and_list(tmp_path):
    # name is a positional arg; no --slug flag
    rc, project = _run(tmp_path, "project", "create", "Alpha Project")
    assert rc == 0
    assert project["name"] == "Alpha Project"
    assert project["id"].startswith("project_")

    rc, projects = _run(tmp_path, "project", "list")
    assert rc == 0
    # project list returns summary objects: {project: <name>, project_id: <id>, ...}
    assert any(p.get("project") == "Alpha Project" for p in projects)


# ---------------------------------------------------------------------------
# secret
# ---------------------------------------------------------------------------


def test_mac_cli_secret_set_and_list_redacts_value(tmp_path):
    # name and value are positional; --scopes and --created-by are required flags
    rc, secret = _run(
        tmp_path,
        "secret",
        "set",
        "deploy-token",
        "never-reveal-this",
        "--scopes",
        '{"capabilities": ["deploy"]}',
        "--created-by",
        "human",
    )
    assert rc == 0
    assert secret["name"] == "deploy-token"

    rc, secrets = _run(tmp_path, "secret", "list")
    assert rc == 0
    for s in secrets:
        assert s.get("value", "***REDACTED***") != "never-reveal-this"
