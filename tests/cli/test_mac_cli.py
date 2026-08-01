"""Tests for the `mac` CLI (mac.cli:main).

Each test exercises a CLI subcommand end-to-end against a real in-file
SQLite database (tmp_path), confirming the command emits valid JSON and
round-trips through the same ControlPlane layer the HTTP API uses.
"""

from __future__ import annotations

import argparse
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


def test_database_migrate_sqlite_to_postgres_cli(tmp_path, monkeypatch):
    from mac import sqlite_postgres_migration

    source = tmp_path / "source.db"
    source.touch()
    seen = {}

    def fake_migrate(sqlite_path, postgres_dsn, **kwargs):
        seen.update(
            {
                "sqlite_path": str(sqlite_path),
                "postgres_dsn": postgres_dsn,
                **kwargs,
            }
        )
        return {
            "schema": "mac.sqlite_postgres_migration.v1",
            "status": "verified",
            "completed_table_count": 148,
        }

    monkeypatch.setattr(
        sqlite_postgres_migration,
        "migrate_sqlite_to_postgres",
        fake_migrate,
    )

    rc, result = _run(
        tmp_path,
        "database",
        "migrate-sqlite-to-postgres",
        "--sqlite",
        str(source),
        "--postgres-url",
        "postgresql:///mac",
        "--hub-stopped",
        "--json",
    )

    assert rc == 0
    assert result["status"] == "verified"
    assert seen["sqlite_path"] == str(source)
    assert seen["postgres_dsn"] == "postgresql:///mac"
    assert seen["report_path"] == str(source) + ".postgres.json"


