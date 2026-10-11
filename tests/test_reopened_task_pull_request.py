"""A reopened attempt lands through the task's open pull request.

Every attempt pushes from its own lease-suffixed branch, and the agent's
pull-request lookup reuses the task's open PR by task id. So a reopened
attempt's evidence names a PR whose head branch is still the earlier
attempt's, and the forge runs checks only on that head. Live on 2026-10-03
(task_f042b8dc): attempt 2 of task_5e8b4f58 pushed 782db394 to a new branch
while PR #919 still pointed at the old one, and the hub waited more than an
hour on checks that never reported. The land step now moves the PR's head
branch to the reviewed head, as it already did for a check-fix attempt.
"""

from __future__ import annotations

import pytest

from mac import gitops, services
from mac.models import TaskState
from tests.test_publication_pull_request import (
    build_repo,
    drive_to_approval,
    git,
    install_forge,
    published_detail,
)
from tests.test_required_checks_send_back import PullRequestForge


@pytest.fixture()
def cp():
    return services.ControlPlane.in_memory()


class OwnedPullRequestForge(PullRequestForge):
    """PR #101 is open on the first attempt's branch and names its task."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pr_head_ref = "task/feature"
        self.owner_task_id = ""

    def pull_request_state(self, repo_url, number, **_):
        state = super().pull_request_state(repo_url, number)
        state["task_id"] = self.owner_task_id
        state["host"] = "github"
        return state


_REUSED_PR = {
    "opened": True,
    "number": 101,
    "url": "https://github.invalid/acme/widgets/pull/101",
    "base": "main",
    "forge": "github",
    "reused": True,
}


def _second_attempt(source, branch="task/feature-lease_2"):
    """The reopened attempt's work, pushed from its own lease branch."""
    git(source, "checkout", "-b", branch, "main")
    (source / "feature.txt").write_text("feature, second attempt\n", encoding="utf-8")
    git(source, "add", "feature.txt")
    git(source, "commit", "-m", "second attempt")
    head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", branch)
    git(source, "checkout", "main")
    return head


def _approve_second_attempt(cp, source, head, branch="task/feature-lease_2"):
    return drive_to_approval(
        cp,
        source,
        head,
        pull_request=_REUSED_PR,
        repo_extra={"remote_ref": "refs/heads/%s" % branch},
    )


def _spy(monkeypatch):
    """Record what the reopened-PR lookup decided on each landing attempt."""
    calls = []
    original = services.ControlPlane._reopened_task_pull_request

    def spy(self, *args, **kwargs):
        outcome = original(self, *args, **kwargs)
        calls.append(outcome)
        return outcome

    monkeypatch.setattr(services.ControlPlane, "_reopened_task_pull_request", spy)
    return calls


def _branch_head(source, branch):
    listed = git(source, "ls-remote", "origin", "refs/heads/%s" % branch).split()
    return listed[0] if listed else ""


def test_a_reopened_attempt_lands_through_the_tasks_open_pull_request(cp, tmp_path, monkeypatch):
    remote, source, main_head, first_head = build_repo(tmp_path)
    forge = OwnedPullRequestForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    second_head = _second_attempt(source)
    task, evidence, reviewer = _approve_second_attempt(cp, source, second_head)
    forge.owner_task_id = task.id

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    # The PR's head branch now carries the reviewed head, so its checks ran
    # there and it merged at that head. No second PR was opened.
    assert _branch_head(source, "task/feature") == second_head
    assert forge.verified[-1]["sha"] == second_head
    assert forge.merges == [{"number": 101, "method": "squash", "sha": second_head}]
    assert forge.opened == []
    detail = published_detail(cp, task.id)
    moved = next(
        item for item in detail["commands"] if item["name"] == "reopened_task_pull_request"
    )
    assert moved["reused"] is True
    assert moved["head"] == "task/feature"
    assert moved["source_branch"] == "task/feature-lease_2"
    assert moved["owner_task_id"] == task.id
    opened = next(item for item in detail["commands"] if item["name"] == "open_pull_request")
    assert opened["opened_by"] == "agent"
    assert opened["head"] == "task/feature"


def test_another_tasks_pull_request_is_never_moved(cp, tmp_path, monkeypatch):
    remote, source, main_head, first_head = build_repo(tmp_path)
    forge = OwnedPullRequestForge(remote, tmp_path / "forge")
    forge.owner_task_id = "task_" + "0" * 32
    install_forge(monkeypatch, forge)
    second_head = _second_attempt(source)
    task, evidence, reviewer = _approve_second_attempt(cp, source, second_head)
    calls = _spy(monkeypatch)

    cp.advance_default_review_workflow(task.id)

    # A stale reference to someone else's PR keeps the existing fallback: the
    # hub opens a PR for the attempt's own branch and leaves #101 alone.
    assert _branch_head(source, "task/feature") == first_head
    assert [item["head"] for item in forge.opened] == ["task/feature-lease_2"]
    assert calls and all(outcome is None for outcome in calls)


