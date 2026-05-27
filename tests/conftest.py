"""Shared pytest fixtures and helpers for the MAC test suite."""

from __future__ import annotations

from typing import Iterable, Optional

import pytest
from mac.services import ControlPlane


def pytest_collection_modifyitems(items: list) -> None:
    """Auto-apply category markers based on test subdirectory."""
    for item in items:
        path = str(item.fspath)
        if "/tests/api/" in path:
            item.add_marker(pytest.mark.api)
        elif "/tests/cli/" in path:
            item.add_marker(pytest.mark.cli)
        elif "/tests/ui/" in path:
            item.add_marker(pytest.mark.ui)


def submit_review_verdict(
    cp: ControlPlane,
    task_id: str,
    reviewer_agent_id: str,
    executor_evidence_id: str,
    *,
    verdict: str = "approved",
) -> str:
    """Produce the reviewer's signed verdict evidence (mac-jqb).

    The default-review workflow no longer auto-approves; it requires a
    separate Evidence row authored by the reviewer agent declaring an
    approve/reject verdict, signed with the reviewer's attestation
    key. Tests that want the workflow to advance to PUBLISHED must
    call this after submit_for_review.

    Returns the verdict evidence id.
    """
    from mac.services import sign_verification_manifest

    key = cp._agent_attestation_key(reviewer_agent_id)
    executor_evidence = cp.get_evidence(executor_evidence_id)
    executor_manifest = executor_evidence.metadata.get("verification") or {}
    repo = dict(executor_manifest.get("repo") or {})
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": verdict,
        "reviewed_evidence_id": executor_evidence_id,
        "repo": repo,
        "checks": [{"name": "reviewer independent verification", "returncode": 0}],
        "worktree_digest": "sha256:" + ("0" * 64),
    }
    manifest["signed_by"] = reviewer_agent_id
    manifest["signature"] = sign_verification_manifest(key, manifest)
    evidence = cp.add_evidence(
        task_id,
        "review",
        "artifact://verdict",
        "reviewer verdict: %s" % verdict,
        reviewer_agent_id,
        metadata={"returncode": 0, "verification": manifest},
    )
    return evidence.id


def bind_soul(
    cp: ControlPlane,
    *,
    persona_name: str = "Test Persona",
    allowed_role_slugs: Optional[Iterable[str]] = None,
    tenant_name: str = "test-tenant",
    instance_name: Optional[str] = None,
) -> str:
    """Create a tenant + persona + hermes instance and return the
    instance id.

    ``allowed_role_slugs`` controls the persona's metadata.role_slugs
    list — pass the slugs the soul should accept. If omitted, the
    persona's name (slugified) becomes the only allowed role (the loom
    default).

    Tests that need to assign a role to an agent should bind a soul
    first via this helper; agents without a soul refuse all role
    assignments by design.
    """
    tenant = cp.register_tenant(tenant_name)
    metadata = None
    if allowed_role_slugs is not None:
        metadata = {"role_slugs": [str(s) for s in allowed_role_slugs]}
    persona = cp.register_persona(
        tenant.id,
        persona_name,
        "hermes://%s/%s/SOUL.md" % (tenant_name, persona_name.lower()),
        "hermes://%s/%s/memory" % (tenant_name, persona_name.lower()),
        metadata=metadata,
    )
    instance = cp.register_hermes_instance(
        tenant.id,
        instance_name or "instance-%s" % persona_name.lower().replace(" ", "-"),
        persona_id=persona.id,
    )
    return instance.id
