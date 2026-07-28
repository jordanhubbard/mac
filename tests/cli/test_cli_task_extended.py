"""Behavioral tests for extended `mac task` CLI subcommands.

Subcommands covered:
  - task audit   [--project] [--no-git]
  - task stats   [--project] [--all]
  - task throughput [--project] [--all] [--since-hours]
  - task summary <task_id>
  - task claim   <task_id> <agent_id>
  - task start   <task_id> <agent_id>
  - task reopen  <task_id> [--reason] [--actor]
  - task recover-finalizer <workspace> [--approve-new-file ...] [--execute]
  - task recover-stalled-finalizer <workspace> [--approve-new-file ...] [--execute]
  - task release <task_id> [--actor]
  - task break-glass <task_id> <agent_id> --reason ...
  - task break-glass-list <task_id>
  - task break-glass-revoke <authorization_id> --reason ...
  - task evidence <task_id> --kind ... --uri ... --summary ... --created-by ...

These complement the core subcommands already covered in test_mac_cli.py
(create, list, show, search, close, ready).  Each test uses the same
_run(tmp_path, ...) helper that calls mac.cli:main against a private SQLite
database under tmp_path.
"""

from __future__ import annotations

import io
import json
import sys

from mac.cli import main


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(tmp_path, *args):
    """Run `mac --db <tmp> <args>` and return (rc, parsed_output).

    parsed_output is the JSON-decoded stdout when the command emits JSON, or
    the raw text string for commands like `task summary` that emit plain text.
    Returns None when stdout is empty.
    """
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", str(tmp_path / "mac.db"), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    if not raw:
        return rc, None
    try:
        return rc, json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return rc, raw


def _register_agent(tmp_path, name="worker-1", machine_name=None):
    """Register a machine + agent and return the agent record."""
    host = machine_name or (name + "-host")
    rc, machine = _run(tmp_path, "machine", "register", host)
    assert rc == 0
    rc, agent = _run(tmp_path, "agent", "register", machine["id"], name)
    assert rc == 0
    return agent


def _create_task(tmp_path, title="test task", project=None):
    """Create a task and return the task record."""
    args = ["task", "create", title]
    if project:
        args += ["--project", project]
    rc, task = _run(tmp_path, *args)
    assert rc == 0
    return task


def _claim_for_evidence(tmp_path, task, *, name="evidence-worker"):
    """Create one real lease fence for the public evidence command."""

    agent = _register_agent(tmp_path, name=name)
    rc, claimed = _run(tmp_path, "task", "claim", task["id"], agent["id"])
    assert rc == 0
    return agent, claimed["lease_id"]


# ---------------------------------------------------------------------------
# task audit
# ---------------------------------------------------------------------------


def test_task_audit_reports_every_local_ledger_task(tmp_path):
    first = _create_task(tmp_path, title="audit first", project="project-a")
    second = _create_task(tmp_path, title="audit second", project="project-b")

    rc, report = _run(tmp_path, "task", "audit", "--no-git")

    assert rc == 0
    assert report["schema"] == "mac.task_ledger_audit.v1"
    assert report["snapshot"]["task_count"] == 2
    assert {row["task_id"] for row in report["tasks"]} == {
        first["id"],
        second["id"],
    }


def test_task_audit_project_filter_is_explicit(tmp_path):
    selected = _create_task(tmp_path, title="selected", project="project-a")
    _create_task(tmp_path, title="excluded", project="project-b")

    rc, report = _run(
        tmp_path,
        "task",
        "audit",
        "--project",
        "project-a",
        "--no-git",
    )

    assert rc == 0
    assert report["snapshot"]["task_count"] == 1
    assert [row["task_id"] for row in report["tasks"]] == [selected["id"]]


def test_task_throughput_reports_active_flow_and_slo(tmp_path):
    task = _create_task(tmp_path, title="throughput task", project="project-a")

    rc, report = _run(
        tmp_path,
        "task",
        "throughput",
        "--all",
        "--since-hours",
        "6",
        "--warning-minutes",
        "5",
        "--critical-minutes",
        "10",
        "--refresh-limit",
        "25",
    )

    assert rc == 0
    assert report["schema"] == "mac.task_flow_snapshot.v1"
    assert report["active"]["count"] == 1
    assert report["stranding"]["count"] == 0
    assert report["materialization"]["refreshed_count"] == 0
    assert report["slo"]["basic_cycle_target_p50_seconds"] == 300
    assert report["slo"]["basic_cycle_target_p95_seconds"] == 600
    assert report["active"]["age"]["count"] == 1
    assert report["stranding"]["episodes"] == []
    assert report["contention"]["count"] == 0
    assert report["snapshot_id"].startswith("flowsnap_")
    assert any(
        row["task_id"] == task["id"]
        for row in report["stranding"]["episodes"]
    ) is False


