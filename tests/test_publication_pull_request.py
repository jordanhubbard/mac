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
from pathlib import Path

import pytest

from mac import gitops
from mac.models import ReviewStatus, TaskState, ValidationError
from mac.services import ControlPlane
from tests.conftest import submit_review_verdict
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
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "clone", str(remote), str(source)], check=True, capture_output=True
    )
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
        queue: bool = False,
    ):
        self.remote = remote
        self.workdir = workdir
        self.merge_blocked = merge_blocked
        self.queue = queue
        self.opened: list[dict] = []
        self.merges: list[dict] = []
        self.enqueued: list[dict] = []
        self.queue_merged_sha = ""
        self.verified: list[dict] = []
        self.checks_pending = False
        self.checks_failed: tuple = ()
        self.checks_known = True

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

    # -- merge queue -----------------------------------------------------
    def merge_queue_enabled(self, repo_url, branch, **_):
        return self.queue

    def enqueue_pull_request(self, repo_url, number, *, sha, **_):
        self.enqueued.append({"number": number, "sha": sha})
        return gitops.PullRequestMergeResult(
            merged=False,
            number=number,
            queued=True,
            serialization="merge_queue",
            reason="enqueued into the merge queue",
        )

    def pull_request_state(self, repo_url, number, **_):
        return {
            "known": True,
            "merged": bool(self.queue_merged_sha),
            "sha": self.queue_merged_sha,
            "state": "closed" if self.queue_merged_sha else "open",
            "head_sha": "",
        }

    def land_from_queue(self, sha: str, number: int = 101) -> str:
        """What the queue does asynchronously: squash the PR onto main."""
        self.queue_merged_sha = self._squash(sha, number)
        return self.queue_merged_sha

    def open_pull_request(self, repo_url, head, *, base=None, title=None, body=None):
        self.opened.append(
            {"repo_url": repo_url, "head": head, "base": base, "title": title}
        )
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
        checkout = self.workdir / ("merge-%d" % (len(self.merges) + len(self.enqueued) + 1))
        subprocess.run(
            ["git", "clone", "--branch", "main", str(self.remote), str(checkout)],
            check=True,
            capture_output=True,
        )
        git(checkout, "config", "user.email", "forge@example.com")
        git(checkout, "config", "user.name", "Fake Forge")
        git(checkout, "fetch", "origin", "task/feature")
        git(checkout, "merge", "--squash", sha)
        git(checkout, "commit", "-m", "squashed (#%d)" % number)
        merged = git(checkout, "rev-parse", "HEAD")
        git(checkout, "push", "origin", "HEAD:refs/heads/main")
        return merged


def install_forge(monkeypatch, forge: FakeForge, *, checks=("sanity",)):
    monkeypatch.setattr(gitops, "resolve_forge", lambda url: "github")
    monkeypatch.setattr(
        gitops, "required_status_check_contexts", lambda url, branch: tuple(checks)
    )
    monkeypatch.setattr(gitops, "open_pull_request", forge.open_pull_request)
    monkeypatch.setattr(gitops, "merge_pull_request", forge.merge_pull_request)
    monkeypatch.setattr(gitops, "merge_queue_enabled", forge.merge_queue_enabled)
    monkeypatch.setattr(gitops, "required_check_verdicts", forge.required_check_verdicts)
    monkeypatch.setattr(gitops, "enqueue_pull_request", forge.enqueue_pull_request)
    monkeypatch.setattr(gitops, "pull_request_state", forge.pull_request_state)


def drive_to_approval(cp, source: Path, task_head: str, *, pull_request=None):
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
            },
            "tests": [{"command": "make smoke", "returncode": 0}],
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
    cp.submit_review(
        review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id
    )
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

    # The hub must never run its own contract gate on this path -- the PR's
    # required checks are the gate. If it did, this runner would fire.
    cp._publication_merge_test_runner = lambda *a, **k: pytest.fail(
        "hub contract gate ran even though the forge gates the merge"
    )

    publication = cp.publish_task(
        task.id, "git://main", reviewer.id, evidence_id=evidence.id
    )

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
    gate = next(
        item for item in commands if item["name"] == "publication_contract_gate"
    )
    assert gate["skipped"] is True

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


