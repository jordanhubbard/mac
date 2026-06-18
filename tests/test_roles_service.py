import pytest

from mac.models import NotFoundError, ValidationError
from mac.roles_service import machine_hardware_satisfies
from mac.services import ControlPlane
from tests.conftest import bind_soul


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _register_machine(cp, hostname="host-1", hardware=None):
    return cp.register_machine(hostname, hardware=hardware or {})


def test_create_and_get_role_resolves_by_id_or_slug(cp):
    role = cp.roles.create_role(
        slug="code-reviewer",
        name="Code Reviewer",
        description="Reviews code for correctness, security, fit.",
        system_prompt="You are the second pair of eyes.",
        level="ic",
        default_capabilities=["review", "python"],
    )
    assert cp.roles.get_role(role.id).id == role.id
    assert cp.roles.get_role("code-reviewer").id == role.id


def test_create_role_validates_required_fields_and_level(cp):
    with pytest.raises(ValidationError):
        cp.roles.create_role(
            slug="bad slug", name="x", description="x", system_prompt="x", level="ic"
        )
    with pytest.raises(ValidationError):
        cp.roles.create_role(
            slug="ok", name="", description="x", system_prompt="x", level="ic"
        )
    with pytest.raises(ValidationError):
        cp.roles.create_role(
            slug="ok", name="x", description="x", system_prompt="x", level="emperor"
        )


def test_update_role_preserves_unmentioned_fields(cp):
    role = cp.roles.create_role(
        slug="qa",
        name="QA Engineer",
        description="Tests stuff.",
        system_prompt="Find bugs.",
        level="ic",
        specialties=["e2e", "regression"],
    )
    updated = cp.roles.update_role(role.id, description="Tests everything.")
    assert updated.description == "Tests everything."
    assert updated.specialties == ["e2e", "regression"]
    assert updated.system_prompt == "Find bugs."


def test_delete_role_refuses_when_agent_assigned(cp):
    role = cp.roles.create_role(
        slug="ops",
        name="Ops",
        description="d",
        system_prompt="p",
        level="ic",
        default_capabilities=["ops"],
    )
    machine = _register_machine(cp)
    soul = bind_soul(cp, persona_name="Ops Soul", allowed_role_slugs=["ops"])
    agent = cp.register_agent(machine.id, "worker", hermes_instance_id=soul)
    cp.roles.assign_role(agent.id, role.id)
    with pytest.raises(ValidationError):
        cp.roles.delete_role(role.id)
    # Unassign clears the reference and delete now succeeds.
    cp.roles.unassign_role(agent.id)
    cp.roles.delete_role(role.id)
    with pytest.raises(NotFoundError):
        cp.roles.get_role(role.id)


def test_assign_role_merges_default_capabilities_into_agent(cp):
    role = cp.roles.create_role(
        slug="devops",
        name="DevOps",
        description="d",
        system_prompt="p",
        level="ic",
        default_capabilities=["ops", "ci"],
    )
    machine = _register_machine(cp)
    soul = bind_soul(cp, persona_name="DevOps Soul", allowed_role_slugs=["devops"])
    agent = cp.register_agent(
        machine.id, "rocky", capabilities=["python"], hermes_instance_id=soul
    )
    refreshed = cp.roles.assign_role(agent.id, "devops")
    assert refreshed.role_id == role.id
    assert set(refreshed.capabilities) == {"python", "ops", "ci"}


