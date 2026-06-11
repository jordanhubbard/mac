"""media-01 service-role election: claim/sync/reconcile lifecycle."""
from __future__ import annotations

from mac.services import ControlPlane

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
    a = _gpu_agent(cp, "hostc", capacity=2)
    res = cp.sync_agent_service_claims(a.id, ["image.generate", "audio.tts", "video.generate"])
    assert res["capacity"] == 2
    assert len(res["held"]) == 2  # capacity-bounded
    assert set(res["held"]).issubset({"image.generate", "audio.tts", "video.generate"})


def test_pool_spreads_across_hosts():
    cp = ControlPlane.in_memory()
    _seed(cp)
    a = _gpu_agent(cp, "hostc", capacity=2)
    b = _gpu_agent(cp, "hostb", capacity=2)
    willing = ["image.generate", "audio.tts", "audio.music", "video.generate"]
    held_a = set(cp.sync_agent_service_claims(a.id, willing)["held"])
    held_b = set(cp.sync_agent_service_claims(b.id, willing)["held"])
    assert len(held_a) == 2 and len(held_b) == 2
    # pool model: together they cover more ops than any one host (spread)
    assert len(held_a | held_b) >= 3


def test_cpu_agent_is_ineligible():
    cp = ControlPlane.in_memory()
    _seed(cp)
    m = cp.register_machine("hosta-host", resources={})
    a = cp.register_agent(
        m.id, "hosta", capabilities=["python"],
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
    a = _gpu_agent(cp, "hostc", capacity=5)
    cp.sync_agent_service_claims(a.id, ["image.generate", "audio.tts"])
    assert set(cp.service_roles.held_ops_for_agent(a.id)) == {"image.generate", "audio.tts"}
    cp.sync_agent_service_claims(a.id, ["image.generate"])  # drop audio.tts
    assert set(cp.service_roles.held_ops_for_agent(a.id)) == {"image.generate"}


def test_expire_reopens_and_reconcile_signals_zero_holders():
    cp = ControlPlane.in_memory()
    _seed(cp)
    a = _gpu_agent(cp, "hostc", capacity=5)
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
    a = _gpu_agent(cp, "hostc", capacity=5)
    role = cp.service_roles.get_role_by_slug("media:image.generate")
    c1 = cp.service_roles.claim_service(role.id, a.id)
    c2 = cp.service_roles.claim_service(role.id, a.id)  # same agent+op -> renew, not duplicate
    assert c1.id == c2.id
    assert len(cp.service_roles.list_active_claims(role_id=role.id)) == 1


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
    a = _gpu_agent(cp, "hostc", capacity=5)
    cp.sync_agent_service_claims(a.id, ["image.generate"])
    assert cp.service_roles.held_ops_for_agent(a.id) == ["image.generate"]
    cp.heartbeat_agent(a.id, status="offline")  # agent goes offline -> claims expired
    assert cp.service_roles.held_ops_for_agent(a.id) == []


def test_sync_auto_seeds_roles_from_env(monkeypatch):
    """Self-driving: a worker sync seeds desired roles from MAC_SERVICE_ROLE_OPS
    (no /dispatch/tick needed) and then claims them."""
    cp = ControlPlane.in_memory()  # no explicit _seed
    monkeypatch.setenv("MAC_SERVICE_ROLE_OPS", "image.generate, audio.tts")
    a = _gpu_agent(cp, "hostc", capacity=5)
    res = cp.sync_agent_service_claims(a.id, ["image.generate", "audio.tts"])
    assert set(res["held"]) == {"image.generate", "audio.tts"}
    assert {r.op for r in cp.service_roles.desired_services()} >= {"image.generate", "audio.tts"}
