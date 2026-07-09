from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple

from mac.models import MACError, TERMINAL_TASK_STATES, TaskState


PLAN_SCHEMA = "mac.local_ledger_migration_plan.v1"
RESULT_SCHEMA = "mac.local_ledger_migration_result.v1"
RETIREMENT_SCHEMA = "mac.local_ledger_retirement_result.v1"
PROVENANCE_SCHEMA = "mac.local_ledger_task_migration.v1"
ARCHIVE_SCHEMA = "mac.local_ledger_archive.v1"
ACTIVE_TASK_STATES = frozenset(
    {
        TaskState.OPEN.value,
        TaskState.WAITING.value,
        TaskState.BLOCKED.value,
        TaskState.CLAIMED.value,
        TaskState.RUNNING.value,
        TaskState.NEEDS_REVIEW.value,
        TaskState.REVIEWING.value,
    }
)


class LocalLedgerMigrationError(MACError):
    """Raised when a local ledger cannot be transferred without data loss."""


class TaskMigrationTarget(Protocol):
    def list_tasks(self, state: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Any]: ...

    def create_task(self, title: str, **kwargs: Any) -> Any: ...

    def task_detail(self, task_id: str) -> Any: ...


@dataclass(frozen=True)
class LocalTaskCandidate:
    id: str
    title: str
    description: str
    project: Optional[str]
    priority: int
    state: str
    required_capabilities: List[str]
    dependencies: List[str]
    active_dependencies: List[str]
    satisfied_dependencies: List[str]
    metadata: Dict[str, Any]
    max_attempts: int
    created_at: str
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LocalLedgerPlan:
    source_db: str
    source_database_id: str
    exists: bool
    database_size_bytes: int
    task_count: int
    active_task_count: int
    state_counts: Dict[str, int]
    tasks: List[LocalTaskCandidate]
    migration_order: List[str]
    issues: List[str]
    can_migrate: bool
    migration_command: str
    schema: str = PLAN_SCHEMA

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["tasks"] = [task.to_dict() for task in self.tasks]
        return data


@dataclass(frozen=True)
class LocalLedgerMigrationResult:
    migration_id: str
    source_db: str
    source_database_id: str
    remote_task_ids: Dict[str, str]
    reused_remote_task_ids: List[str]
    verified_task_ids: List[str]
    cancelled_local_task_ids: List[str]
    archive_path: str
    archive_sha256: str
    manifest_path: str
    completed_at: str
    schema: str = RESULT_SCHEMA

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LocalLedgerRetirementResult:
    source_db: str
    source_database_id: str
    archive_path: str
    archive_sha256: str
    manifest_path: str
    completed_at: str
    schema: str = RETIREMENT_SCHEMA

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def default_local_db_path() -> Path:
    return Path.home() / ".mac" / "mac.db"


def default_archive_dir() -> Path:
    return Path.home() / ".mac" / "archive"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        return dict(result) if isinstance(result, dict) else {}
    return {}


def _json_object(value: Any, *, task_id: str, field: str, issues: List[str]) -> Dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        issues.append("task %s has invalid %s JSON: %s" % (task_id, field, exc))
        return {}
    if not isinstance(parsed, dict):
        issues.append("task %s %s must be a JSON object" % (task_id, field))
        return {}
    return dict(parsed)


