"""Honest turn-outcome & mirror-provenance semantics (task_7f2ce5e4).

Regression for incident task_60be7f29: AgentBus *delivery* succeeded while the
peer *turn* had failed, yet every case was signed peer.reply status "ok" and
mirrored to Slack as a success-looking summary. These tests pin the exact
observed cases:

  * a 300-second embedded turn timeout (LLM request failed / timed out),
  * a real find-tool failure,
  * an output-length (truncation) stop,
  * 20s and 30s synchronous wait expiry followed by a later reply,
  * an ordinary successful exchange,

and assert the structured outcome, the honest (never-ok) reply status, the
delivery-vs-turn separation, the late-reply correlation, and the mirror
provenance that keeps a mirror from being read as execution evidence.
"""

from __future__ import annotations

from pathlib import Path

from mac.agentbus_control import peer_reply_payload
from mac.agentbus_outcomes import (
    DELIVERY_ACKNOWLEDGED,
    DELIVERY_LATE_REPLY,
    DELIVERY_REPLIED,
    DELIVERY_WAIT_EXPIRED,
    NON_OK_REPLY_STATUSES,
    TURN_COMPLETED,
    TURN_MODEL_FAILED,
    TURN_OUTPUT_TRUNCATED,
    TURN_TIMEOUT,
    TURN_TOOL_FAILED,
    classify_turn_result,
    delivery_outcome,
    mirror_provenance,
    reply_status_for_outcome,
)
from mac.agentbus_schemas import validate_payload
from mac.worker import MacWorker, WorkerExecution
from mac.worker_directable import DirectableMixin


# --------------------------------------------------------------------------- #
# 1. classify_turn_result — the exact observed failure shapes.
# --------------------------------------------------------------------------- #
def test_embedded_300s_turn_timeout_is_not_ok() -> None:
    # Rocky's incident: the embedded turn hit the OpenClaw turn limit and
    # returned "LLM request failed / timed out" as ordinary reply text.
    text = "LLM request failed / timed out after 300 seconds."
    # Model-failure fingerprint wins the classification (the literal message).
    assert classify_turn_result({"payloads": [{"text": text}]}, text) == TURN_MODEL_FAILED
    assert reply_status_for_outcome(TURN_MODEL_FAILED) == "failed"

    # A pure turn-limit/timeout stop reason maps to turn_timeout, never ok.
    assert classify_turn_result({"stop_reason": "turn_limit"}, "partial work") == TURN_TIMEOUT
    assert reply_status_for_outcome(TURN_TIMEOUT) == "timeout"
    assert "ok" not in {
        reply_status_for_outcome(TURN_TIMEOUT),
        reply_status_for_outcome(TURN_MODEL_FAILED),
    }


def test_failed_find_tool_is_tool_failed() -> None:
    # Bullwinkle's incident: a real sandbox find-tool failure embedded in text.
    text = "The find tool failed: no such tool 'find' in this sandbox."
    assert classify_turn_result({"payloads": [{"text": text}]}, text) == TURN_TOOL_FAILED
    # Structured tool_status is honored too.
    assert classify_turn_result({"tool_status": "failed"}, "ran the search") == TURN_TOOL_FAILED
    assert reply_status_for_outcome(TURN_TOOL_FAILED) == "failed"


def test_output_length_stop_is_truncated() -> None:
    assert (
        classify_turn_result({"finish_reason": "length"}, "a long answer...")
        == TURN_OUTPUT_TRUNCATED
    )
    assert (
        classify_turn_result(
            {"payloads": [{"text": "answer"}]},
            "answer (response truncated: output length limit reached)",
        )
        == TURN_OUTPUT_TRUNCATED
    )
    assert reply_status_for_outcome(TURN_OUTPUT_TRUNCATED) == "truncated"


def test_ordinary_success_is_completed_and_ok() -> None:
    result = {"payloads": [{"text": "Yes, send me the branch."}]}
    assert classify_turn_result(result, "Yes, send me the branch.") == TURN_COMPLETED
    assert reply_status_for_outcome(TURN_COMPLETED) == "ok"
    # A clean end_turn stop reason with plain text stays completed.
    assert classify_turn_result({"stop_reason": "end_turn"}, "done") == TURN_COMPLETED


def test_every_non_completed_outcome_maps_to_a_non_ok_status() -> None:
    for outcome in (
        TURN_TIMEOUT,
        TURN_OUTPUT_TRUNCATED,
        TURN_TOOL_FAILED,
        TURN_MODEL_FAILED,
    ):
        assert reply_status_for_outcome(outcome) in NON_OK_REPLY_STATUSES
        assert reply_status_for_outcome(outcome) != "ok"


