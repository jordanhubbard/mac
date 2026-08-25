"""Only an administrator-authorized registration may resurrect a tombstone.

delete_agent tombstones (deleted_at set) + benches (dispatch_hold) an agent so
its provenance survives. A worker credential must not turn that durable delete
into self-service resurrection. An administrator can explicitly restore a
recreated identity; that clears the tombstone and the *decommission* hold. An
operator hold on a still-live agent is preserved.
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.models import AuthorizationError
from mac.services import ControlPlane


def _register(cp):
    m = cp.register_machine("host-worker-1")
    return m.id, cp.register_agent(
        m.id, "worker-1", capabilities=["python"], agent_id="agent_worker_1"
    )


def test_reregister_requires_explicit_resurrection_authority():
    cp = ControlPlane.in_memory()
    mid, a = _register(cp)
    cp.delete_agent(a.id, actor="operator")
    gone = cp.get_agent(a.id)
    assert gone.deleted_at and gone.dispatch_hold  # tombstoned + benched

    with pytest.raises(
        AuthorizationError,
        match="resurrection requires administrative authority",
    ):
        cp.register_agent(mid, "worker-1", capabilities=["python"], agent_id=a.id)

    still_gone = cp.get_agent(a.id)
    assert still_gone.deleted_at == gone.deleted_at
    assert still_gone.dispatch_hold

    again = cp.register_agent(
        mid,
        "worker-1",
        capabilities=["python"],
        agent_id=a.id,
        allow_resurrection=True,
    )
    assert again.id == a.id
    live = cp.get_agent(a.id)
    assert live.deleted_at is None  # resurrected
    assert not live.dispatch_hold  # decommission hold cleared
    assert live.status == "idle"


def test_registration_api_only_admin_can_resurrect_tombstone():
    cp = ControlPlane.in_memory()
    mid, agent = _register(cp)
    cp.delete_agent(agent.id, actor="operator")
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "worker": {
                    "scopes": ["agent", "dispatch", "read", "write"],
                    "agent_id": agent.id,
                    "principal_kind": "worker",
                },
                "admin": ["admin"],
            },
        )
    )
    payload = {
        "machine_id": mid,
        "name": "worker-1",
        "capabilities": ["python"],
        "agent_id": agent.id,
    }

    rejected = client.post(
        "/agents",
        headers={"Authorization": "Bearer worker"},
        json=payload,
    )
    assert rejected.status_code == 403
    assert cp.get_agent(agent.id).deleted_at is not None

    restored = client.post(
        "/agents",
        headers={"Authorization": "Bearer admin"},
        json=payload,
    )
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None


def test_reregister_preserves_operator_hold_on_live_agent():
    cp = ControlPlane.in_memory()
    mid, a = _register(cp)
    # an operator benches a STILL-LIVE (non-deleted) agent
    cp.set_agent_dispatch_hold(a.id, reason="operator_bench") if hasattr(
        cp, "set_agent_dispatch_hold"
    ) else cp.store.execute(
        "UPDATE agents SET dispatch_hold=1, dispatch_hold_reason='operator_bench' WHERE id=?",
        (a.id,),
    )
    cp.register_agent(mid, "worker-1", capabilities=["python"], agent_id=a.id)
    live = cp.get_agent(a.id)
    assert live.deleted_at is None
    # not deleted -> the operator hold is NOT cleared by re-registration
    assert live.dispatch_hold
    assert live.dispatch_hold_reason == "operator_bench"


def test_fresh_register_is_not_deleted():
    cp = ControlPlane.in_memory()
    _mid, a = _register(cp)
    assert cp.get_agent(a.id).deleted_at is None