def _json_list(value: Any, *, task_id: str, field: str, issues: List[str]) -> List[str]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        issues.append("task %s has invalid %s JSON: %s" % (task_id, field, exc))
        return []
    if not isinstance(parsed, list):
        issues.append("task %s %s must be a JSON array" % (task_id, field))
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _source_database_id(path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24]
    return "localdb_%s" % digest


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _topological_order(
    active: Mapping[str, Dict[str, Any]],
    all_states: Mapping[str, str],
    issues: List[str],
) -> Tuple[List[str], Dict[str, List[str]], Dict[str, List[str]]]:
    active_dependencies: Dict[str, List[str]] = {}
    satisfied_dependencies: Dict[str, List[str]] = {}
    dependents: Dict[str, List[str]] = {task_id: [] for task_id in active}
    indegree: Dict[str, int] = {task_id: 0 for task_id in active}

    for task_id, task in active.items():
        active_deps: List[str] = []
        satisfied: List[str] = []
        for dependency in task["dependencies"]:
            state = all_states.get(dependency)
            if state is None:
                issues.append("task %s depends on missing local task %s" % (task_id, dependency))
            elif dependency in active:
                active_deps.append(dependency)
                dependents[dependency].append(task_id)
                indegree[task_id] += 1
            elif state == TaskState.COMPLETED.value:
                satisfied.append(dependency)
            else:
                issues.append(
                    "task %s depends on terminal local task %s in state %s"
                    % (task_id, dependency, state)
                )
        active_dependencies[task_id] = active_deps
        satisfied_dependencies[task_id] = satisfied

    ready = sorted(
        (task_id for task_id, count in indegree.items() if count == 0),
        key=lambda task_id: (active[task_id]["created_at"], task_id),
    )
    order: List[str] = []
    while ready:
        task_id = ready.pop(0)
        order.append(task_id)
        for dependent in sorted(dependents[task_id]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=lambda value: (active[value]["created_at"], value))
    if len(order) != len(active):
        cycle = sorted(task_id for task_id, count in indegree.items() if count > 0)
        issues.append("active local tasks contain a dependency cycle: %s" % ", ".join(cycle))
    return order, active_dependencies, satisfied_dependencies


