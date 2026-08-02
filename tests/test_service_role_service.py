"""media-01 service-role election: claim/sync/reconcile lifecycle."""
from __future__ import annotations

import threading

import pytest

from mac.store import StoreError

from mac.models import HealthStatus, ValidationError
from mac.services import ControlPlane
from mac.test_support import ephemeral_dsn, ephemeral_store, store_on

GPU_HW = {"accelerator": "cuda", "gpu": {"name": "X", "vram_mb": 48000}, "memory_mb": 120000}
OPS = ["image.generate", "audio.tts", "audio.music", "audio.asr", "video.generate"]


def _gpu_agent(cp, name, capacity=3):
    m = cp.register_machine("%s-host" % name, resources={})
    return cp.register_agent(
        m.id, name, capabilities=["gpu", "cuda"],
        resources={"hardware": GPU_HW, "capacity": capacity},
    )


def _seed(cp):
    cp.seed_service_roles(OPS)


def test_sync_claims_eligible_ops_up_to_capacity():
    cp = ControlPlane.in_memory()
    _seed(cp)
    a = _gpu_agent(cp, "natasha", capacity=2)
    res = cp.sync_agent_service_claims(a.id, ["image.generate", "audio.tts", "video.generate"])
    assert res["capacity"] == 2
    assert len(res["held"]) == 2  # capacity-bounded
    assert set(res["held"]).issubset({"image.generate", "audio.tts", "video.generate"})


def test_pool_spreads_across_hosts():
    cp = ControlPlane.in_memory()
    _seed(cp)
    a = _gpu_agent(cp, "natasha", capacity=2)
    b = _gpu_agent(cp, "madmax", capacity=2)
    willing = ["image.generate", "audio.tts", "audio.music", "video.generate"]
    held_a = set(cp.sync_agent_service_claims(a.id, willing)["held"])
    held_b = set(cp.sync_agent_service_claims(b.id, willing)["held"])
    assert len(held_a) == 2 and len(held_b) == 2
    # pool model: together they cover more ops than any one host (spread)
    assert len(held_a | held_b) >= 3


def test_cpu_agent_is_ineligible():
    cp = ControlPlane.in_memory()
    _seed(cp)
    m = cp.register_machine("rocky-host", resources={})
    a = cp.register_agent(
        m.id, "rocky", capabilities=["python"],
        resources={"hardware": {"accelerator": "none"}, "capacity": 5},
    )
    res = cp.sync_agent_service_claims(a.id, ["image.generate", "audio.tts"])
    assert res["held"] == []  # no gpu capability + no accelerator


def test_under_vram_hardware_is_ineligible():
    cp = ControlPlane.in_memory()
    _seed(cp)
    m = cp.register_machine("small-host", resources={})
    a = cp.register_agent(
        m.id, "small", capabilities=["gpu", "cuda"],
        resources={"hardware": {"accelerator": "cuda", "gpu": {"vram_mb": 5000}}, "capacity": 5},
    )
    res = cp.sync_agent_service_claims(a.id, ["video.generate"])  # svd needs 15GB
    assert "video.generate" not in res["held"]


def test_release_on_unwilling_then_renew():
    cp = ControlPlane.in_memory()
    _seed(cp)
    a = _gpu_agent(cp, "natasha", capacity=5)
    cp.sync_agent_service_claims(a.id, ["image.generate", "audio.tts"])
    assert set(cp.service_roles.held_ops_for_agent(a.id)) == {"image.generate", "audio.tts"}
    cp.sync_agent_service_claims(a.id, ["image.generate"])  # drop audio.tts
    assert set(cp.service_roles.held_ops_for_agent(a.id)) == {"image.generate"}


def test_expire_reopens_and_reconcile_signals_zero_holders():
    cp = ControlPlane.in_memory()
    _seed(cp)
    a = _gpu_agent(cp, "natasha", capacity=5)
    cp.sync_agent_service_claims(a.id, ["image.generate"])
    role = cp.service_roles.get_role_by_slug("media:image.generate")
    # renew into the past, then sweep with no grace -> expired, slot reopens
    cp.service_roles.claim_service(role.id, a.id, lease_seconds=-100)
    expired = cp.service_roles.expire_service_claims(grace_seconds=0)
    assert any(c.agent_id == a.id for c in expired)
    assert cp.service_roles.list_active_claims(role_id=role.id) == []
    out = cp.reconcile_service_roles()
    assert "image.generate" in out["requested"]  # cluster needs an image agent


