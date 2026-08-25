from types import SimpleNamespace

from mac.attempt_failure_classifier import classify_attempt_failure


def test_attempt_failure_classifier_collects_salvage_pointers():
    result = classify_attempt_failure(
        [
            {
                "event_type": "worker.repository.adopted_pushed_branch",
                "detail": {
                    "checks": {
                        "branch_pushed": True,
                        "repository_branch": "mac/agent/task",
                    }
                },
            },
            {
                "event_type": "memory.learning_recorded",
                "detail": {
                    "id": "memory-1",
                    "lessons": [{"id": "memory-2"}, 3],
                },
            },
            {
                "event_type": "plan.child_published",
                "detail": {"children": [{"id": "task_child"}]},
            },
        ]
    )

    assert result.failure_class == "work"
    assert result.salvage["pushed_branch"] == "mac/agent/task"
    assert result.salvage["recorded_lessons"] == ["memory-2", "3", "memory-1"]
    assert result.salvage["published_children"] == ["task_child"]


def test_attempt_failure_classifier_handles_branch_pushed_without_branch_name():
    result = classify_attempt_failure(
        [{"event_type": "task.evidence_added", "detail": {"branch_pushed": True}}]
    )

    assert result.failure_class == "work"
    assert result.salvage == {"branch_pushed": True}


def test_attempt_failure_classifier_classifies_scope_from_object_history():
    result = classify_attempt_failure(
        [
            SimpleNamespace(
                event_type="task.transitioned",
                detail={"error": "returncode 124", "problems": ["agent run timed out"]},
            )
        ]
    )

    assert result.failure_class == "scope"


def test_mac_evidence_api_timeout_is_shared_environment_not_task_scope():
    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "executor_failed",
                    "error": "MAC API /evidence POST failed: request timed out",
                },
            }
        ]
    )

    assert result.failure_class == "environment"


def test_attempt_failure_classifier_superseded_preempts_environment():
    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "heartbeat_offline",
                    "failure_class": "superseded",
                },
            }
        ]
    )

    assert result.failure_class == "superseded"


def test_bare_executor_failed_is_not_an_environment_verdict():
    """``executor_failed`` is a transport label, not a cause.

    worker.py stamps it on EVERY non-zero executor exit, including a correct
    "the agent's tests failed". Treating it as an environment marker made 61%
    of the live ledger's failures "environment" with no evidence behind it --
    the classifier reading its own input field.
    """

    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "executor_failed",
                    "manual_repair_required": True,
                },
            }
        ]
    )

    assert result.failure_class != "environment"


def test_executor_failed_with_a_real_environment_signal_still_classifies():
    """The label is not the signal; an actual cause in a structured field is."""

    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "executor_failed",
                    "error": "connection refused talking to the sandbox host",
                },
            }
        ]
    )

    assert result.failure_class == "environment"


def test_captured_test_output_does_not_drive_classification():
    """Real pytest output reaches the detail now that diagnosis joins to the
    durable stdout/stderr artifacts. It must not be matched for markers: a
    failing test log routinely prints "no such file or directory"."""

    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "verification_contract_failed",
                    "problems": ["contract verification failed"],
                    "output_tail": (
                        "E   FileNotFoundError: no such file or directory: 'x.txt'\n"
                        "E   assert 1 == 2\n"
                        "FAILED tests/test_x.py::test_y\n"
                    ),
                },
            }
        ]
    )

    assert result.failure_class != "environment"


def test_bare_executor_failed_in_mixed_history_still_salvages():
    result = classify_attempt_failure(
        [
            {
                "event_type": "task.updated",
                "detail": {"pushed_branch": "mac/agent/task"},
            },
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "executor_failed",
                    "manual_repair_required": True,
                    "returncode": 1,
                },
            },
        ]
    )

    assert result.failure_class != "environment"
    # Salvage is independent of classification and must still be collected.
    assert result.salvage.get("pushed_branch") == "mac/agent/task"


def test_bare_worker_exception_is_not_an_environment_verdict():
    """Also a transport label: it says the worker raised, not why."""

    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "worker_exception",
                    "failure": "worker_exception",
                },
            }
        ]
    )

    assert result.failure_class != "environment"


def test_attempt_failure_classifier_classifies_network_error_as_environment():
    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "agent went offline",
                    "error": "network connection refused",
                },
            }
        ]
    )

    assert result.failure_class == "environment"


def test_attempt_failure_classifier_classifies_rate_limit_as_environment():
    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "error": "rate limit exceeded, please retry",
                },
            }
        ]
    )

    assert result.failure_class == "environment"


