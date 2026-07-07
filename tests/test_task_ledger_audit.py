from __future__ import annotations

import subprocess
from pathlib import Path

from mac.task_ledger_audit import (
    TASK_LEDGER_AUDIT_SCHEMA,
    build_task_ledger_audit,
)


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Task Audit Test")
    _git(path, "config", "user.email", "audit@example.invalid")
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "base.txt")
    _git(path, "commit", "-m", "base")
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/remotes/origin/main", base)
    (path / "landed.txt").write_text("landed\n", encoding="utf-8")
    _git(path, "add", "landed.txt")
    _git(path, "commit", "-m", "landed")
    landed = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/remotes/origin/main", landed)
    _git(path, "checkout", "-b", "unmerged", base)
    (path / "unmerged.txt").write_text("unmerged\n", encoding="utf-8")
    _git(path, "add", "unmerged.txt")
    _git(path, "commit", "-m", "unmerged")
    unmerged = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "main")
    return path, landed, unmerged


def _task(
    task_id: str,
    state: str,
    *,
    dependencies: list[str] | None = None,
    metadata: dict | None = None,
) -> dict:
    return {
        "id": task_id,
        "title": task_id,
        "description": "",
        "project": "demo",
        "priority": 0,
        "state": state,
        "required_capabilities": [],
        "dependencies": dependencies or [],
        "metadata": metadata or {},
        "owner_agent_id": None,
        "lease_id": None,
        "leased_until": None,
        "attempt_count": 0,
        "max_attempts": 3,
        "started_at": None,
        "completed_at": "2026-01-01T00:00:00+00:00" if state in {"completed", "failed", "cancelled"} else None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:01+00:00",
    }


