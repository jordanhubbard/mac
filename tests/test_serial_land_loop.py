"""The serial land loop: one land step per publication attempt, per repository.

One test gate decides each landing. A repository with required status checks
lands when they pass, waits while they are pending and blocks when they fail.
A repository without them lands only while the canonical tip is still the base
the worker's verifier ran on; a moved tip, or a conflict with it, sends the
SAME task back to its worker to rebase and retest -- the hub runs no tests --
at most ``LANDING_MAX_REBASES`` times before it blocks.
"""

from __future__ import annotations

import subprocess
import threading
from datetime import timedelta

import pytest

from mac import gitops, services
from mac.models import TaskState, ValidationError
from mac.models import utcnow
from tests.test_publication_pull_request import (
    FakeForge,
    build_repo,
    drive_to_approval,
    git,
    install_forge,
    published_detail,
)


@pytest.fixture()
def cp():
    return services.ControlPlane.in_memory()


def _advance_main(remote, tmp_path, name="other", *, path="other.txt", content="other\n"):
    """Someone else lands on main. Returns the new tip."""
    other = tmp_path / name
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remote), str(other)],
        check=True,
        capture_output=True,
    )
    git(other, "config", "user.email", "other@example.com")
    git(other, "config", "user.name", "Other Agent")
    (other / path).write_text(content, encoding="utf-8")
    git(other, "add", path)
    git(other, "commit", "-m", "someone else landed first")
    git(other, "push", "origin", "HEAD:refs/heads/main")
    return git(other, "rev-parse", "HEAD")


def _main(source):
    return git(source, "ls-remote", "origin", "refs/heads/main").split()[0]


def _landing(cp, task_id):
    return dict((cp.get_task(task_id).metadata or {}).get("landing") or {})


# ---------------------------------------------------------------------------
# Repositories with required checks: the forge's checks are the one gate.
# ---------------------------------------------------------------------------


def test_required_checks_pass_lands_even_when_the_tip_moved(cp, tmp_path, monkeypatch):
    """The checks are the gate; the hub does not second-guess them by asking the
    worker to rebase, and it runs no contract of its own."""
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    moved = _advance_main(remote, tmp_path)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    assert forge.merges == [{"number": 101, "method": "squash", "sha": task_head}]
    detail = published_detail(cp, task.id)
    assert detail["test_gate"] == "required_checks"
    assert detail["merge_serialization"] == "serial_land_loop"
    git(source, "fetch", "origin", "main")
    assert git(source, "rev-parse", "origin/main^") == moved


