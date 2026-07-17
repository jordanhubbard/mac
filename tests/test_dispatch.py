"""Unit tests for the transport-resolution layer in :mod:`mac.dispatch`."""

from __future__ import annotations

import argparse
import io
import sys
from typing import Any, Dict, List, Optional, Tuple

import pytest

from mac.dispatch import (
    DispatchError,
    LocalDispatch,
    RemoteDispatch,
    _Dictish,
    _configured_remote_authority,
    _task_producing_cli_operation,
    _wrap_list,
    resolve_dispatch,
)


@pytest.fixture(autouse=True)
def _mac_secret_key(monkeypatch):
    """LocalDispatch instantiates ControlPlane which requires MAC_SECRET_KEY."""
    monkeypatch.setenv("MAC_SECRET_KEY", "dispatch-test-key-with-at-least-32-characters")


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

    def create_task(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("create_task", args, kwargs))
        return "created"

    def convert_ticketing_source(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("convert_ticketing_source", args, kwargs))
        return "converted"


def test_local_dispatch_forwards_method_calls():
    plane = _FakePlane()
    disp = LocalDispatch(plane)
    result = disp.make_task("hello", priority=1)
    assert result == "ok"
    assert plane.calls == [("make_task", ("hello",), {"priority": 1})]


def test_local_dispatch_exposes_store():
    plane = _FakePlane()
    assert LocalDispatch(plane).store is plane.store


def test_local_dispatch_refuses_hub_owned_reconciler_calls():
    dispatch = LocalDispatch(_FakePlane())
    with pytest.raises(DispatchError, match="running hub"):
        dispatch.repository_ref_reconciler_status()
    with pytest.raises(DispatchError, match="running hub"):
        dispatch.reconcile_repository_refs(mode="audit")


def test_local_dispatch_refuses_unconfirmed_home_task_authority():
    plane = _FakePlane()
    dispatch = LocalDispatch(
        plane,
        db_path="/home/operator/.mac/mac.db",
        local_authority_confirmed=False,
        remote_authority="fleet 'production'",
    )

    with pytest.raises(DispatchError) as excinfo:
        dispatch.create_task("stranded work")

    message = str(excinfo.value)
    assert "never uploaded or reconciled" in message
    assert "fleet 'production'" in message
    assert "--local-authority" in message
    assert plane.calls == []

    without_remote = LocalDispatch(
        plane,
        db_path="/home/operator/.mac/mac.db",
        local_authority_confirmed=False,
    )
    with pytest.raises(DispatchError, match="Target a configured hub"):
        without_remote.create_task("still stranded")


def test_local_dispatch_allows_confirmed_authority_and_read_only_conversion():
    plane = _FakePlane()
    confirmed = LocalDispatch(
        plane,
        db_path="/srv/mac/mac.db",
        local_authority_confirmed=True,
    )
    assert confirmed.create_task("hub work") == "created"

    guarded = LocalDispatch(
        plane,
        db_path="/home/operator/.mac/mac.db",
        local_authority_confirmed=False,
    )
    assert guarded.convert_ticketing_source(".", dry_run=True) == "converted"
    assert [call[0] for call in plane.calls] == [
        "create_task",
        "convert_ticketing_source",
    ]


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


def test_remote_dispatch_repository_ref_reconciler_calls():
    client = _FakeHttpClient()
    dispatch = RemoteDispatch(client)  # type: ignore[arg-type]
    assert dispatch.repository_ref_reconciler_status().to_dict() == {
        "echo": "/repository-refs/reconciler"
    }
    assert dispatch.reconcile_repository_refs(
        mode="prune", actor="operator"
    ).to_dict() == {"echo": "/repository-refs/reconcile"}
    assert client.calls == [
        ("GET", "/repository-refs/reconciler", None),
        (
            "POST",
            "/repository-refs/reconcile",
            {"mode": "prune", "actor": "operator"},
        ),
    ]


# ---------------------------------------------------------------------------
# resolve_dispatch — argument resolution and the no-silent-fallback rule
# ---------------------------------------------------------------------------


def _ns(**kwargs: Any) -> argparse.Namespace:
    base = dict(db=None, hub_url=None, token=None, fleet=None)
    base.update(kwargs)
    return argparse.Namespace(**base)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"command": "interaction", "interaction_command": "task"}, "interaction task creation"),
        ({"command": "project", "project_command": "onboard"}, "project onboarding"),
        ({"command": "bridge", "bridge_command": "import"}, "bridge task import"),
        ({"command": "workflow", "workflow_command": "start"}, "workflow start"),
        ({"command": "rollout", "rollout_command": "health"}, "rollout health"),
        ({"command": "migrate", "migrate_command": "import"}, "task migration import"),
        (
            {"command": "migrate", "migrate_command": "acc", "mode": "import"},
            "ACC task migration",
        ),
        (
            {"command": "task", "task_command": "migrate-beads", "dry_run": False},
            "beads task migration",
        ),
        (
            {"command": "task", "task_command": "convert-ticketing", "dry_run": False},
            "ticket conversion",
        ),
        ({"command": "task", "task_command": "list"}, None),
    ],
)
def test_task_producing_cli_operation_classifies_indirect_writes(values, expected):
    assert _task_producing_cli_operation(_ns(**values)) == expected


