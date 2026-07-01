"""Failure-boundary coverage for Kubernetes fleet bootstrap."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from mac.k8s import bootstrap
from mac.k8s.config_loader import NotifierChannelConfig


class _Mac:
    def __init__(self):
        self.posts = []
        self.puts = []
        self.get_value = {}
        self.post_value = {}

    def get(self, path):
        if isinstance(self.get_value, BaseException):
            raise self.get_value
        return self.get_value

    def post(self, path, body):
        self.posts.append((path, body))
        if isinstance(self.post_value, BaseException):
            raise self.post_value
        return self.post_value

    def put(self, path, body):
        self.puts.append((path, body))
        return {}


def _cfg(**extra):
    values = {
        "mac_url": "http://mac",
        "dispatcher": {
            "machine": {"hostname": "dispatcher", "machine_id": "machine"},
            "agent": {"name": "dispatcher", "agent_id": "agent"},
        },
    }
    values.update(extra)
    return bootstrap.BootstrapConfig(**values)


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda mac: bootstrap._post_machine(mac, {}), "hostname and machine_id"),
        (lambda mac: bootstrap._post_agent(mac, {}, machine_db_id="m"), "name and agent_id"),
    ],
)
def test_post_seed_validates_required_fields(call, message) -> None:
    with pytest.raises(SystemExit, match=message):
        call(_Mac())


def test_post_seed_rejects_non_object_responses() -> None:
    mac = _Mac()
    mac.post_value = []
    with pytest.raises(SystemExit, match="POST /machines returned non-object"):
        bootstrap._post_machine(mac, {"hostname": "host", "machine_id": "m"})
    with pytest.raises(SystemExit, match="POST /agents returned non-object"):
        bootstrap._post_agent(mac, {"name": "agent", "agent_id": "a"}, machine_db_id="m")


def test_dispatcher_and_role_seed_response_validation() -> None:
    mac = _Mac()
    mac.post_value = {}
    with pytest.raises(SystemExit, match="response missing id"):
        bootstrap.register_dispatcher(mac, _cfg())

    with pytest.raises(SystemExit, match="requires machine_id"):
        bootstrap.seed_role_machines_and_agents(mac, _cfg(role_machines=[{}]))
    with pytest.raises(SystemExit, match="missing response id"):
        bootstrap.seed_role_machines_and_agents(
            mac, _cfg(role_machines=[{"hostname": "h", "machine_id": "seed"}])
        )
    with pytest.raises(SystemExit, match="requires machine_id ref"):
        bootstrap.seed_role_machines_and_agents(mac, _cfg(role_agents=[{}]))
    with pytest.raises(SystemExit, match="unknown machine_id"):
        bootstrap.seed_role_machines_and_agents(
            mac, _cfg(role_agents=[{"name": "a", "agent_id": "a", "machine_id": "missing"}])
        )


def test_role_project_and_metadata_reconcile_failures(monkeypatch) -> None:
    mac = _Mac()
    mac.post_value = []
    with pytest.raises(SystemExit, match="POST /roles returned non-object"):
        bootstrap.register_role_definitions(mac, _cfg(role_definitions=[{"slug": "one"}]))
    with pytest.raises(SystemExit, match="projects entry requires name"):
        bootstrap.register_projects(mac, _cfg(projects=[{}]))
    with pytest.raises(SystemExit, match="POST /projects returned non-object"):
        bootstrap.register_projects(mac, _cfg(projects=[{"name": "one"}]))

    mac.get_value = RuntimeError("offline")
    bootstrap._reconcile_project_metadata(mac, "one", {"x": 1})
    mac.get_value = {"record": {"metadata": {"x": 1}}}
    bootstrap._reconcile_project_metadata(mac, "one", {"x": 1})
    assert mac.puts == []
    mac.get_value = {"project": {"metadata": {"old": True}}}
    mac.put = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("write failed"))
    bootstrap._reconcile_project_metadata(mac, "one", {"x": 1})


def test_notifier_channel_paths() -> None:
    mac = _Mac()
    bootstrap.register_notifier_channels(mac, _cfg())
    channel = NotifierChannelConfig("alerts", "webhook", ["task.*"], {"url": "x"})
    mac.post_value = RuntimeError("offline")
    with pytest.raises(SystemExit, match="failed to register"):
        bootstrap.register_notifier_channels(mac, _cfg(notifier_channels=[channel]))
    mac.post_value = []
    with pytest.raises(SystemExit, match="returned non-object"):
        bootstrap.register_notifier_channels(mac, _cfg(notifier_channels=[channel]))
    mac.post_value = {"id": "channel"}
    bootstrap.register_notifier_channels(mac, _cfg(notifier_channels=[channel]))
    assert mac.posts[-1][1]["event_types"] == ["task.*"]


def test_existing_secret_data_dict_and_non_404_failure() -> None:
    class Core:
        def read_namespaced_secret(self, *_a):
            return {"data": {"one": "MQ=="}}

    assert bootstrap._existing_secret_data(Core(), "ns", "secret")[0] == {"one": "MQ=="}

    class Bad:
        def read_namespaced_secret(self, *_a):
            exc = RuntimeError("forbidden")
            exc.status = 403
            raise exc

    with pytest.raises(SystemExit, match="reading Secret"):
        bootstrap._existing_secret_data(Bad(), "ns", "secret")


def test_rotate_configuration_and_write_failures() -> None:
    mac = _Mac()
    bootstrap.rotate_attestation_keys(mac, _cfg())
    with pytest.raises(SystemExit, match="requires a CoreV1"):
        bootstrap.rotate_attestation_keys(mac, _cfg(attestation_keys={}))
    core = SimpleNamespace(read_namespaced_secret=lambda *_a: (_ for _ in ()).throw(SimpleNamespace(status=404)))
    with pytest.raises(SystemExit, match="namespace and secret_name"):
        bootstrap.rotate_attestation_keys(mac, _cfg(attestation_keys={}), core)
    with pytest.raises(SystemExit, match="non-empty"):
        bootstrap.rotate_attestation_keys(
            mac, _cfg(attestation_keys={"namespace": "ns", "secret_name": "s", "roles": []}), core
        )

    class MissingCore:
        def read_namespaced_secret(self, *_a):
            exc = RuntimeError("missing")
            exc.status = 404
            raise exc

        def create_namespaced_secret(self, *_a):
            raise RuntimeError("write failed")

    mac.post_value = []
    spec = {"namespace": "ns", "secret_name": "s", "roles": {"role": "agent"}}
    with pytest.raises(SystemExit, match="not an object"):
        bootstrap.rotate_attestation_keys(mac, _cfg(attestation_keys=spec), MissingCore())
    mac.post_value = {"attestation_key": "key"}
    with pytest.raises(SystemExit, match="writing Secret"):
        bootstrap.rotate_attestation_keys(
            mac, _cfg(attestation_keys=spec), MissingCore(),
            secret_factory=lambda ns, name, data: {"data": data},
        )


def test_build_secret_factory_and_token_precedence(monkeypatch) -> None:
    assert bootstrap._build_secret_body(
        "ns", "name", {"one": "MQ=="}, factory=lambda *args: args
    ) == ("ns", "name", {"one": "MQ=="})
    monkeypatch.delenv("MAC_WORKER_TOKEN", raising=False)
    monkeypatch.delenv("MAC_API_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="is required"):
        bootstrap._token_from_env()
    monkeypatch.setenv("MAC_API_TOKEN", "api")
    assert bootstrap._token_from_env() == "api"
    monkeypatch.setenv("MAC_WORKER_TOKEN", "worker")
    assert bootstrap._token_from_env() == "worker"


def test_main_runs_full_pipeline_with_and_without_secret_rotation(monkeypatch) -> None:
    calls = []
    cfg = _cfg()
    monkeypatch.setattr(bootstrap.BootstrapConfig, "from_env", classmethod(lambda cls: cfg))
    monkeypatch.setattr(bootstrap, "_token_from_env", lambda: "token")
    for name in (
        "wait_for_mac_api", "register_dispatcher", "register_role_definitions",
        "seed_role_machines_and_agents", "register_projects", "register_fleet",
        "register_notifier_channels",
    ):
        monkeypatch.setattr(bootstrap, name, lambda *_a, _name=name: calls.append(_name))

    class Client:
        def __init__(self, url, token):
            calls.append((url, token))

    import mac.hermes_adapter as adapter
    import mac.k8s.k8s_client as k8s_client

    monkeypatch.setattr(adapter, "MacApiClient", Client)
    monkeypatch.setattr(k8s_client, "load_in_cluster_config", lambda: calls.append("load"))
    kubernetes = ModuleType("kubernetes")
    kubernetes.client = SimpleNamespace(CoreV1Api=lambda: "core")
    monkeypatch.setitem(sys.modules, "kubernetes", kubernetes)
    assert bootstrap.main([]) == 0
    assert "register_dispatcher" in calls and "load" not in calls

    cfg.attestation_keys = {"configured": True}
    monkeypatch.setattr(bootstrap, "rotate_attestation_keys", lambda *a: calls.append(("rotate", a[-1])))
    assert bootstrap.main([]) == 0
    assert "load" in calls and ("rotate", "core") in calls
