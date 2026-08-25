"""Failure transitions should self-document on the task (problem + remediation)."""

from mac.models import TaskState
from mac.services import _failure_diagnosis


def _d(reason=None, error=None, problems=None):
    d = {}
    if reason is not None:
        d["reason"] = reason
    if error is not None:
        d["error"] = error
    if problems is not None:
        d["problems"] = problems
    return d


def test_non_failure_transition_returns_none():
    assert _failure_diagnosis(TaskState.COMPLETED.value, _d(reason="x")) is None
    assert _failure_diagnosis(TaskState.OPEN.value, _d(reason="x")) is None


def test_empty_detail_returns_actionable_generic_diagnosis():
    diagnosis = _failure_diagnosis(TaskState.BLOCKED.value, {})
    assert diagnosis is not None
    assert "without a more specific failure description" in diagnosis
    assert "repair the producer" in diagnosis
    assert "without a more specific failure description" in (
        _failure_diagnosis(TaskState.BLOCKED.value, None) or ""
    )


def test_clone_auth_diagnosis():
    out = _failure_diagnosis(
        TaskState.BLOCKED.value,
        _d(
            reason="worker_exception",
            error="could not clone repository for K8s task: authentication failed",
        ),
    )
    assert out and "Remediation:" in out
    assert "clone" in out.lower() and "GH_TOKEN" in out


def test_heartbeat_offline_diagnosis():
    out = _failure_diagnosis(TaskState.FAILED.value, _d(reason="heartbeat_offline"))
    assert out and "offline" in out.lower() and "connectivity" in out.lower()


def test_timeout_diagnosis():
    out = _failure_diagnosis(TaskState.BLOCKED.value, _d(error="agent run timed out after 5400s"))
    assert out and "too large" in out.lower() and "MAC_EXECUTOR_AGENT_TIMEOUT" in out


def test_contract_failed_diagnosis():
    out = _failure_diagnosis(
        TaskState.BLOCKED.value,
        _d(
            reason="verification_contract_failed",
            problems=["repo evidence requires pushed=true with remote_ref, or pr_url"],
        ),
    )
    assert out and "contract" in out.lower() and "push" in out.lower()


def test_review_starvation_diagnosis():
    out = _failure_diagnosis(TaskState.BLOCKED.value, _d(reason="review_retraction_cap_hit"))
    assert out and "review" in out.lower()


def test_max_attempts_diagnosis():
    out = _failure_diagnosis(TaskState.FAILED.value, _d(reason="max attempts"))
    assert out and "max_attempts" in out and "reopen" in out.lower()


def test_generic_fallback_includes_reason():
    out = _failure_diagnosis(TaskState.BLOCKED.value, _d(reason="something_unmapped"))
    assert out and "something_unmapped" in out and "Remediation:" in out
