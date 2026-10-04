"""Publication lands through a reviewed pull request, not a push to main.

The old default had the hub merge an approved task branch locally and push the
result straight to the canonical branch. These tests pin the new default: the
agent's branch is pushed, a pull request is opened against the canonical
branch, and the *forge* squash-merges it. The hub never pushes main.

The forge itself is faked (no network, no credential), but everything below it
is real: a real bare git remote, a real task branch, and a fake merge that
performs an actual squash merge so the assertions are about real git history.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from mac import gitops
from mac.models import ReviewStatus, TaskState, ValidationError
from mac.services import ControlPlane
from tests.conftest import submit_review_verdict, verifier_test_item
from tests.test_control_plane import _sign, register_agent


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def build_repo(tmp_path: Path):
    """A bare remote with ``main`` and a pushed ``task/feature`` branch."""
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(source)], check=True, capture_output=True)
    git(source, "config", "user.email", "mac-test@example.com")
    git(source, "config", "user.name", "MAC Test")
    (source / "base.txt").write_text("base\n", encoding="utf-8")
    git(source, "add", "base.txt")
    git(source, "commit", "-m", "base")
    git(source, "branch", "-M", "main")
    git(source, "push", "-u", "origin", "main")
    main_head = git(source, "rev-parse", "HEAD")

    git(source, "checkout", "-b", "task/feature")
    (source / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(source, "add", "feature.txt")
    git(source, "commit", "-m", "feature branch")
    task_head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "task/feature")
    git(source, "checkout", "main")
    return remote, source, main_head, task_head


class FakeForge:
    """A forge that records PR calls and really squash-merges on request."""

    def __init__(
        self,
        remote: Path,
        workdir: Path,
        *,
        merge_blocked: str = "",
    ):
        self.remote = remote
        self.workdir = workdir
        self.merge_blocked = merge_blocked
        self.opened: list[dict] = []
        self.merges: list[dict] = []
        self.queue_merged_sha = ""
        self.verified: list[dict] = []
        self.checks_pending = False
        self.checks_failed: tuple = ()
        self.checks_known = True
        self.pr_head_ref = ""
        self.mergeable_state = ""
        self.update_conflict = False
        self.branch_updates: list[dict] = []
        self.closed: list[dict] = []
        self.close_error = ""
        self.failed_check_logs: dict = {}
        self.pr_state = "open"

    def failed_check_details(self, repo_url, sha, failed, **_):
        return [
            {
                "name": name,
                "conclusion": "failure",
                "details_url": "https://github.invalid/acme/widgets/actions/runs/1/job/%d" % index,
                "log_tail": self.failed_check_logs.get(name, ""),
            }
            for index, name in enumerate(failed)
        ]

    # -- required checks --------------------------------------------------
    def required_check_verdicts(self, repo_url, sha, contexts, **_):
        self.verified.append({"sha": sha, "contexts": list(contexts)})
        return {
            "known": self.checks_known,
            "contexts": list(contexts),
            "passed": [] if self.checks_pending else list(contexts),
            "pending": list(contexts) if self.checks_pending else [],
            "failed": list(self.checks_failed),
        }

    def pull_request_state(self, repo_url, number, **_):
        return {
            "known": True,
            "merged": bool(self.queue_merged_sha),
            "sha": self.queue_merged_sha,
            "state": "closed" if self.queue_merged_sha else self.pr_state,
            "head_sha": "",
            "head_ref": self.pr_head_ref,
            "mergeable_state": self.mergeable_state,
        }

    def update_pull_request_branch(self, repo_url, number, *, expected_head_sha=None, **_):
        """GitHub's update-branch: merge main into the PR branch, for real."""
        self.branch_updates.append({"number": number, "expected_head_sha": expected_head_sha})
        if self.update_conflict:
            return {"updated": False, "conflict": True, "reason": "merge conflict"}
        checkout = self.workdir / ("update-%d" % len(self.branch_updates))
        subprocess.run(
            ["git", "clone", "--branch", "task/feature", str(self.remote), str(checkout)],
            check=True,
            capture_output=True,
        )
        git(checkout, "config", "user.email", "forge@example.com")
        git(checkout, "config", "user.name", "Fake Forge")
        git(checkout, "fetch", "origin", "main")
        git(checkout, "merge", "--no-ff", "--no-edit", "origin/main")
        git(checkout, "push", "origin", "HEAD:refs/heads/task/feature")
        self.mergeable_state = ""
        return {"updated": True, "conflict": False, "reason": ""}

    def close_pull_request(self, repo_url, number, *, comment="", **_):
        if self.close_error:
            raise RuntimeError(self.close_error)
        self.closed.append({"repo_url": repo_url, "number": number, "comment": comment})

    def land_from_queue(self, sha: str, number: int = 101) -> str:
        """Land the PR behind publication's back (a human, or a dead attempt)."""
        self.queue_merged_sha = self._squash(sha, number)
        return self.queue_merged_sha

    def open_pull_request(self, repo_url, head, *, base=None, title=None, body=None):
        self.opened.append({"repo_url": repo_url, "head": head, "base": base, "title": title})
        return gitops.PullRequestResult(
            host="github",
            number=101,
            url="https://github.invalid/acme/widgets/pull/101",
            state="open",
        )

    def merge_pull_request(self, repo_url, number, *, method="squash", sha=None, **_):
        self.merges.append({"number": number, "method": method, "sha": sha})
        if self.merge_blocked:
            return gitops.PullRequestMergeResult(
                merged=False, number=number, blocked=True, reason=self.merge_blocked
            )
        return gitops.PullRequestMergeResult(
            merged=True, number=number, sha=self._squash(sha, number)
        )

    def _squash(self, sha, number) -> str:
        checkout = self.workdir / ("merge-%d" % (len(self.merges) + 1))
        subprocess.run(
            ["git", "clone", "--branch", "main", str(self.remote), str(checkout)],
            check=True,
            capture_output=True,
        )
        git(checkout, "config", "user.email", "forge@example.com")
        git(checkout, "config", "user.name", "Fake Forge")
        git(checkout, "fetch", "origin")
        git(checkout, "merge", "--squash", sha)
        git(checkout, "commit", "-m", "squashed (#%d)" % number)
        merged = git(checkout, "rev-parse", "HEAD")
        git(checkout, "push", "origin", "HEAD:refs/heads/main")
        return merged


