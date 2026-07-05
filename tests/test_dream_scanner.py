from __future__ import annotations

import json
import sqlite3

from mac.dream_scanner import (
    DREAM_FAILURE_CANDIDATE_SCHEMA,
    DREAM_FAILURE_EVIDENCE_SCHEMA,
    DREAM_FAILURE_SCAN_SCHEMA,
    scan_dream_failure_candidates,
)
from mac import dream_scanner as scanner
from mac.store import SQLiteStore


def _make_hermes_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            model TEXT,
            billing_provider TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL,
            finish_reason TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, model, billing_provider) VALUES (?, ?, ?, ?)",
        ("sess-a", "cli", "gpt-5", "openai"),
    )
    conn.execute(
        """
        INSERT INTO messages
        (session_id, role, content, tool_calls, timestamp)
        VALUES (?, 'assistant', '', ?, ?)
        """,
        (
            "sess-a",
            json.dumps([{"type": "function", "function": {"name": "web_fetch"}}]),
            1735689600.0,
        ),
    )
    for idx, token in enumerate(("sk-secret-token-one", "sk-secret-token-two"), start=1):
        conn.execute(
            """
            INSERT INTO messages
            (session_id, role, content, tool_call_id, tool_name, timestamp)
            VALUES (?, 'tool', ?, ?, 'web_fetch', ?)
            """,
            (
                "sess-a",
                "RuntimeError: request failed token=%s https://user:pass@example.test/path"
                % token,
                "call-%d" % idx,
                1735689600.0 + idx,
            ),
        )
    conn.commit()
    conn.close()


def _find_candidate(report, kind, contains):
    for candidate in report["candidates"]:
        if candidate["kind"] == kind and contains in candidate["signature"]:
            return candidate
    raise AssertionError("missing %s candidate containing %r: %r" % (kind, contains, report))


def test_scans_repeated_hermes_tool_errors_with_redacted_evidence(tmp_path):
    hermes_db = tmp_path / "state.db"
    _make_hermes_db(hermes_db)

    report = scan_dream_failure_candidates(hermes_db_path=hermes_db)

    assert report["schema"] == DREAM_FAILURE_SCAN_SCHEMA
    assert report["sources"][0]["status"] == "scanned"
    candidate = _find_candidate(report, "tool_call_error", "web_fetch")
    assert candidate["schema"] == DREAM_FAILURE_CANDIDATE_SCHEMA
    assert candidate["count"] == 2
    assert candidate["window"]["first_seen_at"] == "2025-01-01T00:00:01.000000+00:00"
    assert candidate["window"]["last_seen_at"] == "2025-01-01T00:00:02.000000+00:00"
    assert candidate["dimensions"]["tools"] == [{"name": "web_fetch", "count": 2}]
    assert candidate["evidence"][0]["schema"] == DREAM_FAILURE_EVIDENCE_SCHEMA
    evidence_text = json.dumps(candidate["evidence"], sort_keys=True)
    assert "sk-secret" not in evidence_text
    assert "user:pass" not in evidence_text


def test_scans_repeated_command_audit_test_failures(tmp_path):
    store = SQLiteStore(str(tmp_path / "mac.db"))
    for idx in range(2):
        store.execute(
            """
            INSERT INTO command_audit (
                id, command_id, agent_id, phase, argv, cwd, task_id, lease_id,
                started_at, completed_at, duration_ms, returncode,
                stdout_sha256, stderr_sha256, stdout_bytes, stderr_bytes,
                metadata, created_at
            ) VALUES (?, ?, ?, 'failed', ?, '/workspace', 'task-1', 'lease-1',
                      ?, ?, 12.0, 1, 'sha256:stdout', 'sha256:stderr',
                      10, 20, ?, ?)
            """,
            (
                "audit-%d" % idx,
                "cmd-test",
                "agent-1",
                json.dumps(["scripts/run-contract-tests.sh"]),
                "2025-01-01T00:00:0%d+00:00" % idx,
                "2025-01-01T00:00:0%d+00:00" % idx,
                json.dumps({"error": "pytest failed password=super-secret"}),
                "2025-01-01T00:00:0%d+00:00" % idx,
            ),
        )

    report = scan_dream_failure_candidates(mac_store=store)

    candidate = _find_candidate(report, "test_failure", "run-contract-tests.sh")
    assert candidate["count"] == 2
    assert candidate["dimensions"]["commands"] == [
        {"name": "run-contract-tests.sh", "count": 2}
    ]
    assert candidate["dimensions"]["tasks"] == [{"name": "task-1", "count": 2}]
    assert candidate["evidence_truncated"] is False
    assert "super-secret" not in json.dumps(candidate["evidence"], sort_keys=True)
    store.close()