def test_publication_defers_while_the_pull_request_checks_are_pending(
    cp, tmp_path, monkeypatch
):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(
        remote,
        tmp_path / "forge",
        merge_blocked="Required status check \"sanity\" is expected.",
    )
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    with pytest.raises(ValidationError) as excinfo:
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert "required checks" in str(excinfo.value)
    assert (
        getattr(excinfo.value, "publication_failure_kind", "")
        == "pull_request_checks_pending"
    )
    assert getattr(excinfo.value, "publication_retry_after_seconds", 0) > 0
    # The PR exists; main is untouched; the task is not completed.
    assert len(forge.opened) == 1
    assert git(source, "ls-remote", "origin", "refs/heads/main").split()[0] == main_head
    assert cp.get_task(task.id).state != TaskState.COMPLETED.value


def test_direct_push_opt_out_still_pushes_the_canonical_branch(
    cp, tmp_path, monkeypatch
):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    monkeypatch.setenv("MAC_PUBLICATION_STRATEGY", "direct_push")
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    cp._publication_merge_test_runner = lambda *a, **k: (0, "suite passed")

    publication = cp.publish_task(
        task.id, "git://main", reviewer.id, evidence_id=evidence.id
    )

    assert publication.status == "published"
    assert forge.opened == []
    assert forge.merges == []
    detail = published_detail(cp, task.id)
    assert detail["publication_mode"] in {"fast_forward", "merge_commit"}
    assert any(item["name"] == "push_main_occ" for item in detail["commands"])
    strategy = next(
        item for item in detail["commands"] if item["name"] == "publication_strategy"
    )
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
    cp._publication_merge_test_runner = lambda *a, **k: (0, "suite passed")

    publication = cp.publish_task(
        task.id, "git://main", reviewer.id, evidence_id=evidence.id
    )

    assert publication.status == "published"
    detail = published_detail(cp, task.id)
    strategy = next(
        item for item in detail["commands"] if item["name"] == "publication_strategy"
    )
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
        cp._resolve_publication_strategy("https://github.com/acme/widgets.git")[
            "strategy"
        ]
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
    result = gitops.merge_pull_request(
        "https://github.com/acme/widgets.git", 7, sha="a" * 40
    )
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
    assert gitops.required_status_check_contexts(
        "https://github.com/acme/widgets.git", "main"
    ) == ("sanity", "compatibility")


