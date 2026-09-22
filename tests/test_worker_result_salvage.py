"""Late executor exits preserve only deterministically accepted deliverables."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.models import TaskState
from mac.services import ControlPlane
from mac.worker import MacWorker, WorkerExecution, _salvage_accepted_late_exit


def _transport(client: TestClient):
    def transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Any:
        request = getattr(client, method.lower())
        response = request(path, **({"json": payload} if payload is not None else {}))
        if response.status_code >= 400:
            raise MacApiError(response.text)
        return response.json() if response.content else None

    return transport


def _canary_metadata(expected: object) -> dict:
    canonical = json.dumps(expected, sort_keys=True, separators=(",", ":"))
    return {
        "schema": "mac.canary_workload.v1",
        "canary": True,
        "workload": {
            "family": "late_exit_salvage",
            "expected_result_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        },
        "execution_contract": {
            "schema": "mac.task_execution_contract.v1",
            "type": "operator_directive",
            "repository_required": False,
            "evidence_type": "operator_result",
        },
    }


def _manifest(result: str) -> dict:
    return {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "operator_result",
        "summary": "Completed deterministic canary before harness teardown failed.",
        "operator_result": {
            "summary": "Completed deterministic canary before harness teardown failed.",
            "result": result,
        },
    }


def _run_late_exit(tmp_path: Path, metadata: dict, manifest: dict):
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("late-exit-worker-host")
    agent = cp.register_agent(machine.id, "late-exit-worker", capabilities=["python"])
    task = cp.create_task(
        "Late-exit deterministic canary",
        required_capabilities=["python"],
        metadata=metadata,
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(_task: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        result_text = manifest["operator_result"]["result"]
        return WorkerExecution(
            17,
            "harness teardown failed after result emission",
            stdout=result_text,
            stderr="late transport failure\n",
        )

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=_transport(client)),
        agent.id,
        tmp_path,
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )
    return cp, task, worker.run_once()


def test_late_nonzero_exit_preserves_complete_typed_accepted_deliverable(tmp_path: Path) -> None:
    payload = {"answer": 42}
    marker = "MAC_CANARY_RESULT=" + json.dumps(payload, sort_keys=True, separators=(",", ":"))

    cp, task, result = _run_late_exit(
        tmp_path,
        _canary_metadata(payload),
        _manifest("work log\n%s" % marker),
    )

    assert result.status == "submitted_for_review"
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value
    evidence = cp.list_evidence(task.id)[0]
    assert evidence.metadata["returncode"] == 0
    salvage = evidence.metadata["late_exit_salvage"]
    assert salvage["original_returncode"] == 17
    assert salvage["acceptance"]["required"] is True
    assert salvage["acceptance"]["status"] == "pass"


@pytest.mark.parametrize(
    ("metadata", "result_text"),
    [
        ({}, 'MAC_CANARY_RESULT={"answer":42}'),
        (_canary_metadata({"answer": 42}), "arbitrary prose without a result marker"),
        (_canary_metadata({"answer": 42}), 'MAC_CANARY_RES={"answer":42}'),
        (
            _canary_metadata({"answer": 42}),
            'MAC_CANARY_RESULT={"answer":42}\nMAC_CANARY_RESULT={"answer":42}',
        ),
        (_canary_metadata({"answer": 42}), 'MAC_CANARY_RESULT={"answer":41}'),
    ],
    ids=[
        "acceptance-absent",
        "arbitrary-output",
        "partial-marker",
        "duplicate-marker",
        "replacement-marker",
    ],
)
def test_late_nonzero_exit_fails_closed_without_exact_typed_acceptance(
    tmp_path: Path,
    metadata: dict,
    result_text: str,
) -> None:
    execution = WorkerExecution(17, "late transport failure")
    (tmp_path / "mac-evidence.json").write_text(
        json.dumps(_manifest(result_text), sort_keys=True),
        encoding="utf-8",
    )

    candidate, acceptance = _salvage_accepted_late_exit(
        {"metadata": metadata},
        tmp_path,
        execution,
    )

    assert candidate is execution
    assert candidate.returncode == 17
    assert acceptance is None or acceptance["status"] != "pass"
