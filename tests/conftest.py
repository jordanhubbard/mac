"""Shared pytest fixtures and helpers for the MAC test suite."""

from __future__ import annotations

import os
import uuid
from typing import Iterable, Iterator, Optional

import pytest
from mac.codegraph_audit import CODEGRAPH_AUDIT_SCHEMA, codegraph_relevant_files
from mac.services import ControlPlane


# Namespaces addressable by MAC_TEST_DISABLE_GROUPS. Each maps a group name to a
# predicate over the test's file path. Markers ARE the namespaces (the api/cli/ui
# dir markers below and the serial marks in pyproject already work this way); these
# entries just auto-tag the big filename clusters so an operator can switch a whole
# cluster off with one flag. Registered under [tool.pytest.ini_options].markers so
# they stay first-class and lintable.
_PATH_NAMESPACES = {
    "fleet": lambda p: "/tests/test_fleet_" in p or "/tests/test_deploy_fleet" in p,
    "work_package": lambda p: "/tests/test_work_package" in p,
    "worker": lambda p: "/tests/test_worker" in p,
    "heavy_e2e": lambda p: p.endswith("_e2e.py") or "/tests/test_documentation_book.py" in p,
}


def _disabled_groups() -> set[str]:
    """Namespaces the operator asked to switch off via MAC_TEST_DISABLE_GROUPS
    (comma-separated). Empty/unset => nothing disabled (default behaviour)."""
    raw = os.environ.get("MAC_TEST_DISABLE_GROUPS", "")
    return {g.strip() for g in raw.split(",") if g.strip()}


def pytest_collection_modifyitems(config, items: list) -> None:
    """Auto-apply category markers, then deselect any namespace the operator
    switched off via MAC_TEST_DISABLE_GROUPS.

    Disabling a namespace removes ITS coverage, so this is only safe on the
    non-gating fast-verify/rollout path — ``run-contract-tests.sh`` hard-refuses
    the flag unless MAC_TEST_COVERAGE=0, keeping the merge gate exhaustive.
    Disabled tests are *deselected* (not collected into the run) so they cost
    zero wall-clock, rather than reported as skipped.
    """
    for item in items:
        path = str(item.fspath)
        if "/tests/api/" in path:
            item.add_marker(pytest.mark.api)
        elif "/tests/cli/" in path:
            item.add_marker(pytest.mark.cli)
        elif "/tests/ui/" in path:
            item.add_marker(pytest.mark.ui)
        for namespace, matches in _PATH_NAMESPACES.items():
            if matches(path):
                item.add_marker(getattr(pytest.mark, namespace))

    disabled = _disabled_groups()
    if not disabled:
        return

    kept: list = []
    dropped: list = []
    for item in items:
        markers = {mark.name for mark in item.iter_markers()}
        (dropped if markers & disabled else kept).append(item)

    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept


@pytest.fixture(autouse=True)
def _mac_cli_json_output():
    """The `mac` CLI now defaults to human-readable text (one-liners); `--json`
    switches to JSON. The suite asserts the JSON contract (json.loads of CLI
    stdout), so force JSON output for every test. Production default stays text.
    The pure text renderer is exercised directly via `cli._render_text` tests,
    which don't go through this flag."""
    try:
        from mac import cli as _mac_cli

        _mac_cli._set_output_json(True)
        yield
        _mac_cli._set_output_json(False)
    except Exception:
        yield


@pytest.fixture(autouse=True)
def _no_ticket_mirror(monkeypatch):
    """Stop any test that drives `mac task create/close` from auto-emitting real
    `.tickets/<id>.md` files into the working repo.

    `tickets_mirror.emit()` targets the git repo root's `.tickets/` dir, so a
    CLI-driven test (e.g. tests/test_dispatch.py's hub create/close cases) would
    otherwise litter version control with throwaway mirrors like `task_xyz.md`
    / `task_remote_1.md`. Suite-wide so it covers every test dir, not just
    tests/cli/. Dedicated emit tests opt back in by deleting this var and
    pointing `tickets_dir` at a tmp directory.
    """
    monkeypatch.setenv("MAC_NO_TICKET_MIRROR", "1")


# ----------------------------------------------------------------------
# Live-Postgres fixtures (K8s Phase 3.6).
#
# Opt-in via MAC_TEST_PG_URL. Tests marked `pytest.mark.postgres` skip
# cleanly when the env var is unset, so the default `pytest` run stays
# fast and SQLite-only. CI provisions a Postgres service container,
# sets MAC_TEST_PG_URL, and runs `pytest -m postgres` separately.
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _drop_ephemeral_schemas() -> Iterator[None]:
    """Drop the per-test Postgres schemas created during this test.

    ControlPlane.in_memory() creates a schema per call. Without this sweep they
    survive for the life of the database and a full run leaves thousands of
    them behind, which makes the next run slower and the database unreadable.
    """
    from mac import test_support

    try:
        yield
    finally:
        try:
            test_support.drop_created_schemas()
        except Exception:  # noqa: BLE001 - teardown must not mask a test failure
            pass


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
    reviewer_llm_model: str = "test-reviewer-llm",
    trusted_internal: bool = False,
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
    relevant_files = codegraph_relevant_files(repo.get("files_changed") or [])
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": verdict,
        "reviewed_evidence_id": executor_evidence_id,
        "repo": repo,
        "checks": [{"name": "reviewer independent verification", "returncode": 0}],
        "worktree_digest": "sha256:" + ("0" * 64),
        "llm_model": reviewer_llm_model,
        "llm": {
            "tool": "test",
            "agent": "review",
            "model": reviewer_llm_model,
        },
    }
    if relevant_files:
        manifest["codegraph"] = {
            "schema": CODEGRAPH_AUDIT_SCHEMA,
            "status": "pass",
            "reason": "test_fixture",
            "relevant_files": relevant_files,
            "commands": [
                {"argv": ["codegraph", "sync"], "returncode": 0},
                {"argv": ["codegraph", "affected"], "returncode": 0},
            ],
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
        _trusted_internal=trusted_internal,
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
