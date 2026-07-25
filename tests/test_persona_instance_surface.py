"""Targeted tests for the PersonaInstance service, API, notifier, and role surface.

These cover the runtime-neutral persona-instance rename of the identity service,
ControlPlane, HTTP routes, notifier routing, provenance keyword, and the
soul-role linkage, plus the one-release backward-compatible aliases retained for
the pre-persona ``hermes``-named call sites.
"""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from mac.api import (
    PersonaInstanceRegister,
    PersonaRuntimeProofCreate,
    HermesInstanceRegister,
    HermesRuntimeProofCreate,
    create_app,
)
from mac.models import PersonaInstance
from mac.services import ControlPlane


def _cp() -> ControlPlane:
    return ControlPlane.in_memory()


# Identity service ---------------------------------------------------------

def test_identity_persona_instance_roundtrip():
    cp = _cp()
    tenant = cp.register_tenant("acme")
    instance = cp.identity.register_persona_instance(tenant.id, "rocky")
    assert isinstance(instance, PersonaInstance)
    assert cp.identity.get_persona_instance(instance.id).id == instance.id
    listed = cp.identity.list_persona_instances(tenant.id)
    assert [i.id for i in listed] == [instance.id]


def test_identity_persona_context_exposes_both_keys():
    cp = _cp()
    tenant = cp.register_tenant("acme")
    instance = cp.identity.register_persona_instance(tenant.id, "rocky")
    context = cp.identity.persona_context(instance.id)
    # New runtime-neutral key plus the retained backward-compatible key.
    assert context["persona_instance"]["id"] == instance.id
    assert context["hermes_instance"]["id"] == instance.id


def test_identity_hermes_aliases_delegate_to_persona():
    cp = _cp()
    tenant = cp.register_tenant("acme")
    instance = cp.identity.register_hermes_instance(tenant.id, "rocky")
    assert cp.identity.get_hermes_instance(instance.id).id == instance.id
    assert cp.identity.hermes_context(instance.id)["persona_instance"]["id"] == instance.id


def test_platform_binding_accepts_persona_instance_id_keyword():
    cp = _cp()
    tenant = cp.register_tenant("acme")
    instance = cp.identity.register_persona_instance(tenant.id, "rocky")
    binding = cp.identity.register_platform_binding(
        tenant.id, persona_instance_id=instance.id, platform="slack", external_id="U1"
    )
    assert binding.persona_instance_id == instance.id
    # Listing by the persona keyword and the deprecated keyword both work.
    assert cp.identity.list_platform_bindings(persona_instance_id=instance.id)
    assert cp.identity.list_platform_bindings(hermes_instance_id=instance.id)


# ControlPlane -------------------------------------------------------------

def test_control_plane_persona_delegators():
    cp = _cp()
    tenant = cp.register_tenant("acme")
    instance = cp.register_persona_instance(tenant.id, "rocky")
    assert cp.get_persona_instance(instance.id).id == instance.id
    assert [i.id for i in cp.list_persona_instances()] == [instance.id]
    assert cp.persona_context(instance.id)["persona_instance"]["id"] == instance.id
    work = cp.persona_work_context(instance.id)
    assert work["authority"]["tasks"] == "mac"


# HTTP routes --------------------------------------------------------------

def _client(cp: ControlPlane) -> TestClient:
    return TestClient(create_app(control_plane=cp))


def test_persona_instance_routes():
    cp = _cp()
    client = _client(cp)
    tenant = client.post("/tenants", json={"name": "acme"}).json()
    instance = client.post(
        "/persona-instances",
        json={"tenant_id": tenant["id"], "name": "rocky"},
    ).json()
    listed = client.get("/persona-instances").json()
    assert any(item["id"] == instance["id"] for item in listed)
    context = client.get("/persona-instances/%s/context" % instance["id"]).json()
    assert context["persona_instance"]["id"] == instance["id"]
    work = client.get("/persona-instances/%s/work-context" % instance["id"]).json()
    assert work["authority"]["tasks"] == "mac"


def test_old_hermes_instance_routes_removed():
    cp = _cp()
    client = _client(cp)
    tenant = client.post("/tenants", json={"name": "acme"}).json()
    # Old live route is gone after the one-release migration boundary.
    resp = client.post(
        "/hermes-instances",
        json={"tenant_id": tenant["id"], "name": "rocky"},
    )
    assert resp.status_code == 404


def test_platform_binding_route_accepts_persona_instance_id():
    cp = _cp()
    client = _client(cp)
    tenant = client.post("/tenants", json={"name": "acme"}).json()
    instance = client.post(
        "/persona-instances", json={"tenant_id": tenant["id"], "name": "rocky"}
    ).json()
    resp = client.post(
        "/platform-bindings",
        json={
            "tenant_id": tenant["id"],
            "persona_instance_id": instance["id"],
            "platform": "slack",
            "external_id": "U1",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["persona_instance_id"] == instance["id"]


def test_request_type_aliases_are_persona_types():
    assert HermesInstanceRegister is PersonaInstanceRegister
    assert HermesRuntimeProofCreate is PersonaRuntimeProofCreate


# Provenance ---------------------------------------------------------------

def test_action_event_accepts_persona_instance_id_keyword():
    cp = _cp()
    event = cp.action_events.record_action_event(
        persona_instance_id="persona_1",
        action_type="test",
        action_name="probe",
    )
    # Persisted column remains hermes_instance_id; persona alias reads it back.
    assert event.hermes_instance_id == "persona_1"
    assert event.persona_instance_id == "persona_1"


# Roles / agent linkage ----------------------------------------------------

def test_soul_role_linkage_uses_persona_instance_id(monkeypatch):
    cp = _cp()
    machine = cp.register_machine("host")
    agent = cp.register_agent(machine.id, "agent")
    assert agent.persona_instance_id is None
    linked = replace(agent, hermes_instance_id="persona_1")
    assert linked.persona_instance_id == "persona_1"


def test_register_and_update_agent_accept_persona_instance_id_keyword():
    cp = _cp()
    tenant = cp.register_tenant("acme")
    persona = cp.register_persona(
        tenant.id, "concierge", "soul://concierge", "memory://concierge"
    )
    first = cp.register_persona_instance(tenant.id, "rocky", persona_id=persona.id)
    second = cp.register_persona_instance(tenant.id, "boris", persona_id=persona.id)
    machine = cp.register_machine("host")
    # register_agent binds via the runtime-neutral keyword.
    agent = cp.register_agent(
        machine.id, "rocky-agent", persona_instance_id=first.id
    )
    assert agent.persona_instance_id == first.id
    # update_agent rebinds via the runtime-neutral keyword.
    updated = cp.update_agent(agent.id, persona_instance_id=second.id)
    assert updated.persona_instance_id == second.id
    # The deprecated keyword still works during the migration boundary.
    rebound = cp.update_agent(agent.id, hermes_instance_id=first.id)
    assert rebound.persona_instance_id == first.id


# Notifier -----------------------------------------------------------------

def test_notifier_persona_instance_target_routing():
    cp = _cp()
    tenant = cp.register_tenant("acme")
    persona = cp.register_persona(
        tenant.id,
        "concierge",
        "soul://concierge",
        "memory://concierge",
    )
    instance = cp.register_persona_instance(tenant.id, "rocky", persona_id=persona.id)
    machine = cp.register_machine("host")
    agent = cp.register_agent(
        machine.id, "rocky-agent", hermes_instance_id=instance.id
    )
    notifiers = cp.notifiers
    targets = notifiers._agents_for_persona_instance(instance.id)
    assert [a.id for a in targets] == [agent.id]
