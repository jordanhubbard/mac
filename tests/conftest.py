"""Shared pytest fixtures and helpers for the MAC test suite."""

from __future__ import annotations

import os
import uuid
from typing import Iterable, Iterator, Optional

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


# ----------------------------------------------------------------------
# Live-Postgres fixtures (K8s Phase 3.6).
#
# Opt-in via MAC_TEST_PG_URL. Tests marked `pytest.mark.postgres` skip
# cleanly when the env var is unset, so the default `pytest` run stays
# fast and SQLite-only. CI provisions a Postgres service container,
# sets MAC_TEST_PG_URL, and runs `pytest -m postgres` separately.
# ----------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.environ.get("MAC_TEST_PG_URL", "").strip()
    if not dsn:
        pytest.skip(
            "MAC_TEST_PG_URL is unset; live-Postgres tests are opt-in. "
            "Example: MAC_TEST_PG_URL=postgresql://postgres:test@127.0.0.1:5432/mac"
        )
    return dsn


@pytest.fixture()
def postgres_store(pg_dsn: str) -> Iterator[object]:
    """`PostgresStore` against a per-test schema with DDL applied.

    Each test runs inside an isolated PostgreSQL SCHEMA inside the shared
    MAC_TEST_PG_URL database. The schema is dropped on teardown so a
    re-run sees a clean namespace. Using schemas (not separate
    databases) keeps fixture cost low and the pool warm.
    """
    pytest.importorskip("psycopg")
    import psycopg

    from mac.store_postgres import PostgresStore

    schema = "mac_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in pg_dsn else "?"
    scoped_dsn = f"{pg_dsn}{sep}options=-csearch_path%3D{schema}"
    store = PostgresStore(scoped_dsn, pool_size=2, min_size=1)
    try:
        store.initialize()
        yield store
    finally:
        store.close()
        with psycopg.connect(pg_dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def submit_review_verdict(
    cp: ControlPlane,
    task_id: str,
    reviewer_agent_id: str,
    executor_evidence_id: str,
    *,
    verdict: str = "approved",
    feedback: str = "",
    summary: str = "",
    findings: Optional[list] = None,
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
    if feedback:
        manifest["feedback"] = feedback
    if summary:
        manifest["summary"] = summary
    if findings is not None:
        manifest["findings"] = findings
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
