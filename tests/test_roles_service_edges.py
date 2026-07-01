"""Validation and hardware-matcher coverage for agent roles."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from mac.models import NotFoundError, ValidationError
from mac.roles_service import _accelerator_matches, machine_hardware_satisfies
from mac.services import ControlPlane


def _create(service, **extra):
    values = {
        "slug": "role",
        "name": "Role",
        "description": "Description",
        "system_prompt": "Prompt",
        "level": "ic",
    }
    values.update(extra)
    return service.create_role(**values)


def test_create_role_remaining_required_fields_and_parent(monkeypatch) -> None:
    service = ControlPlane.in_memory().roles
    with pytest.raises(ValidationError, match="description"):
        _create(service, description="")
    with pytest.raises(ValidationError, match="system_prompt"):
        _create(service, system_prompt="")
    tenant = ControlPlane.in_memory().register_tenant("tenant")
    # Tenant validation happens before persistence.
    monkeypatch.setattr(service, "_get_tenant", lambda *_a: (_ for _ in ()).throw(NotFoundError("tenant")))
    with pytest.raises(NotFoundError):
        _create(service, tenant_id=tenant.id)
    parent = _create(service, slug="parent")
    child = _create(service, slug="child", reports_to=parent.id)
    assert child.reports_to == parent.id
    updated = _create(service, slug="child", description="Updated")
    assert updated.id == child.id and updated.description == "Updated"


def test_list_roles_query_variants(monkeypatch) -> None:
    service = ControlPlane.in_memory().roles
    calls = []
    monkeypatch.setattr(service.store, "query_all", lambda sql, params: calls.append((sql, params)) or [])
    assert service.list_roles(tenant_id="tenant", include_defaults=True) == []
    assert "OR tenant_id IS NULL" in calls[-1][0]
    assert service.list_roles(tenant_id="tenant", include_defaults=False, level="IC") == []
    assert "tenant_id = ?" in calls[-1][0] and calls[-1][1][-1] == "ic"
    assert service.list_roles(level="manager") == []


@pytest.mark.parametrize(
    "requirements",
    [
        "bad",
        {"cpu_count_min": -1},
        {"memory_gb_min": "large"},
        {"os": "linux"},
        {"cpu_arch": ["arm64", 3]},
        {"tags_all": "gpu"},
        {"accelerators": {}},
    ],
)
def test_hardware_requirement_schema_validation(requirements) -> None:
    service = ControlPlane.in_memory().roles
    with pytest.raises(ValidationError, match="hardware_requirements"):
        service._validate_hardware_requirements(requirements)
    assert service._validate_hardware_requirements({"future": {"anything": True}})


def test_soul_role_lookup_missing_identity_and_persona(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("host")
    agent = cp.register_agent(machine.id, "agent")
    assert cp.roles._allowed_role_slugs_for(agent) is None
    attached = replace(agent, hermes_instance_id="hermes")
    monkeypatch.setattr(cp.roles, "_get_hermes_instance", lambda *_a: (_ for _ in ()).throw(NotFoundError()))
    assert cp.roles._allowed_role_slugs_for(attached) is None
    monkeypatch.setattr(cp.roles, "_get_hermes_instance", lambda *_a: SimpleNamespace(persona_id="persona"))
    monkeypatch.setattr(cp.roles, "_get_persona", lambda *_a: (_ for _ in ()).throw(NotFoundError()))
    assert cp.roles._allowed_role_slugs_for(attached) == []
    monkeypatch.setattr(cp.roles, "_get_persona", lambda *_a: SimpleNamespace(metadata="bad", name=""))
    assert cp.roles._allowed_role_slugs_for(attached) == []


def test_hardware_matcher_all_mismatch_and_coercion_paths() -> None:
    assert machine_hardware_satisfies({}, {}) == (True, [])
    ok, reasons = machine_hardware_satisfies(
        {
            "os": ["linux"],
            "cpu_arch": ["arm64"],
            "cpu_count_min": "bad",
            "memory_gb_min": 32,
            "disk_gb_min": 100,
            "tags_all": ["gpu", "trusted"],
            "accelerators": ["bad", {"kind": "gpu", "memory_gb_min": 80}],
        },
        {"os": "darwin", "cpu_arch": "x86", "memory_gb": "bad", "disk_gb": 10, "tags": ["gpu"], "accelerators": "bad"},
    )
    assert ok is False
    assert len(reasons) >= 6


def test_accelerator_match_field_numeric_and_count_paths() -> None:
    assert _accelerator_matches({}, "bad") is False
    assert _accelerator_matches({"kind": "gpu"}, {"kind": "cpu"}) is False
    assert _accelerator_matches({"vendor": "nvidia"}, {"vendor": "amd"}) is False
    assert _accelerator_matches({"model": "h100"}, {"model": "a100"}) is False
    assert _accelerator_matches({"memory_gb_min": 80}, {"memory_gb": 40}) is False
    assert _accelerator_matches({"memory_gb_min": "bad"}, {"memory_gb": 80}) is False
    assert _accelerator_matches({"count_min": 2}, {"count": 1}) is False
    assert _accelerator_matches({"count_min": "bad"}, {"count": 2}) is False
    assert _accelerator_matches(
        {"kind": "gpu", "vendor": "nvidia", "memory_gb_min": 40, "count_min": 1},
        {"kind": "gpu", "vendor": "nvidia", "memory_gb": 80, "count": 8},
    ) is True


def test_seed_defaults_missing_catalog_and_parent_link(monkeypatch, tmp_path) -> None:
    service = ControlPlane.in_memory().roles
    with pytest.raises(NotFoundError, match="catalog missing"):
        service.seed_defaults(source=tmp_path / "missing")
    source = tmp_path / "roles.json"
    source.write_text(json.dumps([
        {"slug": "child", "name": "Child", "reports_to": "parent"},
        {"slug": "parent", "name": "Parent"},
    ]))
    roles = service.seed_defaults(source=source)
    by_slug = {role.slug: role for role in roles}
    assert service.get_role(by_slug["child"].id).reports_to == by_slug["parent"].id