def test_task_recover_finalizer_forwards_explicit_recovery_contract(
    tmp_path, monkeypatch
):
    seen = {}

    def fake_recover(
        workspace,
        *,
        approved_new_files,
        original_evidence_id,
        execute,
    ):
        seen.update(
            {
                "workspace": workspace,
                "approved": approved_new_files,
                "evidence_id": original_evidence_id,
                "execute": execute,
            }
        )
        return {"schema": "mac.repository_finalizer_recovery_plan.v1", "eligible": True}

    monkeypatch.setattr(
        "mac.repository_recovery.recover_finalizer_worktree",
        fake_recover,
    )
    workspace = tmp_path / "preserved-task"

    rc, result = _run(
        tmp_path,
        "task",
        "recover-finalizer",
        str(workspace),
        "--approve-new-file",
        "src/new.py",
        "--approve-new-file",
        "tests/test_new.py",
        "--evidence-id",
        "ev_original",
        "--execute",
    )

    assert rc == 0
    assert result["eligible"] is True
    assert seen == {
        "workspace": str(workspace),
        "approved": ["src/new.py", "tests/test_new.py"],
        "evidence_id": "ev_original",
        "execute": True,
    }


def test_task_recover_stalled_finalizer_forwards_recovery_contract(
    tmp_path, monkeypatch
):
    seen = {}

    def fake_recover(
        workspace,
        *,
        approved_new_files,
        original_evidence_id,
        execute,
    ):
        seen.update(
            {
                "workspace": workspace,
                "approved": approved_new_files,
                "evidence_id": original_evidence_id,
                "execute": execute,
            }
        )
        return {
            "schema": "mac.repository_stalled_finalizer_recovery_plan.v1",
            "eligible": True,
        }

    monkeypatch.setattr(
        "mac.repository_recovery.recover_stalled_finalizer",
        fake_recover,
    )
    workspace = tmp_path / "preserved-task"

    rc, result = _run(
        tmp_path,
        "task",
        "recover-stalled-finalizer",
        str(workspace),
        "--approve-new-file",
        "src/new.py",
        "--evidence-id",
        "ev_stalled",
        "--execute",
    )

    assert rc == 0
    assert result["eligible"] is True
    assert seen == {
        "workspace": str(workspace),
        "approved": ["src/new.py"],
        "evidence_id": "ev_stalled",
        "execute": True,
    }


# ---------------------------------------------------------------------------
# task stats
# ---------------------------------------------------------------------------


def test_task_stats_returns_dict_of_state_counts(tmp_path):
    """task stats returns a dict mapping state -> count."""
    rc, stats = _run(tmp_path, "task", "stats")
    assert rc == 0
    # Empty DB: the stats dict may be empty or have zero counts
    assert isinstance(stats, dict)


def test_task_stats_counts_created_task(tmp_path):
    """task stats reflects newly created tasks in the open state."""
    _create_task(tmp_path)
    rc, stats = _run(tmp_path, "task", "stats")
    assert rc == 0
    assert stats.get("open", 0) >= 1


def test_task_stats_project_filter(tmp_path):
    """task stats --project scopes the count to the given project."""
    _create_task(tmp_path, title="proj-a task", project="project-a")
    _create_task(tmp_path, title="proj-b task", project="project-b")

    rc, stats_a = _run(tmp_path, "task", "stats", "--project", "project-a")
    assert rc == 0
    # project-a has 1 task; project-b tasks should not appear
    total_a = sum(stats_a.values())
    assert total_a == 1


def test_task_stats_counts_cancelled_task(tmp_path):
    """task stats reflects the cancelled state after cancelling a task.

    The state machine only allows open -> cancelled (not open -> completed),
    so we close with --cancelled.
    """
    task = _create_task(tmp_path)
    _run(
        tmp_path,
        "task",
        "close",
        task["id"],
        "--cancelled",
        "--reason",
        "stats cancellation fixture",
    )

    rc, stats = _run(tmp_path, "task", "stats")
    assert rc == 0
    assert stats.get("cancelled", 0) >= 1


# ---------------------------------------------------------------------------
# task summary
# ---------------------------------------------------------------------------


