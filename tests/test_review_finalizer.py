"""Contract tests for the shared deterministic review finalizer bridge."""

from __future__ import annotations

import json

import pytest

from mac import review_finalizer


def _write_task(tmp_path, payload) -> None:
    (tmp_path / "task.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(("status", "expected"), [("complete", 0), ("failed", 1)])
def test_main_runs_finalizer_and_returns_manifest_status(
    tmp_path, monkeypatch, status, expected
) -> None:
    review_context = {"executor_evidence_id": "ev-executor"}
    task = {"id": "task", "metadata": {"review_context": review_context}}
    _write_task(tmp_path, {"task": task})
    monkeypatch.setenv("MAC_TASK_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("MAC_TASK_FILE", raising=False)
    captured = {}

    def finalize(workspace, received_task, received_context):
        captured.update(
            workspace=workspace,
            task=received_task,
            review_context=received_context,
        )
        (workspace / "mac-evidence.json").write_text(
            json.dumps({"status": status}), encoding="utf-8"
        )

    monkeypatch.setattr(review_finalizer, "run_deterministic_review_verdict", finalize)

    assert review_finalizer.main() == expected
    assert captured == {
        "workspace": tmp_path,
        "task": task,
        "review_context": review_context,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"task": "not-a-task"},
        {"task": {"id": "task", "metadata": {}}},
    ],
)
def test_main_rejects_missing_review_context(tmp_path, monkeypatch, payload) -> None:
    _write_task(tmp_path, payload)
    monkeypatch.setenv("MAC_TASK_WORKSPACE", str(tmp_path))
    with pytest.raises(SystemExit, match="requires a review task"):
        review_finalizer.main()


