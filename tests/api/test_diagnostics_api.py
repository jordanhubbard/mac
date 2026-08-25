"""Hub-native diagnostics over HTTP (`mac diagnostics` in remote mode).

The diagnostics checks used to reach into ``ControlPlane.store`` for direct
SQL. In remote mode that store is a refusing stand-in, so a client either got a
DispatchError or (worse) an accidental local database. These tests pin the
hub-native contract: the ``/diagnostics`` route runs every check against the
hub's authoritative backend, and ``RemoteDispatch.diagnostics_report`` fetches
that report over HTTP instead of touching a local store.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.dispatch import RemoteDispatch
from mac.http_client import HubClient
from mac.services import ControlPlane


def _client() -> TestClient:
    return TestClient(create_app(control_plane=ControlPlane.in_memory()))


def test_diagnostics_route_serves_report_from_hub_authority():
    client = _client()
    resp = client.get("/diagnostics")
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["schema"] == "mac.diagnostics.report.v1"
    assert "database-reachable" in report["checks"]
    assert "lifecycle-stage-dwell" in report["checks"]
    assert "data-source-identity" in report["checks"]
    # The report self-identifies the authoritative backend it ran against.
    # Which engine that is depends on how the suite is configured; what must
    # hold is that the report names the one it actually used.
    assert report["data_source"]["backend"] in {"sqlite", "postgres"}
    assert report["data_source"]["authoritative"] is True


def test_diagnostics_route_supports_check_subset():
    client = _client()
    resp = client.get("/diagnostics", params={"check": ["failed-tasks"]})
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert {f["check"] for f in report["findings"]} == {"failed-tasks"}
    # Even a subset request keeps the machine-readable data_source identity.
    assert report["data_source"]["backend"] in {"sqlite", "postgres"}


def test_remote_dispatch_diagnostics_hits_hub_and_never_touches_local_store():
    """The remote path must fetch /diagnostics, not run SQL via _RemoteStore."""
    client = _client()

    def _transport(method, url, body, token):
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        path = parts.path + (("?" + parts.query) if parts.query else "")
        resp = client.request(method, path, json=body)
        assert resp.status_code == 200, resp.text
        return resp.json()

    dispatch = RemoteDispatch(
        HubClient("http://hub.example:8789", token="tok", transport=_transport)
    )

    report = dispatch.diagnostics_report()
    assert report["schema"] == "mac.diagnostics.report.v1"
    assert report["data_source"]["backend"] in {"sqlite", "postgres"}
    # The client augments the identity with the hub URL it actually talked to.
    assert report["data_source"]["hub_url"] == "http://hub.example:8789"


def test_remote_store_still_refuses_direct_sql():
    """Diagnostics no longer relies on this, but the refusal must stay honest."""
    import pytest

    from mac.dispatch import DispatchError, _RemoteStore

    store = _RemoteStore()
    with pytest.raises(DispatchError):
        store.query_all("SELECT 1")
