from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from mac.k8s.bootstrap import (
    BootstrapConfig,
    _slot_already_populated,
    register_dispatcher,
    register_fleet,
    register_projects,
    rotate_attestation_keys,
    seed_role_machines_and_agents,
    wait_for_mac_api,
)

# imports relocated from test_k8s_bootstrap_edges.py
import sys
from types import ModuleType, SimpleNamespace
from mac.k8s import bootstrap
from mac.k8s.config_loader import NotifierChannelConfig


class _FakeMac:
    def __init__(
        self,
        responses: Optional[Dict[str, Any]] = None,
        *,
        health_errors: int = 0,
    ) -> None:
        self.responses = responses or {}
        self.posted: List[Dict[str, Any]] = []
        self.gotten: List[str] = []
        self._health_errors_remaining = health_errors

    def get(self, path: str) -> Dict[str, Any]:
        self.gotten.append(path)
        if path == "/health" and self._health_errors_remaining > 0:
            self._health_errors_remaining -= 1
            raise RuntimeError("health probe error (fake)")
        return self._resolve("GET " + path, None)

    def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        self.posted.append({"path": path, "body": body})
        return self._resolve("POST " + path, body)

    def _resolve(self, key: str, body: Any) -> Dict[str, Any]:
        if key in self.responses:
            v = self.responses[key]
            if callable(v):
                return v(body)
            return v
        if key.startswith("POST /machines"):
            mid = (body or {}).get("machine_id") or "m-unknown"
            return {
                "id": "db-" + mid,
                "hostname": (body or {}).get("hostname", ""),
                "machine_id": mid,
            }
        if key.startswith("POST /agents"):
            aid = (body or {}).get("agent_id") or "a-unknown"
            return {
                "id": "db-" + aid,
                "agent_id": aid,
                "name": (body or {}).get("name", ""),
                "capabilities": list((body or {}).get("capabilities") or []),
            }
        return {}


class _NotFound(Exception):
    def __init__(self) -> None:
        super().__init__("not found")
        self.status = 404


class _MacApiNotFound(RuntimeError):
    """Mirrors the real mac.hermes_adapter.MacApiError on a 404: a plain
    RuntimeError whose message carries the 'not found' detail, with NO
    ``.status`` attribute. register_fleet must detect not-found from the
    message, like worker.py does."""

    def __init__(self, path: str) -> None:
        super().__init__('mac API GET %s failed: {"detail":"fleet not found: ai-k8s"}' % path)


class _FakeCore:
    def __init__(self, initial_data: Optional[Dict[str, str]] = None) -> None:
        self._data = initial_data
        self.read_calls: List[Dict[str, str]] = []
        self.created: List[Dict[str, Any]] = []
        self.patched: List[Dict[str, Any]] = []

    def read_namespaced_secret(self, name: str, namespace: str) -> Any:
        self.read_calls.append({"name": name, "namespace": namespace})
        if self._data is None:
            raise _NotFound()
        # Mimic V1Secret: ``.data`` attribute.
        return _Obj(data=dict(self._data))

    def create_namespaced_secret(self, namespace: str, body: Any) -> Any:
        self.created.append({"namespace": namespace, "body": body})
        return body

    def patch_namespaced_secret(self, name: str, namespace: str, body: Any) -> Any:
        self.patched.append({"name": name, "namespace": namespace, "body": body})
        return body


class _Obj:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


def _dict_factory(namespace: str, name: str, data: Dict[str, str]) -> Dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "data": dict(data),
    }


def _dispatcher_cfg() -> Dict[str, Any]:
    return {
        "machine": {
            "machine_id": "mac-runner",
            "hostname": "mac-runner.ai.svc.cluster.local",
            "labels": {"kind": "k8s-deployment", "namespace": "ai"},
        },
        "agent": {
            "agent_id": "mac-runner",
            "name": "mac-runner",
            "capabilities": ["ops", "python", "review", "hermes"],
        },
    }


def _role_machines() -> List[Dict[str, Any]]:
    return [
        {
            "machine_id": "mac-worker-machine",
            "hostname": "mac-worker.ai.svc.cluster.local",
            "labels": {"kind": "virtual", "owner": "mac-k8s-bootstrap"},
        }
    ]


def _roles_block() -> Dict[str, Any]:
    """Unified-schema ``roles`` map (one block per role)."""
    return {
        "python-coder": {
            "agent_id": "mac-worker-python-coder",
            "name": "mac-worker-python-coder",
            "machine_id": "mac-worker-machine",
            "capabilities": ["python", "ops"],
            "image": "ghcr.io/x/coder:latest",
            "executor": "/usr/local/bin/mac-task-executor-codex",
            "attestation_key_secret": {
                "name": "mac-agent-attestation-keys",
                "key": "python-coder",
            },
        },
        "python-reviewer": {
            "agent_id": "mac-worker-python-reviewer",
            "name": "mac-worker-python-reviewer",
            "machine_id": "mac-worker-machine",
            "capabilities": ["review", "python"],
            "image": "ghcr.io/x/reviewer:latest",
            "executor": "/usr/local/bin/mac-task-executor-codex-review",
            "attestation_key_secret": {
                "name": "mac-agent-attestation-keys",
                "key": "python-reviewer",
            },
        },
    }


