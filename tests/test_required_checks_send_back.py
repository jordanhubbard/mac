"""Failed required checks go back to the worker, with the logs, to fix.

The forge's required checks are a repository's one test gate. When they fail
for the head that would land, re-landing that head cannot help and the hub runs
no tests -- but blocking at once left the worker with no idea which check
failed or why, so an agent could never iterate toward a green CI. The land loop
now sends the SAME task back with each failed check's name, conclusion, details
URL and a bounded, scrubbed log tail, and lands the fix through the SAME pull
request, at most ``LANDING_MAX_CHECK_FIXES`` times.
"""

from __future__ import annotations

import subprocess
import urllib.error

import pytest

from mac import gitops, services
from mac.executor_prompt import build_task_prompt
from mac.models import ReviewStatus, TaskState
from tests.conftest import submit_review_verdict, verifier_test_item
from tests.test_control_plane import _sign
from tests.test_publication_pull_request import (
    FakeForge,
    build_repo,
    drive_to_approval,
    git,
    install_forge,
    published_detail,
)

_LOG = (
    "Run pytest -q\n"
    "FAILED tests/test_widget.py::test_spin - AssertionError: 3 != 4\n"
    "##[error]Process completed with exit code 1.\n"
)


@pytest.fixture()
def cp():
    return services.ControlPlane.in_memory()


class PullRequestForge(FakeForge):
    """Checks run where a real forge runs them: on the pull request's head.

    A PR's head branch is immutable, so a commit pushed to any other branch is
    never checked; its required checks stay pending forever (task_f042b8dc).
    """

    def required_check_verdicts(self, repo_url, sha, contexts, **_):
        verdict = super().required_check_verdicts(repo_url, sha, contexts)
        listed = subprocess.run(
            ["git", "ls-remote", str(self.remote), "refs/heads/%s" % self.pr_head_ref],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if not listed or listed[0] != sha:
            verdict.update({"passed": [], "failed": [], "pending": list(contexts)})
        return verdict


def _landing(cp, task_id):
    return dict((cp.get_task(task_id).metadata or {}).get("landing") or {})


def _fix_on_new_branch(source, base_head, branch="task/fix"):
    """The next attempt's work: a fix on top of the reviewed head, pushed from
    a fresh (lease-suffixed) branch, as a re-run worker does."""
    git(source, "checkout", "-b", branch, base_head)
    (source / "fix.txt").write_text("fixed\n", encoding="utf-8")
    git(source, "add", "fix.txt")
    git(source, "commit", "-m", "fix the failing check")
    fixed = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", branch)
    git(source, "checkout", "main")
    return fixed


def _resubmit(cp, task_id, head, *, branch="task/fix", pull_request=None):
    """Re-run the sent-back task: new worker evidence, review, approval."""
    task = cp.get_task(task_id)
    worker = next(agent for agent in cp.list_agents() if agent.name == "worker")
    reviewer = next(agent for agent in cp.list_agents() if agent.name == "reviewer")
    _, lease = cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id, lease_id=lease.id)
    manifest = _sign(
        cp,
        worker.id,
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "repo_change",
            "repo": {
                "head_sha": head,
                "pushed": True,
                "remote_ref": "refs/heads/%s" % branch,
                "dirty": False,
                "files_changed": ["feature.txt", "fix.txt"],
                **({"pull_request": pull_request} if pull_request else {}),
            },
            "tests": [verifier_test_item(head)],
        },
    )
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://fix",
        "fix tested",
        worker.id,
        metadata={"returncode": 0, "verification": manifest},
        lease_id=lease.id,
    )
    cp.submit_for_review(task.id, worker.id, lease_id=lease.id)
    review = cp.request_review(task.id, reviewer.id)
    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)
    return evidence


