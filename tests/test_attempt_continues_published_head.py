"""A task with an open pull request continues from that pull request's head.

Live 2026-10-04, Aviation task_b3e16b5f: PR #301 was at d5940f5 with 5 of 6
required checks green after three check-fix rounds. The reopened attempt was
prepared from ``main``, built d1136bb5 with only the dependency bump, and the
hub's PR reuse (``fix_failed_checks``) force-moved PR #301 onto it -- dropping
every earlier round's commits and turning two checks red again.

The worker now starts such an attempt from the published head (rebased onto
the canonical tip when it applies cleanly), so the head the hub later moves
the pull request to descends from every earlier round.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mac import services
from mac.executor_prompt import build_task_prompt
from mac.hermes_adapter import MacApiClient
from mac.models import TaskState
from mac.worker import MacWorker, WorkerExecution
from tests.test_publication_pull_request import build_repo, drive_to_approval, git, install_forge
from tests.test_required_checks_send_back import PullRequestForge, _resubmit


@pytest.fixture()
def cp():
    return services.ControlPlane.in_memory()


@pytest.fixture(autouse=True)
def _isolated_mac_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "mac-home"
    home.mkdir()
    monkeypatch.setenv("MAC_HOME", str(home))


def _sent_back(cp, tmp_path, monkeypatch):
    """Round 1 published ``task/feature`` as PR #101; its checks failed."""
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = PullRequestForge(remote, tmp_path / "forge")
    forge.pr_head_ref = "task/feature"
    forge.checks_failed = ("sanity",)
    install_forge(monkeypatch, forge)
    task, _evidence, _reviewer = drive_to_approval(cp, source, task_head)
    assert cp.advance_default_review_workflow(task.id)["status"] == "required_checks_failed"
    return source, main_head, task_head, forge, task


def _prepare(tmp_path, cp, task_id, lease_id):
    """What the worker hands the agent for the task's next attempt."""
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=lambda *_args, **_kwargs: {}),
        "agent-continue",
        tmp_path / ("workspaces-" + lease_id),
        lambda *_args: WorkerExecution(0, "unused"),
        attestation_key="test-key",
    )
    task = cp.get_task(task_id).to_dict()
    worker._prepare_task_workspace(task, {"id": lease_id})
    runtime = task["metadata"]["runtime"]
    return Path(runtime["repository_worktree"]), runtime, build_task_prompt(task)


def _advance_main(source, name, content="main moved\n"):
    (source / name).write_text(content, encoding="utf-8")
    git(source, "add", name)
    git(source, "commit", "-m", "main moved: %s" % name)
    git(source, "push", "origin", "main")
    return git(source, "rev-parse", "HEAD")


