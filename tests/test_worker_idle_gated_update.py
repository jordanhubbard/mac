"""Idle-gated worker redeploys + poisoned-checkout rollback.

The compromise: a repo update that arrives while the agent is mid-task is
DEFERRED (stashed to disk), and applied on a later iteration only when the
agent is idle — always BEFORE the next claim, so no task ever starts on a
stale pin while an update is pending. And an update whose tree fails the
import self-test is rolled back to the prior SHA instead of being adopted
(the June poisoned-checkout incident: executors spawned from a broken tree
died before any telemetry, leaving traceless lease expiries).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mac import worker


class _Client:
    def __init__(self, agent: dict | None = None) -> None:
        self.agent = agent if agent is not None else {}
        self.posts: list[tuple[str, dict]] = []

    def get(self, path: str):
        if path.startswith("/agents/"):
            return self.agent
        return []

    def post(self, path: str, payload: dict):
        self.posts.append((path, payload))
        return {}


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _instance(tmp_path: Path, client=None) -> worker.MacWorker:
    return worker.MacWorker(
        client or _Client(),
        "agent",
        tmp_path / "workspace",
        lambda _task, _path: worker.WorkerExecution(0, "ok"),
        self_update_repo=tmp_path,
    )


OLD = "a" * 40
NEW = "b" * 40


def _git_ok_sequence(monkeypatch, *, calls=None):
    """inside-worktree, clean, before-sha, pull ok, after-sha (updated)."""
    results = iter([
        _cp(stdout="true"), _cp(), _cp(stdout=OLD), _cp(stdout="ok"), _cp(stdout=NEW),
    ])
    log = calls if calls is not None else []

    def run(_repo, args, **_kwargs):
        log.append(args)
        return next(results)

    monkeypatch.setattr(worker, "_run_git", run)
    return log


# ── busy deferral ────────────────────────────────────────────────────────────


def test_update_defers_and_stashes_while_agent_is_mid_task(tmp_path, monkeypatch):
    instance = _instance(tmp_path, _Client({"current_task_id": "task_busy"}))
    git_calls = []
    monkeypatch.setattr(
        worker, "_run_git", lambda *_a, **_k: git_calls.append(1) or _cp()
    )
    result = instance._execute_repo_update({"branch": "main"}, "s1")
    assert result["status"] == "deferred"
    assert result["active_task_id"] == "task_busy"
    assert git_calls == []  # the pinned checkout was not touched at all
    pending = json.loads(instance.pending_repo_update_path.read_text())
    assert pending["request"]["branch"] == "main"
    assert pending["stream_id"] == "s1"


def test_force_bypasses_the_idle_gate(tmp_path, monkeypatch):
    instance = _instance(tmp_path, _Client({"current_task_id": "task_busy"}))
    _git_ok_sequence(monkeypatch)
    monkeypatch.setattr(instance, "_repo_update_self_test", lambda _repo: {"ok": True})
    result = instance._execute_repo_update({"force": True}, "s1")
    assert result["status"] == "updated"


# ── pending application before the next claim ───────────────────────────────


def test_pending_update_applies_once_idle_and_clears_stash(tmp_path, monkeypatch):
    client = _Client({"current_task_id": "task_busy"})
    instance = _instance(tmp_path, client)
    monkeypatch.setattr(worker, "_run_git", lambda *_a, **_k: _cp())
    instance._execute_repo_update({}, "s1")
    assert instance.pending_repo_update_path.exists()

    # Still busy: stash retained, nothing applied.
    assert instance.apply_pending_repo_update_if_idle() is None
    assert instance.pending_repo_update_path.exists()

    # Idle now: the stash applies (as force) and clears.
    client.agent = {"current_task_id": None}
    _git_ok_sequence(monkeypatch)
    monkeypatch.setattr(instance, "_repo_update_self_test", lambda _repo: {"ok": True})
    monkeypatch.setattr(instance, "_observe_log", lambda *_a, **_k: None)
    result = instance.apply_pending_repo_update_if_idle()
    assert result is not None and result["status"] == "updated"
    assert result["restart_requested"] is True
    assert not instance.pending_repo_update_path.exists()


def test_run_once_applies_pending_update_before_claiming(tmp_path, monkeypatch):
    instance = _instance(tmp_path, _Client({"current_task_id": None}))
    instance._stash_pending_repo_update({}, "s1")
    monkeypatch.setattr(instance, "_process_agentbus_control", lambda: None)
    monkeypatch.setattr(instance, "_poll_debug_terminal_sessions", lambda: None)
    monkeypatch.setattr(instance, "_heartbeat", lambda: None)
    monkeypatch.setattr(instance, "_maybe_sync_service_claims", lambda: None)
    monkeypatch.setattr(instance, "_maintain_openclaw_gateway_leases", lambda: None)
    monkeypatch.setattr(instance, "_process_human_delivery_outbox", lambda: None)
    monkeypatch.setattr(instance, "_process_review_nudges", lambda: None)
    monkeypatch.setattr(instance, "_observe_log", lambda *_a, **_k: None)

    def must_not_claim():
        raise AssertionError("claimed before the pending update was applied")

    monkeypatch.setattr(instance, "_claim_next_for_agent", must_not_claim)
    _git_ok_sequence(monkeypatch)
    monkeypatch.setattr(instance, "_repo_update_self_test", lambda _repo: {"ok": True})

    result = instance.run_once()
    assert result.status == "self_update_restart"


# ── poisoned-checkout rollback ───────────────────────────────────────────────


def test_failed_self_test_rolls_back_to_prior_sha(tmp_path, monkeypatch):
    instance = _instance(tmp_path, _Client({"current_task_id": None}))
    calls = _git_ok_sequence(monkeypatch)

    # _git_ok_sequence already patched _run_git; re-wrap it so the rollback
    # reset succeeds instead of consuming the exhausted iterator.
    inner = worker._run_git

    def wrapper(repo, args, **kwargs):
        if args[:2] == ["reset", "--hard"]:
            calls.append(args)
            return _cp()
        return inner(repo, args, **kwargs)

    monkeypatch.setattr(worker, "_run_git", wrapper)
    monkeypatch.setattr(
        instance,
        "_repo_update_self_test",
        lambda _repo: {"ok": False, "stderr": "ImportError: no module named half_published"},
    )
    result = instance._execute_repo_update({}, "s1")
    assert result["status"] == "rolled_back"
    assert result["rollback_ok"] is True
    assert result["restart_requested"] is False
    assert ["reset", "--hard", OLD] in calls
    assert "ImportError" in result["self_test"]["stderr"]


def test_self_test_runs_real_python_and_can_be_disabled(tmp_path, monkeypatch):
    instance = _instance(tmp_path, _Client({}))
    monkeypatch.setenv("MAC_REPO_UPDATE_SELF_TEST", "0")
    assert instance._repo_update_self_test(tmp_path)["ok"] is True
    monkeypatch.setenv("MAC_REPO_UPDATE_SELF_TEST", "1")
    # Point the self-test at a python that fails fast: the check is the
    # subprocess contract, not this host's mac install.
    monkeypatch.setenv("MAC_REPO_UPDATE_SELF_TEST_PYTHON", "/usr/bin/false")
    verdict = instance._repo_update_self_test(tmp_path)
    assert verdict["ok"] is False
