"""Focused contracts for infrastructure adapters and executable shims."""

from __future__ import annotations

import io
import json
import runpy
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest


class _Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False


def test_hub_client_urllib_transport_success_and_errors(monkeypatch):
    from mac.http_client import HubClient, HubClientError

    seen = []

    class Response:
        def __init__(self, payload: bytes):
            self.payload = payload

        def read(self):
            return self.payload

    def open_ok(request, timeout):
        seen.append((request, timeout))
        return _Context(Response(b'{"ok": true}'))

    monkeypatch.setattr("mac.http_client.urllib.request.urlopen", open_ok)
    client = HubClient("https://hub.example/", token="token")
    assert client.request("POST", "/tasks", {"title": "test"}) == {"ok": True}
    request, timeout = seen[-1]
    assert timeout == 30
    assert request.full_url == "https://hub.example/tasks"
    assert request.headers["Authorization"] == "Bearer token"
    assert json.loads(request.data) == {"title": "test"}

    monkeypatch.setattr(
        "mac.http_client.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Context(Response(b"")),
    )
    assert HubClient("https://hub.example").request("GET", "/health") is None

    def http_error(*_args, **_kwargs):
        raise HTTPError(
            "https://hub.example/tasks",
            403,
            "Forbidden",
            {},
            io.BytesIO(b"scope denied"),
        )

    monkeypatch.setattr("mac.http_client.urllib.request.urlopen", http_error)
    with pytest.raises(HubClientError, match="scope denied"):
        client.request("GET", "/tasks")

    def url_error(*_args, **_kwargs):
        raise URLError("offline")

    monkeypatch.setattr("mac.http_client.urllib.request.urlopen", url_error)
    with pytest.raises(HubClientError, match="offline"):
        client.request("GET", "/tasks")


def test_k8s_clients_cover_normal_terminal_and_error_paths(monkeypatch):
    from kubernetes.client.rest import ApiException
    from mac.k8s import k8s_client as module

    class Record:
        def __init__(self, payload):
            self.payload = payload

        def to_dict(self):
            return self.payload

    class Batch:
        def __init__(self):
            self.deleted = []
            self.delete_error = None
            self.read_error = None

        def create_namespaced_job(self, **kwargs):
            return Record({"metadata": {"name": kwargs["body"]["name"]}, "none": None})

        def list_namespaced_job(self, **_kwargs):
            return SimpleNamespace(
                items=[
                    Record({"metadata": {"name": "active"}, "status": {}}),
                    Record(
                        {
                            "metadata": {"name": "done"},
                            "status": {"conditions": [{"type": "Complete", "status": "True"}]},
                        }
                    ),
                ]
            )

        def delete_namespaced_job(self, **kwargs):
            if self.delete_error:
                raise self.delete_error
            self.deleted.append(kwargs)

        def read_namespaced_job(self, **_kwargs):
            if self.read_error:
                raise self.read_error
            return {"metadata": {"name": "job"}, "none": None}

    class Apps:
        def __init__(self):
            self.error = None
            self.scaled = None

        def read_namespaced_deployment(self, **_kwargs):
            if self.error:
                raise self.error
            return Record({"metadata": {"name": "api"}})

        def patch_namespaced_deployment_scale(self, **kwargs):
            self.scaled = kwargs

    batch = Batch()
    apps = Apps()
    monkeypatch.setattr(module.k8s_client, "BatchV1Api", lambda: batch)
    monkeypatch.setattr(module.k8s_client, "AppsV1Api", lambda: apps)

    jobs = module.K8sJobsClient()
    assert jobs.create("ns", {"name": "job"}) == {"metadata": {"name": "job"}}
    assert [item["metadata"]["name"] for item in jobs.list_active("ns", "app=mac")] == ["active"]
    jobs.delete("ns", "job")
    assert jobs.read("ns", "job") == {"metadata": {"name": "job"}}
    batch.delete_error = ApiException(status=404)
    jobs.delete("ns", "gone")
    batch.delete_error = ApiException(status=500)
    with pytest.raises(ApiException):
        jobs.delete("ns", "broken")
    batch.read_error = ApiException(status=404)
    assert jobs.read("ns", "gone") == {}
    batch.read_error = ApiException(status=500)
    with pytest.raises(ApiException):
        jobs.read("ns", "broken")

    deployments = module.K8sDeploymentsClient()
    assert deployments.get_deployment("ns", "api") == {"metadata": {"name": "api"}}
    deployments.scale_deployment("ns", "api", 3)
    assert apps.scaled["body"] == {"spec": {"replicas": 3}}
    apps.error = ApiException(status=404)
    assert deployments.get_deployment("ns", "gone") is None
    apps.error = ApiException(status=500)
    with pytest.raises(ApiException):
        deployments.get_deployment("ns", "broken")

    assert module._to_dict("raw") == {"value": "raw"}
    assert module._strip_none([{"keep": 1, "drop": None}]) == [{"keep": 1}]


