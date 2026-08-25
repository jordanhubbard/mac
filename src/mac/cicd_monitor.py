"""Autonomous GitHub Actions monitoring and post-publication follow-up.

The task lifecycle proves that reviewed work reached the canonical branch, but
that proof lands before repository CI has necessarily finished.  This module
adds the delayed half of that lifecycle without adding another task state:

* registered GitHub projects are checked periodically;
* a canonical publication can schedule an exact-SHA check for later;
* pending runs are retried instead of being mistaken for success;
* CI failures coalesce into bounded, low-priority repository maintenance; and
* schedules and results are durable observability records, so a process restart
  does not lose the clock or duplicate work.

The controller is deliberately composed like ``GitHubIssueIngestor`` and
``BacklogGroomer``.  Network access, tokens, time, and cross-replica
reconciliation are injectable, keeping tests hermetic and letting the API
lifespan own start/stop wiring.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import threading
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from mac import gitops
from mac.config_coercion import bounded_env_number
from mac.github_ingest import GITHUB_API_ROOT, parse_github_owner_repo
from mac.reconciliation import ReconciliationCoordinator

CICD_MONITOR_SCHEMA = "mac.cicd_monitor.v1"
CICD_SCHEDULE_SCHEMA = "mac.cicd_followup_schedule.v1"
CICD_CHECK_SCHEMA = "mac.cicd_check.v1"

SCHEDULE_EVENT = "cicd.followup.scheduled"
FOLLOWUP_PENDING_EVENT = "cicd.followup.pending"
FOLLOWUP_COMPLETED_EVENT = "cicd.followup.completed"
CHECK_PENDING_EVENT = "cicd.check.pending"
CHECK_COMPLETED_EVENT = "cicd.check.completed"
CLEANUP_TASK_EVENT = "cicd.cleanup.task"

MIN_WAKE_INTERVAL_SECONDS = 60.0
MAX_WAKE_INTERVAL_SECONDS = 60 * 60.0
MAX_INITIAL_DELAY_SECONDS = 60 * 60.0
DEFAULT_WAKE_INTERVAL_SECONDS = 15 * 60.0
DEFAULT_INITIAL_DELAY_SECONDS = 120.0
DEFAULT_POST_PUBLICATION_DELAY_HOURS = 2.0
DEFAULT_PENDING_RETRY_SECONDS = 30 * 60.0
DEFAULT_ABSENT_RECHECK_SECONDS = 24 * 60 * 60.0
DEFAULT_API_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_CHECKS_PER_RUN = 100
DEFAULT_MAX_OBSERVATIONS = 1000

FAST_CADENCE_SECONDS = 4 * 60 * 60.0
SLOW_CADENCE_SECONDS = 8 * 60 * 60.0
LATENCY_SPLIT_SECONDS = 2 * 60 * 60.0
DEFAULT_CADENCE_SECONDS = 6 * 60 * 60.0

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_TERMINAL_SUCCESS = frozenset({"success", "neutral", "skipped"})
_TERMINAL_FAILURE = frozenset(
    {
        "action_required",
        "cancelled",
        "failure",
        "stale",
        "startup_failure",
        "timed_out",
    }
)
_PENDING_STATES = frozenset({"queued", "in_progress", "pending", "requested", "waiting"})

_log = logging.getLogger("mac.cicd_monitor")

ActionsFetcher = Callable[..., Mapping[str, Any]]
TokenProvider = Callable[[], str]
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_error(value: Any) -> str:
    return str(value).strip()[:500]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(float(value), high))


def cadence_seconds_for_latency(average_latency_seconds: Optional[float]) -> float:
    """Return the required 4h/8h cadence from observed CI latency."""

    if average_latency_seconds is None:
        return DEFAULT_CADENCE_SECONDS
    return (
        FAST_CADENCE_SECONDS
        if float(average_latency_seconds) <= LATENCY_SPLIT_SECONDS
        else SLOW_CADENCE_SECONDS
    )


def post_publication_delay_hours(
    average_latency_seconds: Optional[float],
    *,
    configured_hours: Optional[float] = None,
    default_hours: float = DEFAULT_POST_PUBLICATION_DELAY_HOURS,
) -> float:
    """Choose a small exact-SHA delay and clamp it to the 1h..8h contract."""

    if configured_hours is not None:
        value = float(configured_hours)
    elif average_latency_seconds is not None:
        value = float(average_latency_seconds) / 3600.0
    else:
        value = float(default_hours)
    return _clamp(value, 1.0, 8.0)


def _http_json(url: str, *, token: str, timeout: float) -> Mapping[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "mac-cicd-monitor",
    }
    if token:
        headers["Authorization"] = "Bearer %s" % token
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("GitHub Actions API returned a non-object response")
    return payload


def _http_actions_status(
    owner: str,
    repo: str,
    *,
    token: str,
    branch: str,
    head_sha: str = "",
    limit: int = 50,
    timeout: float = DEFAULT_API_TIMEOUT_SECONDS,
) -> Mapping[str, Any]:
    """Fetch workflow inventory and recent Actions runs for one target.

    ``head_sha`` selects the exact canonical commit for publication follow-up.
    Periodic checks omit it and select the repository's default branch.
    """

    quoted_owner = urllib.parse.quote(owner, safe="")
    quoted_repo = urllib.parse.quote(repo, safe="")
    workflows_url = "%s/repos/%s/%s/actions/workflows?%s" % (
        GITHUB_API_ROOT,
        quoted_owner,
        quoted_repo,
        urllib.parse.urlencode({"per_page": "100", "page": "1"}),
    )
    query: Dict[str, str] = {
        "per_page": str(max(1, min(int(limit), 100))),
        "page": "1",
    }
    if head_sha:
        query["head_sha"] = head_sha
    elif branch:
        query["branch"] = branch
    runs_url = "%s/repos/%s/%s/actions/runs?%s" % (
        GITHUB_API_ROOT,
        quoted_owner,
        quoted_repo,
        urllib.parse.urlencode(query),
    )
    return {
        "workflows": _http_json(workflows_url, token=token, timeout=timeout),
        "runs": _http_json(runs_url, token=token, timeout=timeout),
    }


@dataclass(frozen=True)
class CICDMonitorConfig:
    """Fleet-wide monitor configuration.

    The monitor is default-on; per-project policy still filters candidates to
    registered GitHub repositories and honors an explicit project opt-out.
    """

    enabled: bool = True
    interval_seconds: float = DEFAULT_WAKE_INTERVAL_SECONDS
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS
    default_post_publication_delay_hours: float = DEFAULT_POST_PUBLICATION_DELAY_HOURS
    pending_retry_seconds: float = DEFAULT_PENDING_RETRY_SECONDS
    absent_recheck_seconds: float = DEFAULT_ABSENT_RECHECK_SECONDS
    api_timeout_seconds: float = DEFAULT_API_TIMEOUT_SECONDS
    max_checks_per_run: int = DEFAULT_MAX_CHECKS_PER_RUN
    max_observations: int = DEFAULT_MAX_OBSERVATIONS
    configuration_error: str = ""

    @property
    def active(self) -> bool:
        return self.enabled and not self.configuration_error

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "active": self.active}

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "CICDMonitorConfig":
        env = os.environ if environ is None else environ
        errors: List[str] = []
        raw_enabled = str(env.get("MAC_CICD_MONITOR_ENABLED", "1")).strip().lower()
        enabled = raw_enabled not in _FALSE_VALUES
        if raw_enabled and raw_enabled not in _FALSE_VALUES.union(_TRUE_VALUES):
            errors.append("MAC_CICD_MONITOR_ENABLED must be a boolean value")

        def number(name: str, default: float, low: float, high: float) -> float:
            return bounded_env_number(env, name, default, low, high, errors=errors)

        interval = number(
            "MAC_CICD_MONITOR_INTERVAL_SECONDS",
            DEFAULT_WAKE_INTERVAL_SECONDS,
            MIN_WAKE_INTERVAL_SECONDS,
            MAX_WAKE_INTERVAL_SECONDS,
        )
        initial_delay = number(
            "MAC_CICD_MONITOR_INITIAL_DELAY_SECONDS",
            DEFAULT_INITIAL_DELAY_SECONDS,
            0.0,
            MAX_INITIAL_DELAY_SECONDS,
        )
        pending_retry = number(
            "MAC_CICD_MONITOR_PENDING_RETRY_SECONDS",
            DEFAULT_PENDING_RETRY_SECONDS,
            MIN_WAKE_INTERVAL_SECONDS,
            SLOW_CADENCE_SECONDS,
        )
        absent_recheck = number(
            "MAC_CICD_MONITOR_ABSENT_RECHECK_SECONDS",
            DEFAULT_ABSENT_RECHECK_SECONDS,
            SLOW_CADENCE_SECONDS,
            7 * 24 * 60 * 60.0,
        )
        timeout = number(
            "MAC_CICD_MONITOR_API_TIMEOUT_SECONDS",
            DEFAULT_API_TIMEOUT_SECONDS,
            1.0,
            120.0,
        )
        max_checks = int(
            number(
                "MAC_CICD_MONITOR_MAX_CHECKS_PER_RUN",
                DEFAULT_MAX_CHECKS_PER_RUN,
                1,
                1000,
            )
        )
        max_observations = int(
            number(
                "MAC_CICD_MONITOR_MAX_OBSERVATIONS",
                DEFAULT_MAX_OBSERVATIONS,
                10,
                1000,
            )
        )
        raw_delay = env.get("MAC_CICD_MONITOR_POST_PUBLICATION_DELAY_HOURS")
        try:
            delay = (
                DEFAULT_POST_PUBLICATION_DELAY_HOURS
                if raw_delay in {None, ""}
                else _clamp(float(str(raw_delay).strip()), 1.0, 8.0)
            )
        except (TypeError, ValueError):
            errors.append("MAC_CICD_MONITOR_POST_PUBLICATION_DELAY_HOURS must be numeric")
            delay = DEFAULT_POST_PUBLICATION_DELAY_HOURS
        return cls(
            enabled=enabled,
            interval_seconds=interval,
            initial_delay_seconds=initial_delay,
            default_post_publication_delay_hours=delay,
            pending_retry_seconds=pending_retry,
            absent_recheck_seconds=absent_recheck,
            api_timeout_seconds=timeout,
            max_checks_per_run=max_checks,
            max_observations=max_observations,
            configuration_error="; ".join(errors),
        )


@dataclass(frozen=True)
class ProjectCICDPolicy:
    enabled: bool
    default_branch: str = "main"
    post_publication_delay_hours: Optional[float] = None
    required_capabilities: Tuple[str, ...] = ()
    # CI drift is maintenance input, not an emergency. Priority aging can
    # still pull it forward naturally if the cleanup remains open.
    priority: int = -10

    @classmethod
    def from_metadata(cls, metadata: Any, repository_url: str) -> "ProjectCICDPolicy":
        parsed = parse_github_owner_repo(repository_url)
        default_enabled = parsed is not None
        data = metadata if isinstance(metadata, Mapping) else {}
        raw = data.get("cicd_monitor")

        enabled = default_enabled
        if isinstance(raw, bool):
            enabled = raw and default_enabled
            raw_map: Mapping[str, Any] = {}
        elif isinstance(raw, Mapping):
            raw_map = raw
            if "enabled" in raw:
                configured = raw.get("enabled")
                if isinstance(configured, str):
                    configured_enabled = configured.strip().lower() not in _FALSE_VALUES
                else:
                    configured_enabled = bool(configured)
                enabled = configured_enabled and default_enabled
        else:
            raw_map = {}

        branch = (
            str(
                raw_map.get("default_branch")
                or data.get("default_branch")
                or data.get("canonical_branch")
                or "main"
            ).strip()
            or "main"
        )
        delay: Optional[float] = None
        if raw_map.get("post_publication_delay_hours") not in {None, ""}:
            try:
                delay = _clamp(float(raw_map.get("post_publication_delay_hours")), 1.0, 8.0)
            except (TypeError, ValueError):
                delay = None
        caps_raw = raw_map.get("required_capabilities") or []
        caps = (
            tuple(
                str(item).strip()
                for item in caps_raw
                if isinstance(item, str) and str(item).strip()
            )
            if isinstance(caps_raw, (list, tuple))
            else ()
        )
        try:
            priority = int(raw_map.get("priority") if raw_map.get("priority") is not None else -10)
        except (TypeError, ValueError):
            priority = -10
        return cls(
            enabled=enabled,
            default_branch=branch,
            post_publication_delay_hours=delay,
            required_capabilities=caps,
            priority=priority,
        )


@dataclass(frozen=True)
class _Target:
    project: str
    repository_url: str
    owner: str
    repo: str
    branch: str
    policy: ProjectCICDPolicy
    head_sha: str = ""
    task_id: str = ""
    publication_id: str = ""
    schedule_key: str = ""
    trigger: str = "periodic"

    @property
    def repository(self) -> str:
        return "%s/%s" % (self.owner, self.repo)


class _LocalReconciliation:
    """Fallback for fakes/embedded callers without a durable store."""

    def claim(self, name: str) -> Mapping[str, Any]:
        return {"name": name, "cursor": None}

    def complete(self, claim: Any, *, cursor: Optional[str]) -> bool:
        return True

    def abandon(self, claim: Any) -> bool:
        return True


class CICDMonitor:
    """Check repository Actions state and create durable failure work."""

    def __init__(
        self,
        control_plane: Any,
        config: CICDMonitorConfig,
        *,
        actions_fetcher: Optional[ActionsFetcher] = None,
        token_provider: Optional[TokenProvider] = None,
        now: Optional[Clock] = None,
        reconciliation: Optional[Any] = None,
    ) -> None:
        self.control_plane = control_plane
        self.config = config
        self._actions_fetcher = actions_fetcher or _http_actions_status
        self._token_provider = token_provider or (lambda: gitops.token_for_host("github"))
        self._now = now or _utcnow
        if reconciliation is not None:
            self.reconciliation = reconciliation
        elif getattr(control_plane, "store", None) is not None:
            self.reconciliation = ReconciliationCoordinator(control_plane.store)
        else:
            self.reconciliation = _LocalReconciliation()
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
                    "cicd.monitor.configuration_invalid",
                    "warning",
                    {"error": self.config.configuration_error},
                )
            return False
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            thread = threading.Thread(
                target=self._loop,
                name="mac-cicd-monitor",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        self._observe("cicd.monitor.started", "info", {"config": self.config.to_dict()})
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._observe("cicd.monitor.stopped", "info", {})
        return stopped

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            thread = self._thread
            last_report = copy.deepcopy(self._last_report)
        return {
            "schema": CICD_MONITOR_SCHEMA,
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
            except Exception:  # noqa: BLE001 - future ticks must survive.
                _log.warning("cicd-monitor tick failed", exc_info=True)
            if self._stop_event.wait(max(0.01, self.config.interval_seconds)):
                return

    # -- publication scheduling --------------------------------------------

    def schedule_publication_followup(
        self,
        *,
        task_id: str,
        publication_id: str,
        project: str,
        canonical_sha: str,
        repository_url: str = "",
        published_at: Optional[str] = None,
        actor: str = "cicd-monitor",
    ) -> Dict[str, Any]:
        """Durably schedule one exact-SHA post-publication check.

        The method is idempotent by ``publication_id + canonical_sha``.  The API
        wiring should call it only after publication has committed and canonical
        integration proof is durable.
        """

        sha = str(canonical_sha or "").strip().lower()
        if not _SHA_RE.fullmatch(sha):
            raise ValueError("canonical_sha must be a 40-character Git SHA")
        record = self._project_record(project)
        metadata = getattr(record, "metadata", None) or {}
        repo_url = str(
            repository_url
            or (metadata.get("repository_url") if isinstance(metadata, Mapping) else "")
            or ""
        ).strip()
        policy = ProjectCICDPolicy.from_metadata(metadata, repo_url)
        parsed = parse_github_owner_repo(repo_url)
        if not policy.enabled or parsed is None:
            return {
                "schema": CICD_SCHEDULE_SCHEMA,
                "status": "disabled",
                "task_id": task_id,
                "publication_id": publication_id,
            }
        owner, repo = parsed
        schedule_key = self._schedule_key(publication_id, task_id, sha)
        existing = self._matching_observation(
            SCHEDULE_EVENT,
            subject_type="task",
            subject_id=task_id,
            key="schedule_key",
            value=schedule_key,
        )
        if existing is not None:
            detail = self._event_detail(existing)
            return {**detail, "status": "already_scheduled"}

        average = self._latest_average_latency(project)
        delay_hours = post_publication_delay_hours(
            average,
            configured_hours=policy.post_publication_delay_hours,
            default_hours=self.config.default_post_publication_delay_hours,
        )
        published = _parse_time(published_at) or self._now()
        due = published + timedelta(hours=delay_hours)
        detail = {
            "schema": CICD_SCHEDULE_SCHEMA,
            "status": "scheduled",
            "schedule_key": schedule_key,
            "task_id": str(task_id),
            "publication_id": str(publication_id),
            "project": str(project),
            "repository_url": repo_url,
            "repository": "%s/%s" % (owner, repo),
            "default_branch": policy.default_branch,
            "canonical_sha": sha,
            "published_at": _iso(published),
            "scheduled_at": _iso(self._now()),
            "due_at": _iso(due),
            "delay_hours": delay_hours,
            "average_latency_seconds": average,
        }
        self._record(
            SCHEDULE_EVENT,
            "info",
            detail,
            subject_type="task",
            subject_id=str(task_id),
            source=actor,
        )
        return detail

    # -- checking -----------------------------------------------------------

    def run_once(
        self,
        *,
        actor: str = "cicd-monitor",
        trigger: str = "operator",
    ) -> Dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return self._busy_report(trigger)
        claim = None
        started_at = _iso(self._now())
        try:
            claim = self.reconciliation.claim("cicd-monitor")
            if claim is None:
                return self._busy_report(trigger, reason="reconciler_leased")
            token = ""
            try:
                token = str(self._token_provider() or "").strip()
            except Exception as exc:  # noqa: BLE001 - public repos remain checkable.
                _log.warning("cicd-monitor token lookup failed: %s", _safe_error(exc))

            targets = self._due_followups()
            followup_projects = {target.project for target in targets}
            for target in self._due_periodic_targets():
                if len(targets) >= self.config.max_checks_per_run:
                    break
                if target.project in followup_projects:
                    continue
                targets.append(target)
            targets = targets[: self.config.max_checks_per_run]

            results: List[Dict[str, Any]] = []
            for target in targets:
                results.append(self._check_target(target, token=token, actor=actor))
            self.reconciliation.complete(claim, cursor=getattr(claim, "cursor", None))
            report = {
                "schema": CICD_MONITOR_SCHEMA,
                "run_id": "cicd_%s" % uuid.uuid4().hex,
                "status": "ok",
                "trigger": trigger,
                "started_at": started_at,
                "finished_at": _iso(self._now()),
                "checked_count": len(results),
                "failure_count": sum(1 for result in results if result.get("status") == "failure"),
                "pending_count": sum(1 for result in results if result.get("status") == "pending"),
                "created_task_count": sum(
                    1 for result in results if result.get("cleanup_task_created")
                ),
                "repositories": results,
            }
            with self._state_lock:
                self._last_report = report
            self._observe(
                "cicd.monitor.run",
                "warning"
                if report["failure_count"] or any(r.get("error") for r in results)
                else "info",
                report,
            )
            return report
        except Exception:
            if claim is not None:
                try:
                    self.reconciliation.abandon(claim)
                except Exception:  # noqa: BLE001 - preserve the original failure.
                    pass
            raise
        finally:
            self._run_lock.release()

    def _check_target(self, target: _Target, *, token: str, actor: str) -> Dict[str, Any]:
        try:
            payload = self._actions_fetcher(
                target.owner,
                target.repo,
                token=token,
                branch=target.branch,
                head_sha=target.head_sha,
                limit=50,
                timeout=self.config.api_timeout_seconds,
            )
            result = self._classify(payload, target)
        except Exception as exc:  # noqa: BLE001 - isolate one repository.
            result = {
                "schema": CICD_CHECK_SCHEMA,
                "status": "error",
                "project": target.project,
                "repository": target.repository,
                "repository_url": target.repository_url,
                "branch": target.branch,
                "canonical_sha": target.head_sha,
                "trigger": target.trigger,
                "error": _safe_error(exc),
                "checked_at": _iso(self._now()),
            }
            self._record_check(target, result, actor=actor, pending=True)
            return result

        if result["status"] == "pending":
            self._record_check(target, result, actor=actor, pending=True)
            return result

        cleanup_task = None
        cleanup_created = False
        if result["status"] == "failure":
            try:
                cleanup_task, cleanup_created = self._ensure_cleanup_task(
                    target, result, actor=actor
                )
            except Exception as exc:  # noqa: BLE001 - CI is advisory maintenance.
                result["cleanup_task_error"] = _safe_error(exc)
                _log.warning(
                    "cicd-monitor could not file cleanup for %s: %s",
                    target.repository,
                    result["cleanup_task_error"],
                )
            result["cleanup_task_id"] = getattr(cleanup_task, "id", None)
            result["cleanup_task_created"] = cleanup_created
        self._record_check(target, result, actor=actor, pending=False)
        return result

    def _classify(self, payload: Mapping[str, Any], target: _Target) -> Dict[str, Any]:
        workflows_payload = payload.get("workflows")
        runs_payload = payload.get("runs")
        workflows = (
            workflows_payload.get("workflows") or []
            if isinstance(workflows_payload, Mapping)
            else payload.get("workflows") or []
        )
        runs = (
            runs_payload.get("workflow_runs") or []
            if isinstance(runs_payload, Mapping)
            else payload.get("workflow_runs") or []
        )
        workflows = [item for item in workflows if isinstance(item, Mapping)]
        runs = [item for item in runs if isinstance(item, Mapping)]
        if target.head_sha:
            runs = [
                run
                for run in runs
                if str(run.get("head_sha") or "").strip().lower() == target.head_sha.lower()
            ]
        latest = self._latest_runs(runs)
        durations = [
            duration
            for duration in (self._run_duration(run) for run in latest)
            if duration is not None
        ]
        average = sum(durations) / len(durations) if durations else None
        common: Dict[str, Any] = {
            "schema": CICD_CHECK_SCHEMA,
            "project": target.project,
            "repository": target.repository,
            "repository_url": target.repository_url,
            "branch": target.branch,
            "canonical_sha": target.head_sha
            or (str(latest[0].get("head_sha") or "").strip() if latest else ""),
            "trigger": target.trigger,
            "task_id": target.task_id,
            "publication_id": target.publication_id,
            "schedule_key": target.schedule_key,
            "checked_at": _iso(self._now()),
            "workflow_count": len(workflows),
            "run_count": len(latest),
            "average_latency_seconds": average,
            "cadence_seconds": cadence_seconds_for_latency(average),
            "runs": [self._run_summary(run) for run in latest],
        }
        if not workflows and not latest:
            return {**common, "status": "ci_absent"}
        if not latest:
            return {
                **common,
                "status": "pending" if target.head_sha else "ci_idle",
            }

        pending = [
            run
            for run in latest
            if str(run.get("status") or "").strip().lower() in _PENDING_STATES
            or (
                str(run.get("status") or "").strip().lower() != "completed"
                and not str(run.get("conclusion") or "").strip()
            )
        ]
        if pending:
            return {**common, "status": "pending"}
        failed = [
            run
            for run in latest
            if str(run.get("conclusion") or "").strip().lower() in _TERMINAL_FAILURE
            or (str(run.get("conclusion") or "").strip().lower() not in _TERMINAL_SUCCESS)
        ]
        if failed:
            failed_summaries = [self._run_summary(run) for run in failed]
            fingerprint = self._failure_fingerprint(
                target,
                failed_summaries,
                canonical_sha=str(common.get("canonical_sha") or ""),
            )
            return {
                **common,
                "status": "failure",
                "failure_fingerprint": fingerprint,
                "failed_runs": failed_summaries,
            }
        return {**common, "status": "success"}

    @staticmethod
    def _latest_runs(runs: Iterable[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
        by_workflow: Dict[str, Mapping[str, Any]] = {}
        for run in runs:
            key = str(run.get("workflow_id") or run.get("name") or run.get("id") or "unknown")
            current = by_workflow.get(key)
            if current is None or CICDMonitor._run_sort_key(run) > CICDMonitor._run_sort_key(
                current
            ):
                by_workflow[key] = run
        return sorted(
            by_workflow.values(),
            key=CICDMonitor._run_sort_key,
            reverse=True,
        )

    @staticmethod
    def _run_sort_key(run: Mapping[str, Any]) -> Tuple[str, int, int]:
        return (
            str(run.get("created_at") or run.get("run_started_at") or ""),
            int(run.get("run_number") or 0),
            int(run.get("run_attempt") or 0),
        )

    @staticmethod
    def _run_duration(run: Mapping[str, Any]) -> Optional[float]:
        started = _parse_time(run.get("run_started_at") or run.get("created_at"))
        completed = _parse_time(run.get("updated_at"))
        if started is None or completed is None or completed < started:
            return None
        return (completed - started).total_seconds()

    @staticmethod
    def _run_summary(run: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "id": run.get("id"),
            "workflow_id": run.get("workflow_id"),
            "name": str(run.get("name") or ""),
            "status": str(run.get("status") or ""),
            "conclusion": str(run.get("conclusion") or ""),
            "head_sha": str(run.get("head_sha") or ""),
            "head_branch": str(run.get("head_branch") or ""),
            "event": str(run.get("event") or ""),
            "url": str(run.get("html_url") or ""),
            "created_at": str(run.get("created_at") or ""),
            "run_started_at": str(run.get("run_started_at") or ""),
            "updated_at": str(run.get("updated_at") or ""),
        }

    @staticmethod
    def _failure_fingerprint(
        target: _Target,
        failed_runs: Iterable[Mapping[str, Any]],
        *,
        canonical_sha: str,
    ) -> str:
        material = {
            "repository": target.repository.lower(),
            "canonical_sha": canonical_sha.lower(),
            "failures": sorted(
                [
                    {
                        "workflow_id": item.get("workflow_id"),
                        "name": item.get("name"),
                        "conclusion": item.get("conclusion"),
                    }
                    for item in failed_runs
                ],
                key=lambda item: json.dumps(item, sort_keys=True),
            ),
        }
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return "sha256:%s" % digest

    def _ensure_cleanup_task(
        self, target: _Target, result: Mapping[str, Any], *, actor: str
    ) -> Tuple[Any, bool]:
        fingerprint = str(result.get("failure_fingerprint") or "")
        if not fingerprint:
            return None, False
        # Keep CI as bounded maintenance pressure. A repository gets at most
        # one unfinished cleanup task even if new commits/workflows fail while
        # that work is pending; every observation remains durable and the next
        # completed cleanup permits a fresh task if CI later regresses.
        get_task = getattr(self.control_plane, "get_task", None)
        if callable(get_task):
            try:
                for event in self._observations(
                    CLEANUP_TASK_EVENT,
                    subject_type="project",
                    subject_id=target.project,
                ):
                    detail = self._event_detail(event)
                    if str(detail.get("repository") or "").lower() != target.repository.lower():
                        continue
                    task_id = str(detail.get("task_id") or "")
                    if not task_id:
                        continue
                    try:
                        task = get_task(task_id)
                    except Exception:  # noqa: BLE001 - stale link; keep looking.
                        continue
                    if str(getattr(task, "state", "") or "").lower() not in {
                        "completed",
                        "cancelled",
                        "failed",
                    }:
                        return task, False
            except Exception as exc:  # noqa: BLE001 - task creation is still safe.
                _log.warning(
                    "cicd-monitor could not coalesce cleanup task for %s: %s",
                    target.repository,
                    _safe_error(exc),
                )
        short = fingerprint.removeprefix("sha256:")[:24]
        origin = {
            "type": "cicd_cleanup",
            "provider": "github",
            "key": "cicd:%s:%s" % (target.repository.lower(), short),
            "repository": target.repository,
            "repository_url": target.repository_url,
            "owner": target.owner,
            "repo": target.repo,
            "default_branch": target.branch,
            "canonical_sha": str(result.get("canonical_sha") or ""),
            "failure_fingerprint": fingerprint,
            "source_task_id": target.task_id,
            "publication_id": target.publication_id,
            "failed_runs": list(result.get("failed_runs") or []),
        }
        metadata: Dict[str, Any] = {
            "origin": origin,
            "cicd_check": dict(result),
            "maintenance": {
                "blocking": False,
                "urgency": "background",
                "coalesce_scope": target.repository.lower(),
            },
        }
        if target.task_id:
            metadata["relationships"] = {"parent_task_id": target.task_id}
        description = self._cleanup_task_description(target, result)
        task = self.control_plane.create_task(
            "Reconcile CI health for %s" % target.repository,
            description=description,
            project=target.project,
            priority=target.policy.priority,
            required_capabilities=list(target.policy.required_capabilities),
            metadata=metadata,
            actor=actor,
            idempotency_key="cicd-failure:%s" % short,
            _idempotency_scope="cicd-monitor",
        )
        self._record(
            CLEANUP_TASK_EVENT,
            "info",
            {
                "schema": CICD_MONITOR_SCHEMA,
                "repository": target.repository,
                "task_id": getattr(task, "id", ""),
                "failure_fingerprint": fingerprint,
                "blocking": False,
            },
            subject_type="project",
            subject_id=target.project,
            source=actor,
        )
        return task, True

    @staticmethod
    def _cleanup_task_description(target: _Target, result: Mapping[str, Any]) -> str:
        failed = list(result.get("failed_runs") or [])
        run_lines = [
            "- %s: %s (%s)"
            % (
                item.get("name") or item.get("workflow_id") or item.get("id"),
                item.get("conclusion") or "failure",
                item.get("url") or "no URL",
            )
            for item in failed
            if isinstance(item, Mapping)
        ]
        return "\n".join(
            [
                "GitHub Actions failed for %s." % target.repository,
                "Repository: %s" % target.repository_url,
                "Branch: %s" % target.branch,
                "Canonical SHA: %s" % (result.get("canonical_sha") or target.head_sha or "unknown"),
                "",
                "Failed runs:",
                *(run_lines or ["- no run details returned"]),
                "",
                "This is background maintenance, not a release gate or incident. "
                "Inspect each failed run and its failed logs (for example, `gh run "
                "view <run-id> --log-failed --repo %s`). Classify the failure as "
                "code, infrastructure, flaky test, or external dependency." % target.repository,
                "",
                "Fix the problem directly when the repair is bounded and useful. "
                "If it is transient or intentionally accepted, record that evidence "
                "and close the cleanup. File narrower follow-up work only when the "
                "remaining repair genuinely needs to be split.",
            ]
        )

    # -- due-work discovery -------------------------------------------------

    def _due_followups(self) -> List[_Target]:
        now = self._now()
        schedules = self._observations(SCHEDULE_EVENT, limit=self.config.max_observations)
        targets: List[_Target] = []
        for event in reversed(schedules):
            detail = self._event_detail(event)
            key = str(detail.get("schedule_key") or "")
            if not key or self._followup_completed(key):
                continue
            due_at = _parse_time(detail.get("due_at"))
            if due_at is None or due_at > now:
                continue
            if not self._pending_retry_due(
                FOLLOWUP_PENDING_EVENT,
                subject_type="task",
                subject_id=str(detail.get("task_id") or ""),
                schedule_key=key,
            ):
                continue
            parsed = parse_github_owner_repo(str(detail.get("repository_url") or ""))
            if parsed is None:
                continue
            owner, repo = parsed
            metadata = self._project_metadata(str(detail.get("project") or ""))
            policy = ProjectCICDPolicy.from_metadata(
                metadata, str(detail.get("repository_url") or "")
            )
            targets.append(
                _Target(
                    project=str(detail.get("project") or ""),
                    repository_url=str(detail.get("repository_url") or ""),
                    owner=owner,
                    repo=repo,
                    branch=str(detail.get("default_branch") or policy.default_branch),
                    policy=policy,
                    head_sha=str(detail.get("canonical_sha") or "").lower(),
                    task_id=str(detail.get("task_id") or ""),
                    publication_id=str(detail.get("publication_id") or ""),
                    schedule_key=key,
                    trigger="post_publication",
                )
            )
            if len(targets) >= self.config.max_checks_per_run:
                break
        return targets

    def _due_periodic_targets(self) -> List[_Target]:
        now = self._now()
        targets: List[_Target] = []
        for record in self._candidate_projects():
            project = str(getattr(record, "name", "") or "")
            metadata = getattr(record, "metadata", None) or {}
            repo_url = str(
                metadata.get("repository_url") if isinstance(metadata, Mapping) else ""
            ).strip()
            policy = ProjectCICDPolicy.from_metadata(metadata, repo_url)
            parsed = parse_github_owner_repo(repo_url)
            if not policy.enabled or parsed is None:
                continue
            last = self._latest_check(project)
            if last is not None:
                detail = self._event_detail(last)
                checked_at = _parse_time(detail.get("checked_at") or self._event_created_at(last))
                if checked_at is not None:
                    if detail.get("status") == "ci_absent":
                        cadence = self.config.absent_recheck_seconds
                    else:
                        average = self._float_or_none(detail.get("average_latency_seconds"))
                        cadence = cadence_seconds_for_latency(average)
                    if (now - checked_at).total_seconds() < cadence:
                        continue
            if not self._pending_retry_due(
                CHECK_PENDING_EVENT,
                subject_type="project",
                subject_id=project,
            ):
                continue
            owner, repo = parsed
            targets.append(
                _Target(
                    project=project,
                    repository_url=repo_url,
                    owner=owner,
                    repo=repo,
                    branch=policy.default_branch,
                    policy=policy,
                )
            )
        return targets

    # -- durable event helpers ---------------------------------------------

    def _record_check(
        self,
        target: _Target,
        result: Mapping[str, Any],
        *,
        actor: str,
        pending: bool,
    ) -> None:
        detail = dict(result)
        if pending:
            detail["retry_after"] = _iso(
                self._now() + timedelta(seconds=self.config.pending_retry_seconds)
            )
            self._record(
                CHECK_PENDING_EVENT,
                "warning" if result.get("error") else "info",
                detail,
                subject_type="project",
                subject_id=target.project,
                source=actor,
            )
            if target.schedule_key:
                self._record(
                    FOLLOWUP_PENDING_EVENT,
                    "warning" if result.get("error") else "info",
                    detail,
                    subject_type="task",
                    subject_id=target.task_id,
                    source=actor,
                )
            return
        level = "warning" if result.get("status") == "failure" else "info"
        self._record(
            CHECK_COMPLETED_EVENT,
            level,
            detail,
            subject_type="project",
            subject_id=target.project,
            source=actor,
        )
        if target.schedule_key:
            self._record(
                FOLLOWUP_COMPLETED_EVENT,
                level,
                detail,
                subject_type="task",
                subject_id=target.task_id,
                source=actor,
            )

    def _record(
        self,
        event_type: str,
        level: str,
        detail: Mapping[str, Any],
        *,
        subject_type: str,
        subject_id: str,
        source: str,
    ) -> None:
        self.control_plane.record_log(
            event_type,
            layer="control_plane",
            source=source,
            level=level,
            subject_type=subject_type,
            subject_id=subject_id,
            detail=dict(detail),
        )

    def _observe(self, event_type: str, level: str, detail: Mapping[str, Any]) -> None:
        try:
            self._record(
                event_type,
                level,
                detail,
                subject_type="service",
                subject_id="cicd-monitor",
                source="cicd-monitor",
            )
        except Exception:  # noqa: BLE001 - telemetry cannot stop the controller.
            _log.warning("could not record cicd-monitor telemetry", exc_info=True)

    def _observations(
        self,
        name: str,
        *,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Any]:
        kwargs: Dict[str, Any] = {
            "kind": "log",
            "name": name,
            "limit": limit or self.config.max_observations,
        }
        if subject_type is not None:
            kwargs["subject_type"] = subject_type
        if subject_id is not None:
            kwargs["subject_id"] = subject_id
        try:
            return list(self.control_plane.list_observability(**kwargs))
        except Exception as exc:  # noqa: BLE001 - retry on a later controller pass.
            _log.warning(
                "cicd-monitor could not list %s observations: %s",
                name,
                _safe_error(exc),
            )
            return []

    def _matching_observation(
        self,
        name: str,
        *,
        subject_type: str,
        subject_id: str,
        key: str,
        value: str,
    ) -> Optional[Any]:
        for event in self._observations(name, subject_type=subject_type, subject_id=subject_id):
            if str(self._event_detail(event).get(key) or "") == value:
                return event
        return None

    def _followup_completed(self, schedule_key: str) -> bool:
        for event in self._observations(FOLLOWUP_COMPLETED_EVENT):
            if str(self._event_detail(event).get("schedule_key") or "") == schedule_key:
                return True
        return False

    def _pending_retry_due(
        self,
        name: str,
        *,
        subject_type: str,
        subject_id: str,
        schedule_key: str = "",
    ) -> bool:
        now = self._now()
        for event in self._observations(name, subject_type=subject_type, subject_id=subject_id):
            detail = self._event_detail(event)
            if schedule_key and str(detail.get("schedule_key") or "") != schedule_key:
                continue
            retry_after = _parse_time(detail.get("retry_after"))
            return retry_after is None or retry_after <= now
        return True

    def _latest_check(self, project: str) -> Optional[Any]:
        events = self._observations(
            CHECK_COMPLETED_EVENT,
            subject_type="project",
            subject_id=project,
        )
        return events[0] if events else None

    def _latest_average_latency(self, project: str) -> Optional[float]:
        event = self._latest_check(project)
        if event is None:
            return None
        return self._float_or_none(self._event_detail(event).get("average_latency_seconds"))

    @staticmethod
    def _event_detail(event: Any) -> Dict[str, Any]:
        if isinstance(event, Mapping):
            detail = event.get("detail")
        else:
            detail = getattr(event, "detail", None)
        return dict(detail) if isinstance(detail, Mapping) else {}

    @staticmethod
    def _event_created_at(event: Any) -> str:
        if isinstance(event, Mapping):
            return str(event.get("created_at") or "")
        return str(getattr(event, "created_at", "") or "")

    @staticmethod
    def _float_or_none(value: Any) -> Optional[float]:
        if value in {None, ""}:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # -- project helpers ----------------------------------------------------

    def _candidate_projects(self) -> List[Any]:
        try:
            return list(self.control_plane.list_project_records())
        except Exception as exc:  # noqa: BLE001 - retry next pass.
            _log.warning("cicd-monitor could not list projects: %s", _safe_error(exc))
            return []

    def _project_record(self, project: str) -> Any:
        for record in self._candidate_projects():
            if str(getattr(record, "name", "") or "") == str(project):
                return record
        raise ValueError("project record not found: %s" % project)

    def _project_metadata(self, project: str) -> Mapping[str, Any]:
        try:
            metadata = getattr(self._project_record(project), "metadata", None)
        except ValueError:
            return {}
        return metadata if isinstance(metadata, Mapping) else {}

    @staticmethod
    def _schedule_key(publication_id: str, task_id: str, sha: str) -> str:
        identity = str(publication_id or "").strip() or str(task_id or "").strip()
        return "github-publication:%s:%s" % (identity, sha.lower())

    @staticmethod
    def _busy_report(trigger: str, reason: str = "run_active") -> Dict[str, Any]:
        return {
            "schema": CICD_MONITOR_SCHEMA,
            "status": "busy",
            "trigger": trigger,
            "reason": reason,
            "checked_count": 0,
            "repositories": [],
        }


__all__ = [
    "ActionsFetcher",
    "CHECK_COMPLETED_EVENT",
    "CHECK_PENDING_EVENT",
    "CICD_CHECK_SCHEMA",
    "CICD_MONITOR_SCHEMA",
    "CICD_SCHEDULE_SCHEMA",
    "CICDMonitor",
    "CICDMonitorConfig",
    "FOLLOWUP_COMPLETED_EVENT",
    "FOLLOWUP_PENDING_EVENT",
    "ProjectCICDPolicy",
    "SCHEDULE_EVENT",
    "cadence_seconds_for_latency",
    "post_publication_delay_hours",
]
