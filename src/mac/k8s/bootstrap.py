from __future__ import annotations

import base64
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

from mac.k8s.config_loader import MacConfigFile, load_config_file

JsonDict = Dict[str, Any]
log = logging.getLogger(__name__)

DEFAULT_HEALTH_ATTEMPTS = 60
DEFAULT_HEALTH_DELAY_S = 2.0

@dataclass
class BootstrapConfig:
    mac_url: str
    dispatcher: JsonDict
    role_machines: List[JsonDict] = field(default_factory=list)
    role_agents: List[JsonDict] = field(default_factory=list)
    attestation_keys: Optional[JsonDict] = None
    projects: List[JsonDict] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "BootstrapConfig":
        return cls.from_file(os.environ.get("MAC_CONFIG_FILE"))

    @classmethod
    def from_file(cls, path: Optional[str]) -> "BootstrapConfig":
        cfg_file: MacConfigFile = load_config_file(path)
        return cls(
            mac_url=cfg_file.mac_url,
            dispatcher=dict(cfg_file.dispatcher),
            role_machines=[dict(m) for m in cfg_file.role_machines],
            role_agents=cfg_file.role_agents(),
            attestation_keys=cfg_file.attestation_keys_block(),
            projects=[
                {
                    "name": p.name,
                    "description": p.description,
                    "status": p.status,
                    "metadata": dict(p.metadata),
                }
                for p in cfg_file.projects
            ],
        )

class MacApiProtocol(Protocol):
    def post(self, path: str, body: JsonDict) -> JsonDict: ...
    def get(self, path: str) -> JsonDict: ...

class CoreV1Protocol(Protocol):
    def read_namespaced_secret(self, name: str, namespace: str) -> Any: ...
    def create_namespaced_secret(self, namespace: str, body: Any) -> Any: ...
    def patch_namespaced_secret(
        self, name: str, namespace: str, body: Any
    ) -> Any: ...

def wait_for_mac_api(
    mac: MacApiProtocol,
    *,
    attempts: int = DEFAULT_HEALTH_ATTEMPTS,
    delay_s: float = DEFAULT_HEALTH_DELAY_S,
    sleeper: Optional[Callable[[float], None]] = None,
) -> None:
    sleep = sleeper or time.sleep
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            mac.get("/health")
            log.info("mac-api /health reachable on attempt %d", attempt)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.info(
                "[wait] mac-api /health not ready (%d/%d): %s",
                attempt,
                attempts,
                exc,
            )
            if attempt < attempts:
                sleep(delay_s)
    raise SystemExit(
        "mac-api /health never became ready after %d attempts (last error: %s)"
        % (attempts, last_exc)
    )

def _post_machine(mac: MacApiProtocol, spec: JsonDict) -> JsonDict:
    body = {
        "hostname": spec.get("hostname") or "",
        "machine_id": spec.get("machine_id") or spec.get("id") or "",
        "labels": dict(spec.get("labels") or {}),
        "trusted": bool(spec.get("trusted", True)),
    }
    if not body["hostname"] or not body["machine_id"]:
        raise SystemExit(
            "machine spec requires hostname and machine_id (got %r)" % spec
        )
    resp = mac.post("/machines", body)
    if not isinstance(resp, dict):
        raise SystemExit(
            "POST /machines returned non-object: %r" % (resp,)
        )
    return resp

def _post_agent(
    mac: MacApiProtocol, spec: JsonDict, *, machine_db_id: str
) -> JsonDict:
    body = {
        "machine_id": machine_db_id,
        "name": spec.get("name") or spec.get("agent_id") or "",
        "agent_id": spec.get("agent_id") or spec.get("id") or "",
        "capabilities": list(spec.get("capabilities") or []),
    }
    if not body["name"] or not body["agent_id"]:
        raise SystemExit(
            "agent spec requires name and agent_id (got %r)" % spec
        )
    resp = mac.post("/agents", body)
    if not isinstance(resp, dict):
        raise SystemExit(
            "POST /agents returned non-object: %r" % (resp,)
        )
    return resp

