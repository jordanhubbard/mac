"""Unit tests for the transport-resolution layer in :mod:`mac.dispatch`."""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest


@pytest.fixture(autouse=True)
def _mac_secret_key(monkeypatch):
    """LocalDispatch instantiates ControlPlane which requires MAC_SECRET_KEY."""
    monkeypatch.setenv("MAC_SECRET_KEY", "dispatch-test-key-with-at-least-32-characters")


from mac.dispatch import (
    DispatchError,
    LocalDispatch,
    RemoteDispatch,
    _Dictish,
    _wrap_list,
    resolve_dispatch,
)


# ---------------------------------------------------------------------------
# _Dictish — the wrapper that satisfies cli._print's `.to_dict()` contract
# ---------------------------------------------------------------------------


def test_dictish_to_dict_returns_underlying():
    d = _Dictish({"a": 1, "b": "two"})
    assert d.to_dict() == {"a": 1, "b": "two"}


def test_dictish_supports_dict_access():
    d = _Dictish({"id": "task_1", "state": "open"})
    assert d["id"] == "task_1"
    assert d.get("state") == "open"
    assert d.get("missing", "default") == "default"
    assert "id" in d
    assert "missing" not in d


def test_dictish_handles_empty_payload():
    d = _Dictish(None)  # type: ignore[arg-type]
    assert d.to_dict() == {}


def test_wrap_list_wraps_dicts():
    wrapped = _wrap_list([{"a": 1}, {"b": 2}])
    assert all(isinstance(item, _Dictish) for item in wrapped)
    assert [item.to_dict() for item in wrapped] == [{"a": 1}, {"b": 2}]


def test_wrap_list_handles_none_and_empty():
    assert _wrap_list(None) == []
    assert _wrap_list([]) == []


def test_wrap_list_handles_envelope_wrapper():
    # Some endpoints wrap lists in {"items": [...]} or {"results": [...]}.
    payload = {"items": [{"id": "x"}, {"id": "y"}]}
    wrapped = _wrap_list(payload)
    assert [w.to_dict() for w in wrapped] == [{"id": "x"}, {"id": "y"}]


# ---------------------------------------------------------------------------
# LocalDispatch — pass-through to a ControlPlane stand-in
# ---------------------------------------------------------------------------


class _FakePlane:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, tuple, dict]] = []
        self.store = object()

    def make_task(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("make_task", args, kwargs))
        return "ok"


def test_local_dispatch_forwards_method_calls():
    plane = _FakePlane()
    disp = LocalDispatch(plane)
    result = disp.make_task("hello", priority=1)
    assert result == "ok"
    assert plane.calls == [("make_task", ("hello",), {"priority": 1})]


def test_local_dispatch_exposes_store():
    plane = _FakePlane()
    assert LocalDispatch(plane).store is plane.store


# ---------------------------------------------------------------------------
# RemoteDispatch — refuses direct SQL, errors on unwrapped methods
# ---------------------------------------------------------------------------


class _FakeHttpClient:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        self.calls.append((method, path, body))
        return {"echo": path}


def test_remote_dispatch_refuses_direct_store_access():
    disp = RemoteDispatch(_FakeHttpClient())  # type: ignore[arg-type]
    with pytest.raises(DispatchError, match="direct SQLite access"):
        disp.store.query_all("SELECT 1")


def test_remote_dispatch_errors_on_unwrapped_method():
    disp = RemoteDispatch(_FakeHttpClient())  # type: ignore[arg-type]
    with pytest.raises(DispatchError, match="not yet supported in hub mode"):
        disp.completely_made_up_method(1, 2)


# ---------------------------------------------------------------------------
# resolve_dispatch — argument resolution and the no-silent-fallback rule
# ---------------------------------------------------------------------------