def _role_agents() -> List[Dict[str, Any]]:
    return [
        {
            "agent_id": "mac-worker-python-coder",
            "name": "mac-worker-python-coder",
            "machine_id": "mac-worker-machine",
            "capabilities": ["python", "ops"],
        },
        {
            "agent_id": "mac-worker-python-reviewer",
            "name": "mac-worker-python-reviewer",
            "machine_id": "mac-worker-machine",
            "capabilities": ["review", "python"],
        },
    ]


def _attestation_keys() -> Dict[str, Any]:
    return {
        "namespace": "ai",
        "secret_name": "mac-agent-attestation-keys",
        "roles": {
            "python-coder": "mac-worker-python-coder",
            "python-reviewer": "mac-worker-python-reviewer",
        },
    }


def _config_yaml(**overrides: Any) -> Dict[str, Any]:
    """Build a full unified-schema YAML doc as a Python dict."""
    doc: Dict[str, Any] = {
        "mac_url": "http://mac-api.ai.svc.cluster.local:8000",
        "dispatcher": _dispatcher_cfg(),
        "role_machines": _role_machines(),
        "roles": _roles_block(),
        "capability_role_aliases": {
            "python": "python-coder",
            "review": "python-reviewer",
        },
        "attestation_keys": {
            "namespace": "ai",
            "secret_name": "mac-agent-attestation-keys",
        },
    }
    doc.update(overrides)
    return doc


def _write_yaml(tmp_path: Path, doc: Dict[str, Any]) -> Path:
    f = tmp_path / "config.yaml"
    f.write_text(yaml.safe_dump(doc))
    return f


def _full_cfg(**overrides: Any) -> BootstrapConfig:
    cfg = BootstrapConfig(
        mac_url="http://mac-api.ai.svc.cluster.local:8000",
        dispatcher=_dispatcher_cfg(),
        role_machines=_role_machines(),
        role_agents=_role_agents(),
        attestation_keys=_attestation_keys(),
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class TestBootstrapConfigFromFile:
    def test_happy_path(self, tmp_path: Path) -> None:
        f = _write_yaml(tmp_path, _config_yaml(mac_url="http://mac-api.ai.svc.cluster.local:8000/"))
        cfg = BootstrapConfig.from_file(str(f))
        # trailing slash stripped.
        assert cfg.mac_url == "http://mac-api.ai.svc.cluster.local:8000"
        assert cfg.dispatcher["machine"]["machine_id"] == "mac-runner"
        assert cfg.dispatcher["agent"]["agent_id"] == "mac-runner"
        assert [m["machine_id"] for m in cfg.role_machines] == ["mac-worker-machine"]
        # role_agents derived from the unified `roles` block.
        assert len(cfg.role_agents) == 2
        assert cfg.role_agents[0]["agent_id"] == "mac-worker-python-coder"
        assert cfg.role_agents[1]["agent_id"] == "mac-worker-python-reviewer"
        assert cfg.attestation_keys is not None
        assert cfg.attestation_keys["secret_name"] == "mac-agent-attestation-keys"
        assert cfg.attestation_keys["roles"] == {
            "python-coder": "mac-worker-python-coder",
            "python-reviewer": "mac-worker-python-reviewer",
        }

    def test_from_env_reads_mac_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = _write_yaml(tmp_path, _config_yaml())
        monkeypatch.setenv("MAC_CONFIG_FILE", str(f))
        cfg = BootstrapConfig.from_env()
        assert cfg.mac_url == "http://mac-api.ai.svc.cluster.local:8000"
        assert len(cfg.role_agents) == 2

    def test_attestation_keys_optional(self, tmp_path: Path) -> None:
        doc = _config_yaml()
        doc.pop("attestation_keys")
        f = _write_yaml(tmp_path, doc)
        cfg = BootstrapConfig.from_file(str(f))
        assert cfg.attestation_keys is None

    def test_attestation_keys_none_when_no_roles(self, tmp_path: Path) -> None:
        doc = _config_yaml()
        doc["roles"] = {}
        f = _write_yaml(tmp_path, doc)
        cfg = BootstrapConfig.from_file(str(f))
        assert cfg.attestation_keys is None

    def test_fleet_block_parsed(self, tmp_path: Path) -> None:
        doc = _config_yaml(
            fleet={
                "name": "ai-k8s",
                "description": "K8s-native MAC fleet",
                "status": "active",
            }
        )
        f = _write_yaml(tmp_path, doc)
        cfg = BootstrapConfig.from_file(str(f))
        assert cfg.fleet == {
            "name": "ai-k8s",
            "description": "K8s-native MAC fleet",
            "status": "active",
        }

    def test_fleet_block_defaults(self, tmp_path: Path) -> None:
        doc = _config_yaml(fleet={"name": "ai-k8s"})
        f = _write_yaml(tmp_path, doc)
        cfg = BootstrapConfig.from_file(str(f))
        assert cfg.fleet == {
            "name": "ai-k8s",
            "description": "",
            "status": "active",
        }

    def test_fleet_block_absent_is_none(self, tmp_path: Path) -> None:
        cfg = BootstrapConfig.from_file(str(_write_yaml(tmp_path, _config_yaml())))
        assert cfg.fleet is None

    def test_fleet_block_blank_name_raises(self, tmp_path: Path) -> None:
        doc = _config_yaml(fleet={"name": "  "})
        f = _write_yaml(tmp_path, doc)
        with pytest.raises(SystemExit, match="fleet.*name"):
            BootstrapConfig.from_file(str(f))

    def test_fleet_block_not_mapping_raises(self, tmp_path: Path) -> None:
        doc = _config_yaml(fleet=["ai-k8s"])
        f = _write_yaml(tmp_path, doc)
        with pytest.raises(SystemExit, match="fleet must be a mapping"):
            BootstrapConfig.from_file(str(f))

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="is missing"):
            BootstrapConfig.from_file(str(tmp_path / "does-not-exist.yaml"))

    def test_default_path_missing_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("MAC_CONFIG_FILE", raising=False)
        from mac.k8s import config_loader

        monkeypatch.setattr(config_loader, "DEFAULT_CONFIG_PATH", str(tmp_path / "nope.yaml"))
        with pytest.raises(SystemExit, match="is missing"):
            BootstrapConfig.from_env()

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "config.yaml"
        # Unbalanced bracket -> YAML parse error.
        f.write_text("mac_url: [unterminated\n")
        with pytest.raises(SystemExit, match="not valid YAML"):
            BootstrapConfig.from_file(str(f))

    def test_non_mapping_root_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "config.yaml"
        f.write_text("- a\n- b\n")
        with pytest.raises(SystemExit, match="must decode to a mapping"):
            BootstrapConfig.from_file(str(f))

    def test_missing_mac_url_raises(self, tmp_path: Path) -> None:
        doc = _config_yaml()
        doc.pop("mac_url")
        f = _write_yaml(tmp_path, doc)
        with pytest.raises(SystemExit, match="mac_url"):
            BootstrapConfig.from_file(str(f))

    def test_missing_dispatcher_raises(self, tmp_path: Path) -> None:
        doc = _config_yaml()
        doc.pop("dispatcher")
        f = _write_yaml(tmp_path, doc)
        with pytest.raises(SystemExit, match="dispatcher"):
            BootstrapConfig.from_file(str(f))

    def test_dispatcher_missing_inner_raises(self, tmp_path: Path) -> None:
        doc = _config_yaml()
        doc["dispatcher"] = {"machine": _dispatcher_cfg()["machine"]}
        f = _write_yaml(tmp_path, doc)
        with pytest.raises(SystemExit, match="machine, agent"):
            BootstrapConfig.from_file(str(f))

    def test_role_missing_required_field_raises(self, tmp_path: Path) -> None:
        # Drop `executor` from one role.
        doc = _config_yaml()
        doc["roles"]["python-coder"].pop("executor")
        f = _write_yaml(tmp_path, doc)
        with pytest.raises(SystemExit, match="roles.python-coder.*executor"):
            BootstrapConfig.from_file(str(f))

    def test_role_attestation_secret_missing_key_raises(self, tmp_path: Path) -> None:
        doc = _config_yaml()
        doc["roles"]["python-coder"]["attestation_key_secret"] = {"name": "n"}
        f = _write_yaml(tmp_path, doc)
        with pytest.raises(SystemExit, match="attestation_key_secret.*requires.*key"):
            BootstrapConfig.from_file(str(f))