def register_dispatcher(mac: MacApiProtocol, cfg: BootstrapConfig) -> None:
    machine_spec = cfg.dispatcher["machine"]
    agent_spec = cfg.dispatcher["agent"]
    machine = _post_machine(mac, machine_spec)
    machine_db_id = machine.get("id")
    if not machine_db_id:
        raise SystemExit(
            "POST /machines response missing id: %r" % (machine,)
        )
    agent = _post_agent(mac, agent_spec, machine_db_id=machine_db_id)
    log.info(
        "dispatcher: machine=%s agent=%s caps=%s",
        machine.get("hostname"),
        agent.get("name"),
        agent.get("capabilities"),
    )

def seed_role_machines_and_agents(
    mac: MacApiProtocol, cfg: BootstrapConfig
) -> None:
    machine_db_ids: Dict[str, str] = {}
    for m in cfg.role_machines:
        seed_id = m.get("machine_id") or m.get("id")
        if not seed_id:
            raise SystemExit(
                "role_machines entry requires machine_id or id: %r" % (m,)
            )
        resp = _post_machine(mac, m)
        db_id = resp.get("id")
        if not db_id:
            raise SystemExit(
                "POST /machines for seed_id=%s missing response id: %r"
                % (seed_id, resp)
            )
        machine_db_ids[str(seed_id)] = str(db_id)
        log.info(
            "role-machine: seed_id=%s db_id=%s hostname=%s",
            seed_id,
            db_id,
            resp.get("hostname"),
        )

    for a in cfg.role_agents:
        ref = a.get("machine_id")
        if not ref:
            raise SystemExit(
                "role_agents entry requires machine_id ref: %r" % (a,)
            )
        machine_db_id = machine_db_ids.get(str(ref))
        if not machine_db_id:
            raise SystemExit(
                "role_agents entry references unknown machine_id=%r "
                "(known seed ids: %s)"
                % (ref, sorted(machine_db_ids))
            )
        resp = _post_agent(mac, a, machine_db_id=machine_db_id)
        log.info(
            "role-agent: agent_id=%s name=%s caps=%s",
            resp.get("agent_id") or resp.get("id"),
            resp.get("name"),
            resp.get("capabilities"),
        )

def register_projects(mac: MacApiProtocol, cfg: BootstrapConfig) -> None:
    for spec in cfg.projects:
        name = spec.get("name")
        if not name:
            raise SystemExit("projects entry requires name: %r" % (spec,))
        body = {
            "name": name,
            "description": spec.get("description") or "",
            "status": spec.get("status") or "active",
            "metadata": dict(spec.get("metadata") or {}),
            "actor": "mac-k8s-bootstrap",
        }
        resp = mac.post("/projects", body)
        if not isinstance(resp, dict):
            raise SystemExit("POST /projects returned non-object: %r" % (resp,))
        log.info(
            "project: name=%s id=%s status=%s",
            resp.get("name"),
            resp.get("id"),
            resp.get("status"),
        )


def _existing_secret_data(
    core: CoreV1Protocol, namespace: str, name: str
) -> tuple[Optional[Dict[str, str]], Any]:
    try:
        obj = core.read_namespaced_secret(name, namespace)
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status", None)
        if status == 404:
            return None, None
        raise SystemExit(
            "reading Secret %s/%s failed: %s" % (namespace, name, exc)
        ) from exc
    data = getattr(obj, "data", None)
    if data is None and isinstance(obj, dict):
        data = obj.get("data")
    return dict(data or {}), obj