def _ns(**kwargs: Any) -> argparse.Namespace:
    base = dict(db=None, hub_url=None, token=None, fleet=None)
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_resolve_dispatch_with_explicit_db(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MAC_API_URL", raising=False)
    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_QUIET_LOCAL_BANNER", "1")
    db_path = tmp_path / "mac.db"
    args = _ns(db=str(db_path))
    disp = resolve_dispatch(args)
    assert isinstance(disp, LocalDispatch)


def test_resolve_dispatch_with_mac_db_env(tmp_path, monkeypatch):
    monkeypatch.delenv("MAC_API_URL", raising=False)
    monkeypatch.setenv("MAC_DB", str(tmp_path / "from_env.db"))
    monkeypatch.setenv("MAC_QUIET_LOCAL_BANNER", "1")
    disp = resolve_dispatch(_ns())
    assert isinstance(disp, LocalDispatch)


def test_resolve_dispatch_with_explicit_hub_url(monkeypatch):
    monkeypatch.delenv("MAC_API_URL", raising=False)
    monkeypatch.delenv("MAC_DB", raising=False)
    # Block ~/.mac/.env from leaking in by pointing the loader at /dev/null.
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    args = _ns(hub_url="http://hub.example:8789", token="t")
    disp = resolve_dispatch(args)
    assert isinstance(disp, RemoteDispatch)


def test_resolve_dispatch_with_mac_api_url_env(monkeypatch):
    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    monkeypatch.setenv("MAC_API_URL", "http://hub.example:8789")
    monkeypatch.setenv("MAC_API_TOKEN", "tok")
    disp = resolve_dispatch(_ns())
    assert isinstance(disp, RemoteDispatch)


def test_resolve_dispatch_explicit_db_wins_over_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    monkeypatch.setenv("MAC_API_URL", "http://hub.example:8789")
    monkeypatch.setenv("MAC_QUIET_LOCAL_BANNER", "1")
    db_path = tmp_path / "mac.db"
    disp = resolve_dispatch(_ns(db=str(db_path)))
    assert isinstance(disp, LocalDispatch)


def test_resolve_dispatch_errors_when_nothing_configured(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MAC_API_URL", raising=False)
    monkeypatch.delenv("MAC_URL", raising=False)
    monkeypatch.delenv("MAC_HUB_URL", raising=False)
    monkeypatch.delenv("HGMAC_URL", raising=False)
    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.delenv("MAC_FLEET", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    # Isolate from the dev machine's real ~/.mac/fleets.yaml: point the loader
    # at a path that doesn't exist so no default fleet is inferred.
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(tmp_path / "absent.yaml"))
    with pytest.raises(SystemExit) as excinfo:
        resolve_dispatch(_ns())
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "no hub configured" in captured.err
    assert "--db" in captured.err
    assert "MAC_API_URL" in captured.err


# ---------------------------------------------------------------------------
# default-fleet selection + ~/.mac/.env auto-loading (flagless usability)
# ---------------------------------------------------------------------------


def _write_fleets(tmp_path, monkeypatch, body: str):
    path = tmp_path / "fleets.yaml"
    path.write_text(body)
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(path))
    return path


def _clear_hub_env(monkeypatch):
    for name in ("MAC_API_URL", "MAC_URL", "MAC_HUB_URL", "HGMAC_URL", "MAC_DB", "MAC_FLEET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")


def test_default_fleet_lone_fleet_is_default(tmp_path, monkeypatch):
    import mac.dispatch as d

    _write_fleets(tmp_path, monkeypatch, "fleets:\n  only:\n    hub_url: http://only:8789\n")
    assert d._default_fleet_from_yaml() == "only"


def test_default_fleet_marked_wins_among_many(tmp_path, monkeypatch):
    import mac.dispatch as d

    _write_fleets(
        tmp_path,
        monkeypatch,
        "fleets:\n"
        "  a:\n    hub_url: http://a:8789\n"
        "  b:\n    hub_url: http://b:8789\n    default: true\n",
    )
    assert d._default_fleet_from_yaml() == "b"


def test_default_fleet_ambiguous_returns_none(tmp_path, monkeypatch):
    import mac.dispatch as d

    _write_fleets(
        tmp_path,
        monkeypatch,
        "fleets:\n  a:\n    hub_url: http://a:8789\n  b:\n    hub_url: http://b:8789\n",
    )
    assert d._default_fleet_from_yaml() is None


def test_resolve_dispatch_uses_default_fleet_url_and_dotenv_token(tmp_path, monkeypatch):
    """Flagless `mac`: lone fleet -> hub_url, token pulled from ~/.mac/.env."""
    _clear_hub_env(monkeypatch)
    _write_fleets(tmp_path, monkeypatch, "fleets:\n  only:\n    hub_url: http://only:8789\n")
    env_file = tmp_path / ".env"
    env_file.write_text("export MAC_API_TOKEN__ONLY=sekret\n")
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", str(env_file))
    disp = resolve_dispatch(_ns())  # no --fleet, no --hub-url, no --token
    assert isinstance(disp, RemoteDispatch)
    assert disp._client.base_url == "http://only:8789"
    assert disp._client.token == "sekret"


class _FakeTransport:
    """Records HTTP calls and returns canned responses for cli end-to-end tests."""

    def __init__(self, response_for=None):
        self.response_for = response_for or {}
        self.calls: List[Tuple[str, str, Optional[Dict[str, Any]], Optional[str]]] = []

    def __call__(self, method: str, url: str, body, token):
        self.calls.append((method, url, body, token))
        # Match by exact (method, path) or method-only fallback.
        from urllib.parse import urlsplit

        path = urlsplit(url).path
        for (m, p), resp in self.response_for.items():
            if m == method and p == path:
                return resp
        return self.response_for.get(method, {})


def test_remote_dispatch_read_endpoints_hit_correct_paths():
    """ready/search/stats route to the hub endpoints (parity-ready-http-01)."""
    from mac.http_client import HubClient

    fake = _FakeTransport(
        response_for={
            ("GET", "/tasks/ready"): [{"id": "t1"}],
            ("GET", "/tasks/search"): [{"id": "t2"}],
            ("GET", "/tasks/stats"): {"open": 5},
        }
    )
    disp = RemoteDispatch(HubClient("http://hub:8789", token="tok", transport=fake))

    assert [x.to_dict() for x in disp.ready_tasks(project="mac", limit=3)] == [{"id": "t1"}]
    assert [x.to_dict() for x in disp.search_tasks("foo", project="mac")] == [{"id": "t2"}]
    assert disp.task_stats(project="mac") == {"open": 5}

    gets = [url for (method, url, _body, _tok) in fake.calls if method == "GET"]
    assert any("/tasks/ready" in u and "project=mac" in u and "limit=3" in u for u in gets)
    assert any("/tasks/search" in u and "q=foo" in u and "project=mac" in u for u in gets)
    assert any("/tasks/stats" in u and "project=mac" in u for u in gets)
    # tenant_id was None and must be omitted from the query string.
    assert all("tenant_id" not in u for u in gets)


def test_remote_dispatch_secret_access_uses_api_body_shape():
    """`mac secret access` in hub mode must use SecretAccessRequest's field names."""
    from mac.http_client import HubClient

    fake = _FakeTransport(response_for={("POST", "/secrets/github.token/access"): {"granted": True}})
    disp = RemoteDispatch(HubClient("http://hub:8789", token="tok", transport=fake))

    assert disp.request_secret("github.token", "agent_rocky", "git-clone").to_dict() == {
        "granted": True
    }

    method, url, body, token = fake.calls[-1]
    assert method == "POST"
    assert url == "http://hub:8789/secrets/github.token/access"
    assert token == "tok"
    assert body == {"accessor_agent_id": "agent_rocky", "purpose": "git-clone"}


def test_remote_dispatch_read_agentbus_chunks_passes_agent_id():
    """`mac agentbus read` in hub mode — regression for the (stream_id, **kw)
    signature that dropped agent_id and raised a positional-arg TypeError."""
    from mac.http_client import HubClient

    fake = _FakeTransport(
        response_for={("GET", "/agentbus/streams/bus_x/chunks"): [{"sequence": 0}]}
    )
    disp = RemoteDispatch(HubClient("http://hub:8789", token="tok", transport=fake))
    # Mirror the CLI call shape: agent_id first, then stream_id, kwargs after.
    chunks = disp.read_agentbus_chunks("agent_rocky", "bus_x", after_sequence=2, limit=5)
    assert [c.to_dict() for c in chunks] == [{"sequence": 0}]
    url = next(u for (m, u, _b, _t) in fake.calls if m == "GET")
    assert "/agentbus/streams/bus_x/chunks" in url
    assert "agent_id=agent_rocky" in url
    assert "after_sequence=2" in url
    assert "limit=5" in url


def test_remote_dispatch_memory_wrappers_hit_hub_paths():
    from mac.http_client import HubClient

    fake = _FakeTransport(
        response_for={
            ("POST", "/memory"): {"id": "mem_1"},
            ("GET", "/memory"): [{"id": "mem_2"}],
            ("POST", "/memory/remembered"): {"id": "mem_key"},
            ("GET", "/memory/remembered"): [{"key": "k"}],
            ("DELETE", "/memory/remembered/k"): {"deleted": 1, "key": "k", "project": "mac"},
            ("GET", "/v1/memory/dreams/recall"): [{"memory_id": "mem_dream"}],
        }
    )
    disp = RemoteDispatch(HubClient("http://hub:8789", token="tok", transport=fake))

    created = disp.add_memory(
        None,
        "project",
        "mac",
        "deployment_learning:mac",
        "learned thing",
        None,
        "deployment-learning",
    )
    found = disp.search_memory("task_1", "dream", "project:mac")
    remembered = disp.remember_memory(
        "k",
        "remember this",
        project="mac",
        actor="operator",
    )
    listed = disp.list_remembered_memory(project="mac")
    forgotten = disp.forget_memory("k", project="mac")
    dreams = disp.recall_dream_artifacts(
        "hub memories",
        project="mac",
        agent_id="agent_rocky",
        scope="project",
        kind="knowledge_snippet",
        min_confidence="medium",
        limit=7,
    )

    assert created.to_dict() == {"id": "mem_1"}
    assert [item.to_dict() for item in found] == [{"id": "mem_2"}]
    assert remembered.to_dict() == {"id": "mem_key"}
    assert [item.to_dict() for item in listed] == [{"key": "k"}]
    assert forgotten.to_dict() == {"deleted": 1, "key": "k", "project": "mac"}
    assert [item.to_dict() for item in dreams] == [{"memory_id": "mem_dream"}]
    post = fake.calls[0]
    assert post[0] == "POST"
    assert post[1] == "http://hub:8789/memory"
    assert post[2]["record_type"] == "deployment_learning:mac"
    memory_get = fake.calls[1][1]
    assert "/memory" in memory_get
    assert "task_id=task_1" in memory_get
    assert "subject_type=dream" in memory_get
    remember_post = fake.calls[2]
    assert remember_post[1] == "http://hub:8789/memory/remembered"
    assert remember_post[2] == {
        "key": "k",
        "content": "remember this",
        "project": "mac",
        "actor": "operator",
    }
    assert fake.calls[3][1] == "http://hub:8789/memory/remembered?project=mac"
    assert fake.calls[4][1] == "http://hub:8789/memory/remembered/k?project=mac"
    dream_get = fake.calls[5][1]
    assert "/v1/memory/dreams/recall" in dream_get
    assert "agent_id=agent_rocky" in dream_get
    assert "min_confidence=medium" in dream_get
    assert "limit=7" in dream_get


def test_remote_dispatch_nap_wrappers_hit_hub_paths():
    from mac.http_client import HubClient

    fake = _FakeTransport(
        response_for={
            ("GET", "/nap-due"): [{"agent_id": "agent_rocky"}],
            ("POST", "/agents/agent_rocky/nap-cycle"): {"cycled": True},
            ("POST", "/agents/agent_rocky/nap-consolidate"): {"summaries_written": 1},
        }
    )
    disp = RemoteDispatch(HubClient("http://hub:8789", token="tok", transport=fake))

    due = disp.list_due_nap_agents(as_of="2026-06-18T00:00:00Z")
    cycle = disp.run_nap_cycle(
        "agent_rocky",
        actor="operator",
        embed_into_medium=True,
        emit_dream_artifacts=False,
        qdrant_url="http://qdrant:6333",
    )
    consolidated = disp.consolidate_nap(
        "agent_rocky",
        since="2026-06-17T00:00:00Z",
        nap_run_id="nap_1",
        embed_into_medium=False,
        emit_dream_artifacts=True,
        created_by="operator",
        qdrant_url="http://qdrant:6333",
    )

    assert [item.to_dict() for item in due] == [{"agent_id": "agent_rocky"}]
    assert cycle.to_dict() == {"cycled": True}
    assert consolidated.to_dict() == {"summaries_written": 1}
    assert fake.calls[0][0] == "GET"
    assert "as_of=2026-06-18T00%3A00%3A00Z" in fake.calls[0][1]
    assert fake.calls[1][2] == {
        "actor": "operator",
        "embed_into_medium": True,
        "emit_dream_artifacts": False,
        "qdrant_url": "http://qdrant:6333",
    }
    assert fake.calls[2][2] == {
        "since": "2026-06-17T00:00:00Z",
        "nap_run_id": "nap_1",
        "embed_into_medium": False,
        "emit_dream_artifacts": True,
        "created_by": "operator",
        "qdrant_url": "http://qdrant:6333",
    }
    with pytest.raises(DispatchError, match="vector writer"):
        disp.run_nap_cycle("agent_rocky", vector_writer=object())


def test_remote_dispatch_repo_update_hits_hub_endpoint():
    from mac.http_client import HubClient

    fake = _FakeTransport(
        response_for={
            ("POST", "/agentbus/repo-update"): {
                "schema": "mac.agentbus.repo_update_publish.v1",
                "count": 3,
            }
        }
    )
    disp = RemoteDispatch(HubClient("http://hub:8789", token="tok", transport=fake))

    result = disp.publish_agentbus_repo_update(
        sender_agent_id="agent_hub",
        all_agents=True,
        repo_path="/home/mac/.mac/src/mac",
        remote="origin",
        branch="main",
        restart=True,
        restart_services=["mac.service"],
        request_id="refresh-1",
    )

    assert result.to_dict() == {
        "schema": "mac.agentbus.repo_update_publish.v1",
        "count": 3,
    }
    method, url, payload, token = fake.calls[0]
    assert method == "POST"
    assert url == "http://hub:8789/agentbus/repo-update"
    assert token == "tok"
    assert payload == {
        "sender_agent_id": "agent_hub",
        "recipient_agent_ids": [],
        "all_agents": True,
        "repo_path": "/home/mac/.mac/src/mac",
        "remote": "origin",
        "branch": "main",
        "restart": True,
        "restart_services": ["mac.service"],
        "request_id": "refresh-1",
    }


def test_remote_dispatch_create_task_via_cli(monkeypatch):
    """End-to-end: `mac --hub-url ... task create` posts to /tasks."""
    import io
    import json as _json
    import sys

    from mac.cli import main
    from mac.http_client import HubClient
    import mac.dispatch as dispatch_mod

    monkeypatch.delenv("MAC_API_URL", raising=False)
    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")

    fake = _FakeTransport(
        response_for={
            ("POST", "/tasks"): {
                "id": "task_remote_1",
                "title": "Stop the runaway loop",
                "state": "open",
                "project": "mac",
            }
        }
    )
    orig_init = HubClient.__init__

    def init_with_fake_transport(self, base_url, *, token=None, transport=None):
        orig_init(self, base_url, token=token, transport=fake)

    monkeypatch.setattr(HubClient, "__init__", init_with_fake_transport)

    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(
            [
                "--hub-url",
                "http://hub.example:8789",
                "--token",
                "tok",
                "task",
                "create",
                "Stop the runaway loop",
                "--project",
                "mac",
                "--actor",
                "jordanh",
            ]
        )
    finally:
        sys.stdout = old
    assert rc == 0
    body = _json.loads(out.getvalue())
    assert body["id"] == "task_remote_1"
    # Verify we actually went over HTTP, not into SQLite.
    assert len(fake.calls) == 1
    method, url, payload, token = fake.calls[0]
    assert method == "POST"
    assert url == "http://hub.example:8789/tasks"
    assert payload["title"] == "Stop the runaway loop"
    assert payload["project"] == "mac"
    assert payload["actor"] == "jordanh"
    assert token == "tok"


def test_remote_dispatch_task_show_via_cli(monkeypatch):
    import io
    import json as _json
    import sys

    from mac.cli import main
    from mac.http_client import HubClient

    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")

    fake = _FakeTransport(
        response_for={
            ("GET", "/tasks/task_xyz"): {"id": "task_xyz", "state": "reviewing"}
        }
    )
    orig_init = HubClient.__init__
    monkeypatch.setattr(
        HubClient,
        "__init__",
        lambda self, base_url, *, token=None, transport=None: orig_init(self, base_url, token=token, transport=fake),
    )

    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--hub-url", "http://hub.example:8789", "task", "show", "task_xyz"])
    finally:
        sys.stdout = old
    assert rc == 0
    assert _json.loads(out.getvalue()) == {"id": "task_xyz", "state": "reviewing"}
    assert fake.calls[0][0] == "GET"
    assert fake.calls[0][1] == "http://hub.example:8789/tasks/task_xyz"


def test_remote_dispatch_task_claim_returns_task_and_lease(monkeypatch):
    """`task claim` reads `task` and `lease.id` from a two-field response."""
    import io
    import json as _json
    import sys

    from mac.cli import main
    from mac.http_client import HubClient

    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")

    fake = _FakeTransport(
        response_for={
            ("POST", "/tasks/task_xyz/claim"): {
                "task": {"id": "task_xyz", "state": "claimed"},
                "lease": {"id": "lease_42", "agent_id": "agent_natasha"},
            }
        }
    )
    orig_init = HubClient.__init__
    monkeypatch.setattr(
        HubClient,
        "__init__",
        lambda self, base_url, *, token=None, transport=None: orig_init(self, base_url, token=token, transport=fake),
    )

    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(
            [
                "--hub-url",
                "http://hub.example:8789",
                "task",
                "claim",
                "task_xyz",
                "agent_natasha",
            ]
        )
    finally:
        sys.stdout = old
    assert rc == 0
    body = _json.loads(out.getvalue())
    # cli.cmd_task_claim builds {"task": ..., "lease_id": lease.id if lease else None}
    assert body["task"]["id"] == "task_xyz"
    assert body["lease_id"] == "lease_42"


def test_remote_dispatch_nap_cycle_via_cli_uses_hub_writer(monkeypatch):
    import json as _json

    from mac.cli import main
    from mac.http_client import HubClient

    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    monkeypatch.setattr(
        "mac.cli._build_vector_writer",
        lambda _args: (_ for _ in ()).throw(AssertionError("built local writer")),
    )

    fake = _FakeTransport(
        response_for={
            ("POST", "/agents/agent_rocky/nap-cycle"): {
                "nap_run": {"agent_id": "agent_rocky"},
                "consolidation": {"summaries_written": 1},
            }
        }
    )
    orig_init = HubClient.__init__
    monkeypatch.setattr(
        HubClient,
        "__init__",
        lambda self, base_url, *, token=None, transport=None: orig_init(
            self, base_url, token=token, transport=fake
        ),
    )

    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(
            [
                "--hub-url",
                "http://hub.example:8789",
                "nap",
                "cycle",
                "agent_rocky",
                "--qdrant-url",
                "http://qdrant:6333",
            ]
        )
    finally:
        sys.stdout = old
    assert rc == 0
    assert _json.loads(out.getvalue())["nap_run"]["agent_id"] == "agent_rocky"
    method, url, payload, _token = fake.calls[0]
    assert method == "POST"
    assert url == "http://hub.example:8789/agents/agent_rocky/nap-cycle"
    assert payload["embed_into_medium"] is True
    assert payload["emit_dream_artifacts"] is True
    assert payload["qdrant_url"] == "http://qdrant:6333"


def test_remote_dispatch_memory_list_via_cli_uses_hub(monkeypatch):
    import json as _json

    from mac.cli import main
    from mac.http_client import HubClient

    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")

    fake = _FakeTransport(
        response_for={("GET", "/memory/remembered"): [{"key": "k", "content": "v"}]}
    )
    orig_init = HubClient.__init__
    monkeypatch.setattr(
        HubClient,
        "__init__",
        lambda self, base_url, *, token=None, transport=None: orig_init(
            self, base_url, token=token, transport=fake
        ),
    )

    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(
            [
                "--hub-url",
                "http://hub.example:8789",
                "memory",
                "list",
                "--project",
                "mac",
            ]
        )
    finally:
        sys.stdout = old
    assert rc == 0
    assert _json.loads(out.getvalue()) == [{"key": "k", "content": "v"}]
    method, url, _payload, _token = fake.calls[0]
    assert method == "GET"
    assert url == "http://hub.example:8789/memory/remembered?project=mac"


def test_remote_dispatch_fleet_refresh_source_via_cli_uses_hub(monkeypatch):
    import json as _json

    from mac.cli import main
    from mac.http_client import HubClient

    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")

    fake = _FakeTransport(
        response_for={
            ("POST", "/agentbus/repo-update"): {
                "schema": "mac.agentbus.repo_update_publish.v1",
                "count": 3,
            }
        }
    )
    orig_init = HubClient.__init__
    monkeypatch.setattr(
        HubClient,
        "__init__",
        lambda self, base_url, *, token=None, transport=None: orig_init(
            self, base_url, token=token, transport=fake
        ),
    )

    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(
            [
                "--hub-url",
                "http://hub.example:8789",
                "fleet",
                "refresh-source",
                "--sender-agent-id",
                "agent_hub",
                "--request-id",
                "refresh-1",
                "--restart-service",
                "mac.service",
            ]
        )
    finally:
        sys.stdout = old
    assert rc == 0
    assert _json.loads(out.getvalue()) == {
        "schema": "mac.agentbus.repo_update_publish.v1",
        "count": 3,
    }
    method, url, payload, _token = fake.calls[0]
    assert method == "POST"
    assert url == "http://hub.example:8789/agentbus/repo-update"
    assert payload["sender_agent_id"] == "agent_hub"
    assert payload["all_agents"] is True
    assert payload["remote"] == "origin"
    assert payload["branch"] == "main"
    assert payload["restart"] is True
    assert payload["restart_services"] == ["mac.service"]
    assert payload["request_id"] == "refresh-1"


def test_remote_dispatch_task_close_uses_api_transition_shape(monkeypatch):
    import io
    import json as _json
    import sys

    from mac.cli import main
    from mac.http_client import HubClient

    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")

    fake = _FakeTransport(
        response_for={
            ("POST", "/tasks/task_xyz/transition"): {
                "id": "task_xyz",
                "state": "completed",
            }
        }
    )
    orig_init = HubClient.__init__
    monkeypatch.setattr(
        HubClient,
        "__init__",
        lambda self, base_url, *, token=None, transport=None: orig_init(self, base_url, token=token, transport=fake),
    )

    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(
            [
                "--hub-url",
                "http://hub.example:8789",
                "task",
                "close",
                "task_xyz",
                "--reason",
                "done",
                "--actor",
                "codex-cli",
            ]
        )
    finally:
        sys.stdout = old
    assert rc == 0
    assert _json.loads(out.getvalue()) == {"id": "task_xyz", "state": "completed"}
    method, url, payload, _token = fake.calls[0]
    assert method == "POST"
    assert url == "http://hub.example:8789/tasks/task_xyz/transition"
    assert payload == {
        "target_state": "completed",
        "actor": "codex-cli",
        "detail": {"reason": "done"},
    }


def test_dictish_supports_attribute_access():
    d = _Dictish({"id": "lease_42", "agent_id": "agent_x"})
    assert d.id == "lease_42"  # noqa: E501 — matches typed-object access pattern
    assert d.agent_id == "agent_x"


def test_dictish_attribute_missing_raises_attribute_error():
    d = _Dictish({"id": "x"})
    with pytest.raises(AttributeError):
        _ = d.does_not_exist


def test_dictish_falsy_when_empty():
    assert not _Dictish({})
    assert _Dictish({"x": 1})


def test_resolve_dispatch_emits_local_banner(tmp_path, monkeypatch, capsys):
    # Reset banner-once state so this test sees the message.
    import mac.dispatch as dispatch_mod

    dispatch_mod._LOCAL_BANNER_PRINTED = False
    monkeypatch.delenv("MAC_QUIET_LOCAL_BANNER", raising=False)
    monkeypatch.delenv("MAC_API_URL", raising=False)
    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    db_path = tmp_path / "mac.db"
    resolve_dispatch(_ns(db=str(db_path)))
    captured = capsys.readouterr()
    assert "LOCAL db" in captured.err
    assert str(db_path) in captured.err
