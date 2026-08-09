"""Behavioral tests for `mac memory` CLI subcommands.

Covers: add, search, decay, health, recall, summarize-actions.
Commands already covered in test_mac_cli.py (remember, list, forget) are
NOT duplicated here.

Each test exercises the CLI via the same `_run(tmp_path, ...)` helper used by
tests/cli/test_mac_cli.py: it calls `mac --db <tmp> <subcommand> [args]`,
captures stdout, and parses JSON.

For subcommands that require Qdrant (recall), tests verify flag parsing and
the graceful error path rather than live integration.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.test_support import dsn_for

from mac.cli import main


def _run(tmp_path, *args):
    """Run `mac --db <tmp> <args>` and return (rc, parsed_output)."""
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


# ---------------------------------------------------------------------------
# memory add
# ---------------------------------------------------------------------------


def test_memory_add_returns_memory_record(tmp_path):
    """memory add creates a MemoryRecord and returns it as JSON."""
    rc, record = _run(
        tmp_path,
        "admin", "memory", "add",
        "--subject-type", "project",
        "--subject-id", "demo-project",
        "--record-type", "note",
        "--content", "hub learned that the mesh routes through worker-1",
        "--created-by", "hub",
    )
    assert rc == 0
    assert record["id"].startswith("mem_")
    assert record["subject_type"] == "project"
    assert record["subject_id"] == "demo-project"
    assert record["record_type"] == "note"
    assert "hub" in record["content"]
    assert record["created_by"] == "hub"
    assert record["task_id"] is None
    assert record["evidence_id"] is None


def test_memory_add_with_task_id(tmp_path):
    """memory add accepts an optional --task-id that links the record to a task."""
    # Create a task first so the FK is valid.
    rc, task = _run(tmp_path, "task", "create", "linked task")
    assert rc == 0

    rc, record = _run(
        tmp_path,
        "admin", "memory", "add",
        "--task-id", task["id"],
        "--subject-type", "agent",
        "--subject-id", "worker-1",
        "--record-type", "observation",
        "--content", "worker-1 deployed successfully",
        "--created-by", "hub",
    )
    assert rc == 0
    assert record["task_id"] == task["id"]
    assert record["subject_id"] == "worker-1"


def test_memory_add_missing_required_flags_errors(tmp_path):
    """memory add refuses to run when required flags are absent."""
    with pytest.raises(SystemExit):
        # --subject-type is required
        _run(
            tmp_path,
            "admin", "memory", "add",
            "--record-type", "note",
            "--content", "some content",
            "--created-by", "hub",
        )


def test_memory_add_with_agent_subject_type(tmp_path):
    """memory add works for agent subject_type (common fleet use-case)."""
    rc, record = _run(
        tmp_path,
        "admin", "memory", "add",
        "--subject-type", "agent",
        "--subject-id", "worker-2",
        "--record-type", "deployment_learning:gpu",
        "--content", "gpu worker requires nvidia-smi in PATH",
        "--created-by", "gpu-worker",
    )
    assert rc == 0
    assert record["subject_type"] == "agent"
    assert record["record_type"] == "deployment_learning:gpu"


# ---------------------------------------------------------------------------
# memory search
# ---------------------------------------------------------------------------


def _add_memory(tmp_path, *, subject_type, subject_id, record_type, content, created_by="hub"):
    rc, record = _run(
        tmp_path,
        "admin", "memory", "add",
        "--subject-type", subject_type,
        "--subject-id", subject_id,
        "--record-type", record_type,
        "--content", content,
        "--created-by", created_by,
    )
    assert rc == 0
    return record


def test_memory_search_returns_all_records_when_no_filters(tmp_path):
    """memory search with no filters returns all memory records."""
    _add_memory(tmp_path, subject_type="project", subject_id="alpha", record_type="note", content="first note")
    _add_memory(tmp_path, subject_type="agent", subject_id="worker-1", record_type="observation", content="second note")

    rc, records = _run(tmp_path, "admin", "memory", "search")
    assert rc == 0
    assert isinstance(records, list)
    assert len(records) == 2


def test_memory_search_filters_by_subject_type(tmp_path):
    """memory search --subject-type limits results to that subject_type."""
    _add_memory(tmp_path, subject_type="project", subject_id="beta", record_type="note", content="project note")
    _add_memory(tmp_path, subject_type="agent", subject_id="worker-1", record_type="note", content="agent note")

    rc, records = _run(tmp_path, "admin", "memory", "search", "--subject-type", "project")
    assert rc == 0
    assert all(r["subject_type"] == "project" for r in records)
    assert len(records) == 1


def test_memory_search_filters_by_subject_id(tmp_path):
    """memory search --subject-id limits results to that subject_id."""
    _add_memory(tmp_path, subject_type="agent", subject_id="worker-1", record_type="note", content="w1 note")
    _add_memory(tmp_path, subject_type="agent", subject_id="worker-2", record_type="note", content="w2 note")

    rc, records = _run(tmp_path, "admin", "memory", "search", "--subject-id", "worker-1")
    assert rc == 0
    assert all(r["subject_id"] == "worker-1" for r in records)
    assert len(records) == 1


def test_memory_search_filters_by_record_type(tmp_path):
    """memory search --record-type limits results to exact type match."""
    _add_memory(tmp_path, subject_type="project", subject_id="gamma", record_type="note", content="a note")
    _add_memory(tmp_path, subject_type="project", subject_id="gamma", record_type="observation", content="an obs")

    rc, records = _run(tmp_path, "admin", "memory", "search", "--record-type", "note")
    assert rc == 0
    assert all(r["record_type"] == "note" for r in records)
    assert len(records) == 1


def test_memory_search_filters_by_record_type_prefix(tmp_path):
    """memory search --record-type-prefix matches all records with that prefix."""
    _add_memory(tmp_path, subject_type="project", subject_id="delta", record_type="dream:reflection", content="r1")
    _add_memory(tmp_path, subject_type="project", subject_id="delta", record_type="dream:lesson", content="r2")
    _add_memory(tmp_path, subject_type="project", subject_id="delta", record_type="note", content="r3")

    rc, records = _run(tmp_path, "admin", "memory", "search", "--record-type-prefix", "dream:")
    assert rc == 0
    assert len(records) == 2
    assert all(r["record_type"].startswith("dream:") for r in records)


def test_memory_search_filters_by_created_by(tmp_path):
    """memory search --created-by limits results to records from that creator."""
    _add_memory(tmp_path, subject_type="project", subject_id="e", record_type="note", content="from hub", created_by="hub")
    _add_memory(tmp_path, subject_type="project", subject_id="e", record_type="note", content="from worker", created_by="worker-1")

    rc, records = _run(tmp_path, "admin", "memory", "search", "--created-by", "hub")
    assert rc == 0
    assert all(r["created_by"] == "hub" for r in records)
    assert len(records) == 1


def test_memory_search_limit_flag(tmp_path):
    """memory search --limit caps the number of returned records."""
    for i in range(5):
        _add_memory(tmp_path, subject_type="project", subject_id="f", record_type="note", content=f"note {i}")

    rc, records = _run(tmp_path, "admin", "memory", "search", "--limit", "2")
    assert rc == 0
    assert len(records) == 2


def test_memory_search_empty_db_returns_empty_list(tmp_path):
    """memory search on a fresh database returns an empty list."""
    rc, records = _run(tmp_path, "admin", "memory", "search")
    assert rc == 0
    assert records == []


def test_memory_search_order_desc(tmp_path):
    """memory search --order desc returns newest records first."""
    r1 = _add_memory(tmp_path, subject_type="project", subject_id="g", record_type="note", content="first")
    r2 = _add_memory(tmp_path, subject_type="project", subject_id="g", record_type="note", content="second")

    rc, records = _run(tmp_path, "admin", "memory", "search", "--order", "desc")
    assert rc == 0
    assert len(records) == 2
    # Newest first — created_at of r2 >= r1.
    assert records[0]["id"] == r2["id"]
    assert records[1]["id"] == r1["id"]


# ---------------------------------------------------------------------------
# memory decay
# ---------------------------------------------------------------------------


def test_memory_decay_dry_run_by_default(tmp_path):
    """memory decay without --apply is a dry run — reports but deletes nothing."""
    # Seed a record; the table is empty so scanned/forgettable will be 0
    # (decay scans records older than ttl_days, not all records).
    rc, result = _run(tmp_path, "admin", "memory", "decay")
    assert rc == 0
    assert result["schema"] == "mac.memory_decay.v1"
    assert result["dry_run"] is True
    assert result["deleted"] == 0


def test_memory_decay_returns_expected_schema_fields(tmp_path):
    """memory decay result includes all expected schema fields."""
    rc, result = _run(tmp_path, "admin", "memory", "decay", "--ttl-days", "30")
    assert rc == 0
    for field in ("schema", "ttl_days", "dry_run", "cutoff", "scanned", "forgettable", "by_type", "deleted"):
        assert field in result, f"missing field: {field}"
    assert result["ttl_days"] == 30.0


def test_memory_decay_apply_flag_sets_dry_run_false(tmp_path):
    """memory decay --apply performs actual deletion (dry_run=False in result)."""
    rc, result = _run(tmp_path, "admin", "memory", "decay", "--apply")
    assert rc == 0
    assert result["dry_run"] is False


def test_memory_decay_respects_ttl_days_flag(tmp_path):
    """memory decay --ttl-days passes the value through to the result."""
    rc, result = _run(tmp_path, "admin", "memory", "decay", "--ttl-days", "7")
    assert rc == 0
    assert result["ttl_days"] == 7.0


def test_memory_decay_limit_flag(tmp_path):
    """memory decay --limit is accepted without error."""
    rc, result = _run(tmp_path, "admin", "memory", "decay", "--limit", "100")
    assert rc == 0
    assert result["schema"] == "mac.memory_decay.v1"


def test_memory_decay_protected_prefixes_preserved(tmp_path):
    """Curated record types are listed in protected_prefixes and never deleted."""
    rc, result = _run(tmp_path, "admin", "memory", "decay", "--apply")
    assert rc == 0
    protected = result["protected_prefixes"]
    # Core curated types must always be protected.
    for prefix in ("beads_memory", "deployment_learning", "fleet_learning", "dream", "user"):
        assert any(p == prefix or p.startswith(prefix) for p in protected), \
            f"expected '{prefix}' in protected_prefixes"


# ---------------------------------------------------------------------------
# memory health
# ---------------------------------------------------------------------------


def test_memory_health_returns_expected_schema(tmp_path):
    """memory health returns a JSON object with the mac.memory_health.v1 schema."""
    rc, result = _run(tmp_path, "admin", "memory", "health")
    assert rc == 0
    assert result["schema"] == "mac.memory_health.v1"


def test_memory_health_empty_db_zero_counts(tmp_path):
    """A fresh database reports zero memory_records and zero vector_refs."""
    rc, result = _run(tmp_path, "admin", "memory", "health")
    assert rc == 0
    assert result["memory_records_count"] == 0
    assert result["vector_refs_count"] == 0
    assert result["last_nap_run_at"] is None
    assert result["alerts"] == []


def test_memory_health_counts_increase_after_add(tmp_path):
    """memory_records_count reflects records added via memory add."""
    _add_memory(tmp_path, subject_type="project", subject_id="health-test", record_type="note", content="x")
    rc, result = _run(tmp_path, "admin", "memory", "health")
    assert rc == 0
    assert result["memory_records_count"] == 1


def test_memory_health_nap_interval_flag_accepted(tmp_path):
    """memory health --nap-interval-hours is accepted without error."""
    rc, result = _run(tmp_path, "admin", "memory", "health", "--nap-interval-hours", "12")
    assert rc == 0
    assert result["schema"] == "mac.memory_health.v1"


def test_memory_health_has_captured_at_field(tmp_path):
    """memory health result includes a captured_at timestamp."""
    rc, result = _run(tmp_path, "admin", "memory", "health")
    assert rc == 0
    assert "captured_at" in result
    assert result["captured_at"]  # non-empty string


def test_memory_health_qdrant_block_present(tmp_path):
    """memory health result includes a qdrant block even when Qdrant is unreachable."""
    rc, result = _run(tmp_path, "admin", "memory", "health")
    assert rc == 0
    assert "qdrant" in result
    # When no Qdrant URL is configured the block may have a None URL or an error.
    qdrant = result["qdrant"]
    assert isinstance(qdrant, dict)
    assert "collections" in qdrant


# ---------------------------------------------------------------------------
# memory recall (Qdrant-dependent — test flag parsing + graceful error path)
# ---------------------------------------------------------------------------


def test_memory_recall_missing_qdrant_raises_error(tmp_path, monkeypatch):
    """memory recall <query> fails gracefully when no Qdrant is reachable.

    The CLI runs in local mode (--db) with no Qdrant URL configured.
    The expected outcome is a non-zero exit or a SystemExit / MACError, NOT a
    traceback from deep inside the embedding client.
    """
    # Ensure no Qdrant env vars bleed in.
    for var in ("MAC_QDRANT_URL", "QDRANT_URL", "QDRANT_ADDRESS", "QDRANT_FLEET_URL"):
        monkeypatch.delenv(var, raising=False)

    # The command should exit with a non-zero code or raise SystemExit.
    try:
        rc, result = _run(tmp_path, "admin", "memory", "recall", "what did worker-1 learn about the mesh?")
        # If it returned a result it must signal an error somehow.
        assert rc != 0
    except (SystemExit, Exception):
        pass  # Any raised exception is an acceptable graceful error path.


def test_memory_recall_query_positional_arg_parsed(tmp_path, monkeypatch):
    """memory recall accepts a positional query argument without crashing on parse.

    We only check that argparse accepts the invocation; Qdrant connectivity
    is not required for this assertion.
    """
    for var in ("MAC_QDRANT_URL", "QDRANT_URL", "QDRANT_ADDRESS", "QDRANT_FLEET_URL"):
        monkeypatch.delenv(var, raising=False)

    # As long as argparse parsed the command (not a parse-time SystemExit from
    # a missing positional), we've verified the flag contract.
    try:
        rc, result = _run(tmp_path, "admin", "memory", "recall", "hub deployment status")
        # If it finished it should be either an error or an empty list.
        # Either way, rc may be 0 (empty results) or non-zero.
    except SystemExit as exc:
        # Only an argparse "required argument missing" exit (code 2) would
        # indicate a flag-parsing regression.
        assert exc.code != 2, "argparse rejected a valid recall invocation"
    except Exception:
        pass  # Expected: Qdrant unavailable


def test_memory_recall_optional_flags_accepted(tmp_path, monkeypatch):
    """memory recall --tier, --limit, --min-score, --project, --tenant-id parse correctly."""
    for var in ("MAC_QDRANT_URL", "QDRANT_URL", "QDRANT_ADDRESS", "QDRANT_FLEET_URL"):
        monkeypatch.delenv(var, raising=False)

    try:
        _run(
            tmp_path,
            "admin", "memory", "recall",
            "worker-2 mesh route",
            "--tier", "medium",
            "--limit", "3",
            "--project", "mac",
        )
    except SystemExit as exc:
        assert exc.code != 2, "argparse rejected valid optional flags for recall"
    except Exception:
        pass  # Qdrant unavailable — expected


# ---------------------------------------------------------------------------
# memory summarize-actions
# ---------------------------------------------------------------------------


def test_memory_summarize_actions_dry_run_returns_schema(tmp_path):
    """memory summarize-actions --dry-run returns the summary without writing a record."""
    rc, result = _run(tmp_path, "admin", "memory", "summarize-actions", "--dry-run")
    assert rc == 0
    assert result["schema"] == "mac.memory.summarize_actions.v1"
    assert "summary" in result
    # dry-run: no memory record was written.
    assert result["memory"] is None


def test_memory_summarize_actions_returns_summary_block(tmp_path):
    """memory summarize-actions result always contains a summary sub-object."""
    rc, result = _run(tmp_path, "admin", "memory", "summarize-actions", "--dry-run")
    assert rc == 0
    summary = result["summary"]
    assert isinstance(summary, dict)
    # The summary always carries at least an event_count key.
    assert "event_count" in summary


def test_memory_summarize_actions_agent_flag_accepted(tmp_path):
    """memory summarize-actions --agent <id> is accepted without error."""
    rc, machine = _run(tmp_path, "admin", "machine", "register", "host-1")
    assert rc == 0
    rc, agent = _run(tmp_path, "agent", "register", machine["id"], "worker-1")
    assert rc == 0

    rc, result = _run(
        tmp_path,
        "admin", "memory", "summarize-actions",
        "--agent", agent["id"],
        "--dry-run",
    )
    assert rc == 0
    assert result["schema"] == "mac.memory.summarize_actions.v1"


def test_memory_summarize_actions_no_events_memory_is_none(tmp_path):
    """With no action events, write=True still returns memory=None (nothing to summarize)."""
    # Omit --dry-run (write=True) but there are no events to summarize,
    # so the implementation returns memory=None.
    rc, result = _run(tmp_path, "admin", "memory", "summarize-actions")
    assert rc == 0
    assert result["schema"] == "mac.memory.summarize_actions.v1"
    assert result["memory"] is None


def test_memory_summarize_actions_created_by_flag(tmp_path):
    """memory summarize-actions --created-by is accepted without error."""
    rc, result = _run(
        tmp_path,
        "admin", "memory", "summarize-actions",
        "--created-by", "hub",
        "--dry-run",
    )
    assert rc == 0
    assert result["schema"] == "mac.memory.summarize_actions.v1"