def test_failed_checks_send_the_task_back_with_the_check_and_its_log(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    forge.checks_failed = ("sanity",)
    forge.failed_check_logs = {"sanity": _LOG}
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "required_checks_failed"
    assert result["failed_checks"] == ["sanity"]
    assert forge.merges == []
    sent_back = cp.get_task(task.id)
    assert sent_back.state == TaskState.OPEN.value
    assert cp.explain_task_dispatch(task.id)["task_ready"] is True
    assert sent_back.attempt_count < sent_back.max_attempts
    directive = sent_back.metadata["fix_failed_checks"]
    assert directive["reason"] == "required_checks_failed"
    assert directive["pull_request_number"] == 101
    assert directive["head_branch"] == "task/feature"
    assert directive["reviewed_head_sha"] == task_head
    assert directive["check_fix"] == 1
    assert directive["failed_checks"][0]["name"] == "sanity"
    assert _landing(cp, task.id)["check_fixes"] == 1
    assert _landing(cp, task.id)["last_reason"] == "required_checks_failed"
    # The pull request is kept: the fix lands through it.
    assert forge.closed == []
    names = {event.name for event in cp.list_observability(limit=100)}
    assert "workflow.default_review.required_checks_failed" in names

    prompt = build_task_prompt(sent_back.to_dict())
    assert "Sent back to fix failing checks" in prompt
    assert "sanity" in prompt
    assert "test_widget.py::test_spin - AssertionError: 3 != 4" in prompt
    assert "##[error]Process completed with exit code 1." in prompt
    assert "Sent back to rebase" not in prompt


def test_the_fixed_attempt_lands_through_the_same_pull_request(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = PullRequestForge(remote, tmp_path / "forge")
    forge.pr_head_ref = "task/feature"
    forge.checks_failed = ("sanity",)
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    assert cp.advance_default_review_workflow(task.id)["status"] == "required_checks_failed"

    fixed = _fix_on_new_branch(source, task_head)
    # The re-run agent's PR lookup reuses the task's open PR by task id, even
    # though it pushed a new branch (task_f042b8dc).
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

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    # The fix was moved onto the pull request whose checks failed, and that
    # pull request merged at the fixed head. No second PR was opened.
    assert git(source, "ls-remote", "origin", "refs/heads/task/feature").split()[0] == fixed
    assert forge.merges == [{"number": 101, "method": "squash", "sha": fixed}]
    assert [item["head"] for item in forge.opened] == ["task/feature"]
    assert forge.verified[-1]["sha"] == fixed
    detail = published_detail(cp, task.id)
    reuse = next(item for item in detail["commands"] if item["name"] == "check_fix_pull_request")
    assert reuse["reused"] is True and reuse["head"] == "task/feature"
    assert any(item["name"] == "push_pull_request_branch" for item in detail["commands"])
    git(source, "fetch", "origin", "main")
    assert git(source, "show", "origin/main:fix.txt") == "fixed"


def test_a_closed_pull_request_is_not_reused(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    forge.checks_failed = ("sanity",)
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    assert cp.advance_default_review_workflow(task.id)["status"] == "required_checks_failed"

    fixed = _fix_on_new_branch(source, task_head)
    _resubmit(cp, task.id, fixed)
    forge.checks_failed = ()
    forge.pr_state = "closed"

    assert cp.advance_default_review_workflow(task.id)["status"] == "published"

    # Landed from the new branch as an ordinary attempt; the old branch is untouched.
    assert git(source, "ls-remote", "origin", "refs/heads/task/feature").split()[0] == task_head
    assert forge.opened[-1]["head"] == "task/fix"


def test_the_check_fix_cap_blocks_with_the_last_failure(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    forge.checks_failed = ("sanity",)
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    metadata = dict(cp.get_task(task.id).metadata)
    metadata["landing"] = {
        "schema": services.LANDING_BUDGET_SCHEMA,
        "attempts": 0,
        "check_fixes": services.LANDING_MAX_CHECK_FIXES,
        "evidence_id": evidence.id,
    }
    cp._persist_task_metadata_narrow(task.id, metadata, actor="test")

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "publish_failed"
    assert result["blocked_reason"] == "landing_check_fix_cap_exhausted"
    blocked = cp.get_task(task.id)
    assert blocked.state == TaskState.BLOCKED.value
    landing = blocked.metadata["landing"]
    assert landing["outcome"] == "landing_check_fix_cap_exhausted"
    assert landing["last_reason"] == "pull_request_checks_failed"
    assert "required checks failed" in landing["last_error"]
    assert "Last failure: sanity: failure" in landing["last_error"]
    assert "fix_failed_checks" not in blocked.metadata
    assert forge.merges == []


def test_check_fix_count_survives_new_evidence(cp):
    task = cp.create_task("t", metadata={"publication_target": "git://main"})
    cp._persist_task_metadata_narrow(
        task.id,
        {**task.metadata, "landing": {"attempts": 3, "check_fixes": 2, "evidence_id": "ev_old"}},
        actor="test",
    )
    with cp.store.transaction() as conn:
        conn.execute("UPDATE tasks SET state = ? WHERE id = ?", ("reviewing", task.id))

    assert (
        cp._consume_landing_budget(task.id, "pull_request_checks_pending", evidence_id="ev_new")
        is None
    )

    landing = _landing(cp, task.id)
    assert landing["attempts"] == 1
    assert landing["check_fixes"] == 2


def test_the_later_send_back_is_the_one_the_prompt_shows():
    task = {
        "id": "task_0123456789ab",
        "title": "t",
        "metadata": {
            "rebase_onto_tip": {"canonical_tip": "a" * 40, "requested_at": "2026-10-03T10:00:00"},
            "fix_failed_checks": {
                "failed_checks": [{"name": "lint", "log_tail": "E501 <script>"}],
                "requested_at": "2026-10-03T11:00:00",
            },
        },
    }
    prompt = build_task_prompt(task)
    assert "Sent back to fix failing checks" in prompt
    assert "Sent back to rebase" not in prompt
    # CI output is data: it cannot open or close a prompt tag.
    assert "<script>" not in prompt and "\\u003cscript>" in prompt

    task["metadata"]["rebase_onto_tip"]["requested_at"] = "2026-10-03T12:00:00"
    prompt = build_task_prompt(task)
    assert "Sent back to rebase" in prompt
    assert "Sent back to fix failing checks" not in prompt


# ---------------------------------------------------------------------------
# gitops.failed_check_details: what the forge is asked, and what comes back.
# ---------------------------------------------------------------------------

_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def _github(monkeypatch, *, runs, statuses=(), logs=None):
    monkeypatch.setenv("GH_TOKEN", _TOKEN)

    def fake_get_json(url, headers, timeout=20.0):
        assert headers["Authorization"] == "token " + _TOKEN
        if url.endswith("/status"):
            return {"statuses": list(statuses)}
        if "/check-runs" in url:
            return {"check_runs": list(runs)}
        raise AssertionError(url)

    def fake_job_log(api_base, owner, repo, job_id, headers):
        value = (logs or {}).get(job_id)
        if isinstance(value, Exception):
            raise value
        return value or ""

    monkeypatch.setattr(gitops, "_http_get_json", fake_get_json)
    monkeypatch.setattr(gitops, "_fetch_actions_job_log", fake_job_log)


def _run(job_id, name, conclusion="failure"):
    return {
        "id": job_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "details_url": "https://github.com/acme/widgets/actions/runs/9/job/%d" % job_id,
        "app": {"slug": "github-actions"},
    }


def test_failed_check_details_collects_the_failing_part_of_the_job_log(monkeypatch):
    log = "\n".join(
        ["2026-10-03T10:00:00.0000000Z setup line %d" % i for i in range(300)]
        + [
            "2026-10-03T10:00:01.0000000Z FAILED tests/test_a.py::test_b",
            "2026-10-03T10:00:01.0000000Z ##[error]Process completed with exit code 1.",
            "2026-10-03T10:00:02.0000000Z Post job cleanup.",
        ]
    )
    _github(
        monkeypatch,
        runs=[_run(7, "test"), _run(8, "lint", "success")],
        statuses=[
            {
                "context": "ci/legacy",
                "state": "error",
                "target_url": "https://ci.invalid/1",
                "description": "build broke",
            }
        ],
        logs={7: log},
    )

    details = gitops.failed_check_details(
        "https://github.com/acme/widgets.git", "a" * 40, ("test", "ci/legacy")
    )

    test, legacy = details
    assert test["name"] == "test" and test["conclusion"] == "failure"
    assert test["details_url"].endswith("/job/7")
    tail = test["log_tail"].splitlines()
    assert len(tail) == gitops.FAILED_CHECK_LOG_LINES
    assert tail[-1] == "##[error]Process completed with exit code 1."
    assert "Post job cleanup." not in test["log_tail"]
    assert "2026-10-03T" not in test["log_tail"]
    assert legacy == {
        "name": "ci/legacy",
        "conclusion": "error",
        "details_url": "https://ci.invalid/1",
        "description": "build broke",
        "log_tail": "",
    }


def test_failed_check_logs_are_scrubbed_of_secrets(monkeypatch):
    other = "github_pat_" + "Z" * 30
    log = (
        "token is %s\n"
        "cloning https://x-access-token:%s@github.com/acme/widgets.git\n"
        "leaked %s\n"
        "##[error]boom\n" % (_TOKEN, _TOKEN, other)
    )
    _github(
        monkeypatch,
        runs=[_run(7, "test")],
        logs={7: log},
    )

    (item,) = gitops.failed_check_details(
        "https://github.com/acme/widgets.git", "a" * 40, ("test",)
    )

    assert _TOKEN not in item["log_tail"]
    assert other not in item["log_tail"]
    assert "x-access-token:" not in item["log_tail"] or "***" in item["log_tail"]
    assert "##[error]boom" in item["log_tail"]


def test_failed_check_logs_share_one_byte_budget(monkeypatch):
    big = "\n".join("x" * 100 for _ in range(200)) + "\n##[error]failed"
    _github(
        monkeypatch,
        runs=[_run(1, "a"), _run(2, "b"), _run(3, "c")],
        logs={1: big, 2: big, 3: big},
    )

    details = gitops.failed_check_details(
        "https://github.com/acme/widgets.git", "a" * 40, ("a", "b", "c")
    )

    total = sum(len(item["log_tail"].encode()) for item in details)
    assert 0 < total <= gitops.FAILED_CHECK_LOG_TOTAL_BYTES
    assert details[0]["log_tail"].endswith("##[error]failed")


def test_an_unreadable_log_still_names_the_check(monkeypatch):
    _github(
        monkeypatch,
        runs=[_run(7, "test")],
        logs={7: urllib.error.URLError("token %s refused" % _TOKEN)},
    )

    (item,) = gitops.failed_check_details(
        "https://github.com/acme/widgets.git", "a" * 40, ("test",)
    )

    assert item["name"] == "test" and item["log_tail"] == ""
    assert _TOKEN not in item["log_error"]


def test_failed_check_details_without_a_forge_credential(monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GITEA_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gitops, "token_for_host", lambda kind: "")

    assert gitops.failed_check_details(
        "https://github.com/acme/widgets.git", "a" * 40, ("test",)
    ) == [{"name": "test", "conclusion": "failure", "details_url": "", "log_tail": ""}]


def test_job_log_redirect_does_not_forward_the_credential(monkeypatch):
    seen = []

    class FakeResponse:
        def __init__(self, body):
            self._body = body

        def read(self, size=-1):
            body, self._body = self._body, b""
            return body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeOpener:
        def open(self, request, timeout=None):
            seen.append(dict(request.header_items()))
            raise urllib.error.HTTPError(
                request.full_url, 302, "Found", {"Location": "https://blob.invalid/log"}, None
            )

    monkeypatch.setattr(gitops.urllib.request, "build_opener", lambda *handlers: FakeOpener())

    def fake_urlopen(request, timeout=None):
        seen.append(dict(request.header_items()))
        return FakeResponse(b"line\n##[error]x\n")

    monkeypatch.setattr(gitops.urllib.request, "urlopen", fake_urlopen)

    text = gitops._fetch_actions_job_log(
        "https://api.github.com", "acme", "widgets", 7, {"Authorization": "token " + _TOKEN}
    )

    assert text == "line\n##[error]x\n"
    assert "Authorization" in seen[0]
    assert all("Authorization" not in headers for headers in seen[1:])