def install_forge(monkeypatch, forge: FakeForge, *, checks=("sanity",), strict=False):
    monkeypatch.setattr(gitops, "resolve_forge", lambda url: "github")
    monkeypatch.setattr(gitops, "required_status_check_contexts", lambda url, branch: tuple(checks))
    monkeypatch.setattr(
        gitops,
        "required_status_check_policy",
        lambda url, branch: gitops.RequiredStatusChecks(tuple(checks), strict),
    )
    monkeypatch.setattr(gitops, "update_pull_request_branch", forge.update_pull_request_branch)
    monkeypatch.setattr(gitops, "close_pull_request", forge.close_pull_request)
    monkeypatch.setattr(gitops, "open_pull_request", forge.open_pull_request)
    monkeypatch.setattr(gitops, "merge_pull_request", forge.merge_pull_request)
    monkeypatch.setattr(gitops, "required_check_verdicts", forge.required_check_verdicts)
    monkeypatch.setattr(gitops, "pull_request_state", forge.pull_request_state)
    monkeypatch.setattr(gitops, "failed_check_details", forge.failed_check_details)


def drive_to_approval(cp, source: Path, task_head: str, *, pull_request=None, repo_extra=None):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    cp.create_project(
        "pr-publication",
        metadata={"repository_url": "https://github.com/acme/widgets.git"},
        dispatch_paused=False,
    )
    task = cp.create_task(
        "publish through a pull request",
        project="pr-publication",
        required_capabilities=["python"],
        metadata={
            "origin": {
                "type": "direct_task",
                "repository_path": str(source),
                "repository_contract": {
                    "schema": "mac.repository_contract.v1",
                    "default_branch": "main",
                    "test": {"command": "make suite"},
                },
            },
            "publication_target": "git://main",
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    manifest = _sign(
        cp,
        worker.id,
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "repo_change",
            "repo": {
                "head_sha": task_head,
                "pushed": True,
                "remote_ref": "refs/heads/task/feature",
                "dirty": False,
                "files_changed": ["feature.txt"],
                **({"pull_request": pull_request} if pull_request else {}),
                **(repo_extra or {}),
            },
            "tests": [verifier_test_item(task_head)],
        },
    )
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://feature",
        "feature branch tested",
        worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)
    return task, evidence, reviewer


def published_detail(cp, task_id):
    events = [
        event
        for event in cp.list_observability(limit=100)
        if event.name == "task.git_published" and event.subject_id == task_id
    ]
    assert events, "no git publication was recorded"
    return events[0].detail


def test_publication_opens_and_squash_merges_a_pull_request(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    publication = cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert publication.status == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value

    # A pull request was opened from the agent's branch onto main.
    assert len(forge.opened) == 1
    assert forge.opened[0]["head"] == "task/feature"
    assert forge.opened[0]["base"] == "main"
    assert task.id in forge.opened[0]["title"]

    # It was squash-merged, pinned to the reviewed head.
    assert forge.merges == [{"number": 101, "method": "squash", "sha": task_head}]

    detail = published_detail(cp, task.id)
    assert detail["publication_mode"] == "pull_request_squash"
    assert detail["pull_request_number"] == 101
    assert detail["pull_request_url"].endswith("/pull/101")
    assert detail["head_sha"] == task_head
    assert detail["contains_reviewed_head"] is False

    final = git(source, "ls-remote", "origin", "refs/heads/main").split()[0]
    assert final == detail["final_sha"]
    assert final != main_head
    # A squash: one parent, the old main tip -- and the reviewed commit is
    # deliberately NOT an ancestor.
    git(source, "fetch", "origin", "main")
    parents = git(source, "rev-list", "--parents", "-n", "1", final).split()
    assert parents[1:] == [main_head]
    assert (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", task_head, final],
            cwd=source,
            capture_output=True,
        ).returncode
        != 0
    )

    # The hub never pushed main itself.
    commands = detail["commands"]
    assert not any(item["name"] == "push_main_occ" for item in commands)
    strategy = next(item for item in commands if item["name"] == "publication_strategy")
    assert strategy["strategy"] == "pull_request"
    assert strategy["required_status_checks"] == ["sanity"]
    assert strategy["test_gate"] == "required_checks"
    # The hub runs no contract gate of its own: the PR's checks are the gate.
    assert not any(item["name"] == "publication_contract_gate" for item in commands)

    # The completion proof is honest about squashing and still admits the task.
    proofs = [
        item.metadata["verification"]["canonical_integration"]
        for item in cp.list_evidence(task.id)
        if item.metadata.get("verification", {}).get("canonical_integration")
    ]
    assert len(proofs) == 1
    assert proofs[0]["squash_merged"] is True
    assert proofs[0]["contains_reviewed_head"] is False
    assert proofs[0]["canonical_tip_sha"] == final
    assert proofs[0]["reviewed_head_sha"] == task_head