class TestWaitForMacApi:
    def test_first_attempt_succeeds(self) -> None:
        mac = _FakeMac()
        slept: List[float] = []
        wait_for_mac_api(
            mac,
            attempts=5,
            delay_s=0.0,
            sleeper=lambda d: slept.append(d),
        )
        assert mac.gotten == ["/health"]
        assert slept == []

    def test_retries_then_succeeds(self) -> None:
        mac = _FakeMac(health_errors=3)
        slept: List[float] = []
        wait_for_mac_api(
            mac,
            attempts=10,
            delay_s=0.1,
            sleeper=lambda d: slept.append(d),
        )
        assert len(mac.gotten) == 4  # 3 failures then 1 success
        # We slept between attempts only (not after the success).
        assert slept == [0.1, 0.1, 0.1]

    def test_exhausts_attempts_raises(self) -> None:
        mac = _FakeMac(health_errors=999)
        slept: List[float] = []
        with pytest.raises(SystemExit, match="never became ready"):
            wait_for_mac_api(
                mac,
                attempts=4,
                delay_s=0.05,
                sleeper=lambda d: slept.append(d),
            )
        assert len(mac.gotten) == 4
        assert slept == [0.05, 0.05, 0.05]


class TestRegisterDispatcher:
    def test_posts_machine_and_agent(self) -> None:
        mac = _FakeMac()
        cfg = _full_cfg()
        register_dispatcher(mac, cfg)
        paths = [p["path"] for p in mac.posted]
        assert paths == ["/machines", "/agents"]
        machine_body = mac.posted[0]["body"]
        assert machine_body["machine_id"] == "mac-runner"
        assert machine_body["hostname"] == "mac-runner.ai.svc.cluster.local"
        assert machine_body["trusted"] is True
        agent_body = mac.posted[1]["body"]
        assert agent_body["machine_id"] == "db-mac-runner"
        assert agent_body["agent_id"] == "mac-runner"
        assert agent_body["capabilities"] == [
            "ops",
            "python",
            "review",
            "hermes",
        ]

    def test_machine_response_missing_id_raises(self) -> None:
        mac = _FakeMac(responses={"POST /machines": {"hostname": "x"}})
        with pytest.raises(SystemExit, match="missing id"):
            register_dispatcher(mac, _full_cfg())


