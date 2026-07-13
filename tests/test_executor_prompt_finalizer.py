"""Isolation checks for the extracted prompt and finalizer boundaries."""

from __future__ import annotations

import json

from mac import executor_finalizer as finalizer
from mac import executor_prompt as prompt
from mac import task_executor as te


def test_task_executor_reexports_prompt_and_finalizer_surface() -> None:
    for module, names in (
        (
            prompt,
            (
                "classify_outcome",
                "build_task_prompt",
                "build_review_prompt",
                "task_evidence_type",
            ),
        ),
        (
            finalizer,
            (
                "run_deterministic_git_finalizer",
                "run_deterministic_review_verdict",
                "write_fallback_evidence_manifest",
            ),
        ),
    ):
        for name in names:
            assert getattr(te, name) is getattr(module, name)


def test_classify_outcome_keeps_non_repo_signals_not_applicable(tmp_path) -> None:
    (tmp_path / "mac-evidence.json").write_text(
        json.dumps(
            {
                "evidence_type": "operator_result",
                "status": "complete",
                "summary": "done",
            }
        ),
        encoding="utf-8",
    )
    outcome = prompt.classify_outcome(tmp_path, {"id": "task_test"}, 0)
    assert outcome["outcome"] == "success"
    assert outcome["signals"]["pushed"] is None


def test_finalizer_status_split_distinguishes_new_files() -> None:
    tracked, untracked, staged = finalizer._split_porcelain_status(
        " M tracked.py\n?? new.py\nA  staged.py\n"
    )
    assert tracked == [" M tracked.py", "A  staged.py"]
    assert untracked == ["new.py"]
    assert staged == ["staged.py"]


def test_fallback_manifest_remains_unverified_operator_result(tmp_path) -> None:
    result = type("Result", (), {"stdout": "done", "stderr": "", "returncode": 0})()
    finalizer.write_fallback_evidence_manifest(
        tmp_path,
        {"id": "task_test", "title": "Test", "project": "mac"},
        result,
        None,
    )
    manifest = json.loads((tmp_path / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["evidence_type"] == "operator_result"
    assert manifest["status"] == "complete"