def _slot_already_populated(value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        return bool(base64.b64decode(value))
    except Exception:  # noqa: BLE001
        return False

def rotate_attestation_keys(
    mac: MacApiProtocol,
    cfg: BootstrapConfig,
    core: Optional[CoreV1Protocol] = None,
    *,
    secret_factory: Optional[Callable[[str, str, Dict[str, str]], Any]] = None,
) -> None:
    if cfg.attestation_keys is None:
        log.info("attestation_keys not configured; skipping key-rotation step")
        return
    if core is None:
        raise SystemExit(
            "rotate_attestation_keys requires a CoreV1 client when "
            "attestation_keys is configured"
        )

    spec = cfg.attestation_keys
    namespace = str(spec.get("namespace") or "").strip()
    secret_name = str(spec.get("secret_name") or "").strip()
    roles = spec.get("roles") or {}
    if not namespace or not secret_name:
        raise SystemExit(
            "attestation_keys requires namespace and secret_name"
        )
    if not isinstance(roles, dict) or not roles:
        raise SystemExit(
            "attestation_keys.roles must be a non-empty {role: agent_id} map"
        )

    existing_data, existing_obj = _existing_secret_data(
        core, namespace, secret_name
    )
    if existing_data is None:
        log.info(
            "Secret %s/%s not present; will create after rotation",
            namespace,
            secret_name,
        )
        existing_data = {}
    else:
        log.info(
            "Secret %s/%s present with %d key(s)",
            namespace,
            secret_name,
            len(existing_data),
        )

    new_data = dict(existing_data)
    rotated_any = False
    for role_slug, agent_id in roles.items():
        if _slot_already_populated(existing_data.get(role_slug)):
            log.info(
                "[skip] role=%s agent=%s key already provisioned",
                role_slug,
                agent_id,
            )
            continue
        resp = mac.post(
            "/agents/%s/attestation-key/rotate" % agent_id, {}
        )
        if not isinstance(resp, dict):
            raise SystemExit(
                "rotate response for agent=%s not an object: %r"
                % (agent_id, resp)
            )
        key = resp.get("attestation_key")
        if not key:
            raise SystemExit(
                "rotate response for agent=%s missing attestation_key: %r"
                % (agent_id, resp)
            )
        new_data[str(role_slug)] = base64.b64encode(
            key.encode("utf-8")
        ).decode("ascii")
        rotated_any = True
        log.info(
            "rotated role=%s agent=%s (key len=%d)",
            role_slug,
            agent_id,
            len(key),
        )

    if not rotated_any:
        log.info("no role keys needed rotation — Secret unchanged")
        return

    body = _build_secret_body(
        namespace, secret_name, new_data, factory=secret_factory
    )
    try:
        if existing_obj is None:
            core.create_namespaced_secret(namespace, body)
            log.info(
                "created Secret %s/%s with %d role key(s)",
                namespace,
                secret_name,
                len(new_data),
            )
        else:
            core.patch_namespaced_secret(secret_name, namespace, body)
            log.info(
                "patched Secret %s/%s (now %d role key(s))",
                namespace,
                secret_name,
                len(new_data),
            )
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            "writing Secret %s/%s failed: %s" % (namespace, secret_name, exc)
        ) from exc

def _build_secret_body(
    namespace: str,
    name: str,
    data: Dict[str, str],
    *,
    factory: Optional[Callable[[str, str, Dict[str, str]], Any]] = None,
) -> Any:
    if factory is not None:
        return factory(namespace, name, data)
    from kubernetes import client as k8s_client  # noqa: WPS433

    meta = k8s_client.V1ObjectMeta(
        name=name,
        namespace=namespace,
        labels={
            "app.kubernetes.io/name": "mac",
            "app.kubernetes.io/component": "runner",
            "app.kubernetes.io/managed-by": "mac-k8s-bootstrap",
        },
    )
    return k8s_client.V1Secret(metadata=meta, type="Opaque", data=data)

def _token_from_env() -> str:
    token = os.environ.get("MAC_WORKER_TOKEN") or os.environ.get(
        "MAC_API_TOKEN", ""
    )
    if not token:
        raise SystemExit(
            "MAC_WORKER_TOKEN (or MAC_API_TOKEN) is required"
        )
    return token

def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    logger = logging.getLogger("mac-k8s-bootstrap")

    cfg = BootstrapConfig.from_env()
    token = _token_from_env()

    from mac.hermes_adapter import MacApiClient
    from mac.k8s.k8s_client import load_in_cluster_config
    from kubernetes import client as k8s_client

    mac = MacApiClient(cfg.mac_url, token=token)

    logger.info("mac-k8s-bootstrap starting: mac_url=%s", cfg.mac_url)
    wait_for_mac_api(mac)
    register_dispatcher(mac, cfg)
    seed_role_machines_and_agents(mac, cfg)
    register_projects(mac, cfg)

    if cfg.attestation_keys is not None:
        load_in_cluster_config()
        core = k8s_client.CoreV1Api()
        rotate_attestation_keys(mac, cfg, core)
    else:
        logger.info("attestation_keys not configured; skipping key-rotation step")

    logger.info("mac-k8s-bootstrap complete")
    return 0

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