class TestSeedRoleMachinesAndAgents:
    def test_posts_all_machines_and_agents(self) -> None:
        mac = _FakeMac()
        seed_role_machines_and_agents(mac, _full_cfg())
        paths = [p["path"] for p in mac.posted]
        assert paths == [
            "/machines",
            "/agents",
            "/agents",
        ]
        # The single machine is POSTed once with the seed-side id.
        assert mac.posted[0]["body"]["machine_id"] == "mac-worker-machine"
        # Both agents reference the resolved db-side machine id.
        assert mac.posted[1]["body"]["machine_id"] == "db-mac-worker-machine"
        assert mac.posted[2]["body"]["machine_id"] == "db-mac-worker-machine"
        # And carry their per-agent fields.
        assert mac.posted[1]["body"]["agent_id"] == "mac-worker-python-coder"
        assert mac.posted[2]["body"]["agent_id"] == "mac-worker-python-reviewer"

    def test_unknown_machine_ref_raises(self) -> None:
        cfg = _full_cfg(
            role_agents=[
                {
                    "agent_id": "x",
                    "name": "x",
                    "machine_id": "does-not-exist",
                    "capabilities": [],
                }
            ]
        )
        with pytest.raises(SystemExit, match="unknown machine_id"):
            seed_role_machines_and_agents(_FakeMac(), cfg)

    def test_resolves_multiple_machines_correctly(self) -> None:
        cfg = _full_cfg(
            role_machines=[
                {"machine_id": "m-a", "hostname": "a.svc", "labels": {}},
                {"machine_id": "m-b", "hostname": "b.svc", "labels": {}},
            ],
            role_agents=[
                {
                    "agent_id": "agent-a",
                    "name": "agent-a",
                    "machine_id": "m-a",
                    "capabilities": [],
                },
                {
                    "agent_id": "agent-b",
                    "name": "agent-b",
                    "machine_id": "m-b",
                    "capabilities": [],
                },
            ],
        )
        mac = _FakeMac()
        seed_role_machines_and_agents(mac, cfg)
        # agent-a should reference db-m-a; agent-b -> db-m-b.
        agent_posts = [p for p in mac.posted if p["path"] == "/agents"]
        assert agent_posts[0]["body"]["machine_id"] == "db-m-a"
        assert agent_posts[1]["body"]["machine_id"] == "db-m-b"


class TestRotateAttestationKeys:
    def test_skips_when_attestation_keys_none(self) -> None:
        cfg = _full_cfg(attestation_keys=None)
        mac = _FakeMac()
        # No core needed — must be a no-op.
        rotate_attestation_keys(mac, cfg, core=None)
        assert mac.posted == []

    def test_requires_core_when_configured(self) -> None:
        with pytest.raises(SystemExit, match="requires a CoreV1 client"):
            rotate_attestation_keys(_FakeMac(), _full_cfg(), core=None)

    def test_creates_secret_when_absent(self) -> None:
        mac = _FakeMac(
            responses={
                "POST /agents/mac-worker-python-coder/attestation-key/rotate": {
                    "attestation_key": "key-AAA"
                },
                "POST /agents/mac-worker-python-reviewer/attestation-key/rotate": {
                    "attestation_key": "key-BBB"
                },
            }
        )
        core = _FakeCore(initial_data=None)
        rotate_attestation_keys(mac, _full_cfg(), core, secret_factory=_dict_factory)
        # Both roles rotated.
        rotate_paths = [
            p["path"] for p in mac.posted if p["path"].endswith("/attestation-key/rotate")
        ]
        assert sorted(rotate_paths) == [
            "/agents/mac-worker-python-coder/attestation-key/rotate",
            "/agents/mac-worker-python-reviewer/attestation-key/rotate",
        ]
        # Secret was CREATED, not patched.
        assert len(core.created) == 1
        assert core.patched == []
        body = core.created[0]["body"]
        # data should hold both keys base64-encoded.
        assert set(body["data"].keys()) == {"python-coder", "python-reviewer"}
        assert base64.b64decode(body["data"]["python-coder"]).decode("utf-8") == "key-AAA"
        assert base64.b64decode(body["data"]["python-reviewer"]).decode("utf-8") == "key-BBB"

    def test_patches_existing_secret_when_partial(self) -> None:
        already = base64.b64encode(b"old-coder-key").decode("ascii")
        mac = _FakeMac(
            responses={
                "POST /agents/mac-worker-python-reviewer/attestation-key/rotate": {
                    "attestation_key": "key-NEW"
                }
            }
        )
        core = _FakeCore(initial_data={"python-coder": already})
        rotate_attestation_keys(mac, _full_cfg(), core, secret_factory=_dict_factory)
        # Only the reviewer was rotated.
        rotate_paths = [
            p["path"] for p in mac.posted if p["path"].endswith("/attestation-key/rotate")
        ]
        assert rotate_paths == ["/agents/mac-worker-python-reviewer/attestation-key/rotate"]
        # Secret was PATCHED, not created.
        assert core.created == []
        assert len(core.patched) == 1
        body = core.patched[0]["body"]
        assert body["data"]["python-coder"] == already
        assert base64.b64decode(body["data"]["python-reviewer"]).decode("utf-8") == "key-NEW"

    def test_no_op_when_all_populated(self) -> None:
        coder = base64.b64encode(b"existing-coder").decode("ascii")
        reviewer = base64.b64encode(b"existing-reviewer").decode("ascii")
        mac = _FakeMac()
        core = _FakeCore(initial_data={"python-coder": coder, "python-reviewer": reviewer})
        rotate_attestation_keys(mac, _full_cfg(), core, secret_factory=_dict_factory)
        # No POSTs.
        assert mac.posted == []
        # Idempotent: neither create nor patch.
        assert core.created == []
        assert core.patched == []

    def test_rotate_response_missing_key_raises(self) -> None:
        mac = _FakeMac(
            responses={"POST /agents/mac-worker-python-coder/attestation-key/rotate": {"ok": True}}
        )
        core = _FakeCore(initial_data=None)
        with pytest.raises(SystemExit, match="missing attestation_key"):
            rotate_attestation_keys(mac, _full_cfg(), core, secret_factory=_dict_factory)

    def test_secret_read_error_raises_systemexit(self) -> None:
        class _Boom:
            status = 500

            def __str__(self) -> str:
                return "explode"

        class _BadCore(_FakeCore):
            def read_namespaced_secret(self, name: str, namespace: str) -> Any:
                exc = RuntimeError("explode")
                exc.status = 500  # type: ignore[attr-defined]
                raise exc

        with pytest.raises(SystemExit, match="reading Secret"):
            rotate_attestation_keys(
                _FakeMac(),
                _full_cfg(),
                _BadCore(initial_data=None),
                secret_factory=_dict_factory,
            )

    def test_slot_already_populated_helper(self) -> None:
        # Empty / unparseable values are not "populated".
        assert _slot_already_populated(None) is False
        assert _slot_already_populated("") is False
        assert _slot_already_populated("!!!not-base64!!!") is False
        assert _slot_already_populated(base64.b64encode(b"").decode()) is False
        # Valid base64 with content IS populated.
        assert _slot_already_populated(base64.b64encode(b"x").decode()) is True


