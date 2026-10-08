"""The fleet's coding CLIs are an ordered list the hub owns.

The hub's ``MAC_CODING_AGENTS`` reaches workers through every assignment and
every heartbeat. A worker runs the first CLI that works on it and moves down
the list only on a structured route failure; a task's own outcome never moves
it.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from mac import anthropic_passthrough
from mac import coding_agent as ca
from mac import executor_sandbox as ex
from mac.api import create_app
from mac.inference_tokens import InferenceTokenLifecycle
from mac.services import ControlPlane
from mac.test_support import ephemeral_dsn, store_on
from mac.worker import _adopt_hub_coding_policy, _task_coding_policy_env
from mac.worker_credentials import WorkerCredentialLifecycle

_HUB = {"MAC_HUB_URL": "http://hub.example:8789", "MAC_INFERENCE_TOKEN": "t"}


def _which(*names: str):
    return lambda name: "/usr/bin/" + name if name in names else None


# --- the list and where it comes from -----------------------------------------


def test_the_list_parses_in_order_drops_duplicates_and_reports_unknowns():
    assert ca.parse_agent_list(" Claude, opencode,claude ,codex,") == (
        ("claude", "opencode"),
        ("codex",),
    )
    assert ca.parse_agent_list(["opencode"]) == (("opencode",), ())


def test_the_hubs_list_wins_and_says_where_it_came_from():
    env = {
        ca.TASK_AGENTS_ENV: "claude,opencode",
        ca.HUB_AGENTS_ENV: "opencode",
        ca.AGENTS_ENV: "opencode",
        ca.FORCE_ENV: "opencode",
    }
    assert ca.coding_agent_order(env) == ca.AgentOrder(("claude", "opencode"), "hub")
    env.pop(ca.TASK_AGENTS_ENV)
    assert ca.coding_agent_order(env).agents == ("opencode",)
    assert ca.coding_agent_order(env).source == "hub"


def test_a_local_list_is_used_only_without_a_hub_list_and_is_reported():
    order = ca.coding_agent_order({ca.AGENTS_ENV: "claude"})
    assert order.agents == ("claude",) and order.source == "worker-local"
    assert any("local override" in note for note in order.notes)


def test_the_old_switch_is_a_reported_deprecated_alias_and_still_disables():
    order = ca.coding_agent_order({ca.FORCE_ENV: "claude"})
    assert order.agents == ("claude",) and order.source == "deprecated:MAC_CODING_AGENT"
    assert any("deprecated" in note for note in order.notes)
    off = ca.resolve_coding_agent(env={**_HUB, ca.FORCE_ENV: "off"}, which=_which("opencode"))
    assert off.available is False


def test_nothing_configured_means_opencode():
    assert ca.coding_agent_order({}) == ca.AgentOrder(("opencode",), "default")
    order = ca.coding_agent_order({ca.TASK_AGENTS_ENV: "codex"})
    assert order.agents == ("opencode",) and order.source == "default"
    assert any("unknown coding CLIs codex" in note for note in order.notes)


def test_the_hub_policy_carries_the_list_and_only_the_models_it_sets():
    assert ca.hub_coding_policy({}) == {"schema": "mac.coding_policy.v1", "agents": ["opencode"]}
    policy = ca.hub_coding_policy(
        {"MAC_CODING_AGENTS": "opencode,claude", "MAC_CLAUDE_MODEL": "anthropic/claude-opus-5.5"}
    )
    assert policy["agents"] == ["opencode", "claude"]
    assert policy["claude_model"] == "anthropic/claude-opus-5.5"
    assert "judge_model" not in policy
    assert ca.coding_policy_env(policy) == {
        ca.TASK_AGENTS_ENV: "opencode,claude",
        "MAC_CLAUDE_MODEL": "anthropic/claude-opus-5.5",
    }
    # A malformed document never empties the list.
    assert ca.coding_policy_env({"agents": ["codex"]}) == {}
    assert ca.coding_policy_env("opencode") == {}


# --- choosing a CLI ------------------------------------------------------------


def test_the_first_cli_that_works_runs_and_the_skips_are_recorded():
    env = {**_HUB, ca.TASK_AGENTS_ENV: "claude,opencode"}
    choice = ca.resolve_coding_agent(env=env, which=_which("opencode"))
    assert choice.agent == "opencode"
    assert choice.order == ("claude", "opencode") and choice.order_source == "hub"
    assert [(s["agent"], s["failure_class"]) for s in choice.skipped] == [
        ("claude", "agent_binary_missing")
    ]
    assert choice.observable()["skipped"][0]["agent"] == "claude"


def test_each_structured_class_moves_the_list():
    no_token = {"MAC_HUB_URL": "http://hub.example:8789", ca.TASK_AGENTS_ENV: "claude,opencode"}
    choice = ca.resolve_coding_agent(env=no_token, which=_which("claude", "opencode"))
    assert choice.available is False
    assert {s["failure_class"] for s in choice.skipped} == {"inference_token_unavailable"}

    no_hub = {"MAC_INFERENCE_TOKEN": "t", ca.TASK_AGENTS_ENV: "claude"}
    choice = ca.resolve_coding_agent(env=no_hub, which=_which("claude"))
    assert choice.skipped[0]["failure_class"] == "not_configured"

    env = {**_HUB, ca.TASK_AGENTS_ENV: "claude,opencode"}
    choice = ca.resolve_coding_agent(
        env=env,
        which=_which("claude", "opencode"),
        accept=lambda candidate: candidate.agent == "opencode",
    )
    assert choice.agent == "opencode"
    assert choice.skipped[0] == {
        "agent": "claude",
        "failure_class": "preflight_failed",
        "detail": "claude did not pass the in-sandbox preflight",
    }

    choice = ca.resolve_coding_agent(
        env=env, which=_which("claude", "opencode"), exclude=("claude",)
    )
    assert choice.agent == "opencode"
    assert choice.skipped[0]["failure_class"] == "already_failed"


def test_selected_agent_follows_the_run_then_the_list():
    assert ca.selected_agent({ca.TASK_AGENTS_ENV: "claude,opencode"}) == "claude"
    assert (
        ca.selected_agent({ca.TASK_AGENTS_ENV: "claude", ca.ACTIVE_AGENT_ENV: "opencode"})
        == "opencode"
    )


# --- the hub projects it; the worker inherits it ---------------------------------


def _plane() -> ControlPlane:
    cp = ControlPlane(
        store_on(ephemeral_dsn(), initialize=True),
        secret_key="coding-cli-list-test-key-32-bytes-xx",
    )
    machine = cp.register_machine("worker-host", machine_id="machine_worker", labels={})
    cp.register_agent(machine.id, "alpha", ["python"], resources={}, agent_id="agent_alpha")
    return cp


def test_every_assignment_carries_the_hubs_list_not_the_tasks(monkeypatch):
    monkeypatch.setenv("MAC_CODING_AGENTS", "opencode,claude")
    monkeypatch.setenv("MAC_JUDGE_MODEL", "anthropic/claude-opus-5.5")
    cp = _plane()
    task = cp.create_task(
        "t", metadata={"runtime": {"coding_policy": {"agents": ["claude"]}}}
    )
    claimed, lease = cp.claim_task(task.id, "agent_alpha")
    payload = cp._assignment_task_payload(claimed, lease)
    policy = payload["metadata"]["runtime"]["coding_policy"]
    assert policy["agents"] == ["opencode", "claude"]
    assert policy["judge_model"] == "anthropic/claude-opus-5.5"
    assert _task_coding_policy_env(payload) == {
        ca.TASK_AGENTS_ENV: "opencode,claude",
        "MAC_JUDGE_MODEL": "anthropic/claude-opus-5.5",
    }


def test_every_heartbeat_returns_the_hubs_list_and_the_worker_adopts_it(monkeypatch):
    monkeypatch.setenv("MAC_CODING_AGENTS", "claude,opencode")
    cp = _plane()
    lifecycle = WorkerCredentialLifecycle(cp.store)
    issued = lifecycle.issue("agent_alpha")
    lifecycle.activate("agent_alpha", issued.record["id"])
    client = TestClient(
        create_app(control_plane=cp, auth_tokens={"static-admin": {"scopes": ["admin"]}})
    )
    reply = client.post(
        "/agents/agent_alpha/heartbeat",
        headers={"Authorization": "Bearer " + issued.token},
        json={"status": "idle"},
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["coding_policy"]["agents"] == ["claude", "opencode"]

    # Registered so monkeypatch restores it after the worker writes it.
    monkeypatch.setenv(ca.HUB_AGENTS_ENV, "opencode")
    _adopt_hub_coding_policy(reply.json()["coding_policy"])
    order = ca.coding_agent_order()
    assert order.agents == ("claude", "opencode") and order.source == "hub"


def test_the_heartbeat_reports_every_listed_cli(monkeypatch):
    from mac.worker import _resources_with_command_inventory

    monkeypatch.setenv(ca.HUB_AGENTS_ENV, "opencode,claude")
    resources = _resources_with_command_inventory({})
    coding = resources["coding_clis"]
    assert coding["schema"] == "mac.coding_clis.v2"
    assert list(coding["clis"]) == ["opencode", "claude"]
    assert coding["order"] == {"agents": ["opencode", "claude"], "source": "hub", "notes": []}


# --- failing over after a run --------------------------------------------------


class _Result:
    def __init__(self, returncode: int = 0, **attrs: Any) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""
        for name, value in attrs.items():
            setattr(self, name, value)


@pytest.fixture
def run_state(monkeypatch):
    monkeypatch.setattr(ex, "_LAST_CODING_ROUTE", {})
    monkeypatch.setattr(ex, "_CODING_AGENT_RUNS", [])
    posts: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        ex,
        "_post_board_as_hub",
        lambda task_id, kind, body, **meta: posts.append({"kind": kind, "body": body, **meta}),
    )
    return posts


def _ran(agent: str, order=("claude", "opencode")) -> None:
    ex._LAST_CODING_ROUTE.clear()
    ex._LAST_CODING_ROUTE.update({"agent": agent, "order": list(order), "skipped": []})


def test_a_rate_limited_route_moves_to_the_next_cli(run_state, monkeypatch, tmp_path):
    posts = run_state
    _ran("claude")
    failures = {"claude": {"failure_class": "route_rate_limited", "status_code": 429}}
    monkeypatch.setattr(ex, "_route_failure_since", lambda task_id, agent, since: failures.get(agent))
    calls: List[Dict[str, Any]] = []

    def invoke(runner, prompt, workspace, audit_id, opts):
        calls.append(opts)
        _ran("opencode")
        return _Result(0)

    monkeypatch.setattr(ex, "_invoke_agent", invoke)
    out = ex._fail_over_coding_agent(None, "p", tmp_path, "task_1", _Result(1), {"task": {}}, "t0")
    assert out.returncode == 0
    assert calls[0]["exclude_agents"] == ["claude"]
    assert "route_rate_limited" in posts[0]["body"] and posts[0]["kind"] == "status"
    assert [run["agent"] for run in ex._CODING_AGENT_RUNS] == ["claude", "opencode"]
    assert ex._CODING_AGENT_RUNS[0]["route_failure"]["status_code"] == 429


@pytest.mark.parametrize(
    "result",
    [
        _Result(1),  # the route answered; the task itself failed
        _Result(68, mac_repository_verification_failure={"failure_class": "repository_test_failed"}),
        _Result(66, mac_read_only_repository_violation="wrote to the repository"),
        _Result(0),
    ],
)
def test_a_task_outcome_never_moves_the_list(run_state, monkeypatch, tmp_path, result):
    _ran("claude")
    route = {"failure_class": "route_rate_limited", "status_code": 429}
    monkeypatch.setattr(
        ex,
        "_route_failure_since",
        lambda *a: None if result.returncode == 1 else route,
    )
    monkeypatch.setattr(ex, "_invoke_agent", lambda *a: pytest.fail("must not fail over"))
    assert ex._fail_over_coding_agent(None, "p", tmp_path, "t", result, {}, "t0") is result


def test_each_cli_runs_at_most_once(run_state, monkeypatch, tmp_path):
    posts = run_state
    _ran("claude")
    failure = {"failure_class": "route_upstream_unavailable", "status_code": 503}
    monkeypatch.setattr(ex, "_route_failure_since", lambda *a: failure)
    calls: List[Any] = []

    def invoke(runner, prompt, workspace, audit_id, opts):
        calls.append(opts["exclude_agents"])
        _ran("opencode")
        return _Result(1)

    monkeypatch.setattr(ex, "_invoke_agent", invoke)
    out = ex._fail_over_coding_agent(None, "p", tmp_path, "t", _Result(1), {}, "t0")
    assert out.returncode == 1 and calls == [["claude"]]
    assert "no other coding CLI is on the list" in posts[-1]["body"]


def test_the_runs_are_written_to_the_evidence(run_state, tmp_path):
    (tmp_path / "mac-evidence.json").write_text(json.dumps({"schema": "x"}))
    ex._LAST_JUDGE_VERDICT.clear()
    _ran("opencode", order=("opencode", "claude"))
    ex._LAST_CODING_ROUTE["order_source"] = "hub"
    ex._CODING_AGENT_RUNS.append({"agent": "opencode", "returncode": 0, "skipped": []})
    ex._record_judge_verdict_in_evidence(tmp_path)
    manifest = json.loads((tmp_path / "mac-evidence.json").read_text())
    assert manifest["coding_agents"] == {
        "order": ["opencode", "claude"],
        "order_source": "hub",
        "runs": [{"agent": "opencode", "returncode": 0, "skipped": []}],
    }
    assert "judge" not in manifest


# --- reading the router's own records -----------------------------------------------


def _route_event(path: str, status: int, created: str, sequence: int) -> Dict[str, Any]:
    return {
        "created_at": created,
        "sequence": sequence,
        "detail": {"path": path, "status_code": status, "provider": "openrouter"},
    }


@pytest.mark.parametrize(
    "events,agent,expected",
    [
        ([_route_event("/v1/messages", 429, "t1", 1)], "claude", "route_rate_limited"),
        ([_route_event("/v1/messages", 401, "t1", 1)], "claude", "route_auth_failed"),
        ([_route_event("/v1/messages", 502, "t1", 1)], "claude", "route_upstream_unavailable"),
        (
            [_route_event("/v1/messages", 429, "t1", 1), _route_event("/v1/messages", 200, "t2", 2)],
            "claude",
            None,
        ),
        ([_route_event("/v1/chat/completions", 429, "t1", 1)], "claude", None),
        ([_route_event("/v1/chat/completions", 503, "t1", 1)], "opencode", "route_upstream_unavailable"),
        ([_route_event("/v1/messages", 400, "t1", 1)], "claude", None),
        ([], "claude", None),
    ],
)
def test_route_failures_come_from_the_routers_status_codes(monkeypatch, events, agent, expected):
    monkeypatch.setattr(ex, "_hub_env", lambda: ("http://hub", "worker-token"))
    seen: Dict[str, str] = {}

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        return io.BytesIO(json.dumps(events).encode())

    monkeypatch.setattr(ex.urllib.request, "urlopen", urlopen)
    failure = ex._route_failure_since("task_1", agent, "2026-10-08T00:00:00+00:00")
    assert (failure or {}).get("failure_class") == expected
    assert "name=llm.route" in seen["url"] and "subject_id=task_1" in seen["url"]


# --- the /v1/messages route reports itself like the chat route -----------------------


def test_the_messages_route_records_each_call_against_the_tokens_task(monkeypatch):
    cp = _plane()
    task = cp.create_task("router")
    token = InferenceTokenLifecycle(cp.store).mint("agent_alpha", task_id=task.id).token
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    app = create_app(control_plane=cp, auth_tokens={"static-admin": {"scopes": ["admin"]}})
    observed: List[Dict[str, Any]] = []

    def opener(request, timeout):
        import urllib.error

        raise urllib.error.HTTPError(
            request.full_url, 429, "busy", {}, io.BytesIO(b'{"type":"error"}')
        )

    env = {
        "MAC_ROUTER_BACKEND": "inproc",
        "MAC_ROUTER_PROVIDERS": "openrouter=https://openrouter.ai/api/v1,0,key=OPENROUTER_API_KEY",
        "MAC_ROUTER_ANTHROPIC_PROVIDER": "openrouter",
    }
    assert anthropic_passthrough.mount_anthropic_messages(
        app, env=env, opener=opener, route_observer=observed.append
    )
    reply = TestClient(app).post(
        "/v1/messages",
        headers={"Authorization": "Bearer " + token, "X-MAC-Task-ID": "task_spoofed"},
        json={"model": "anthropic/claude-opus-5.5", "max_tokens": 8, "messages": []},
    )
    assert reply.status_code == 429
    assert observed[0]["schema"] == "mac.llm_route.v1"
    assert observed[0]["path"] == "/v1/messages"
    assert observed[0]["status_code"] == 429 and observed[0]["outcome"] == "provider_failure"
    # The token's task, not a header, says whose call this was.
    assert observed[0]["task_id"] == task.id