def test_assign_role_rejects_when_hardware_mismatch(cp):
    role = cp.roles.create_role(
        slug="gpu-runner",
        name="GPU Runner",
        description="d",
        system_prompt="p",
        level="ic",
        hardware_requirements={"cpu_arch": ["arm64"], "memory_gb_min": 32},
    )
    soul = bind_soul(cp, persona_name="GPU Soul", allowed_role_slugs=["gpu-runner"])
    cpu_only = _register_machine(
        cp,
        hostname="cpu-only",
        hardware={"cpu_arch": "x86_64", "memory_gb": 16},
    )
    agent = cp.register_agent(cpu_only.id, "wrong-host", hermes_instance_id=soul)
    with pytest.raises(ValidationError) as exc:
        cp.roles.assign_role(agent.id, role.id)
    assert "cpu_arch" in str(exc.value) or "memory_gb" in str(exc.value)

    fit = _register_machine(
        cp,
        hostname="arm-big",
        hardware={"cpu_arch": "arm64", "memory_gb": 64},
    )
    agent2 = cp.register_agent(fit.id, "right-host", hermes_instance_id=soul)
    cp.roles.assign_role(agent2.id, role.id)  # no error


def test_machine_hardware_satisfies_handles_accelerators_and_tags():
    req = {
        "accelerators": [
            {"kind": "gpu", "vendor": "nvidia", "memory_gb_min": 40, "count_min": 1}
        ],
        "tags_all": ["dgx"],
    }
    ok, _ = machine_hardware_satisfies(
        req,
        {
            "tags": ["dgx", "internal"],
            "accelerators": [
                {"kind": "gpu", "vendor": "nvidia", "memory_gb": 80, "count": 8}
            ],
        },
    )
    assert ok

    bad_ok, reasons = machine_hardware_satisfies(
        req,
        {"tags": ["internal"], "accelerators": [{"kind": "gpu", "vendor": "amd"}]},
    )
    assert not bad_ok
    assert any("tags" in r for r in reasons) or any("accelerator" in r for r in reasons)


def test_seed_defaults_loads_thirteen_loom_roles_idempotent(cp):
    rows = cp.roles.seed_defaults()
    slugs = sorted(role.slug for role in rows)
    # CEO/CFO/CTO removed; 13 of loom's 16 personas remain.
    assert "ceo" not in slugs
    assert "code-reviewer" in slugs
    assert len(slugs) == 13

    # Idempotent: a second call doesn't duplicate, and replace=False
    # leaves existing rows alone.
    rows_again = cp.roles.seed_defaults()
    assert {r.slug for r in rows_again} == set(slugs)

    listed = cp.roles.list_roles(include_defaults=True)
    assert len(listed) == 13


def test_soulless_agent_refuses_any_role_assignment(cp):
    """Agents without a hermes_instance_id have no soul. Per the
    layering rule (soul takes precedence over role), they cannot take
    any role — the work waits for an agent whose soul accepts it."""
    cp.roles.create_role(
        slug="qa",
        name="QA",
        description="d",
        system_prompt="p",
        level="ic",
    )
    machine = _register_machine(cp)
    agent = cp.register_agent(machine.id, "soulless")
    assert agent.hermes_instance_id is None
    with pytest.raises(ValidationError) as exc:
        cp.roles.assign_role(agent.id, "qa")
    assert "no soul" in str(exc.value).lower()


def test_designer_souled_agent_refuses_qa_role(cp):
    """The user's concrete example: a designer soul cannot take a QA
    role. The task is simply left unclaimed for a QA-souled agent to
    pick up later."""
    cp.roles.create_role(slug="qa", name="QA", description="d", system_prompt="p", level="ic")
    cp.roles.create_role(slug="design", name="Design", description="d", system_prompt="p", level="ic")
    machine = _register_machine(cp)
    designer_soul = bind_soul(
        cp,
        persona_name="Web Designer Soul",
        allowed_role_slugs=["design"],
    )
    agent = cp.register_agent(machine.id, "designer", hermes_instance_id=designer_soul)
    with pytest.raises(ValidationError) as exc:
        cp.roles.assign_role(agent.id, "qa")
    assert "soul does not accept" in str(exc.value).lower()
    # Same agent CAN take its on-soul role.
    refreshed = cp.roles.assign_role(agent.id, "design")
    assert refreshed.role_id is not None