class _RecordingMac:
    def __init__(self) -> None:
        self.order: List[str] = []

    def get(self, path: str) -> Dict[str, Any]:
        self.order.append("GET " + path)
        return {}

    def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        self.order.append("POST " + path)
        if path == "/machines":
            return {
                "id": "db-" + (body.get("machine_id") or ""),
                "hostname": body.get("hostname", ""),
            }
        if path == "/agents":
            return {
                "id": "db-" + (body.get("agent_id") or ""),
                "agent_id": body.get("agent_id"),
                "name": body.get("name"),
                "capabilities": list(body.get("capabilities") or []),
            }
        if path.endswith("/attestation-key/rotate"):
            return {"attestation_key": "k-" + path.split("/")[2]}
        return {}


def _set_config_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    with_keys: bool = True,
) -> None:
    doc = _config_yaml()
    if not with_keys:
        doc.pop("attestation_keys")
    f = _write_yaml(tmp_path, doc)
    monkeypatch.setenv("MAC_CONFIG_FILE", str(f))
    monkeypatch.setenv("MAC_WORKER_TOKEN", "fake-token")


class TestMainCallOrder:
    def _drive(
        self,
        mac: _RecordingMac,
        core: Optional[_FakeCore],
        *,
        attestation: bool,
    ) -> None:
        cfg = BootstrapConfig(
            mac_url="http://mac-api.ai.svc.cluster.local:8000",
            dispatcher=_dispatcher_cfg(),
            role_machines=_role_machines(),
            role_agents=_role_agents(),
            attestation_keys=_attestation_keys() if attestation else None,
        )
        wait_for_mac_api(mac, attempts=2, delay_s=0.0, sleeper=lambda d: None)
        register_dispatcher(mac, cfg)
        seed_role_machines_and_agents(mac, cfg)
        rotate_attestation_keys(mac, cfg, core, secret_factory=_dict_factory)

    def test_full_pipeline_order(self) -> None:
        mac = _RecordingMac()
        core = _FakeCore(initial_data=None)
        self._drive(mac, core, attestation=True)
        assert mac.order[0] == "GET /health"
        # Dispatcher pair.
        assert mac.order[1:3] == ["POST /machines", "POST /agents"]
        # Role seed: one machine, then two agents.
        assert mac.order[3:6] == [
            "POST /machines",
            "POST /agents",
            "POST /agents",
        ]
        assert mac.order[6:8] == [
            "POST /agents/mac-worker-python-coder/attestation-key/rotate",
            "POST /agents/mac-worker-python-reviewer/attestation-key/rotate",
        ]
        # Secret was created exactly once.
        assert len(core.created) == 1

    def test_health_failure_aborts(self) -> None:
        class _NeverHealthy:
            def __init__(self) -> None:
                self.order: List[str] = []

            def get(self, path: str) -> Dict[str, Any]:
                self.order.append("GET " + path)
                raise RuntimeError("never up")

            def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
                self.order.append("POST " + path)
                return {}

        mac = _NeverHealthy()
        core = _FakeCore(initial_data=None)
        with pytest.raises(SystemExit):
            wait_for_mac_api(mac, attempts=2, delay_s=0.0, sleeper=lambda d: None)
        # We never proceeded past health (no POSTs at all).
        assert all(entry.startswith("GET ") for entry in mac.order)

    def test_dispatcher_failure_aborts(self) -> None:
        class _FailDispatcherMac(_RecordingMac):
            def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
                self.order.append("POST " + path)
                if path == "/machines":
                    raise RuntimeError("mac-api 500")
                return super().post(path, body)

        mac = _FailDispatcherMac()
        core = _FakeCore(initial_data=None)
        cfg = BootstrapConfig(
            mac_url="http://x",
            dispatcher=_dispatcher_cfg(),
            role_machines=_role_machines(),
            role_agents=_role_agents(),
            attestation_keys=_attestation_keys(),
        )
        wait_for_mac_api(mac, attempts=1, delay_s=0.0, sleeper=lambda d: None)
        with pytest.raises(RuntimeError, match="mac-api 500"):
            register_dispatcher(mac, cfg)
        assert "POST /agents" not in mac.order  # dispatcher's agent never posted
        # And no Secret writes happened.
        assert core.created == []
        assert core.patched == []

    def test_role_seed_failure_aborts_before_rotate(self) -> None:
        class _FailRoleAgentMac(_RecordingMac):
            def __init__(self) -> None:
                super().__init__()
                self._agent_calls = 0

            def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
                if path == "/agents":
                    self._agent_calls += 1
                    if self._agent_calls == 2:
                        self.order.append("POST " + path)
                        raise RuntimeError("role agent 500")
                return super().post(path, body)

        mac = _FailRoleAgentMac()
        core = _FakeCore(initial_data=None)
        cfg = BootstrapConfig(
            mac_url="http://x",
            dispatcher=_dispatcher_cfg(),
            role_machines=_role_machines(),
            role_agents=_role_agents(),
            attestation_keys=_attestation_keys(),
        )
        wait_for_mac_api(mac, attempts=1, delay_s=0.0, sleeper=lambda d: None)
        register_dispatcher(mac, cfg)
        with pytest.raises(RuntimeError, match="role agent 500"):
            seed_role_machines_and_agents(mac, cfg)
        # rotate must NOT have run.
        assert not any("attestation-key/rotate" in s for s in mac.order)
        assert core.created == []
        assert core.patched == []

    def test_rotate_failure_aborts(self) -> None:
        class _FailRotateMac(_RecordingMac):
            def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
                if path.endswith("/attestation-key/rotate"):
                    self.order.append("POST " + path)
                    raise RuntimeError("rotate 500")
                return super().post(path, body)

        mac = _FailRotateMac()
        core = _FakeCore(initial_data=None)
        cfg = BootstrapConfig(
            mac_url="http://x",
            dispatcher=_dispatcher_cfg(),
            role_machines=_role_machines(),
            role_agents=_role_agents(),
            attestation_keys=_attestation_keys(),
        )
        wait_for_mac_api(mac, attempts=1, delay_s=0.0, sleeper=lambda d: None)
        register_dispatcher(mac, cfg)
        seed_role_machines_and_agents(mac, cfg)
        with pytest.raises(RuntimeError, match="rotate 500"):
            rotate_attestation_keys(mac, cfg, core, secret_factory=_dict_factory)
        # No partial Secret write.
        assert core.created == []
        assert core.patched == []


