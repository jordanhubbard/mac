"""A live re-registration must resurrect a tombstoned agent.

delete_agent tombstones (deleted_at set) + benches (dispatch_hold) an agent so
its provenance survives. But when that identity comes back — a recreated pod
re-deploying under the same name — register_agent must undelete it, or the
worker heartbeats forever as a dead tombstone (observed live: a redeployed GKE
worker stayed offline/deleted despite healthy heartbeats). Resurrection clears
the tombstone and the *decommission* hold; an operator hold on a still-live
agent is preserved.
"""
from __future__ import annotations

from mac.services import ControlPlane


def _register(cp):
    m = cp.register_machine("host-worker-1")
    return m.id, cp.register_agent(m.id, "worker-1", capabilities=["python"], agent_id="agent_worker_1")


def test_reregister_resurrects_tombstoned_agent():
    cp = ControlPlane.in_memory()
    mid, a = _register(cp)
    cp.delete_agent(a.id, actor="operator")
    gone = cp.get_agent(a.id)
    assert gone.deleted_at and gone.dispatch_hold  # tombstoned + benched

    again = cp.register_agent(mid, "worker-1", capabilities=["python"], agent_id=a.id)
    assert again.id == a.id
    live = cp.get_agent(a.id)
    assert live.deleted_at is None          # resurrected
    assert not live.dispatch_hold           # decommission hold cleared
    assert live.status == "idle"


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