def test_attempt_failure_classifier_classifies_command_not_found_as_environment():
    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "error": "command not found: python3",
                },
            }
        ]
    )

    assert result.failure_class == "environment"


def test_attempt_failure_classifier_classifies_authentication_failed_as_environment():
    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "worker_exception",
                    "error": "authentication failed: could not clone repository",
                },
            }
        ]
    )

    assert result.failure_class == "environment"


def test_attempt_failure_classifier_scope_preempts_environment():
    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "worker_exception",
                    "problems": ["agent run timed out"],
                },
            }
        ]
    )

    assert result.failure_class == "scope"


def test_attempt_failure_classifier_empty_history_classifies_as_work():
    result = classify_attempt_failure([])

    assert result.failure_class == "work"
    assert result.salvage == {}


def test_attempt_failure_classifier_classifies_sandbox_policy_denial_as_environment():
    # A coding-agent preflight refused by the OpenShell egress policy is an
    # environment prerequisite (repair the sandbox policy/route), not new work
    # to retry against the same denied destination.
    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "executor_failed",
                    "failure_class": "sandbox_policy_denied",
                    "error": (
                        "coding-agent preflight: POST "
                        "host.openshell.internal:8789/v1/responses not "
                        "permitted by policy"
                    ),
                },
            }
        ]
    )

    assert result.failure_class == "environment"


def test_a_worker_with_no_execution_boundary_is_an_environment_failure_and_retries():
    """The worker could not run anything; the task's work was never at fault.

    Observed live 2026-08-02: the executor refuses to launch without a sandbox,
    worker.py stamped manual_repair_required=True, and the retry policy read
    that as non_retryable -- so the task went terminal at attempt 1 of 3 with
    two attempts unused, on a worker that could never have run it. The same
    task on a worker that has OpenShell runs normally, so this must move rather
    than burn the task.
    """
    from mac.services import _blocked_attempt_retry_kind
    from mac.worker import WorkerExecution, _executor_failure_transition_detail

    refusal = (
        "[executor] coding-agent routing: claude\n"
        "RuntimeError: refusing to launch an approval-bypassed agent without an "
        "OpenShell sandbox: MAC_OPENSHELL_SANDBOX is unset and "
        "MAC_ALLOW_UNSANDBOXED_YOLO is disabled."
    )
    detail = _executor_failure_transition_detail(
        WorkerExecution(
            returncode=1, summary="executor failed", stdout="", stderr=refusal, metadata={}
        ),
        evidence_id="ev_boundary",
    )

    assert detail["failure"] == "executor_execution_boundary_unavailable"
    assert detail["manual_repair_required"] is False
    assert _blocked_attempt_retry_kind(detail) == "transient"
    assert (
        classify_attempt_failure(
            [{"event_type": "task.transitioned", "detail": detail}]
        ).failure_class
        == "environment"
    )


def test_an_agents_own_test_failure_is_still_work_and_still_stops():
    """The counterpart guard: this must not become a licence to retry bad work.

    worker.py stamps executor_failed on EVERY non-zero exit, so matching that
    label would classify a correct "the agent's tests failed" as environment --
    which is how the ledger once showed a 61% environment rate.
    """
    from mac.services import _blocked_attempt_retry_kind
    from mac.worker import WorkerExecution, _executor_failure_transition_detail

    detail = _executor_failure_transition_detail(
        WorkerExecution(
            returncode=1,
            summary="contract tests failed",
            stdout="",
            stderr="2 tests failed: assertion error in test_foo",
            metadata={},
        ),
        evidence_id="ev_work",
    )

    assert detail.get("failure") is None
    assert detail["manual_repair_required"] is True
    assert _blocked_attempt_retry_kind(detail) == "non_retryable"
    assert (
        classify_attempt_failure(
            [{"event_type": "task.transitioned", "detail": detail}]
        ).failure_class
        == "work"
    )


def test_plan_decomposed_contract_text_is_not_a_scope_marker():
    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "verification_contract_failed",
                    "problems": ["plan_decomposed evidence requires a non-empty children list"],
                },
            }
        ]
    )
    assert result.failure_class != "scope"


def test_sandbox_hub_environment_fault_is_environment_not_scope():
    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "sandbox_hub_environment_fault",
                    "failure_class": "environment",
                    "problems": [
                        "plan_decomposed evidence could not be routed to durable child tasks"
                    ],
                },
            }
        ]
    )
    assert result.failure_class == "environment"