class TestRegisterProjects:
    def test_posts_each_project_with_actor(self) -> None:
        mac = _FakeMac(
            responses={
                "POST /projects": lambda b: {
                    "id": "project_" + b["name"],
                    "name": b["name"],
                    "status": b.get("status", "active"),
                }
            }
        )
        cfg = BootstrapConfig(
            mac_url="http://x",
            dispatcher=_dispatcher_cfg(),
            projects=[
                {
                    "name": "ivan-plugin",
                    "description": "ivan",
                    "status": "active",
                    "metadata": {"repository": "https://x/y.git"},
                },
                {"name": "mac", "description": "", "status": "active", "metadata": {}},
            ],
        )
        register_projects(mac, cfg)
        posts = [p for p in mac.posted if p["path"] == "/projects"]
        assert [p["body"]["name"] for p in posts] == ["ivan-plugin", "mac"]
        assert posts[0]["body"]["actor"] == "mac-k8s-bootstrap"
        assert posts[0]["body"]["metadata"] == {"repository": "https://x/y.git"}

    def test_empty_projects_is_noop(self) -> None:
        mac = _FakeMac()
        cfg = BootstrapConfig(mac_url="http://x", dispatcher=_dispatcher_cfg(), projects=[])
        register_projects(mac, cfg)
        assert [p for p in mac.posted if p["path"] == "/projects"] == []

    def test_missing_name_raises(self) -> None:
        mac = _FakeMac()
        cfg = BootstrapConfig(
            mac_url="http://x",
            dispatcher=_dispatcher_cfg(),
            projects=[{"name": "", "description": "", "status": "active", "metadata": {}}],
        )
        with pytest.raises(SystemExit, match="requires name"):
            register_projects(mac, cfg)

    def test_updates_metadata_of_existing_project(self) -> None:
        """create_project is create-only on the control plane, so a project
        that already exists keeps its old metadata. Bootstrap must apply the
        configured metadata to existing projects too (merging over what is
        there) so GitOps config.yaml changes actually take effect on
        restart — e.g. adding a publication_target."""
        existing_meta = {
            "repository": "https://x/y.git",
            "default_branch": "main",
        }
        puts: List[Dict[str, Any]] = []

        def _projects_post(body: Dict[str, Any]) -> Dict[str, Any]:
            # Simulate create-only: returns the pre-existing record, NOT the
            # metadata in the POST body.
            return {
                "id": "project_" + body["name"],
                "name": body["name"],
                "status": "active",
                "metadata": dict(existing_meta),
            }

        def _projects_get(_body: Any) -> Dict[str, Any]:
            return {
                "record": {
                    "id": "project_ivan-plugin",
                    "name": "ivan-plugin",
                    "metadata": dict(existing_meta),
                    "status": "active",
                }
            }

        def _projects_put(body: Dict[str, Any]) -> Dict[str, Any]:
            puts.append(body)
            return {"name": "ivan-plugin", "metadata": body.get("metadata")}

        mac = _FakeMac(
            responses={
                "POST /projects": _projects_post,
                "GET /projects/ivan-plugin": _projects_get,
                "PUT /projects/ivan-plugin": _projects_put,
            }
        )
        # Teach the fake to handle PUT.
        mac.put = lambda path, body: mac._resolve("PUT " + path, body)  # type: ignore[attr-defined]

        cfg = BootstrapConfig(
            mac_url="http://x",
            dispatcher=_dispatcher_cfg(),
            projects=[
                {
                    "name": "ivan-plugin",
                    "description": "ivan",
                    "status": "active",
                    "metadata": {
                        "repository": "https://x/y.git",
                        "default_branch": "main",
                        "publication_target": "gitea://merge-request",
                    },
                },
            ],
        )
        register_projects(mac, cfg)

        # The new publication_target must have been PUT onto the project,
        # merged over the existing metadata (repository preserved).
        assert len(puts) == 1, "expected one metadata update PUT"
        meta = puts[0]["metadata"]
        assert meta["publication_target"] == "gitea://merge-request"
        assert meta["repository"] == "https://x/y.git"
        assert meta["default_branch"] == "main"

    def test_no_put_when_metadata_unchanged(self) -> None:
        """If the existing project metadata already contains the configured
        keys, bootstrap must not issue a redundant PUT."""
        meta = {
            "repository": "https://x/y.git",
            "default_branch": "main",
            "publication_target": "gitea://merge-request",
        }
        puts: List[Dict[str, Any]] = []
        mac = _FakeMac(
            responses={
                "POST /projects": lambda b: {
                    "id": "p_" + b["name"],
                    "name": b["name"],
                    "status": "active",
                    "metadata": dict(meta),
                },
                "GET /projects/ivan-plugin": lambda _b: {
                    "record": {"name": "ivan-plugin", "metadata": dict(meta)}
                },
                "PUT /projects/ivan-plugin": lambda b: puts.append(b) or {},
            }
        )
        mac.put = lambda path, body: mac._resolve("PUT " + path, body)  # type: ignore[attr-defined]
        cfg = BootstrapConfig(
            mac_url="http://x",
            dispatcher=_dispatcher_cfg(),
            projects=[
                {
                    "name": "ivan-plugin",
                    "description": "ivan",
                    "status": "active",
                    "metadata": dict(meta),
                }
            ],
        )
        register_projects(mac, cfg)
        assert puts == [], "no PUT expected when metadata already matches"


