"""Generic migration importer for external task systems.

ACC, Linear, JIRA, etc. export shapes vary. Rather than hard-code a reader per
source, this importer accepts a normalized JSONL stream of records and replays
them against a `ControlPlane`. The exporting tool is responsible for mapping
its native shape into this format; the importer enforces the contract.

Each line is a JSON object with a `record` discriminator:

    {"record": "tenant",     "name": "personal", "metadata": {...}}
    {"record": "user",       "tenant": "personal", "handle": "jordan", ...}
    {"record": "task",       "title": "...", "metadata": {"source": "acc", "external_id": "ACC-42"}, ...}
    {"record": "evidence",   "task_ref": "acc:ACC-42", "kind": "test", "uri": "...", ...}
    {"record": "provenance", "task_ref": "acc:ACC-42", "event_type": "imported", ...}

`task_ref` is "source:external_id" — the importer resolves it to the local task
id by looking up the project bridge entry. Tenants/users/personas use natural
keys (name, handle).

Note: "provenance" records land in `memory_records`, not `task_history`. This is
deliberate — `task_history` is reserved for transitions that the live system
produces under its own state machine; migrated history is provenance, not
authoritative lifecycle. The unified `events` stream surfaces provenance rows
as `subject_type='task'`, `event_type='task.memory_recorded'`. The older
record name "history" is still accepted as an alias for back-compat.

The importer is idempotent on natural keys: re-running the same stream produces
no duplicate identity rows, and tasks deduplicate via `import_project_item`
when `record="task"` carries `source` + `external_id`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, TextIO, Tuple
from urllib.parse import quote

from mac.models import MACError, ValidationError, json_dumps, new_id, utcnow
from mac.services import ControlPlane


JsonDict = Dict[str, Any]


ACC_TERMINAL_STATUSES = {"completed", "cancelled", "failed"}
ACC_ACTIVE_STATUSES = {"claimed", "in_progress", "in-progress"}
ACC_PRIVATE_TABLES = ("bus_messages", "gateway_sessions", "conversation_chain_events")
ACC_JSON_COLUMNS = {
    "blocked_by",
    "changed_files",
    "data",
    "detail",
    "inputs",
    "metadata",
    "output",
    "payload",
    "tags",
}


@dataclass
class MigrationReport:
    """Summary of what the importer did."""

    tenants_imported: int = 0
    users_imported: int = 0
    machines_imported: int = 0
    agents_imported: int = 0
    tasks_imported: int = 0
    evidence_imported: int = 0
    provenance_imported: int = 0
    skipped: int = 0
    errors: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "tenants_imported": self.tenants_imported,
            "users_imported": self.users_imported,
            "machines_imported": self.machines_imported,
            "agents_imported": self.agents_imported,
            "tasks_imported": self.tasks_imported,
            "evidence_imported": self.evidence_imported,
            "provenance_imported": self.provenance_imported,
            "skipped": self.skipped,
            "errors": list(self.errors),
        }


@dataclass
class AccMigrationPlan:
    source_uri: str
    records: List[JsonDict]
    counts: JsonDict
    blockers: List[JsonDict]
    warnings: List[str]
    skipped_private_tables: List[JsonDict]
    soul_snapshot_paths: List[str]

    def to_dict(self) -> JsonDict:
        return {
            "source_uri": self.source_uri,
            "counts": self.counts,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "skipped_private_tables": list(self.skipped_private_tables),
            "soul_snapshot_paths": list(self.soul_snapshot_paths),
            "records_planned": len(self.records),
        }


@dataclass
class AccMigrationReport:
    mode: str
    source_uri: str
    counts: JsonDict
    blockers: List[JsonDict]
    warnings: List[str]
    skipped_private_tables: List[JsonDict]
    soul_snapshot_paths: List[str]
    import_report: Optional[MigrationReport] = None

    def to_dict(self) -> JsonDict:
        data = {
            "mode": self.mode,
            "source_uri": self.source_uri,
            "counts": self.counts,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "skipped_private_tables": list(self.skipped_private_tables),
            "soul_snapshot_paths": list(self.soul_snapshot_paths),
        }
        if self.import_report is not None:
            data["import"] = self.import_report.to_dict()
        return data


class Migrator:
    """Replay a normalized JSONL stream against a ControlPlane."""

    def __init__(self, control_plane: ControlPlane) -> None:
        self.cp = control_plane
        # task_ref ("source:external_id") -> local task id
        self._task_ref_to_id: Dict[str, str] = {}

    def import_stream(self, stream: Iterable[str]) -> MigrationReport:
        report = MigrationReport()
        for line_number, raw in enumerate(stream, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                report.skipped += 1
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                report.errors.append(
                    {"line": line_number, "error": "invalid JSON: %s" % exc}
                )
                continue
            try:
                self._apply(record, report)
            except MACError as exc:
                report.errors.append(
                    {
                        "line": line_number,
                        "error": "%s: %s" % (type(exc).__name__, exc),
                        "record": record,
                    }
                )
        return report

    def import_file(self, path: Path) -> MigrationReport:
        with path.open("r", encoding="utf-8") as handle:
            return self.import_stream(handle)

    def _apply(self, record: JsonDict, report: MigrationReport) -> None:
        kind = record.get("record")
        if kind == "tenant":
            self._apply_tenant(record)
            report.tenants_imported += 1
        elif kind == "user":
            self._apply_user(record)
            report.users_imported += 1
        elif kind == "machine":
            self._apply_machine(record)
            report.machines_imported += 1
        elif kind == "agent":
            self._apply_agent(record)
            report.agents_imported += 1
        elif kind == "task":
            self._apply_task(record)
            report.tasks_imported += 1
        elif kind == "evidence":
            self._apply_evidence(record)
            report.evidence_imported += 1
        elif kind in ("provenance", "history"):  # 'history' kept as alias
            self._apply_provenance(record)
            report.provenance_imported += 1
        else:
            raise ValidationError("unknown record type: %s" % kind)

    def _apply_tenant(self, record: JsonDict) -> None:
        name = record.get("name")
        if not name:
            raise ValidationError("tenant record requires 'name'")
        self.cp.register_tenant(name, metadata=record.get("metadata"))

    def _apply_user(self, record: JsonDict) -> None:
        tenant_name = record.get("tenant")
        if not tenant_name:
            raise ValidationError("user record requires 'tenant'")
        tenant = self.cp.get_tenant(tenant_name)
        handle = record.get("handle")
        if not handle:
            raise ValidationError("user record requires 'handle'")
        self.cp.register_user(
            tenant.id,
            handle,
            display_name=record.get("display_name", "") or "",
            metadata=record.get("metadata"),
        )

    def _apply_machine(self, record: JsonDict) -> None:
        hostname = record.get("hostname")
        if not hostname:
            raise ValidationError("machine record requires 'hostname'")
        self.cp.register_machine(
            hostname,
            labels=record.get("labels"),
            resources=record.get("resources"),
            trusted=record.get("trusted", True),
            machine_id=record.get("machine_id"),
        )

    def _apply_agent(self, record: JsonDict) -> None:
        machine_id = record.get("machine_id")
        if not machine_id:
            raise ValidationError("agent record requires 'machine_id'")
        name = record.get("name")
        if not name:
            raise ValidationError("agent record requires 'name'")
        self.cp.register_agent(
            machine_id,
            name,
            capabilities=record.get("capabilities"),
            resources=record.get("resources"),
            agent_id=record.get("agent_id"),
        )

    def _apply_task(self, record: JsonDict) -> None:
        title = record.get("title")
        if not title:
            raise ValidationError("task record requires 'title'")
        metadata = dict(record.get("metadata") or {})
        source = metadata.get("source")
        external_id = metadata.get("external_id")
        if source and external_id:
            task_id = self._import_external_task(record, metadata, source, str(external_id))
            self._task_ref_to_id["%s:%s" % (source, external_id)] = task_id
            return
        # No source — direct task creation. Caller is responsible for handling
        # duplicates if they re-run.
        task = self.cp.create_task(
            title,
            description=record.get("description", ""),
            project=record.get("project"),
            priority=int(record.get("priority", 0)),
            required_capabilities=record.get("required_capabilities"),
            dependencies=record.get("dependencies"),
            metadata=metadata,
            max_attempts=int(record.get("max_attempts", 3)),
            actor=record.get("actor", "migration"),
        )
        ref = record.get("task_ref")
        if ref:
            self._task_ref_to_id[ref] = task.id

    def _import_external_task(
        self,
        record: JsonDict,
        metadata: JsonDict,
        source: str,
        external_id: str,
    ) -> str:
        existing = self.cp.store.query_one(
            "SELECT task_id FROM project_items WHERE source = ? AND external_id = ?",
            (source, external_id),
        )
        if existing is not None:
            return existing["task_id"]

        task = self.cp.create_task(
            record["title"],
            description=record.get("description", ""),
            project=record.get("project") or source,
            priority=int(record.get("priority", 0)),
            required_capabilities=record.get("required_capabilities"),
            dependencies=record.get("dependencies"),
            metadata=metadata,
            max_attempts=int(record.get("max_attempts", 3)),
            actor=record.get("actor", "migration"),
        )
        now = utcnow()
        self.cp.store.execute(
            """
            INSERT INTO project_items (
                id, source, external_id, title, payload, task_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("item"),
                source,
                external_id,
                record["title"],
                json_dumps(record.get("payload") or {}),
                task.id,
                "imported",
                now,
                now,
            ),
        )
        self.cp.add_memory(
            task.id,
            "project_item",
            "%s:%s" % (source, external_id),
            "imported",
            "Imported %s:%s as durable task %s" % (source, external_id, task.id),
            None,
            record.get("actor", "migration"),
        )
        return task.id

    def _apply_evidence(self, record: JsonDict) -> None:
        task_id = self._resolve_task_ref(record)
        self.cp.add_evidence(
            task_id,
            record.get("kind"),
            record.get("uri"),
            record.get("summary"),
            record.get("created_by", "migration"),
            checksum=record.get("checksum"),
            metadata=record.get("metadata"),
        )

    def _apply_provenance(self, record: JsonDict) -> None:
        task_id = None if record.get("standalone") else self._resolve_task_ref(record)
        # Migrated history is provenance, not authoritative state machine
        # transitions — it lands in memory_records and surfaces in the unified
        # events stream as task.memory_recorded.
        self.cp.add_memory(
            task_id,
            subject_type=record.get("subject_type") or "migration",
            subject_id=record.get("subject_id") or record.get("event_id") or record.get("event_type"),
            record_type=record.get("event_type") or "imported",
            content=record.get("content") or json.dumps(record),
            evidence_id=None,
            created_by=record.get("actor", "migration"),
        )

    def _resolve_task_ref(self, record: JsonDict) -> str:
        task_id = record.get("task_id")
        if task_id:
            return task_id
        ref = record.get("task_ref")
        if not ref:
            raise ValidationError("record requires 'task_id' or 'task_ref'")
        if ref in self._task_ref_to_id:
            return self._task_ref_to_id[ref]
        if ":" not in ref:
            raise ValidationError(
                "task_ref must be 'source:external_id' (got %r)" % ref
            )
        source, external_id = ref.split(":", 1)
        row = self.cp.store.query_one(
            "SELECT task_id FROM project_items WHERE source = ? AND external_id = ?",
            (source, external_id),
        )
        if row is None:
            raise ValidationError("task_ref does not resolve: %s" % ref)
        self._task_ref_to_id[ref] = row["task_id"]
        return row["task_id"]