def test_k8s_config_falls_back_to_kubeconfig(monkeypatch):
    from mac.k8s import k8s_client as module

    calls = []

    def fail_in_cluster():
        calls.append("in-cluster")
        raise module.k8s_config.ConfigException("not in a pod")

    monkeypatch.setattr(module.k8s_config, "load_incluster_config", fail_in_cluster)
    monkeypatch.setattr(module.k8s_config, "load_kube_config", lambda: calls.append("kube"))
    module.load_in_cluster_config()
    assert calls == ["in-cluster", "kube"]


def test_openshell_supervisor_contracts(tmp_path, monkeypatch, capsys):
    from mac import openshell_supervisor as module

    # main() exports this for its child process via os.environ directly. Register
    # the key with monkeypatch first so the in-process CLI test cannot leak the
    # sandbox setting into later executor tests.
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "0")
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "1")
    monkeypatch.setenv("PASSTHROUGH", "value")
    argv = module.build_supervisor_argv(
        agent_id="agent_rocky",
        policy_path="/policy.yaml",
        child_argv=["child", "--flag"],
        openshell_bin="openshell-bin",
        keep=False,
        env_passthrough=["PASSTHROUGH", "MISSING"],
        extra_create_args=["--debug"],
    )
    assert argv[:3] == ["openshell-bin", "sandbox", "create"]
    assert "--keep" not in argv
    assert "PASSTHROUGH=value" in argv
    assert argv[-3:] == ["--", "child", "--flag"]
    for kwargs in (
        {"agent_id": "", "policy_path": "x", "child_argv": ["x"]},
        {"agent_id": "a", "policy_path": "", "child_argv": ["x"]},
        {"agent_id": "a", "policy_path": "x", "child_argv": []},
    ):
        with pytest.raises(ValueError):
            module.build_supervisor_argv(**kwargs)

    monkeypatch.setenv("MAC_OPENSHELL_POLICY", "~/policy.yaml")
    assert module.default_policy_path("agent").name == "policy.yaml"
    monkeypatch.delenv("MAC_OPENSHELL_POLICY", raising=False)
    default_policy = module.default_policy_path("agent_rocky")
    assert default_policy.name == "rocky-policy.yaml"
    assert default_policy.parent.name == "openshell"
    monkeypatch.setenv("MAC_OPENSHELL_CHILD", "python -m worker")
    assert module.default_child_argv() == ["python", "-m", "worker"]
    monkeypatch.delenv("MAC_OPENSHELL_CHILD")
    assert module.default_child_argv() == ["mac-hermes-gateway"]

    monkeypatch.setattr(module, "openshell_required_for_identity", lambda **_kw: True)
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    assert module.main(["--agent-id", "agent", "--policy", str(tmp_path / "missing")]) == 78
    assert "not found" in capsys.readouterr().err

    openshell = tmp_path / "openshell"
    openshell.write_text("", encoding="utf-8")
    monkeypatch.setattr(module.shutil, "which", lambda _name: str(openshell))
    assert module.main(["--agent-id", "agent", "--policy", str(tmp_path / "missing")]) == 78
    assert "policy" in capsys.readouterr().err

    monkeypatch.setattr(module, "openshell_required_for_identity", lambda **_kw: False)
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(module.subprocess, "call", lambda command: 9 if command == ["child"] else 7)
    assert module.main(["--agent-id", "agent", "--allow-unsandboxed", "--", "child"]) == 9
    assert module.main(["--agent-id", "agent", "--", "child"]) == 78

    policy = tmp_path / "policy.yaml"
    policy.write_text("filesystem: {}\n", encoding="utf-8")
    monkeypatch.setattr(module.shutil, "which", lambda _name: str(openshell))
    seen = []
    monkeypatch.setattr(module.subprocess, "call", lambda command: seen.append(command) or 0)
    assert module.main(["--agent-id", "agent", "--policy", str(policy), "--", "child"]) == 0
    assert seen[-1][0] == str(openshell)
    assert module.os.environ["MAC_OPENSHELL_SANDBOX"] == "1"


