"""One landing budget bounds every wait between review and landing.

Observed live: two approved tasks sat in REVIEWING and retried publication
~7,140 times each on "git publication requires evidence repo.head_sha" -- a
ValidationError retrying can never fix, retried on every tick with no attempt
cap and no deadline. ``metadata.landing`` is now the single budget every wait
charges (see ``ControlPlane._consume_landing_budget``).
"""

from datetime import timedelta

import pytest

import mac.services as services
from mac.models import (
    PublicationDeferredError,
    TaskState,
    TransitionError,
    ValidationError,
    parse_time,
    utcnow,
)
from tests.test_control_plane import (  # noqa: F401 - pytest fixtures
    _drive_task_to_approved,
    _expire_landing_backoff,
    cp,
    register_agent,
    verified_repo_metadata,
)

HEAD_SHA_ERROR = "git publication requires evidence repo.head_sha"


def _landing(cp, task_id):
    return dict(cp.get_task(task_id).metadata.get("landing") or {})


def _blocked_detail(cp, task_id):
    for event in reversed(cp.task_history(task_id, limit=50)):
        if event.to_state == TaskState.BLOCKED.value:
            return dict(event.detail or {})
    raise AssertionError("task never entered BLOCKED")


def _age_landing(cp, task_id, seconds):
    """Move the first landing attempt ``seconds`` into the past."""
    task = cp.get_task(task_id)
    metadata = dict(task.metadata)
    landing = dict(metadata.get("landing") or {})
    landing["first_attempt_at"] = (parse_time(utcnow()) - timedelta(seconds=seconds)).isoformat(
        timespec="microseconds"
    )
    landing.pop("not_before", None)
    metadata["landing"] = landing
    cp._persist_task_metadata_narrow(task_id, metadata, actor="test")


def _raiser(calls, make_exc):
    def _publish(*_args, **_kwargs):
        calls.append(1)
        raise make_exc()

    return _publish


def _transient():
    return ValidationError("git publication fetch_source failed: connection reset by peer")


def test_in_flight_head_sha_failure_blocks_on_first_tick(cp, monkeypatch):
    """The live shape: an approved REVIEWING task whose publication raises the
    repo.head_sha ValidationError. The first sweep after deploy must move it to
    BLOCKED with that reason -- not retry it another 7,000 times."""
    task, _worker, _reviewer, _evidence = _drive_task_to_approved(cp)
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value
    calls = []
    monkeypatch.setattr(cp, "publish_task", _raiser(calls, lambda: ValidationError(HEAD_SHA_ERROR)))

    sweep = cp.advance_default_review_workflows()

    [result] = [r for r in sweep["results"] if r["task_id"] == task.id]
    assert result["status"] == "publish_failed"
    assert result["blocked_reason"] == "landing_non_retryable"
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value
    detail = _blocked_detail(cp, task.id)
    assert detail["reason"] == "landing_non_retryable"
    assert HEAD_SHA_ERROR in detail["error"]
    assert detail["manual_repair_required"] is True
    assert _landing(cp, task.id)["outcome"] == "landing_non_retryable"
    # The sweep no longer selects it: one attempt, ever.
    again = cp.advance_default_review_workflows()
    assert task.id not in {r["task_id"] for r in again["results"]}
    assert calls == [1]


def test_transient_failure_backs_off_and_charges_an_attempt(cp, monkeypatch):
    task, _worker, _reviewer, _evidence = _drive_task_to_approved(cp)
    calls = []
    monkeypatch.setattr(cp, "publish_task", _raiser(calls, _transient))

    first = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "publish_failed"
    assert "blocked_reason" not in first
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value
    landing = _landing(cp, task.id)
    assert landing["attempts"] == 1
    assert landing["last_reason"] == "publish_failed"
    assert "connection reset" in landing["last_error"]
    delay = (parse_time(landing["not_before"]) - parse_time(landing["last_attempt_at"])).seconds
    assert delay == services.LANDING_BACKOFF_MIN_SECONDS

    backoff = cp.advance_default_review_workflow(task.id)
    assert backoff["status"] == "landing_backoff"
    assert calls == [1]

    _expire_landing_backoff(cp, task.id)
    cp.advance_default_review_workflow(task.id)
    landing = _landing(cp, task.id)
    assert landing["attempts"] == 2
    delay = (parse_time(landing["not_before"]) - parse_time(landing["last_attempt_at"])).seconds
    assert delay == 2 * services.LANDING_BACKOFF_MIN_SECONDS
    assert calls == [1, 1]