def test_persona_role_slugs_defaults_from_persona_name(cp):
    """When ``metadata.role_slugs`` is absent, the persona's name
    (slugified) is the default — loom personas map 1-to-1 to roles by
    name, so the default is exactly right for the seeded fleet."""
    cp.roles.create_role(
        slug="code-reviewer",
        name="Code Reviewer",
        description="d",
        system_prompt="p",
        level="ic",
    )
    machine = _register_machine(cp)
    # No metadata.role_slugs — default to slugify(persona.name) =
    # "code-reviewer".
    soul = bind_soul(cp, persona_name="Code Reviewer")
    agent = cp.register_agent(machine.id, "rocky", hermes_instance_id=soul)
    refreshed = cp.roles.assign_role(agent.id, "code-reviewer")
    assert refreshed.role_id is not None


def test_dispatch_skips_agent_whose_soul_no_longer_accepts_role(cp):
    """Compatibility is re-checked at dispatch time: editing the
    persona's allowed list immediately stops affected agents from being
    eligible for required_role tasks (they keep the role assignment
    until explicitly unassigned, but dispatch refuses them)."""
    cp.roles.create_role(
        slug="qa",
        name="QA",
        description="d",
        system_prompt="p",
        level="ic",
        default_capabilities=["python"],
    )
    machine = _register_machine(cp)
    soul = bind_soul(cp, persona_name="QA Soul", allowed_role_slugs=["qa"])
    agent = cp.register_agent(
        machine.id, "rocky", capabilities=["python"], hermes_instance_id=soul
    )
    cp.roles.assign_role(agent.id, "qa")

    # Tighten the persona's allowed list to a different slug — agent
    # still holds the qa role but is now incompatible.
    instance = cp.identity.get_hermes_instance(soul)
    persona = cp.identity.get_persona(instance.persona_id)
    cp.store.execute(
        "UPDATE personas SET metadata = ? WHERE id = ?",
        ('{"role_slugs": ["design"]}', persona.id),
    )

    cp.create_task("py", required_capabilities=["python"], metadata={"required_role": "qa"})
    assert cp.dispatch_once(lease_seconds=300) is None


def test_register_agent_preserves_hermes_instance_id_across_reregistration(cp):
    """An ops re-register that doesn't pass hermes_instance_id must not
    orphan the agent from its soul. The column is preserved unless the
    caller explicitly passes a new value."""
    machine = _register_machine(cp)
    soul = bind_soul(cp, persona_name="QA Soul", allowed_role_slugs=["qa"])
    agent = cp.register_agent(machine.id, "rocky", hermes_instance_id=soul)
    assert agent.hermes_instance_id == soul

    # Re-register without hermes_instance_id — value preserved.
    again = cp.register_agent(
        machine.id, "rocky", capabilities=["python"], agent_id=agent.id
    )
    assert again.hermes_instance_id == soul

    # Re-register WITH a new hermes_instance_id — value updated.
    new_soul = bind_soul(
        cp,
        persona_name="Other Soul",
        tenant_name="other-tenant",
        allowed_role_slugs=["qa"],
    )
    rebound = cp.register_agent(
        machine.id, "rocky", agent_id=agent.id, hermes_instance_id=new_soul
    )
    assert rebound.hermes_instance_id == new_soul


def test_hermes_instance_without_persona_refuses_role_assignment(cp):
    """A hermes_instance with no persona_id has no soul to consult, so
    no role can be assigned through it. The agent has an instance ref
    but isn't a complete persona — refused."""
    cp.roles.create_role(slug="qa", name="QA", description="d", system_prompt="p", level="ic")
    tenant = cp.register_tenant("empty-tenant")
    # Hermes instance with no persona attached.
    instance = cp.register_hermes_instance(tenant.id, "personaless")
    machine = _register_machine(cp)
    agent = cp.register_agent(machine.id, "rocky", hermes_instance_id=instance.id)
    with pytest.raises(ValidationError) as exc:
        cp.roles.assign_role(agent.id, "qa")
    assert "soul does not accept" in str(exc.value).lower()