# --------------------------------------------------------------------------- #
# 2. Delivery vs. turn: 20s / 30s wait expiry then a later (late) reply.
# --------------------------------------------------------------------------- #
def test_20s_and_30s_wait_expiry_then_late_reply_is_correlated_not_lost() -> None:
    # The synchronous 20s and 30s waits expired before a valid reply arrived.
    assert (
        delivery_outcome(wait_budget_seconds=20, reply_present=False, reply_within_budget=False)
        == DELIVERY_WAIT_EXPIRED
    )
    assert (
        delivery_outcome(wait_budget_seconds=30, reply_present=False, reply_within_budget=False)
        == DELIVERY_WAIT_EXPIRED
    )

    # The valid reply arriving later is surfaced as LATE (still correlated),
    # not as a fresh success and not lost/duplicated.
    late = delivery_outcome(wait_budget_seconds=30, reply_present=True, reply_within_budget=False)
    assert late == DELIVERY_LATE_REPLY

    # Within-budget and fire-and-forget stay distinct.
    assert (
        delivery_outcome(wait_budget_seconds=30, reply_present=True, reply_within_budget=True)
        == DELIVERY_REPLIED
    )
    assert (
        delivery_outcome(wait_budget_seconds=0, reply_present=False, reply_within_budget=False)
        == DELIVERY_ACKNOWLEDGED
    )


def test_late_reply_payload_stays_correlated_to_original_stream() -> None:
    payload = peer_reply_payload(
        from_agent_id="agent_rocky",
        to_agent_id="agent_natasha",
        reply="here is the answer you waited for",
        status="ok",
        correlation_id="corr-late",
        in_reply_to="bus_original",
        turn_outcome=TURN_COMPLETED,
        late=True,
    )
    assert payload["late"] is True
    assert payload["correlation_id"] == "corr-late"
    assert payload["in_reply_to"] == "bus_original"
    # And it still validates against the registered peer_reply schema.
    _, problems = validate_payload(payload)
    assert problems == []


# --------------------------------------------------------------------------- #
# 3. Mirror provenance — never accepted as task-execution evidence.
# --------------------------------------------------------------------------- #
def test_mirror_provenance_marks_summary_and_binding() -> None:
    persona = mirror_provenance(
        source_stream_id="bus_x",
        source_status="ok",
        reply_status="failed",
        task_executor_bound=False,
        summarizer_model="azure/anthropic/claude-sonnet-4-6",
    )
    assert persona["summary_is_model_generated"] is True
    assert persona["is_execution_evidence"] is False
    assert persona["turn_binding"] == "persona"
    assert persona["reply_status"] == "failed"
    assert persona["summarizer_model"]

    executor = mirror_provenance(source_stream_id="bus_y", task_executor_bound=True)
    assert executor["turn_binding"] == "task_executor"
    assert executor["is_execution_evidence"] is False


def test_mirror_record_validates_with_provenance() -> None:
    record = {
        "schema": "mac.fleet_conversation_mirror.v1",
        "stream_id": "bus_z",
        "sender_agent_id": "agent_rocky",
        **mirror_provenance(source_stream_id="bus_z", reply_status="timeout"),
    }
    schema_name, problems = validate_payload(record)
    assert schema_name == "mac.fleet_conversation_mirror.v1"
    assert problems == []


# --------------------------------------------------------------------------- #
# 4. Worker-directable end-to-end: honest status is signed on the wire.
# --------------------------------------------------------------------------- #
class _Client:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    def get(self, path: str):
        return []

    def post(self, path: str, body):
        self.posts.append((path, body))
        return {}


def _worker(tmp_path: Path) -> MacWorker:
    return MacWorker(
        _Client(),
        "agent_worker",
        tmp_path,
        lambda _task, _task_dir: WorkerExecution(0, "unused"),
    )


def _publish_and_capture(worker: MacWorker, turn_result) -> dict:
    stream = {"id": "bus_peer", "sender_agent_id": "agent_sender"}

    def fake_run(self, prompt, **kw):
        return turn_result

    # Bind the fake to the instance so _directable_turn_outcome normalizes it.
    worker._run_directable_turn = fake_run.__get__(worker, MacWorker)
    text, outcome = worker._directable_turn_outcome("prompt", stream_id="bus_peer")
    status = reply_status_for_outcome(outcome)
    worker._publish_peer_reply(
        stream, text, status=status, correlation_id="corr", turn_outcome=outcome
    )
    return worker.client.posts[-1][1]["payload"]


def test_worker_directable_signs_tool_failure_as_failed_not_ok(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    payload = _publish_and_capture(
        worker, ("The find tool failed: no such tool 'find'.", TURN_TOOL_FAILED)
    )
    assert payload["status"] == "failed"
    assert payload["status"] != "ok"
    assert payload["turn_outcome"] == TURN_TOOL_FAILED


def test_worker_directable_bare_string_failure_is_reclassified(tmp_path: Path) -> None:
    # A test double / legacy caller returning a bare string must still be
    # reclassified so an embedded failure is never silently signed ok.
    worker = _worker(tmp_path)
    payload = _publish_and_capture(worker, "LLM request failed / timed out.")
    assert payload["status"] != "ok"
    assert payload["turn_outcome"] in {TURN_MODEL_FAILED, TURN_TIMEOUT}


def test_worker_directable_success_is_ok_without_outcome_noise(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    payload = _publish_and_capture(worker, ("All done, build is green.", TURN_COMPLETED))
    assert payload["status"] == "ok"
    assert payload["turn_outcome"] == TURN_COMPLETED


def test_directable_mixin_exposes_turn_outcome_helper() -> None:
    # The public seam both call sites use exists on the mixin.
    assert hasattr(DirectableMixin, "_directable_turn_outcome")