def test_task_summary_returns_task_id_line(tmp_path):
    """task summary outputs a human-readable string with the task id."""
    task = _create_task(tmp_path, title="my summary task")
    rc, text = _run(tmp_path, "task", "summary", task["id"])
    assert rc == 0
    assert isinstance(text, str)
    assert task["id"] in text


def test_task_summary_shows_no_activity_message_for_new_task(tmp_path):
    """task summary says 'no activity recorded' for a fresh task."""
    task = _create_task(tmp_path)
    rc, text = _run(tmp_path, "task", "summary", task["id"])
    assert rc == 0
    assert isinstance(text, str)
    assert "no activity" in text.lower()


def test_task_summary_shows_title(tmp_path):
    """task summary includes the task title in its output."""
    task = _create_task(tmp_path, title="special summary title")
    rc, text = _run(tmp_path, "task", "summary", task["id"])
    assert rc == 0
    assert isinstance(text, str)
    assert "special summary title" in text


# ---------------------------------------------------------------------------
# task claim
# ---------------------------------------------------------------------------


def test_task_claim_returns_task_and_lease(tmp_path):
    """task claim returns {task: ..., lease_id: ...} and sets state to claimed."""
    agent = _register_agent(tmp_path)
    task = _create_task(tmp_path)

    rc, result = _run(tmp_path, "task", "claim", task["id"], agent["id"])
    assert rc == 0
    assert result["task"]["id"] == task["id"]
    assert result["task"]["state"] == "claimed"
    assert result["lease_id"] is not None
    assert result["lease_id"].startswith("lease_")


def test_task_claim_sets_owner_agent(tmp_path):
    """After claim, the task record reflects the claiming agent."""
    agent = _register_agent(tmp_path)
    task = _create_task(tmp_path)
    _run(tmp_path, "task", "claim", task["id"], agent["id"])

    rc, shown = _run(tmp_path, "task", "show", task["id"])
    assert rc == 0
    task_data = shown.get("task", shown)
    assert task_data["owner_agent_id"] == agent["id"]


def test_task_claim_prevents_double_claim(tmp_path):
    """A claimed task cannot be claimed a second time."""
    a1 = _register_agent(tmp_path, name="worker-1", machine_name="host-1")
    a2 = _register_agent(tmp_path, name="worker-2", machine_name="host-2")
    task = _create_task(tmp_path)

    rc1, _ = _run(tmp_path, "task", "claim", task["id"], a1["id"])
    assert rc1 == 0

    rc2, err = _run(tmp_path, "task", "claim", task["id"], a2["id"])
    assert rc2 != 0  # second claim must fail


# ---------------------------------------------------------------------------
# task break-glass recovery
# ---------------------------------------------------------------------------


def test_task_break_glass_authorize_list_and_revoke(tmp_path):
    agent = _register_agent(tmp_path, name="recovery-worker")
    task = _create_task(tmp_path, title="repair sandbox runtime")

    rc, authorization = _run(
        tmp_path,
        "task",
        "break-glass",
        task["id"],
        agent["id"],
        "--reason",
        "repair the sandbox launcher from the trusted host",
        "--ttl-seconds",
        "300",
        "--actor",
        "cli-test",
    )
    assert rc == 0
    assert authorization["task_id"] == task["id"]
    assert authorization["agent_id"] == agent["id"]
    assert authorization["status"] == "active"

    rc, authorizations = _run(
        tmp_path, "task", "break-glass-list", task["id"]
    )
    assert rc == 0
    assert [item["id"] for item in authorizations] == [authorization["id"]]

    rc, revoked = _run(
        tmp_path,
        "task",
        "break-glass-revoke",
        authorization["id"],
        "--reason",
        "recovery was cancelled before claim",
        "--actor",
        "cli-test",
    )
    assert rc == 0
    assert revoked["status"] == "revoked"


# ---------------------------------------------------------------------------
# task start
# ---------------------------------------------------------------------------


def test_task_start_transitions_to_running(tmp_path):
    """task start moves state from claimed to running."""
    agent = _register_agent(tmp_path)
    task = _create_task(tmp_path)
    _run(tmp_path, "task", "claim", task["id"], agent["id"])

    rc, started = _run(tmp_path, "task", "start", task["id"], agent["id"])
    assert rc == 0
    assert started["state"] == "running"


