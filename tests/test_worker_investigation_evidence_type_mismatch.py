"""Regression: a task whose own execution contract declares a non-repository
outcome (evidence_type=investigation, repository_required=False -- e.g. the
`mac project register` onboarding task, whose own instructions say "do NOT
push or open a pull request") must not be blocked by demanding
pushed=true/pr_url just because the agent's own evidence manifest defaulted
to evidence_type="repo_change" (the executor's generic default whenever it
made a git commit). Confirmed live: an agent (bullwinkle) did the onboarding
work correctly -- authored .mac/project.yaml, committed it locally exactly as
instructed -- and was blocked with "repo evidence requires pushed=true with
remote_ref, or pr_url" anyway, because the worker trusted the agent's
(wrong) manifest evidence_type over the task's own declared contract.

The task's declared execution_contract is the operator's authoritative
intent; the worker must coerce a mismatched agent-claimed evidence_type back
to the task's declared non-repository outcome rather than reject the work.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from mac import worker
from mac.worker import MacWorker


def _write_task(task_dir: Path, task_id: str, *, metadata: Dict[str, Any]) -> None:
    (task_dir / "task.json").write_text(
        json.dumps(
            {"task": {"id": task_id, "title": "author project contract", "metadata": metadata}}
        ),
        encoding="utf-8",
    )


def _investigation_task_metadata() -> Dict[str, Any]:
    # Mirrors ControlPlane._register_repository_onboarding_task's metadata
    # shape: a top-level evidence_type hint plus the normalized
    # execution_contract it drives (mac.task_execution_contract.v1,
    # reason=explicit_non_repository_outcome, repository_required=False).
    return {
        "evidence_type": "investigation",
        "execution_contract": {
            "schema": "mac.task_execution_contract.v1",
            "type": "operator_directive",
            "evidence_type": "investigation",
            "quality": "weak",
            "reason": "explicit_non_repository_outcome",
            "repository_required": False,
            "repository_context": {"workflow_role": "work"},
            "required_capabilities": [],
        },
    }


def _manifest(evidence_type: str, *, pushed: bool = False) -> Dict[str, Any]:
    repo: Dict[str, Any] = {"head_sha": "a" * 40, "dirty": False, "pushed": pushed}
    return {
        "schema": worker.VERIFICATION_SCHEMA,
        "status": "complete",
        "evidence_type": evidence_type,
        "signed_by": "agent-test",
        "signature": "sig",
        "repo": repo,
        "summary": "authored .mac/project.yaml and committed it locally",
    }


def _make_worker() -> MacWorker:
    return MacWorker.__new__(MacWorker)  # type: ignore[call-arg]


def test_agent_mislabeled_repo_change_is_coerced_to_declared_investigation(tmp_path: Path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_task(task_dir, "task_onboard", metadata=_investigation_task_metadata())

    # The agent did the work correctly (committed locally, did not push, per
    # its instructions) but its own tooling defaulted evidence_type to
    # "repo_change" rather than the task-declared "investigation".
    manifest = _manifest("repo_change", pushed=False)
    evidence = {"metadata": {"verification": manifest}}

    w = _make_worker()
    problems = w._execution_submission_problems(task_dir, evidence)

    assert problems == [], (
        "a task-declared non-repository outcome must not be rejected for "
        "missing push evidence just because the agent's manifest claimed "
        "repo_change: %r" % (problems,)
    )


def test_agent_falsely_claiming_investigation_is_still_rejected(tmp_path: Path):
    # The reverse direction (already covered, kept here as a paired
    # sanity check so the two behaviors are visibly symmetric in one file):
    # an agent cannot unilaterally claim evidence_type=investigation on an
    # ordinary repo_change task to dodge push requirements.
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_task(task_dir, "task_ordinary", metadata={})

    manifest = _manifest("investigation", pushed=False)
    evidence = {"metadata": {"verification": manifest}}

    w = _make_worker()
    problems = w._execution_submission_problems(task_dir, evidence)

    assert any("operator-authored" in problem for problem in problems)
