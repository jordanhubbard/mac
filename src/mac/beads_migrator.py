"""Beads → MAC tickets migrator.

Detects whether a repository is managed by Beads and converts its
``.beads/issues.jsonl`` + memories store into MAC tasks plus a
wedow-ticket-compatible markdown mirror under ``.tickets/<id>.md``.

The hub task ledger is the canonical store; the ``.tickets/`` files
are local compatibility artifacts for one-way migrations. This repo
ignores them so task state does not accumulate as untracked source-tree
ledger files during normal MAC operation.

The migrator is idempotent: re-running on a repo with already-migrated
issues skips them by matching ``task.metadata.original_beads_id``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from mac.models import Task, TaskState, TERMINAL_TASK_STATES


BEADS_DIR_NAME = ".beads"
ISSUES_JSONL = "issues.jsonl"
TICKETS_DIR_NAME = ".tickets"


# Beads -> MAC state mapping. Beads has fewer states; in_progress and
# blocked are handled specially below because MAC requires an owner for
# claimed/running and dependency rows for blocked.
_STATUS_TO_STATE = {
    "open": TaskState.OPEN.value,
    "closed": TaskState.COMPLETED.value,
    "blocked": TaskState.BLOCKED.value,
    "deferred": TaskState.OPEN.value,
    # in_progress is preserved in metadata; MAC keeps the task OPEN
    # because no agent is actually executing it post-import.
    "in_progress": TaskState.OPEN.value,
}


@dataclass
class DetectionReport:
    repo_path: str
    has_beads_dir: bool
    has_issues_jsonl: bool
    has_embeddeddolt: bool
    issue_count: int
    open_count: int
    closed_count: int


@dataclass
class MigrationReport:
    repo_path: str
    project: str
    dry_run: bool
    detected: DetectionReport
    issues_migrated: int = 0
    issues_skipped_existing: int = 0
    issues_failed: int = 0
    memories_migrated: int = 0
    memories_failed: int = 0
    tickets_written: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["detected"] = asdict(self.detected)
        return data


def detect(repo_path: Path) -> DetectionReport:
    """Inspect a path for beads artifacts without modifying anything."""
    repo_path = Path(repo_path).expanduser()
    beads_dir = repo_path / BEADS_DIR_NAME
    jsonl = beads_dir / ISSUES_JSONL
    dolt = beads_dir / "embeddeddolt"
    issues: List[Dict[str, Any]] = []
    if jsonl.exists():
        issues = _read_issues_jsonl(jsonl)
    return DetectionReport(
        repo_path=str(repo_path),
        has_beads_dir=beads_dir.is_dir(),
        has_issues_jsonl=jsonl.is_file(),
        has_embeddeddolt=dolt.is_dir(),
        issue_count=len(issues),
        open_count=sum(1 for i in issues if i.get("status") == "open"),
        closed_count=sum(1 for i in issues if i.get("status") == "closed"),
    )


def migrate(
    repo_path: Path,
    cp: Optional[Any],
    *,
    project: str,
    actor: str = "beads-migrator",
    dry_run: bool = False,
    emit_tickets: bool = True,
    memories: Optional[Dict[str, str]] = None,
    tickets_only: bool = False,
) -> MigrationReport:
    """Migrate a beads repo into MAC tasks + .tickets/ mirror.

    ``cp`` is a ControlPlane instance. The migrator only calls
    ``cp.create_task`` and the underlying store for state fix-ups; it
    never invokes ``bd`` itself. When ``tickets_only=True``, ``cp`` may
    be ``None`` — only the .tickets/<id>.md mirror is produced.
    """
    if tickets_only and not emit_tickets:
        raise ValueError("tickets_only=True requires emit_tickets=True")
    if not tickets_only and cp is None:
        raise ValueError("cp is required unless tickets_only=True")
    repo_path = Path(repo_path).expanduser()
    detected = detect(repo_path)
    report = MigrationReport(
        repo_path=str(repo_path),
        project=project,
        dry_run=dry_run,
        detected=detected,
    )
    if not detected.has_issues_jsonl:
        report.errors.append("no .beads/issues.jsonl found at %s" % repo_path)
        return report

    issues = _read_issues_jsonl(repo_path / BEADS_DIR_NAME / ISSUES_JSONL)
    tickets_dir = repo_path / TICKETS_DIR_NAME
    if emit_tickets and not dry_run:
        tickets_dir.mkdir(exist_ok=True)

    bead_id_to_task_id: Dict[str, str] = {}
    for issue in issues:
        beads_id = str(issue.get("id") or "").strip()
        if not beads_id:
            report.issues_failed += 1
            report.errors.append("issue missing id: %r" % issue)
            continue
        existing_task_id: Optional[str] = None
        if not tickets_only:
            try:
                existing = _find_task_by_beads_id(cp, beads_id)
            except Exception as exc:  # noqa: BLE001 - migration is best-effort
                report.issues_failed += 1
                report.errors.append("lookup failed for %s: %s" % (beads_id, exc))
                continue
            if existing is not None:
                report.issues_skipped_existing += 1
                existing_task_id = existing.id
                bead_id_to_task_id[beads_id] = existing.id
                if emit_tickets and not dry_run:
                    _write_ticket(tickets_dir, issue, existing.id)
                    report.tickets_written += 1
                continue
            try:
                task_id = _create_task_from_bead(
                    cp, issue, project=project, actor=actor, dry_run=dry_run
                )
            except Exception as exc:  # noqa: BLE001 - migration is best-effort
                report.issues_failed += 1
                report.errors.append("create failed for %s: %s" % (beads_id, exc))
                continue
            if task_id is not None:
                bead_id_to_task_id[beads_id] = task_id
            report.issues_migrated += 1
            if emit_tickets and not dry_run and task_id is not None:
                _write_ticket(tickets_dir, issue, task_id)
                report.tickets_written += 1
        else:
            # tickets-only path: skip DB entirely; use the beads id as
            # the mac-task-id placeholder so the frontmatter still
            # round-trips against future DB-aware imports.
            report.issues_migrated += 1
            if not dry_run:
                _write_ticket(tickets_dir, issue, "pending:%s" % beads_id)
                report.tickets_written += 1

    if memories and not tickets_only:
        for key, value in memories.items():
            if key == "schema_version":
                continue
            try:
                if not dry_run:
                    _import_memory(cp, project, key, value, actor=actor)
                report.memories_migrated += 1
            except Exception as exc:  # noqa: BLE001
                report.memories_failed += 1
                report.errors.append("memory %s failed: %s" % (key, exc))
    return report


# Internal helpers ----------------------------------------------------


def _read_issues_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("_type") == "issue":
            out.append(obj)
    return out


def read_beads_memories_via_cli(repo_path: Path) -> Dict[str, str]:
    """Best-effort: invoke `bd memories --json` against the repo and
    return the resulting dict. Returns {} on any failure; the migrator
    only treats memories as an additive concern.
    """
    bd_path = shutil.which("bd")
    if not bd_path:
        return {}
    try:
        completed = subprocess.run(
            [bd_path, "memories", "--json"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0:
        return {}
    try:
        parsed = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(k): str(v) if not isinstance(v, str) else v
        for k, v in parsed.items()
        if k != "schema_version"
    }


def _find_task_by_beads_id(cp: Any, beads_id: str) -> Optional[Task]:
    rows = cp.store.query_all(
        """
        SELECT * FROM tasks
        WHERE json_extract(metadata, '$.original_beads_id') = ?
        LIMIT 1
        """,
        (beads_id,),
    )
    if not rows:
        return None
    return cp.get_task(rows[0]["id"])


def _create_task_from_bead(
    cp: Any,
    issue: Dict[str, Any],
    *,
    project: str,
    actor: str,
    dry_run: bool,
) -> Optional[str]:
    title = str(issue.get("title") or "").strip() or ("Imported %s" % issue.get("id"))
    description = str(issue.get("description") or "")
    priority = _coerce_priority(issue.get("priority"))
    bead_id = str(issue.get("id"))
    status = str(issue.get("status") or "open").lower()
    metadata: Dict[str, Any] = {
        "original_beads_id": bead_id,
        "beads_status": status,
        "beads_type": issue.get("issue_type"),
        "beads_priority": issue.get("priority"),
        "beads_assignee": issue.get("assignee"),
        "beads_owner": issue.get("owner"),
        "beads_created_at": issue.get("created_at"),
        "beads_updated_at": issue.get("updated_at"),
        "beads_closed_at": issue.get("closed_at"),
        "beads_close_reason": issue.get("close_reason"),
        "beads_notes": issue.get("notes"),
        "beads_acceptance_criteria": issue.get("acceptance_criteria"),
        "beads_design": issue.get("design"),
    }
    # Strip None values to keep metadata tidy
    metadata = {k: v for k, v in metadata.items() if v not in (None, "")}

    if dry_run:
        return None

    task = cp.create_task(
        title,
        description=description,
        project=project,
        priority=priority,
        metadata=metadata,
        actor=actor,
    )
    target_state = _STATUS_TO_STATE.get(status)
    if target_state and target_state != TaskState.OPEN.value:
        _force_task_state(cp, task.id, target_state, issue, actor)
    return task.id


def _coerce_priority(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _force_task_state(
    cp: Any,
    task_id: str,
    target_state: str,
    issue: Dict[str, Any],
    actor: str,
) -> None:
    """Bypass the lifecycle state machine and write the historical end
    state directly. Used only by the migrator — real workflow runs
    must go through ``cp.transition_task``.
    """
    now = _now_iso()
    completed_at = None
    if target_state == TaskState.COMPLETED.value:
        completed_at = issue.get("closed_at") or now
    cp.store.execute(
        """
        UPDATE tasks
        SET state = ?,
            completed_at = COALESCE(?, completed_at),
            updated_at = ?
        WHERE id = ?
        """,
        (target_state, completed_at, now, task_id),
    )
    cp.store.execute(
        """
        INSERT INTO task_history (id, task_id, event_type, actor, from_state, to_state, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "hist_%s" % os.urandom(8).hex(),
            task_id,
            "task.imported_from_beads",
            actor,
            TaskState.OPEN.value,
            target_state,
            json.dumps({"beads_id": issue.get("id"), "beads_status": issue.get("status")}),
            now,
        ),
    )