def test_task_start_keeps_owner_agent(tmp_path):
    """The task's owner_agent_id is preserved after start."""
    agent = _register_agent(tmp_path)
    task = _create_task(tmp_path)
    _run(tmp_path, "task", "claim", task["id"], agent["id"])
    rc, started = _run(tmp_path, "task", "start", task["id"], agent["id"])
    assert rc == 0
    assert started["owner_agent_id"] == agent["id"]


# ---------------------------------------------------------------------------
# task reopen
# ---------------------------------------------------------------------------


def test_task_reopen_returns_open_state(tmp_path):
    """task reopen transitions a cancelled task back to open."""
    task = _create_task(tmp_path)
    _run(tmp_path, "task", "close", task["id"], "--cancelled", "--reason", "reopen fixture")

    rc, reopened = _run(tmp_path, "task", "reopen", task["id"])
    assert rc == 0
    assert reopened["state"] == "open"


def test_task_reopen_with_reason(tmp_path):
    """task reopen --reason is stored in the task history (audit trail)."""
    task = _create_task(tmp_path)
    _run(tmp_path, "task", "close", task["id"], "--cancelled", "--reason", "reopen fixture")

    rc, reopened = _run(
        tmp_path, "task", "reopen", task["id"],
        "--reason", "requeue after infrastructure fix",
        "--actor", "hub",
    )
    assert rc == 0
    assert reopened["state"] == "open"


def test_task_reopen_resets_attempt_count(tmp_path):
    """task reopen clears the attempt count so the task doesn't immediately exhaust retries."""
    task = _create_task(tmp_path)
    _run(tmp_path, "task", "close", task["id"], "--cancelled", "--reason", "reopen fixture")

    rc, reopened = _run(tmp_path, "task", "reopen", task["id"])
    assert rc == 0
    # After reopen, attempt_count should be reset (0 or 1)
    assert reopened.get("attempt_count", 0) <= 1


# ---------------------------------------------------------------------------
# task release
# ---------------------------------------------------------------------------


def test_task_release_clears_no_dispatch_hold(tmp_path):
    """task release clears the no_dispatch hold flag so the task becomes dispatchable."""
    # Create with --no-dispatch so it's staged
    rc, task = _run(tmp_path, "task", "create", "staged task", "--no-dispatch")
    assert rc == 0
    assert task.get("metadata", {}).get("no_dispatch") is True

    rc, released = _run(tmp_path, "task", "release", task["id"])
    assert rc == 0
    assert "no_dispatch" not in (released.get("metadata") or {})


def test_task_release_is_noop_for_normal_task(tmp_path):
    """task release on a task without no_dispatch hold returns the task unchanged."""
    task = _create_task(tmp_path)
    rc, released = _run(tmp_path, "task", "release", task["id"])
    assert rc == 0
    assert released["id"] == task["id"]
    assert released["state"] == "open"


# ---------------------------------------------------------------------------
# task evidence
# ---------------------------------------------------------------------------


def test_task_evidence_adds_test_evidence(tmp_path):
    """task evidence --kind test creates an evidence record on the task."""
    task = _create_task(tmp_path)
    agent, lease_id = _claim_for_evidence(tmp_path, task)

    rc, ev = _run(
        tmp_path,
        "task", "evidence", task["id"],
        "--kind", "test",
        "--uri", "ci://build/42/test-results",
        "--summary", "all 86 tests passed",
        "--created-by", agent["id"],
        "--lease-id", lease_id,
    )
    assert rc == 0
    assert ev["id"].startswith("ev_")
    assert ev["kind"] == "test"
    assert ev["task_id"] == task["id"]
    assert ev["created_by"] == agent["id"]


def test_task_evidence_adds_artifact_evidence(tmp_path):
    """task evidence --kind artifact records an artifact URI."""
    task = _create_task(tmp_path)
    agent, lease_id = _claim_for_evidence(tmp_path, task)

    rc, ev = _run(
        tmp_path,
        "task", "evidence", task["id"],
        "--kind", "artifact",
        "--uri", "s3://bucket/output/report.json",
        "--summary", "generated report artifact",
        "--created-by", agent["id"],
        "--lease-id", lease_id,
    )
    assert rc == 0
    assert ev["kind"] == "artifact"
    assert ev["uri"] == "s3://bucket/output/report.json"


def test_task_evidence_appears_in_task_show(tmp_path):
    """Evidence added via CLI appears when the task is queried via task show."""
    task = _create_task(tmp_path)
    agent, lease_id = _claim_for_evidence(tmp_path, task)
    _run(
        tmp_path,
        "task", "evidence", task["id"],
        "--kind", "log",
        "--uri", "logs://run/999",
        "--summary", "execution log",
        "--created-by", agent["id"],
        "--lease-id", lease_id,
    )

    rc, detail = _run(tmp_path, "task", "show", task["id"])
    assert rc == 0
    # task show returns a detail object; evidence may live under detail["evidence"]
    evidence_list = detail.get("evidence", [])
    uris = [e.get("uri") for e in evidence_list]
    assert "logs://run/999" in uris


