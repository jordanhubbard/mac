"""Per-participant successor-hold columns on fleet_release_epoch_agents.

These columns are durable facts only. Nothing reads or writes them yet; this
file proves the fresh-database shape, the four-value CHECK, and that a
database created without the columns migrates cleanly and idempotently.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mac.store import StoreError


ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "src" / "mac" / "data" / "postgres" / "schema.sql").read_text()
STORE_POSTGRES = (
    ROOT / "src" / "mac" / "store_postgres.py"
).read_text()

_ACTION_COL = "successor_hold_action"
_REASON_COL = "successor_hold_reason"
_RESOLVED_COL = "resolved_successor_hold_reason"
_ACTIONS = ("cohort", "preserve", "release", "adopt")
_NOW = "2026-01-01T00:00:00Z"


def _agents_create_body(schema_sql: str) -> str:
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS fleet_release_epoch_agents\s*\((?P<body>.*?)\n\);",
        schema_sql,
        re.DOTALL,
    )
    assert match, "fleet_release_epoch_agents CREATE TABLE not found"
    return match.group("body")


def test_create_table_declares_successor_hold_columns() -> None:
    body = _agents_create_body(SCHEMA)
    assert re.search(
        r"successor_hold_action TEXT NOT NULL DEFAULT 'cohort'\s+"
        r"CHECK \(\s*successor_hold_action IN "
        r"\('cohort', 'preserve', 'release', 'adopt'\)\s*\)",
        body,
    )
    assert re.search(r"\bsuccessor_hold_reason TEXT\b", body)
    assert re.search(r"\bresolved_successor_hold_reason TEXT\b", body)


def test_cohort_default_column_is_retained() -> None:
    epochs = re.search(
        r"CREATE TABLE IF NOT EXISTS fleet_release_epochs\s*\((?P<body>.*?)\n\);",
        SCHEMA,
        re.DOTALL,
    )
    assert epochs, "fleet_release_epochs CREATE TABLE not found"
    assert "successor_hold_reason TEXT" in epochs.group("body")
    assert not re.search(
        r"ALTER TABLE fleet_release_epochs\s+"
        r"ADD COLUMN IF NOT EXISTS\s+successor_hold_reason",
        SCHEMA,
    )


def test_additive_alters_and_ensure_column_retrofit_the_columns() -> None:
    for column in (_ACTION_COL, _REASON_COL, _RESOLVED_COL):
        assert re.search(
            r"ALTER TABLE fleet_release_epoch_agents\s+"
            r"ADD COLUMN IF NOT EXISTS\s+%s" % column,
            SCHEMA,
        ), "%s lacks a schema.sql additive ALTER" % column
        assert re.search(
            r'ensure_column\(\s*"fleet_release_epoch_agents",\s*"%s"' % column,
            STORE_POSTGRES,
        ), "%s lacks a PostgresStore.ensure_column retrofit" % column


def _column_meta(store, name: str) -> dict:
    row = store.query_one(
        "SELECT column_name, is_nullable, column_default, data_type "
        "FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = 'fleet_release_epoch_agents' AND column_name = ?",
        (name,),
    )
    assert row is not None, "missing column %s" % name
    return dict(row)


def _check_defs(store) -> list[str]:
    rows = store.query_all(
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conrelid = 'fleet_release_epoch_agents'::regclass "
        "AND contype = 'c'"
    )
    return [str(row["def"]) for row in rows]


def _assert_action_check(store) -> None:
    joined = " ".join(_check_defs(store))
    for value in _ACTIONS:
        assert value in joined, "CHECK missing allowed value %r in %s" % (
            value,
            joined,
        )


def _seed_participant(store, agent_id: str = "agent_hold_schema") -> None:
    store.execute(
        "INSERT INTO machines (id, hostname, labels, resources, trusted, "
        "created_at, updated_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        ("machine_hold_schema", "hold-host", "{}", "{}", 1, _NOW, _NOW, _NOW),
    )
    store.execute(
        "INSERT INTO agents (id, machine_id, name, capabilities, resources, "
        "status, health_status, created_at, updated_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            agent_id,
            "machine_hold_schema",
            "hold-agent",
            "[]",
            "{}",
            "idle",
            "healthy",
            _NOW,
            _NOW,
            _NOW,
        ),
    )
    store.execute(
        "INSERT INTO worker_credentials (id, agent_id, fleet, "
        "credential_version, token_hash, token_fingerprint, scopes, "
        "environment, state, issued_at, expires_at, created_by, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "cred_hold_schema",
            agent_id,
            "",
            1,
            "token-hash-hold",
            "token-fp-hold",
            "[]",
            "vm",
            "active",
            _NOW,
            _NOW,
            "test",
            _NOW,
        ),
    )
    store.execute(
        "INSERT INTO fleet_release_epochs (epoch_id, request_sha256, "
        "identity_sha256, identity_payload, state, policy_snapshot, actor, "
        "prepared_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            "epoch_hold_schema",
            "req-sha",
            "ident-sha",
            "{}",
            "open",
            "{}",
            "test",
            _NOW,
        ),
    )
    store.execute(
        "INSERT INTO fleet_release_epoch_agents ("
        "epoch_id, agent_id, ordinal, prior_dispatch_hold, "
        "epoch_hold_reason, epoch_hold_at, prior_active_service_claim_ids, "
        "generation, baseline_seen, principal_id, principal_version, "
        "principal_fingerprint, prior_live_principal_ids, "
        "prior_attestation_ciphertext_sha256, report_executor_action, "
        "prior_report_executor_projection_sha256, created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "epoch_hold_schema",
            agent_id,
            0,
            1,
            "epoch-hold",
            _NOW,
            "[]",
            "gen-1",
            _NOW,
            "cred_hold_schema",
            1,
            "fp",
            "[]",
            "att-sha",
            "preserve",
            "proj-sha",
            _NOW,
        ),
    )


@pytest.mark.postgres
def test_fresh_database_has_successor_hold_columns_and_check(postgres_store) -> None:
    action = _column_meta(postgres_store, _ACTION_COL)
    reason = _column_meta(postgres_store, _REASON_COL)
    resolved = _column_meta(postgres_store, _RESOLVED_COL)
    assert action["is_nullable"] == "NO"
    assert "cohort" in str(action["column_default"] or "")
    assert reason["is_nullable"] == "YES"
    assert resolved["is_nullable"] == "YES"
    _assert_action_check(postgres_store)

    _seed_participant(postgres_store)
    row = postgres_store.query_one(
        "SELECT successor_hold_action, successor_hold_reason, "
        "resolved_successor_hold_reason FROM fleet_release_epoch_agents "
        "WHERE epoch_id = ?",
        ("epoch_hold_schema",),
    )
    assert dict(row) == {
        _ACTION_COL: "cohort",
        _REASON_COL: None,
        _RESOLVED_COL: None,
    }
    for value in _ACTIONS:
        postgres_store.execute(
            "UPDATE fleet_release_epoch_agents SET successor_hold_action = ?",
            (value,),
        )
    with pytest.raises(StoreError):
        postgres_store.execute(
            "UPDATE fleet_release_epoch_agents SET successor_hold_action = ?",
            ("unknown",),
        )
    with pytest.raises(StoreError):
        postgres_store.execute(
            "UPDATE fleet_release_epoch_agents SET successor_hold_action = NULL"
        )


@pytest.mark.postgres
def test_existing_database_migrates_successor_hold_columns_idempotently(
    postgres_store,
) -> None:
    _seed_participant(postgres_store)
    postgres_store.execute(
        "ALTER TABLE fleet_release_epoch_agents "
        "DROP COLUMN IF EXISTS successor_hold_action, "
        "DROP COLUMN IF EXISTS successor_hold_reason, "
        "DROP COLUMN IF EXISTS resolved_successor_hold_reason"
    )
    assert (
        postgres_store.query_one(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'fleet_release_epoch_agents' "
            "AND column_name = ?",
            (_ACTION_COL,),
        )
        is None
    )

    postgres_store.initialize()
    postgres_store.initialize()

    action = _column_meta(postgres_store, _ACTION_COL)
    assert action["is_nullable"] == "NO"
    assert "cohort" in str(action["column_default"] or "")
    assert _column_meta(postgres_store, _REASON_COL)["is_nullable"] == "YES"
    assert _column_meta(postgres_store, _RESOLVED_COL)["is_nullable"] == "YES"
    _assert_action_check(postgres_store)
    action_checks = [
        definition
        for definition in _check_defs(postgres_store)
        if _ACTION_COL in definition
    ]
    assert len(action_checks) == 1, action_checks

    row = postgres_store.query_one(
        "SELECT successor_hold_action, successor_hold_reason, "
        "resolved_successor_hold_reason FROM fleet_release_epoch_agents "
        "WHERE epoch_id = ?",
        ("epoch_hold_schema",),
    )
    assert dict(row) == {
        _ACTION_COL: "cohort",
        _REASON_COL: None,
        _RESOLVED_COL: None,
    }
    with pytest.raises(StoreError):
        postgres_store.execute(
            "UPDATE fleet_release_epoch_agents SET successor_hold_action = ?",
            ("bogus",),
        )