def test_explicit_empty_role_slugs_refuses_all_roles(cp):
    """An empty list ``metadata.role_slugs=[]`` means the soul exists
    but accepts no roles — different from absence (which defaults to
    persona-name). Explicit empty refuses every role."""
    cp.roles.create_role(slug="qa", name="QA", description="d", system_prompt="p", level="ic")
    cp.roles.create_role(slug="design", name="Design", description="d", system_prompt="p", level="ic")
    machine = _register_machine(cp)
    soul = bind_soul(
        cp,
        persona_name="No-Roles Soul",
        allowed_role_slugs=[],  # explicit empty
    )
    agent = cp.register_agent(machine.id, "rocky", hermes_instance_id=soul)
    with pytest.raises(ValidationError):
        cp.roles.assign_role(agent.id, "qa")
    with pytest.raises(ValidationError):
        cp.roles.assign_role(agent.id, "design")


def test_dispatch_refuses_role_id_smuggled_in_via_raw_db_write(cp):
    """Even if a misconfigured operator hand-edits agents.role_id to a
    role incompatible with the agent's soul, dispatch re-checks
    compatibility every match — so the agent never claims a task that
    asks for that role."""
    cp.roles.create_role(
        slug="qa",
        name="QA",
        description="d",
        system_prompt="p",
        level="ic",
    )
    role = cp.roles.create_role(
        slug="design",
        name="Design",
        description="d",
        system_prompt="p",
        level="ic",
    )
    machine = _register_machine(cp)
    qa_soul = bind_soul(cp, persona_name="QA Soul", allowed_role_slugs=["qa"])
    agent = cp.register_agent(machine.id, "rocky", hermes_instance_id=qa_soul)
    # Hand-edit role_id to "design" — bypasses assign_role's compat check.
    cp.store.execute(
        "UPDATE agents SET role_id = ? WHERE id = ?", (role.id, agent.id)
    )
    cp.create_task("d-work", metadata={"required_role": "design"})
    # Dispatch's compat re-check refuses the agent even though
    # agents.role_id literally says "design".
    assert cp.dispatch_once(lease_seconds=300) is None


def test_agent_identity_returns_layered_view(cp):
    """The layered identity (soul -> role -> mood -> hardware) is
    returned as separate fields; callers compose the LLM prompt
    themselves."""
    cp.roles.create_role(
        slug="qa",
        name="QA",
        description="d",
        system_prompt="Run tests.",
        level="ic",
        default_capabilities=["python"],
    )
    machine = cp.register_machine(
        "host-id", hardware={"cpu_arch": "arm64", "memory_gb": 32}
    )
    soul = bind_soul(cp, persona_name="QA Soul", allowed_role_slugs=["qa"])
    agent = cp.register_agent(machine.id, "rocky", hermes_instance_id=soul)
    cp.roles.assign_role(agent.id, "qa")
    cp.set_mood(agent.id, "cheerful", reason="working")

    identity = cp.agent_identity(agent.id)
    assert identity["agent"]["id"] == agent.id
    assert identity["soul"]["persona"]["name"] == "QA Soul"
    assert identity["allowed_role_slugs"] == ["qa"]
    assert identity["role"]["slug"] == "qa"
    assert identity["mood"]["mode"] == "cheerful"
    assert identity["machine_hardware"]["cpu_arch"] == "arm64"


def test_tenant_scoped_role_shadows_global_default(cp):
    tenant = cp.register_tenant("alpha")
    cp.roles.create_role(
        slug="reviewer",
        name="Global Reviewer",
        description="d",
        system_prompt="global",
        level="ic",
    )
    cp.roles.create_role(
        slug="reviewer",
        name="Alpha Reviewer",
        description="d",
        system_prompt="alpha-specific",
        level="ic",
        tenant_id=tenant.id,
    )
    # Tenant-scoped lookup prefers the tenant copy.
    by_tenant = cp.roles.get_role("reviewer", tenant_id=tenant.id)
    assert by_tenant.system_prompt == "alpha-specific"
    # Without a tenant, fall back to the global.
    assert cp.roles.get_role("reviewer").system_prompt == "global"