def test_openshell_collector_file_post_and_modes(tmp_path, monkeypatch, capsys):
    from mac import openshell_collector as module

    events = tmp_path / "events.jsonl"
    events.write_text('\n{"outcome":"allowed","name":"read"}\n', encoding="utf-8")
    assert list(module.iter_json_lines(events))[0]["name"] == "read"

    posted = []
    monkeypatch.setattr(
        module, "post_action_event", lambda *args, **kwargs: posted.append((args, kwargs))
    )
    assert (
        module.collect_once(
            [{"outcome": "denied", "name": "write"}],
            base_url="https://hub",
            token="token",
            agent_id="agent",
            sandbox_id="sandbox",
        )
        == 1
    )
    assert posted[-1][0][2]["outcome"] == "denied"

    assert module.main(["--events-file", str(events)]) == 2
    assert "required" in capsys.readouterr().err
    assert (
        module.main(["--events-file", str(events), "--hub-url", "https://hub", "--token", "token"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["posted"] == 1

    class StopFollowing(Exception):
        pass

    monkeypatch.setattr(
        module.time, "sleep", lambda _seconds: (_ for _ in ()).throw(StopFollowing())
    )
    with pytest.raises(StopFollowing):
        module.main(
            [
                "--events-file",
                str(events),
                "--hub-url",
                "https://hub",
                "--token",
                "token",
                "--follow",
                "--interval",
                "0",
            ]
        )


def test_openshell_collector_http_request(monkeypatch):
    from mac import openshell_collector as module

    seen = []

    class Response:
        def read(self):
            return b"{}"

    def fake_open(request, timeout):
        seen.append((request, timeout))
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_open)
    module.post_action_event("https://hub/", "token", {"outcome": "allowed"}, timeout=2)
    request, timeout = seen[-1]
    assert request.full_url == "https://hub/action-events"
    assert request.headers["Authorization"] == "Bearer token"
    assert timeout == 2


class _PgCursor:
    def __init__(self, *, rows=(), columns=(), rowcount=0, error=None):
        self.rows = list(rows)
        self.description = [SimpleNamespace(name=name) for name in columns] or None
        self.rowcount = rowcount
        self.error = error
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        if self.error:
            raise self.error
        self.executed.append((sql, params))

    def executemany(self, sql, params):
        if self.error:
            raise self.error
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self.rows)


class _PgConnection:
    def __init__(self, cursor):
        self.value = cursor

    def cursor(self):
        return self.value

    def transaction(self):
        return _Context(self)


class _PgPool:
    def __init__(self, cursor):
        self.conn = _PgConnection(cursor)
        self.closed = False

    def connection(self):
        return _Context(self.conn)

    def close(self):
        self.closed = True


def test_postgres_store_adapter_with_fake_pool(monkeypatch):
    from mac import store_postgres as module
    from mac.store import StoreError

    cursor = _PgCursor(rows=[(1, "one"), (2, "two")], columns=["id", "name"], rowcount=2)
    pool = _PgPool(cursor)
    monkeypatch.setattr(module, "ConnectionPool", lambda **_kwargs: pool)
    store = module.PostgresStore("postgresql://test", pool_size=2, min_size=0)
    result = store.execute("SELECT * FROM things WHERE id = ?", [1])
    first = result.fetchone()
    assert first["name"] == "one"
    assert first[0] == 1
    assert first.get("missing", "fallback") == "fallback"
    assert "id" in first
    assert first.keys() == ["id", "name"]
    assert len(first) == 2
    assert dict(first) == {"id": 1, "name": "one"}
    assert [row["id"] for row in result] == [2]
    assert result.fetchall() == []

    cursor.rows = [(3, "three")]
    assert store.query_one("SELECT ?", [3])["id"] == 3
    cursor.rows = [(4, "four")]
    assert store.query_all("SELECT ?", [4])[0]["name"] == "four"
    cursor.description = None
    cursor.rowcount = 0
    assert store.execute("UPDATE things SET name = ?", ["new"]).rowcount == 0
    store.executemany("INSERT INTO things VALUES (?, ?)", [(1, "a"), (2, "b")])
    store.ensure_column("things", "extra", "extra TEXT")
    assert callable(store.initialize)
    assert callable(store.verify_schema)
    with store.transaction() as transaction:
        transaction.execute("UPDATE things SET name = ?", ["inside"])
    store.close()
    assert pool.closed

    error_cursor = _PgCursor(error=module.psycopg.Error("database down"))
    error_pool = _PgPool(error_cursor)
    store._pool = error_pool
    for operation in (
        lambda: store.execute("SELECT 1"),
        lambda: store.executemany("INSERT INTO t VALUES (?)", [(1,)]),
        lambda: store.ensure_column("t", "c", "c TEXT"),
        store.verify_schema,
    ):
        with pytest.raises(StoreError, match="database down"):
            operation()
    with pytest.raises(StoreError, match="database down"):
        with store.transaction() as transaction:
            transaction.execute("SELECT 1")


@pytest.mark.parametrize(
    "command,separator",
    [
        ("scrub-regex", "|"),
        ("scrub-vars", " "),
        ("provider-key-envs", " "),
    ],
)
def test_provider_registry_module_cli(command, separator, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["mac.providers", command])
    runpy.run_module("mac.providers", run_name="__main__")
    assert separator in capsys.readouterr().out


def test_provider_registry_module_cli_rejects_unknown(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["mac.providers", "unknown"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("mac.providers", run_name="__main__")
    assert excinfo.value.code == 2
    assert "unknown" in capsys.readouterr().err