def test_attempt_cap_blocks_with_the_last_error(cp, monkeypatch):
    monkeypatch.setenv("MAC_LANDING_MAX_ATTEMPTS", "3")
    task, _worker, _reviewer, _evidence = _drive_task_to_approved(cp)
    calls = []
    monkeypatch.setattr(cp, "publish_task", _raiser(calls, _transient))

    for _ in range(2):
        cp.advance_default_review_workflow(task.id)
        assert cp.get_task(task.id).state == TaskState.REVIEWING.value
        _expire_landing_backoff(cp, task.id)
    final = cp.advance_default_review_workflow(task.id)

    assert final["blocked_reason"] == "landing_budget_exhausted"
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value
    detail = _blocked_detail(cp, task.id)
    assert detail["reason"] == "landing_budget_exhausted"
    assert detail["exhausted_by"] == "attempts"
    assert detail["attempts"] == 3
    assert "connection reset" in detail["error"]
    assert len(calls) == 3


def test_deadline_blocks_even_a_pure_wait(cp, monkeypatch):
    """The release barrier never charges an attempt, but it does not get to
    hold a task past the deadline either."""
    task, _worker, _reviewer, _evidence = _drive_task_to_approved(cp)
    barrier = {"epoch_id": "epoch-1", "state": "open"}
    calls = []
    monkeypatch.setattr(
        cp,
        "publish_task",
        _raiser(calls, lambda: PublicationDeferredError("fleet release", barrier=barrier)),
    )

    for _ in range(3):
        assert cp.advance_default_review_workflow(task.id)["status"] == "publication_deferred"
    landing = _landing(cp, task.id)
    assert landing["attempts"] == 0
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value

    _age_landing(cp, task.id, services.DEFAULT_LANDING_DEADLINE_SECONDS + 1)
    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "landing_budget_exhausted"
    assert result["exhausted_by"] == "deadline"
    assert result["waiting_on"] == "publication_deferred"
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value


def test_checks_pending_charges_the_deadline_not_attempts(cp, monkeypatch):
    task, _worker, _reviewer, _evidence = _drive_task_to_approved(cp)

    def _pending():
        exc = ValidationError("waiting on the pull request's required checks")
        exc.publication_retry_after_seconds = 600
        exc.publication_failure_kind = "pull_request_checks_pending"
        return exc

    monkeypatch.setattr(cp, "publish_task", _raiser([], _pending))
    for _ in range(12):
        cp.advance_default_review_workflow(task.id)
        _expire_landing_backoff(cp, task.id)
        task_metadata = dict(cp.get_task(task.id).metadata)
        task_metadata.pop("publication_retry", None)
        cp._persist_task_metadata_narrow(task.id, task_metadata, actor="test")
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value
    assert _landing(cp, task.id)["attempts"] == 0

    _age_landing(cp, task.id, services.DEFAULT_LANDING_DEADLINE_SECONDS + 1)
    result = cp.advance_default_review_workflow(task.id)
    assert result["blocked_reason"] == "landing_budget_exhausted"
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value


def test_waiting_for_hub_reviewer_is_bounded_by_the_deadline(cp, monkeypatch):
    """No approval identity (registration failing) is a pure wait: it charges
    the landing deadline, not attempts, and ends in BLOCKED."""
    monkeypatch.setattr(cp, "_ensure_hub_reviewer_agent", lambda **_kwargs: None)
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task(
        "No hub-reviewer exists",
        required_capabilities=["python"],
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    assert cp.advance_default_review_workflow(task.id)["status"] == "waiting_for_hub_reviewer"
    first_seen = _landing(cp, task.id)["first_attempt_at"]
    assert cp.advance_default_review_workflow(task.id)["status"] == "waiting_for_hub_reviewer"
    # A pure wait does not rewrite metadata on every tick.
    assert _landing(cp, task.id)["first_attempt_at"] == first_seen

    _age_landing(cp, task.id, services.DEFAULT_LANDING_DEADLINE_SECONDS + 1)
    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "landing_budget_exhausted"
    assert result["waiting_on"] == "waiting_for_hub_reviewer"
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value


@pytest.mark.parametrize(
    "exc, mode",
    [
        (ValidationError(HEAD_SHA_ERROR), "permanent"),
        (ValidationError("git publication merge_source failed: CONFLICT"), "permanent"),
        (ValidationError("git fetch: Could not resolve host: github.com"), "retry"),
        (ValidationError("git publication push timed out"), "retry"),
        (TransitionError("task state changed during publish; retry"), "retry"),
        (PublicationDeferredError("release", barrier={}), "wait"),
    ],
)
def test_landing_failure_classification(exc, mode):
    assert services._landing_failure_mode(exc) == mode


def test_landing_failure_classification_reads_publication_hints():
    retry = ValidationError("pull request could not be opened")
    retry.publication_retry_after_seconds = 600
    retry.publication_failure_kind = "pull_request_open_failed"
    wait = ValidationError("checks pending")
    wait.publication_retry_after_seconds = 600
    wait.publication_failure_kind = "pull_request_checks_pending"
    conflict = ValidationError("merge gate: does not integrate")
    conflict.conflict_integration_context = {"task_id": "t"}

    assert services._landing_failure_mode(retry) == "retry"
    assert services._landing_failure_mode(wait) == "wait"
    assert services._landing_failure_mode(conflict) == "wait"
