"""Report/answer (non-code) deliverable tasks.

A task declared ``metadata.deliverable == "report"`` is satisfied by a
substantive ``operator_result`` — no repo diff, no pushed branch — so
investigation / triage / answer tasks (and system-smoke tasks) run without
faking a code change, while the code-substance gate is untouched for real
code tasks.
"""
from __future__ import annotations

import pytest

from mac import task_executor as te
from mac import worker as wk
from mac.models import (
    REPORT_DELIVERABLE,
    metadata_declares_report_deliverable,
    normalize_deliverable_kind,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("report", "report"),
        ("answer", "report"),
        ("Investigation", "report"),
        ("TRIAGE", "report"),
        ("code", ""),
        ("", ""),
        (None, ""),
        ("weird", "weird"),
    ],
)
def test_normalize_deliverable_kind(value, expected):
    assert normalize_deliverable_kind(value) == expected


def test_predicate_reads_metadata():
    assert metadata_declares_report_deliverable({"deliverable": "report"})
    assert metadata_declares_report_deliverable({"deliverable": "answer"})
    assert not metadata_declares_report_deliverable({"deliverable": "code"})
    assert not metadata_declares_report_deliverable({})
    assert not metadata_declares_report_deliverable(None)


def _repo_task(deliverable=None):
    md = {
        "origin": {
            "repository_path": "/repo",
            "repository_contract": {"schema": "mac.repo.v1", "test": {"command": "make test"}},
        },
        "execution_contract": {"type": "repository"},
    }
    if deliverable:
        md["deliverable"] = deliverable
    return {"id": "task_x", "metadata": md}


def test_report_declaration_flips_all_repo_coupling_checks():
    code_task = _repo_task()
    report_task = _repo_task(deliverable="report")

    # executor repo-coupling
    assert te.task_is_repo_coupled(code_task) is True
    assert te.task_is_repo_coupled(report_task) is False

    # worker worktree/finalizer trigger
    assert wk._repository_task_origin(code_task) is not None
    assert wk._repository_task_origin(report_task) is None


def test_report_task_uses_operator_result_fallback(tmp_path):
    """The executor fallback writes operator_result for a report task even
    though it carries a repository contract (would be repo_change otherwise)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    task = _repo_task(deliverable="report")
    result = type("R", (), {"returncode": 0, "stdout": "Investigated: config is correct.\n", "stderr": ""})()
    te.write_fallback_evidence_manifest(ws, task, result, None)
    import json

    manifest = json.loads((ws / "mac-evidence.json").read_text())
    assert manifest["evidence_type"] == "operator_result"
    assert manifest["status"] == "complete"


def test_code_task_still_blocks_operator_result_fallback(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    task = _repo_task()  # code
    result = type("R", (), {"returncode": 0, "stdout": "hi", "stderr": ""})()
    te.write_fallback_evidence_manifest(ws, task, result, None)
    # Repo-coupled code task: fallback refuses to fabricate operator_result.
    assert not (ws / "mac-evidence.json").exists()


def test_services_repo_coupled_and_operator_result_enforcement_honor_report():
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    report = cp.create_task("report task", metadata=_repo_task(deliverable="report")["metadata"])
    code = cp.create_task("code task", metadata=_repo_task()["metadata"])

    assert cp._task_is_repo_coupled(report) is False
    assert cp._task_is_repo_coupled(code) is True
    # operator_result enforcement: raises for the code task, silent for report.
    md_op = {"verification": {"evidence_type": "operator_result"}}
    cp._enforce_repo_coupled_evidence_type(report, md_op)  # no raise
    with pytest.raises(Exception, match="operator_result"):
        cp._enforce_repo_coupled_evidence_type(code, md_op)
