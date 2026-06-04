"""Tests for the `mac` CLI (mac.cli:main).

Each test exercises a CLI subcommand end-to-end against a real in-file
SQLite database (tmp_path), confirming the command emits valid JSON and
round-trips through the same ControlPlane layer the HTTP API uses.
"""

from __future__ import annotations

import io
import json
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


def test_mac_cli_task_show(tmp_path):
    rc, task = _run(tmp_path, "task", "create", "Show me")
    assert rc == 0

    rc, detail = _run(tmp_path, "task", "show", task["id"])
    assert rc == 0
    # task show returns {task: {...}, evidence: [], history: [], ...}
    assert detail["task"]["id"] == task["id"]
    assert detail["task"]["title"] == "Show me"


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