def test_scans_model_provider_errors_and_skill_tool_names(tmp_path):
    store = SQLiteStore(str(tmp_path / "mac.db"))
    attrs = {
        "provider": "openai",
        "requested_model": "gpt-5",
        "error": "429 rate limit",
        "skill": "codex",
        "tool": "apply_patch",
        "api_key": "sk-action-secret",
    }
    store.execute(
        """
        INSERT INTO action_events (
            event_id, timestamp, agent_id, hermes_instance_id, task_id,
            session_id, sandbox_id, actor, action_type, action_name,
            subject_type, subject_id, outcome, severity, policy_id,
            policy_version, command_id, parent_event_id, attributes,
            redaction_state
        ) VALUES (
            'act-1', '2025-01-01T00:00:00+00:00', 'agent-1', NULL, 'task-1',
            'sess-1', NULL, 'agent-1', 'llm', 'llm.route',
            'task', 'task-1', 'failure', 'error', NULL,
            NULL, NULL, NULL, ?, 'redacted'
        )
        """,
        (json.dumps(attrs),),
    )
    store.execute(
        """
        INSERT INTO observability_events (
            id, kind, layer, source, level, name, subject_type, subject_id,
            value, unit, detail, created_at
        ) VALUES (
            'obs-1', 'log', 'router', 'agent-1', 'error', 'llm.route',
            'task', 'task-1', NULL, '', ?, '2025-01-01T00:00:01+00:00'
        )
        """,
        (
            json.dumps(
                {
                    "provider": "openai",
                    "resolved_model": "gpt-5",
                    "error": "provider overloaded",
                }
            ),
        ),
    )

    report = scan_dream_failure_candidates(mac_store=store, min_count=1)

    model_candidate = _find_candidate(report, "model_provider_error", "llm.route")
    assert model_candidate["dimensions"]["providers"] == [{"name": "openai", "count": 1}]
    assert model_candidate["dimensions"]["models"] == [{"name": "gpt-5", "count": 1}]
    tool_candidate = _find_candidate(report, "tool_or_skill_name", "tool:apply_patch")
    assert tool_candidate["dimensions"]["tools"] == [{"name": "apply_patch", "count": 1}]
    skill_candidate = _find_candidate(report, "tool_or_skill_name", "skill:codex")
    assert skill_candidate["dimensions"]["skills"] == [{"name": "codex", "count": 1}]
    assert "sk-action-secret" not in json.dumps(report, sort_keys=True)
    store.close()


def test_window_filter_and_missing_sources_are_deterministic(tmp_path):
    missing = tmp_path / "missing-state.db"
    report = scan_dream_failure_candidates(
        hermes_db_path=missing,
        since="2025-01-01T00:00:00+00:00",
        until="2025-01-02T00:00:00+00:00",
    )

    assert report == {
        "schema": DREAM_FAILURE_SCAN_SCHEMA,
        "window_filter": {
            "since": "2025-01-01T00:00:00.000000+00:00",
            "until": "2025-01-02T00:00:00.000000+00:00",
        },
        "min_count": 2,
        "sources": [
            {
                "name": "hermes_session_db",
                "path": str(missing),
                "status": "missing",
                "rows": 0,
            }
        ],
        "candidate_count": 0,
        "candidates": [],
    }


def test_scans_mac_db_path_and_normalizes_option_edges(tmp_path):
    store = SQLiteStore(str(tmp_path / "mac.db"))
    for idx in range(2):
        store.execute(
            """
            INSERT INTO command_audit (
                id, command_id, agent_id, phase, argv, cwd, task_id, lease_id,
                started_at, completed_at, duration_ms, returncode,
                stdout_sha256, stderr_sha256, stdout_bytes, stderr_bytes,
                metadata, created_at
            ) VALUES (?, 'cmd-pytest', 'agent-1', 'failed', ?, '/workspace',
                      'task-1', NULL, ?, ?, 12.0, 2, NULL, NULL, 0, 0,
                      '{}', ?)
            """,
            (
                "audit-path-%d" % idx,
                json.dumps(["python3", "-m", "pytest", "tests/test_example.py"]),
                "2025-01-01T00:00:0%d+00:00" % idx,
                "2025-01-01T00:00:0%d+00:00" % idx,
                "2025-01-01T00:00:0%d+00:00" % idx,
            ),
        )
    store.close()

    report = scan_dream_failure_candidates(
        mac_db_path=tmp_path / "mac.db",
        min_count=0,
        max_evidence_per_candidate=0,
    )

    candidate = _find_candidate(report, "test_failure", "python3 -m")
    assert candidate["count"] == 2
    assert candidate["evidence_truncated"] is True
    assert len(candidate["evidence"]) == 1
    assert report["sources"][0]["name"] == "mac_sqlite_db"