def test_stopping_an_admitted_publisher_fences_the_forge_mutation(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    admitted = threading.Event()
    resume = threading.Event()
    original_verdicts = forge.required_check_verdicts

    def pause_after_admission(*args, **kwargs):
        admitted.set()
        assert resume.wait(timeout=5)
        return original_verdicts(*args, **kwargs)

    monkeypatch.setattr(gitops, "required_check_verdicts", pause_after_admission)
    outcome = {}

    def publish():
        try:
            cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)
        except Exception as exc:  # noqa: BLE001 - asserted below
            outcome["error"] = exc

    publisher = threading.Thread(target=publish)
    publisher.start()
    assert admitted.wait(timeout=5)
    cp.stop_task(task.id, actor="operator", reason="publication hold")
    resume.set()
    publisher.join(timeout=10)

    assert not publisher.is_alive()
    assert getattr(outcome.get("error"), "publication_failure_kind", "") == (
        "publication_authority_revoked"
    )
    assert forge.merges == []
    assert git(source, "ls-remote", "origin", "refs/heads/main").split()[0] == main_head
    assert cp.get_task(task.id).state == TaskState.STOPPED.value
    assert cp.get_evidence(evidence.id).id == evidence.id

    restarted = cp.start_stopped_task(task.id, actor="operator")
    assert restarted.state == TaskState.OPEN.value
    assert cp.get_evidence(evidence.id).id == evidence.id


def _expire_publication_backoff(cp, task_id):
    """Let the next land step run now, as if every backoff had elapsed."""
    metadata = dict(cp.get_task(task_id).metadata)
    landing = dict(metadata.get("landing") or {})
    landing.pop("not_before", None)
    metadata["landing"] = landing
    metadata.pop("publication_retry", None)
    cp._persist_task_metadata_narrow(task_id, metadata, actor="test")


def test_revoked_publication_authority_retries_under_the_landing_budget(cp, tmp_path, monkeypatch):
    """Live 2026-10-03: an approved task's land step raised "git publication
    authority changed before forge mutation; a fresh review publication
    attempt is required". The landing budget read that bare ValidationError as
    non-retryable, so the task went BLOCKED and then FAILED. The message asks
    for a fresh attempt, and a fresh attempt is what the task now gets."""
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    review_state = cp.get_task(task.id).state
    original_verdicts = forge.required_check_verdicts
    touched = []

    def touch_task_once(*args, **kwargs):
        # A write to the task row between admission and the merge request:
        # the fence sees a different ``updated_at``.
        if not touched:
            touched.append(1)
            metadata = dict(cp.get_task(task.id).metadata)
            metadata["concurrent_note"] = "written mid-publication"
            cp._persist_task_metadata_narrow(task.id, metadata, actor="test")
        return original_verdicts(*args, **kwargs)

    monkeypatch.setattr(gitops, "required_check_verdicts", touch_task_once)

    first = cp.advance_default_review_workflow(task.id)

    assert first["status"] == "publish_failed"
    assert "fresh review publication attempt" in first["error"]
    assert "blocked_reason" not in first
    after = cp.get_task(task.id)
    assert after.state == review_state
    landing = dict(after.metadata["landing"])
    assert landing["attempts"] == 1
    assert landing["last_reason"] == "publication_authority_revoked"
    assert landing["not_before"]
    assert not landing.get("blocked_at")
    assert forge.merges == []
    assert git(source, "ls-remote", "origin", "refs/heads/main").split()[0] == main_head

    # Still backing off: the next tick does not retry yet.
    assert cp.advance_default_review_workflow(task.id)["status"] in {
        "publication_backoff",
        "landing_backoff",
    }

    # Once the backoff elapses, a fresh attempt re-reads the task (new
    # authority) and lands.
    _expire_publication_backoff(cp, task.id)
    second = cp.advance_default_review_workflow(task.id)

    assert second["status"] == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    assert len(forge.merges) == 1