def test_check_fix_round_starts_from_the_published_head(cp, tmp_path, monkeypatch):
    source, main_head, task_head, forge, task = _sent_back(cp, tmp_path, monkeypatch)

    worktree, runtime, prompt = _prepare(tmp_path, cp, task.id, "lease-round-2")

    continuation = runtime["repository_continuation"]
    assert continuation["status"] == "continued"
    assert continuation["pull_request_number"] == 101
    assert continuation["published_head_sha"] == task_head
    # The attempt starts AT round 1's head; the canonical base is unchanged.
    assert git(worktree, "rev-parse", "HEAD") == task_head
    assert runtime["repository_base_sha"] == main_head
    assert (worktree / "feature.txt").read_text(encoding="utf-8") == "feature\n"
    assert "Continuing from your published work" in prompt
    assert "round 1" in prompt
    assert "do not redo, revert or drop them" in prompt

    # Round 2's fix is a commit on top; the hub moves PR #101 onto it.
    (worktree / "fix.txt").write_text("fixed\n", encoding="utf-8")
    git(worktree, "add", "fix.txt")
    git(worktree, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-m", "fix the check")
    fixed = git(worktree, "rev-parse", "HEAD")
    git(worktree, "push", "origin", "HEAD:refs/heads/task/fix")
    _resubmit(
        cp,
        task.id,
        fixed,
        pull_request={
            "opened": True,
            "number": 101,
            "url": "https://github.invalid/acme/widgets/pull/101",
            "base": "main",
            "forge": "github",
        },
    )
    forge.checks_failed = ()

    assert cp.advance_default_review_workflow(task.id)["status"] == "published"

    # The pull request moved forward, not sideways: round 1 survives.
    pr_head = git(source, "ls-remote", "origin", "refs/heads/task/feature").split()[0]
    assert pr_head == fixed
    git(source, "fetch", "origin")
    git(source, "merge-base", "--is-ancestor", task_head, pr_head)
    assert forge.merges == [{"number": 101, "method": "squash", "sha": fixed}]
    assert git(source, "show", "origin/main:feature.txt") == "feature"
    assert git(source, "show", "origin/main:fix.txt") == "fixed"


def test_reopened_attempt_keeps_earlier_rounds_rebased_onto_main(cp, tmp_path, monkeypatch):
    source, main_head, task_head, _forge, task = _sent_back(cp, tmp_path, monkeypatch)
    cp._transition_task_internal(task.id, TaskState.BLOCKED.value, "test", {"reason": "test"})
    cp.reopen_task(task.id, "operator", reason="retry the dependency bump")
    assert cp.get_task(task.id).state == TaskState.OPEN.value
    new_main = _advance_main(source, "bump.txt")

    worktree, runtime, prompt = _prepare(tmp_path, cp, task.id, "lease-reopened")

    continuation = runtime["repository_continuation"]
    assert continuation["status"] == "rebased"
    assert continuation["canonical_tip"] == new_main
    head = git(worktree, "rev-parse", "HEAD")
    assert continuation["head_sha"] == head
    git(worktree, "merge-base", "--is-ancestor", new_main, head)
    assert (worktree / "feature.txt").read_text(encoding="utf-8") == "feature\n"
    assert (worktree / "bump.txt").read_text(encoding="utf-8") == "main moved\n"
    assert "feature branch" in git(worktree, "log", "--format=%s", "%s..HEAD" % new_main)
    assert "Continuing from your published work" in prompt
    assert "rebased onto %s" % new_main[:12] in prompt


@pytest.mark.parametrize("outcome", ["closed", "merged", "branch_deleted"])
def test_merged_or_closed_pull_request_starts_from_main(cp, tmp_path, monkeypatch, outcome):
    source, main_head, task_head, forge, task = _sent_back(cp, tmp_path, monkeypatch)
    canonical = main_head
    if outcome == "closed":
        forge.pr_state = "closed"
    elif outcome == "merged":
        canonical = forge.land_from_queue(task_head)
    else:
        git(source, "push", "origin", "--delete", "task/feature")

    worktree, runtime, prompt = _prepare(tmp_path, cp, task.id, "lease-" + outcome)

    continuation = runtime["repository_continuation"]
    assert continuation["status"] == "fallback_canonical"
    assert continuation["reason"]
    assert git(worktree, "rev-parse", "HEAD") == canonical
    assert runtime["repository_base_sha"] == canonical
    assert "Continuing from your published work" not in prompt
    if outcome != "merged":
        assert not (worktree / "feature.txt").exists()


def test_conflict_with_main_starts_from_the_unrebased_published_head(cp, tmp_path, monkeypatch):
    source, main_head, task_head, _forge, task = _sent_back(cp, tmp_path, monkeypatch)
    new_main = _advance_main(source, "feature.txt", "main's own feature\n")

    worktree, runtime, prompt = _prepare(tmp_path, cp, task.id, "lease-conflict")

    continuation = runtime["repository_continuation"]
    assert continuation["status"] == "conflict"
    assert continuation["canonical_tip"] == new_main
    # Nothing is lost: the published head, as it was, with a clean worktree.
    assert git(worktree, "rev-parse", "HEAD") == task_head
    assert git(worktree, "status", "--porcelain") == ""
    assert (worktree / "feature.txt").read_text(encoding="utf-8") == "feature\n"
    assert "NOT rebased" in prompt
    assert "Integrate the default branch first" in prompt