def test_required_status_check_contexts_is_unknown_without_credentials(monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "MAC_TASK_GIT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert (
        gitops.required_status_check_contexts(
            "https://github.com/acme/widgets.git", "main"
        )
        is None
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

    publication = cp.publish_task(
        task.id, "git://main", reviewer.id, evidence_id=evidence.id
    )

    assert publication.status == "published"
    # The hub opened nothing; it merged the agent's PR, pinned to the head the
    # reviewer approved.
    assert forge.opened == []
    assert forge.merges == [{"number": 77, "method": "squash", "sha": task_head}]

    detail = published_detail(cp, task.id)
    assert detail["pull_request_number"] == 77
    assert detail["pull_request_opened_by"] == "agent"
    opened = next(
        item for item in detail["commands"] if item["name"] == "open_pull_request"
    )
    assert opened["opened_by"] == "agent"

    proof = [
        item.metadata["verification"]["canonical_integration"]
        for item in cp.list_evidence(task.id)
        if item.metadata.get("verification", {}).get("canonical_integration")
    ][0]
    assert proof["squash_merged"] is True
    assert proof["contains_reviewed_head"] is False
    assert proof["pull_request_opened_by"] == "agent"


def test_hub_fallback_pull_request_is_recorded_as_a_fallback(
    cp, tmp_path, monkeypatch
):
    """A worker that opened no PR still publishes -- visibly, not silently."""
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    detail = published_detail(cp, task.id)
    opened = next(
        item for item in detail["commands"] if item["name"] == "open_pull_request"
    )
    assert opened["opened_by"] == "hub_fallback"
    assert opened["agent_reason"]
    assert detail["pull_request_opened_by"] == "hub_fallback"


def test_agent_opens_the_pull_request_onto_the_contract_canonical_branch(
    tmp_path, monkeypatch
):
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


def test_agent_pull_request_declines_without_a_forge_and_never_raises(
    tmp_path, monkeypatch
):
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


def test_agent_pull_request_reports_forge_errors_without_the_token(
    tmp_path, monkeypatch
):
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
# Merge safety: the queue serializes the merges, and the fallback says so.
# ---------------------------------------------------------------------------


def test_merge_queue_enqueues_instead_of_merging_directly(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge", queue=True)
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    with pytest.raises(ValidationError) as excinfo:
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert "merge queue" in str(excinfo.value)
    assert getattr(excinfo.value, "publication_failure_kind", "") == (
        "pull_request_queued"
    )
    assert getattr(excinfo.value, "publication_retry_after_seconds", 0) > 0
    # Enqueued, pinned to the reviewed head -- and never merged directly.
    assert forge.enqueued == [{"number": 101, "sha": task_head}]
    assert forge.merges == []
    # main is untouched and the task is not complete until the queue lands it.
    assert git(source, "ls-remote", "origin", "refs/heads/main").split()[0] == main_head
    assert cp.get_task(task.id).state != TaskState.COMPLETED.value


def test_publication_completes_once_the_merge_queue_lands_the_pull_request(
    cp, tmp_path, monkeypatch
):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge", queue=True)
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    with pytest.raises(ValidationError):
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    # The queue tests the projected merge and lands it, asynchronously.
    landed = forge.land_from_queue(task_head)

    publication = cp.publish_task(
        task.id, "git://main", reviewer.id, evidence_id=evidence.id
    )

    assert publication.status == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    # Observed, not merged a second time.
    assert forge.merges == []
    assert len(forge.enqueued) == 1
    detail = published_detail(cp, task.id)
    assert detail["final_sha"] == landed
    assert detail["merge_serialization"] == "merge_queue"
    serialization = next(
        item for item in detail["commands"] if item["name"] == "merge_serialization"
    )
    assert serialization["merge_queue"] is True
    assert "what was tested is what lands" in serialization["guarantee"]


def test_without_a_forge_queue_macs_own_queue_revalidates_before_merging(
    cp, tmp_path, monkeypatch
):
    """No FORGE queue means mac's own queue serializes, and it still refuses.

    The checks ran against a merge candidate built from one canonical tip. If
    the branch advances before the merge executes, the landed tree was never
    tested. This repository has no forge merge queue (GitHub's is
    organization-only), so `mac_native_queue` owns the landing -- and its land
    gate compares the canonical tip's TREE with the tree the entry was tested on
    top of, which is strictly stronger than the SHA comparison it replaced.
    The stale projection is rejected and re-projected instead of merged blind.
    """
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    # Someone else lands on main between the gate and the merge -- exactly once,
    # so the second attempt projects onto the tip that is really there.
    moved: list[str] = []

    def advance_main_once(url, branch):
        if not moved:
            other = tmp_path / "other"
            subprocess.run(
                ["git", "clone", "--branch", "main", str(remote), str(other)],
                check=True,
                capture_output=True,
            )
            git(other, "config", "user.email", "other@example.com")
            git(other, "config", "user.name", "Other Agent")
            (other / "other.txt").write_text("other\n", encoding="utf-8")
            git(other, "add", "other.txt")
            git(other, "commit", "-m", "someone else landed first")
            git(other, "push", "origin", "HEAD:refs/heads/main")
            moved.append(git(other, "rev-parse", "HEAD"))
        return ("sanity",)

    monkeypatch.setattr(
        gitops, "required_status_check_contexts", advance_main_once
    )

    publication = cp.publish_task(
        task.id, "git://main", reviewer.id, evidence_id=evidence.id
    )

    assert publication.status == "published"
    detail = published_detail(cp, task.id)
    # The first attempt refused to merge a projection that was already stale.
    assert detail["attempt"] == 2
    assert [item["sha"] for item in forge.merges] == [task_head]
    # ``commands`` is per-attempt, so this is the second attempt's own land
    # gate -- the first attempt's raised before it could merge.
    land_gates = [
        item for item in detail["commands"] if item["name"] == "merge_queue_land_gate"
    ]
    assert len(land_gates) == 1
    assert land_gates[0]["allowed"] is True
    serialization = next(
        item for item in detail["commands"] if item["name"] == "merge_serialization"
    )
    assert serialization["merge_queue"] is True
    assert serialization["mode"] == "mac_native_queue"
    assert "what was tested is what lands" in serialization["guarantee"]
    assert detail["merge_serialization"] == "mac_native_queue"
    # What landed is a squash of the reviewed head onto the tip that really was
    # main when the merge was requested.
    final = git(source, "ls-remote", "origin", "refs/heads/main").split()[0]
    git(source, "fetch", "origin", "main")
    parents = git(source, "rev-list", "--parents", "-n", "1", final).split()
    assert parents[1:] == [moved[0]]


def test_merge_queue_enabled_reads_the_branch_ruleset(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "x" * 36)
    monkeypatch.setattr(
        gitops,
        "_http_get_json",
        lambda *a, **k: [
            {"type": "pull_request", "parameters": {}},
            {"type": "merge_queue", "parameters": {"merge_method": "SQUASH"}},
        ],
    )
    assert (
        gitops.merge_queue_enabled("https://github.com/acme/widgets.git", "main") is True
    )
    monkeypatch.setattr(
        gitops, "_http_get_json", lambda *a, **k: [{"type": "pull_request"}]
    )
    assert (
        gitops.merge_queue_enabled("https://github.com/acme/widgets.git", "main")
        is False
    )


def test_merge_queue_enabled_is_unknown_without_credentials_or_on_gitea(monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "MAC_TASK_GIT_TOKEN", "GITEA_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert (
        gitops.merge_queue_enabled("https://github.com/acme/widgets.git", "main") is None
    )
    monkeypatch.setenv("GITEA_TOKEN", "gt_" + "x" * 36)
    # gitea has no merge queue: unknown, so the caller records the weaker
    # guarantee rather than assuming serialization it will not get.
    assert (
        gitops.merge_queue_enabled("https://gitea.invalid/acme/widgets.git", "main")
        is None
    )


def test_enqueue_pins_the_reviewed_head_and_classifies_refusals(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "x" * 36)
    sent: list[dict] = []

    def fake_graphql(url, headers, query, variables, token):
        sent.append(dict(variables))
        if "enqueuePullRequest" in query:
            return {}, ""
        return (
            {"repository": {"pullRequest": {"id": "PR_1", "merged": False}}},
            "",
        )

    monkeypatch.setattr(gitops, "_graphql", fake_graphql)
    result = gitops.enqueue_pull_request(
        "https://github.com/acme/widgets.git", 7, sha="a" * 40
    )
    assert result.queued is True
    assert result.merged is False
    assert result.serialization == "merge_queue"
    assert sent[-1]["expectedHeadOid"] == "a" * 40

    def refuse(url, headers, query, variables, token):
        if "enqueuePullRequest" in query:
            return {}, "Pull request is not mergeable: required status check pending"
        return {"repository": {"pullRequest": {"id": "PR_1", "merged": False}}}, ""

    monkeypatch.setattr(gitops, "_graphql", refuse)
    blocked = gitops.enqueue_pull_request(
        "https://github.com/acme/widgets.git", 7, sha="a" * 40
    )
    assert blocked.blocked is True
    assert blocked.queued is False

    def explode(url, headers, query, variables, token):
        if "enqueuePullRequest" in query:
            return {}, "internal server error"
        return {"repository": {"pullRequest": {"id": "PR_1", "merged": False}}}, ""

    monkeypatch.setattr(gitops, "_graphql", explode)
    with pytest.raises(RuntimeError, match="internal server error"):
        gitops.enqueue_pull_request(
            "https://github.com/acme/widgets.git", 7, sha="a" * 40
        )


def test_enqueue_failures_never_echo_the_token(monkeypatch):
    token = "ghp_" + "q" * 36
    monkeypatch.setenv("GH_TOKEN", token)
    captured: list[str] = []

    def reflecting_http(url, headers, query, variables, token_arg):
        captured.append(token_arg)
        return {}, gitops._scrub_secret(
            "bad credentials for token %s" % token, token_arg
        )

    monkeypatch.setattr(gitops, "_graphql", reflecting_http)
    with pytest.raises(RuntimeError) as excinfo:
        gitops.enqueue_pull_request(
            "https://github.com/acme/widgets.git", 7, sha="a" * 40
        )
    assert token not in str(excinfo.value)
    assert captured == [token]


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


def test_agent_asks_the_hub_for_the_forge_credential_when_its_env_has_none(
    tmp_path, monkeypatch
):
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "MAC_TASK_GIT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    hub_token = "ghp_" + "h" * 36
    client = _install_fake_hub(
        monkeypatch, {"name": "github.token", "value": hub_token}
    )
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


def test_merge_is_not_requested_until_required_checks_actually_passed(
    cp, tmp_path, monkeypatch
):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    # The forge would happily merge -- the caller holds a ruleset bypass, which
    # is exactly the situation this must not depend on.
    forge.checks_pending = True
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    with pytest.raises(ValidationError) as excinfo:
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert (
        getattr(excinfo.value, "publication_failure_kind", "")
        == "pull_request_checks_pending"
    )
    # It asked about the reviewed head, and asked for nothing else.
    assert forge.verified == [{"sha": task_head, "contexts": ["sanity"]}]
    assert forge.merges == []
    assert forge.enqueued == []
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

    assert (
        getattr(excinfo.value, "publication_failure_kind", "")
        == "pull_request_checks_failed"
    )
    assert forge.merges == []
    assert git(source, "ls-remote", "origin", "refs/heads/main").split()[0] == main_head


def test_unreadable_check_results_are_not_treated_as_passing(
    cp, tmp_path, monkeypatch
):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    forge.checks_known = False
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    with pytest.raises(ValidationError) as excinfo:
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert (
        getattr(excinfo.value, "publication_failure_kind", "")
        == "pull_request_checks_pending"
    )
    assert forge.merges == []


def test_verified_checks_are_recorded_and_allow_the_merge(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    detail = published_detail(cp, task.id)
    verification = next(
        item
        for item in detail["commands"]
        if item["name"] == "required_check_verification"
    )
    assert verification["case"] == "verified"
    assert verification["passed"] == ["sanity"]
    assert verification["pending"] == []
    assert verification["head_sha"] == task_head


def test_no_required_contexts_is_recorded_distinctly_from_pending(
    cp, tmp_path, monkeypatch
):
    """An unprotected repo is not a repo whose checks have not started."""
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge, checks=())
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    ran: list[str] = []
    cp._publication_merge_test_runner = lambda *a, **k: (
        ran.append("gate") or (0, "suite passed")
    )

    cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    detail = published_detail(cp, task.id)
    verification = next(
        item
        for item in detail["commands"]
        if item["name"] == "required_check_verification"
    )
    assert verification["case"] == "none_configured"
    assert verification["contexts"] == []
    # The local contract gate is what protected this one, and it really ran.
    gate = next(
        item for item in detail["commands"] if item["name"] == "publication_contract_gate"
    )
    assert gate.get("skipped") is not True
    assert ran == ["gate"]


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
    verdict = gitops.required_check_verdicts(
        "https://github.com/acme/widgets.git", "a" * 40, ()
    )
    assert verdict["known"] is True
    assert verdict["pending"] == []
