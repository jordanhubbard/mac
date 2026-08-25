"""Autonomous per-repo backlog grooming.

The fleet self-drives execution but does not manufacture its own backlog: when
the human-authored (and GitHub-issue) queue drains, a registered repo goes idle
even if there is obvious work to do. This groomer closes that gap for opted-in
repos.

Design (deliberately small — it reuses existing machinery):

* On a slow schedule, for each opted-in project that is *going idle* (its
  count of pending/in-flight tasks is below a low-water mark) and is past its
  grooming cadence, create ONE **grooming task**.
* The grooming task mirrors the onboarding "investigation" task: it is
  repo-coupled (``origin.repository_url`` → MAC clones the repo) but declares
  ``evidence_type: "investigation"`` so it is not held to code-substance
  verification. Its prompt asks the agent to analyze the repo and emit a
  prioritized backlog as ``plan_steps`` in ``mac-evidence.json``.
* The executor's existing ``maybe_auto_decompose`` hook promotes those
  ``plan_steps`` into child tasks (inheriting the parent's project, so they are
  repo-coupled), which the normal hub tick then dispatches. No new promotion
  code is needed, and the server-side decompose guardrails (depth / child-count
  caps) still apply.

Opt-in is per project via ``ProjectRecord.metadata["backlog_grooming"]``, so
enabling the groomer fleet-wide is safe: it is a no-op until a project opts in.
Runs on its own daemon thread (mirroring ``RepositoryRefReconciler`` and
``GitHubIssueIngestor``).
"""

from __future__ import annotations

import copy
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from mac.config_coercion import bounded_env_number, parse_timestamp as _parse_ts

GROOMER_SCHEMA = "mac.backlog_groomer.v1"

MIN_INTERVAL_SECONDS = 60.0
MAX_INTERVAL_SECONDS = 24 * 60 * 60.0
MAX_INITIAL_DELAY_SECONDS = 60 * 60.0
DEFAULT_INTERVAL_SECONDS = 900.0  # how often the groomer wakes
DEFAULT_INITIAL_DELAY_SECONDS = 120.0
DEFAULT_MIN_READY = 2  # groom only when a project has < this pending work
DEFAULT_REGROOM_INTERVAL_SECONDS = 6 * 60 * 60.0  # don't re-groom a project faster than this
DEFAULT_BACKLOG_SIZE = 5

_log = logging.getLogger("mac.backlog_groomer")