def _import_memory(cp: Any, project: str, key: str, value: str, *, actor: str) -> None:
    # memory_service.add_memory takes positional args and has no metadata
    # kwarg, so the bd memory key is encoded into record_type and the
    # body is prefixed with `[beads-key:<k>]` so it round-trips.
    cp.memory.add_memory(
        None,
        "project",
        project,
        "beads_memory:%s" % key,
        "[beads-key:%s]\n\n%s" % (key, value),
        None,
        actor,
    )


def _ticket_path(tickets_dir: Path, issue_id: str) -> Path:
    return tickets_dir / ("%s.md" % issue_id)


def _write_ticket(tickets_dir: Path, issue: Dict[str, Any], mac_task_id: str) -> None:
    path = _ticket_path(tickets_dir, str(issue.get("id")))
    path.write_text(_render_ticket(issue, mac_task_id), encoding="utf-8")


def _render_ticket(issue: Dict[str, Any], mac_task_id: str) -> str:
    """Render a wedow-compatible ticket file with a mac_task_id link
    appended to the frontmatter for cross-reference."""
    deps, links, parent = _classify_dependencies(issue.get("dependencies") or [])
    frontmatter = [
        "---",
        "id: %s" % issue.get("id"),
        "status: %s" % (issue.get("status") or "open"),
        "deps: %s" % _yaml_list(deps),
        "links: %s" % _yaml_list(links),
        "created: %s" % (issue.get("created_at") or ""),
        "type: %s" % (issue.get("issue_type") or "task"),
        "priority: %s" % (issue.get("priority") if issue.get("priority") is not None else 2),
    ]
    if issue.get("assignee"):
        frontmatter.append("assignee: %s" % issue["assignee"])
    if issue.get("external_ref"):
        frontmatter.append("external-ref: %s" % issue["external_ref"])
    if parent:
        frontmatter.append("parent: %s" % parent)
    frontmatter.append("mac-task-id: %s" % mac_task_id)
    frontmatter.append("---")
    body: List[str] = []
    body.append("# %s" % (issue.get("title") or "Untitled"))
    body.append("")
    if issue.get("description"):
        body.append(str(issue["description"]).rstrip())
        body.append("")
    if issue.get("design"):
        body.append("## Design")
        body.append("")
        body.append(str(issue["design"]).rstrip())
        body.append("")
    if issue.get("acceptance_criteria"):
        body.append("## Acceptance Criteria")
        body.append("")
        body.append(str(issue["acceptance_criteria"]).rstrip())
        body.append("")
    if issue.get("notes"):
        body.append("## Notes")
        body.append("")
        body.append(str(issue["notes"]).rstrip())
        body.append("")
    if issue.get("close_reason"):
        body.append("## Close Reason")
        body.append("")
        body.append(str(issue["close_reason"]).rstrip())
        body.append("")
    return "\n".join(frontmatter) + "\n" + "\n".join(body) + ("" if body and body[-1] == "" else "\n")


def _classify_dependencies(raw: Iterable[Dict[str, Any]]) -> Tuple[List[str], List[str], Optional[str]]:
    deps: List[str] = []
    links: List[str] = []
    parent: Optional[str] = None
    for dep in raw:
        if not isinstance(dep, dict):
            continue
        kind = str(dep.get("type") or "").lower()
        target = str(dep.get("depends_on_id") or dep.get("target") or "").strip()
        if not target:
            continue
        if kind == "blocks":
            deps.append(target)
        elif kind == "related":
            links.append(target)
        elif kind == "parent-child" and parent is None:
            parent = target
    return deps, links, parent


def _yaml_list(items: Iterable[str]) -> str:
    items = list(items)
    if not items:
        return "[]"
    return "[" + ", ".join(items) + "]"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