def import_jsonl(
    control_plane: ControlPlane,
    path: Optional[Path] = None,
    stream: Optional[TextIO] = None,
) -> MigrationReport:
    """Convenience entrypoint. Provide either a path or an open text stream."""
    migrator = Migrator(control_plane)
    if path is not None:
        return migrator.import_file(path)
    if stream is not None:
        return migrator.import_stream(stream)
    raise ValidationError("import_jsonl requires path or stream")


def plan_acc_sqlite_migration(
    acc_db_path: Path,
    audit_limit: int = 1000,
    agent_home: Optional[Path] = None,
) -> AccMigrationPlan:
    db_path = acc_db_path.expanduser()
    if not db_path.is_file():
        raise ValidationError("ACC database not found: %s" % acc_db_path)
    uri_path = quote(str(db_path.resolve()), safe="/:")
    conn = sqlite3.connect("file:%s?mode=ro" % uri_path, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = _acc_tables(conn)
        warnings: List[str] = []
        records: List[JsonDict] = []
        counts: JsonDict = {
            "agents": 0,
            "projects": 0,
            "tasks": 0,
            "tasks_by_status": {},
            "tasks_planned_for_import": 0,
            "terminal_tasks_skipped": 0,
            "active_tasks_blocking": 0,
            "attempts": 0,
            "audit_events": 0,
            "audit_events_planned": 0,
        }

        records.extend(_acc_agent_records(conn, tables, counts, warnings))
        projects = _acc_project_records(conn, tables, counts, warnings)
        records.extend(projects)
        task_records, blockers = _acc_task_records(conn, tables, counts, warnings)
        records.extend(task_records)
        records.extend(_acc_attempt_provenance_records(conn, tables, counts, warnings, task_records))
        records.extend(_acc_audit_provenance_records(conn, tables, counts, warnings, task_records, audit_limit))
        skipped_private = _acc_skipped_private_tables(conn, tables)
        return AccMigrationPlan(
            source_uri="sqlite://%s" % db_path.resolve(),
            records=records,
            counts=counts,
            blockers=blockers,
            warnings=warnings,
            skipped_private_tables=skipped_private,
            soul_snapshot_paths=_soul_snapshot_paths(agent_home),
        )
    finally:
        conn.close()


def migrate_acc_sqlite(
    control_plane: ControlPlane,
    acc_db_path: Path,
    mode: str = "dry-run",
    allow_active: bool = False,
    audit_limit: int = 1000,
    agent_home: Optional[Path] = None,
) -> AccMigrationReport:
    if mode not in {"dry-run", "import"}:
        raise ValidationError("mode must be dry-run or import")
    plan = plan_acc_sqlite_migration(acc_db_path, audit_limit=audit_limit, agent_home=agent_home)
    if mode == "dry-run":
        return AccMigrationReport(
            mode=mode,
            source_uri=plan.source_uri,
            counts=plan.counts,
            blockers=plan.blockers,
            warnings=plan.warnings,
            skipped_private_tables=plan.skipped_private_tables,
            soul_snapshot_paths=plan.soul_snapshot_paths,
        )
    if plan.blockers and not allow_active:
        raise ValidationError(
            "ACC migration blocked by %d claimed/in-progress task(s); drain ACC or pass --allow-active"
            % len(plan.blockers)
        )
    records = plan.records
    if allow_active:
        records = [_downgrade_active_acc_task(record) for record in records]
    import_report = Migrator(control_plane).import_stream(json.dumps(record) for record in records)
    return AccMigrationReport(
        mode=mode,
        source_uri=plan.source_uri,
        counts=plan.counts,
        blockers=plan.blockers,
        warnings=plan.warnings,
        skipped_private_tables=plan.skipped_private_tables,
        soul_snapshot_paths=plan.soul_snapshot_paths,
        import_report=import_report,
    )


def _acc_tables(conn: sqlite3.Connection) -> Set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row["name"]) for row in rows}