def test_one_agent_cannot_double_hold_an_op():
    cp = ControlPlane.in_memory()
    _seed(cp)
    a = _gpu_agent(cp, "natasha", capacity=5)
    role = cp.service_roles.get_role_by_slug("media:image.generate")
    c1 = cp.service_roles.claim_service(role.id, a.id)
    c2 = cp.service_roles.claim_service(role.id, a.id)  # same agent+op -> renew, not duplicate
    assert c1.id == c2.id
    assert len(cp.service_roles.list_active_claims(role_id=role.id)) == 1


def test_losing_the_claim_race_returns_the_winners_claim(monkeypatch):
    """Two hosts can both read no active claim and both reach the INSERT.

    The partial unique index lets one win; the loser must get the winner's
    claim back, because losing that race is ordinary pool behaviour.

    test_one_agent_cannot_double_hold_an_op only ever exercises the sequential
    path -- its pre-check finds the existing claim and renews, so it never
    reaches the INSERT. That is why this stayed invisible: the recovery arm was
    written as `except StoreError`, and under Postgres the loser
    raises psycopg's UniqueViolation (wrapped as StoreError), so the arm could
    never run and a benign race became a hard failure of the split-brain guard.
    """
    cp = ControlPlane.in_memory()
    _seed(cp)
    a = _gpu_agent(cp, "natasha", capacity=5)
    role = cp.service_roles.get_role_by_slug("media:image.generate")
    svc = cp.service_roles
    winner = svc.claim_service(role.id, a.id)

    # Force the interleaving deterministically: the loser's pre-check sees no
    # active claim, exactly as it would had the winner committed just after,
    # so it proceeds into the INSERT and collides.
    real_active_claim = svc._active_claim
    calls = {"n": 0}

    def racing_precheck(role_id, agent_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_active_claim(role_id, agent_id)

    monkeypatch.setattr(svc, "_active_claim", racing_precheck)

    loser = svc.claim_service(role.id, a.id)

    assert loser.id == winner.id
    assert len(svc.list_active_claims(role_id=role.id)) == 1
    # The pre-check ran, then the post-conflict read resolved the winner.
    assert calls["n"] == 2


def test_reconcile_auto_seeds_from_env_and_signals_zero_holders(monkeypatch):
    cp = ControlPlane.in_memory()
    monkeypatch.setenv("MAC_SERVICE_ROLE_OPS", "image.generate, video.generate")
    out = cp.reconcile_service_roles()
    ops = {r.op for r in cp.service_roles.desired_services()}
    assert {"image.generate", "video.generate"} <= ops  # auto-seeded
    assert set(out["requested"]) == {"image.generate", "video.generate"}  # zero holders


def test_offline_holder_is_reaped_by_reconcile():
    cp = ControlPlane.in_memory()
    _seed(cp)
    a = _gpu_agent(cp, "natasha", capacity=5)
    cp.sync_agent_service_claims(a.id, ["image.generate"])
    assert cp.service_roles.held_ops_for_agent(a.id) == ["image.generate"]
    cp.heartbeat_agent(a.id, status="offline")  # agent goes offline -> claims expired
    assert cp.service_roles.held_ops_for_agent(a.id) == []


def test_sync_auto_seeds_roles_from_env(monkeypatch):
    """Self-driving: a worker sync seeds desired roles from MAC_SERVICE_ROLE_OPS
    (no /dispatch/tick needed) and then claims them."""
    cp = ControlPlane.in_memory()  # no explicit _seed
    monkeypatch.setenv("MAC_SERVICE_ROLE_OPS", "image.generate, audio.tts")
    a = _gpu_agent(cp, "natasha", capacity=5)
    res = cp.sync_agent_service_claims(a.id, ["image.generate", "audio.tts"])
    assert set(res["held"]) == {"image.generate", "audio.tts"}
    assert {r.op for r in cp.service_roles.desired_services()} >= {"image.generate", "audio.tts"}


def test_dispatch_hold_atomically_withdraws_and_blocks_service_claim_renewal():
    cp = ControlPlane.in_memory()
    _seed(cp)
    agent = _gpu_agent(cp, "held-natasha", capacity=5)
    initial = cp.sync_agent_service_claims(agent.id, ["image.generate"])
    assert initial["held"] == ["image.generate"]
    claim = cp.service_roles.list_active_claims(agent_id=agent.id)[0]

    cp.set_agent_dispatch_hold(agent.id, "fleet deployment")
    assert cp.service_roles.list_active_claims(agent_id=agent.id) == []
    blocked = cp.sync_agent_service_claims(
        agent.id, ["image.generate"], lease_seconds=7200
    )

    assert blocked["held"] == []
    assert blocked["eligible"] is False
    assert blocked["eligibility_reason"] == "agent_dispatch_held"
    assert cp.service_roles.list_active_claims(agent_id=agent.id) == []
    persisted = cp.store.query_one(
        "SELECT status, expires_at FROM service_claims WHERE id = ?", (claim.id,)
    )
    assert persisted["status"] == "released"
    assert persisted["expires_at"] == claim.expires_at


def test_dispatch_hold_cas_withdraws_service_claim_before_arm_returns():
    cp = ControlPlane.in_memory()
    _seed(cp)
    agent = _gpu_agent(cp, "cas-held-natasha", capacity=5)
    cp.sync_agent_service_claims(agent.id, ["image.generate"])

    changed, held = cp.acquire_agent_dispatch_hold(
        agent.id,
        "fleet epoch arm",
        expected_dispatch_hold=False,
    )

    assert changed is True
    assert held.dispatch_hold is True
    assert cp.service_roles.list_active_claims(agent_id=agent.id) == []


def test_successor_hold_epoch_withdraws_service_claims_and_rolls_them_back_atomically():
    cp = ControlPlane.in_memory()
    _seed(cp)
    agents = sorted(
        (
            _gpu_agent(cp, "successor-service-first", capacity=1),
            _gpu_agent(cp, "successor-service-second", capacity=1),
        ),
        key=lambda item: item.id,
    )
    cp.sync_agent_service_claims(agents[0].id, ["image.generate"])
    cp.sync_agent_service_claims(agents[1].id, ["audio.tts"])
    claims = [
        cp.service_roles.list_active_claims(agent_id=agent.id)[0]
        for agent in agents
    ]
    for index, agent in enumerate(agents):
        cp.store.execute(
            "UPDATE agents SET dispatch_hold = 1, dispatch_hold_reason = ? "
            "WHERE id = ?",
            ("deployment-%s" % index, agent.id),
        )

    transitioned = cp.release_agent_dispatch_holds_batch(
        [
            (agents[0].id, "deployment-0"),
            (agents[1].id, "deployment-1"),
        ],
        epoch_id="successor-service-commit",
        successor_reason="synchronized successor service hold",
    )
    assert all(agent.dispatch_hold is True for agent in transitioned)
    assert all(
        agent.dispatch_hold_reason == "synchronized successor service hold"
        for agent in transitioned
    )
    assert cp.service_roles.list_active_claims(agent_id=agents[0].id) == []
    assert cp.service_roles.list_active_claims(agent_id=agents[1].id) == []

    for index, (agent, claim) in enumerate(zip(agents, claims)):
        cp.store.execute(
            "UPDATE agents SET dispatch_hold = 1, dispatch_hold_reason = ? "
            "WHERE id = ?",
            ("rollback-deployment-%s" % index, agent.id),
        )
        cp.store.execute(
            "UPDATE service_claims SET status = 'active' WHERE id = ?",
            (claim.id,),
        )

    with pytest.raises(ValidationError, match="lost dispatch-hold ownership"):
        cp.release_agent_dispatch_holds_batch(
            [
                (agents[0].id, "rollback-deployment-0"),
                (agents[1].id, "stale-rollback-deployment-1"),
            ],
            epoch_id="successor-service-rollback",
            successor_reason="second synchronized successor service hold",
        )

    assert {
        row["status"]
        for row in cp.store.query_all(
            "SELECT status FROM service_claims WHERE id IN (?, ?)",
            (claims[0].id, claims[1].id),
        )
    } == {"active"}
    assert [
        cp.get_agent(agent.id).dispatch_hold_reason for agent in agents
    ] == ["rollback-deployment-0", "rollback-deployment-1"]


@pytest.mark.parametrize(
    ("status", "health_status", "reason"),
    (
        ("draining", "degraded", "agent_status_unavailable"),
        ("idle", "degraded", "agent_health_unavailable"),
    ),
)
def test_unavailable_agent_cannot_acquire_service_claim(
    status, health_status, reason
):
    cp = ControlPlane.in_memory()
    _seed(cp)
    agent = _gpu_agent(cp, "unavailable-%s" % reason, capacity=5)
    cp.heartbeat_agent(agent.id, status=status, health_status=health_status)

    blocked = cp.sync_agent_service_claims(agent.id, ["image.generate"])

    assert blocked["held"] == []
    assert blocked["eligible"] is False
    assert blocked["eligibility_reason"] == reason
    assert cp.service_roles.list_active_claims(agent_id=agent.id) == []


def test_service_claim_sync_waits_for_agent_hold_fence_before_renewing(tmp_path):
    """A hold winning the agent-row lock must defeat a stale concurrent sync."""

    # Both planes must see ONE database: the race under test is two writers
    # contending for the same agent row. Under SQLite that was one file path
    # opened twice; the equivalent is one schema opened twice, so the DSN is
    # created first and both stores attach to it. ephemeral_store() would give
    # each plane its own schema, and the race could never happen.
    dsn = ephemeral_dsn()
    owner_store = store_on(dsn)
    worker_store = store_on(dsn)
    owner = ControlPlane(owner_store, secret_key="s" * 32)
    worker = ControlPlane(worker_store, secret_key="s" * 32)
    _seed(owner)
    agent = _gpu_agent(owner, "racing-natasha", capacity=5)
    owner.sync_agent_service_claims(agent.id, ["image.generate"])
    original = owner.service_roles.list_active_claims(agent_id=agent.id)[0]
    started = threading.Event()
    finished = threading.Event()
    outcome = {}

    def sync_after_stale_read_window():
        started.set()
        try:
            outcome["result"] = worker.sync_agent_service_claims(
                agent.id, ["image.generate"], lease_seconds=7200
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            outcome["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(target=sync_after_stale_read_window, daemon=True)
    try:
        with owner_store.transaction() as conn:
            conn.execute(
                "UPDATE agents SET dispatch_hold = 1, dispatch_hold_reason = ?, "
                "dispatch_hold_at = updated_at WHERE id = ?",
                ("concurrent fleet deployment", agent.id),
            )
            thread.start()
            assert started.wait(timeout=1)
            # The worker cannot complete against pre-hold state while this
            # transaction owns the agent-row/write fence.
            assert finished.wait(timeout=0.05) is False

        assert finished.wait(timeout=5)
        thread.join(timeout=1)
        assert "error" not in outcome
        result = outcome["result"]
        assert result["eligible"] is False
        assert result["eligibility_reason"] == "agent_dispatch_held"
        assert result["held"] == []
        assert owner.service_roles.list_active_claims(agent_id=agent.id) == []
        persisted = owner_store.query_one(
            "SELECT status, expires_at FROM service_claims WHERE id = ?",
            (original.id,),
        )
        assert persisted["status"] == "released"
        assert persisted["expires_at"] == original.expires_at
    finally:
        if thread.is_alive():
            thread.join(timeout=6)
        worker_store.close()
        owner_store.close()


def test_service_holder_liveness_uses_dispatch_and_health_fence():
    cp = ControlPlane.in_memory()
    agent = _gpu_agent(cp, "holder-liveness", capacity=1)
    assert cp._service_holder_live(agent.id) is True

    cp.set_agent_dispatch_hold(agent.id, "operator maintenance")
    assert cp._service_holder_live(agent.id) is False
    cp.clear_agent_dispatch_hold(agent.id)
    cp.heartbeat_agent(agent.id, health_status=HealthStatus.DEGRADED.value)
    assert cp._service_holder_live(agent.id) is False