def _history(task_id: str, state: str, detail: dict | None = None) -> list[dict]:
    events = [
        {
            "id": "hist_%s_created" % task_id,
            "task_id": task_id,
            "event_type": "task.created",
            "actor": "human",
            "from_state": None,
            "to_state": "open",
            "detail": {},
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    if state != "open":
        events.append(
            {
                "id": "hist_%s_terminal" % task_id,
                "task_id": task_id,
                "event_type": "task.transitioned",
                "actor": "worker",
                "from_state": "open",
                "to_state": state,
                "detail": detail or {},
                "created_at": "2026-01-01T00:00:01+00:00",
            }
        )
    return events


def _repo_evidence(task_id: str, sha: str, *, published: bool = True) -> tuple[list[dict], list[dict]]:
    evidence_id = "ev_%s" % task_id
    evidence = [
        {
            "id": evidence_id,
            "task_id": task_id,
            "kind": "test",
            "uri": "artifact://evidence",
            "summary": "verified",
            "metadata": {
                "verification": {
                    "schema": "mac.worker_evidence.v1",
                    "status": "complete",
                    "evidence_type": "repo_change",
                    "repo": {
                        "head_sha": sha,
                        "pushed": True,
                        "dirty": False,
                        "remote_ref": "refs/heads/task",
                        "files_changed": ["src/example.py"],
                    },
                    "tests": [{"name": "tests", "returncode": 0}],
                }
            },
            "created_by": "worker",
            "created_at": "2026-01-01T00:00:01+00:00",
        }
    ]
    publications = (
        [
            {
                "id": "pub_%s" % task_id,
                "task_id": task_id,
                "target": "git://main",
                "status": "published",
                "evidence_id": evidence_id,
                "created_by": "reviewer",
                "created_at": "2026-01-01T00:00:02+00:00",
            }
        ]
        if published
        else []
    )
    return evidence, publications


def _detail(task: dict, *, history: list[dict] | None = None, evidence=None, publications=None) -> dict:
    return {
        "task": task,
        "history": history if history is not None else _history(task["id"], task["state"]),
        "evidence": evidence or [],
        "reviews": [],
        "publications": publications or [],
    }


def _registered_repo(path: Path) -> dict:
    return {
        "id": "projectrepo_demo",
        "name": "demo",
        "project": "demo",
        "path": str(path),
        "enabled": True,
        "metadata": {
            "repository_contract": {
                "schema": "mac.repository_contract.v1",
                "project": "demo",
                "canonical_remote_url": "git@example.invalid:demo/repo.git",
            }
        },
    }


def _repo_task_metadata() -> dict:
    return {
        "execution_contract": {
            "type": "repository",
            "repository_id": "projectrepo_demo",
        }
    }


def test_completed_repository_task_requires_canonical_ancestry(tmp_path):
    repo, landed, unmerged = _repo(tmp_path)
    landed_task = _task("task_" + "1" * 32, "completed", metadata=_repo_task_metadata())
    unmerged_task = _task("task_" + "2" * 32, "completed", metadata=_repo_task_metadata())
    landed_evidence, landed_publications = _repo_evidence(landed_task["id"], landed)
    unmerged_evidence, unmerged_publications = _repo_evidence(unmerged_task["id"], unmerged)

    report = build_task_ledger_audit(
        [
            _detail(landed_task, evidence=landed_evidence, publications=landed_publications),
            _detail(unmerged_task, evidence=unmerged_evidence, publications=unmerged_publications),
        ],
        [_registered_repo(repo)],
    )
    rows = {row["task_id"]: row for row in report["tasks"]}

    assert report["schema"] == TASK_LEDGER_AUDIT_SCHEMA
    assert rows[landed_task["id"]]["repository"]["integration_status"] == "ancestor"
    assert rows[landed_task["id"]]["assessment"]["verdict"] == "verified"
    assert rows[unmerged_task["id"]]["repository"]["integration_status"] == "not_integrated"
    assert rows[unmerged_task["id"]]["assessment"]["verdict"] == "contradiction"


def test_cancelled_entry_reason_ignores_later_self_transition_and_checks_replacement(tmp_path):
    repo, landed, _unmerged = _repo(tmp_path)
    replacement = _task("task_" + "3" * 32, "completed", metadata=_repo_task_metadata())
    replacement_evidence, replacement_publications = _repo_evidence(replacement["id"], landed)
    cancelled = _task("task_" + "4" * 32, "cancelled")
    cancelled["metadata"] = {
        "repository_ref_lifecycle": {"replacement_task_id": replacement["id"]}
    }
    cancelled_history = _history(
        cancelled["id"],
        "cancelled",
        {
            "reason": "superseded by consolidated implementation",
            "disposition": "superseded",
            "replacement_task_id": replacement["id"],
        },
    )
    cancelled_history.append(
        {
            "id": "hist_annotation",
            "task_id": cancelled["id"],
            "event_type": "task.updated",
            "actor": "reaudit",
            "from_state": "cancelled",
            "to_state": "cancelled",
            "detail": {},
            "created_at": "2026-01-02T00:00:00+00:00",
        }
    )

    report = build_task_ledger_audit(
        [
            _detail(cancelled, history=cancelled_history),
            _detail(
                replacement,
                evidence=replacement_evidence,
                publications=replacement_publications,
            ),
        ],
        [_registered_repo(repo)],
    )
    row = next(item for item in report["tasks"] if item["task_id"] == cancelled["id"])

    assert row["history"]["entry_reason"] == "superseded by consolidated implementation"
    assert row["replacement"]["state"] == "completed"
    assert row["replacement"]["assessment"] == "verified"
    assert row["assessment"]["verdict"] == "verified"


def test_failed_but_merged_and_stranded_blocked_tasks_are_contradictions(tmp_path):
    repo, landed, _unmerged = _repo(tmp_path)
    failed = _task("task_" + "5" * 32, "failed", metadata=_repo_task_metadata())
    failed_evidence, _ = _repo_evidence(failed["id"], landed, published=False)
    failed_repo = failed_evidence[0]["metadata"]["verification"]["repo"]
    failed_repo["base_sha"] = _git(repo, "rev-parse", "%s^" % landed)
    failed_repo["files_changed"] = ["landed.txt"]
    dependency = _task(
        "task_" + "6" * 32,
        "completed",
        metadata={"deliverable": "report"},
    )
    dependency_evidence = [
        {
            "id": "ev_report",
            "task_id": dependency["id"],
            "kind": "artifact",
            "uri": "artifact://report",
            "summary": "report complete",
            "metadata": {
                "verification": {
                    "schema": "mac.worker_evidence.v1",
                    "status": "complete",
                    "evidence_type": "operator_result",
                }
            },
            "created_by": "operator",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    blocked = _task(
        "task_" + "7" * 32,
        "blocked",
        dependencies=[dependency["id"]],
    )

    report = build_task_ledger_audit(
        [
            _detail(
                failed,
                history=_history(failed["id"], "failed", {"reason": "max attempts"}),
                evidence=failed_evidence,
            ),
            _detail(dependency, evidence=dependency_evidence),
            _detail(blocked, history=_history(blocked["id"], "blocked")),
        ],
        [_registered_repo(repo)],
    )
    rows = {row["task_id"]: row for row in report["tasks"]}

    assert rows[failed["id"]]["assessment"]["findings"] == [
        "failed_task_work_is_on_canonical_branch"
    ]
    assert rows[blocked["id"]]["dependencies"]["all_completed"] is True
    assert "blocked_with_all_dependencies_completed" in rows[blocked["id"]]["assessment"]["findings"]
    assert rows[blocked["id"]]["assessment"]["recommended_action"] == "reopen_stranded_blocked_task"


def test_snapshot_discloses_concurrent_task_changes():
    task = _task("task_" + "8" * 32, "open")
    end = dict(task)
    end["state"] = "claimed"
    end["updated_at"] = "2026-01-01T00:01:00+00:00"

    report = build_task_ledger_audit(
        [_detail(task)],
        [],
        start_tasks=[task],
        end_tasks=[end],
        verify_git=False,
    )

    assert report["snapshot"]["changed_during_run"] is True
    assert report["snapshot"]["updated_task_ids"] == [task["id"]]


def test_paired_state_audit_events_are_not_history_discontinuities():
    task = _task("task_" + "9" * 32, "open")
    history = _history(task["id"], "blocked", {"reason": "retry later"})
    history.extend(
        [
            {
                "id": "hist_transitioned",
                "task_id": task["id"],
                "event_type": "task.transitioned",
                "actor": "dispatcher",
                "from_state": "blocked",
                "to_state": "open",
                "detail": {"reason": "retry elapsed"},
                "created_at": "2026-01-01T00:01:00+00:00",
            },
            {
                "id": "hist_auto_reopened",
                "task_id": task["id"],
                "event_type": "task.auto_reopened",
                "actor": "dispatcher",
                "from_state": "blocked",
                "to_state": "open",
                "detail": {"reason": "retry elapsed"},
                "created_at": "2026-01-01T00:01:00+00:00",
            },
        ]
    )

    report = build_task_ledger_audit(
        [_detail(task, history=history)],
        [],
        verify_git=False,
    )
    row = report["tasks"][0]

    assert row["history"]["valid"] is True
    assert row["history"]["repeated_transition_event_count"] == 1


def test_canonical_task_commit_message_recovers_missing_evidence_sha(tmp_path):
    repo, _landed, _unmerged = _repo(tmp_path)
    task = _task("task_" + "a" * 32, "completed", metadata=_repo_task_metadata())
    (repo / "attributed.txt").write_text("done\n", encoding="utf-8")
    _git(repo, "add", "attributed.txt")
    _git(repo, "commit", "-m", "MAC task %s: implementation" % task["id"])
    attributed = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", attributed)

    report = build_task_ledger_audit(
        [_detail(task)],
        [_registered_repo(repo)],
    )
    row = report["tasks"][0]

    assert row["repository"]["proof_sha"] == attributed
    assert row["repository"]["claims"][0]["source"] == "canonical_commit_message"
    assert row["assessment"]["verdict"] == "verified"


def test_canonical_short_task_id_commit_message_recovers_legacy_publication(tmp_path):
    repo, _landed, _unmerged = _repo(tmp_path)
    task = _task("task_" + "b" * 32, "completed", metadata=_repo_task_metadata())
    (repo / "legacy-attributed.txt").write_text("done\n", encoding="utf-8")
    _git(repo, "add", "legacy-attributed.txt")
    _git(repo, "commit", "-m", "legacy publication for %s" % task["id"][:13])
    attributed = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", attributed)

    report = build_task_ledger_audit(
        [_detail(task)],
        [_registered_repo(repo)],
    )
    row = report["tasks"][0]

    assert row["repository"]["proof_sha"] == attributed
    assert (
        row["repository"]["claims"][0]["attribution_status"]
        == "task_id_prefix_in_commit_message"
    )
    assert row["assessment"]["verdict"] == "verified"


def test_canonical_short_task_id_in_other_task_prose_is_not_attribution(tmp_path):
    repo, _landed, _unmerged = _repo(tmp_path)
    task = _task("task_" + "d" * 32, "open", metadata=_repo_task_metadata())
    other_task = "task_" + "e" * 32
    (repo / "other-task.txt").write_text("done\n", encoding="utf-8")
    _git(repo, "add", "other-task.txt")
    _git(
        repo,
        "commit",
        "-m",
        "MAC task %s: reproduce failure on %s payload"
        % (other_task, task["id"][:13]),
    )
    unrelated = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", unrelated)

    report = build_task_ledger_audit(
        [_detail(task)],
        [_registered_repo(repo)],
    )
    row = report["tasks"][0]

    assert row["repository"]["proof_sha"] is None
    assert row["repository"]["claims"] == []
    assert row["assessment"]["verdict"] == "active_valid"


def test_canonical_exact_task_title_recovers_legacy_publication(tmp_path):
    repo, _landed, _unmerged = _repo(tmp_path)
    title = "Add deterministic navigation helper to the runtime"
    task = _task("task_" + "c" * 32, "completed", metadata=_repo_task_metadata())
    task["title"] = title
    (repo / "title-attributed.txt").write_text("done\n", encoding="utf-8")
    _git(repo, "add", "title-attributed.txt")
    _git(repo, "commit", "-m", title)
    attributed = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", attributed)

    report = build_task_ledger_audit(
        [_detail(task)],
        [_registered_repo(repo)],
    )
    row = report["tasks"][0]

    assert row["repository"]["proof_sha"] == attributed
    assert (
        row["repository"]["claims"][0]["attribution_status"]
        == "task_title_in_commit_message"
    )
    assert row["assessment"]["verdict"] == "verified"


def test_operator_adjudication_proves_code_and_operational_completion(tmp_path):
    repo, landed, _unmerged = _repo(tmp_path)
    code_task = _task("task_" + "d" * 32, "completed", metadata=_repo_task_metadata())
    operational_task = _task(
        "task_" + "e" * 32,
        "completed",
        metadata=_repo_task_metadata(),
    )
    details = [
        _detail(
            code_task,
            evidence=[
                {
                    "id": "ev_adjudicated_code",
                    "kind": "artifact",
                    "created_by": "task-ledger-audit",
                    "metadata": {
                        "schema": "mac.task_ledger_adjudication.v1",
                        "disposition": "completed_verified",
                        "canonical_sha": landed,
                    },
                }
            ],
        ),
        _detail(
            operational_task,
            evidence=[
                {
                    "id": "ev_adjudicated_ops",
                    "kind": "artifact",
                    "created_by": "task-ledger-audit",
                    "metadata": {
                        "schema": "mac.task_ledger_adjudication.v1",
                        "disposition": "completed_operationally_verified",
                        "scope": "operational",
                    },
                }
            ],
        ),
    ]

    report = build_task_ledger_audit(details, [_registered_repo(repo)])
    rows = {row["task_id"]: row for row in report["tasks"]}

    assert rows[code_task["id"]]["assessment"]["verdict"] == "verified"
    assert rows[code_task["id"]]["repository"]["proof_sha"] == landed
    assert rows[operational_task["id"]]["assessment"]["verdict"] == "verified"


def test_operator_cancellation_adjudication_resolves_semantic_review():
    task = _task("task_" + "f" * 32, "cancelled")
    detail = _detail(
        task,
        history=_history(task["id"], "cancelled", {"reason": "duplicate run"}),
        evidence=[
            {
                "id": "ev_cancel_adjudication",
                "kind": "artifact",
                "created_by": "task-ledger-audit",
                "metadata": {
                    "schema": "mac.task_ledger_adjudication.v1",
                    "disposition": "cancellation_confirmed",
                    "scope": "operational",
                },
            }
        ],
    )

    report = build_task_ledger_audit([detail], [], verify_git=False)

    assert report["tasks"][0]["assessment"]["verdict"] == "verified"


def test_blocked_dependency_chain_reports_terminal_failure():
    failed = _task("task_" + "b" * 32, "failed", metadata={"deliverable": "report"})
    child = _task(
        "task_" + "c" * 32,
        "blocked",
        dependencies=[failed["id"]],
    )
    parent = _task(
        "task_" + "d" * 32,
        "blocked",
        dependencies=[child["id"]],
    )

    report = build_task_ledger_audit(
        [
            _detail(failed, history=_history(failed["id"], "failed", {"reason": "tool missing"})),
            _detail(child, history=_history(child["id"], "blocked")),
            _detail(parent, history=_history(parent["id"], "blocked")),
        ],
        [],
        verify_git=False,
    )
    rows = {row["task_id"]: row for row in report["tasks"]}

    assert rows[parent["id"]]["dependencies"]["terminal_blockers"] == [
        {
            "task_id": failed["id"],
            "state": "failed",
            "path": [parent["id"], child["id"], failed["id"]],
        }
    ]
    assert rows[parent["id"]]["assessment"]["verdict"] == "contradiction"
    assert "blocked_by_failed_cancelled_or_missing_dependency" in rows[parent["id"]]["assessment"]["findings"]


def test_dependency_cycle_is_not_reported_as_valid_blocking():
    first = _task("task_" + "e" * 32, "blocked")
    second = _task("task_" + "f" * 32, "blocked")
    first["dependencies"] = [second["id"]]
    second["dependencies"] = [first["id"]]

    report = build_task_ledger_audit(
        [
            _detail(first, history=_history(first["id"], "blocked")),
            _detail(second, history=_history(second["id"], "blocked")),
        ],
        [],
        verify_git=False,
    )

    for row in report["tasks"]:
        assert row["dependencies"]["cycle_count"] == 1
        assert row["assessment"]["verdict"] == "contradiction"
        assert "blocked_by_dependency_cycle" in row["assessment"]["findings"]
