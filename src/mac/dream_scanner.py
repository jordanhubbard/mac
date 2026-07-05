"""Deterministic raw-signal scanner for dream-cycle candidate generation."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import quote

from mac.models import JsonDict


DREAM_FAILURE_SCAN_SCHEMA = "mac.dream_failure_scan.v1"
DREAM_FAILURE_CANDIDATE_SCHEMA = "mac.dream_failure_candidate.v1"
DREAM_FAILURE_EVIDENCE_SCHEMA = "mac.dream_failure_evidence.v1"

_FAILURE_RE = re.compile(
    r"\b(error|exception|traceback|failed|failure|timeout|timed out|denied|"
    r"unauthorized|forbidden|not found|no such|invalid|rate limit|quota|"
    r"overloaded|exit status|returncode|assertionerror)\b",
    re.IGNORECASE,
)
_MODEL_RE = re.compile(
    r"\b(llm|model|provider|openai|anthropic|gemini|bedrock|xai|moonshot|"
    r"api key|context length|rate limit|quota|429|overloaded)\b",
    re.IGNORECASE,
)
_TEST_RE = re.compile(
    r"\b(pytest|unittest|tox|coverage|run-contract-tests|assertionerror|"
    r"tests?/|test_[A-Za-z0-9_./-]*\.py)\b",
    re.IGNORECASE,
)
_SKILL_RE = re.compile(
    r"(?:\$|skills?/|[\"']?skills?[\"']?\s*[:=]\s*[\"']?)"
    r"([A-Za-z0-9][A-Za-z0-9_.-]{1,80})",
    re.IGNORECASE,
)
_URL_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/@\s]+)@")
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|token|secret|password|authorization|"
    r"access[_-]?token|refresh[_-]?token)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;}]+"
)
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,})\b"
)
_LONG_ATOM_RE = re.compile(r"\b[A-Za-z0-9_./+=-]{80,}\b")


def scan_dream_failure_candidates(
    *,
    hermes_db_path: Optional[str | Path] = None,
    mac_db_path: Optional[str | Path] = None,
    mac_store: Any = None,
    since: Any = None,
    until: Any = None,
    min_count: int = 2,
    max_evidence_per_candidate: int = 5,
) -> JsonDict:
    """Scan Hermes sessions and MAC ledgers into stable dream candidates.

    The scanner is intentionally side-effect free. It reads from the supplied
    SQLite sources, groups matching failure signals by deterministic signatures,
    and returns redacted evidence rows suitable for later dream/memory stages.
    """

    if min_count < 1:
        min_count = 1
    if max_evidence_per_candidate < 1:
        max_evidence_per_candidate = 1
    since_iso = _coerce_time(since) if since is not None else ""
    until_iso = _coerce_time(until) if until is not None else ""
    buckets: dict[tuple[str, str], _CandidateBucket] = {}
    sources: list[JsonDict] = []

    def emit(
        kind: str,
        signature: str,
        evidence: JsonDict,
        *,
        severity: str = "warning",
        dimensions: Optional[Mapping[str, Any]] = None,
    ) -> None:
        timestamp = str(evidence.get("timestamp") or "")
        if not _timestamp_in_window(timestamp, since_iso, until_iso):
            return
        clean_signature = _clean_signature(signature)
        if not clean_signature:
            return
        key = (kind, clean_signature)
        if key not in buckets:
            buckets[key] = _CandidateBucket(
                kind=kind,
                signature=clean_signature,
                severity=severity,
                max_evidence=max_evidence_per_candidate,
            )
        buckets[key].add(evidence, dimensions or {})

    if hermes_db_path is not None:
        _scan_hermes_session_db(Path(hermes_db_path), emit, sources)
    if mac_store is not None:
        _scan_mac_ledgers(_StoreReader(mac_store), emit, sources, "mac_store")
    if mac_db_path is not None:
        _scan_mac_sqlite_db(Path(mac_db_path), emit, sources)

    candidates = [
        bucket.to_dict()
        for bucket in buckets.values()
        if bucket.count >= min_count
    ]
    candidates.sort(key=lambda item: (item["kind"], item["signature"]))
    return {
        "schema": DREAM_FAILURE_SCAN_SCHEMA,
        "window_filter": {
            "since": since_iso or None,
            "until": until_iso or None,
        },
        "min_count": min_count,
        "sources": sources,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def _scan_hermes_session_db(
    db_path: Path,
    emit: Callable[..., None],
    sources: list[JsonDict],
) -> None:
    source = {"name": "hermes_session_db", "path": str(db_path), "status": "missing", "rows": 0}
    sources.append(source)
    if not db_path.exists():
        return
    try:
        with _connect_readonly(db_path) as conn:
            conn.row_factory = sqlite3.Row
            if not _raw_table_exists(conn, "messages"):
                source.update({"status": "skipped", "reason": "messages table missing"})
                return
            has_sessions = _raw_table_exists(conn, "sessions")
            sql = (
                """
                SELECT m.id, m.session_id, m.role, m.content, m.tool_call_id,
                       m.tool_calls, m.tool_name, m.timestamp, m.finish_reason,
                       s.source AS session_source, s.model, s.billing_provider
                FROM messages m
                LEFT JOIN sessions s ON s.id = m.session_id
                ORDER BY m.timestamp, m.id
                """
                if has_sessions
                else
                """
                SELECT m.id, m.session_id, m.role, m.content, m.tool_call_id,
                       m.tool_calls, m.tool_name, m.timestamp, m.finish_reason,
                       NULL AS session_source, NULL AS model, NULL AS billing_provider
                FROM messages m
                ORDER BY m.timestamp, m.id
                """
            )
            rows = conn.execute(sql).fetchall()
            source.update({"status": "scanned", "rows": len(rows)})
            for row in rows:
                _scan_hermes_message(dict(row), emit)
    except Exception as exc:  # noqa: BLE001 - scanner evidence should degrade
        source.update({"status": "error", "error": _redact_excerpt(str(exc), 200)})


def _scan_mac_sqlite_db(
    db_path: Path,
    emit: Callable[..., None],
    sources: list[JsonDict],
) -> None:
    source = {"name": "mac_sqlite_db", "path": str(db_path), "status": "missing", "rows": 0}
    sources.append(source)
    if not db_path.exists():
        return
    try:
        with _connect_readonly(db_path) as conn:
            conn.row_factory = sqlite3.Row
            _scan_mac_ledgers(_RawReader(conn), emit, sources, "mac_sqlite_db", source)
    except Exception as exc:  # noqa: BLE001
        source.update({"status": "error", "error": _redact_excerpt(str(exc), 200)})


def _scan_mac_ledgers(
    reader: "_Reader",
    emit: Callable[..., None],
    sources: list[JsonDict],
    name: str,
    source: Optional[JsonDict] = None,
) -> None:
    source = source or {"name": name, "status": "scanned", "rows": 0}
    if source not in sources:
        sources.append(source)
    total = 0
    for table, scanner in (
        ("command_audit", _scan_command_audit_row),
        ("action_events", _scan_action_event_row),
        ("observability_events", _scan_observability_row),
    ):
        if not reader.table_exists(table):
            continue
        rows = reader.query_all(_select_sql_for_table(table))
        total += len(rows)
        for row in rows:
            scanner(row, emit)
    source.update({"status": "scanned", "rows": total})


def _scan_hermes_message(row: Mapping[str, Any], emit: Callable[..., None]) -> None:
    timestamp = _coerce_time(row.get("timestamp"))
    text = _message_text(row.get("content"))
    tool_name = _clean_name(row.get("tool_name"))
    provider = _clean_name(row.get("billing_provider"))
    model = _clean_name(row.get("model"))
    base_evidence = {
        "schema": DREAM_FAILURE_EVIDENCE_SCHEMA,
        "source": "hermes.messages",
        "row_id": str(row.get("id") or ""),
        "timestamp": timestamp,
        "session_id": row.get("session_id"),
        "excerpt": _redact_excerpt(text),
    }
    dimensions = {"provider": provider, "model": model, "tool_name": tool_name}

    for called_tool in _tool_names_from_calls(row.get("tool_calls")):
        evidence = {
            **base_evidence,
            "excerpt": _redact_excerpt("tool call requested: %s" % called_tool),
        }
        emit(
            "tool_or_skill_name",
            "tool:%s" % called_tool,
            evidence,
            severity="info",
            dimensions={**dimensions, "tool_name": called_tool},
        )

    if tool_name:
        emit(
            "tool_or_skill_name",
            "tool:%s" % tool_name,
            {**base_evidence, "excerpt": _redact_excerpt("tool result: %s" % tool_name)},
            severity="info",
            dimensions=dimensions,
        )

    for skill in _extract_skill_names(text):
        emit(
            "tool_or_skill_name",
            "skill:%s" % skill,
            {**base_evidence, "excerpt": _redact_excerpt("skill referenced: %s" % skill)},
            severity="info",
            dimensions={**dimensions, "skill_name": skill},
        )

    if not _is_failure_text(text):
        return
    failure_sig = _failure_signature(text)
    emit(
        "repeated_failure",
        failure_sig,
        base_evidence,
        severity="error",
        dimensions=dimensions,
    )
    if tool_name or str(row.get("role") or "").lower() == "tool":
        emit(
            "tool_call_error",
            "tool:%s:%s" % (tool_name or "unknown", failure_sig),
            base_evidence,
            severity="error",
            dimensions=dimensions,
        )
    if _is_model_provider_text(text) or provider or model:
        emit(
            "model_provider_error",
            "provider:%s:model:%s:%s" % (provider or "unknown", model or "unknown", failure_sig),
            base_evidence,
            severity="error",
            dimensions=dimensions,
        )


def _scan_command_audit_row(row: Mapping[str, Any], emit: Callable[..., None]) -> None:
    argv = _json_list(row.get("argv"))
    metadata = _json_dict(row.get("metadata"))
    phase = str(row.get("phase") or "").lower()
    returncode = row.get("returncode")
    command = _command_label(argv)
    timestamp = _coerce_time(row.get("completed_at") or row.get("created_at") or row.get("started_at"))
    failed = phase in {"failed", "timeout", "error"} or (
        returncode is not None and str(returncode) not in {"0", "None"}
    )
    text = " ".join(
        [
            " ".join(argv),
            phase,
            str(returncode or ""),
            json.dumps(metadata, sort_keys=True, default=str),
        ]
    )
    evidence = {
        "schema": DREAM_FAILURE_EVIDENCE_SCHEMA,
        "source": "mac.command_audit",
        "row_id": str(row.get("id") or ""),
        "timestamp": timestamp,
        "agent_id": row.get("agent_id"),
        "task_id": row.get("task_id"),
        "command_id": row.get("command_id"),
        "excerpt": _redact_excerpt(
            "command %s %s returncode=%s %s"
            % (command, phase or "unknown", returncode, metadata.get("error") or "")
        ),
    }
    dimensions = {
        "agent_id": row.get("agent_id"),
        "task_id": row.get("task_id"),
        "command": command,
    }
    for tool in _tool_names_from_command(argv):
        emit(
            "tool_or_skill_name",
            "tool:%s" % tool,
            evidence,
            severity="info",
            dimensions={**dimensions, "tool_name": tool},
        )
    if not failed:
        return
    sig = "command:%s:%s:returncode:%s" % (command, phase or "failed", returncode)
    emit("repeated_failure", sig, evidence, severity="error", dimensions=dimensions)
    if _is_test_text(text):
        emit("test_failure", sig, evidence, severity="error", dimensions=dimensions)


def _scan_action_event_row(row: Mapping[str, Any], emit: Callable[..., None]) -> None:
    attrs = _json_dict(row.get("attributes"))
    action_type = str(row.get("action_type") or "")
    action_name = str(row.get("action_name") or "")
    outcome = str(row.get("outcome") or "").lower()
    severity = str(row.get("severity") or "").lower()
    timestamp = _coerce_time(row.get("timestamp"))
    text = " ".join([action_type, action_name, outcome, severity, json.dumps(attrs, sort_keys=True, default=str)])
    tool_name = _first_clean(attrs, "tool", "tool_name")
    provider = _first_clean(attrs, "provider", "billing_provider")
    model = _first_clean(attrs, "model", "requested_model", "resolved_model", "response_model")
    evidence = {
        "schema": DREAM_FAILURE_EVIDENCE_SCHEMA,
        "source": "mac.action_events",
        "row_id": str(row.get("event_id") or ""),
        "timestamp": timestamp,
        "agent_id": row.get("agent_id"),
        "task_id": row.get("task_id"),
        "session_id": row.get("session_id"),
        "command_id": row.get("command_id"),
        "excerpt": _redact_excerpt(text),
    }
    dimensions = {
        "agent_id": row.get("agent_id"),
        "task_id": row.get("task_id"),
        "session_id": row.get("session_id"),
        "tool_name": tool_name,
        "provider": provider,
        "model": model,
    }
    _emit_names_from_text(text, evidence, dimensions, emit)
    failed = outcome in {"failure", "error"} or severity in {"error", "critical"} or _is_failure_text(text)
    if not failed:
        return
    sig = "action:%s:%s:%s" % (action_type or "unknown", action_name or "unknown", _failure_signature(text))
    emit("repeated_failure", sig, evidence, severity="error", dimensions=dimensions)
    if tool_name or "tool" in action_type.lower():
        emit("tool_call_error", sig, evidence, severity="error", dimensions=dimensions)
    if _is_model_provider_text(text) or provider or model:
        emit("model_provider_error", sig, evidence, severity="error", dimensions=dimensions)
    if _is_test_text(text):
        emit("test_failure", sig, evidence, severity="error", dimensions=dimensions)


def _scan_observability_row(row: Mapping[str, Any], emit: Callable[..., None]) -> None:
    detail = _json_dict(row.get("detail"))
    level = str(row.get("level") or "").lower()
    name = str(row.get("name") or "")
    timestamp = _coerce_time(row.get("created_at"))
    text = " ".join([name, level, json.dumps(detail, sort_keys=True, default=str)])
    provider = _first_clean(detail, "provider", "billing_provider")
    model = _first_clean(detail, "model", "requested_model", "resolved_model", "response_model")
    evidence = {
        "schema": DREAM_FAILURE_EVIDENCE_SCHEMA,
        "source": "mac.observability_events",
        "row_id": str(row.get("id") or row.get("sequence") or ""),
        "timestamp": timestamp,
        "task_id": row.get("subject_id") if row.get("subject_type") == "task" else None,
        "excerpt": _redact_excerpt(text),
    }
    dimensions = {
        "provider": provider,
        "model": model,
        "task_id": evidence.get("task_id"),
    }
    _emit_names_from_text(text, evidence, dimensions, emit)
    failed = level in {"error", "critical"} or _is_failure_text(text)
    if not failed:
        return
    sig = "observability:%s:%s" % (name or "unknown", _failure_signature(text))
    emit("repeated_failure", sig, evidence, severity="error", dimensions=dimensions)
    if _is_model_provider_text(text) or provider or model or name == "llm.route":
        emit("model_provider_error", sig, evidence, severity="error", dimensions=dimensions)
    if _is_test_text(text):
        emit("test_failure", sig, evidence, severity="error", dimensions=dimensions)


def _emit_names_from_text(
    text: str,
    evidence: JsonDict,
    dimensions: Mapping[str, Any],
    emit: Callable[..., None],
) -> None:
    for skill in _extract_skill_names(text):
        emit(
            "tool_or_skill_name",
            "skill:%s" % skill,
            {**evidence, "excerpt": _redact_excerpt("skill referenced: %s" % skill)},
            severity="info",
            dimensions={**dimensions, "skill_name": skill},
        )
    for key in ("tool_name", "tool"):
        value = dimensions.get(key)
        if value:
            emit(
                "tool_or_skill_name",
                "tool:%s" % value,
                evidence,
                severity="info",
                dimensions=dimensions,
            )


@dataclass
class _CandidateBucket:
    kind: str
    signature: str
    severity: str
    max_evidence: int
    count: int = 0
    first_seen_at: str = ""
    last_seen_at: str = ""
    evidence: list[JsonDict] = field(default_factory=list)
    tools: Counter[str] = field(default_factory=Counter)
    skills: Counter[str] = field(default_factory=Counter)
    providers: Counter[str] = field(default_factory=Counter)
    models: Counter[str] = field(default_factory=Counter)
    commands: Counter[str] = field(default_factory=Counter)
    agents: Counter[str] = field(default_factory=Counter)
    tasks: Counter[str] = field(default_factory=Counter)
    sessions: Counter[str] = field(default_factory=Counter)

    def add(self, evidence: JsonDict, dimensions: Mapping[str, Any]) -> None:
        self.count += 1
        timestamp = str(evidence.get("timestamp") or "")
        if timestamp and (not self.first_seen_at or timestamp < self.first_seen_at):
            self.first_seen_at = timestamp
        if timestamp and (not self.last_seen_at or timestamp > self.last_seen_at):
            self.last_seen_at = timestamp
        self._count(self.tools, dimensions.get("tool_name"))
        self._count(self.skills, dimensions.get("skill_name"))
        self._count(self.providers, dimensions.get("provider"))
        self._count(self.models, dimensions.get("model"))
        self._count(self.commands, dimensions.get("command"))
        self._count(self.agents, dimensions.get("agent_id") or evidence.get("agent_id"))
        self._count(self.tasks, dimensions.get("task_id") or evidence.get("task_id"))
        self._count(self.sessions, dimensions.get("session_id") or evidence.get("session_id"))
        if len(self.evidence) < self.max_evidence:
            self.evidence.append(_clean_evidence(evidence))

    @staticmethod
    def _count(counter: Counter[str], value: Any) -> None:
        text = _clean_name(value)
        if text:
            counter[text] += 1

    def to_dict(self) -> JsonDict:
        return {
            "schema": DREAM_FAILURE_CANDIDATE_SCHEMA,
            "candidate_id": _stable_id("dreamcand", self.kind, self.signature),
            "kind": self.kind,
            "signature": self.signature,
            "severity": self.severity,
            "count": self.count,
            "window": {
                "first_seen_at": self.first_seen_at or None,
                "last_seen_at": self.last_seen_at or None,
            },
            "dimensions": {
                "tools": _counter_rows(self.tools),
                "skills": _counter_rows(self.skills),
                "providers": _counter_rows(self.providers),
                "models": _counter_rows(self.models),
                "commands": _counter_rows(self.commands),
                "agents": _counter_rows(self.agents),
                "tasks": _counter_rows(self.tasks),
                "sessions": _counter_rows(self.sessions),
            },
            "evidence_count": self.count,
            "evidence_truncated": self.count > len(self.evidence),
            "evidence": self.evidence,
        }


class _Reader:
    def query_all(self, sql: str) -> list[JsonDict]:
        raise NotImplementedError

    def table_exists(self, table: str) -> bool:
        raise NotImplementedError


class _RawReader(_Reader):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def query_all(self, sql: str) -> list[JsonDict]:
        return [dict(row) for row in self.conn.execute(sql).fetchall()]

    def table_exists(self, table: str) -> bool:
        return _raw_table_exists(self.conn, table)


class _StoreReader(_Reader):
    def __init__(self, store: Any) -> None:
        self.store = store

    def query_all(self, sql: str) -> list[JsonDict]:
        return [dict(row) for row in self.store.query_all(sql)]

    def table_exists(self, table: str) -> bool:
        try:
            row = self.store.query_one(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
                (table,),
            )
            return row is not None
        except Exception:  # noqa: BLE001 - non-SQLite stores do not expose sqlite_master
            try:
                self.store.query_all("SELECT 1 FROM %s LIMIT 1" % table)
                return True
            except Exception:
                return False


def _select_sql_for_table(table: str) -> str:
    if table == "command_audit":
        return """
            SELECT id, command_id, agent_id, phase, argv, cwd, task_id, lease_id,
                   started_at, completed_at, duration_ms, returncode,
                   stdout_sha256, stderr_sha256, stdout_bytes, stderr_bytes,
                   metadata, created_at
            FROM command_audit
            ORDER BY created_at, id
        """
    if table == "action_events":
        return """
            SELECT event_id, timestamp, agent_id, hermes_instance_id, task_id,
                   session_id, sandbox_id, actor, action_type, action_name,
                   subject_type, subject_id, outcome, severity, policy_id,
                   policy_version, command_id, parent_event_id, attributes,
                   redaction_state
            FROM action_events
            ORDER BY timestamp, event_id
        """
    return """
        SELECT sequence, id, kind, layer, source, level, name, subject_type,
               subject_id, value, unit, detail, created_at
        FROM observability_events
        ORDER BY created_at, sequence
    """


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = "file:%s?mode=ro" % quote(str(path.resolve()), safe="/:")
    return sqlite3.connect(uri, uri=True)


def _raw_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _coerce_time(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat(timespec="microseconds")
    text = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return datetime.fromtimestamp(float(text), timezone.utc).isoformat(timespec="microseconds")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except ValueError:
        return text[:80]


def _timestamp_in_window(timestamp: str, since: str, until: str) -> bool:
    if since and (not timestamp or timestamp < since):
        return False
    if until and timestamp and timestamp > until:
        return False
    return True


def _json_dict(value: Any) -> JsonDict:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        loaded = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not value:
        return []
    try:
        loaded = json.loads(str(value))
    except (TypeError, ValueError):
        return [str(value)]
    if isinstance(loaded, list):
        return [str(item) for item in loaded]
    return [str(loaded)]


def _message_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    text = str(value)
    try:
        loaded = json.loads(text)
    except (TypeError, ValueError):
        return text
    if isinstance(loaded, (dict, list)):
        return json.dumps(loaded, sort_keys=True, default=str)
    return text


def _tool_names_from_calls(value: Any) -> list[str]:
    if not value:
        return []
    try:
        loaded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return []
    calls = loaded if isinstance(loaded, list) else [loaded]
    out: list[str] = []
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
        name = _clean_name(function.get("name") or call.get("name") or call.get("tool_name"))
        if name:
            out.append(name)
    return sorted(set(out))


def _tool_names_from_command(argv: Iterable[str]) -> list[str]:
    tools = []
    for arg in argv:
        base = Path(str(arg)).name
        if base in {"pytest", "coverage", "tox", "python", "python3"}:
            tools.append(base)
        elif base.startswith("mac-") or base.startswith("mac_"):
            tools.append(base)
    return sorted(set(tools))


def _command_label(argv: list[str]) -> str:
    if not argv:
        return "command"
    first = Path(argv[0]).name or argv[0]
    if first in {"python", "python3"} and len(argv) > 1:
        return "%s %s" % (first, Path(argv[1]).name)
    return first[:120]


def _extract_skill_names(text: str) -> list[str]:
    found = []
    for match in _SKILL_RE.finditer(text or ""):
        name = match.group(1).strip(" .,/\\")
        if name and not name.lower().endswith((".md", ".json", ".py")):
            found.append(name)
    return sorted(set(found))


def _is_failure_text(text: str) -> bool:
    return bool(_FAILURE_RE.search(text or ""))


def _is_model_provider_text(text: str) -> bool:
    return bool(_MODEL_RE.search(text or ""))


def _is_test_text(text: str) -> bool:
    return bool(_TEST_RE.search(text or ""))


def _failure_signature(text: str) -> str:
    cleaned = _redact_excerpt(text, 600).lower()
    patterns = (
        r"([a-z_][\w.]*(?:error|exception))\s*[:\-]\s*([^.;\n]{0,140})",
        r"(no module named\s+[\"']?[^\"'\s,;]+)",
        r"(returned non-zero exit status\s+\d+)",
        r"(exit status\s+\d+)",
        r"(returncode[=\s:]+\d+)",
        r"(rate limit|429|quota exceeded|context length)",
    )
    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if match:
            cleaned = " ".join(part for part in match.groups() if part)
            break
    cleaned = re.sub(r"\b0x[0-9a-f]+\b", "<hex>", cleaned)
    cleaned = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", cleaned)
    cleaned = re.sub(r"/[^\s,;:]+", "<path>", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,:;")
    return cleaned[:180] or "failure"


def _clean_signature(signature: str) -> str:
    return re.sub(r"\s+", " ", _redact_excerpt(signature, 300).lower()).strip(" .,:;")


def _redact_excerpt(value: Any, max_chars: int = 320) -> str:
    text = str(value or "").replace("\x00", "")
    text = _URL_USERINFO_RE.sub(r"\1<redacted>@", text)
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    text = _SECRET_ASSIGN_RE.sub(lambda m: "%s<redacted>" % m.group(1), text)
    text = _KNOWN_TOKEN_RE.sub("<redacted>", text)
    text = _LONG_ATOM_RE.sub("<redacted-long-value>", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _clean_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _redact_excerpt(text, 120)


def _first_clean(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value:
            return _clean_name(value)
    return ""


def _clean_evidence(evidence: Mapping[str, Any]) -> JsonDict:
    out: JsonDict = {}
    for key, value in evidence.items():
        if value is None or value == "":
            continue
        if key == "excerpt":
            out[key] = _redact_excerpt(value)
        else:
            out[key] = value
    return out


def _counter_rows(counter: Counter[str]) -> list[JsonDict]:
    return [
        {"name": name, "count": count}
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]
    return "%s_%s" % (prefix, digest)
