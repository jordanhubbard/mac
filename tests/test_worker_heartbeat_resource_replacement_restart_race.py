"""Regression: a failed prerequisite resource GET must not erase the hub snapshot.

Restart-race fingerprint (Rocky's first heartbeat after a hub restart): the
worker's ``GET /agents/{id}`` for its own complete resource document raced hub
availability and failed, but ``_resources_with_live_report_executor_attestation``
still synthesised a partial, attestation-only resources map from ``None``.  The
heartbeat therefore *supplied* ``resources``, and the hub -- correctly treating a
supplied resources document as a full-document replacement so workers can
deliberately withdraw stale advertisements -- swapped the stored map for that
partial one.  That erased hardware, media_routes, openclaw_runtime,
chat_gateway, gateway_ownership, and representation together.

The fix keeps hub replacement semantics unchanged and instead makes the worker
omit ``resources`` entirely whenever no prerequisite GET produced a base
document.  These tests prove:

* a GET failure followed by a heartbeat sends NO ``resources`` key, and
* the last complete hub snapshot survives the heartbeat unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi.testclient import TestClient

from typing import Optional

from mac.api import create_app
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.services import ControlPlane
from mac.worker import MacWorker, WorkerExecution


def _complete_resource_snapshot() -> Dict[str, Any]:
    """A complete advertisement like Natasha/Bullwinkle retained across restart."""
    return {
        "hardware": {"cpu": "test-cpu", "memory_gb": 16},
        "media_routes": {"/v1/media": "http://worker.test/media"},
        "openclaw_runtime": {"schema": "mac.openclaw_runtime.v1", "ready": True},
        "chat_gateway": {"endpoint": "http://worker.test/chat"},
        "gateway_ownership": {"owner": "worker"},
        "representation": {"display_name": "Rocky"},
    }


def api_transport(client: TestClient):
    def transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Any:
        request = getattr(client, method.lower())
        kwargs: Dict[str, Any] = {}
        if payload is not None:
            kwargs["json"] = payload
        response = request(path, **kwargs)
        if response.status_code >= 400:
            raise MacApiError(response.text)
        return response.json() if response.content else None

    return transport


def _worker_with_complete_snapshot(
    tmp_path: Path,
) -> Tuple[ControlPlane, TestClient, MacWorker, str]:
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(
        machine.id,
        "worker",
        capabilities=["python"],
        resources=_complete_resource_snapshot(),
    )
    client = TestClient(create_app(control_plane=cp))
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        lambda *a, **k: WorkerExecution(0, "ok"),
        attestation_key=cp._agent_attestation_key(agent.id),
    )
    return cp, client, worker, agent.id


def test_heartbeat_after_get_failure_omits_resources_entirely(tmp_path: Path):
    cp, _client, worker, agent_id = _worker_with_complete_snapshot(tmp_path)

    posts: List[Tuple[str, Any]] = []
    real_post = worker.client.post
    real_get = worker.client.get

    def failing_get(path: str, *args: Any, **kwargs: Any):
        if path.endswith("/agents/%s" % agent_id):
            # Reproduce the restart race: the prerequisite resource GET is
            # transiently unavailable while the hub is still coming up.
            raise MacApiError("hub unavailable during restart")
        return real_get(path, *args, **kwargs)

    def recording_post(path: str, body: Any = None):
        posts.append((path, body))
        return real_post(path, body)

    worker.client.get = failing_get  # type: ignore[assignment]
    worker.client.post = recording_post  # type: ignore[assignment]

    worker._heartbeat()

    heartbeats = [body for path, body in posts if path.endswith("/heartbeat")]
    assert heartbeats, "worker did not heartbeat"
    # The whole point: a failed prerequisite GET must omit resources entirely so
    # the heartbeat cannot act as a full-document replacement.
    assert "resources" not in heartbeats[0], heartbeats[0]


def test_heartbeat_after_get_failure_cannot_erase_last_complete_snapshot(tmp_path: Path):
    cp, _client, worker, agent_id = _worker_with_complete_snapshot(tmp_path)

    before = dict(cp.get_agent(agent_id).resources)
    # Sanity: the seeded advertisement is the complete one, not a partial map.
    for key in (
        "hardware",
        "media_routes",
        "openclaw_runtime",
        "chat_gateway",
        "gateway_ownership",
        "representation",
    ):
        assert key in before, key

    real_get = worker.client.get

    def failing_get(path: str, *args: Any, **kwargs: Any):
        if path.endswith("/agents/%s" % agent_id):
            raise MacApiError("hub unavailable during restart")
        return real_get(path, *args, **kwargs)

    worker.client.get = failing_get  # type: ignore[assignment]

    worker._heartbeat()

    after = dict(cp.get_agent(agent_id).resources)
    assert after == before, "heartbeat after a failed resource GET erased the complete hub snapshot"


def test_heartbeat_with_successful_get_still_refreshes_resources(tmp_path: Path):
    """Guard against over-correcting: when the GET succeeds the worker still
    supplies resources (the busy-refresh / attestation-refresh guarantee)."""
    cp, _client, worker, agent_id = _worker_with_complete_snapshot(tmp_path)

    posts: List[Tuple[str, Any]] = []
    real_post = worker.client.post

    def recording_post(path: str, body: Any = None):
        posts.append((path, body))
        return real_post(path, body)

    worker.client.post = recording_post  # type: ignore[assignment]

    worker._heartbeat()

    heartbeats = [body for path, body in posts if path.endswith("/heartbeat")]
    assert heartbeats, "worker did not heartbeat"
    assert isinstance(heartbeats[0].get("resources"), dict)
    # The complete advertisement survives a normal heartbeat too.
    after = dict(cp.get_agent(agent_id).resources)
    for key in (
        "hardware",
        "media_routes",
        "openclaw_runtime",
        "chat_gateway",
        "gateway_ownership",
        "representation",
    ):
        assert key in after, key
