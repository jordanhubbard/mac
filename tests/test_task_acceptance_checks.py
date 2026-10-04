"""A task's own acceptance checks gate its landing, like required checks.

Live 2026-10-04: task_bb7a198c said "Done when Memory Sanitizers passes on your
PR". Memory Sanitizers is not a required check in that repository, so the hub
landed PR #970 as soon as the required checks passed, with Memory Sanitizers
red, and marked the task complete without meeting its stated acceptance.

``metadata.acceptance_checks`` names forge checks that are, for THIS task's
pull request, additional required contexts: pending waits, failed sends the
task back with the log, and a name that never reports blocks at the landing
deadline instead of hanging or passing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from mac import gitops, services
from mac.cli import _render_text
from mac.executor_prompt import build_task_prompt
from mac.models import TaskState, ValidationError, parse_time, utcnow
from tests.test_publication_pull_request import (
    FakeForge,
    build_repo,
    drive_to_approval,
    install_forge,
)

_SANITIZER_LOG = (
    "Run make sanitize\n"
    "==4242==ERROR: AddressSanitizer: heap-use-after-free on address 0x6020\n"
    "##[error]Process completed with exit code 1.\n"
)


@pytest.fixture()
def cp():
    return services.ControlPlane.in_memory()


class PerCheckForge(FakeForge):
    """Reports each context individually: success, pending, failure, or (absent
    from ``states``) nothing at all for the head."""

    def __init__(self, *args, states=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.states = dict(states or {})

    def required_check_verdicts(self, repo_url, sha, contexts, **_):
        self.verified.append({"sha": sha, "contexts": list(contexts)})
        verdict = {"known": True, "contexts": list(contexts), "passed": [], "pending": []}
        verdict["failed"], verdict["missing"] = [], []
        for name in contexts:
            state = self.states.get(name)
            if state == "success":
                verdict["passed"].append(name)
            elif state == "failure":
                verdict["failed"].append(name)
            else:
                verdict["pending"].append(name)
                if state is None:
                    verdict["missing"].append(name)
        return verdict


def _approved_with_acceptance(cp, tmp_path, monkeypatch, states, *, required=("sanity",)):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = PerCheckForge(remote, tmp_path / "forge", states=states)
    forge.failed_check_logs = {"Memory Sanitizers": _SANITIZER_LOG}
    install_forge(monkeypatch, forge, checks=required)
    task, _evidence, _reviewer = drive_to_approval(cp, source, task_head)
    metadata = dict(cp.get_task(task.id).metadata)
    metadata["acceptance_checks"] = ["Memory Sanitizers"]
    cp._persist_task_metadata_narrow(task.id, metadata, actor="test")
    return forge, task, task_head


def _landing(cp, task_id):
    return dict((cp.get_task(task_id).metadata or {}).get("landing") or {})


def _age_landing_past_deadline(cp, task_id):
    metadata = dict(cp.get_task(task_id).metadata)
    landing = dict(metadata.get("landing") or {})
    landing["first_attempt_at"] = (
        parse_time(utcnow()) - timedelta(seconds=services.DEFAULT_LANDING_DEADLINE_SECONDS + 1)
    ).isoformat(timespec="microseconds")
    landing.pop("not_before", None)
    metadata["landing"] = landing
    metadata.pop("publication_retry", None)
    cp._persist_task_metadata_narrow(task_id, metadata, actor="test")


def test_a_failed_acceptance_check_sends_the_task_back_with_its_log(cp, tmp_path, monkeypatch):
    forge, task, task_head = _approved_with_acceptance(
        cp, tmp_path, monkeypatch, {"sanity": "success", "Memory Sanitizers": "failure"}
    )

    result = cp.advance_default_review_workflow(task.id)

    # The repository does not require it; the task does.
    assert result["status"] == "required_checks_failed"
    assert result["failed_checks"] == ["Memory Sanitizers"]
    assert forge.merges == []
    assert forge.verified[-1]["contexts"] == ["sanity", "Memory Sanitizers"]
    sent_back = cp.get_task(task.id)
    assert sent_back.state == TaskState.OPEN.value
    (failed,) = sent_back.metadata["fix_failed_checks"]["failed_checks"]
    assert failed["name"] == "Memory Sanitizers"
    assert failed["acceptance_check"] is True
    assert "heap-use-after-free" in failed["log_tail"]
    assert _landing(cp, task.id)["check_fixes"] == 1

    prompt = build_task_prompt(sent_back.to_dict())
    assert "Sent back to fix failing checks" in prompt
    assert "heap-use-after-free" in prompt
    assert "Memory Sanitizers is this task's own acceptance check" in prompt


def test_a_passing_acceptance_check_lands(cp, tmp_path, monkeypatch):
    forge, task, task_head = _approved_with_acceptance(
        cp, tmp_path, monkeypatch, {"sanity": "success", "Memory Sanitizers": "success"}
    )

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    assert forge.merges == [{"number": 101, "method": "squash", "sha": task_head}]
    assert forge.verified[-1]["contexts"] == ["sanity", "Memory Sanitizers"]


def test_a_pending_acceptance_check_waits(cp, tmp_path, monkeypatch):
    forge, task, _ = _approved_with_acceptance(
        cp, tmp_path, monkeypatch, {"sanity": "success", "Memory Sanitizers": "pending"}
    )

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "publish_failed"
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value
    landing = _landing(cp, task.id)
    assert landing["attempts"] == 0
    assert landing["last_reason"] == "pull_request_checks_pending"
    assert "Memory Sanitizers" in result["error"]
    assert forge.merges == []


def test_an_acceptance_check_that_never_reports_blocks_at_the_deadline(cp, tmp_path, monkeypatch):
    forge, task, _ = _approved_with_acceptance(cp, tmp_path, monkeypatch, {"sanity": "success"})

    waiting = cp.advance_default_review_workflow(task.id)

    # Before the deadline it is a wait, not a pass and not a block.
    assert waiting["status"] == "publish_failed"
    assert "blocked_reason" not in waiting
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value
    assert "Memory Sanitizers not reported" in waiting["error"]
    assert _landing(cp, task.id)["attempts"] == 0
    assert forge.merges == []

    _age_landing_past_deadline(cp, task.id)
    blocked = cp.advance_default_review_workflow(task.id)

    assert blocked["blocked_reason"] == "acceptance_checks_never_reported"
    task_now = cp.get_task(task.id)
    assert task_now.state == TaskState.BLOCKED.value
    landing = task_now.metadata["landing"]
    assert landing["outcome"] == "acceptance_checks_never_reported"
    assert landing["last_reason"] == services.ACCEPTANCE_CHECKS_NOT_REPORTED
    assert "Memory Sanitizers not reported" in landing["last_error"]
    assert forge.merges == []


def test_acceptance_checks_gate_a_repository_without_required_checks(cp, tmp_path, monkeypatch):
    forge, task, task_head = _approved_with_acceptance(
        cp, tmp_path, monkeypatch, {"Memory Sanitizers": "pending"}, required=()
    )

    assert cp.advance_default_review_workflow(task.id)["status"] == "publish_failed"
    assert forge.verified[-1]["contexts"] == ["Memory Sanitizers"]
    assert forge.merges == []

    forge.states["Memory Sanitizers"] = "success"
    metadata = dict(cp.get_task(task.id).metadata)
    metadata.pop("publication_retry", None)
    landing = dict(metadata.get("landing") or {})
    landing.pop("not_before", None)
    metadata["landing"] = landing
    cp._persist_task_metadata_narrow(task.id, metadata, actor="test")

    assert cp.advance_default_review_workflow(task.id)["status"] == "published"
    assert forge.merges == [{"number": 101, "method": "squash", "sha": task_head}]


def test_without_acceptance_checks_only_required_checks_gate(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = PerCheckForge(remote, tmp_path / "forge", states={"sanity": "success"})
    install_forge(monkeypatch, forge)
    task, _evidence, _reviewer = drive_to_approval(cp, source, task_head)

    assert cp.advance_default_review_workflow(task.id)["status"] == "published"
    assert [item["contexts"] for item in forge.verified] == [["sanity"]]


def test_required_check_verdicts_separate_unreported_from_pending(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "x" * 36)

    def fake_get_json(url, headers, timeout=20.0):
        if url.endswith("/status"):
            return {"statuses": [{"context": "ci/legacy", "state": "pending"}]}
        return {"check_runs": [{"name": "build", "status": "in_progress"}]}

    monkeypatch.setattr(gitops, "_http_get_json", fake_get_json)

    verdict = gitops.required_check_verdicts(
        "https://github.com/acme/widgets.git", "a" * 40, ("build", "ci/legacy", "Typo")
    )

    assert verdict["pending"] == ["build", "ci/legacy", "Typo"]
    assert verdict["missing"] == ["Typo"]


@pytest.mark.parametrize(
    "value",
    [
        "Memory Sanitizers",
        {"name": "Memory Sanitizers"},
        [""],
        ["  "],
        [None],
        [3],
        ["x" * (services.ACCEPTANCE_CHECK_NAME_MAX + 1)],
        ["c%d" % index for index in range(services.ACCEPTANCE_CHECKS_MAX + 1)],
    ],
)
def test_acceptance_checks_validation_rejects_bad_shapes(cp, value):
    with pytest.raises(ValidationError, match="acceptance_checks"):
        cp.create_task("t", metadata={"acceptance_checks": value})
    task = cp.create_task("t")
    with pytest.raises(ValidationError, match="acceptance_checks"):
        cp.update_task(task.id, metadata={"acceptance_checks": value})


def test_acceptance_checks_are_normalized_and_shown(cp):
    task = cp.create_task(
        "t", metadata={"acceptance_checks": [" Memory Sanitizers ", "lint", "lint"]}
    )

    assert task.metadata["acceptance_checks"] == ["Memory Sanitizers", "lint"]
    updated = cp.update_task(task.id, metadata={**task.metadata, "acceptance_checks": ["x"]})
    assert updated.metadata["acceptance_checks"] == ["x"]

    shown = _render_text({"task": task.to_dict()})
    assert "acceptance_checks: Memory Sanitizers, lint" in shown
    prompt = build_task_prompt(task.to_dict())
    assert "Acceptance checks (this task's definition of done)" in prompt
    assert '["Memory Sanitizers", "lint"]' in prompt
    assert "Acceptance checks" not in build_task_prompt(cp.create_task("u").to_dict())