def test_a_second_consumer_does_not_revoke_the_land_step_in_progress(cp, tmp_path, monkeypatch):
    """The sweep and the event-driven consumer can both reach the land step for
    one task. The loser used to record a ``landing_serialized`` wait on the
    task, bumping ``updated_at`` under the winner and revoking its authority
    fence just before the merge. The loser now writes nothing."""
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    admitted = threading.Event()
    resume = threading.Event()
    original_verdicts = forge.required_check_verdicts

    def pause_after_admission(*args, **kwargs):
        admitted.set()
        assert resume.wait(timeout=30)
        return original_verdicts(*args, **kwargs)

    monkeypatch.setattr(gitops, "required_check_verdicts", pause_after_admission)
    outcome = {}

    def land():
        outcome["result"] = cp.advance_default_review_workflow(task.id, actor="sweep")

    lander = threading.Thread(target=land)
    lander.start()
    try:
        assert admitted.wait(timeout=30)
        before = cp.get_task(task.id)
        loser = cp.advance_default_review_workflow(task.id, actor="event-driven-review")
        assert loser["status"] == "landing_in_progress"
        unchanged = cp.get_task(task.id)
        assert unchanged.updated_at == before.updated_at
        assert "landing" not in unchanged.metadata
        assert "publication_retry" not in unchanged.metadata
    finally:
        resume.set()
        lander.join(timeout=60)

    assert not lander.is_alive()
    assert outcome["result"]["status"] == "published"
    assert len(forge.merges) == 1


def test_publication_defers_while_the_pull_request_checks_are_pending(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(
        remote,
        tmp_path / "forge",
        merge_blocked='Required status check "sanity" is expected.',
    )
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    with pytest.raises(ValidationError) as excinfo:
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert "required checks" in str(excinfo.value)
    assert getattr(excinfo.value, "publication_failure_kind", "") == "pull_request_checks_pending"
    assert getattr(excinfo.value, "publication_retry_after_seconds", 0) > 0
    # The PR exists; main is untouched; the task is not completed.
    assert len(forge.opened) == 1
    assert git(source, "ls-remote", "origin", "refs/heads/main").split()[0] == main_head
    assert cp.get_task(task.id).state != TaskState.COMPLETED.value


def test_direct_push_opt_out_still_pushes_the_canonical_branch(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    monkeypatch.setenv("MAC_PUBLICATION_STRATEGY", "direct_push")
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    publication = cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert publication.status == "published"
    assert forge.opened == []
    assert forge.merges == []
    detail = published_detail(cp, task.id)
    assert detail["publication_mode"] in {"fast_forward", "merge_commit"}
    assert any(item["name"] == "push_main_occ" for item in detail["commands"])
    strategy = next(item for item in detail["commands"] if item["name"] == "publication_strategy")
    assert strategy["strategy"] == "direct_push"
    assert "opt-out" in strategy["reason"]
    final = git(source, "ls-remote", "origin", "refs/heads/main").split()[0]
    assert final != main_head
    git(source, "fetch", "origin", "main")
    git(source, "merge-base", "--is-ancestor", task_head, final)


def test_repository_without_a_forge_falls_back_to_direct_push(cp, tmp_path):
    # No monkeypatching: the canonical remote is a bare local path, so there is
    # no API to open a pull request against. Publication must still land.
    remote, source, main_head, task_head = build_repo(tmp_path)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    publication = cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert publication.status == "published"
    detail = published_detail(cp, task.id)
    strategy = next(item for item in detail["commands"] if item["name"] == "publication_strategy")
    assert strategy["strategy"] == "direct_push"
    assert "no API-reachable forge" in strategy["reason"]
    assert any(item["name"] == "push_main_occ" for item in detail["commands"])


def test_unknown_publication_strategy_is_rejected(cp, monkeypatch):
    monkeypatch.setenv("MAC_PUBLICATION_STRATEGY", "yolo")
    with pytest.raises(ValidationError, match="MAC_PUBLICATION_STRATEGY"):
        cp._resolve_publication_strategy("https://github.com/acme/widgets.git")


def test_pull_request_is_the_default_strategy(cp, monkeypatch):
    monkeypatch.delenv("MAC_PUBLICATION_STRATEGY", raising=False)
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "x" * 36)
    assert (
        cp._resolve_publication_strategy("https://github.com/acme/widgets.git")["strategy"]
        == "pull_request"
    )


# ---------------------------------------------------------------------------
# gitops forge helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///srv/git/widgets.git",
        "/srv/git/widgets.git",
        "git://example.invalid/widgets.git",
        "https://github.com/acme",
    ],
)
def test_resolve_forge_declines_remotes_without_a_reachable_api(url, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "x" * 36)
    assert gitops.resolve_forge(url) is None


def test_resolve_forge_requires_a_credential(monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "MAC_TASK_GIT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert gitops.resolve_forge("https://github.com/acme/widgets.git") is None
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "x" * 36)
    assert gitops.resolve_forge("https://github.com/acme/widgets.git") == "github"


def test_merge_pull_request_reports_gate_refusals_as_blocked(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "x" * 36)
    monkeypatch.setattr(
        gitops,
        "_http_put_json",
        lambda *a, **k: (405, {}, 'Required status check "sanity" is expected.'),
    )
    result = gitops.merge_pull_request("https://github.com/acme/widgets.git", 7, sha="a" * 40)
    assert result.blocked is True
    assert result.merged is False
    assert "sanity" in result.reason