def _acc_columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    return {str(row["name"]) for row in conn.execute("PRAGMA table_info(%s)" % table)}


def _acc_rows(
    conn: sqlite3.Connection,
    table: str,
    order_by: Sequence[Tuple[str, str]] = (),
    limit: Optional[int] = None,
) -> List[sqlite3.Row]:
    columns = _acc_columns(conn, table)
    sql = "SELECT * FROM %s" % table
    order_parts = ["%s %s" % (column, direction) for column, direction in order_by if column in columns]
    if order_parts:
        sql += " ORDER BY " + ", ".join(order_parts)
    params: Tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (max(0, int(limit)),)
    return conn.execute(sql, params).fetchall()


def _acc_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute("SELECT COUNT(*) AS count FROM %s" % table).fetchone()["count"])


def _acc_agent_records(
    conn: sqlite3.Connection,
    tables: Set[str],
    counts: JsonDict,
    warnings: List[str],
) -> List[JsonDict]:
    if "agents" not in tables:
        warnings.append("ACC table agents not found; agents not imported")
        return []
    records = []
    for row in _acc_rows(conn, "agents", (("name", "ASC"),)):
        payload = _acc_row_payload(row)
        data = _dict(payload.get("data"))
        name = _string(payload.get("name")) or _string(data.get("name"))
        if not name:
            continue
        host = _string(payload.get("host")) or _string(data.get("host")) or "%s-host" % name
        machine_id = "acc_machine_%s" % _stable_key(host)
        agent_id = "acc_agent_%s" % _stable_key(name)
        records.append(
            {
                "record": "machine",
                "machine_id": machine_id,
                "hostname": host,
                "labels": {"migration": {"source": "acc", "agent": name}},
                "resources": _dict(data.get("resources")),
            }
        )
        records.append(
            {
                "record": "agent",
                "agent_id": agent_id,
                "machine_id": machine_id,
                "name": name,
                "capabilities": _string_list(data.get("capabilities")),
                "resources": _dict(data.get("resources")),
            }
        )
        counts["agents"] += 1
    return records


