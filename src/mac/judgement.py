"""Hub judgement: hourly process-quality authority over the task lifecycle.

The claim/fix/deliver cycle grew gates until they were the failure mode.
On 2026-08-23 release blockers passed their tests, got pushed, and then
died in semantic review — 17 million tokens, no publication, two other P0s
blocked behind a failed ADR. Nothing on the hub was allowed to look at
that and stop it.

This process does. It is not sandboxed. It reads the current mac checkout
and ``skills/judgement/SKILL.md``, judges the quality and number of
lifecycle gates and the states tasks have wound up in, and intervenes
with the same verbs an operator has: stop a task, hold an agent, stop the
fleet, redeploy an image, and start the appropriate entities back up.

No-op unless ``MAC_JUDGEMENT_ENABLED`` is set. The hub deploy turns it on.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from mac.config_coercion import bounded_env_int, bounded_env_number

JUDGEMENT_SCHEMA = "mac.judgement.v1"
JUDGEMENT_ACTOR = "hub-judgement"
HOLD_REASON_PREFIX = "judgement:"
SKILL_RELATIVE_PATH = "skills/judgement/SKILL.md"

MIN_INTERVAL_SECONDS = 300.0
DEFAULT_INTERVAL_SECONDS = 3600.0
DEFAULT_INITIAL_DELAY_SECONDS = 180.0
DEFAULT_MAX_ACTIONS_PER_CYCLE = 20
DEFAULT_MAX_REDEPLOYS_PER_DAY = 2
DEFAULT_REVIEWING_STUCK_SECONDS = 2 * 60 * 60.0
DEFAULT_REJECTION_LOOP_THRESHOLD = 2
DEFAULT_EXCESSIVE_REVIEWING_COUNT = 20
DEFAULT_EXCESSIVE_REVIEWING_FRACTION = 0.10
DEFAULT_TOO_MANY_GATES = 3

_ACTIVE_REVIEW_STATES = frozenset({"needs_review", "reviewing"})
_IN_FLIGHT_STATES = frozenset({"claimed", "running", "needs_review", "reviewing"})
_ORPHAN_PR_STATES = frozenset({"completed", "cancelled"})
_UNLANDED_PR_STATES = frozenset({"failed", "blocked", "reviewing", "needs_review", "waiting"})
_TASK_ID_RE = re.compile(r"task_[0-9a-f]{8,}")
_NONTERMINAL_STATES = frozenset(
    {
        "open",
        "waiting",
        "blocked",
        "claimed",
        "running",
        "needs_review",
        "reviewing",
        "needs_input",
        "stopped",
    }
)

_log = logging.getLogger("mac.judgement")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class JudgementConfig:
    enabled: bool = False
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS
    max_actions_per_cycle: int = DEFAULT_MAX_ACTIONS_PER_CYCLE
    max_redeploys_per_day: int = DEFAULT_MAX_REDEPLOYS_PER_DAY
    reviewing_stuck_seconds: float = DEFAULT_REVIEWING_STUCK_SECONDS
    rejection_loop_threshold: int = DEFAULT_REJECTION_LOOP_THRESHOLD
    excessive_reviewing_count: int = DEFAULT_EXCESSIVE_REVIEWING_COUNT
    excessive_reviewing_fraction: float = DEFAULT_EXCESSIVE_REVIEWING_FRACTION
    too_many_gates: int = DEFAULT_TOO_MANY_GATES
    repo_root: str = ""
    redeploy_command: str = ""
    configuration_error: str = ""

    @property
    def active(self) -> bool:
        return self.enabled and not self.configuration_error

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "active": self.active}

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "JudgementConfig":
        env = os.environ if environ is None else environ
        errors: List[str] = []
        enabled = str(env.get("MAC_JUDGEMENT_ENABLED") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        def _num(name: str, default: float, low: float, high: float) -> float:
            return bounded_env_number(env, name, default, low, high, errors=errors)

        repo_root = str(env.get("MAC_JUDGEMENT_REPO_ROOT") or "").strip()
        redeploy_command = str(env.get("MAC_JUDGEMENT_REDEPLOY_CMD") or "").strip()
        return cls(
            enabled=enabled,
            interval_seconds=_num(
                "MAC_JUDGEMENT_INTERVAL_SECONDS",
                DEFAULT_INTERVAL_SECONDS,
                MIN_INTERVAL_SECONDS,
                24 * 60 * 60.0,
            ),
            initial_delay_seconds=_num(
                "MAC_JUDGEMENT_INITIAL_DELAY_SECONDS",
                DEFAULT_INITIAL_DELAY_SECONDS,
                0.0,
                60 * 60.0,
            ),
            max_actions_per_cycle=bounded_env_int(
                env,
                "MAC_JUDGEMENT_MAX_ACTIONS_PER_CYCLE",
                DEFAULT_MAX_ACTIONS_PER_CYCLE,
                1,
                200,
                errors=errors,
            ),
            max_redeploys_per_day=bounded_env_int(
                env,
                "MAC_JUDGEMENT_MAX_REDEPLOYS_PER_DAY",
                DEFAULT_MAX_REDEPLOYS_PER_DAY,
                0,
                12,
                errors=errors,
            ),
            reviewing_stuck_seconds=_num(
                "MAC_JUDGEMENT_REVIEWING_STUCK_SECONDS",
                DEFAULT_REVIEWING_STUCK_SECONDS,
                60.0,
                7 * 24 * 60 * 60.0,
            ),
            rejection_loop_threshold=int(
                _num(
                    "MAC_JUDGEMENT_REJECTION_LOOP_THRESHOLD",
                    DEFAULT_REJECTION_LOOP_THRESHOLD,
                    2,
                    20,
                )
            ),
            excessive_reviewing_count=bounded_env_int(
                env,
                "MAC_JUDGEMENT_EXCESSIVE_REVIEWING_COUNT",
                DEFAULT_EXCESSIVE_REVIEWING_COUNT,
                1,
                1000,
                errors=errors,
            ),
            excessive_reviewing_fraction=_num(
                "MAC_JUDGEMENT_EXCESSIVE_REVIEWING_FRACTION",
                DEFAULT_EXCESSIVE_REVIEWING_FRACTION,
                0.01,
                1.0,
            ),
            too_many_gates=int(_num("MAC_JUDGEMENT_TOO_MANY_GATES", DEFAULT_TOO_MANY_GATES, 2, 20)),
            repo_root=repo_root,
            redeploy_command=redeploy_command,
            configuration_error="; ".join(errors),
        )


@dataclass
class Finding:
    kind: str
    summary: str
    detail: Dict[str, Any] = field(default_factory=dict)
    task_id: str = ""
    agent_id: str = ""
    recommended_action: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "detail": dict(self.detail),
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "recommended_action": self.recommended_action,
        }


RedeployRunner = Callable[[Sequence[str], str], Dict[str, Any]]
PullRequestLister = Callable[[str], Dict[str, Any]]
PullRequestCloser = Callable[[int, str, str], Dict[str, Any]]


class JudgementProcess:
    """Observe lifecycle-gate quality and intervene with privileged actions."""

    def __init__(
        self,
        control_plane: Any,
        config: JudgementConfig,
        *,
        environ: Optional[Mapping[str, str]] = None,
        redeploy_runner: Optional[RedeployRunner] = None,
        pr_lister: Optional[PullRequestLister] = None,
        pr_closer: Optional[PullRequestCloser] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.control_plane = control_plane
        self.config = config
        self._environ = environ
        self._redeploy_runner = redeploy_runner or _default_redeploy_runner
        self._pr_lister = pr_lister or _default_pr_lister
        self._pr_closer = pr_closer or _default_pr_closer
        self._now = now or _utcnow
        self._stop_event = threading.Event()
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last_report: Optional[Dict[str, Any]] = None
        self._redeploy_times: List[datetime] = []

    def start(self) -> bool:
        if not self.config.active:
            if self.config.configuration_error:
                self._observe(
                    "judgement.configuration_invalid",
                    "warning",
                    {"error": self.config.configuration_error},
                )
            return False
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            thread = threading.Thread(target=self._loop, name="mac-judgement", daemon=True)
            self._thread = thread
            thread.start()
        self._observe("judgement.started", "info", {"config": self.config.to_dict()})
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._observe("judgement.stopped", "info", {})
        return stopped

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            thread = self._thread
            last_report = copy.deepcopy(self._last_report)
        skill = self._skill_status()
        return {
            "schema": JUDGEMENT_SCHEMA,
            "config": self.config.to_dict(),
            "thread_alive": bool(thread is not None and thread.is_alive()),
            "run_active": self._run_lock.locked(),
            "skill": skill,
            "last_report": last_report,
        }

    def _loop(self) -> None:
        if self._stop_event.wait(max(0.0, self.config.initial_delay_seconds)):
            return
        while not self._stop_event.is_set():
            try:
                self.run_once(trigger="scheduled")
            except Exception:  # noqa: BLE001 - the next cycle must still run
                _log.warning("judgement cycle failed", exc_info=True)
            if self._stop_event.wait(max(0.01, self.config.interval_seconds)):
                return

    def run_once(
        self,
        *,
        actor: str = JUDGEMENT_ACTOR,
        trigger: str = "operator",
    ) -> Dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {
                "schema": JUDGEMENT_SCHEMA,
                "status": "busy",
                "trigger": trigger,
                "findings": [],
                "actions": [],
            }
        run_id = "judge_%s" % uuid.uuid4().hex
        findings: List[Finding] = []
        check_errors: List[str] = []
        try:
            for check in (
                self._check_review_rejection_loops,
                self._check_high_token_without_publication,
                self._check_failed_dependency_deadlocks,
                self._check_stuck_reviewing,
                self._check_semantic_reviewer_still_assigned,
                self._check_excessive_reviewing_population,
                self._check_too_many_gates,
                self._check_orphaned_pull_requests,
            ):
                try:
                    findings.extend(check())
                except Exception as exc:  # noqa: BLE001 - one check must not blind others
                    check_errors.append("%s: %s" % (check.__name__, str(exc)[:200]))
            actions = self._act_on_findings(findings, actor=actor, run_id=run_id)
        finally:
            self._run_lock.release()
        report = {
            "schema": JUDGEMENT_SCHEMA,
            "run_id": run_id,
            "status": "ok",
            "trigger": trigger,
            "finding_count": len(findings),
            "action_count": len(actions),
            "budget": self.config.max_actions_per_cycle,
            "findings": [finding.to_dict() for finding in findings],
            "actions": actions,
            "check_errors": check_errors,
            "skill": self._skill_status(),
        }
        with self._state_lock:
            self._last_report = report
        self._observe(
            "judgement.run",
            "info",
            {
                "run_id": run_id,
                "trigger": trigger,
                "finding_count": len(findings),
                "action_count": len(actions),
            },
        )
        return report

    # -- observe ------------------------------------------------------------

    def _check_review_rejection_loops(self) -> List[Finding]:
        findings: List[Finding] = []
        for task in self._lifecycle_tasks():
            reviews = list(self.control_plane.list_reviews(task.id))
            rejected = [
                review
                for review in reviews
                if str(getattr(review, "status", "") or "").lower() == "rejected"
            ]
            if len(rejected) < self.config.rejection_loop_threshold:
                continue
            findings.append(
                Finding(
                    kind="review_rejection_loop",
                    task_id=task.id,
                    agent_id=str(getattr(task, "owner_agent_id", "") or ""),
                    summary=(
                        "%s was rejected %d times after entering review" % (task.id, len(rejected))
                    ),
                    detail={
                        "rejected_count": len(rejected),
                        "threshold": self.config.rejection_loop_threshold,
                        "state": task.state,
                    },
                    recommended_action="stop_task",
                )
            )
        return findings

    def _check_high_token_without_publication(self) -> List[Finding]:
        findings: List[Finding] = []
        for task in self._lifecycle_tasks():
            try:
                detail = self.control_plane.task_detail(task.id, history_limit=0)
            except Exception:  # noqa: BLE001 - a missing profile must not stop the cycle
                continue
            profile = detail.get("profile") if isinstance(detail, Mapping) else None
            if not isinstance(profile, Mapping):
                continue
            signals = profile.get("signals") or []
            if not any(
                isinstance(signal, Mapping)
                and signal.get("code") == "high_token_work_without_publication"
                for signal in signals
            ):
                continue
            findings.append(
                Finding(
                    kind="high_token_without_publication",
                    task_id=task.id,
                    agent_id=str(getattr(task, "owner_agent_id", "") or ""),
                    summary="%s spent tokens and never published" % task.id,
                    detail={"signals": list(signals)},
                    recommended_action="stop_task",
                )
            )
        return findings

    def _check_failed_dependency_deadlocks(self) -> List[Finding]:
        findings: List[Finding] = []
        tasks = {task.id: task for task in self._all_known_tasks()}
        for task in tasks.values():
            if str(getattr(task, "state", "") or "") not in {"blocked", "waiting"}:
                continue
            deps = list(getattr(task, "dependencies", None) or [])
            failed: List[str] = []
            for dep_id in deps:
                dep = tasks.get(str(dep_id))
                if dep is None:
                    try:
                        dep = self.control_plane.get_task(str(dep_id))
                    except Exception:  # noqa: BLE001
                        continue
                if str(getattr(dep, "state", "") or "").lower() == "failed":
                    failed.append(dep.id)
            if not failed:
                continue
            findings.append(
                Finding(
                    kind="failed_dependency_deadlock",
                    task_id=task.id,
                    summary=(
                        "%s is blocked on failed dependencies %s" % (task.id, ", ".join(failed))
                    ),
                    detail={"failed_dependencies": failed, "join": "all_success"},
                    recommended_action="stop_task",
                )
            )
        return findings

    def _check_stuck_reviewing(self) -> List[Finding]:
        findings: List[Finding] = []
        now = self._now()
        for task in self._lifecycle_tasks():
            if str(getattr(task, "state", "") or "") not in _ACTIVE_REVIEW_STATES:
                continue
            updated = _parse_ts(getattr(task, "updated_at", None))
            if updated is None:
                continue
            age = (now - updated).total_seconds()
            if age < self.config.reviewing_stuck_seconds:
                continue
            reviews = list(self.control_plane.list_reviews(task.id))
            pending = [
                review
                for review in reviews
                if str(getattr(review, "status", "") or "").lower() == "pending"
            ]
            reviewer_id = ""
            if pending:
                reviewer_id = str(getattr(pending[-1], "reviewer_agent_id", "") or "")
            semantic = bool(reviewer_id) and not self._agent_is_virtual(reviewer_id)
            findings.append(
                Finding(
                    kind="stuck_reviewing",
                    task_id=task.id,
                    agent_id=reviewer_id if semantic else "",
                    summary=("%s has been %s for %.0f minutes" % (task.id, task.state, age / 60.0)),
                    detail={
                        "age_seconds": age,
                        "threshold_seconds": self.config.reviewing_stuck_seconds,
                        "pending_reviewer_agent_id": reviewer_id,
                        "semantic_reviewer": semantic,
                    },
                    recommended_action="hold_agent" if semantic else "stop_task",
                )
            )
        return findings

    def _check_semantic_reviewer_still_assigned(self) -> List[Finding]:
        findings: List[Finding] = []
        for task in self._lifecycle_tasks():
            if str(getattr(task, "state", "") or "") not in _ACTIVE_REVIEW_STATES:
                continue
            for review in self.control_plane.list_reviews(task.id):
                if str(getattr(review, "status", "") or "").lower() != "pending":
                    continue
                reviewer_id = str(getattr(review, "reviewer_agent_id", "") or "")
                if not reviewer_id or self._agent_is_virtual(reviewer_id):
                    continue
                findings.append(
                    Finding(
                        kind="semantic_reviewer_still_assigned",
                        task_id=task.id,
                        agent_id=reviewer_id,
                        summary=(
                            "%s still has a pending semantic review on %s" % (task.id, reviewer_id)
                        ),
                        detail={"review_id": getattr(review, "id", "")},
                        recommended_action="stop_task",
                    )
                )
        return findings

    def _check_excessive_reviewing_population(self) -> List[Finding]:
        tasks = self._all_known_tasks()
        live = [
            task for task in tasks if str(getattr(task, "state", "") or "") in _NONTERMINAL_STATES
        ]
        reviewing = [
            task for task in live if str(getattr(task, "state", "") or "") in _ACTIVE_REVIEW_STATES
        ]
        if not live:
            return []
        fraction = len(reviewing) / float(len(live))
        if (
            len(reviewing) < self.config.excessive_reviewing_count
            and fraction < self.config.excessive_reviewing_fraction
        ):
            return []
        return [
            Finding(
                kind="excessive_reviewing_population",
                summary=(
                    "%d of %d live tasks are in review (%.0f%%)"
                    % (len(reviewing), len(live), fraction * 100.0)
                ),
                detail={
                    "reviewing_count": len(reviewing),
                    "live_count": len(live),
                    "fraction": fraction,
                    "task_ids": [task.id for task in reviewing[:50]],
                },
                recommended_action="fleet_stop",
            )
        ]

    def _check_too_many_gates(self) -> List[Finding]:
        findings: List[Finding] = []
        for task in self._lifecycle_tasks():
            reviews = list(self.control_plane.list_reviews(task.id))
            rejections = [
                review
                for review in reviews
                if str(getattr(review, "status", "") or "").lower() in {"rejected", "retracted"}
            ]
            if len(rejections) < self.config.too_many_gates:
                continue
            findings.append(
                Finding(
                    kind="too_many_gates",
                    task_id=task.id,
                    summary=(
                        "%s collected %d review/contract gates on one attempt"
                        % (task.id, len(rejections))
                    ),
                    detail={
                        "gate_count": len(rejections),
                        "threshold": self.config.too_many_gates,
                    },
                    recommended_action="stop_task",
                )
            )
        return findings

    def _check_orphaned_pull_requests(self) -> List[Finding]:
        """Open PRs whose tasks died in review, already landed, or were copied.

        Observed 2026-08-23: 56 open PRs against main, zero review
        decisions. Agents pushed good work, the semantic reviewer never
        published it, and the next attempt opened another PR. Some of
        that work later landed under a different number; the earlier
        PRs stayed open.
        """
        try:
            listing = self._pr_lister(self._repo_root())
        except Exception as exc:  # noqa: BLE001 - missing gh must not blind other checks
            _log.info("judgement could not list pull requests: %s", exc)
            return []
        if not isinstance(listing, Mapping):
            return []
        open_prs = [pr for pr in (listing.get("open") or []) if isinstance(pr, Mapping)]
        merged_prs = [pr for pr in (listing.get("merged") or []) if isinstance(pr, Mapping)]
        if not open_prs:
            return []
        merged_task_ids = set()
        for pr in merged_prs:
            merged_task_ids |= _task_ids_from_text(
                " ".join(
                    [
                        str(pr.get("title") or ""),
                        str(pr.get("body") or ""),
                        str(pr.get("headRefName") or ""),
                    ]
                )
            )
        findings: List[Finding] = []
        by_task: Dict[str, List[Mapping[str, Any]]] = {}
        for pr in open_prs:
            ids = _task_ids_from_text(
                " ".join(
                    [
                        str(pr.get("title") or ""),
                        str(pr.get("body") or ""),
                        str(pr.get("headRefName") or ""),
                    ]
                )
            )
            number = int(pr.get("number") or 0)
            if number <= 0:
                continue
            for raw_id in ids:
                task = self._resolve_task(raw_id)
                task_id = str(getattr(task, "id", "") or raw_id)
                by_task.setdefault(task_id, []).append(pr)
                state = str(getattr(task, "state", "") or "").lower()
                already_merged = bool(
                    raw_id in merged_task_ids
                    or task_id in merged_task_ids
                    or any(
                        raw_id.startswith(merged) or merged.startswith(raw_id)
                        for merged in merged_task_ids
                    )
                )
                if already_merged or state in _ORPHAN_PR_STATES:
                    findings.append(
                        Finding(
                            kind="orphaned_pull_request",
                            task_id=task_id,
                            summary=(
                                "PR #%d is still open after %s %s"
                                % (
                                    number,
                                    task_id,
                                    "already landed" if already_merged else state or "ended",
                                )
                            ),
                            detail={
                                "pr_number": number,
                                "url": pr.get("url") or "",
                                "task_state": state,
                                "already_merged": already_merged,
                            },
                            recommended_action="close_pr",
                        )
                    )
                    continue
                if state in _UNLANDED_PR_STATES:
                    findings.append(
                        Finding(
                            kind="unlanded_pull_request",
                            task_id=task_id,
                            summary=("PR #%d never landed; %s is %s" % (number, task_id, state)),
                            detail={
                                "pr_number": number,
                                "url": pr.get("url") or "",
                                "task_state": state,
                                "mergeable": pr.get("mergeable"),
                            },
                            recommended_action=(
                                "stop_task"
                                if state in (_ACTIVE_REVIEW_STATES | {"blocked"})
                                else ""
                            ),
                        )
                    )
        for task_id, prs in by_task.items():
            if len(prs) < 2:
                continue
            newest = max(int(pr.get("number") or 0) for pr in prs)
            for pr in prs:
                number = int(pr.get("number") or 0)
                if number <= 0 or number == newest:
                    continue
                findings.append(
                    Finding(
                        kind="duplicate_pull_request",
                        task_id=task_id,
                        summary=("PR #%d duplicates #%d for %s" % (number, newest, task_id)),
                        detail={
                            "pr_number": number,
                            "kept_pr_number": newest,
                            "url": pr.get("url") or "",
                        },
                        recommended_action="close_pr",
                    )
                )
        return findings

    # -- act ----------------------------------------------------------------

    def _act_on_findings(
        self,
        findings: Sequence[Finding],
        *,
        actor: str,
        run_id: str,
    ) -> List[Dict[str, Any]]:
        actions: List[Dict[str, Any]] = []
        budget = self.config.max_actions_per_cycle
        fleet_stopped = False
        for finding in findings:
            if len(actions) >= budget:
                actions.append(
                    {
                        "action": "skipped",
                        "reason": "cycle_budget",
                        "finding_kind": finding.kind,
                        "task_id": finding.task_id,
                    }
                )
                continue
            recommended = finding.recommended_action
            if recommended == "close_pr":
                actions.append(self._close_pull_request(actor=actor, finding=finding))
                continue
            if recommended == "fleet_stop":
                if fleet_stopped:
                    continue
                actions.append(self._fleet_stop(actor=actor, run_id=run_id, finding=finding))
                fleet_stopped = True
                continue
            if recommended == "hold_agent" and finding.agent_id:
                actions.append(self._hold_agent(finding.agent_id, actor=actor, finding=finding))
                continue
            if finding.task_id:
                actions.append(self._stop_task(finding.task_id, actor=actor, finding=finding))
                if (
                    finding.kind == "semantic_reviewer_still_assigned"
                    and not fleet_stopped
                    and self._semantic_assignment_count(findings) >= 3
                ):
                    actions.append(self._fleet_stop(actor=actor, run_id=run_id, finding=finding))
                    actions.append(self._redeploy(actor=actor, run_id=run_id, finding=finding))
                    actions.append(self._fleet_start(actor=actor, run_id=run_id, finding=finding))
                    fleet_stopped = True
        return actions

    def _close_pull_request(self, *, actor: str, finding: Finding) -> Dict[str, Any]:
        number = int((finding.detail or {}).get("pr_number") or 0)
        if number <= 0:
            return {
                "action": "skipped",
                "reason": "pr_number_missing",
                "finding_kind": finding.kind,
            }
        comment = (
            "Closed by hub judgement (%s). %s "
            "The semantic reviewer is gone; do not open another PR for "
            "the same task unless hub-verify can still land the branch."
            % (finding.kind, finding.summary)
        )
        try:
            result = self._pr_closer(number, comment, self._repo_root())
        except Exception as exc:  # noqa: BLE001
            return {
                "action": "error",
                "finding_kind": finding.kind,
                "pr_number": number,
                "error": str(exc)[:200],
            }
        self._observe(
            "judgement.pr_closed",
            "warning",
            {
                "pr_number": number,
                "kind": finding.kind,
                "task_id": finding.task_id,
                "actor": actor,
                "result": result,
            },
        )
        return {
            "action": "pr_closed",
            "finding_kind": finding.kind,
            "pr_number": number,
            "task_id": finding.task_id,
            "result": result,
        }

    def _stop_task(self, task_id: str, *, actor: str, finding: Finding) -> Dict[str, Any]:
        reason = "%s%s" % (HOLD_REASON_PREFIX, finding.kind)
        try:
            task = self.control_plane.get_task(task_id)
            if str(getattr(task, "state", "") or "") == "stopped":
                return {
                    "action": "already_stopped",
                    "task_id": task_id,
                    "finding_kind": finding.kind,
                }
            self.control_plane.stop_task(task_id, actor=actor, reason=reason)
        except Exception as exc:  # noqa: BLE001 - one intervention must not abort the cycle
            return {
                "action": "error",
                "task_id": task_id,
                "finding_kind": finding.kind,
                "error": str(exc)[:200],
            }
        self._observe(
            "judgement.task_stopped",
            "warning",
            {"task_id": task_id, "kind": finding.kind, "reason": reason},
        )
        return {
            "action": "task_stopped",
            "task_id": task_id,
            "finding_kind": finding.kind,
            "reason": reason,
        }

    def _hold_agent(self, agent_id: str, *, actor: str, finding: Finding) -> Dict[str, Any]:
        reason = "%s%s" % (HOLD_REASON_PREFIX, finding.kind)
        try:
            if self._agent_is_virtual(agent_id):
                return {
                    "action": "skipped",
                    "reason": "virtual_agent",
                    "agent_id": agent_id,
                    "finding_kind": finding.kind,
                }
            self.control_plane.set_agent_dispatch_hold(agent_id, reason)
        except Exception as exc:  # noqa: BLE001
            return {
                "action": "error",
                "agent_id": agent_id,
                "finding_kind": finding.kind,
                "error": str(exc)[:200],
            }
        self._observe(
            "judgement.agent_held",
            "warning",
            {"agent_id": agent_id, "kind": finding.kind, "reason": reason, "actor": actor},
        )
        return {
            "action": "agent_held",
            "agent_id": agent_id,
            "finding_kind": finding.kind,
            "reason": reason,
        }

    def _fleet_stop(self, *, actor: str, run_id: str, finding: Finding) -> Dict[str, Any]:
        held: List[str] = []
        stopped: List[str] = []
        paused: List[str] = []
        reason = "%sfleet_stop:%s" % (HOLD_REASON_PREFIX, run_id)
        for agent in self.control_plane.list_agents():
            if self._agent_is_virtual(agent.id):
                continue
            if bool(getattr(agent, "dispatch_hold", False)):
                continue
            try:
                self.control_plane.set_agent_dispatch_hold(agent.id, reason)
                held.append(agent.id)
            except Exception:  # noqa: BLE001
                continue
        for task in self._lifecycle_tasks():
            if str(getattr(task, "state", "") or "") not in _IN_FLIGHT_STATES:
                continue
            try:
                self.control_plane.stop_task(task.id, actor=actor, reason=reason)
                stopped.append(task.id)
            except Exception:  # noqa: BLE001
                continue
        for project in self._registered_projects():
            try:
                self.control_plane.set_project_dispatch(project, paused=True, actor=actor)
                paused.append(project)
            except Exception:  # noqa: BLE001
                continue
        self._observe(
            "judgement.fleet_stopped",
            "error",
            {
                "run_id": run_id,
                "kind": finding.kind,
                "held_agents": held,
                "stopped_tasks": stopped,
                "paused_projects": paused,
            },
        )
        return {
            "action": "fleet_stopped",
            "finding_kind": finding.kind,
            "held_agents": held,
            "stopped_tasks": stopped,
            "paused_projects": paused,
            "reason": reason,
        }

    def _fleet_start(self, *, actor: str, run_id: str, finding: Finding) -> Dict[str, Any]:
        resumed: List[str] = []
        activated: List[str] = []
        for agent in self.control_plane.list_agents():
            reason = str(getattr(agent, "dispatch_hold_reason", "") or "")
            if not reason.startswith(HOLD_REASON_PREFIX):
                continue
            try:
                self.control_plane.clear_agent_dispatch_hold(agent.id)
                resumed.append(agent.id)
            except Exception:  # noqa: BLE001
                continue
        for project in self._registered_projects():
            try:
                self.control_plane.set_project_dispatch(project, paused=False, actor=actor)
                activated.append(project)
            except Exception:  # noqa: BLE001
                continue
        self._observe(
            "judgement.fleet_started",
            "info",
            {
                "run_id": run_id,
                "kind": finding.kind,
                "resumed_agents": resumed,
                "activated_projects": activated,
            },
        )
        return {
            "action": "fleet_started",
            "finding_kind": finding.kind,
            "resumed_agents": resumed,
            "activated_projects": activated,
        }

    def _redeploy(self, *, actor: str, run_id: str, finding: Finding) -> Dict[str, Any]:
        now = self._now()
        recent = [
            stamp
            for stamp in self._redeploy_times
            if (now - stamp).total_seconds() < 24 * 60 * 60.0
        ]
        self._redeploy_times = recent
        if len(recent) >= self.config.max_redeploys_per_day:
            return {
                "action": "skipped",
                "reason": "redeploy_daily_budget",
                "finding_kind": finding.kind,
            }
        repo_root = self._repo_root()
        command = self._redeploy_command(repo_root)
        if not command:
            return {
                "action": "skipped",
                "reason": "redeploy_command_missing",
                "finding_kind": finding.kind,
                "repo_root": repo_root,
            }
        try:
            result = self._redeploy_runner(command, repo_root)
        except Exception as exc:  # noqa: BLE001
            return {
                "action": "error",
                "finding_kind": finding.kind,
                "error": str(exc)[:200],
            }
        self._redeploy_times.append(now)
        self._observe(
            "judgement.redeployed",
            "error",
            {
                "run_id": run_id,
                "kind": finding.kind,
                "command": list(command),
                "repo_root": repo_root,
                "actor": actor,
                "result": result,
            },
        )
        return {
            "action": "redeployed",
            "finding_kind": finding.kind,
            "command": list(command),
            "repo_root": repo_root,
            "result": result,
        }

    # -- helpers ------------------------------------------------------------

    def _lifecycle_tasks(self) -> List[Any]:
        return list(self.control_plane.list_tasks(state=list(_NONTERMINAL_STATES | {"failed"})))

    def _all_known_tasks(self) -> List[Any]:
        return list(self.control_plane.list_tasks())

    def _registered_projects(self) -> List[str]:
        try:
            projects = self.control_plane.list_projects()
        except Exception:  # noqa: BLE001
            return []
        names: List[str] = []
        for project in projects:
            name = getattr(project, "name", None) or (
                project.get("name") if isinstance(project, Mapping) else None
            )
            if name:
                names.append(str(name))
        return names

    def _agent_is_virtual(self, agent_id: str) -> bool:
        if not agent_id:
            return False
        checker = getattr(self.control_plane, "_agent_is_virtual", None)
        if callable(checker):
            try:
                return bool(checker(agent_id))
            except Exception:  # noqa: BLE001
                return False
        try:
            agent = self.control_plane.get_agent(agent_id)
        except Exception:  # noqa: BLE001
            return False
        resources = getattr(agent, "resources", None) or {}
        return bool(isinstance(resources, Mapping) and resources.get("virtual"))

    def _resolve_task(self, task_id: str) -> Any:
        raw = str(task_id or "").strip()
        if not raw:
            return None
        getter = getattr(self.control_plane, "get_task", None)
        if callable(getter):
            for candidate in (raw, raw[:13] if len(raw) > 13 else ""):
                if not candidate:
                    continue
                try:
                    return getter(candidate)
                except Exception:  # noqa: BLE001
                    continue
        prefix = raw if len(raw) <= 13 else raw[:13]
        for task in self._all_known_tasks():
            ident = str(getattr(task, "id", "") or "")
            if ident == raw or ident.startswith(prefix) or raw.startswith(ident):
                return task
        return None

    def _semantic_assignment_count(self, findings: Sequence[Finding]) -> int:
        return sum(1 for finding in findings if finding.kind == "semantic_reviewer_still_assigned")

    def _repo_root(self) -> str:
        if self.config.repo_root:
            return self.config.repo_root
        here = Path(__file__).resolve()
        for parent in here.parents:
            if (parent / SKILL_RELATIVE_PATH).is_file():
                return str(parent)
        return str(Path.cwd())

    def _redeploy_command(self, repo_root: str) -> List[str]:
        if self.config.redeploy_command:
            return [self.config.redeploy_command]
        script = Path(repo_root) / "deploy" / "deploy-mac-fleet.sh"
        if script.is_file():
            return [str(script)]
        return []

    def _skill_status(self) -> Dict[str, Any]:
        path = Path(self._repo_root()) / SKILL_RELATIVE_PATH
        if not path.is_file():
            return {"path": str(path), "present": False}
        text = path.read_text(encoding="utf-8")
        kinds = [
            "review_rejection_loop",
            "high_token_without_publication",
            "failed_dependency_deadlock",
            "stuck_reviewing",
            "semantic_reviewer_still_assigned",
            "excessive_reviewing_population",
            "too_many_gates",
            "orphaned_pull_request",
            "duplicate_pull_request",
            "unlanded_pull_request",
        ]
        missing = [kind for kind in kinds if kind not in text]
        return {
            "path": str(path),
            "present": True,
            "bytes": len(text.encode("utf-8")),
            "checklist_kinds": kinds,
            "missing_kinds": missing,
        }

    def _observe(self, event_type: str, level: str, detail: Dict[str, Any]) -> None:
        try:
            self.control_plane.record_log(
                event_type,
                level=level,
                layer="control_plane",
                source="judgement",
                subject_type="service",
                subject_id="judgement",
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - telemetry must never break the cycle
            _log.debug("judgement observability write failed", exc_info=True)


def _task_ids_from_text(text: str) -> set[str]:
    return set(_TASK_ID_RE.findall(text or ""))


def _default_pr_lister(repo_root: str) -> Dict[str, Any]:
    def _run(state: str, limit: int) -> List[Dict[str, Any]]:
        completed = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--base",
                "main",
                "--state",
                state,
                "--limit",
                str(limit),
                "--json",
                "number,title,body,headRefName,url,mergeable",
            ],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "gh pr list failed")[:300])
        payload = json.loads(completed.stdout or "[]")
        return [item for item in payload if isinstance(item, dict)]

    return {"open": _run("open", 100), "merged": _run("merged", 50)}


def _default_pr_closer(number: int, comment: str, repo_root: str) -> Dict[str, Any]:
    completed = subprocess.run(
        [
            "gh",
            "pr",
            "close",
            str(int(number)),
            "--comment",
            comment,
        ],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return {
        "returncode": int(completed.returncode),
        "stdout_excerpt": (completed.stdout or "")[-300:],
        "stderr_excerpt": (completed.stderr or "")[-300:],
    }


def _default_redeploy_runner(command: Sequence[str], repo_root: str) -> Dict[str, Any]:
    completed = subprocess.run(
        list(command),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30 * 60,
    )
    return {
        "returncode": int(completed.returncode),
        "stdout_excerpt": (completed.stdout or "")[-400:],
        "stderr_excerpt": (completed.stderr or "")[-400:],
    }


__all__ = [
    "JUDGEMENT_SCHEMA",
    "JUDGEMENT_ACTOR",
    "HOLD_REASON_PREFIX",
    "JudgementConfig",
    "JudgementProcess",
    "Finding",
]
