"""Contract tests for mac.harness_recovery_reflex.

All seven requirements from the task spec are covered here.  No live HTTP or
LLM calls are made: the ``_call_llm`` seam is always replaced by a mock.

Test numbering mirrors the task description:
  1. MAC_RECOVERY_REFLEX_ENABLED unset/false -> choose_remediation always escalates, no LLM.
  2. LLM returns unrecognised string -> choose_remediation maps to escalate.
  3. LLM returns valid whitelist member -> corresponding enum member returned.
  4. try_recovery with recovery_count=2 -> (False, "escalate", "recovery limit reached"), no LLM.
  5. Simulated unrecognised fetch failure -> try_recovery invokes mock DISPATCH,
     returns recovered=True, recovery_log updated.
  6. Recovery observability event emitted on each invocation
     (assert _observe_log called with step/choice/result).
  7. Verification/finaliser paths not wrapped: assert no try_recovery called when
     submission_problems exist (using minimal worker fixture).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, call, patch

import pytest

import mac.harness_recovery_reflex as hrr
from mac.harness_recovery_reflex import (
    RECOVERY_LIMIT,
    RemediationChoice,
    choose_remediation,
    try_recovery,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_state(recovery_count: int = 0) -> Dict[str, Any]:
    """Return a minimal attempt_state dict."""
    return {"recovery_count": recovery_count, "recovery_log": []}


def _noop_observe(step: str, choice: str, result: str) -> None:
    pass


def _noop_dispatch(action: str, context: dict) -> None:
    pass


# ---------------------------------------------------------------------------
# 1. MAC_RECOVERY_REFLEX_ENABLED unset/false -> always escalate, no LLM called
# ---------------------------------------------------------------------------


class TestReflexDisabled:
    """When the feature flag is off, choose_remediation always returns 'escalate'
    without ever invoking the LLM callable."""

    def test_unset_env_var_always_escalates(self, monkeypatch):
        monkeypatch.delenv("MAC_RECOVERY_REFLEX_ENABLED", raising=False)
        mock_llm = MagicMock(return_value="retry_fetch")

        result = choose_remediation(_fresh_state(), "some fetch error", llm_fn=mock_llm)

        assert result == RemediationChoice.escalate.value
        mock_llm.assert_not_called()

    def test_false_env_var_always_escalates(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "false")
        mock_llm = MagicMock(return_value="clear_cache")

        result = choose_remediation(_fresh_state(), "connection timeout", llm_fn=mock_llm)

        assert result == RemediationChoice.escalate.value
        mock_llm.assert_not_called()

    def test_zero_env_var_always_escalates(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "0")
        mock_llm = MagicMock(return_value="retry_fetch")

        result = choose_remediation(_fresh_state(), "npm registry unreachable", llm_fn=mock_llm)

        assert result == RemediationChoice.escalate.value
        mock_llm.assert_not_called()

    def test_empty_env_var_always_escalates(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "")
        mock_llm = MagicMock(return_value="retry_fetch")

        result = choose_remediation(_fresh_state(), "empty test", llm_fn=mock_llm)

        assert result == RemediationChoice.escalate.value
        mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# 2. LLM returns unrecognised string -> mapped to escalate
# ---------------------------------------------------------------------------


class TestUnrecognisedLLMResponse:
    """Any string not in the whitelist must silently map to 'escalate'."""

    @pytest.mark.parametrize(
        "bad_response",
        [
            "restart",
            "reboot",
            "undefined",
            "retry fetch",          # space instead of underscore - not in whitelist
            "{}",
            "escalate\nextra",      # extra content (strip doesn't remove embedded newlines fully)
            "",
            "   ",
        ],
    )
    def test_unrecognised_llm_output_maps_to_escalate(self, monkeypatch, bad_response):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        mock_llm = MagicMock(return_value=bad_response)

        result = choose_remediation(_fresh_state(), "pkg fetch failed", llm_fn=mock_llm)

        assert result == RemediationChoice.escalate.value
        mock_llm.assert_called_once()

    def test_whitelist_match_is_case_insensitive_via_lower(self, monkeypatch):
        """The module lowercases the raw LLM response before checking the whitelist.
        So 'RETRY_FETCH' -> 'retry_fetch' which IS in the whitelist and is returned."""
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        mock_llm = MagicMock(return_value="RETRY_FETCH")
        # "RETRY_FETCH".strip().lower() == "retry_fetch" which IS in the whitelist
        result = choose_remediation(_fresh_state(), "err", llm_fn=mock_llm)
        # After .lower() it becomes retry_fetch which is valid
        assert result == RemediationChoice.retry_fetch.value


# ---------------------------------------------------------------------------
# 3. LLM returns valid whitelist member -> corresponding enum member returned
# ---------------------------------------------------------------------------


class TestValidLLMResponse:
    """Each whitelisted action must be returned verbatim (as a string)."""

    @pytest.mark.parametrize(
        "action",
        [
            RemediationChoice.retry_fetch.value,
            RemediationChoice.clear_cache.value,
            RemediationChoice.escalate.value,
        ],
    )
    def test_whitelist_member_returned_as_is(self, monkeypatch, action):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        mock_llm = MagicMock(return_value=action)

        result = choose_remediation(_fresh_state(), "network blip", llm_fn=mock_llm)

        assert result == action
        mock_llm.assert_called_once()

    def test_returned_value_is_string_not_enum_instance(self, monkeypatch):
        """choose_remediation returns a plain str, not a RemediationChoice enum."""
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        mock_llm = MagicMock(return_value="retry_fetch")

        result = choose_remediation(_fresh_state(), "fetch err", llm_fn=mock_llm)

        assert isinstance(result, str)
        assert result == "retry_fetch"

    def test_llm_response_with_leading_trailing_whitespace(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        mock_llm = MagicMock(return_value="  clear_cache  ")

        result = choose_remediation(_fresh_state(), "cache corrupt", llm_fn=mock_llm)

        assert result == RemediationChoice.clear_cache.value


# ---------------------------------------------------------------------------
# 4. try_recovery with recovery_count=2 -> limit reached, no LLM call
# ---------------------------------------------------------------------------


class TestRecoveryLimit:
    """When recovery_count >= RECOVERY_LIMIT the reflex is exhausted."""

    def test_at_limit_returns_escalate_tuple(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _fresh_state(recovery_count=RECOVERY_LIMIT)
        mock_llm = MagicMock(return_value="retry_fetch")
        mock_dispatch = MagicMock()
        observe_calls: List[tuple] = []

        recovered, choice, msg = try_recovery(
            state,
            "unrecognised fetch failure",
            mock_dispatch,
            lambda s, c, r: observe_calls.append((s, c, r)),
            llm_fn=mock_llm,
        )

        assert recovered is False
        assert choice == RemediationChoice.escalate.value
        assert msg == "recovery limit reached"
        mock_llm.assert_not_called()
        mock_dispatch.assert_not_called()

    def test_above_limit_also_blocked(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _fresh_state(recovery_count=RECOVERY_LIMIT + 5)
        mock_llm = MagicMock(return_value="clear_cache")

        recovered, choice, _ = try_recovery(
            state,
            "any failure",
            _noop_dispatch,
            _noop_observe,
            llm_fn=mock_llm,
        )

        assert recovered is False
        assert choice == RemediationChoice.escalate.value
        mock_llm.assert_not_called()

    def test_recovery_limit_constant_is_two(self):
        assert RECOVERY_LIMIT == 2


# ---------------------------------------------------------------------------
# 5. Simulated unrecognised fetch failure -> DISPATCH invoked, recovered=True,
#    recovery_log updated
# ---------------------------------------------------------------------------


class TestSuccessfulRecoveryDispatch:
    """try_recovery with a fetch-failure scenario and a cooperative mock DISPATCH."""

    def test_fetch_failure_triggers_dispatch_and_returns_recovered(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _fresh_state(recovery_count=0)
        dispatched: List[tuple] = []

        def mock_dispatch(action: str, ctx: dict) -> None:
            dispatched.append((action, ctx))

        mock_llm = MagicMock(return_value="retry_fetch")
        observe_calls: List[tuple] = []

        recovered, choice, msg = try_recovery(
            state,
            "unrecognised fetch failure: pkg registry connection refused",
            mock_dispatch,
            lambda s, c, r: observe_calls.append((s, c, r)),
            llm_fn=mock_llm,
        )

        assert recovered is True
        assert choice == RemediationChoice.retry_fetch.value
        assert len(dispatched) == 1
        action_used, ctx = dispatched[0]
        assert action_used == "retry_fetch"
        assert "failure_info" in ctx

    def test_recovery_log_updated_after_successful_dispatch(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _fresh_state(recovery_count=0)
        mock_llm = MagicMock(return_value="retry_fetch")

        recovered, choice, msg = try_recovery(
            state,
            "unrecognised fetch failure",
            _noop_dispatch,
            _noop_observe,
            llm_fn=mock_llm,
        )

        assert recovered is True
        assert state["recovery_count"] == 1
        assert len(state["recovery_log"]) == 1
        assert "retry_fetch" in state["recovery_log"][0]

    def test_recovery_count_incremented_each_attempt(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _fresh_state(recovery_count=0)
        mock_llm = MagicMock(return_value="clear_cache")

        try_recovery(state, "fetch failure A", _noop_dispatch, _noop_observe, llm_fn=mock_llm)
        assert state["recovery_count"] == 1

        try_recovery(state, "fetch failure B", _noop_dispatch, _noop_observe, llm_fn=mock_llm)
        assert state["recovery_count"] == 2

        # Third attempt hits the limit; count must NOT be incremented further
        recovered, choice, msg = try_recovery(
            state, "fetch failure C", _noop_dispatch, _noop_observe, llm_fn=mock_llm
        )
        assert recovered is False
        assert state["recovery_count"] == 2  # unchanged at limit

    def test_clear_cache_choice_also_dispatched(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _fresh_state()
        dispatched: List[str] = []
        mock_llm = MagicMock(return_value="clear_cache")

        recovered, choice, _ = try_recovery(
            state,
            "stale cache detected",
            lambda a, _: dispatched.append(a),
            _noop_observe,
            llm_fn=mock_llm,
        )

        assert recovered is True
        assert choice == RemediationChoice.clear_cache.value
        assert dispatched == ["clear_cache"]


# ---------------------------------------------------------------------------
# 6. Recovery observability event emitted on each invocation
# ---------------------------------------------------------------------------


class TestObservability:
    """The observe callable receives (step, choice, result) on every try_recovery call."""

    def test_observe_called_on_successful_recovery(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _fresh_state()
        mock_observe = MagicMock()
        mock_llm = MagicMock(return_value="retry_fetch")

        try_recovery(state, "fetch err", _noop_dispatch, mock_observe, llm_fn=mock_llm)

        mock_observe.assert_called_once()
        args = mock_observe.call_args[0]
        step, choice, result = args
        assert step == "try_recovery"
        assert choice == RemediationChoice.retry_fetch.value
        assert isinstance(result, str) and result

    def test_observe_called_on_limit_exceeded(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _fresh_state(recovery_count=RECOVERY_LIMIT)
        mock_observe = MagicMock()

        try_recovery(state, "any err", _noop_dispatch, mock_observe)

        mock_observe.assert_called_once()
        step, choice, result = mock_observe.call_args[0]
        assert step == "try_recovery"
        assert choice == RemediationChoice.escalate.value
        assert "recovery limit" in result

    def test_observe_called_on_llm_escalation(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _fresh_state()
        mock_observe = MagicMock()
        mock_llm = MagicMock(return_value="escalate")

        try_recovery(state, "unfixable err", _noop_dispatch, mock_observe, llm_fn=mock_llm)

        mock_observe.assert_called_once()
        step, choice, result = mock_observe.call_args[0]
        assert step == "try_recovery"
        assert choice == RemediationChoice.escalate.value

    def test_observe_called_exactly_once_per_try_recovery_call(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _fresh_state()
        observe_count = {"n": 0}

        def counting_observe(s, c, r):
            observe_count["n"] += 1

        mock_llm = MagicMock(return_value="retry_fetch")

        try_recovery(state, "error 1", _noop_dispatch, counting_observe, llm_fn=mock_llm)
        assert observe_count["n"] == 1

        try_recovery(state, "error 2", _noop_dispatch, counting_observe, llm_fn=mock_llm)
        assert observe_count["n"] == 2

    def test_observe_receives_non_empty_result_string(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _fresh_state()
        captured: List[tuple] = []
        mock_llm = MagicMock(return_value="clear_cache")

        try_recovery(
            state,
            "cache issue",
            _noop_dispatch,
            lambda s, c, r: captured.append((s, c, r)),
            llm_fn=mock_llm,
        )

        assert len(captured) == 1
        _, _, result = captured[0]
        assert result.strip() != ""


# ---------------------------------------------------------------------------
# 7. Verification/finaliser paths not wrapped: no try_recovery when
#    submission_problems exist (minimal worker fixture)
# ---------------------------------------------------------------------------


class TestNoRecoveryOnSubmissionProblems:
    """The worker must NOT invoke try_recovery from the verification/finaliser
    code paths (i.e. when submission_problems are present). We verify this
    by creating a minimal MacWorker fixture and patching try_recovery to
    detect any unwanted call during the post-execution verification branch.
    """

    def test_try_recovery_not_called_when_submission_problems_present(
        self, tmp_path, monkeypatch
    ):
        """When the executor succeeds but submission_problems exist, the worker
        transitions to 'blocked' via the verification path. try_recovery must
        NOT be invoked in that branch."""
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")

        from mac.api import create_app
        from mac.services import ControlPlane
        from mac.worker import MacWorker, WorkerExecution
        from mac.hermes_adapter import MacApiClient, MacApiError
        from fastapi.testclient import TestClient

        cp = ControlPlane.in_memory()
        app = create_app(control_plane=cp, auth_tokens={})
        http = TestClient(app, raise_server_exceptions=True)

        def transport(method: str, path: str, payload):
            req = getattr(http, method.lower())
            kwargs = {}
            if payload is not None:
                kwargs["json"] = payload
            resp = req(path, **kwargs)
            if resp.status_code >= 400:
                raise MacApiError(resp.text)
            return resp.json() if resp.content else None

        client = MacApiClient("http://mac.test", transport=transport)
        machine = cp.register_machine("test-host")
        agent = cp.register_agent(machine.id, "worker", capabilities=["python"])

        # Executor succeeds but submission_problems will be injected.
        def executor(task, task_dir):
            return WorkerExecution(returncode=0, summary="ok")

        worker = MacWorker(
            client,
            agent.id,
            tmp_path,
            executor,
            lease_seconds=30,
        )

        # Create a task and dispatch it so the worker can claim it.
        cp.create_task("Test task", required_capabilities=["python"])
        assignment = cp.dispatch_once()
        assert assignment is not None

        # Patch _execution_submission_problems to return a non-empty problem list
        # so the worker enters the verification-failure branch.
        monkeypatch.setattr(
            worker,
            "_execution_submission_problems",
            lambda task_dir, evidence: ["missing repo.head_sha"],
        )

        # Patch try_recovery in the module to detect any unwanted call.
        recovery_calls: List[tuple] = []

        def spy_try_recovery(*args, **kwargs):
            recovery_calls.append(args)
            return False, "escalate", "should not be called"

        monkeypatch.setattr(hrr, "try_recovery", spy_try_recovery)

        result = worker.execute_assignment(assignment["task"], assignment["lease"])

        # The task should be blocked due to submission_problems.
        assert result.status == "blocked"
        # try_recovery must NOT have been called.
        assert recovery_calls == [], (
            "try_recovery was called unexpectedly during submission_problems handling: %r"
            % recovery_calls
        )

    def test_try_recovery_not_called_when_executor_fails(
        self, tmp_path, monkeypatch
    ):
        """When the executor itself fails (returncode != 0), the worker
        transitions to 'blocked'. try_recovery must not fire from the
        standard worker harness for executor failures either."""
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")

        recovery_calls: List[tuple] = []

        def spy_try_recovery(*args, **kwargs):
            recovery_calls.append(args)
            return False, "escalate", "unexpected call"

        # Build a worker whose executor always fails.
        from mac.api import create_app
        from mac.services import ControlPlane
        from mac.worker import MacWorker, WorkerExecution
        from fastapi.testclient import TestClient
        from mac.hermes_adapter import MacApiClient, MacApiError

        cp = ControlPlane.in_memory()
        app = create_app(control_plane=cp, auth_tokens={})
        http = TestClient(app, raise_server_exceptions=True)

        def transport(method, path, payload):
            req = getattr(http, method.lower())
            kw = {}
            if payload is not None:
                kw["json"] = payload
            r = req(path, **kw)
            if r.status_code >= 400:
                raise MacApiError(r.text)
            return r.json() if r.content else None

        client = MacApiClient("http://mac.test", transport=transport)
        machine = cp.register_machine("host2")
        agent = cp.register_agent(machine.id, "worker2", capabilities=[])
        worker = MacWorker(
            client,
            agent.id,
            tmp_path,
            lambda task, task_dir: WorkerExecution(returncode=1, summary="executor failed"),
            lease_seconds=30,
        )

        cp.create_task("Failing task")
        assignment = cp.dispatch_once()
        assert assignment is not None

        monkeypatch.setattr(hrr, "try_recovery", spy_try_recovery)

        result = worker.execute_assignment(assignment["task"], assignment["lease"])

        assert result.status == "blocked"
        assert recovery_calls == [], (
            "try_recovery was unexpectedly called: %r" % recovery_calls
        )


class TestRecoveryOnNonStandardPrepFailure:
    """Workspace preparation is an environment-prerequisite step. A prep
    failure that is NOT a ``RuntimeError``/``OSError`` (e.g. a git
    ``CalledProcessError``, a ``MacApiError`` from the fetch/rebase round-trip,
    or a ``KeyError`` from malformed task metadata) must still be routed
    through ``try_recovery`` instead of skipping recovery and wedging the
    assignment into a bare ``worker_exception`` -> blocked loop.
    """

    def _make_worker(self, tmp_path):
        from mac.api import create_app
        from mac.services import ControlPlane
        from mac.worker import MacWorker, WorkerExecution
        from mac.hermes_adapter import MacApiClient, MacApiError
        from fastapi.testclient import TestClient

        cp = ControlPlane.in_memory()
        app = create_app(control_plane=cp, auth_tokens={})
        http = TestClient(app, raise_server_exceptions=True)

        def transport(method, path, payload):
            req = getattr(http, method.lower())
            kw = {}
            if payload is not None:
                kw["json"] = payload
            r = req(path, **kw)
            if r.status_code >= 400:
                raise MacApiError(r.text)
            return r.json() if r.content else None

        client = MacApiClient("http://mac.test", transport=transport)
        machine = cp.register_machine("prep-host")
        agent = cp.register_agent(machine.id, "prep-worker", capabilities=[])
        worker = MacWorker(
            client,
            agent.id,
            tmp_path,
            lambda task, task_dir: WorkerExecution(returncode=0, summary="ok"),
            lease_seconds=30,
        )
        cp.create_task("Prep failure task")
        assignment = cp.dispatch_once()
        assert assignment is not None
        return worker, assignment

    def test_non_oserror_prep_failure_routes_through_recovery(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        worker, assignment = self._make_worker(tmp_path)

        # A prep failure that is neither RuntimeError nor OSError. Before the
        # fix this escaped the recovery handler entirely.
        def boom(task, lease):
            raise KeyError("repository_base_sha")

        monkeypatch.setattr(worker, "_prepare_task_workspace", boom)

        recovery_calls: List[tuple] = []

        def spy_try_recovery(*args, **kwargs):
            recovery_calls.append(args)
            return False, "escalate", "cannot recover"

        monkeypatch.setattr(hrr, "try_recovery", spy_try_recovery)

        # An unrecovered prep failure re-raises out of execute_assignment (by
        # design) AFTER the worker records diagnostics and posts the blocked
        # transition. Capture the re-raise so we can assert on both effects.
        task_id = assignment["task"]["id"]
        with pytest.raises(KeyError):
            worker.execute_assignment(
                assignment["task"], assignment["lease"]
            )

        # try_recovery MUST have been consulted for the prep failure.
        assert len(recovery_calls) == 1, (
            "try_recovery was not invoked for a non-OSError prep failure: %r"
            % recovery_calls
        )
        # The task is blocked with a diagnosable worker_exception (traceback in
        # output_tail), not a silent, output-less wedge.
        task = worker.client.get("/tasks/%s" % task_id)["task"]
        assert task["state"] == "blocked"
        detail = task["metadata"]["activity"][-1]["detail"]
        assert detail["failure"] == "worker_exception"
        assert detail.get("output_tail")
        assert not detail.get("output_tail_unavailable_reason")

    def test_recovered_prep_failure_retries_preparation(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        worker, assignment = self._make_worker(tmp_path)

        calls = {"n": 0}
        real_prepare = worker._prepare_task_workspace

        def flaky_prepare(task, lease):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyError("transient prep glitch")
            return real_prepare(task, lease)

        monkeypatch.setattr(worker, "_prepare_task_workspace", flaky_prepare)

        def spy_try_recovery(*args, **kwargs):
            return True, "retry", "recovered"

        monkeypatch.setattr(hrr, "try_recovery", spy_try_recovery)

        result = worker.execute_assignment(
            assignment["task"], assignment["lease"]
        )

        # Preparation was retried after a successful recovery decision.
        assert calls["n"] == 2
        # The task proceeds past prep (no longer wedged on the prep failure).
        assert result.status != "no_task"


def test_try_recovery_without_dispatcher_does_not_claim_it_dispatched(monkeypatch):
    """A caller with no remediation dispatcher must not get a 'dispatched' log.

    The worker passes None at every call site; recording "dispatched <action>"
    there made recovery_log report repairs that never happened, which is the
    signal operators and this fleet's own agents read to judge fleet health.
    """

    monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
    state: dict = {}
    seen = []
    recovered, choice, message = try_recovery(
        state,
        "worktree preparation exploded",
        None,
        lambda step, ch, res: seen.append((step, ch, res)),
        llm_fn=lambda _prompt: "retry_fetch",
    )

    assert recovered is True  # the retry decision is still honoured
    assert "dispatched" not in message
    assert "no remediation dispatcher wired" in message
    assert state["recovery_count"] == 1
    assert seen and "dispatched" not in seen[0][2]


def test_try_recovery_with_dispatcher_still_reports_dispatch(monkeypatch):
    monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
    calls = []
    recovered, choice, message = try_recovery(
        {},
        "boom",
        lambda action, ctx: calls.append((action, ctx)),
        lambda *_: None,
        llm_fn=lambda _prompt: "retry_fetch",
    )

    assert recovered is True
    assert calls, "a real dispatcher must still be invoked"
    assert message.startswith("dispatched ")