def _acc_project_records(
    conn: sqlite3.Connection,
    tables: Set[str],
    counts: JsonDict,
    warnings: List[str],
) -> List[JsonDict]:
    if "projects" not in tables:
        warnings.append("ACC table projects not found; projects not imported")
        return []
    records = []
    for row in _acc_rows(conn, "projects", (("id", "ASC"),)):
        payload = _acc_row_payload(row)
        project_id = _string(payload.get("id"))
        if not project_id:
            continue
        counts["projects"] += 1
        records.append(
            {
                "record": "provenance",
                "task_ref": None,
                "subject_type": "acc_project",
                "subject_id": project_id,
                "event_type": "acc.project_imported",
                "content": json.dumps(_redact_project_payload(payload), sort_keys=True),
                "actor": "migration",
                "standalone": True,
            }
        )
    return records


def _acc_task_records(
    conn: sqlite3.Connection,
    tables: Set[str],
    counts: JsonDict,
    warnings: List[str],
) -> Tuple[List[JsonDict], List[JsonDict]]:
    if "fleet_tasks" not in tables:
        warnings.append("ACC table fleet_tasks not found; tasks not imported")
        return [], []
    records = []
    blockers = []
    for row in _acc_rows(conn, "fleet_tasks", (("created_at", "ASC"), ("id", "ASC"))):
        payload = _acc_row_payload(row)
        task_id = _string(payload.get("id"))
        status = (_string(payload.get("status")) or "open").lower()
        if not task_id:
            continue
        counts["tasks"] += 1
        counts["tasks_by_status"][status] = counts["tasks_by_status"].get(status, 0) + 1
        if status in ACC_TERMINAL_STATUSES:
            counts["terminal_tasks_skipped"] += 1
            continue
        metadata = _dict(payload.get("metadata"))
        if status in ACC_ACTIVE_STATUSES:
            counts["active_tasks_blocking"] += 1
            blockers.append(
                {
                    "id": task_id,
                    "title": payload.get("title"),
                    "status": status,
                    "claimed_by": payload.get("claimed_by"),
                    "claim_expires_at": payload.get("claim_expires_at"),
                }
            )
        record = {
            "record": "task",
            "title": payload.get("title") or task_id,
            "description": payload.get("description") or "",
            "project": payload.get("project_id"),
            "priority": _acc_priority(payload.get("priority")),
            "required_capabilities": _acc_required_capabilities(payload, metadata),
            "metadata": {
                "source": "acc",
                "external_id": task_id,
                "acc_status": status,
                "acc_task_type": payload.get("task_type"),
                "acc_project_id": payload.get("project_id"),
                "acc_claimed_by": payload.get("claimed_by"),
                "acc_claimed_at": payload.get("claimed_at"),
                "acc_review_of": payload.get("review_of"),
                "acc_review_result": payload.get("review_result"),
                "acc_metadata": metadata,
            },
            "payload": payload,
            "actor": "migration",
            "task_ref": "acc:%s" % task_id,
        }
        records.append(record)
        records.append(
            {
                "record": "provenance",
                "task_ref": "acc:%s" % task_id,
                "event_type": "acc.task_imported",
                "content": "ACC task %s imported with original status %s" % (task_id, status),
                "actor": "migration",
            }
        )
        counts["tasks_planned_for_import"] += 1
    return records, blockers