def test_required_checks_pending_waits_under_the_landing_deadline(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    forge.checks_pending = True
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "publish_failed"
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value
    landing = _landing(cp, task.id)
    # A wait charges the deadline, never an attempt.
    assert landing["attempts"] == 0
    assert landing["last_reason"] == "pull_request_checks_pending"
    assert forge.merges == []
    assert _main(source) == main_head


def test_required_checks_failed_blocks_naming_the_failing_checks(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    forge.checks_failed = ("sanity",)
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "publish_failed"
    assert result["blocked_reason"] == "landing_non_retryable"
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value
    landing = _landing(cp, task.id)
    assert "required checks failed" in landing["last_error"]
    assert "sanity" in landing["last_error"]
    assert forge.merges == []


# ---------------------------------------------------------------------------
# Repositories without required checks: the worker's verifier is the gate.
# ---------------------------------------------------------------------------


def test_no_checks_and_an_unchanged_tip_lands(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge, checks=())
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert forge.merges == [{"number": 101, "method": "squash", "sha": task_head}]
    detail = published_detail(cp, task.id)
    assert detail["test_gate"] == "worker_verifier"
    freshness = next(item for item in detail["commands"] if item["name"] == "land_freshness")
    assert freshness["verified_base_is_tip"] is True
    assert any(item["name"] == "revalidate_canonical_tip" for item in detail["commands"])


def test_no_checks_and_a_moved_tip_sends_the_same_task_back_to_rebase(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge, checks=())
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    moved = _advance_main(remote, tmp_path)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "rebase_required"
    assert result["reason"] == "canonical_moved"
    # Decided before any pull request was opened or merged.
    assert forge.opened == [] and forge.merges == []
    assert _main(source) == moved
    sent_back = cp.get_task(task.id)
    assert sent_back.id == task.id
    assert sent_back.state == TaskState.OPEN.value
    # Dispatchable again: the next claim re-runs the worker, whose finalizer
    # syncs onto the current tip before its verifier runs.
    assert cp.explain_task_dispatch(task.id)["task_ready"] is True
    assert sent_back.attempt_count < sent_back.max_attempts
    directive = sent_back.metadata["rebase_onto_tip"]
    assert directive["canonical_tip"] == moved
    assert directive["reviewed_head_sha"] == task_head
    assert directive["previous_remote_ref"] == "refs/heads/task/feature"
    assert directive["reason"] == "canonical_moved"
    assert directive["rebase"] == 1
    assert _landing(cp, task.id)["rebases"] == 1
    names = {event.name for event in cp.list_observability(limit=100)}
    assert "workflow.default_review.rebase_required" in names


def test_a_conflict_sends_the_task_back_to_rebase(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)  # even with required checks
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    _advance_main(remote, tmp_path, path="feature.txt", content="conflicting\n")

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "rebase_required"
    assert result["reason"] == "conflict"
    assert forge.opened == [] and forge.merges == []
    directive = cp.get_task(task.id).metadata["rebase_onto_tip"]
    assert directive["conflicted_files"] == ["feature.txt"]
    assert cp.get_task(task.id).state == TaskState.OPEN.value
    assert _landing(cp, task.id)["rebases"] == 1


def test_the_rebase_cap_blocks_the_task(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge, checks=())
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    metadata = dict(cp.get_task(task.id).metadata)
    metadata["landing"] = {
        "schema": services.LANDING_BUDGET_SCHEMA,
        "attempts": 0,
        "rebases": services.LANDING_MAX_REBASES,
        "evidence_id": evidence.id,
        "first_attempt_at": utcnow(),
    }
    cp._persist_task_metadata_narrow(task.id, metadata, actor="test")
    _advance_main(remote, tmp_path)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "publish_failed"
    assert result["blocked_reason"] == "landing_rebase_cap_exhausted"
    blocked = cp.get_task(task.id)
    assert blocked.state == TaskState.BLOCKED.value
    assert "rebase_onto_tip" not in blocked.metadata
    assert blocked.metadata["landing"]["outcome"] == "landing_rebase_cap_exhausted"


def test_rebased_evidence_keeps_the_rebase_count(cp):
    """New evidence resets the landing budget, but not the rebase count: the
    rebased run's evidence is exactly what a send-back asks for."""
    task = cp.create_task("t", metadata={"publication_target": "git://main"})
    cp._persist_task_metadata_narrow(
        task.id,
        {
            **task.metadata,
            "landing": {"attempts": 3, "rebases": 1, "evidence_id": "ev_old"},
        },
        actor="test",
    )
    with cp.store.transaction() as conn:
        conn.execute("UPDATE tasks SET state = ? WHERE id = ?", ("reviewing", task.id))

    assert (
        cp._consume_landing_budget(task.id, "pull_request_checks_pending", evidence_id="ev_new")
        is None
    )

    landing = _landing(cp, task.id)
    assert landing["evidence_id"] == "ev_new"
    assert landing["attempts"] == 1
    assert landing["rebases"] == 1


# ---------------------------------------------------------------------------
# Optimistic concurrency and per-repository serialization.
# ---------------------------------------------------------------------------


def test_a_tip_that_moves_during_the_merge_is_retried_not_merged(cp, tmp_path, monkeypatch):
    """The tip moves after the land step read it and before the merge request.
    The re-validation refuses the merge, the step retries at once, and the
    retry sees a tip the worker never verified: send back to rebase."""
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge, checks=())
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    moved: list[str] = []
    observe = forge.pull_request_state

    def move_main_once(*args, **kwargs):
        if not moved:
            moved.append(_advance_main(remote, tmp_path))
        return observe(*args, **kwargs)

    monkeypatch.setattr(gitops, "pull_request_state", move_main_once)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "rebase_required"
    assert result["canonical_tip"] == moved[0]
    assert forge.merges == []
    assert _main(source) == moved[0]


def test_land_steps_are_serialized_per_repository(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    held = threading.Event()
    release = threading.Event()

    def hold(url):
        with cp._repository_land_lock(url, "main"):
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold, args=(str(remote),))
    holder.start()
    try:
        assert held.wait(30)
        # The same repository waits; another repository is not held up.
        with pytest.raises(ValidationError) as busy:
            with cp._repository_land_lock(str(remote), "main"):
                pass
        assert busy.value.publication_failure_kind == "landing_serialized"
        with cp._repository_land_lock(str(tmp_path / "elsewhere.git"), "main"):
            pass

        result = cp.advance_default_review_workflow(task.id)

        assert result["status"] == "publish_failed"
        assert cp.get_task(task.id).state == TaskState.REVIEWING.value
        landing = _landing(cp, task.id)
        assert landing["last_reason"] == "landing_serialized"
        assert landing["attempts"] == 0
        assert forge.opened == [] and forge.merges == []
    finally:
        release.set()
        holder.join(30)

    # Once the other landing finishes, the next tick lands.
    metadata = dict(cp.get_task(task.id).metadata)
    metadata["landing"].pop("not_before", None)
    metadata.pop("publication_retry", None)
    cp._persist_task_metadata_narrow(task.id, metadata, actor="test")
    assert cp.advance_default_review_workflow(task.id)["status"] == "published"


# ---------------------------------------------------------------------------
# In-flight work from the retired native queue.
# ---------------------------------------------------------------------------


def test_a_task_parked_on_the_old_queue_lands_on_the_next_tick(cp, tmp_path, monkeypatch):
    """Approved tasks that were waiting on the native queue hold no queue state
    any more (migration 0005 dropped the tables). They are still REVIEWING with
    the queue's deferral in their publication retry metadata, and the next
    tick lands them through the land loop."""
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    past = (services.parse_time(utcnow()) - timedelta(minutes=5)).isoformat()
    metadata = dict(cp.get_task(task.id).metadata)
    metadata["publication_retry"] = {
        "schema": "mac.publication_retry.v1",
        "failure_kind": "merge_queue_deferred",
        "failed_at": past,
        "not_before": past,
        "retry_after_seconds": 300,
        "error": "mac merge queue deferred publication: window full",
    }
    cp._persist_task_metadata_narrow(task.id, metadata, actor="test")

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    assert forge.merges == [{"number": 101, "method": "squash", "sha": task_head}]