def test_main_requires_finalizer_manifest(tmp_path, monkeypatch) -> None:
    task_file = tmp_path / "custom-task.json"
    task_file.write_text(
        json.dumps({"metadata": {"review_context": {"id": "review"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAC_TASK_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MAC_TASK_FILE", str(task_file))
    monkeypatch.setattr(review_finalizer, "run_deterministic_review_verdict", lambda *_a: None)

    with pytest.raises(SystemExit, match="did not produce"):
        review_finalizer.main()


def _git_review(repo, *args) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_review_checkout(root):
    """A prepared review checkout: a git repo whose HEAD is the executor commit."""
    repo = root / "review_worktree"
    repo.mkdir()
    _git_review(repo, "init", "-q")
    _git_review(repo, "config", "user.email", "exec@example.com")
    _git_review(repo, "config", "user.name", "Executor")
    (repo / "shipped.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git_review(repo, "add", "-A")
    _git_review(repo, "commit", "-q", "-m", "executor change")
    return repo


def test_review_verdict_finalizer_does_not_touch_new_files_in_review_checkout(
    tmp_path, monkeypatch
) -> None:
    """`run_deterministic_review_verdict` verifies the executor commit is
    present in the review checkout; it must NOT itself stage/commit files there.
    Intended new files are the executor/worker path's responsibility (already
    committed into the executor commit whose SHA the review path verifies).

    This models the ACTUAL source behavior: the review finalizer reads git
    (cat-file -e / rev-parse HEAD) but never runs `git add`/`git commit` in the
    review checkout, so a stray uncommitted new file is neither committed nor
    silently folded into the verdict's committed state. The approval is driven
    by HEAD == executor-commit + independent checks, proving intended new files
    are not dropped: they must already be in the verified executor commit."""
    from mac import executor_finalizer

    review_repo = _init_review_checkout(tmp_path)
    exec_head = _git_review(review_repo, "rev-parse", "HEAD")

    # A stray uncommitted NEW file sitting in the review checkout.
    (review_repo / "stray_new.py").write_text("STRAY = 1\n", encoding="utf-8")
    assert "stray_new.py" in _git_review(review_repo, "status", "--porcelain")

    # Semantic reviewer approved.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "mac-evidence.json").write_text(
        json.dumps(
            {
                "schema": "mac.worker_evidence.v1",
                "status": "complete",
                "evidence_type": "review_verdict",
                "verdict": "approved",
            }
        ),
        encoding="utf-8",
    )
    # Executor evidence naming the exact commit HEAD points at.
    (workspace / "executor-evidence.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "verification": {
                        "repo": {"head_sha": exec_head, "files_changed": ["shipped.py"]}
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("MAC_ATTESTATION_KEY", "test-attestation-key")
    monkeypatch.setenv("MAC_WORKER_AGENT_ID", "agent-reviewer")
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(review_repo))

    # Make the heavy independent checks hermetic + passing so the finalizer
    # reaches its verdict without running real bootstrap/tests.
    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        executor_finalizer, "_run_repository_bootstrap_if_needed", lambda *a, **k: None
    )
    monkeypatch.setattr(executor_finalizer, "run_with_stall_watchdog", lambda *a, **k: _Proc())
    monkeypatch.setattr(executor_finalizer, "_cooperative_integration_check", lambda *a, **k: None)
    monkeypatch.setattr(executor_finalizer, "_review_experiment_assignment", lambda *a, **k: None)

    task = {"id": "task-review", "owner_agent_id": "agent-reviewer"}
    review_context = {"executor_evidence_id": "ev-exec", "review_id": "rv-1"}

    review_finalizer.run_deterministic_review_verdict(workspace, task, review_context)

    # The finalizer must NOT have committed the stray new file: the review
    # checkout is verification-only for new-file handling.
    assert "stray_new.py" in _git_review(review_repo, "status", "--porcelain")
    assert _git_review(review_repo, "rev-parse", "HEAD") == exec_head
    assert "stray_new.py" not in _git_review(review_repo, "ls-files")

    manifest = json.loads((workspace / "mac-evidence.json").read_text(encoding="utf-8"))
    # Verdict is driven by HEAD == executor commit (present + matching), proving
    # intended new files are carried by the verified executor commit, not
    # silently dropped by the review path.
    assert manifest["status"] == "complete"
    assert manifest["verdict"] == "approved"
    assert manifest["repo"]["head_sha"] == exec_head


def test_review_verdict_finalizer_rejects_when_executor_commit_absent(
    tmp_path, monkeypatch
) -> None:
    """If the executor commit is NOT present in the review checkout, the
    finalizer deterministically records a rejection rather than approving —
    it does not fabricate an approval over a missing commit. This is the
    contract that guards against intended (new-file-bearing) commits being
    silently dropped: a missing/mismatched commit fails the independent check."""
    from mac import executor_finalizer

    review_repo = _init_review_checkout(tmp_path)
    # A DIFFERENT sha the review checkout does not contain.
    missing_head = "0" * 40

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "mac-evidence.json").write_text(
        json.dumps(
            {
                "schema": "mac.worker_evidence.v1",
                "status": "complete",
                "evidence_type": "review_verdict",
                "verdict": "approved",
            }
        ),
        encoding="utf-8",
    )
    (workspace / "executor-evidence.json").write_text(
        json.dumps({"metadata": {"verification": {"repo": {"head_sha": missing_head}}}}),
        encoding="utf-8",
    )

    monkeypatch.setenv("MAC_ATTESTATION_KEY", "test-attestation-key")
    monkeypatch.setenv("MAC_WORKER_AGENT_ID", "agent-reviewer")
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(review_repo))
    monkeypatch.setattr(executor_finalizer, "_review_experiment_assignment", lambda *a, **k: None)

    task = {"id": "task-review", "owner_agent_id": "agent-reviewer"}
    review_context = {"executor_evidence_id": "ev-exec", "review_id": "rv-1"}

    review_finalizer.run_deterministic_review_verdict(workspace, task, review_context)

    manifest = json.loads((workspace / "mac-evidence.json").read_text(encoding="utf-8"))
    # Semantic approval cannot override a failed independent commit-presence
    # check: the verdict is rejected and the feedback names the missing commit.
    assert manifest["verdict"] == "rejected"
    assert "not present" in str(manifest.get("feedback") or "")
    # The review checkout was not mutated.
    assert _git_review(review_repo, "status", "--porcelain") == ""