def test_home_db_task_create_requires_explicit_local_authority(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    monkeypatch.delenv("MAC_SECRET_KEY", raising=False)
    fleet_config = tmp_path / "fleets.yaml"
    fleet_config.write_text(
        "fleets:\n  production:\n    default: true\n    agents: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(fleet_config))
    db_path = tmp_path / ".mac" / "mac.db"
    db_path.parent.mkdir(parents=True)

    rc = main(["--db", str(db_path), "task", "create", "stranded work"])

    assert rc == 1
    error = capsys.readouterr().err
    assert "not a repository ticket store" in error
    assert "never uploaded or reconciled" in error
    assert "--local-authority" in error
    assert not db_path.exists()


def test_mac_cli_migrate_local_ledger_distinguishes_source_from_target(
    tmp_path, capsys
):
    source = tmp_path / "source.db"
    source.touch()

    rc, result = _run(
        tmp_path,
        "migrate",
        "local-ledger",
        "--source-db",
        str(source),
    )

    assert rc == 1
    assert result is None
    assert "--db selects the migration target authority" in capsys.readouterr().err


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


def test_mac_cli_openshell_reconcile_apply_preserves_resources(tmp_path):
    rc, machine = _run(tmp_path, "machine", "register", "openshell-host")
    assert rc == 0
    rc, agent = _run(
        tmp_path,
        "agent",
        "register",
        machine["id"],
        "rocky",
        "--capabilities",
        "python,ops",
        "--resources",
        '{"hardware":{"accelerator":"gpu"},"commands":{"available":["git"]}}',
        "--agent-id",
        "agent_rocky",
    )
    assert rc == 0

    rc, result = _run(
        tmp_path,
        "openshell",
        "reconcile",
        "--agent",
        "rocky",
        "--apply",
        "--validated",
        "--actor",
        "test",
        "--sandbox-id",
        "smoke-cli",
        "--validation-summary",
        "cli smoke passed",
    )

    assert rc == 0
    assert result["dry_run"] is False
    assert result["agents"][0]["after"]["effective"]["deployed"] is True

    rc, status = _run(tmp_path, "openshell", "status", "--agent", agent["id"])
    assert rc == 0
    assert status["required"] is True
    assert status["effective"]["assigned"] is True
    assert status["effective"]["deployed"] is True

    rc, refreshed = _run(tmp_path, "agent", "list")
    assert rc == 0
    rocky = next(item for item in refreshed if item["id"] == agent["id"])
    assert rocky["resources"]["openshell_required"] is True
    assert rocky["resources"]["hardware"] == {"accelerator": "gpu"}


def test_mac_cli_openshell_reconcile_fleet_skips_missing_agents(tmp_path):
    rc, machine = _run(tmp_path, "machine", "register", "fleet-host")
    assert rc == 0
    rc, _agent = _run(
        tmp_path,
        "agent",
        "register",
        machine["id"],
        "present",
        "--agent-id",
        "agent_present",
    )
    assert rc == 0
    fleet_config = tmp_path / "fleets.yaml"
    fleet_config.write_text(
        """
version: 1
fleets:
  test-fleet:
    default: true
    agents:
      - name: present
        enabled: true
        os: linux
      - name: missing
        enabled: true
        os: linux
""".strip()
        + "\n",
        encoding="utf-8",
    )

    rc, result = _run(
        tmp_path,
        "openshell",
        "reconcile",
        "--fleet-config",
        str(fleet_config),
        "--target-fleet",
        "test-fleet",
    )

    assert rc == 0
    assert result["dry_run"] is True
    assert result["missing_agents"] == ["missing"]
    assert [row["agent_name"] for row in result["agents"]] == ["present"]


def test_fleet_refresh_source_publishes_repo_update_for_all_agents(tmp_path):
    rc, machine = _run(tmp_path, "machine", "register", "refresh-host")
    assert rc == 0
    rc, sender = _run(tmp_path, "agent", "register", machine["id"], "hub")
    assert rc == 0
    rc, worker = _run(tmp_path, "agent", "register", machine["id"], "worker")
    assert rc == 0

    rc, published = _run(
        tmp_path,
        "fleet",
        "refresh-source",
        "--sender-agent-id",
        sender["id"],
        "--remote",
        "origin",
        "--branch",
        "main",
        "--request-id",
        "refresh-local",
    )

    assert rc == 0
    assert published["schema"] == "mac.agentbus.repo_update_publish.v1"
    assert published["count"] == 2
    assert len(published["streams"]) == 2
    assert {stream["recipient_agent_id"] for stream in published["streams"]} == {
        sender["id"],
        worker["id"],
    }

    rc, targeted = _run(
        tmp_path,
        "fleet",
        "refresh-source",
        "--sender-agent-id",
        sender["id"],
        "--agent-id",
        worker["id"],
        "--request-id",
        "refresh-targeted",
        "--restart-service",
        "mac.service",
    )

    assert rc == 0
    assert targeted["schema"] == "mac.agentbus.repo_update_publish.v1"
    assert targeted["count"] == 1
    assert targeted["streams"][0]["recipient_agent_id"] == worker["id"]

    rc, chunks = _run(tmp_path, "agentbus", "read", targeted["streams"][0]["id"], worker["id"])
    assert rc == 0
    assert chunks[0]["payload"]["restart_services"] == ["mac.service"]


def test_agentbus_cli_wrappers_pass_sender_as_keyword(monkeypatch, capsys):
    from mac.cli import cmd_agentbus_append, cmd_agentbus_close, cmd_agentbus_open

    calls = []

    class FakePlane:
        def open_agentbus_stream(self, *, sender_agent_id, **kwargs):
            calls.append(("open", sender_agent_id, kwargs))
            return {"id": kwargs["stream_id"]}

        def append_agentbus_chunk(self, stream_id, *, sender_agent_id, **kwargs):
            calls.append(("append", stream_id, sender_agent_id, kwargs))
            return {"id": stream_id}

        def close_agentbus_stream(self, stream_id, *, sender_agent_id, **kwargs):
            calls.append(("close", stream_id, sender_agent_id, kwargs))
            return {"id": stream_id}

    monkeypatch.setattr("mac.cli._plane", lambda args: FakePlane())

    cmd_agentbus_open(
        argparse.Namespace(
            sender_agent_id="agent_sender",
            recipient_agent_id="agent_recipient",
            content_type="application/json",
            topic="probe",
            headers='{"trace": true}',
            task_id="task_1",
            stream_id="bus_1",
        )
    )
    cmd_agentbus_append(
        argparse.Namespace(
            stream_id="bus_1",
            sender_agent_id="agent_sender",
            payload='{"ok": true}',
            payload_encoding="json",
            content_type="application/json",
            final=True,
        )
    )
    cmd_agentbus_close(
        argparse.Namespace(
            stream_id="bus_1",
            sender_agent_id="agent_sender",
            status="complete",
        )
    )

    capsys.readouterr()
    assert calls == [
        (
            "open",
            "agent_sender",
            {
                "recipient_agent_id": "agent_recipient",
                "content_type": "application/json",
                "topic": "probe",
                "headers": {"trace": True},
                "task_id": "task_1",
                "stream_id": "bus_1",
            },
        ),
        (
            "append",
            "bus_1",
            "agent_sender",
            {
                "payload": {"ok": True},
                "content_type": "application/json",
                "payload_encoding": "json",
                "final": True,
            },
        ),
        ("close", "bus_1", "agent_sender", {"status": "complete"}),
    ]


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


def test_task_create_empty_project_opts_out_of_cwd_inference(tmp_path, monkeypatch):
    # Explicit --project '' opts out of the cwd default (never silently
    # inferred). It does not produce an unscoped task: work with no project of
    # its own is scoped to fleet-maintenance so it stays countable.
    monkeypatch.setattr("mac.cli._default_project_from_cwd", lambda: "inferred-proj")
    rc, task = _run(tmp_path, "task", "create", "No project", "--project", "")
    assert rc == 0
    assert task["project"] == "fleet-maintenance"


def test_task_create_can_opt_out_of_project_scoping_entirely(tmp_path, monkeypatch):
    """The fleet-maintenance default is a policy, and policies can be disabled."""
    monkeypatch.setattr("mac.cli._default_project_from_cwd", lambda: "inferred-proj")
    monkeypatch.setenv("MAC_SCOPE_UNPROJECTED_TASKS", "0")
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
    _run(tmp_path, "project", "create", "alpha", "--active")
    _run(tmp_path, "project", "create", "beta", "--active")
    _run(tmp_path, "task", "create", "RA", "--project", "alpha")
    _run(tmp_path, "task", "create", "RB", "--project", "beta")
    monkeypatch.setattr("mac.cli._default_project_from_cwd", lambda: "alpha")
    rc, ready = _run(tmp_path, "task", "ready")
    assert rc == 0 and ready and all(t["project"] == "alpha" for t in ready)
    rc, everything = _run(tmp_path, "task", "ready", "--all")
    assert {"alpha", "beta"} <= {t["project"] for t in everything}


def test_task_why_unclaimed_reports_authoritative_reason(tmp_path, monkeypatch):
    # Pin the inferred project rather than relying on the checkout being named
    # "mac": agents are required to work from git worktrees, whose directory
    # names differ, and an unpinned cwd made this pass or fail by location.
    monkeypatch.setattr("mac.cli._default_project_from_cwd", lambda: "mac")
    _run(tmp_path, "project", "create", "mac", "--active")
    rc, task = _run(tmp_path, "task", "create", "Explain unclaimed")
    assert rc == 0
    rc, explanation = _run(tmp_path, "task", "why-unclaimed", task["id"])
    assert rc == 0
    assert explanation["task"]["id"] == task["id"]
    assert explanation["task_ready"] is True
    assert explanation["dispatchable"] is False
    assert explanation["unclaimed_reasons"][0]["code"] == "no_agents_registered"


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
    rc, parent = _run(tmp_path, "task", "create", "Parent", "--project", "")
    assert rc == 0
    rc, child = _run(
        tmp_path,
        "task",
        "create",
        "Child",
        "--project",
        "",
        "--dependencies",
        parent["id"],
    )
    assert rc == 0
    rc, cancelled_parent = _run(
        tmp_path,
        "task",
        "close",
        parent["id"],
        "--cancelled",
        "--reason",
        "dependency cancellation fixture",
    )
    assert rc == 0
    assert cancelled_parent["state"] == "cancelled"

    db_path = tmp_path / "mac.db"

    rc, ready = _run(tmp_path, "task", "ready", "--all")
    assert rc == 0
    assert child["id"] not in {task["id"] for task in ready}

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE tasks SET state = ? WHERE id = ?", ("completed", parent["id"]))

    # Readiness is now side-effect free. A raw out-of-band state mutation is
    # repaired by the explicit reconciliation tick, not by the query itself.
    rc, _tick = _run(tmp_path, "dispatch", "tick", "--limit", "0")
    assert rc == 0
    rc, ready = _run(tmp_path, "task", "ready", "--all")
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
    with pytest.raises(SystemExit) as exc:
        _run(
            tmp_path,
            "task", "create", "x",
            "--metadata", "{not: valid json",
        )
    assert "invalid JSON" in str(exc.value)


def test_mac_cli_task_create_exposes_publication_lane_policy(tmp_path):
    rc, task = _run(
        tmp_path,
        "task",
        "create",
        "Compatibility task",
        "--publication-lane",
        "legacy",
    )

    assert rc == 0
    assert task["metadata"]["publication_lane_policy"] == "legacy"
    assert task["publication_lane"] == "legacy"
    assert task["publication_route"]["route_state"] == "legacy_compatibility"


def test_mac_cli_task_create_idempotency_key_reuses_exact_task(tmp_path, capsys):
    command = (
        "task",
        "create",
        "Retry-safe CLI task",
        "--description",
        "same request",
        "--idempotency-key",
        "cli-retry-9",
    )

    first_rc, first = _run(tmp_path, *command)
    retry_rc, retry = _run(tmp_path, *command)

    assert first_rc == 0
    assert retry_rc == 0
    assert retry["id"] == first["id"]

    changed_rc, changed = _run(
        tmp_path,
        "task",
        "create",
        "Changed CLI request",
        "--idempotency-key",
        "cli-retry-9",
    )
    assert changed_rc == 1
    assert changed is None
    assert "already bound to a different request" in capsys.readouterr().err


def test_task_create_tolerates_older_hub_without_route_endpoint(monkeypatch, capsys):
    from mac import cli

    class OlderHub:
        def create_task(self, title, **_kwargs):
            return {"id": "task_" + ("a" * 32), "title": title, "state": "open"}

        def task_publication_route(self, _task_id):
            raise RuntimeError("HTTP 404: route is unavailable on this hub")

    monkeypatch.setattr(cli, "_plane", lambda _args: OlderHub())
    monkeypatch.setattr(cli, "_OUTPUT_JSON", True)
    cli.cmd_task_create(
        argparse.Namespace(
            title="Mixed-version create",
            description="",
            description_file=None,
            metadata="{}",
            metadata_file=None,
            no_dispatch=False,
            no_decompose=False,
            publication_lane="auto",
            model="",
            model_strength=None,
            kind="",
            project="",
            priority=0,
            required_capabilities="",
            dependencies="",
            max_attempts=3,
            actor="human",
            no_ticket=True,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["publication_lane"] == "unknown"
    assert payload["publication_route"]["route_state"] == "unreported"


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


def test_task_ask_parks_the_task_on_a_stated_question(tmp_path):
    """`mac task ask` parks work on a human question instead of failing it."""
    rc, task = _run(tmp_path, "task", "create", "Ambiguous work")
    assert rc == 0

    rc, parked = _run(
        tmp_path,
        "task",
        "ask",
        task["id"],
        "--question",
        "which database?",
        "--question",
        "which region?",
        "--why",
        "the spec names neither",
    )
    assert rc == 0
    assert parked["state"] == "needs_input"
    payload = parked["metadata"]["needs_input"]
    assert [q["question"] for q in payload["questions"]] == [
        "which database?",
        "which region?",
    ]
    assert payload["asked_by"] == "human"


def test_task_answer_returns_a_parked_task_to_the_pool(tmp_path):
    rc, task = _run(tmp_path, "task", "create", "Ambiguous work")
    assert rc == 0
    _run(tmp_path, "task", "ask", task["id"], "--question", "which database?")

    rc, answered = _run(
        tmp_path,
        "task",
        "answer",
        task["id"],
        "--answer",
        "postgres",
        "--actor",
        "jordan",
    )
    assert rc == 0
    assert answered["state"] == "open"
    # Cleared as outstanding, retained as history next to its answer.
    assert "needs_input" not in answered["metadata"]
    record = answered["metadata"]["needs_input_history"][-1]
    assert record["answer"] == "postgres"
    assert record["answered_by"] == "jordan"
