"""Soul preservation smoke test.

Demonstrates that the (tenant, persona, hermes_instance, platform_binding)
identity quadruple survives a simulated Hermes process loss. The recovery
contract is documented in docs/soul-preservation-runbook.md.
"""

from typing import Any, Dict, Optional

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.hermes_adapter import (
    ConversationTaskInput,
    HermesMacAdapter,
    MacApiClient,
    MacApiError,
    PlatformBindingSpec,
)
from mac.services import ControlPlane


def _api_transport(client: TestClient):
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


def test_soul_survives_hermes_process_loss():
    """Full pre-restart -> simulated loss -> post-restart identity continuity."""
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    adapter = HermesMacAdapter(MacApiClient("http://mac.test", transport=_api_transport(client)))

    # --- Pre-restart: register Rocky and attach two platform bindings.
    pre = adapter.register_identity(
        tenant_name="personal",
        persona_name="Rocky",
        instance_name="rocky",
        soul_ref="hermes://personal/rocky/SOUL.md",
        memory_scope="hermes://personal/rocky/memory",
        home_ref="hermes://personal/rocky",
        platform_bindings=[
            PlatformBindingSpec("slack", "T123/C456", "#ops"),
            PlatformBindingSpec("telegram", "chat-42", "@rocky"),
        ],
    )
    pre_tenant_id = pre["tenant"]["id"]
    pre_persona_id = pre["persona"]["id"]
    pre_instance_id = pre["hermes_instance"]["id"]
    pre_binding_ids = {b["id"] for b in pre["platform_bindings"]}
    pre_persona_soul = pre["persona"]["soul_ref"]
    pre_persona_memory = pre["persona"]["memory_scope"]

    # Stamp some operational state that should be preserved across the restart.
    adapter.create_task_from_conversation(
        pre_instance_id,
        ConversationTaskInput(
            title="pre-restart work",
            summary="A task created before the simulated Hermes process loss.",
            platform_binding_id=pre["platform_bindings"][0]["id"],
            conversation_ref="slack://T123/C456/1700000000.000100",
            required_capabilities=["ops"],
        ),
    )

    # --- Simulate Hermes process loss. The adapter is the only stateful client
    # of mac; we throw it away and any in-memory Hermes state with it.
    del adapter

    # --- Recovery payload comes from mac alone. A fresh Hermes process would
    # GET this on startup.
    context = client.get("/hermes-instances/%s/context" % pre_instance_id).json()
    assert context["memory_contract"]["personality_authority"] == "hermes"
    assert context["memory_contract"]["user_memory_authority"] == "hermes"
    assert context["persona"]["soul_ref"] == pre_persona_soul
    assert context["persona"]["memory_scope"] == pre_persona_memory
    assert {b["id"] for b in context["platform_bindings"]} == pre_binding_ids

    # --- Post-restart: a new Hermes process re-registers using the recovery
    # payload. The adapter is idempotent on (tenant_id, name) natural keys, so
    # all four identity ids must round-trip unchanged.
    fresh_adapter = HermesMacAdapter(
        MacApiClient("http://mac.test", transport=_api_transport(client))
    )
    post = fresh_adapter.register_identity(
        tenant_name=context["tenant"]["name"],
        persona_name=context["persona"]["name"],
        instance_name=context["hermes_instance"]["name"],
        soul_ref=context["persona"]["soul_ref"],
        memory_scope=context["persona"]["memory_scope"],
        home_ref=context["hermes_instance"]["home_ref"],
        platform_bindings=[
            PlatformBindingSpec(b["platform"], b["external_id"], b["display_name"])
            for b in context["platform_bindings"]
        ],
    )

    assert post["tenant"]["id"] == pre_tenant_id
    assert post["persona"]["id"] == pre_persona_id
    assert post["hermes_instance"]["id"] == pre_instance_id
    assert {b["id"] for b in post["platform_bindings"]} == pre_binding_ids

    # Pre-restart task is still attached to the same instance.
    pre_tasks = client.get("/tasks", params={"tenant_id": pre_tenant_id}).json()
    assert any(
        task["metadata"]["origin"]["hermes_instance_id"] == pre_instance_id
        for task in pre_tasks
    )

    # New conversations bind to the same persona via the same platform binding.
    new_task = fresh_adapter.create_task_from_conversation(
        post["hermes_instance"]["id"],
        ConversationTaskInput(
            title="post-restart work",
            summary="A task created after the simulated Hermes process loss.",
            platform_binding_id=post["platform_bindings"][0]["id"],
            conversation_ref="slack://T123/C456/1700000100.000200",
            required_capabilities=["ops"],
        ),
    )
    assert new_task["metadata"]["origin"]["persona_id"] == pre_persona_id


def test_reusing_tenant_name_glues_new_identity_onto_old_tenant_id():
    """The runbook warns against tenant-name reuse. This test pins the actual
    failure mode: re-registering "personal" with different downstream data
    upserts onto the same tenant id — the old tenant's persona/bindings stay
    attached. Operators reading the runbook should know what they'd see."""
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    adapter = HermesMacAdapter(MacApiClient("http://mac.test", transport=_api_transport(client)))

    first = adapter.register_identity(
        tenant_name="shared-name",
        persona_name="Rocky",
        instance_name="rocky",
        soul_ref="hermes://shared/rocky/SOUL.md",
        memory_scope="hermes://shared/rocky/memory",
    )
    # A different operator reuses the same tenant name for what they think is a
    # fresh tenant. mac upserts on (name UNIQUE), so this lands on the same id.
    second = adapter.register_identity(
        tenant_name="shared-name",
        persona_name="Natasha",
        instance_name="natasha",
        soul_ref="hermes://shared/natasha/SOUL.md",
        memory_scope="hermes://shared/natasha/memory",
    )
    assert second["tenant"]["id"] == first["tenant"]["id"]
    # Both personas now coexist under the shared tenant. Tombstone, don't recycle.
    personas = client.get("/personas", params={"tenant_id": first["tenant"]["id"]}).json()
    assert {p["name"] for p in personas} == {"Rocky", "Natasha"}


def test_reregistering_does_not_proliferate_personas_or_bindings():
    """Idempotency guarantee: calling register_identity in a tight loop produces
    exactly one persona and one binding per natural key."""
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    adapter = HermesMacAdapter(MacApiClient("http://mac.test", transport=_api_transport(client)))

    for _ in range(5):
        adapter.register_identity(
            tenant_name="team",
            persona_name="Natasha",
            instance_name="natasha",
            soul_ref="hermes://team/natasha/SOUL.md",
            memory_scope="hermes://team/natasha/memory",
            platform_bindings=[
                PlatformBindingSpec("slack", "T999/C000", "#deploys"),
            ],
        )

    tenants = client.get("/tenants").json()
    personas = client.get("/personas").json()
    instances = client.get("/hermes-instances").json()
    bindings = client.get("/platform-bindings").json()
    assert len([t for t in tenants if t["name"] == "team"]) == 1
    assert len([p for p in personas if p["name"] == "Natasha"]) == 1
    assert len([h for h in instances if h["name"] == "natasha"]) == 1
    assert len([b for b in bindings if b["external_id"] == "T999/C000"]) == 1