def test_merge_pull_request_raises_on_a_real_error(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "x" * 36)
    monkeypatch.setattr(
        gitops, "_http_put_json", lambda *a, **k: (500, {}, "internal server error")
    )
    with pytest.raises(RuntimeError, match="internal server error"):
        gitops.merge_pull_request("https://github.com/acme/widgets.git", 7)


def test_merge_failures_never_echo_the_token(monkeypatch):
    token = "ghp_" + "s" * 36
    monkeypatch.setenv("GH_TOKEN", token)
    # A forge that reflects the Authorization header back into its error body.
    monkeypatch.setattr(
        gitops,
        "_http_put_json",
        lambda *a, **k: (403, {}, "bad credentials for token %s" % token),
    )
    with pytest.raises(RuntimeError) as excinfo:
        gitops.merge_pull_request("https://github.com/acme/widgets.git", 7)
    assert token not in str(excinfo.value)
    assert "***" in str(excinfo.value)

    monkeypatch.setattr(
        gitops,
        "_http_put_json",
        lambda *a, **k: (405, {}, "required status check pending for %s" % token),
    )
    blocked = gitops.merge_pull_request("https://github.com/acme/widgets.git", 7)
    assert blocked.blocked is True
    assert token not in blocked.reason


def test_required_status_check_contexts_reads_rulesets(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "x" * 36)
    monkeypatch.setattr(
        gitops,
        "_http_get_json",
        lambda *a, **k: [
            {"type": "pull_request", "parameters": {}},
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": "sanity"},
                        {"context": "compatibility"},
                        {"context": "sanity"},
                    ]
                },
            },
        ],
    )
    assert gitops.required_status_check_contexts("https://github.com/acme/widgets.git", "main") == (
        "sanity",
        "compatibility",
    )


def test_required_status_check_policy_reports_a_strict_ruleset(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "x" * 36)
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [{"context": "sanity"}],
            },
        }
    ]
    monkeypatch.setattr(gitops, "_http_get_json", lambda *a, **k: rules)
    policy = gitops.required_status_check_policy("https://github.com/acme/widgets.git", "main")
    assert policy == gitops.RequiredStatusChecks(contexts=("sanity",), strict=True)

    rules[0]["parameters"]["strict_required_status_checks_policy"] = False
    policy = gitops.required_status_check_policy("https://github.com/acme/widgets.git", "main")
    assert policy.strict is False
    assert gitops.required_status_check_contexts("https://github.com/acme/widgets.git", "main") == (
        "sanity",
    )


def test_update_pull_request_branch_reports_updated_and_conflict(monkeypatch):
    token = "ghp_" + "u" * 36
    monkeypatch.setenv("GH_TOKEN", token)
    calls = []

    def put(url, headers, body, timeout=30.0):
        calls.append((url, body))
        return responses.pop(0)

    responses = [
        (202, {"message": "Updating pull request branch."}, ""),
        (422, {}, "merge conflict between base and head"),
        (422, {}, "expected head sha didn't match current head ref %s" % token),
    ]
    monkeypatch.setattr(gitops, "_http_put_json", put)
    url = "https://github.com/acme/widgets.git"

    assert gitops.update_pull_request_branch(url, 7, expected_head_sha="a" * 40) == {
        "updated": True,
        "conflict": False,
        "reason": "",
    }
    assert calls[0] == (
        "https://api.github.com/repos/acme/widgets/pulls/7/update-branch",
        {"expected_head_sha": "a" * 40},
    )
    conflict = gitops.update_pull_request_branch(url, 7)
    assert conflict["updated"] is False and conflict["conflict"] is True
    moved = gitops.update_pull_request_branch(url, 7)
    assert moved["updated"] is False and moved["conflict"] is False
    assert token not in moved["reason"]


def test_close_pull_request_comments_then_closes(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "c" * 36)
    calls = []
    monkeypatch.setattr(
        gitops, "_http_post_json", lambda url, headers, body, **_: calls.append(("POST", url, body))
    )
    monkeypatch.setattr(
        gitops,
        "_http_patch_json",
        lambda url, headers, body, **_: calls.append(("PATCH", url, body)),
    )

    gitops.close_pull_request("https://github.com/acme/widgets.git", 9, comment="superseded")

    assert calls == [
        (
            "POST",
            "https://api.github.com/repos/acme/widgets/issues/9/comments",
            {"body": "superseded"},
        ),
        ("PATCH", "https://api.github.com/repos/acme/widgets/pulls/9", {"state": "closed"}),
    ]


def test_close_pull_request_failure_never_echoes_the_token(monkeypatch):
    token = "ghp_" + "t" * 36
    monkeypatch.setenv("GH_TOKEN", token)

    def refuse(url, headers, body, **_):
        raise RuntimeError("PATCH %s -> 403 bad credentials %s" % (url, token))

    monkeypatch.setattr(gitops, "_http_patch_json", refuse)
    with pytest.raises(RuntimeError) as excinfo:
        gitops.close_pull_request("https://github.com/acme/widgets.git", 9)
    assert token not in str(excinfo.value)


def test_required_status_check_contexts_is_unknown_without_credentials(monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "MAC_TASK_GIT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert (
        gitops.required_status_check_contexts("https://github.com/acme/widgets.git", "main") is None
    )