def _acc_attempt_provenance_records(
    conn: sqlite3.Connection,
    tables: Set[str],
    counts: JsonDict,
    warnings: List[str],
    task_records: List[JsonDict],
) -> List[JsonDict]:
    if "fleet_task_attempts" not in tables:
        warnings.append("ACC table fleet_task_attempts not found; attempts not imported")
        return []
    imported_refs = _planned_task_refs(task_records)
    records = []
    for row in _acc_rows(conn, "fleet_task_attempts", (("started_at", "ASC"), ("attempt_id", "ASC"))):
        payload = _acc_row_payload(row)
        counts["attempts"] += 1
        task_ref = "acc:%s" % payload.get("task_id")
        if task_ref not in imported_refs:
            continue
        records.append(
            {
                "record": "provenance",
                "task_ref": task_ref,
                "event_type": "acc.task_attempt",
                "event_id": payload.get("attempt_id"),
                "content": json.dumps(payload, sort_keys=True),
                "actor": payload.get("agent") or "migration",
            }
        )
    return records


def _acc_audit_provenance_records(
    conn: sqlite3.Connection,
    tables: Set[str],
    counts: JsonDict,
    warnings: List[str],
    task_records: List[JsonDict],
    audit_limit: int,
) -> List[JsonDict]:
    if "work_audit_events" not in tables:
        warnings.append("ACC table work_audit_events not found; work audit not imported")
        return []
    total = _acc_count(conn, "work_audit_events")
    counts["audit_events"] = total
    if audit_limit <= 0:
        warnings.append("ACC work_audit_events skipped because audit_limit <= 0")
        return []
    if total > audit_limit:
        warnings.append("ACC work_audit_events limited to latest %d of %d rows" % (audit_limit, total))
    imported_refs = _planned_task_refs(task_records)
    records = []
    for row in _acc_rows(conn, "work_audit_events", (("seq", "DESC"),), audit_limit):
        payload = _acc_row_payload(row)
        task_id = _string(payload.get("task_id"))
        task_ref = "acc:%s" % task_id if task_id else None
        if not task_ref or task_ref not in imported_refs:
            continue
        records.append(
            {
                "record": "provenance",
                "task_ref": task_ref,
                "event_type": payload.get("event_type") or "acc.audit",
                "event_id": payload.get("event_id"),
                "content": json.dumps(payload, sort_keys=True),
                "actor": payload.get("agent") or "migration",
            }
        )
        counts["audit_events_planned"] += 1
    return records