def inspect_local_ledger(source_db: Optional[Path | str] = None) -> LocalLedgerPlan:
    path = Path(source_db or default_local_db_path()).expanduser().resolve()
    source_id = _source_database_id(path)
    command = "mac migrate local-ledger --execute"
    if path != default_local_db_path().expanduser().resolve():
        command += " --source-db %s" % path
    if not path.is_file():
        return LocalLedgerPlan(
            source_db=str(path),
            source_database_id=source_id,
            exists=False,
            database_size_bytes=0,
            task_count=0,
            active_task_count=0,
            state_counts={},
            tasks=[],
            migration_order=[],
            issues=[],
            can_migrate=False,
            migration_command=command,
        )

    issues: List[str] = []
    uri = "file:%s?mode=ro" % path.as_posix()
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise LocalLedgerMigrationError("could not open local ledger %s: %s" % (path, exc)) from exc
    try:
        try:
            if not _table_exists(conn, "tasks"):
                return LocalLedgerPlan(
                    source_db=str(path),
                    source_database_id=source_id,
                    exists=True,
                    database_size_bytes=path.stat().st_size,
                    task_count=0,
                    active_task_count=0,
                    state_counts={},
                    tasks=[],
                    migration_order=[],
                    issues=["database has no MAC tasks table"],
                    can_migrate=False,
                    migration_command=command,
                )
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at, id").fetchall()
        except sqlite3.Error as exc:
            raise LocalLedgerMigrationError(
                "could not inspect local ledger %s: %s" % (path, exc)
            ) from exc
    finally:
        conn.close()

    state_counts: Dict[str, int] = {}
    all_states: Dict[str, str] = {}
    active: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        task_id = str(row["id"])
        state = str(row["state"])
        state_counts[state] = state_counts.get(state, 0) + 1
        all_states[task_id] = state
    for row in rows:
        task_id = str(row["id"])
        state = str(row["state"])
        if state not in ACTIVE_TASK_STATES:
            continue
        dependencies = _json_list(
            row["dependencies"], task_id=task_id, field="dependencies", issues=issues
        )
        metadata = _json_object(row["metadata"], task_id=task_id, field="metadata", issues=issues)
        capabilities = _json_list(
            row["required_capabilities"],
            task_id=task_id,
            field="required_capabilities",
            issues=issues,
        )
        active[task_id] = {
            "id": task_id,
            "title": str(row["title"]),
            "description": str(row["description"]),
            "project": row["project"],
            "priority": int(row["priority"]),
            "state": state,
            "required_capabilities": capabilities,
            "dependencies": dependencies,
            "metadata": metadata,
            "max_attempts": int(row["max_attempts"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    order, active_dependencies, satisfied_dependencies = _topological_order(
        active, all_states, issues
    )
    candidates = [
        LocalTaskCandidate(
            **active[task_id],
            active_dependencies=active_dependencies.get(task_id, []),
            satisfied_dependencies=satisfied_dependencies.get(task_id, []),
        )
        for task_id in order
    ]
    return LocalLedgerPlan(
        source_db=str(path),
        source_database_id=source_id,
        exists=True,
        database_size_bytes=path.stat().st_size,
        task_count=len(rows),
        active_task_count=len(active),
        state_counts=dict(sorted(state_counts.items())),
        tasks=candidates,
        migration_order=order,
        issues=issues,
        can_migrate=bool(active) and not issues and len(order) == len(active),
        migration_command=command,
    )


def local_ledger_notice(source_db: Optional[Path | str] = None) -> Optional[Dict[str, Any]]:
    plan = inspect_local_ledger(source_db)
    if not plan.exists or plan.active_task_count == 0:
        return None
    return {
        "status": "migration_required" if plan.can_migrate else "manual_review_required",
        "source_db": plan.source_db,
        "active_task_count": plan.active_task_count,
        "state_counts": plan.state_counts,
        "issues": plan.issues,
        "next_command": plan.migration_command,
        "message": (
            "This client has active tasks in an isolated SQLite authority. "
            "They are not visible to the hub; inspect and explicitly migrate them."
        ),
    }


def _migration_id(plan: LocalLedgerPlan) -> str:
    material = "%s\n%s" % (plan.source_database_id, "\n".join(plan.migration_order))
    return "localmig_%s" % hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _sanitize_metadata(
    task: LocalTaskCandidate,
    *,
    migration_id: str,
    source_database_id: str,
) -> Dict[str, Any]:
    metadata = json.loads(json.dumps(task.metadata))
    for key in (
        "acc_metadata",
        "execution_contract",
        "repository_ref_lifecycle",
        "toolchain_requirements",
    ):
        metadata.pop(key, None)
    origin = metadata.get("origin")
    if isinstance(origin, dict):
        origin = dict(origin)
        for key in (
            "repository_contract",
            "repository_id",
            "repository_name",
            "repository_path",
        ):
            origin.pop(key, None)
        if origin:
            metadata["origin"] = origin
        else:
            metadata.pop("origin", None)
    metadata["local_ledger_migration"] = {
        "schema": PROVENANCE_SCHEMA,
        "migration_id": migration_id,
        "source_database_id": source_database_id,
        "source_task_id": task.id,
        "source_state": task.state,
        "source_dependencies": task.dependencies,
        "source_updated_at": task.updated_at,
    }
    return metadata


def _remote_provenance_index(
    target: TaskMigrationTarget,
    source_database_id: str,
) -> Dict[str, str]:
    index: Dict[str, str] = {}
    for item in target.list_tasks():
        task = _as_dict(item)
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        provenance = metadata.get("local_ledger_migration")
        if not isinstance(provenance, dict):
            continue
        if provenance.get("schema") != PROVENANCE_SCHEMA:
            continue
        if provenance.get("source_database_id") != source_database_id:
            continue
        source_task_id = str(provenance.get("source_task_id") or "")
        target_task_id = str(task.get("id") or "")
        if not source_task_id or not target_task_id:
            continue
        existing = index.get(source_task_id)
        if existing and existing != target_task_id:
            raise LocalLedgerMigrationError(
                "hub contains multiple migrated copies of local task %s: %s, %s"
                % (source_task_id, existing, target_task_id)
            )
        index[source_task_id] = target_task_id
    return index


def _remote_task(target: TaskMigrationTarget, task_id: str) -> Dict[str, Any]:
    detail = _as_dict(target.task_detail(task_id))
    task = detail.get("task") if isinstance(detail.get("task"), dict) else detail
    return dict(task) if isinstance(task, dict) else {}


def _verify_remote_task(
    target: TaskMigrationTarget,
    candidate: LocalTaskCandidate,
    target_task_id: str,
    expected_dependencies: List[str],
    source_database_id: str,
) -> None:
    remote = _remote_task(target, target_task_id)
    errors: List[str] = []
    expected = {
        "title": candidate.title,
        "project": candidate.project,
        "priority": candidate.priority,
        "dependencies": expected_dependencies,
        "required_capabilities": candidate.required_capabilities,
        "max_attempts": candidate.max_attempts,
    }
    for key, value in expected.items():
        if remote.get(key) != value:
            errors.append("%s expected %r got %r" % (key, value, remote.get(key)))
    metadata = remote.get("metadata") if isinstance(remote.get("metadata"), dict) else {}
    provenance = metadata.get("local_ledger_migration")
    if not isinstance(provenance, dict):
        errors.append("migration provenance is missing")
    else:
        if provenance.get("source_database_id") != source_database_id:
            errors.append("source_database_id does not match")
        if provenance.get("source_task_id") != candidate.id:
            errors.append("source_task_id does not match")
    if errors:
        raise LocalLedgerMigrationError(
            "remote verification failed for %s -> %s: %s"
            % (candidate.id, target_task_id, "; ".join(errors))
        )


def _load_source_secret_key(source_db: Path, explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    live = os.environ.get("MAC_SECRET_KEY")
    if live:
        return live
    env_path = source_db.parent / ".env"
    if env_path.is_file():
        try:
            from mac.fleet_env import parse_env_file

            value = parse_env_file(env_path).get("MAC_SECRET_KEY")
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - converted to a migration error below.
            raise LocalLedgerMigrationError(
                "could not read source MAC_SECRET_KEY from %s: %s" % (env_path, exc)
            ) from exc
    raise LocalLedgerMigrationError(
        "MAC_SECRET_KEY is required to cancel the source tasks safely; set it in "
        "the environment or in %s" % env_path
    )


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(0o600)
        temp_path.replace(path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        finally:
            raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_database(source_db: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    source = sqlite3.connect(str(source_db))
    target = sqlite3.connect(str(destination))
    try:
        source.execute("PRAGMA wal_checkpoint(FULL)")
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise LocalLedgerMigrationError("database backup failed integrity_check")
        target.commit()
    except Exception:
        target.close()
        source.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        target.close()
        source.close()
    destination.chmod(0o600)


def _restore_database(recovery_path: Path, source_db: Path) -> None:
    source_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(source_db) + suffix).unlink(missing_ok=True)
    _backup_database(recovery_path, source_db)
    recovery_path.unlink(missing_ok=True)


def _archive_database(
    source_db: Path,
    archive_dir: Path,
    migration_id: str,
    cancelled_task_ids: Iterable[str],
) -> Tuple[Path, str]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.chmod(0o700)
    archive_path = archive_dir / ("mac-%s-%s.db" % (stamp, migration_id))
    if archive_path.exists():
        raise LocalLedgerMigrationError("archive already exists: %s" % archive_path)

    _backup_database(source_db, archive_path)
    target = sqlite3.connect(str(archive_path))
    try:
        for task_id in cancelled_task_ids:
            row = target.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None or row[0] != TaskState.CANCELLED.value:
                raise LocalLedgerMigrationError(
                    "archive does not contain cancelled task %s" % task_id
                )
        target.commit()
    except Exception:
        target.close()
        archive_path.unlink(missing_ok=True)
        raise
    else:
        target.close()

    return archive_path, _sha256(archive_path)


def _verify_archive_manifest(
    manifest_path: Path,
    *,
    archive_path: Path,
    archive_hash: str,
) -> None:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LocalLedgerMigrationError(
            "could not read archive manifest %s: %s" % (manifest_path, exc)
        ) from exc
    if payload.get("schema") != ARCHIVE_SCHEMA:
        raise LocalLedgerMigrationError("archive manifest schema does not match")
    if payload.get("status") != "archive_verified":
        raise LocalLedgerMigrationError("archive manifest is not in archive_verified state")
    if payload.get("archive_path") != str(archive_path):
        raise LocalLedgerMigrationError("archive manifest path does not match")
    if payload.get("archive_sha256") != archive_hash:
        raise LocalLedgerMigrationError("archive manifest hash does not match")
    if _sha256(archive_path) != archive_hash:
        raise LocalLedgerMigrationError("archive changed after verification")


def _remove_live_database(source_db: Path) -> None:
    source_db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(source_db) + suffix).unlink(missing_ok=True)


def retire_inactive_local_ledger(
    *,
    source_db: Optional[Path | str] = None,
    archive_dir: Optional[Path | str] = None,
) -> LocalLedgerRetirementResult:
    """Archive and remove a legacy local authority only when it has no active work."""
    plan = inspect_local_ledger(source_db)
    if not plan.exists:
        raise LocalLedgerMigrationError("local ledger does not exist: %s" % plan.source_db)
    if plan.issues:
        raise LocalLedgerMigrationError(
            "local ledger requires manual review before retirement: %s"
            % "; ".join(plan.issues)
        )
    if plan.active_task_count:
        raise LocalLedgerMigrationError(
            "local ledger contains %d active task(s); migrate them to the hub before retirement"
            % plan.active_task_count
        )

    source_path = Path(plan.source_db)
    archive_root = Path(archive_dir or default_archive_dir()).expanduser().resolve()
    retirement_id = "localretire_%s" % hashlib.sha256(
        plan.source_database_id.encode("utf-8")
    ).hexdigest()[:24]
    archive_path, archive_hash = _archive_database(
        source_path,
        archive_root,
        retirement_id,
        (),
    )
    manifest_path = archive_path.with_suffix(archive_path.suffix + ".manifest.json")
    payload: Dict[str, Any] = {
        "schema": ARCHIVE_SCHEMA,
        "status": "archive_verified",
        "retirement_schema": RETIREMENT_SCHEMA,
        "retirement_id": retirement_id,
        "source_db": str(source_path),
        "source_database_id": plan.source_database_id,
        "source_plan": plan.to_dict(),
        "archive_path": str(archive_path),
        "archive_sha256": archive_hash,
        "archived_at": _utcnow(),
    }
    _write_json_atomic(manifest_path, payload)
    _verify_archive_manifest(
        manifest_path,
        archive_path=archive_path,
        archive_hash=archive_hash,
    )
    _remove_live_database(source_path)
    completed_at = _utcnow()
    payload.update({"status": "completed", "completed_at": completed_at})
    _write_json_atomic(manifest_path, payload)
    return LocalLedgerRetirementResult(
        source_db=str(source_path),
        source_database_id=plan.source_database_id,
        archive_path=str(archive_path),
        archive_sha256=archive_hash,
        manifest_path=str(manifest_path),
        completed_at=completed_at,
    )


def migrate_local_ledger(
    target: TaskMigrationTarget,
    *,
    source_db: Optional[Path | str] = None,
    archive_dir: Optional[Path | str] = None,
    actor: str = "local-ledger-migrator",
    source_secret_key: Optional[str] = None,
) -> LocalLedgerMigrationResult:
    plan = inspect_local_ledger(source_db)
    if not plan.exists:
        raise LocalLedgerMigrationError("local ledger does not exist: %s" % plan.source_db)
    if plan.active_task_count == 0:
        raise LocalLedgerMigrationError("local ledger has no active tasks to migrate")
    if not plan.can_migrate:
        raise LocalLedgerMigrationError(
            "local ledger requires manual repair before migration: %s"
            % "; ".join(plan.issues)
        )

    source_path = Path(plan.source_db)
    archive_root = Path(archive_dir or default_archive_dir()).expanduser().resolve()
    migration_id = _migration_id(plan)
    existing = _remote_provenance_index(target, plan.source_database_id)
    mapping: Dict[str, str] = {}
    reused: List[str] = []

    by_id = {task.id: task for task in plan.tasks}
    for source_task_id in plan.migration_order:
        candidate = by_id[source_task_id]
        remote_dependencies = [mapping[dep] for dep in candidate.active_dependencies]
        target_task_id = existing.get(source_task_id)
        if target_task_id:
            reused.append(target_task_id)
        else:
            created = _as_dict(
                target.create_task(
                    candidate.title,
                    description=candidate.description,
                    project=candidate.project,
                    priority=candidate.priority,
                    required_capabilities=candidate.required_capabilities,
                    dependencies=remote_dependencies,
                    metadata=_sanitize_metadata(
                        candidate,
                        migration_id=migration_id,
                        source_database_id=plan.source_database_id,
                    ),
                    max_attempts=candidate.max_attempts,
                    actor=actor,
                )
            )
            target_task_id = str(created.get("id") or "")
            if not target_task_id:
                raise LocalLedgerMigrationError(
                    "hub did not return an id while migrating local task %s" % source_task_id
                )
        mapping[source_task_id] = target_task_id

    verified: List[str] = []
    for source_task_id in plan.migration_order:
        candidate = by_id[source_task_id]
        remote_dependencies = [mapping[dep] for dep in candidate.active_dependencies]
        _verify_remote_task(
            target,
            candidate,
            mapping[source_task_id],
            remote_dependencies,
            plan.source_database_id,
        )
        verified.append(mapping[source_task_id])

    started_at = _utcnow()
    pending_manifest = archive_root / (migration_id + ".pending.json")
    manifest_payload: Dict[str, Any] = {
        "schema": ARCHIVE_SCHEMA,
        "status": "remote_verified",
        "migration_id": migration_id,
        "source_db": str(source_path),
        "source_database_id": plan.source_database_id,
        "started_at": started_at,
        "remote_task_ids": mapping,
        "reused_remote_task_ids": reused,
        "verified_remote_task_ids": verified,
        "source_plan": plan.to_dict(),
    }
    _write_json_atomic(pending_manifest, manifest_payload)

    from mac.services import ControlPlane
    from mac.store import SQLiteStore

    secret_key = _load_source_secret_key(source_path, source_secret_key)
    source_store = SQLiteStore(str(source_path))
    recovery_path = archive_root / (migration_id + ".recovery.db")
    cancelled: List[str] = []
    try:
        source_plane = ControlPlane(source_store, secret_key=secret_key)
        _backup_database(source_path, recovery_path)
        with source_store.transaction() as conn:
            placeholders = ", ".join("?" for _ in TERMINAL_TASK_STATES)
            current_rows = conn.execute(
                "SELECT id, updated_at FROM tasks WHERE state NOT IN (%s)" % placeholders,
                tuple(TERMINAL_TASK_STATES),
            ).fetchall()
            current = {str(row["id"]): str(row["updated_at"]) for row in current_rows}
            if set(current) != set(plan.migration_order):
                raise LocalLedgerMigrationError(
                    "local active task set changed during migration; remote copies were retained "
                    "for an idempotent retry"
                )
            for task_id in plan.migration_order:
                if current[task_id] != by_id[task_id].updated_at:
                    raise LocalLedgerMigrationError(
                        "local task %s changed during migration; retry after reviewing it" % task_id
                    )
            for task_id in plan.migration_order:
                source_plane._transition_task_in_transaction(
                    conn,
                    task_id,
                    TaskState.CANCELLED.value,
                    actor,
                    {
                        "reason": "Migrated to hub task %s" % mapping[task_id],
                        "disposition": "superseded",
                        "replacement_task_id": mapping[task_id],
                        "cleanup_grace_seconds": 7 * 24 * 60 * 60,
                        "local_ledger_migration_id": migration_id,
                    },
                )
                cancelled.append(task_id)
    except Exception:
        recovery_path.unlink(missing_ok=True)
        raise
    finally:
        source_store.close()

    try:
        archive_path, archive_hash = _archive_database(
            source_path,
            archive_root,
            migration_id,
            cancelled,
        )
        manifest_path = archive_path.with_suffix(archive_path.suffix + ".manifest.json")
        manifest_payload.update(
            {
                "status": "archive_verified",
                "archived_at": _utcnow(),
                "cancelled_local_task_ids": cancelled,
                "archive_path": str(archive_path),
                "archive_sha256": archive_hash,
            }
        )
        _write_json_atomic(manifest_path, manifest_payload)
        _verify_archive_manifest(
            manifest_path,
            archive_path=archive_path,
            archive_hash=archive_hash,
        )
        _remove_live_database(source_path)
        completed_at = _utcnow()
        manifest_payload.update({"status": "completed", "completed_at": completed_at})
        _write_json_atomic(manifest_path, manifest_payload)
    except Exception:
        _restore_database(recovery_path, source_path)
        raise
    recovery_path.unlink(missing_ok=True)
    pending_manifest.unlink(missing_ok=True)
    return LocalLedgerMigrationResult(
        migration_id=migration_id,
        source_db=str(source_path),
        source_database_id=plan.source_database_id,
        remote_task_ids=mapping,
        reused_remote_task_ids=reused,
        verified_task_ids=verified,
        cancelled_local_task_ids=cancelled,
        archive_path=str(archive_path),
        archive_sha256=archive_hash,
        manifest_path=str(manifest_path),
        completed_at=completed_at,
    )