# ---------------------------------------------------------------------------
# The AGENT owns the pull request; the hub records it and gates completion.
# ---------------------------------------------------------------------------


def test_hub_reuses_the_pull_request_the_agent_opened(cp, tmp_path, monkeypatch):
    """Opening a PR is the agent's job. The hub must not open a second one."""
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(
        cp,
        source,
        task_head,
        pull_request={
            "opened": True,
            "forge": "github",
            "number": 77,
            "url": "https://github.invalid/acme/widgets/pull/77",
            "state": "open",
            "base": "main",
            "head": "task/feature",
            "opened_by": "agent",
        },
    )

    publication = cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert publication.status == "published"
    # The hub opened nothing; it merged the agent's PR, pinned to the head the
    # reviewer approved.
    assert forge.opened == []
    assert forge.merges == [{"number": 77, "method": "squash", "sha": task_head}]

    detail = published_detail(cp, task.id)
    assert detail["pull_request_number"] == 77
    assert detail["pull_request_opened_by"] == "agent"
    opened = next(item for item in detail["commands"] if item["name"] == "open_pull_request")
    assert opened["opened_by"] == "agent"

    proof = [
        item.metadata["verification"]["canonical_integration"]
        for item in cp.list_evidence(task.id)
        if item.metadata.get("verification", {}).get("canonical_integration")
    ][0]
    assert proof["squash_merged"] is True
    assert proof["contains_reviewed_head"] is False
    assert proof["pull_request_opened_by"] == "agent"


def test_hub_does_not_reuse_a_pr_number_pointing_at_a_different_branch(cp, tmp_path, monkeypatch):
    """Evidence can carry a PR number whose live head branch is not the one
    just pushed: a task-id marker collision when the agent's PR-open call
    reused a stale, unrelated PR (see #770 for how that misidentification
    happens). A GitHub PR's head branch is immutable, so reusing that
    number would silently keep pointing at the wrong branch forever while
    the real work sat on an unreferenced branch. Observed live on
    mac-fleet-canary: an approved task's publish repeatedly "succeeded" at
    reusing PR #1 while PR #1's actual head stayed a different task's
    stale, conflicting branch."""
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    forge.pr_head_ref = "some/other/task-branch"
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(
        cp,
        source,
        task_head,
        pull_request={
            "opened": True,
            "forge": "github",
            "number": 77,
            "url": "https://github.invalid/acme/widgets/pull/77",
            "state": "open",
            "base": "main",
            "head": "task/feature",
            "opened_by": "agent",
        },
    )

    publication = cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert publication.status == "published"
    # The cached PR #77 points at a different branch, so the hub must not
    # merge it -- it opens its own PR for the branch it actually pushed.
    assert forge.opened
    assert forge.merges == [{"number": 101, "method": "squash", "sha": task_head}]

    detail = published_detail(cp, task.id)
    assert detail["pull_request_number"] == 101
    assert detail["pull_request_opened_by"] == "hub_fallback"


def test_hub_fallback_pull_request_is_recorded_as_a_fallback(cp, tmp_path, monkeypatch):
    """A worker that opened no PR still publishes -- visibly, not silently."""
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    detail = published_detail(cp, task.id)
    opened = next(item for item in detail["commands"] if item["name"] == "open_pull_request")
    assert opened["opened_by"] == "hub_fallback"
    assert opened["agent_reason"]
    assert detail["pull_request_opened_by"] == "hub_fallback"


def test_agent_opens_the_pull_request_onto_the_contract_canonical_branch(tmp_path, monkeypatch):
    """canonical_branch comes from the contract; it is never assumed to be main."""
    calls: list[dict] = []

    def fake_open(repo_url, head, *, base=None, title=None, body=None):
        calls.append({"repo_url": repo_url, "head": head, "base": base, "title": title})
        return gitops.PullRequestResult(
            host="github", number=5, url="https://github.invalid/pull/5", state="open"
        )

    monkeypatch.setattr(gitops, "resolve_forge", lambda url: "github")
    monkeypatch.setattr(gitops, "open_pull_request", fake_open)
    target = gitops.CanonicalPublicationTarget(
        worktree=tmp_path,
        canonical_remote_url="https://github.com/acme/widgets.git",
        remote="https://github.com/acme/widgets.git",
        remote_display="https://github.com/acme/widgets.git",
        canonical_branch="release/v2",
        destination_branch="task/feature",
        prepared_base_sha="b" * 40,
        task_head_sha="a" * 40,
        isolated_ref="refs/mac/task",
        git_common_dir=tmp_path,
        lock_path=tmp_path / "lock",
    )

    outcome = gitops.agent_pull_request(
        target, task_id="task_1", task_title="widen the widget", head_sha="a" * 40
    )

    assert outcome["opened"] is True
    assert outcome["opened_by"] == "agent"
    assert outcome["number"] == 5
    assert calls == [
        {
            "repo_url": "https://github.com/acme/widgets.git",
            "head": "task/feature",
            "base": "release/v2",
            "title": "widen the widget (task_1)",
        }
    ]