@pytest.mark.parametrize("pr_state", ["closed", "merged"])
def test_a_closed_or_merged_pull_request_is_not_moved(cp, tmp_path, monkeypatch, pr_state):
    remote, source, main_head, first_head = build_repo(tmp_path)
    forge = OwnedPullRequestForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    second_head = _second_attempt(source)
    task, evidence, reviewer = _approve_second_attempt(cp, source, second_head)
    forge.owner_task_id = task.id
    if pr_state == "merged":
        forge.queue_merged_sha = first_head
    else:
        forge.pr_state = "closed"

    calls = _spy(monkeypatch)

    cp.advance_default_review_workflow(task.id)

    assert calls and all(outcome is None for outcome in calls)
    assert _branch_head(source, "task/feature") == first_head


def test_pull_request_state_names_the_task_that_owns_the_pull_request(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    task_id = "task_" + "a" * 32
    other = "task_" + "b" * 32
    payload = {
        "state": "open",
        "head": {"ref": "task/feature", "sha": "f" * 40},
        # The body marker wins over an earlier id-shaped token in the title.
        "title": "Integrate %s onto main (%s)" % (other, task_id),
        "body": "Opened by the MAC agent that produced this change.\n\n- task: `%s`\n" % task_id,
    }
    monkeypatch.setattr(gitops, "_http_get_json", lambda url, headers, timeout=20.0: payload)

    state = gitops.pull_request_state("https://github.com/acme/widgets.git", 101)

    assert state["task_id"] == task_id
    assert state["head_ref"] == "task/feature"

    payload["body"] = ""
    payload["title"] = "a human's pull request"
    assert gitops.pull_request_state("https://github.com/acme/widgets.git", 101)["task_id"] == ""


def _delete_evidence_branch(source, branch="task/feature-lease_2"):
    git(source, "push", "origin", "--delete", branch)


def test_deleted_evidence_ref_uses_the_merged_task_pull_request(cp, tmp_path, monkeypatch):
    remote, source, main_head, first_head = build_repo(tmp_path)
    forge = OwnedPullRequestForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    second_head = _second_attempt(source)
    task, evidence, reviewer = drive_to_approval(
        cp,
        source,
        second_head,
        pull_request=_REUSED_PR,
        repo_extra={
            "remote_ref": "refs/heads/task/feature-lease_2",
            "remote_url": str(remote),
        },
    )
    forge.owner_task_id = task.id
    merged = forge.land_from_queue(second_head)
    _delete_evidence_branch(source)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert result["canonical_sha"] == merged
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value


def test_deleted_evidence_ref_and_closed_pr_blocks_and_reports_once(cp, tmp_path, monkeypatch):
    remote, source, main_head, first_head = build_repo(tmp_path)
    forge = OwnedPullRequestForge(remote, tmp_path / "forge")
    forge.pr_state = "closed"
    install_forge(monkeypatch, forge)
    second_head = _second_attempt(source)
    task, evidence, reviewer = drive_to_approval(
        cp,
        source,
        second_head,
        pull_request=_REUSED_PR,
        repo_extra={
            "remote_ref": "refs/heads/task/feature-lease_2",
            "remote_url": str(remote),
        },
    )
    forge.owner_task_id = task.id
    _delete_evidence_branch(source)

    result = cp.advance_default_review_workflow(task.id)
    again = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "blocked"
    assert result["reason"] == "pull_request_closed_unmerged"
    assert again["status"] == "not_reviewable"
    reports = [
        item for item in cp.list_observability(limit=100) if item.name == "agent.report.filed"
    ]
    assert len(reports) == 1


def test_merged_pr_commit_not_on_canonical_branch_is_rejected(cp, tmp_path, monkeypatch):
    remote, source, main_head, first_head = build_repo(tmp_path)
    forge = OwnedPullRequestForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    second_head = _second_attempt(source)
    task, evidence, reviewer = drive_to_approval(
        cp,
        source,
        second_head,
        pull_request=_REUSED_PR,
        repo_extra={
            "remote_ref": "refs/heads/task/feature-lease_2",
            "remote_url": str(remote),
        },
    )
    forge.owner_task_id = task.id
    forge.queue_merged_sha = second_head
    git(source, "push", "origin", "%s:refs/heads/not-main" % second_head)
    _delete_evidence_branch(source)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "blocked"
    assert result["reason"] == "merged_pull_request_not_on_canonical_branch"
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value
