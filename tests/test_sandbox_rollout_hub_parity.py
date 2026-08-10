"""`mac admin sandbox-image rollout` must work through the hub, not only against --db.

LocalDispatch forwards unknown methods to ControlPlane via __getattr__, so a
new control-plane method is reachable in --db mode the moment it exists.
RemoteDispatch has explicit methods only, so the same method is missing over
HTTP until someone adds it.

That asymmetry made `mac admin sandbox-image rollout` work in every test (they all use
--db) and fail against the hub, which is how the fleet is actually operated.
These tests close it for the rollout and assert the shape of the gap so the
next command does not repeat it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.dispatch import RemoteDispatch
from mac.sandbox_rollout import ROLLOUT_METADATA_KEY
from mac.services import ControlPlane

DIGEST = "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "a" * 64


class _ClientOverHttp:
    """HubClient surface backed by the in-process app."""

    def __init__(self, client):
        self._client = client

    def request(self, method, path, body=None):
        response = self._client.request(method, path, json=body)
        response.raise_for_status()
        return response.json()


@pytest.fixture()
def remote():
    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    machine = cp.register_machine("parity-host")
    cp.register_agent(machine.id, "worker1")
    app = create_app(control_plane=cp)
    return cp, RemoteDispatch(_ClientOverHttp(TestClient(app)))


def test_the_rollout_is_reachable_over_http(remote):
    """The failure this file exists for: AttributeError against a real hub."""
    _cp, dispatch = remote

    result = dispatch.roll_out_sandbox_image(DIGEST)

    assert len(result["filed"]) == 1


def test_the_barrier_task_is_real_on_the_hub_side(remote):
    """Filed through HTTP must produce the same sync task as filing locally --
    otherwise the endpoint is a stub that reports success."""
    cp, dispatch = remote

    result = dispatch.roll_out_sandbox_image(DIGEST)
    task = cp.get_task(result["filed"][0])

    assert task.metadata["execution_mode"] == "sync"
    assert task.metadata[ROLLOUT_METADATA_KEY]["image"] == DIGEST


def test_a_bad_image_is_refused_over_http_too(remote):
    """The digest rule must not be enforced only on the client side, or the
    hub becomes the weaker door into the security boundary."""
    _cp, dispatch = remote

    with pytest.raises(Exception):
        dispatch.roll_out_sandbox_image("ghcr.io/jordanhubbard/mac-openshell-runtime:latest")


def test_remote_dispatch_exposes_the_sandbox_surface_the_cli_calls():
    """A guard on the asymmetry itself.

    Every ControlPlane method the sandbox CLI invokes has to exist on
    RemoteDispatch; --db mode gets them for free and hides their absence.
    """
    for name in ("roll_out_sandbox_image", "list_project_repositories"):
        assert hasattr(RemoteDispatch, name), (
            "%s is missing from RemoteDispatch, so `mac sandbox` breaks against "
            "a hub while passing every --db test" % name
        )
