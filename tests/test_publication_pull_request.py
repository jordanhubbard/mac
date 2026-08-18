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

    def __init__(self, remote: Path, workdir: Path, *, merge_blocked: str = ""):
        self.remote = remote
        self.workdir = workdir
        self.merge_blocked = merge_blocked
        self.opened: list[dict] = []
        self.merges: list[dict] = []

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
        checkout = self.workdir / ("merge-%d" % len(self.merges))
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
        return gitops.PullRequestMergeResult(merged=True, number=number, sha=merged)


def install_forge(monkeypatch, forge: FakeForge, *, checks=("sanity",)):
    monkeypatch.setattr(gitops, "resolve_forge", lambda url: "github")
    monkeypatch.setattr(
        gitops, "required_status_check_contexts", lambda url, branch: tuple(checks)
    )
    monkeypatch.setattr(gitops, "open_pull_request", forge.open_pull_request)
    monkeypatch.setattr(gitops, "merge_pull_request", forge.merge_pull_request)


def drive_to_approval(cp, source: Path, task_head: str):
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