def test_scans_hermes_without_sessions_table_and_reports_missing_messages(tmp_path):
    skipped_db = tmp_path / "skipped.db"
    sqlite3.connect(skipped_db).close()
    skipped = scan_dream_failure_candidates(hermes_db_path=skipped_db, min_count=1)
    assert skipped["sources"][0]["status"] == "skipped"
    assert skipped["sources"][0]["reason"] == "messages table missing"

    no_sessions_db = tmp_path / "state-no-sessions.db"
    conn = sqlite3.connect(no_sessions_db)
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL,
            finish_reason TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO messages (session_id, role, content, timestamp)
        VALUES ('sess-no-parent', 'assistant', 'OpenAI provider error: rate limit', 1735689600.0)
        """
    )
    conn.commit()
    conn.close()

    report = scan_dream_failure_candidates(hermes_db_path=no_sessions_db, min_count=1)

    candidate = _find_candidate(report, "model_provider_error", "rate limit")
    assert candidate["dimensions"]["sessions"] == [{"name": "sess-no-parent", "count": 1}]


def test_window_filter_drops_out_of_range_rows(tmp_path):
    hermes_db = tmp_path / "state.db"
    _make_hermes_db(hermes_db)

    report = scan_dream_failure_candidates(
        hermes_db_path=hermes_db,
        since="2026-01-01T00:00:00+00:00",
        min_count=1,
    )

    assert report["candidate_count"] == 0


def test_helper_edge_cases_for_deterministic_normalization():
    class FallbackStore:
        def __init__(self, succeeds=True):
            self.succeeds = succeeds

        def query_one(self, *_args):
            raise RuntimeError("sqlite_master unavailable")

        def query_all(self, *_args):
            if not self.succeeds:
                raise RuntimeError("missing")
            return []

    reader = scanner._Reader()
    for method in (reader.query_all, reader.table_exists):
        try:
            method("anything")
        except NotImplementedError:
            pass
        else:
            raise AssertionError("abstract reader method should raise")

    assert scanner._StoreReader(FallbackStore()).table_exists("command_audit") is True
    assert scanner._StoreReader(FallbackStore(False)).table_exists("command_audit") is False
    assert scanner._select_sql_for_table("action_events").lstrip().startswith("SELECT event_id")
    assert scanner._select_sql_for_table("observability_events").lstrip().startswith("SELECT sequence")
    assert scanner._coerce_time(None) == ""
    assert scanner._coerce_time("not-a-time") == "not-a-time"
    assert scanner._timestamp_in_window("", "2025-01-01T00:00:00.000000+00:00", "") is False
    assert scanner._timestamp_in_window("2025-01-03T00:00:00.000000+00:00", "", "2025-01-02T00:00:00.000000+00:00") is False
    assert scanner._json_dict({"a": 1}) == {"a": 1}
    assert scanner._json_dict("") == {}
    assert scanner._json_dict("{bad") == {}
    assert scanner._json_dict("[1]") == {}
    assert scanner._json_list(["a", 1]) == ["a", "1"]
    assert scanner._json_list("") == []
    assert scanner._json_list("{bad") == ["{bad"]
    assert scanner._json_list('{"a": 1}') == ["{'a': 1}"]
    assert scanner._message_text(None) == ""
    assert scanner._message_text(["a"]) == '["a"]'
    assert scanner._message_text("{bad") == "{bad"
    assert scanner._message_text('{"a": 1}') == '{"a": 1}'
    assert scanner._tool_names_from_calls("{bad") == []
    assert scanner._tool_names_from_calls([{"name": "search"}, "skip"]) == ["search"]
    assert scanner._tool_names_from_command(["pytest", "mac-agent", "plain"]) == [
        "mac-agent",
        "pytest",
    ]
    assert scanner._command_label([]) == "command"
    assert scanner._command_label(["python3", "script.py"]) == "python3 script.py"
    assert scanner._extract_skill_names("skills/foo.md $coder") == ["coder"]
    assert "no module named" in scanner._failure_signature("ModuleNotFoundError: No module named 'yaml'")
    assert "exit status" in scanner._failure_signature("returned non-zero exit status 12")
    assert scanner._failure_signature("429 from provider") == "<n>"
    assert scanner._redact_excerpt("x" * 400, 20).endswith("…")
    assert scanner._clean_name("") == ""
    assert scanner._first_clean({}, "missing") == ""
    assert scanner._clean_evidence({"excerpt": "token=abc123456789", "empty": None, "row_id": "1"}) == {
        "excerpt": "token=<redacted>",
        "row_id": "1",
    }
