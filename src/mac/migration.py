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
                report.errors.append({"line": line_number, "error": "invalid JSON: %s" % exc})
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
            _trusted_internal=True,
        )

    def _apply_provenance(self, record: JsonDict) -> None:
        task_id = None if record.get("standalone") else self._resolve_task_ref(record)
        # Migrated history is provenance, not authoritative state machine
        # transitions — it lands in memory_records and surfaces in the unified
        # events stream as task.memory_recorded.
        self.cp.add_memory(
            task_id,
            subject_type=record.get("subject_type") or "migration",
            subject_id=record.get("subject_id")
            or record.get("event_id")
            or record.get("event_type"),
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
            raise ValidationError("task_ref must be 'source:external_id' (got %r)" % ref)
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