def test_configured_remote_authority_describes_explicit_selection(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    assert _configured_remote_authority(_ns(profile="operator"), {}) == (
        "client profile 'operator'"
    )
    assert _configured_remote_authority(_ns(hub_url="https://hub.example"), {}) == (
        "the explicitly selected hub"
    )
    assert _configured_remote_authority(_ns(), {"MAC_API_URL": "https://hub.example"}) == (
        "a hub URL from the environment"
    )
    assert _configured_remote_authority(_ns(fleet="production"), {}) == "fleet 'production'"


def test_resolve_dispatch_with_explicit_db(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MAC_API_URL", raising=False)
    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_QUIET_LOCAL_BANNER", "1")
    db_path = tmp_path / "mac.db"
    args = _ns(db=str(db_path))
    disp = resolve_dispatch(args)
    assert isinstance(disp, LocalDispatch)


def test_resolve_dispatch_requires_local_authority_for_mac_db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("MAC_API_URL", "MAC_URL", "MAC_HUB_URL", "HGMAC_URL", "MAC_FLEET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(tmp_path / "absent-fleets.yaml"))
    monkeypatch.setenv("MAC_DB", str(tmp_path / "from_env.db"))
    monkeypatch.setenv("MAC_QUIET_LOCAL_BANNER", "1")

    with pytest.raises(DispatchError, match="server configuration"):
        resolve_dispatch(_ns())

    disp = resolve_dispatch(_ns(local_authority=True))
    assert isinstance(disp, LocalDispatch)


def test_resolve_dispatch_prefers_hub_over_mac_db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    monkeypatch.setenv("MAC_DB", str(tmp_path / "hub.db"))
    monkeypatch.setenv("MAC_HUB_URL", "http://127.0.0.1:8789")

    disp = resolve_dispatch(_ns())

    assert isinstance(disp, RemoteDispatch)


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


@pytest.mark.parametrize("selector", ["hub_url", "fleet", "profile"])
def test_resolve_dispatch_rejects_conflicting_explicit_authorities(
    tmp_path, monkeypatch, selector
):
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    value = "http://hub.example:8789" if selector == "hub_url" else "production"
    with pytest.raises(DispatchError, match="Choose exactly one"):
        resolve_dispatch(_ns(db=str(tmp_path / "mac.db"), **{selector: value}))


def test_resolve_dispatch_guards_canonical_home_db_task_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    config = tmp_path / "fleets.yaml"
    config.write_text(
        "fleets:\n  production:\n    default: true\n    agents: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(config))
    db_path = tmp_path / ".mac" / "mac.db"
    db_path.parent.mkdir(parents=True)

    with pytest.raises(DispatchError, match="--local-authority"):
        resolve_dispatch(_ns(db=str(db_path)))

    confirmed = resolve_dispatch(_ns(db=str(db_path), local_authority=True))
    assert confirmed.create_task("standalone work").title == "standalone work"


def test_resolve_dispatch_refuses_live_hub_database_maintenance(tmp_path, monkeypatch):
    import mac.dispatch as dispatch_mod

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    monkeypatch.setenv("MAC_CONTROL_PLANE_ROLE", "hub")
    db_path = tmp_path / ".mac" / "mac.db"
    db_path.parent.mkdir(parents=True)
    monkeypatch.setenv("MAC_DB", str(db_path))
    monkeypatch.setenv("MAC_HUB_URL", "http://127.0.0.1:8789")
    monkeypatch.setattr(dispatch_mod, "_hub_is_reachable", lambda _url: True)

    with pytest.raises(DispatchError, match="while the hub is running"):
        resolve_dispatch(_ns(local_authority=True))


def test_resolve_dispatch_opens_existing_db_without_schema_initialization(
    tmp_path, monkeypatch
):
    import mac.store as store_mod

    db_path = tmp_path / "existing.db"
    store = store_mod.SQLiteStore(str(db_path))
    store.close()
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(tmp_path / "absent-fleets.yaml"))
    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.delenv("MAC_API_URL", raising=False)

    def fail_if_initialized(_self):
        raise AssertionError("routine CLI open must not run schema DDL")

    monkeypatch.setattr(store_mod.SQLiteStore, "_initialize", fail_if_initialized)

    disp = resolve_dispatch(_ns(db=str(db_path)))

    assert isinstance(disp, LocalDispatch)


def test_resolve_dispatch_rejects_local_authority_without_db(monkeypatch):
    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    with pytest.raises(DispatchError, match="requires --db"):
        resolve_dispatch(_ns(local_authority=True))


def test_resolve_dispatch_errors_when_nothing_configured(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
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


def test_remote_dispatch_review_experiment_surface_hits_hub_authority():
    from mac.http_client import HubClient

    fake = _FakeTransport(
        response_for={
            ("POST", "/tasks/task_1/review-experiment"): {"arm": "blind"},
            ("GET", "/tasks/task_1/review-observation"): {"task_id": "task_1"},
            ("POST", "/tasks/task_1/review-outcomes"): {"status": "confirmed"},
            ("GET", "/review-experiments/exp_1"): {
                "experiment_id": "exp_1"
            },
        }
    )
    dispatch = RemoteDispatch(
        HubClient("http://hub:8789", token="tok", transport=fake)
    )

    assert dispatch.assign_review_experiment(
        "task_1", experiment_id="exp_1", arm="blind", blind=True
    ).to_dict() == {"arm": "blind"}
    assert dispatch.review_observation("task_1").to_dict() == {"task_id": "task_1"}
    assert dispatch.record_review_outcome(
        "task_1", kind="clean_window", status="confirmed", severity_weight=0
    ).to_dict() == {"status": "confirmed"}
    assert dispatch.review_experiment_report(
        "exp_1", project="demo"
    ).to_dict() == {"experiment_id": "exp_1"}

    method, url, body, token = fake.calls[0]
    assert (method, url, token) == (
        "POST",
        "http://hub:8789/tasks/task_1/review-experiment",
        "tok",
    )
    assert body["blind"] is True
    assert "project=demo" in fake.calls[-1][1]


def test_remote_dispatch_fleet_snapshot_reads_hub_authority():
    from mac.http_client import HubClient

    fake = _FakeTransport(
        response_for={
            ("GET", "/fleet/snapshot"): {
                "schema": "mac.fleet_snapshot.v1",
                "members": [{"agent_id": "agent_other"}],
            }
        }
    )
    dispatch = RemoteDispatch(
        HubClient("http://hub:8789", token="tok", transport=fake)
    )

    snapshot = dispatch.fleet_snapshot(exclude_agent_id="agent_self", limit=12)

    assert snapshot.to_dict()["members"] == [{"agent_id": "agent_other"}]
    method, url, _body, _token = fake.calls[0]
    assert method == "GET"
    assert "/fleet/snapshot" in url
    assert "exclude_agent_id=agent_self" in url
    assert "limit=12" in url


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


def test_remote_dispatch_project_repository_wrappers_hit_hub_paths():
    from mac.http_client import HubClient

    fake = _FakeTransport(
        response_for={
            ("POST", "/bridge/repositories"): {"id": "projectrepo_1"},
            ("GET", "/bridge/repositories"): [{"id": "projectrepo_1"}],
        }
    )
    disp = RemoteDispatch(HubClient("http://hub:8789", token="tok", transport=fake))

    registered = disp.register_project_repository(
        "mac",
        "/srv/mac",
        source="repo-mac",
        project="mac",
        required_capabilities=["python"],
        poll_interval_seconds=30,
        metadata={"team": "core"},
        actor="operator",
    )
    listed = disp.list_project_repositories(enabled=True)

    assert registered.to_dict() == {"id": "projectrepo_1"}
    assert [item.to_dict() for item in listed] == [{"id": "projectrepo_1"}]
    assert fake.calls[0] == (
        "POST",
        "http://hub:8789/bridge/repositories",
        {
            "name": "mac",
            "path": "/srv/mac",
            "source": "repo-mac",
            "project": "mac",
            "required_capabilities": ["python"],
            "enabled": True,
            "poll_interval_seconds": 30,
            "metadata": {"team": "core"},
            "actor": "operator",
        },
        "tok",
    )
    assert fake.calls[1][0] == "GET"
    assert fake.calls[1][1] == "http://hub:8789/bridge/repositories?enabled=true"


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


def test_remote_dispatch_agent_reflect_hits_hub_endpoint():
    from mac.http_client import HubClient

    fake = _FakeTransport(
        response_for={
            ("POST", "/agents/agent_reflect/reflect"): {
                "schema": "mac.agentbus.agent_reflection_publish.v1",
                "count": 1,
            }
        }
    )
    disp = RemoteDispatch(HubClient("http://hub:8789", token="tok", transport=fake))

    result = disp.publish_agent_reflection(
        "agent_reflect",
        recipient_agent_id="agent_operator",
        request_id="rid-42",
    )

    assert result.to_dict() == {
        "schema": "mac.agentbus.agent_reflection_publish.v1",
        "count": 1,
    }
    method, url, payload, token = fake.calls[0]
    assert method == "POST"
    assert url == "http://hub:8789/agents/agent_reflect/reflect"
    assert token == "tok"
    assert payload == {
        "recipient_agent_id": "agent_operator",
        "request_id": "rid-42",
    }


def test_remote_dispatch_create_task_via_cli(monkeypatch):
    """End-to-end: `mac --hub-url ... task create` posts to /tasks."""
    import io
    import json as _json
    import sys

    from mac.cli import main
    from mac.http_client import HubClient

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
    # Verify creation and authoritative lane projection both use the hub,
    # never local SQLite. Mixed-version hubs may return 404 for the second
    # call; the CLI intentionally keeps the successful create result.
    assert len(fake.calls) == 2
    method, url, payload, token = fake.calls[0]
    assert method == "POST"
    assert url == "http://hub.example:8789/tasks"
    assert payload["title"] == "Stop the runaway loop"
    assert payload["project"] == "mac"
    assert payload["actor"] == "jordanh"
    assert token == "tok"
    route_method, route_url, route_payload, route_token = fake.calls[1]
    assert route_method == "GET"
    assert route_url == (
        "http://hub.example:8789/tasks/task_remote_1/publication-route"
    )
    assert route_payload is None
    assert route_token == "tok"


def test_remote_observability_prune_via_cli_uses_hub(monkeypatch):
    """The documented CLI path must prune through the hub, never local SQLite."""
    import json as _json

    from mac.cli import main
    from mac.http_client import HubClient

    monkeypatch.delenv("MAC_API_URL", raising=False)
    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")

    fake = _FakeTransport(
        response_for={("POST", "/observability/prune"): {"removed": 7}}
    )
    original_init = HubClient.__init__
    monkeypatch.setattr(
        HubClient,
        "__init__",
        lambda self, base_url, *, token=None, transport=None: original_init(
            self, base_url, token=token, transport=fake
        ),
    )

    output = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = output
    try:
        rc = main(
            [
                "--hub-url",
                "http://hub.example:8789",
                "--token",
                "tok",
                "observability",
                "prune",
                "--keep-last",
                "100",
            ]
        )
    finally:
        sys.stdout = old_stdout

    assert rc == 0
    assert _json.loads(output.getvalue()) == {"removed": 7}
    assert fake.calls == [
        (
            "POST",
            "http://hub.example:8789/observability/prune",
            {"keep_last": 100},
            "tok",
        )
    ]


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
    """`task claim` sends the API's query contract and reads its response."""
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
    method, url, request_body, _token = fake.calls[0]
    from urllib.parse import parse_qs, urlsplit

    assert method == "POST"
    assert urlsplit(url).path == "/tasks/task_xyz/claim"
    assert parse_qs(urlsplit(url).query) == {"agent_id": ["agent_natasha"]}
    assert request_body == {}


def test_remote_cli_task_claim_matches_live_api_and_persists_lease(monkeypatch, tmp_path):
    """Exercise the documented CLI through RemoteDispatch and the real API."""
    import json as _json
    from urllib.parse import urlsplit

    from fastapi.testclient import TestClient

    # mac.api exposes a production-style module app at import time; give that
    # unrelated app an isolated authority before importing the test factory.
    monkeypatch.setenv("MAC_DB", str(tmp_path / "module-app.db"))
    from mac.api import create_app
    from mac.cli import main
    from mac.http_client import HubClient
    from mac.services import ControlPlane

    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    plane = ControlPlane.in_memory()
    machine = plane.register_machine("claim-host")
    agent = plane.register_agent(machine.id, "claim-worker", capabilities=["python"])
    task = plane.create_task("claim through remote CLI", required_capabilities=["python"])
    api = TestClient(create_app(control_plane=plane))

    def api_transport(method, url, body, _token):
        parts = urlsplit(url)
        target = parts.path + (("?" + parts.query) if parts.query else "")
        response = api.request(method, target, json=body)
        assert response.status_code == 200, response.text
        return response.json()

    original_init = HubClient.__init__
    monkeypatch.setattr(
        HubClient,
        "__init__",
        lambda self, base_url, *, token=None, transport=None: original_init(
            self, base_url, token=token, transport=api_transport
        ),
    )

    output = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = output
    try:
        rc = main(
            [
                "--hub-url",
                "http://hub.example:8789",
                "task",
                "claim",
                task.id,
                agent.id,
            ]
        )
    finally:
        sys.stdout = old_stdout

    assert rc == 0
    result = _json.loads(output.getvalue())
    assert result["task"]["state"] == "claimed"
    assert result["task"]["owner_agent_id"] == agent.id
    lease = plane.get_lease(result["lease_id"])
    assert lease.task_id == task.id
    assert lease.agent_id == agent.id


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
    assert "DIRECT SQLite authority" in captured.err
    assert "does not synchronize tasks" in captured.err
    assert str(db_path) in captured.err
