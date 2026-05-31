"""Tests for the extracted autonomous task executor (loop-01).

Covers the logic that used to be an untestable bash heredoc: prompt building,
the fail-closed fallback, deterministic outcome classification, the telemetry
path, and the memory feed (recall in / record out). The agent runner and the
hub HTTP seam are injected, so nothing here spawns Hermes or hits a network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mac import task_executor as te


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Pure builders
# ---------------------------------------------------------------------------


def test_task_evidence_type_defaults_and_honors_contract():
    assert te.task_evidence_type({}) == "operator_result"
    assert te.task_evidence_type({"metadata": {"execution_contract": {"evidence_type": "repo_change"}}}) == "repo_change"
    assert te.task_evidence_type({"metadata": {"execution_contract": {"evidence_type": "bogus"}}}) == "operator_result"


def test_build_task_prompt_injects_recalled_lessons():
    task = {"id": "t1", "title": "Do a thing", "project": "demo"}
    base = te.build_task_prompt(task, Path("/tmp/task.json"), lessons=[])
    assert "Lessons from prior runs" not in base
    with_lessons = te.build_task_prompt(task, Path("/tmp/task.json"), lessons=["push before reporting", "run the contract tests"])
    assert "Lessons from prior runs" in with_lessons
    assert "push before reporting" in with_lessons
    # The task file pointer is always last.
    assert with_lessons.strip().endswith("/tmp/task.json")


def test_build_telemetry_record_shape():
    rec = te.build_telemetry_record("started", task_id="t1", level="info", detail={"kind": "task"})
    assert rec["name"] == "executor.started"
    assert rec["layer"] == "executor"
    assert rec["subject_type"] == "task" and rec["subject_id"] == "t1"
    assert rec["detail"]["schema"] == "mac.executor_telemetry.v1"
    assert rec["detail"]["kind"] == "task"


def test_build_learning_record_shape():
    task = {"id": "t1", "title": "Ship X", "project": "demo", "metadata": {"origin": {"repository_name": "demo-repo"}}}
    outcome = {"evidence_type": "repo_change", "outcome": "success", "signals": {"pushed": True}, "error_signature": ""}
    rec = te.build_learning_record(task, outcome)
    assert rec["subject_type"] == "project" and rec["subject_id"] == "demo"
    assert rec["record_type"] == "deployment_learning:demo"
    assert rec["created_by"] == "mac-hermes-task-executor"
    content = json.loads(rec["content"])
    assert content["schema"] == "mac.deployment_learning.v1"
    assert content["repository"] == "demo-repo"
    assert content["outcome"] == "success"


# ---------------------------------------------------------------------------
# Fail-closed fallback (the loop-01 invariant must survive the extraction)
# ---------------------------------------------------------------------------


def test_fallback_writes_unverified_operator_result_no_synthetic_check(tmp_path):
    task = {"id": "t1", "title": "x", "project": "demo"}
    te.write_fallback_evidence_manifest(tmp_path, task, _FakeResult(0, stdout="Mapped the milestones."), None)
    manifest = json.loads((tmp_path / "mac-evidence.json").read_text())
    assert manifest["evidence_type"] == "operator_result"
    assert manifest["summary"] == "Mapped the milestones."
    assert "checks" not in manifest  # no fabricated passing check


def test_fallback_skips_on_failure_review_and_existing(tmp_path):
    task = {"id": "t1"}
    # non-zero exit → no fabrication
    te.write_fallback_evidence_manifest(tmp_path, task, _FakeResult(1, stdout="boom"), None)
    assert not (tmp_path / "mac-evidence.json").exists()
    # review context → finalizer owns the manifest
    te.write_fallback_evidence_manifest(tmp_path, task, _FakeResult(0, stdout="x"), {"review_id": "r"})
    assert not (tmp_path / "mac-evidence.json").exists()
    # existing manifest (finalizer already wrote) → don't overwrite
    (tmp_path / "mac-evidence.json").write_text('{"kept": true}')
    te.write_fallback_evidence_manifest(tmp_path, task, _FakeResult(0, stdout="x"), None)
    assert json.loads((tmp_path / "mac-evidence.json").read_text()) == {"kept": True}


# ---------------------------------------------------------------------------
# Outcome classification (drives the memory feed)
# ---------------------------------------------------------------------------


def test_classify_outcome_success_and_failure(tmp_path):
    task = {"id": "t1", "project": "demo"}
    # success: pushed repo_change with passing tests
    (tmp_path / "mac-evidence.json").write_text(json.dumps({
        "evidence_type": "repo_change",
        "repo": {"pushed": True, "files_changed": ["a.py"]},
        "tests": {"returncode": 0, "status": "pass"},
        "checks": [{"name": "git_finalizer", "returncode": 0, "status": "pass"}],
    }))
    ok = te.classify_outcome(tmp_path, task, 0)
    assert ok["outcome"] == "success"
    assert ok["signals"]["pushed"] is True and ok["signals"]["tests"] == "pass"

    # failure: tests failed
    (tmp_path / "mac-evidence.json").write_text(json.dumps({
        "evidence_type": "repo_change",
        "repo": {"pushed": True, "files_changed": ["a.py"]},
        "tests": {"returncode": 1, "status": "fail"},
        "checks": [{"name": "git_finalizer", "returncode": 1, "status": "fail"}],
        "summary": "tests broke",
    }))
    bad = te.classify_outcome(tmp_path, task, 0)
    assert bad["outcome"] == "failure"
    assert bad["error_signature"]


def test_classify_outcome_failure_when_no_evidence(tmp_path):
    out = te.classify_outcome(tmp_path, {"id": "t1"}, 0)
    assert out["outcome"] == "failure"


def test_agent_timeout_default_and_override(monkeypatch):
    monkeypatch.delenv("MAC_EXECUTOR_AGENT_TIMEOUT", raising=False)
    assert te._agent_timeout() == 900.0
    monkeypatch.setenv("MAC_EXECUTOR_AGENT_TIMEOUT", "120")
    assert te._agent_timeout() == 120.0
    monkeypatch.setenv("MAC_EXECUTOR_AGENT_TIMEOUT", "0")  # disable the bound
    assert te._agent_timeout() is None


def test_manifest_is_complete(tmp_path):
    assert te._manifest_is_complete(tmp_path) is False
    (tmp_path / "mac-evidence.json").write_text('{"status":"complete","evidence_type":"operator_result"}')
    assert te._manifest_is_complete(tmp_path) is True
    (tmp_path / "mac-evidence.json").write_text('{"status":"running"}')  # partial
    assert te._manifest_is_complete(tmp_path) is False


def test_main_salvages_evidence_when_agent_run_times_out(tmp_path, monkeypatch):
    # loop-01 resilience: the agent wrote a valid deliverable, then a trailing
    # turn hung and the run was bounded (rc=124). The deliverable must NOT be
    # discarded — main() salvages it and reports success.
    task = {"id": "t1", "title": "Plan X", "project": "demo", "metadata": {"publication_target": "test://x"}}
    task_file = tmp_path / "task.json"; task_file.write_text(json.dumps({"task": task}))
    ws = tmp_path / "ws"; ws.mkdir()
    monkeypatch.setenv("MAC_TASK_FILE", str(task_file)); monkeypatch.setenv("MAC_TASK_WORKSPACE", str(ws))
    posts = []
    monkeypatch.setattr(te, "_hub_post", lambda path, payload, **kw: posts.append((path, payload)) or True)
    monkeypatch.setattr(te, "_hub_get", lambda path, **kw: [])

    def timed_out_runner(argv, cwd, task_id, metadata):
        # agent produced a real deliverable before the trailing turn hung
        (ws / "mac-evidence.json").write_text(json.dumps({
            "schema": "mac.worker_evidence.v1", "status": "complete",
            "evidence_type": "operator_result",
            "summary": "Produced a substantive plan with several distinct points.",
        }))
        return _FakeResult(124, stdout="...", stderr="agent run timed out")

    rc = te.main(runner=timed_out_runner)
    assert rc == 0, "valid deliverable should be salvaged despite the timeout"
    names = {p[1]["name"] for p in posts if p[0] == "/observability/logs"}
    assert "executor.evidence_salvaged" in names
    # outcome recorded as success (memory feed)
    mem = [p for p in posts if p[0] == "/memory"]
    assert mem and json.loads(mem[0][1]["content"])["outcome"] == "success"


def test_main_fails_when_timeout_and_no_evidence(tmp_path, monkeypatch):
    task = {"id": "t1", "title": "Plan X", "project": "demo", "metadata": {"publication_target": "test://x"}}
    task_file = tmp_path / "task.json"; task_file.write_text(json.dumps({"task": task}))
    ws = tmp_path / "ws"; ws.mkdir()
    monkeypatch.setenv("MAC_TASK_FILE", str(task_file)); monkeypatch.setenv("MAC_TASK_WORKSPACE", str(ws))
    monkeypatch.setattr(te, "_hub_post", lambda *a, **k: True)
    monkeypatch.setattr(te, "_hub_get", lambda *a, **k: [])
    # timeout with NO deliverable written → honest failure, not salvaged
    rc = te.main(runner=lambda *a: _FakeResult(124, stderr="agent run timed out"))
    assert rc == 124


# ---------------------------------------------------------------------------
# Hub seam: telemetry + memory are best-effort and gated
# ---------------------------------------------------------------------------


def test_hub_post_noop_without_env(monkeypatch):
    monkeypatch.delenv("MAC_HUB_URL", raising=False)
    monkeypatch.delenv("MAC_URL", raising=False)
    monkeypatch.delenv("MAC_TOKEN", raising=False)
    monkeypatch.delenv("MAC_WORKER_TOKEN", raising=False)
    monkeypatch.delenv("MAC_API_TOKEN", raising=False)
    assert te.emit_telemetry("started", task_id="t1") is False
    assert te.record_deployment_learning({"id": "t1", "project": "demo"}, {"outcome": "success"}) is False


def test_recall_deployment_lessons_via_injected_get(monkeypatch):
    captured = {}

    def fake_get(path, *, timeout=5.0):
        captured["path"] = path
        return [{"content": "always push before reporting"}, {"summary": "run contract tests"}, {"nope": 1}]

    monkeypatch.setattr(te, "_hub_get", fake_get)
    lessons = te.recall_deployment_lessons({"title": "Ship X", "project": "demo"})
    assert lessons == ["always push before reporting", "run contract tests"]
    assert "/v1/memory/recall?" in captured["path"]
    assert "project=demo" in captured["path"]


def test_recall_falls_back_to_direct_memory_records(monkeypatch):
    # Vector recall empty (no embeddings yet) → fall back to the project's
    # deployment_learning records so the very next task still gets hindsight.
    learning = json.dumps({
        "schema": "mac.deployment_learning.v1",
        "task_title": "Earlier task",
        "evidence_type": "repo_change",
        "outcome": "failure",
        "error_signature": "check:git_finalizer rc=1",
    })

    def fake_get(path, *, timeout=5.0):
        if path.startswith("/v1/memory/recall"):
            return []  # vector tier not populated yet
        return [
            {"record_type": "deployment_learning:demo", "content": learning, "created_at": "2026-05-31T00:00:00Z"},
            {"record_type": "other", "content": "ignored", "created_at": "2026-05-31T01:00:00Z"},
        ]

    monkeypatch.setattr(te, "_hub_get", fake_get)
    lessons = te.recall_deployment_lessons({"title": "Next task", "project": "demo"})
    assert lessons == ["[failure] Earlier task (repo_change) — failed: check:git_finalizer rc=1"]


# ---------------------------------------------------------------------------
# git finalizer against a real temp repo
# ---------------------------------------------------------------------------


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def test_git_finalizer_emits_repo_change_from_real_state(tmp_path, monkeypatch):
    # origin (bare) + worktree clone
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("hello\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "origin", "main")
    # a feature branch with an uncommitted edit the finalizer should commit+push
    _git(work, "checkout", "-b", "task/x")
    (work / "feature.py").write_text("print('x')\n")

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    task = {
        "id": "t1",
        "metadata": {
            "publication_target": "git://main",
            "origin": {"repository_contract": {"test": {"command": "true"}}},
        },
    }
    te.run_deterministic_git_finalizer(ws, task)
    manifest = json.loads((ws / "mac-evidence.json").read_text())
    assert manifest["evidence_type"] == "repo_change"
    assert manifest["repo"]["pushed"] is True
    assert "feature.py" in manifest["repo"]["files_changed"]
    assert manifest["checks"][0]["status"] == "pass"  # pushed + `true` test passed


# ---------------------------------------------------------------------------
# main() end-to-end with injected runner + hub
# ---------------------------------------------------------------------------


def test_main_runs_records_telemetry_and_memory(tmp_path, monkeypatch):
    task = {"id": "t1", "title": "Plan the rollout", "project": "demo", "metadata": {"publication_target": "test://x"}}
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps({"task": task}))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("MAC_TASK_FILE", str(task_file))
    monkeypatch.setenv("MAC_TASK_WORKSPACE", str(ws))

    posts = []
    monkeypatch.setattr(te, "_hub_post", lambda path, payload, **kw: posts.append((path, payload)) or True)
    monkeypatch.setattr(te, "_hub_get", lambda path, **kw: [{"content": "push before reporting"}])
    # Inject a fake runner: assert it received the recalled lesson, return chatty output.
    seen = {}

    def fake_runner(argv, cwd, task_id, metadata):
        seen["prompt"] = argv[argv.index("--query") + 1]
        return _FakeResult(0, stdout="Produced the rollout plan and mapped the dependencies.\n")

    rc = te.main(runner=fake_runner)
    assert rc == 0
    # recalled lesson reached the prompt
    assert "push before reporting" in seen["prompt"]
    # fallback wrote an unverified operator_result
    manifest = json.loads((ws / "mac-evidence.json").read_text())
    assert manifest["evidence_type"] == "operator_result"
    # telemetry path fired (started + agent_completed + finalized)
    telemetry = [p for p in posts if p[0] == "/observability/logs"]
    names = {p[1]["name"] for p in telemetry}
    assert {"executor.started", "executor.agent_completed", "executor.finalized"} <= names
    # memory feed recorded a deployment lesson
    memory = [p for p in posts if p[0] == "/memory"]
    assert memory and memory[0][1]["record_type"] == "deployment_learning:demo"