def test_agent_pull_request_declines_without_a_forge_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(gitops, "resolve_forge", lambda url: None)
    target = gitops.CanonicalPublicationTarget(
        worktree=tmp_path,
        canonical_remote_url="file:///srv/git/widgets.git",
        remote="file:///srv/git/widgets.git",
        remote_display="file:///srv/git/widgets.git",
        canonical_branch="main",
        destination_branch="task/feature",
        prepared_base_sha="b" * 40,
        task_head_sha="a" * 40,
        isolated_ref="refs/mac/task",
        git_common_dir=tmp_path,
        lock_path=tmp_path / "lock",
    )
    outcome = gitops.agent_pull_request(target, task_id="task_1")
    assert outcome["opened"] is False
    assert "no API-reachable forge" in outcome["reason"]


def test_agent_pull_request_reports_forge_errors_without_the_token(tmp_path, monkeypatch):
    token = "ghp_" + "z" * 36
    monkeypatch.setenv("GH_TOKEN", token)
    monkeypatch.setattr(gitops, "resolve_forge", lambda url: "github")

    def boom(*a, **k):
        raise RuntimeError("bad credentials for token %s" % token)

    monkeypatch.setattr(gitops, "open_pull_request", boom)
    target = gitops.CanonicalPublicationTarget(
        worktree=tmp_path,
        canonical_remote_url="https://github.com/acme/widgets.git",
        remote="https://github.com/acme/widgets.git",
        remote_display="https://github.com/acme/widgets.git",
        canonical_branch="main",
        destination_branch="task/feature",
        prepared_base_sha="b" * 40,
        task_head_sha="a" * 40,
        isolated_ref="refs/mac/task",
        git_common_dir=tmp_path,
        lock_path=tmp_path / "lock",
    )
    outcome = gitops.agent_pull_request(target, task_id="task_1")
    assert outcome["opened"] is False
    assert token not in outcome["reason"]
    assert "***" in outcome["reason"]


# ---------------------------------------------------------------------------
# The credential: the agent's environment first, the hub's secret store second.
# ---------------------------------------------------------------------------


def _pr_target(tmp_path, remote="https://github.com/acme/widgets.git"):
    return gitops.CanonicalPublicationTarget(
        worktree=tmp_path,
        canonical_remote_url=remote,
        remote=remote,
        remote_display=remote,
        canonical_branch="main",
        destination_branch="task/feature",
        prepared_base_sha="b" * 40,
        task_head_sha="a" * 40,
        isolated_ref="refs/mac/task",
        git_common_dir=tmp_path,
        lock_path=tmp_path / "lock",
    )


class _FakeHubClient:
    def __init__(self, base_url, *, token=None, transport=None):
        self.base_url = base_url
        self.token = token
        _FakeHubClient.calls.append(base_url)

    calls: list[str] = []
    payload: dict = {}

    def request(self, method, path, body=None):
        _FakeHubClient.calls.append((method, path))
        return dict(_FakeHubClient.payload)


def _install_fake_hub(monkeypatch, payload):
    import mac.http_client as http_client

    _FakeHubClient.calls = []
    _FakeHubClient.payload = payload
    monkeypatch.setattr(http_client, "HubClient", _FakeHubClient)
    monkeypatch.setenv("MAC_API_URL", "https://hub.invalid")
    monkeypatch.setenv("MAC_API_TOKEN", "hub-token")
    return _FakeHubClient