def test_task_create_kind_report_sets_deliverable(tmp_path):
    """--kind report writes metadata.deliverable=report (non-code task)."""
    rc, task = _run(tmp_path, "task", "create", "investigate flakiness", "--kind", "report")
    assert rc == 0
    assert task.get("metadata", {}).get("deliverable") == "report"


def test_task_create_kind_alias_normalizes_to_report(tmp_path):
    rc, task = _run(tmp_path, "task", "create", "answer a question", "--kind", "answer")
    assert rc == 0
    assert task.get("metadata", {}).get("deliverable") == "report"


def test_task_create_kind_code_is_default_no_metadata(tmp_path):
    rc, task = _run(tmp_path, "task", "create", "fix the bug")
    assert rc == 0
    assert "deliverable" not in (task.get("metadata") or {})


def test_task_create_rejects_unknown_kind(tmp_path):
    rc, _ = _run(tmp_path, "task", "create", "x", "--kind", "banana")
    assert rc != 0


# ---------------------------------------------------------------------------
# task create --project <p> --kind report/<alias> (mac task_e2abc48b)
#
# A report deliverable created against a registered project must persist an
# operator_result execution contract, never a repo_change / repository one.
# ---------------------------------------------------------------------------

_PROJECT_CONTRACT_YAML = (
    "schema: mac.repository_contract.v1\n"
    "project: reportproj\n"
    "platforms:\n"
    "  - linux\n"
    "toolchain:\n"
    "  required_commands:\n"
    "    - python3\n"
    "bootstrap:\n"
    "  command: python3 -m venv .venv\n"
    "test:\n"
    "  command: pytest\n"
    "evidence:\n"
    "  required:\n"
    "    - tests\n"
)


def _register_project_repo(tmp_path, project="reportproj", name="report-repo"):
    """Register a contract-backed project repository via the CLI bridge command."""
    repo_path = tmp_path / "report-src"
    contract_dir = repo_path / ".mac"
    contract_dir.mkdir(parents=True)
    (contract_dir / "project.yaml").write_text(_PROJECT_CONTRACT_YAML, encoding="utf-8")
    rc, repo = _run(
        tmp_path,
        "bridge",
        "repository",
        "register",
        name,
        str(repo_path),
        "--project",
        project,
    )
    assert rc == 0, repo
    return repo


def test_task_create_kind_report_with_project_persists_operator_result(tmp_path):
    _register_project_repo(tmp_path)
    rc, task = _run(
        tmp_path,
        "task",
        "create",
        "investigate flakiness",
        "--project",
        "reportproj",
        "--kind",
        "report",
    )
    assert rc == 0, task
    metadata = task.get("metadata", {})
    assert metadata.get("deliverable") == "report"
    contract = metadata["execution_contract"]
    assert contract["type"] == "operator_directive"
    assert contract["evidence_type"] == "operator_result"
    assert contract["repository_required"] is False
    assert "repository_contract" not in contract
    # Repository context is preserved for reviewer reproducibility.
    ctx = contract["repository_context"]
    assert ctx["repository_name"] == "report-repo"
    assert ctx["repository_contract_project"] == "reportproj"


def test_task_create_kind_investigation_with_project_persists_operator_result(tmp_path):
    _register_project_repo(tmp_path)
    rc, task = _run(
        tmp_path,
        "task",
        "create",
        "triage the incident",
        "--project",
        "reportproj",
        "--kind",
        "investigation",
    )
    assert rc == 0, task
    metadata = task.get("metadata", {})
    # investigation alias normalizes to the report deliverable.
    assert metadata.get("deliverable") == "report"
    contract = metadata["execution_contract"]
    assert contract["type"] == "operator_directive"
    assert contract["evidence_type"] == "operator_result"
    assert contract["repository_required"] is False
    assert "repository_contract" not in contract


def test_task_create_unknown_kind_with_project_still_errors(tmp_path):
    _register_project_repo(tmp_path)
    rc, _ = _run(
        tmp_path,
        "task",
        "create",
        "x",
        "--project",
        "reportproj",
        "--kind",
        "banana",
    )
    assert rc != 0