def _acc_skipped_private_tables(conn: sqlite3.Connection, tables: Set[str]) -> List[JsonDict]:
    skipped = []
    for table in ACC_PRIVATE_TABLES:
        if table in tables:
            skipped.append(
                {
                    "table": table,
                    "rows": _acc_count(conn, table),
                    "reason": "may contain raw conversation/session memory; preserve in Hermes/ACC snapshot, do not import into mac",
                }
            )
    return skipped


def _soul_snapshot_paths(agent_home: Optional[Path]) -> List[str]:
    home = (agent_home or Path.home()).expanduser()
    return [
        str(home / ".hermes"),
        str(home / ".acc" / "data"),
        str(home / ".acc" / "agent.json"),
        str(home / ".acc" / ".env"),
        str(home / ".acc" / "auth.db*"),
        "Qdrant collections backing Hermes/ACC memory",
    ]


def _downgrade_active_acc_task(record: JsonDict) -> JsonDict:
    if record.get("record") == "task":
        metadata = dict(record.get("metadata") or {})
        if metadata.get("acc_status") in ACC_ACTIVE_STATUSES:
            metadata["migration_requeued_from_active_acc_claim"] = True
            record = dict(record)
            record["metadata"] = metadata
    return record


def _planned_task_refs(records: Iterable[JsonDict]) -> Set[str]:
    refs = set()
    for record in records:
        if record.get("record") == "task" and record.get("task_ref"):
            refs.add(record["task_ref"])
    return refs


def _acc_row_payload(row: sqlite3.Row) -> JsonDict:
    payload: JsonDict = {}
    for key in row.keys():
        value = row[key]
        payload[key] = _parse_json(value) if key in ACC_JSON_COLUMNS else value
    return payload


def _parse_json(value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _redact_project_payload(payload: JsonDict) -> JsonDict:
    data = dict(payload)
    project_data = _dict(data.get("data"))
    for key in ("git_url", "repoUrl", "repo_url", "repo"):
        if key in project_data:
            project_data[key] = "[redacted]"
    data["data"] = project_data
    return data


def _acc_priority(value: Any) -> int:
    try:
        # ACC's lower number is more urgent; mac's higher number is more urgent.
        return max(0, 100 - int(value))
    except (TypeError, ValueError):
        return 0


def _acc_required_capabilities(payload: JsonDict, metadata: JsonDict) -> List[str]:
    explicit = _string_list(metadata.get("required_capabilities"))
    if explicit:
        return explicit
    executors = _string_list(metadata.get("required_executors"))
    if executors:
        return executors
    preferred = _string(metadata.get("preferred_executor"))
    if preferred:
        return [preferred]
    task_type = _string(payload.get("task_type"))
    if task_type == "review":
        return ["review"]
    return []


def _string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return sorted({str(item).strip() for item in value if str(item).strip()})
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _stable_key(value: str) -> str:
    text = "".join(ch.lower() if ch.isalnum() else "_" for ch in value)
    return "_".join(part for part in text.split("_") if part)[:80] or "unknown"