# Task states that count as "pending or in-flight work" for idle detection.
_ACTIVE_STATES = frozenset(
    {"open", "waiting", "blocked", "claimed", "running", "needs_review", "reviewing"}
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class BacklogGroomerConfig:
    enabled: bool = False
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS
    min_ready: int = DEFAULT_MIN_READY
    regroom_interval_seconds: float = DEFAULT_REGROOM_INTERVAL_SECONDS
    backlog_size: int = DEFAULT_BACKLOG_SIZE
    configuration_error: str = ""

    @property
    def active(self) -> bool:
        return self.enabled and not self.configuration_error

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "active": self.active}

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "BacklogGroomerConfig":
        env = os.environ if environ is None else environ
        errors: List[str] = []
        enabled = str(env.get("MAC_BACKLOG_GROOM_ENABLED") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        def _num(name: str, default: float, low: float, high: float) -> float:
            return bounded_env_number(env, name, default, low, high, errors=errors)

        interval = _num(
            "MAC_BACKLOG_GROOM_INTERVAL_SECONDS",
            DEFAULT_INTERVAL_SECONDS,
            MIN_INTERVAL_SECONDS,
            MAX_INTERVAL_SECONDS,
        )
        initial_delay = _num(
            "MAC_BACKLOG_GROOM_INITIAL_DELAY_SECONDS",
            DEFAULT_INITIAL_DELAY_SECONDS,
            0.0,
            MAX_INITIAL_DELAY_SECONDS,
        )
        min_ready = int(_num("MAC_BACKLOG_GROOM_MIN_READY", DEFAULT_MIN_READY, 0, 1000))
        regroom = _num(
            "MAC_BACKLOG_GROOM_REGROOM_INTERVAL_SECONDS",
            DEFAULT_REGROOM_INTERVAL_SECONDS,
            MIN_INTERVAL_SECONDS,
            30 * 24 * 60 * 60.0,
        )
        size = int(_num("MAC_BACKLOG_GROOM_BACKLOG_SIZE", DEFAULT_BACKLOG_SIZE, 1, 50))
        return cls(
            enabled=enabled,
            interval_seconds=interval,
            initial_delay_seconds=initial_delay,
            min_ready=min_ready,
            regroom_interval_seconds=regroom,
            backlog_size=size,
            configuration_error="; ".join(errors),
        )


@dataclass(frozen=True)
class ProjectGroomingPolicy:
    enabled: bool = False
    backlog_size: Optional[int] = None
    min_ready: Optional[int] = None
    default_capabilities: tuple = ()

    @classmethod
    def from_metadata(cls, metadata: Any) -> "ProjectGroomingPolicy":
        if not isinstance(metadata, Mapping):
            return cls()
        raw = metadata.get("backlog_grooming")
        if not isinstance(raw, Mapping):
            return cls()
        caps_raw = raw.get("default_capabilities") or []
        caps = (
            tuple(str(c).strip() for c in caps_raw if isinstance(c, str) and str(c).strip())
            if isinstance(caps_raw, (list, tuple))
            else ()
        )

        def _opt_int(key: str) -> Optional[int]:
            if raw.get(key) is None:
                return None
            try:
                return int(raw.get(key))
            except (TypeError, ValueError):
                return None

        return cls(
            enabled=bool(raw.get("enabled")),
            backlog_size=_opt_int("backlog_size"),
            min_ready=_opt_int("min_ready"),
            default_capabilities=caps,
        )


def build_grooming_description(project: str, repo_url: str, backlog_size: int) -> str:
    """Build the task description for an autonomous backlog-grooming task."""
    return "\n".join(
        [
            "Autonomous backlog grooming for project %s (%s)." % (project, repo_url),
            "",
            "MAC has cloned a clean, writable checkout for you at $MAC_TASK_REPO_WORKTREE.",
            "This is READ-ONLY with respect to the remote: do NOT push or open a pull "
            "request. This task produces a PLAN, not code.",
            "",
            "Analyze the repository (start from README.md, AGENTS.md, PLAN.md, open "
            "issues/TODOs, failing or missing tests, and obvious gaps). Reconcile "
            "against PLAN.md if present: prefer already-planned work, and only surface "
            "genuinely new items.",
            "",
            "Produce a prioritized backlog of %d concrete, independently-actionable "
            "next steps and emit them as `plan_steps` in mac-evidence.json so MAC can "
            "turn them into real tasks. Each step MUST be a JSON object with:" % backlog_size,
            '  - "title": a short imperative task title (required)',
            '  - "description": what to do and how to know it is done (acceptance criteria)',
            '  - "required_capabilities": optional list (e.g. ["python"], ["docs"])',
            "",
            "Evidence shape (evidence_type=investigation):",
            '  {"plan_steps": [ {"title": "...", "description": "..."}, ... ],',
            '   "rationale": "one line on how you prioritized"}',
            "",
            "Keep each step small enough for one focused task. Do NOT include "
            "already-open work. Quality over quantity: fewer, well-scoped steps beat "
            "a padded list.",
        ]
    )


class BacklogGroomer:
    """Periodically seed grooming tasks for opted-in repos that are going idle."""

    def __init__(self, control_plane: Any, config: BacklogGroomerConfig) -> None:
        self.control_plane = control_plane
        self.config = config
        self._stop_event = threading.Event()
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last_report: Optional[Dict[str, Any]] = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        if not self.config.active:
            if self.config.configuration_error:
                self._observe(
                    "backlog.groom.configuration_invalid",
                    "warning",
                    {"error": self.config.configuration_error},
                )
            return False
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            thread = threading.Thread(target=self._loop, name="mac-backlog-groomer", daemon=True)
            self._thread = thread
            thread.start()
        self._observe("backlog.groom.started", "info", {"config": self.config.to_dict()})
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._observe("backlog.groom.stopped", "info", {})
        return stopped

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            thread = self._thread
            last_report = copy.deepcopy(self._last_report)
        return {
            "schema": GROOMER_SCHEMA,
            "config": self.config.to_dict(),
            "thread_alive": bool(thread is not None and thread.is_alive()),
            "run_active": self._run_lock.locked(),
            "last_report": last_report,
        }

    def _loop(self) -> None:
        if self._stop_event.wait(max(0.0, self.config.initial_delay_seconds)):
            return
        while not self._stop_event.is_set():
            try:
                self.run_once(trigger="scheduled")
            except Exception:  # noqa: BLE001 - a future tick must still run.
                _log.warning("backlog-groom tick failed", exc_info=True)
            if self._stop_event.wait(max(0.01, self.config.interval_seconds)):
                return

    # -- core ---------------------------------------------------------------

    def run_once(
        self, *, actor: str = "backlog-groomer", trigger: str = "operator"
    ) -> Dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {"schema": GROOMER_SCHEMA, "status": "busy", "trigger": trigger, "projects": []}
        results: List[Dict[str, Any]] = []
        run_id = "groom_%s" % uuid.uuid4().hex
        try:
            active_counts, latest_groom = self._project_work_snapshot()
            for record in self._candidate_projects():
                results.append(
                    self._groom_project(
                        record,
                        actor=actor,
                        active_counts=active_counts,
                        latest_groom=latest_groom,
                    )
                )
        finally:
            self._run_lock.release()
        report = {
            "schema": GROOMER_SCHEMA,
            "run_id": run_id,
            "status": "ok",
            "trigger": trigger,
            "groomed_count": sum(1 for r in results if r.get("groomed")),
            "projects": results,
        }
        with self._state_lock:
            self._last_report = report
        level = "warning" if any(r.get("error") for r in results) else "info"
        self._observe("backlog.groom.run", level, report)
        return report

    def _candidate_projects(self) -> List[Any]:
        try:
            return list(self.control_plane.list_project_records())
        except Exception as exc:  # noqa: BLE001
            _log.warning("backlog-groom could not list projects: %s", exc)
            return []

    def _project_work_snapshot(self):
        """Return (active-task count per project, newest grooming-task time per project)."""
        active_counts: Dict[str, int] = {}
        latest_groom: Dict[str, datetime] = {}
        try:
            tasks = list(self.control_plane.list_tasks())
        except Exception as exc:  # noqa: BLE001
            _log.warning("backlog-groom could not list tasks: %s", exc)
            return active_counts, latest_groom
        for task in tasks:
            project = str(getattr(task, "project", "") or "")
            state = str(getattr(task, "state", "") or "")
            metadata = getattr(task, "metadata", None) or {}
            origin = metadata.get("origin") if isinstance(metadata, Mapping) else None
            is_groom = isinstance(origin, Mapping) and origin.get("type") == "backlog_grooming"
            if state in _ACTIVE_STATES:
                # A pending/in-flight grooming task counts as "grooming already
                # underway" (tracked via latest_groom + the open-groom guard),
                # not as project work to satisfy the idle threshold.
                if not is_groom:
                    active_counts[project] = active_counts.get(project, 0) + 1
            if is_groom:
                ts = _parse_ts(getattr(task, "created_at", None))
                if ts is not None and (project not in latest_groom or ts > latest_groom[project]):
                    latest_groom[project] = ts
                # Record that an open grooming task exists so we don't stack them.
                if state in _ACTIVE_STATES:
                    latest_groom.setdefault("__open__:" + project, _utcnow())
        return active_counts, latest_groom

    def _groom_project(
        self,
        record: Any,
        *,
        actor: str,
        active_counts: Dict[str, int],
        latest_groom: Dict[str, datetime],
    ) -> Dict[str, Any]:
        project = str(getattr(record, "name", "") or "")
        metadata = getattr(record, "metadata", None) or {}
        policy = ProjectGroomingPolicy.from_metadata(metadata)
        result: Dict[str, Any] = {"project": project, "enabled": policy.enabled, "groomed": False}
        if not policy.enabled:
            return result
        repo_url = metadata.get("repository_url") if isinstance(metadata, Mapping) else None
        if not (isinstance(repo_url, str) and repo_url.strip()):
            result["skipped_reason"] = "no repository_url"
            return result
        repo_url = repo_url.strip()

        min_ready = policy.min_ready if policy.min_ready is not None else self.config.min_ready
        active = int(active_counts.get(project, 0))
        result["active_tasks"] = active
        if active >= min_ready:
            result["skipped_reason"] = "not idle (%d >= %d pending)" % (active, min_ready)
            return result

        # Don't stack grooming tasks: skip if one is already open for the project.
        if "__open__:" + project in latest_groom:
            result["skipped_reason"] = "grooming task already open"
            return result

        # Cadence: don't re-groom faster than the regroom interval.
        last = latest_groom.get(project)
        if last is not None:
            age = (_utcnow() - last).total_seconds()
            if age < self.config.regroom_interval_seconds:
                result["skipped_reason"] = "groomed %.0fs ago (< %.0fs)" % (
                    age,
                    self.config.regroom_interval_seconds,
                )
                return result

        size = policy.backlog_size if policy.backlog_size is not None else self.config.backlog_size
        try:
            task = self._create_grooming_task(project, repo_url, size, policy, actor)
        except Exception as exc:  # noqa: BLE001 - isolate one project's failure.
            result["error"] = str(exc)[:500]
            return result
        result["groomed"] = True
        result["task_id"] = getattr(task, "id", None)
        return result

    def _create_grooming_task(
        self, project: str, repo_url: str, size: int, policy: ProjectGroomingPolicy, actor: str
    ) -> Any:
        origin = {
            "type": "backlog_grooming",
            "repository_url": repo_url,
            "repository_name": project,
        }
        metadata = {
            "origin": origin,
            # Repo-coupled (repo cloned) but held to an investigation write-up,
            # not code-substance verification — same contract as onboarding.
            "evidence_type": "investigation",
        }
        return self.control_plane.create_task(
            "Groom backlog for %s" % project,
            description=build_grooming_description(project, repo_url, size),
            project=project,
            required_capabilities=list(policy.default_capabilities),
            metadata=metadata,
            actor=actor,
        )

    # -- telemetry ----------------------------------------------------------

    def _observe(self, event_type: str, level: str, detail: Dict[str, Any]) -> None:
        try:
            self.control_plane.record_log(
                event_type,
                layer="control_plane",
                source="backlog-groomer",
                level=level,
                subject_type="service",
                subject_id="backlog-groomer",
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - telemetry must not stop grooming.
            _log.warning("could not record backlog-groom telemetry", exc_info=True)


__all__ = [
    "GROOMER_SCHEMA",
    "BacklogGroomerConfig",
    "BacklogGroomer",
    "ProjectGroomingPolicy",
    "build_grooming_description",
]
