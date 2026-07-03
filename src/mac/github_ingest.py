"""GitHub-issue → mac-task ingestion.

The autonomous fleet self-drives execution (dispatch → verify → merge → learn)
but does not *manufacture* its own backlog: something has to file the tasks it
drains. Three asynchronous work generators are meant to feed it — Hermes chat,
per-repo mac tasks, and GitHub issues. This module implements the third, which
previously did not exist as running code.

Design:

* Enumerate registered projects (``ProjectRecord``), each of which carries its
  git remote in ``metadata["repository_url"]`` (see
  ``ControlPlane._project_repository_url``). A project opts in to issue
  ingestion by setting ``metadata["github_issue_ingest"]`` (see
  :func:`ProjectIngestPolicy.from_metadata`); nothing happens for a project
  that has not opted in, so enabling the poller fleet-wide is safe.
* For each opted-in GitHub repo, list open issues via the GitHub REST API using
  the same env-backed token the fleet already uses for fetch/publish
  (``gitops.token_for_host("github")`` → ``GH_TOKEN``/``GITHUB_TOKEN``).
* Create a mac task per issue, idempotently. The dedupe key
  (``github:owner/repo#number``) is stamped into ``metadata["origin"]``; an
  issue that already has a task in any state is skipped, so re-polling never
  duplicates work and a completed issue is not reopened.
* Tasks are created under the project, so ``create_task``'s project defaults
  couple them to the repo automatically; they are then dispatched by the
  normal hub tick.

The poller runs on its own daemon thread (mirroring
``RepositoryRefReconciler``) because issues change on a minutes cadence, not the
30s control-loop cadence. Per-repo failures are isolated and recorded as
telemetry; one repo's rate-limit or auth error never stops the others.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from mac import gitops

GITHUB_INGEST_SCHEMA = "mac.github_issue_ingest.v1"

MIN_INTERVAL_SECONDS = 30.0
MAX_INTERVAL_SECONDS = 24 * 60 * 60.0
MAX_INITIAL_DELAY_SECONDS = 60 * 60.0
DEFAULT_INTERVAL_SECONDS = 300.0
DEFAULT_INITIAL_DELAY_SECONDS = 60.0
DEFAULT_MAX_ISSUES_PER_REPO = 25
DEFAULT_MAX_OPEN_TASKS_PER_PROJECT = 50
GITHUB_API_ROOT = "https://api.github.com"

_log = logging.getLogger("mac.github_ingest")

# A fetcher returns the raw GitHub issue objects (list of dicts). Injectable so
# tests never touch the network.
IssueFetcher = Callable[..., List[Dict[str, Any]]]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_error(error: Any) -> str:
    return str(error).strip()[:500]


def issue_origin_key(owner: str, repo: str, number: Any) -> str:
    """Stable dedupe key for a GitHub issue, case-normalized on owner/repo."""
    return "github:%s/%s#%d" % (str(owner).lower(), str(repo).lower(), int(number))


def parse_github_owner_repo(url: str) -> Optional[Tuple[str, str]]:
    """Return ``(owner, repo)`` for a github.com remote URL, else ``None``.

    Handles https, ``git@github.com:owner/repo.git`` scp-like, and ssh forms.
    Returns ``None`` for non-GitHub hosts so gitea/other remotes are skipped.
    """
    value = str(url or "").strip()
    if not value:
        return None
    match = re.match(r"^[A-Za-z0-9._-]+@([A-Za-z0-9._-]+):(.+)$", value)
    if match:
        host = match.group(1).lower()
        path = match.group(2)
    else:
        parsed = urllib.parse.urlsplit(
            value if "://" in value else "https://" + value
        )
        host = (parsed.hostname or "").lower()
        path = parsed.path
    if host != "github.com" and not host.endswith(".github.com"):
        return None
    parts = [segment for segment in path.strip("/").split("/") if segment]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not owner or not repo:
        return None
    return owner, repo


def _http_list_issues(
    owner: str,
    repo: str,
    *,
    token: str,
    labels: Tuple[str, ...],
    state: str,
    limit: int,
    timeout: float = 20.0,
) -> List[Dict[str, Any]]:
    """List issues for ``owner/repo`` via the GitHub REST API (paginated).

    Uses stdlib urllib (no new dependency). Bounded by ``limit`` so a repo with
    thousands of issues cannot stall a poll. The GitHub issues endpoint also
    returns pull requests; the caller filters those out (they carry a
    ``pull_request`` key).
    """
    collected: List[Dict[str, Any]] = []
    per_page = 100
    page = 1
    while len(collected) < limit:
        want = min(per_page, limit - len(collected))
        query = {
            "state": state,
            "per_page": str(want),
            "page": str(page),
            "sort": "created",
            "direction": "asc",
        }
        if labels:
            query["labels"] = ",".join(labels)
        url = "%s/repos/%s/%s/issues?%s" % (
            GITHUB_API_ROOT,
            urllib.parse.quote(owner),
            urllib.parse.quote(repo),
            urllib.parse.urlencode(query),
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mac-github-ingest",
        }
        if token:
            headers["Authorization"] = "Bearer %s" % token
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list) or not payload:
            break
        collected.extend(payload)
        if len(payload) < want:
            break
        page += 1
    return collected[:limit]


@dataclass(frozen=True)
class GitHubIngestConfig:
    """Fleet-wide ingestion config, resolved from the environment."""

    enabled: bool = False
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS
    max_issues_per_repo: int = DEFAULT_MAX_ISSUES_PER_REPO
    max_open_tasks_per_project: int = DEFAULT_MAX_OPEN_TASKS_PER_PROJECT
    configuration_error: str = ""

    @property
    def active(self) -> bool:
        return self.enabled and not self.configuration_error

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "active": self.active}

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "GitHubIngestConfig":
        env = os.environ if environ is None else environ
        errors: List[str] = []
        enabled = str(env.get("MAC_GITHUB_INGEST_ENABLED") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        def _num(name: str, default: float, low: float, high: float) -> float:
            raw = str(env.get(name) or "").strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError:
                errors.append("%s must be numeric" % name)
                return default
            if value < low or value > high:
                errors.append("%s must be between %s and %s" % (name, low, high))
                return default
            return value

        interval = _num(
            "MAC_GITHUB_INGEST_INTERVAL_SECONDS",
            DEFAULT_INTERVAL_SECONDS,
            MIN_INTERVAL_SECONDS,
            MAX_INTERVAL_SECONDS,
        )
        initial_delay = _num(
            "MAC_GITHUB_INGEST_INITIAL_DELAY_SECONDS",
            DEFAULT_INITIAL_DELAY_SECONDS,
            0.0,
            MAX_INITIAL_DELAY_SECONDS,
        )
        max_issues = int(
            _num("MAC_GITHUB_INGEST_MAX_ISSUES_PER_REPO", DEFAULT_MAX_ISSUES_PER_REPO, 1, 1000)
        )
        max_open = int(
            _num(
                "MAC_GITHUB_INGEST_MAX_OPEN_TASKS_PER_PROJECT",
                DEFAULT_MAX_OPEN_TASKS_PER_PROJECT,
                1,
                10000,
            )
        )
        return cls(
            enabled=enabled,
            interval_seconds=interval,
            initial_delay_seconds=initial_delay,
            max_issues_per_repo=max_issues,
            max_open_tasks_per_project=max_open,
            configuration_error="; ".join(errors),
        )


@dataclass(frozen=True)
class ProjectIngestPolicy:
    """Per-project opt-in, read from ``ProjectRecord.metadata``."""

    enabled: bool = False
    labels: Tuple[str, ...] = ()
    state: str = "open"
    default_capabilities: Tuple[str, ...] = ()
    priority: int = 0
    auto_cancel_closed: bool = False

    @classmethod
    def from_metadata(cls, metadata: Any) -> "ProjectIngestPolicy":
        if not isinstance(metadata, Mapping):
            return cls()
        raw = metadata.get("github_issue_ingest")
        if not isinstance(raw, Mapping):
            return cls()
        enabled = bool(raw.get("enabled"))
        labels_raw = raw.get("labels") or []
        labels = tuple(
            str(item).strip()
            for item in labels_raw
            if isinstance(item, (str,)) and str(item).strip()
        ) if isinstance(labels_raw, (list, tuple)) else ()
        state = str(raw.get("state") or "open").strip().lower()
        if state not in {"open", "all", "closed"}:
            state = "open"
        caps_raw = raw.get("default_capabilities") or []
        caps = tuple(
            str(item).strip()
            for item in caps_raw
            if isinstance(item, str) and str(item).strip()
        ) if isinstance(caps_raw, (list, tuple)) else ()
        try:
            priority = int(raw.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        return cls(
            enabled=enabled,
            labels=labels,
            state=state,
            default_capabilities=caps,
            priority=priority,
            auto_cancel_closed=bool(raw.get("auto_cancel_closed")),
        )


class GitHubIssueIngestor:
    """Periodically turn opted-in repos' GitHub issues into mac tasks."""

    def __init__(
        self,
        control_plane: Any,
        config: GitHubIngestConfig,
        *,
        issue_fetcher: Optional[IssueFetcher] = None,
        token_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        self.control_plane = control_plane
        self.config = config
        self._issue_fetcher = issue_fetcher or _http_list_issues
        self._token_provider = token_provider or (
            lambda: gitops.token_for_host("github")
        )
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
                    "github.ingest.configuration_invalid",
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
                name="mac-github-ingest",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        self._observe("github.ingest.started", "info", {"config": self.config.to_dict()})
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._observe("github.ingest.stopped", "info", {})
        return stopped

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            thread = self._thread
            last_report = copy.deepcopy(self._last_report)
        return {
            "schema": GITHUB_INGEST_SCHEMA,
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
                _log.warning("github-ingest tick failed", exc_info=True)
            if self._stop_event.wait(max(0.01, self.config.interval_seconds)):
                return

    # -- core ---------------------------------------------------------------

    def run_once(
        self,
        *,
        actor: str = "github-ingest",
        trigger: str = "operator",
    ) -> Dict[str, Any]:
        """Poll every opted-in repo once. Concurrency-guarded (skips if busy)."""
        if not self._run_lock.acquire(blocking=False):
            return {
                "schema": GITHUB_INGEST_SCHEMA,
                "status": "busy",
                "trigger": trigger,
                "repository_count": 0,
                "repositories": [],
            }
        started_at = _utcnow()
        run_id = "ghingest_%s" % uuid.uuid4().hex
        results: List[Dict[str, Any]] = []
        try:
            token = ""
            try:
                token = str(self._token_provider() or "").strip()
            except Exception as exc:  # noqa: BLE001 - token lookup must not crash the run.
                _log.warning("github-ingest token lookup failed: %s", _safe_error(exc))

            existing_by_key, open_task_counts = self._index_existing_tasks()

            for record in self._candidate_projects():
                results.append(
                    self._ingest_project(
                        record,
                        token=token,
                        actor=actor,
                        existing_by_key=existing_by_key,
                        open_task_counts=open_task_counts,
                    )
                )
        finally:
            self._run_lock.release()

        report = {
            "schema": GITHUB_INGEST_SCHEMA,
            "run_id": run_id,
            "status": "ok",
            "trigger": trigger,
            "started_at": started_at,
            "finished_at": _utcnow(),
            "repository_count": len(results),
            "created_count": sum(int(r.get("created", 0)) for r in results),
            "cancelled_count": sum(int(r.get("cancelled", 0)) for r in results),
            "repositories": results,
        }
        with self._state_lock:
            self._last_report = report
        level = "warning" if any(r.get("error") for r in results) else "info"
        self._observe("github.ingest.run", level, report)
        return report

    def _candidate_projects(self) -> List[Any]:
        try:
            return list(self.control_plane.list_project_records())
        except Exception as exc:  # noqa: BLE001 - report and retry next tick.
            _log.warning("github-ingest could not list projects: %s", _safe_error(exc))
            return []

    def _index_existing_tasks(
        self,
    ) -> Tuple[Dict[str, Any], Dict[str, int]]:
        """Build (origin-key → task) and (project → open github-task count).

        The count is used for per-project backpressure so a flood of issues
        cannot swamp the ledger.
        """
        existing_by_key: Dict[str, Any] = {}
        open_counts: Dict[str, int] = {}
        try:
            tasks = list(self.control_plane.list_tasks())
        except Exception as exc:  # noqa: BLE001
            _log.warning("github-ingest could not list tasks: %s", _safe_error(exc))
            return existing_by_key, open_counts
        open_states = self._open_states()
        for task in tasks:
            metadata = getattr(task, "metadata", None) or {}
            origin = metadata.get("origin") if isinstance(metadata, Mapping) else None
            if not isinstance(origin, Mapping):
                continue
            if origin.get("type") != "github_issue":
                continue
            key = str(origin.get("key") or "")
            if not key:
                continue
            existing_by_key[key] = task
            state = str(getattr(task, "state", "") or "")
            if state in open_states:
                project = str(getattr(task, "project", "") or "")
                open_counts[project] = open_counts.get(project, 0) + 1
        return existing_by_key, open_counts

    @staticmethod
    def _open_states() -> frozenset:
        try:
            from mac.models import TaskState

            return frozenset(
                {
                    TaskState.OPEN.value,
                    TaskState.BLOCKED.value,
                    TaskState.CLAIMED.value,
                    TaskState.RUNNING.value,
                    TaskState.NEEDS_REVIEW.value,
                    TaskState.REVIEWING.value,
                }
            )
        except Exception:  # noqa: BLE001 - defensive; fall back to literals.
            return frozenset(
                {
                    "open",
                    "blocked",
                    "claimed",
                    "running",
                    "needs_review",
                    "reviewing",
                }
            )

    def _ingest_project(
        self,
        record: Any,
        *,
        token: str,
        actor: str,
        existing_by_key: Dict[str, Any],
        open_task_counts: Dict[str, int],
    ) -> Dict[str, Any]:
        project = str(getattr(record, "name", "") or "")
        metadata = getattr(record, "metadata", None) or {}
        policy = ProjectIngestPolicy.from_metadata(metadata)
        result: Dict[str, Any] = {
            "project": project,
            "enabled": policy.enabled,
            "created": 0,
            "cancelled": 0,
            "skipped": 0,
        }
        if not policy.enabled:
            return result
        repo_url = (
            metadata.get("repository_url") if isinstance(metadata, Mapping) else None
        )
        parsed = parse_github_owner_repo(str(repo_url or ""))
        if parsed is None:
            result["skipped_reason"] = "no github repository_url"
            return result
        owner, repo = parsed
        result["repository"] = "%s/%s" % (owner, repo)
        if not token:
            result["error"] = "no github token (GH_TOKEN/GITHUB_TOKEN) configured"
            return result

        try:
            raw_issues = self._issue_fetcher(
                owner,
                repo,
                token=token,
                labels=policy.labels,
                state=policy.state,
                limit=self.config.max_issues_per_repo,
            )
        except urllib.error.HTTPError as exc:  # noqa: PERF203 - explicit clarity.
            result["error"] = "github api %s: %s" % (exc.code, _safe_error(exc))
            return result
        except Exception as exc:  # noqa: BLE001 - isolate this repo's failure.
            result["error"] = _safe_error(exc)
            return result

        # Filter out pull requests (the issues endpoint returns both).
        issues = [
            issue
            for issue in raw_issues
            if isinstance(issue, Mapping) and "pull_request" not in issue
        ]
        result["fetched"] = len(issues)
        if len(raw_issues) >= self.config.max_issues_per_repo:
            # No silent truncation — record that we hit the cap.
            result["truncated_at"] = self.config.max_issues_per_repo

        open_seen_keys: List[str] = []
        current_open = int(open_task_counts.get(project, 0))
        for issue in issues:
            number = issue.get("number")
            if number is None:
                continue
            key = issue_origin_key(owner, repo, number)
            open_seen_keys.append(key)
            if key in existing_by_key:
                result["skipped"] += 1
                continue
            if current_open >= self.config.max_open_tasks_per_project:
                result["backpressure"] = self.config.max_open_tasks_per_project
                break
            try:
                task = self._create_task_for_issue(
                    project=project,
                    owner=owner,
                    repo=repo,
                    repo_url=str(repo_url),
                    issue=issue,
                    policy=policy,
                    key=key,
                    actor=actor,
                )
            except Exception as exc:  # noqa: BLE001 - isolate one issue's failure.
                result.setdefault("errors", []).append(
                    {"issue": number, "error": _safe_error(exc)}
                )
                continue
            existing_by_key[key] = task
            current_open += 1
            result["created"] += 1

        if policy.auto_cancel_closed and policy.state == "open":
            result["cancelled"] = self._cancel_closed_issue_tasks(
                project=project,
                owner=owner,
                repo=repo,
                open_seen_keys=set(open_seen_keys),
                existing_by_key=existing_by_key,
                actor=actor,
            )
        return result

    def _create_task_for_issue(
        self,
        *,
        project: str,
        owner: str,
        repo: str,
        repo_url: str,
        issue: Mapping[str, Any],
        policy: ProjectIngestPolicy,
        key: str,
        actor: str,
    ) -> Any:
        number = int(issue.get("number"))
        title = str(issue.get("title") or "").strip() or ("issue #%d" % number)
        html_url = str(issue.get("html_url") or "").strip()
        body = str(issue.get("body") or "").strip()
        labels = [
            str(label.get("name"))
            for label in (issue.get("labels") or [])
            if isinstance(label, Mapping) and label.get("name")
        ]
        description = self._render_description(
            owner=owner,
            repo=repo,
            number=number,
            html_url=html_url,
            body=body,
            labels=labels,
        )
        origin = {
            "type": "github_issue",
            "provider": "github",
            "key": key,
            "repository_url": repo_url,
            "owner": owner,
            "repo": repo,
            "number": number,
            "url": html_url,
            "labels": labels,
            "issue_updated_at": str(issue.get("updated_at") or ""),
        }
        return self.control_plane.create_task(
            title,
            description=description,
            project=project,
            priority=policy.priority,
            required_capabilities=list(policy.default_capabilities),
            metadata={"origin": origin},
            actor=actor,
        )

    @staticmethod
    def _render_description(
        *,
        owner: str,
        repo: str,
        number: int,
        html_url: str,
        body: str,
        labels: List[str],
    ) -> str:
        header = "GitHub issue %s/%s#%d" % (owner, repo, number)
        if html_url:
            header += "\n%s" % html_url
        if labels:
            header += "\nLabels: %s" % ", ".join(labels)
        parts = [header]
        if body:
            parts.append(body)
        parts.append(
            "---\n"
            "Ingested from the above GitHub issue. Implement the change on a "
            "branch and open a pull request that resolves it."
        )
        return "\n\n".join(parts)

    def _cancel_closed_issue_tasks(
        self,
        *,
        project: str,
        owner: str,
        repo: str,
        open_seen_keys: set,
        existing_by_key: Dict[str, Any],
        actor: str,
    ) -> int:
        """Cancel OPEN github-issue tasks whose issue is no longer open.

        Only OPEN tasks are cancelled — in-flight (claimed/in_progress/review)
        work is left alone so a mid-run task is never yanked out from under a
        worker. Scoped to this repo's key prefix.
        """
        prefix = "github:%s/%s#" % (owner.lower(), repo.lower())
        open_state = self._open_state_value()
        cancelled = 0
        for key, task in list(existing_by_key.items()):
            if not key.startswith(prefix):
                continue
            if key in open_seen_keys:
                continue
            if str(getattr(task, "project", "") or "") != project:
                continue
            if str(getattr(task, "state", "") or "") != open_state:
                continue
            try:
                self.control_plane.transition_task(
                    getattr(task, "id"),
                    "cancelled",
                    actor,
                    {"reason": "github issue closed", "origin_key": key},
                )
                cancelled += 1
            except Exception as exc:  # noqa: BLE001 - isolate; keep going.
                _log.warning(
                    "github-ingest could not cancel task for %s: %s",
                    key,
                    _safe_error(exc),
                )
        return cancelled

    @staticmethod
    def _open_state_value() -> str:
        try:
            from mac.models import TaskState

            return TaskState.OPEN.value
        except Exception:  # noqa: BLE001
            return "open"

    # -- telemetry ----------------------------------------------------------

    def _observe(self, event_type: str, level: str, detail: Dict[str, Any]) -> None:
        try:
            self.control_plane.record_log(
                event_type,
                layer="control_plane",
                source="github-ingest",
                level=level,
                subject_type="service",
                subject_id="github-ingest",
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - telemetry must not stop ingestion.
            _log.warning("could not record github-ingest telemetry", exc_info=True)


__all__ = [
    "GITHUB_INGEST_SCHEMA",
    "GitHubIngestConfig",
    "GitHubIssueIngestor",
    "ProjectIngestPolicy",
    "issue_origin_key",
    "parse_github_owner_repo",
]