class TestRegisterFleet:
    def _fleet_cfg(self, **overrides: Any) -> BootstrapConfig:
        cfg = BootstrapConfig(
            mac_url="http://x",
            dispatcher=_dispatcher_cfg(),
            role_agents=_role_agents(),
            fleet={"name": "ai-k8s", "description": "desc", "status": "active"},
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg

    def test_skips_when_fleet_none(self) -> None:
        mac = _FakeMac()
        register_fleet(mac, self._fleet_cfg(fleet=None))
        assert mac.posted == []
        assert mac.gotten == []

    def test_creates_when_absent(self) -> None:
        puts: List[Dict[str, Any]] = []
        posts: List[Dict[str, Any]] = []

        def _fleet_get(_b: Any) -> Dict[str, Any]:
            raise _MacApiNotFound("/fleets/ai-k8s")

        def _fleet_post(b: Dict[str, Any]) -> Dict[str, Any]:
            posts.append(b)
            return {"id": "fleet_x", "name": b["name"]}

        mac = _FakeMac(
            responses={
                "GET /fleets/ai-k8s": _fleet_get,
                "POST /fleets": _fleet_post,
                "PUT /fleets/ai-k8s": lambda b: puts.append(b) or {},
            }
        )
        mac.put = lambda path, body: mac._resolve("PUT " + path, body)  # type: ignore[attr-defined]
        register_fleet(mac, self._fleet_cfg())
        assert len(posts) == 1
        assert puts == []
        body = posts[0]
        assert body["name"] == "ai-k8s"
        assert body["description"] == "desc"
        assert body["status"] == "active"
        assert body["actor"] == "mac-k8s-bootstrap"
        # Members = dispatcher + role agents, in order, deduped.
        assert body["agent_ids"] == [
            "mac-runner",
            "mac-worker-python-coder",
            "mac-worker-python-reviewer",
        ]

    def test_updates_when_exists(self) -> None:
        puts: List[Dict[str, Any]] = []
        posts: List[Dict[str, Any]] = []
        mac = _FakeMac(
            responses={
                "GET /fleets/ai-k8s": lambda _b: {
                    "id": "fleet_x",
                    "name": "ai-k8s",
                    "agent_ids": ["mac-runner"],
                },
                "POST /fleets": lambda b: posts.append(b) or {},
                "PUT /fleets/ai-k8s": lambda b: puts.append(b) or {"name": "ai-k8s"},
            }
        )
        mac.put = lambda path, body: mac._resolve("PUT " + path, body)  # type: ignore[attr-defined]
        register_fleet(mac, self._fleet_cfg())
        assert posts == []
        assert len(puts) == 1
        body = puts[0]
        assert body["actor"] == "mac-k8s-bootstrap"
        assert body["agent_ids"] == [
            "mac-runner",
            "mac-worker-python-coder",
            "mac-worker-python-reviewer",
        ]

    def test_dedups_dispatcher_in_roles(self) -> None:
        posts: List[Dict[str, Any]] = []
        mac = _FakeMac(
            responses={
                "GET /fleets/ai-k8s": lambda _b: (_ for _ in ()).throw(
                    _MacApiNotFound("/fleets/ai-k8s")
                ),
                "POST /fleets": lambda b: posts.append(b) or {"id": "f"},
            }
        )
        mac.put = lambda path, body: mac._resolve("PUT " + path, body)  # type: ignore[attr-defined]
        cfg = self._fleet_cfg(
            role_agents=[
                {
                    "agent_id": "mac-runner",
                    "name": "mac-runner",
                    "machine_id": "m",
                    "capabilities": [],
                },
                {
                    "agent_id": "mac-worker-python-coder",
                    "name": "mac-worker-python-coder",
                    "machine_id": "m",
                    "capabilities": [],
                },
            ]
        )
        register_fleet(mac, cfg)
        # Dispatcher id appears once even though a role re-declares it.
        assert posts[0]["agent_ids"] == [
            "mac-runner",
            "mac-worker-python-coder",
        ]

    def test_non_object_post_response_raises(self) -> None:
        mac = _FakeMac(
            responses={
                "GET /fleets/ai-k8s": lambda _b: (_ for _ in ()).throw(
                    _MacApiNotFound("/fleets/ai-k8s")
                ),
                "POST /fleets": lambda _b: "not-an-object",
            }
        )
        mac.put = lambda path, body: mac._resolve("PUT " + path, body)  # type: ignore[attr-defined]
        with pytest.raises(SystemExit, match="POST /fleets returned non-object"):
            register_fleet(mac, self._fleet_cfg())

    def test_non_404_get_error_raises(self) -> None:
        """A real GET error (not a 404) must abort, not be treated as
        'create' — otherwise a transient 500 would silently POST a duplicate."""
        mac = _FakeMac(
            responses={
                "GET /fleets/ai-k8s": lambda _b: (_ for _ in ()).throw(
                    RuntimeError("mac API GET /fleets/ai-k8s failed: 500 internal")
                ),
            }
        )
        mac.put = lambda path, body: mac._resolve("PUT " + path, body)  # type: ignore[attr-defined]
        with pytest.raises(SystemExit, match="GET /fleets/ai-k8s failed"):
            register_fleet(mac, self._fleet_cfg())
        # Must NOT have attempted a create.
        assert [p for p in mac.posted if p["path"] == "/fleets"] == []


# --- relocated from test_k8s_bootstrap_edges.py (coverage companion folded in) ---


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
    core = SimpleNamespace(
        read_namespaced_secret=lambda *_a: (_ for _ in ()).throw(SimpleNamespace(status=404))
    )
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
            mac,
            _cfg(attestation_keys=spec),
            MissingCore(),
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


def test_token_from_env_prefers_fleet_scoped_form(monkeypatch) -> None:
    """_token_from_env must resolve the scoped MAC_*__<FLEET> token ahead of the
    legacy flat form, fall back through the chain, treat an empty scoped value as
    set, and still raise SystemExit when nothing resolves (mac-g55y)."""
    for name in (
        "MAC_FLEET",
        "MAC_WORKER_TOKEN",
        "MAC_WORKER_TOKEN__ROCKY",
        "MAC_API_TOKEN",
        "MAC_API_TOKEN__ROCKY",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="is required"):
        bootstrap._token_from_env()
    monkeypatch.setenv("MAC_FLEET", "rocky")
    monkeypatch.setenv("MAC_API_TOKEN", "api-flat")
    assert bootstrap._token_from_env() == "api-flat"
    monkeypatch.setenv("MAC_WORKER_TOKEN", "worker-flat")
    assert bootstrap._token_from_env() == "worker-flat"
    monkeypatch.setenv("MAC_WORKER_TOKEN__ROCKY", "worker-rocky")
    assert bootstrap._token_from_env() == "worker-rocky"
    monkeypatch.setenv("MAC_WORKER_TOKEN__ROCKY", "")
    with pytest.raises(SystemExit, match="is required"):
        bootstrap._token_from_env()


def test_main_runs_full_pipeline_with_and_without_secret_rotation(monkeypatch) -> None:
    calls = []
    cfg = _cfg()
    monkeypatch.setattr(bootstrap.BootstrapConfig, "from_env", classmethod(lambda cls: cfg))
    monkeypatch.setattr(bootstrap, "_token_from_env", lambda: "token")
    for name in (
        "wait_for_mac_api",
        "register_dispatcher",
        "register_role_definitions",
        "seed_role_machines_and_agents",
        "register_projects",
        "register_fleet",
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
    monkeypatch.setattr(
        bootstrap, "rotate_attestation_keys", lambda *a: calls.append(("rotate", a[-1]))
    )
    assert bootstrap.main([]) == 0
    assert "load" in calls and ("rotate", "core") in calls
