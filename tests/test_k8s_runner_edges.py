"""Alternate control-flow coverage for the Kubernetes job dispatcher."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mac.k8s import runner


class _Mac:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def post(self, path, body):
        self.calls.append(("post", path, body))
        value = self.responses.pop(0) if self.responses else {}
        if isinstance(value, BaseException):
            raise value
        return value

    def get(self, path):
        self.calls.append(("get", path, None))
        value = self.responses.pop(0) if self.responses else {}
        if isinstance(value, BaseException):
            raise value
        return value


class _Jobs:
    def __init__(self, result=None):
        self.result = result if result is not None else {"metadata": {"uid": "uid"}}
        self.created = []

    def create(self, namespace, manifest):
        self.created.append((namespace, manifest))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _cfg(**extra):
    values = {"mac_url": "http://mac", "agent_id": "dispatcher", "poll_interval_seconds": 0}
    values.update(extra)
    return runner.RunnerConfig(**values)


def _assignment(role=None):
    metadata = {"required_role": role} if role else {}
    return {
        "task": {"id": "task", "metadata": metadata, "required_capabilities": []},
        "lease": {"id": "lease"},
    }


def test_optional_int_sanitize_deadline_and_terminal_edges(monkeypatch) -> None:
    monkeypatch.delenv("OPTIONAL", raising=False)
    assert runner._optional_int_env("OPTIONAL") is None
    monkeypatch.setenv("OPTIONAL", "bad")
    assert runner._optional_int_env("OPTIONAL") is None
    monkeypatch.setenv("OPTIONAL", "0")
    assert runner._optional_int_env("OPTIONAL") is None
    monkeypatch.setenv("OPTIONAL", "12")
    assert runner._optional_int_env("OPTIONAL") == 12
    assert runner._sanitize_dns_label("---") == "mac-task"
    assert runner._resolve_active_deadline(
        {"metadata": {"k8s": {"active_deadline_seconds": "bad"}}}, _cfg(active_deadline_seconds=99)
    ) == 99
    assert runner._resolve_active_deadline(
        {"metadata": {"k8s": {"active_deadline_seconds": 1}}}, _cfg()
    ) == 60
    assert runner._job_is_terminal(None) is False
    assert runner._job_is_terminal({"status": {"succeeded": "bad", "failed": "bad"}}) is False


def test_agent_token_secret_map_is_reference_only_and_fail_closed() -> None:
    assert runner._agent_token_secret_map("") == {}
    assert runner._agent_token_secret_map(
        '{"agent_a":"mac-worker-agent-a","agent_b":"mac-worker-agent-b"}'
    ) == {
        "agent_a": "mac-worker-agent-a",
        "agent_b": "mac-worker-agent-b",
    }
    with pytest.raises(ValueError, match="JSON object"):
        runner._agent_token_secret_map("[]")
    with pytest.raises(ValueError, match="non-empty"):
        runner._agent_token_secret_map('{"agent_a":""}')


def test_dispatcher_capability_probe_shape_and_role_failures() -> None:
    cfg = _cfg(role_agent_ids={"worker": "worker"}, reviewer_agent_ids={"review": "reviewer"})
    assert runner.check_dispatcher_capabilities(cfg, _Mac([RuntimeError("offline")])) == []
    assert runner.check_dispatcher_capabilities(cfg, _Mac([[]])) == []
    mac = _Mac([
        {"capabilities": []},
        RuntimeError("review missing"),
        RuntimeError("worker missing"),
    ])
    assert runner.check_dispatcher_capabilities(cfg, mac) == []
    mac = _Mac([
        {"capabilities": []},
        {"capabilities": ["review", "shared"]},
        [],
    ])
    assert runner.check_dispatcher_capabilities(cfg, mac) == []


def test_claim_next_failures_and_missing_lease() -> None:
    assert runner.claim_and_launch_one(_Mac([RuntimeError("offline")]), _Jobs(), _cfg()) is None
    assert runner.claim_and_launch_one(_Mac([{}]), _Jobs(), _cfg()) is None
    result = runner.claim_and_launch_one(
        _Mac([{"task": {"id": "task"}, "lease": {}}]), _Jobs(), _cfg()
    )
    assert result is None


def test_claim_delegation_failure_reopens_best_effort() -> None:
    cfg = _cfg(role_agent_ids={"coder": "coder"})
    mac = _Mac([_assignment("coder"), RuntimeError("delegate failed"), RuntimeError("reopen failed")])
    result = runner.claim_and_launch_one(mac, _Jobs(), cfg)
    assert result["status"] == "lease_delegation_failed"
    assert result["to_agent_id"] == "coder"


def test_claim_job_create_failure_and_renewal_start_failure(monkeypatch) -> None:
    mac = _Mac([_assignment(), RuntimeError("reopen failed")])
    result = runner.claim_and_launch_one(mac, _Jobs(RuntimeError("create failed")), _cfg())
    assert result["status"] == "k8s_create_failed"

    monkeypatch.setattr(
        runner, "_start_lease_renewal_thread",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("thread failed")),
    )
    result = runner.claim_and_launch_one(_Mac([_assignment()]), _Jobs(), _cfg())
    assert result["status"] == "launched"
    assert result["job_uid"] == "uid"


def _nudge(**extra):
    payload = {
        "reason": "produce_review_verdict",
        "review_id": "review",
        "task_id": "task",
        "executor_evidence_id": "evidence",
    }
    payload.update(extra)
    return {"message_type": "nudge", "payload": payload}


def test_review_claim_filters_delivery_and_malformed_messages() -> None:
    cfg = _cfg(reviewer_agent_ids={"reviewer": "reviewer-agent"})
    assert runner.claim_and_launch_review_one(_Mac([RuntimeError("offline")]), _Jobs(), cfg) is None
    assert runner.claim_and_launch_review_one(_Mac([{}]), _Jobs(), cfg) is None
    messages = [
        "bad",
        {"message_type": "other"},
        {"message_type": "nudge", "payload": {"reason": "other"}},
        _nudge(review_id=""),
    ]
    assert runner.claim_and_launch_review_one(_Mac([messages]), _Jobs(), cfg) is None


def test_review_claim_and_create_failures_then_success() -> None:
    cfg = _cfg(reviewer_agent_ids={"reviewer": "reviewer-agent"})
    assert runner.claim_and_launch_review_one(
        _Mac([[_nudge()], RuntimeError("claim failed")]), _Jobs(), cfg
    ) is None
    assert runner.claim_and_launch_review_one(
        _Mac([[_nudge()], {"status": "skipped", "reason": "busy"}]), _Jobs(), cfg
    ) is None
    assert runner.claim_and_launch_review_one(
        _Mac([[_nudge()], {"status": "claimed"}]), _Jobs(RuntimeError("create failed")), cfg
    ) is None
    result = runner.claim_and_launch_review_one(
        _Mac([[_nudge()], {"status": "claimed"}]), _Jobs(), cfg
    )
    assert result["status"] == "launched"
    assert result["role"] == "reviewer"


def test_runner_and_review_loops_count_launches_and_sleep(monkeypatch) -> None:
    outcomes = iter([None, {"status": "launched", "task_id": "t", "lease_id": "l", "job_name": "j"}])
    monkeypatch.setattr(runner, "claim_and_launch_one", lambda *_a: next(outcomes))
    sleeps = []
    assert runner.runner_loop(_Mac(), _Jobs(), _cfg(), iterations=2, sleep=sleeps.append) == 1
    assert sleeps == [0]

    outcomes = iter([{"status": "failed"}, {"status": "launched"}])
    monkeypatch.setattr(runner, "claim_and_launch_review_one", lambda *_a: next(outcomes))
    sleeps = []
    assert runner.review_loop(_Mac(), _Jobs(), _cfg(), iterations=2, sleep=sleeps.append) == 1
    assert sleeps == [0]
