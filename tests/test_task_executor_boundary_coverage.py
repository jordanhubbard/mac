"""Executor audit redaction and auto-decompose input rejection."""

from __future__ import annotations

import json

from mac import task_executor as te


def test_audit_safe_argv_redacts_all_sensitive_shapes() -> None:
    long_arg = "x" * 513
    safe = te.audit_safe_argv(
        [
            "tool",
            "--token",
            "secret-one",
            "--api-key",
            "secret-two",
            "Authorization: Bearer secret-three",
            "token=secret-four",
            "API_KEY=secret-five",
            "apikey=secret-six",
            "password=secret-seven",
            "secret=secret-eight",
            long_arg,
            7,
        ]
    )
    assert safe[0] == "tool"
    assert safe[1] == "--token"
    assert safe[2].startswith("<redacted:sha256:")
    assert all("secret-" not in item for item in safe)
    assert safe[-2].startswith("<truncated:sha256:")
    assert safe[-1] == "7"


def test_auto_decompose_rejects_malformed_evidence(tmp_path) -> None:
    evidence = tmp_path / "mac-evidence.json"
    evidence.write_text("not-json")
    assert te.maybe_auto_decompose(tmp_path, {"id": "task"}) is False
    evidence.write_text("[]")
    assert te.maybe_auto_decompose(tmp_path, {"id": "task"}) is False
    evidence.write_text(json.dumps({"plan_steps": [{"no": "title"}]}))
    assert te.maybe_auto_decompose(tmp_path, {"id": "task"}) is False
    evidence.write_text(json.dumps({"plan_steps": [{"title": "child"}]}))
    assert te.maybe_auto_decompose(tmp_path, {"id": ""}) is False
    assert (
        te.maybe_auto_decompose(tmp_path, {"id": "task", "metadata": {"no_decompose": True}})
        is False
    )