def test_agent_asks_the_hub_for_the_forge_credential_when_its_env_has_none(tmp_path, monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "MAC_TASK_GIT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    hub_token = "ghp_" + "h" * 36
    client = _install_fake_hub(monkeypatch, {"name": "github.token", "value": hub_token})
    seen: list[dict] = []

    def fake_open(repo_url, head, *, base=None, title=None, body=None, **kwargs):
        seen.append(kwargs)
        return gitops.PullRequestResult(
            host="github", number=9, url="https://github.invalid/pull/9", state="open"
        )

    monkeypatch.setattr(gitops, "open_pull_request", fake_open)

    outcome = gitops.agent_pull_request(_pr_target(tmp_path), task_id="task_1")

    assert outcome["opened"] is True
    # Resolved BY NAME at the moment of use, not by id and not cached.
    assert ("POST", "/secrets/github.token/resolve") in client.calls
    assert seen == [{"github_token": hub_token}]


def test_hub_resolved_credential_never_reaches_the_evidence(tmp_path, monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "MAC_TASK_GIT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    hub_token = "ghp_" + "k" * 36
    _install_fake_hub(monkeypatch, {"name": "github.token", "value": hub_token})

    def boom(*a, **k):
        raise RuntimeError("forge rejected token %s" % hub_token)

    monkeypatch.setattr(gitops, "open_pull_request", boom)

    outcome = gitops.agent_pull_request(_pr_target(tmp_path), task_id="task_1")

    assert outcome["opened"] is False
    assert hub_token not in outcome["reason"]
    assert "***" in outcome["reason"]


def test_hub_secret_without_the_forge_capability_is_refused(tmp_path, monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "MAC_TASK_GIT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    _install_fake_hub(
        monkeypatch,
        {"name": "github.token", "value": "x" * 40, "capabilities": ["slack"]},
    )
    monkeypatch.setattr(
        gitops,
        "open_pull_request",
        lambda *a, **k: pytest.fail("called the forge with an unauthorised secret"),
    )

    outcome = gitops.agent_pull_request(_pr_target(tmp_path), task_id="task_1")

    assert outcome["opened"] is False
    assert "capability" in outcome["reason"]


def test_forge_token_prefers_the_agents_own_environment(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "e" * 36)
    monkeypatch.setattr(
        gitops,
        "forge_token_from_hub",
        lambda host: pytest.fail("asked the hub for a token it already had"),
    )
    assert gitops.forge_token("github") == "ghp_" + "e" * 36


def test_forge_token_from_hub_is_empty_without_a_hub(monkeypatch):
    for name in ("MAC_API_URL", "MAC_URL", "MAC_HUB_URL"):
        monkeypatch.delenv(name, raising=False)
    assert gitops.forge_token_from_hub("github") == ""


# ---------------------------------------------------------------------------
# Verify, do not assume: an identity with a ruleset bypass can merge past the
# forge's own gates, so the requester checks the gates actually passed.
# ---------------------------------------------------------------------------


def test_merge_is_not_requested_until_required_checks_actually_passed(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    # The forge would happily merge -- the caller holds a ruleset bypass, which
    # is exactly the situation this must not depend on.
    forge.checks_pending = True
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    with pytest.raises(ValidationError) as excinfo:
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert getattr(excinfo.value, "publication_failure_kind", "") == "pull_request_checks_pending"
    # It asked about the reviewed head, and asked for nothing else.
    assert forge.verified == [{"sha": task_head, "contexts": ["sanity"]}]
    assert forge.merges == []
    assert git(source, "ls-remote", "origin", "refs/heads/main").split()[0] == main_head
    assert cp.get_task(task.id).state != TaskState.COMPLETED.value


def test_failed_required_checks_are_not_a_deferral(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    forge.checks_failed = ("sanity",)
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    with pytest.raises(ValidationError) as excinfo:
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert getattr(excinfo.value, "publication_failure_kind", "") == "pull_request_checks_failed"
    assert forge.merges == []
    assert git(source, "ls-remote", "origin", "refs/heads/main").split()[0] == main_head


def test_unreadable_check_results_are_not_treated_as_passing(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    forge.checks_known = False
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    with pytest.raises(ValidationError) as excinfo:
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert getattr(excinfo.value, "publication_failure_kind", "") == "pull_request_checks_pending"
    assert forge.merges == []


def test_verified_checks_are_recorded_and_allow_the_merge(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    detail = published_detail(cp, task.id)
    verification = next(
        item for item in detail["commands"] if item["name"] == "required_check_verification"
    )
    assert verification["case"] == "verified"
    assert verification["passed"] == ["sanity"]
    assert verification["pending"] == []
    assert verification["head_sha"] == task_head


def test_no_required_contexts_is_recorded_distinctly_from_pending(cp, tmp_path, monkeypatch):
    """An unprotected repo is not a repo whose checks have not started."""
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge, checks=())
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    detail = published_detail(cp, task.id)
    verification = next(
        item for item in detail["commands"] if item["name"] == "required_check_verification"
    )
    assert verification["case"] == "none_configured"
    assert verification["contexts"] == []
    # The worker's verifier run is this repository's gate; the hub runs none.
    assert detail["test_gate"] == "worker_verifier"
    assert not any(item["name"] == "publication_contract_gate" for item in detail["commands"])


def test_required_check_verdicts_classifies_each_context(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "x" * 36)
    responses = {
        "status": {"statuses": [{"context": "legacy", "state": "success"}]},
        "check-runs": {
            "check_runs": [
                {"name": "sanity", "status": "completed", "conclusion": "success"},
                {"name": "compat", "status": "in_progress", "conclusion": None},
                {"name": "lint", "status": "completed", "conclusion": "failure"},
                # A required check that did not run is NOT a pass.
                {"name": "docs", "status": "completed", "conclusion": "skipped"},
            ]
        },
    }

    def fake_get(url, headers, *a, **k):
        return responses["check-runs" if "check-runs" in url else "status"]

    monkeypatch.setattr(gitops, "_http_get_json", fake_get)
    verdict = gitops.required_check_verdicts(
        "https://github.com/acme/widgets.git",
        "a" * 40,
        ("sanity", "compat", "lint", "docs", "legacy", "never-reported"),
    )
    assert verdict["known"] is True
    assert verdict["passed"] == ["sanity", "legacy"]
    assert verdict["failed"] == ["lint"]
    assert verdict["pending"] == ["compat", "docs", "never-reported"]


def test_required_check_verdicts_is_unknown_when_the_forge_cannot_be_asked(
    monkeypatch,
):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "x" * 36)

    def boom(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(gitops, "_http_get_json", boom)
    verdict = gitops.required_check_verdicts(
        "https://github.com/acme/widgets.git", "a" * 40, ("sanity",)
    )
    assert verdict["known"] is False
    assert verdict["pending"] == ["sanity"]


def test_required_check_verdicts_with_no_contexts_is_known_and_empty():
    verdict = gitops.required_check_verdicts("https://github.com/acme/widgets.git", "a" * 40, ())
    assert verdict["known"] is True
    assert verdict["pending"] == []
