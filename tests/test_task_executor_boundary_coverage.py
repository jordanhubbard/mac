"""Boundary coverage for executor redaction, HTTP, and process seams."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from mac import executor_scope as scope
from mac import task_executor as te


class _Response:
    def __init__(self, value: object = None, *, raw: bytes | None = None) -> None:
        self._raw = raw if raw is not None else json.dumps(value).encode()

    def read(self) -> bytes:
        return self._raw


def _set_hub_env(monkeypatch) -> None:
    monkeypatch.setenv("MAC_HUB_URL", "http://hub/")
    monkeypatch.setenv("MAC_WORKER_TOKEN", "worker-token")


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


def test_hub_helpers_gate_on_environment(monkeypatch) -> None:
    for name in ("MAC_HUB_URL", "MAC_URL", "MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert te._hub_post("/x", {}) is False
    assert te._hub_post_json("/x", {}) is None
    assert te._hub_get("/x") is None
    assert te._hub_post_child_tasks("", []) is None
    assert te._hub_post_child_tasks("task", []) is None
    assert te._hub_post_child_tasks("task", [{}]) is None


def test_hub_helpers_success_and_request_contract(monkeypatch) -> None:
    _set_hub_env(monkeypatch)
    requests = []

    def open_ok(request, timeout=None):
        requests.append((request, timeout))
        return _Response({"ok": True})

    monkeypatch.setattr("urllib.request.urlopen", open_ok)
    assert te._hub_post("/post", {"b": 2}, timeout=3) is True
    assert te._hub_post_json("/json", {"a": 1}) == {"ok": True}
    assert te._hub_get("/get") == {"ok": True}
    assert te._hub_post_child_tasks("task/quoted", [{"title": "child"}]) == {"ok": True}
    assert [request.get_method() for request, _ in requests] == ["POST", "POST", "GET", "POST"]
    assert all(request.headers["Authorization"] == "Bearer worker-token" for request, _ in requests)
    assert requests[0][1] == 3
    assert requests[-1][1] == 10.0


@pytest.mark.parametrize("helper", ["post", "post_json", "get", "children"])
def test_hub_helpers_swallow_transport_and_parse_errors(monkeypatch, helper: str) -> None:
    _set_hub_env(monkeypatch)

    def fail(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    if helper == "post":
        assert te._hub_post("/x", {}) is False
    elif helper == "post_json":
        assert te._hub_post_json("/x", {}) is None
    elif helper == "get":
        assert te._hub_get("/x") is None
    else:
        assert te._hub_post_child_tasks("task", [{"title": "child"}]) is None

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: _Response(raw=b"not-json"))
    if helper == "post_json":
        assert te._hub_post_json("/x", {}) is None
    elif helper == "get":
        assert te._hub_get("/x") is None
    elif helper == "children":
        assert te._hub_post_child_tasks("task", [{"title": "child"}]) is None


@pytest.mark.parametrize("returncode", [0, 9])
def test_run_audited_command_records_completion(monkeypatch, tmp_path, returncode: int) -> None:
    audits = []
    monkeypatch.setattr(te, "local_agent_id", lambda: "agent-a")
    monkeypatch.setattr(te, "post_command_audit", lambda agent, payload: audits.append((agent, payload)))
    monkeypatch.setattr(
        te,
        "_run_captured",
        lambda *_a, **_k: subprocess.CompletedProcess(["cmd"], returncode, "out", "err"),
    )
    metadata = {"purpose": "test", "timeout": 12}
    result = te.run_audited_command(["cmd", "--token", "secret"], tmp_path, "task", metadata)
    assert result.returncode == returncode
    assert [payload["phase"] for _, payload in audits] == [
        "started",
        "completed" if returncode == 0 else "failed",
    ]
    assert audits[0][1]["argv"][2].startswith("<redacted:")
    assert audits[-1][1]["stdout_bytes"] == 3
    assert "timeout" not in metadata


def test_run_audited_command_timeout_decodes_bytes(monkeypatch, tmp_path) -> None:
    audits = []
    monkeypatch.setattr(te, "local_agent_id", lambda: "agent-a")
    monkeypatch.setattr(te, "post_command_audit", lambda _agent, payload: audits.append(payload))

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("cmd", 2, output=b"partial", stderr=b"stuck")

    monkeypatch.setattr(te, "_run_captured", timeout)
    result = te.run_audited_command(["cmd"], tmp_path, None, {"timeout": 2})
    assert result.returncode == 124
    assert result.stdout == "partial"
    assert "stuck" in result.stderr
    assert audits[-1]["phase"] == "timeout"
    assert audits[-1]["metadata"]["timeout_seconds"] == 2


def test_run_captured_kills_process_group_on_timeout(tmp_path) -> None:
    """The timeout path must SIGKILL the whole process tree promptly.

    A grandchild inheriting the stdout pipe used to keep the post-kill
    output drain blocked until it exited on its own (the 900s rc-124 hangs
    on unsandboxed fleet hosts), and the grandchild itself leaked.
    """
    import os
    import time

    script = "echo started; sleep 30 & echo $!; wait"
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        te._run_captured(["sh", "-c", script], tmp_path, 1.0)
    elapsed = time.monotonic() - start
    # Old behavior blocked ~30s draining the grandchild's inherited pipe.
    assert elapsed < 10
    lines = (exc_info.value.output or "").split()
    assert lines[0] == "started"
    grandchild_pid = int(lines[1])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("grandchild survived the process-group kill")


def test_run_audited_command_records_oserror(monkeypatch, tmp_path) -> None:
    audits = []
    monkeypatch.setattr(te, "local_agent_id", lambda: "agent-a")
    monkeypatch.setattr(te, "post_command_audit", lambda _agent, payload: audits.append(payload))
    monkeypatch.setattr(
        te,
        "_run_captured",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("missing command")),
    )
    with pytest.raises(OSError, match="missing command"):
        te.run_audited_command(["missing"], tmp_path, "task", {})
    assert audits[-1]["phase"] == "error"
    assert audits[-1]["metadata"]["error"] == "missing command"


def test_post_command_audit_skips_empty_agent(monkeypatch) -> None:
    monkeypatch.setattr(te, "_hub_post", lambda *_a, **_k: pytest.fail("must not post"))
    assert te.post_command_audit("", {}) is None


def test_auto_decompose_rejects_malformed_and_normalizes_children(monkeypatch, tmp_path) -> None:
    evidence = tmp_path / "mac-evidence.json"
    evidence.write_text("not-json")
    assert te.maybe_auto_decompose(tmp_path, {"id": "task"}) is False
    evidence.write_text("[]")
    assert te.maybe_auto_decompose(tmp_path, {"id": "task"}) is False
    evidence.write_text(json.dumps({"plan_steps": [{"no": "title"}]}))
    assert te.maybe_auto_decompose(tmp_path, {"id": "task"}) is False
    evidence.write_text(json.dumps({"plan_steps": [{"title": "child"}]}))
    assert te.maybe_auto_decompose(tmp_path, {"id": ""}) is False
    assert te.maybe_auto_decompose(
        tmp_path, {"id": "task", "metadata": {"no_decompose": True}}
    ) is False

    captured = {}
    evidence.write_text(
        json.dumps(
            {
                "plan_steps": [
                    "invalid",
                    {
                        "title": " Child ",
                        "description": " Do it ",
                        "dependencies": ["one"],
                        "required_capabilities": ["python"],
                    },
                ]
            }
        )
    )
    monkeypatch.setattr(
        scope,
        "_hub_post_child_tasks",
        lambda task_id, children: captured.update(task_id=task_id, children=children) or {},
    )
    assert te.maybe_auto_decompose(tmp_path, {"id": "task", "metadata": {}}) is True
    assert captured["children"] == [
        {
            "title": "Child",
            "description": "Do it",
            "dependencies": ["one"],
            "required_capabilities": ["python"],
        }
    ]


def test_learning_format_and_budget_edges() -> None:
    assert te._format_learning_content("not json") == "not json"
    assert te._format_learning_content("[]") == "[]"
    assert te._format_learning_content('{"schema":"other"}') == '{"schema":"other"}'
    failure = te._format_learning_content(
        json.dumps(
            {
                "schema": "mac.deployment_learning.v1",
                "outcome": "failure",
                "task_id": "task-a",
                "evidence_type": "repo_change",
                "error_signature": "tests failed",
            }
        )
    )
    assert failure == "[failure] task-a (repo_change) — failed: tests failed"
    lessons: list[str] = []
    assert te._append_lesson_with_budget(lessons, "  ") is True
    assert te._append_lesson_with_budget(lessons, "x" * 2000) is False
    assert len(lessons[0]) == te._LESSON_PROMPT_BUDGET
    assert te._append_lesson_with_budget(lessons, "more") is False


def test_classify_outcome_handles_invalid_manifest_and_list_tests(tmp_path) -> None:
    path = tmp_path / "mac-evidence.json"
    path.write_text("not-json")
    assert te.classify_outcome(tmp_path, {"id": "task"}, 0)["outcome"] == "failure"
    path.write_text(
        json.dumps(
            {
                "evidence_type": "operator_result",
                "tests": ["invalid", {"status": "pass"}],
                "checks": ["invalid", {"status": "pass"}],
            }
        )
    )
    result = te.classify_outcome(tmp_path, {"id": "task"}, 0)
    assert result["outcome"] == "success"
    assert result["signals"]["tests"] == "pass"
