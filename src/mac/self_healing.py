"""Self-healing sentinel: the hub's own observe → plan → act → verify loop.

MAC's autonomy machinery acts inside domains (dispatch retries leases, the
optimizer tunes policy, the groomer manufactures backlog), but nothing watched
the SYSTEM: a dead nap scheduler, a starved project, an enabled-but-silent
daemon, and a never-exercised learning read-path all sat plainly in the hub's
own tables for weeks until a human went looking. Detection existed as data;
nothing consumed it.

This sentinel closes that loop:

* OBSERVE — each cycle it evaluates concrete invariants against the ledger
  and observability streams (nap liveness, open-task starvation, daemon
  heartbeat freshness, learning read-path silence).
* PLAN/ACT — a violated invariant becomes a filed fleet task (the fleet is
  the actuator; tasks are the plan), deduped by finding fingerprint so a
  standing problem yields one task, not a storm. Findings that need a
  specific host are pinned via ``metadata.target_agent_id``.
* VERIFY — a fingerprint whose previous fix task COMPLETED but whose
  invariant is violated again is re-filed with an incremented attempt and
  the prior task referenced ("the fix did not hold — roll back or re-plan").
  After ``max_attempts`` the sentinel stops filing and raises an operator
  notification instead: autonomy escalates to a human only after it has
  demonstrably failed N times, not before it has tried.

No-op unless ``MAC_SELF_HEAL_ENABLED`` is set.
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

SELF_HEAL_SCHEMA = "mac.self_healing.v1"
SELF_HEAL_ORIGIN_TYPE = "self_heal"

MIN_INTERVAL_SECONDS = 300.0
DEFAULT_INTERVAL_SECONDS = 1800.0
DEFAULT_INITIAL_DELAY_SECONDS = 180.0
DEFAULT_STARVATION_SECONDS = 7 * 24 * 60 * 60.0
DEFAULT_NAP_STALL_SECONDS = 12 * 60 * 60.0
DEFAULT_READ_SILENCE_SECONDS = 24 * 60 * 60.0
# The repo-update sweep's publish->apply spread runs up to ~25 minutes on a
# healthy fleet; hours of lag means a wedged agentbus consumer (the failure
# that left one worker three weeks stale while heartbeating "healthy").
DEFAULT_PIN_DIVERGENCE_SECONDS = 3 * 60 * 60.0
DEFAULT_AGENT_SILENCE_SECONDS = 60 * 60.0
DEFAULT_MAX_ATTEMPTS = 3

_log = logging.getLogger("mac.self_healing")

_ACTIVE_STATES = frozenset(
    {"open", "waiting", "blocked", "claimed", "running", "needs_review", "reviewing"}
)

# Daemon heartbeat observability names → the env flag that enables each.
# A daemon that is enabled but has not emitted its run event within
# 3x its expected interval is silently dead — precisely the failure mode
# that took the nap pipeline down without anyone noticing.
_DAEMON_HEARTBEATS = (
    ("nap.tick.run", "MAC_NAP_TICK_ENABLED", "MAC_NAP_TICK_INTERVAL_SECONDS", 900.0),
    ("backlog.groom.run", "MAC_BACKLOG_GROOM_ENABLED", "MAC_BACKLOG_GROOM_INTERVAL_SECONDS", 900.0),
    ("curiosity.review.run", "MAC_CURIOSITY_REVIEW_ENABLED",
     "MAC_CURIOSITY_REVIEW_INTERVAL_SECONDS", 6 * 60 * 60.0),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SelfHealingConfig:
    enabled: bool = False
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS
    starvation_seconds: float = DEFAULT_STARVATION_SECONDS
    nap_stall_seconds: float = DEFAULT_NAP_STALL_SECONDS
    read_silence_seconds: float = DEFAULT_READ_SILENCE_SECONDS
    pin_divergence_seconds: float = DEFAULT_PIN_DIVERGENCE_SECONDS
    agent_silence_seconds: float = DEFAULT_AGENT_SILENCE_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    configuration_error: str = ""

    @property
    def active(self) -> bool:
        return self.enabled and not self.configuration_error

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "active": self.active}

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "SelfHealingConfig":
        env = os.environ if environ is None else environ
        errors: List[str] = []
        enabled = str(env.get("MAC_SELF_HEAL_ENABLED") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }

        def _num(name: str, default: float, low: float, high: float) -> float:
            return bounded_env_number(env, name, default, low, high, errors=errors)

        return cls(
            enabled=enabled,
            interval_seconds=_num("MAC_SELF_HEAL_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS,
                                  MIN_INTERVAL_SECONDS, 24 * 60 * 60.0),
            initial_delay_seconds=_num("MAC_SELF_HEAL_INITIAL_DELAY_SECONDS",
                                       DEFAULT_INITIAL_DELAY_SECONDS, 0.0, 60 * 60.0),
            starvation_seconds=_num("MAC_SELF_HEAL_STARVATION_SECONDS", DEFAULT_STARVATION_SECONDS,
                                    60 * 60.0, 90 * 24 * 60 * 60.0),
            nap_stall_seconds=_num("MAC_SELF_HEAL_NAP_STALL_SECONDS", DEFAULT_NAP_STALL_SECONDS,
                                   30 * 60.0, 30 * 24 * 60 * 60.0),
            read_silence_seconds=_num("MAC_SELF_HEAL_READ_SILENCE_SECONDS",
                                      DEFAULT_READ_SILENCE_SECONDS,
                                      30 * 60.0, 30 * 24 * 60 * 60.0),
            pin_divergence_seconds=_num("MAC_SELF_HEAL_PIN_DIVERGENCE_SECONDS",
                                        DEFAULT_PIN_DIVERGENCE_SECONDS,
                                        30 * 60.0, 30 * 24 * 60 * 60.0),
            agent_silence_seconds=_num("MAC_SELF_HEAL_AGENT_SILENCE_SECONDS",
                                       DEFAULT_AGENT_SILENCE_SECONDS,
                                       10 * 60.0, 7 * 24 * 60 * 60.0),
            max_attempts=int(_num("MAC_SELF_HEAL_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, 1, 10)),
            configuration_error="; ".join(errors),
        )


@dataclass
class Finding:
    fingerprint: str
    kind: str
    summary: str
    detail: Dict[str, Any]
    target_agent_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SelfHealingSentinel:
    """Evaluate system invariants and turn violations into fleet work."""

    def __init__(self, control_plane: Any, config: SelfHealingConfig,
                 environ: Optional[Mapping[str, str]] = None) -> None:
        self.control_plane = control_plane
        self.config = config
        self._environ = environ
        self._stop_event = threading.Event()
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last_report: Optional[Dict[str, Any]] = None
        # Fingerprints already escalated to operators; cleared when the
        # finding stops recurring so a NEW occurrence re-notifies, but a
        # STANDING exhausted finding doesn't page every cycle.
        self._escalated_fingerprints: set = set()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        if not self.config.active:
            if self.config.configuration_error:
                self._observe("self_heal.configuration_invalid", "warning",
                              {"error": self.config.configuration_error})
            return False
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            thread = threading.Thread(
                target=self._loop, name="mac-self-healing", daemon=True
            )
            self._thread = thread
            thread.start()
        self._observe("self_heal.started", "info", {"config": self.config.to_dict()})
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._observe("self_heal.stopped", "info", {})
        return stopped

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            thread = self._thread
            last_report = copy.deepcopy(self._last_report)
        return {
            "schema": SELF_HEAL_SCHEMA,
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
            except Exception:  # noqa: BLE001 - the next cycle must still run.
                _log.warning("self-heal cycle failed", exc_info=True)
            if self._stop_event.wait(max(0.01, self.config.interval_seconds)):
                return

    # -- observe ------------------------------------------------------------

    def run_once(self, *, actor: str = "self-healing-sentinel",
                 trigger: str = "operator") -> Dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {"schema": SELF_HEAL_SCHEMA, "status": "busy",
                    "trigger": trigger, "findings": []}
        run_id = "heal_%s" % uuid.uuid4().hex
        findings: List[Finding] = []
        check_errors: List[str] = []
        try:
            for check in (
                self._check_nap_liveness,
                self._check_task_starvation,
                self._check_daemon_heartbeats,
                self._check_read_path_silence,
                self._check_stuck_quarantine,
                self._check_fleet_pin_divergence,
                self._check_agent_unhealthy,
            ):
                try:
                    findings.extend(check())
                except Exception as exc:  # noqa: BLE001 - one broken check
                    # must not blind the others.
                    check_errors.append("%s: %s" % (check.__name__, str(exc)[:200]))
            # Re-arm escalations for findings that cleared: a fresh
            # occurrence later deserves a fresh operator notification.
            self._escalated_fingerprints &= {f.fingerprint for f in findings}
            actions = [self._act_on(finding, actor=actor) for finding in findings]
        finally:
            self._run_lock.release()
        report = {
            "schema": SELF_HEAL_SCHEMA,
            "run_id": run_id,
            "status": "ok",
            "trigger": trigger,
            "finding_count": len(findings),
            "filed_count": sum(1 for a in actions if a.get("action") == "task_filed"),
            "escalated_count": sum(1 for a in actions if a.get("action") == "escalated"),
            "findings": [
                {**finding.to_dict(), **action}
                for finding, action in zip(findings, actions)
            ],
            "check_errors": check_errors,
        }
        with self._state_lock:
            self._last_report = report
        level = "warning" if findings or check_errors else "info"
        self._observe("self_heal.run", level, report)
        return report

    def _check_nap_liveness(self) -> List[Finding]:
        schedules = [s for s in self.control_plane.list_nap_schedules() if s.enabled]
        if not schedules:
            return []
        newest: Optional[datetime] = None
        for run in self.control_plane.list_nap_runs():
            ts = _parse_ts(getattr(run, "started_at", None))
            if ts is not None and (newest is None or ts > newest):
                newest = ts
        age = None if newest is None else (_utcnow() - newest).total_seconds()
        if age is not None and age < self.config.nap_stall_seconds:
            return []
        return [Finding(
            fingerprint="nap_liveness",
            kind="nap_liveness",
            summary=(
                "%d agents have enabled nap schedules but no nap has run in %s"
                % (len(schedules),
                   "over %.0f hours" % (age / 3600.0) if age is not None else "recorded history")
            ),
            detail={
                "enabled_schedules": len(schedules),
                "newest_run_age_seconds": age,
                "threshold_seconds": self.config.nap_stall_seconds,
                "remediation": (
                    "Diagnose why nap cycles stopped: check the hub's nap-tick "
                    "status (GET /nap-tick/status), MAC_NAP_TICK_ENABLED in the "
                    "hub env, and whether any external nap scheduler this fleet "
                    "relied on still exists. Apply the fix and verify a new "
                    "nap_run row appears."
                ),
            },
        )]

    def _check_task_starvation(self) -> List[Finding]:
        findings: List[Finding] = []
        stale_by_project: Dict[str, List[Any]] = {}
        now = _utcnow()
        for task in self.control_plane.list_tasks(state="open"):
            metadata = getattr(task, "metadata", None) or {}
            if isinstance(metadata, Mapping) and metadata.get("no_dispatch"):
                continue  # deliberately staged work is not starvation
            ts = _parse_ts(getattr(task, "created_at", None))
            if ts is None or (now - ts).total_seconds() < self.config.starvation_seconds:
                continue
            project = str(getattr(task, "project", "") or "(none)")
            stale_by_project.setdefault(project, []).append(task)
        for project, tasks in sorted(stale_by_project.items()):
            oldest = min(
                (_parse_ts(getattr(t, "created_at", None)) or now for t in tasks),
            )
            task_ids = [getattr(t, "id", "?") for t in tasks[:5]]
            findings.append(Finding(
                fingerprint="task_starvation:%s" % project,
                kind="task_starvation",
                summary=(
                    "project %s has %d open task(s) older than %.0f days"
                    % (project, len(tasks), self.config.starvation_seconds / 86400.0)
                ),
                detail={
                    "project": project,
                    "stale_count": len(tasks),
                    "oldest_created_at": oldest.isoformat(),
                    "sample_task_ids": task_ids,
                    "remediation": (
                        "For each listed task run the dispatch explainability "
                        "surface (mac task why-unclaimed <id>), identify the "
                        "blocking gate (capabilities, project registration, "
                        "target mismatch, worker policy), and either fix the "
                        "gate, re-scope the task, or cancel it with a reason."
                    ),
                },
            ))
        return findings

    def _check_daemon_heartbeats(self) -> List[Finding]:
        env = os.environ if self._environ is None else self._environ
        findings: List[Finding] = []
        for event_name, enable_var, interval_var, default_interval in _DAEMON_HEARTBEATS:
            if str(env.get(enable_var) or "").strip().lower() not in {"1", "true", "yes", "on"}:
                continue
            try:
                interval = float(str(env.get(interval_var) or "").strip() or default_interval)
            except ValueError:
                interval = default_interval
            events = self.control_plane.list_observability(name=event_name, limit=1)
            newest = _parse_ts(getattr(events[0], "created_at", None)) if events else None
            age = None if newest is None else (_utcnow() - newest).total_seconds()
            grace = max(3 * interval, 15 * 60.0)
            if age is not None and age < grace:
                continue
            findings.append(Finding(
                fingerprint="daemon_silent:%s" % event_name,
                kind="daemon_silent",
                summary=(
                    "%s is enabled (%s) but its heartbeat event %r is %s"
                    % (enable_var, "set", event_name,
                       "absent" if age is None else "%.0f min stale" % (age / 60.0))
                ),
                detail={
                    "event_name": event_name,
                    "enable_var": enable_var,
                    "expected_interval_seconds": interval,
                    "age_seconds": age,
                    "remediation": (
                        "The daemon's thread has died or the hub is running "
                        "stale code. Check the hub process logs for the "
                        "daemon's exception, restart the control plane, and "
                        "verify the heartbeat event resumes."
                    ),
                },
            ))
        return findings

    def _check_read_path_silence(self) -> List[Finding]:
        openclaw_agents = []
        for agent in self.control_plane.list_agents():
            resources = getattr(agent, "resources", None) or {}
            gateway = resources.get("chat_gateway") if isinstance(resources, Mapping) else None
            if isinstance(gateway, Mapping) and gateway.get("implementation") == "openclaw" \
                    and gateway.get("verified"):
                openclaw_agents.append(agent)
        if not openclaw_agents:
            return []
        events = self.control_plane.list_observability(
            name="continuity.context_served", limit=1
        )
        newest = _parse_ts(getattr(events[0], "created_at", None)) if events else None
        age = None if newest is None else (_utcnow() - newest).total_seconds()
        if age is not None and age < self.config.read_silence_seconds:
            return []
        # Pin the diagnosis to one affected gateway host; its agent can probe
        # its own plugin env and sandbox, which the hub cannot see.
        target = str(getattr(openclaw_agents[0], "id", "") or "") or None
        return [Finding(
            fingerprint="read_path_silence:continuity",
            kind="read_path_silence",
            summary=(
                "%d verified OpenClaw gateway(s) exist but the continuity "
                "endpoint has served nothing in %s"
                % (len(openclaw_agents),
                   "over %.0f hours" % (age / 3600.0) if age is not None else "recorded history")
            ),
            detail={
                "verified_openclaw_agents": [
                    str(getattr(a, "id", "?")) for a in openclaw_agents
                ],
                "age_seconds": age,
                "remediation": (
                    "From the gateway host, verify the mac-continuity plugin "
                    "can reach the hub: check MAC_OPENCLAW_AGENT_ID / "
                    "MAC_OPENCLAW_CONTROL_URL / MAC_OPENCLAW_ROUTER_API_KEY "
                    "inside the sandbox, call GET /v1/agents/<id>/continuity "
                    "with the bound token, and inspect gateway logs for "
                    "'mac-continuity: context lookup skipped'. Fix the wiring "
                    "and confirm a continuity.context_served event appears."
                ),
            },
            target_agent_id=target,
        )]

    def _check_stuck_quarantine(self) -> List[Finding]:
        """Auto-quarantine is one-way: a hold is set on zombie signals but
        nothing ever re-verifies the agent or lifts the hold. Catch holds
        that have outlived the starvation threshold."""
        findings: List[Finding] = []
        now = _utcnow()
        for agent in self.control_plane.list_agents():
            if not getattr(agent, "dispatch_hold", False):
                continue
            reason = str(getattr(agent, "dispatch_hold_reason", "") or "")
            if not reason.startswith("auto_quarantine"):
                continue  # operator-set holds are deliberate; leave them alone
            held_at = _parse_ts(getattr(agent, "dispatch_hold_at", None))
            age = None if held_at is None else (now - held_at).total_seconds()
            if age is not None and age < self.config.starvation_seconds:
                continue
            agent_id = str(getattr(agent, "id", "") or "")
            findings.append(Finding(
                fingerprint="stuck_quarantine:%s" % agent_id,
                kind="stuck_quarantine",
                summary=(
                    "agent %s has been auto-quarantined for %s with no re-verification"
                    % (agent_id,
                       "%.0f days" % (age / 86400.0) if age is not None else "an unknown time")
                ),
                detail={
                    "agent_id": agent_id,
                    "hold_reason": reason,
                    "held_at": getattr(agent, "dispatch_hold_at", None),
                    "remediation": (
                        "Diagnose the quarantined agent (host reachable? worker "
                        "process healthy? source checkout importable?). If it is "
                        "healthy, clear the dispatch hold and verify it claims and "
                        "completes a task with telemetry. If it is not, fix the "
                        "host or retire the agent registration."
                    ),
                },
                # Deliberately unpinned: the held agent cannot claim work.
            ))
        return findings

    _REPO_UPDATE_STATUSES = (
        "updated", "no_update", "skipped", "deferred", "rolled_back", "error",
    )

    def _check_fleet_pin_divergence(self) -> List[Finding]:
        """A heartbeating agent whose repo-update trail lags the fleet's
        newest application is running stale code with a wedged agentbus
        consumer — the failure that left a worker three weeks behind while
        reporting healthy. Silent/held agents are the unhealthy check's job."""
        latest: Dict[str, datetime] = {}
        for status in self._REPO_UPDATE_STATUSES:
            events = self.control_plane.list_observability(
                name="worker.agentbus.repo_update.%s" % status, limit=200
            )
            for event in events:
                source = str(getattr(event, "source", "") or "")
                ts = _parse_ts(getattr(event, "created_at", None))
                if source and ts and (source not in latest or ts > latest[source]):
                    latest[source] = ts
        if not latest:
            return []
        fleet_newest = max(latest.values())
        now = _utcnow()
        if (now - fleet_newest).total_seconds() < self.config.pin_divergence_seconds:
            # The newest sweep is still inside the divergence window; agents
            # that haven't consumed it yet are not diverged, just in-flight.
            return []
        findings: List[Finding] = []
        for agent in self.control_plane.list_agents():
            agent_id = str(getattr(agent, "id", "") or "")
            if getattr(agent, "dispatch_hold", False):
                continue
            last_seen = _parse_ts(getattr(agent, "last_seen_at", None))
            if last_seen is None or (now - last_seen).total_seconds() > self.config.agent_silence_seconds:
                continue  # not heartbeating -> _check_agent_unhealthy owns it
            agent_latest = latest.get(agent_id)
            lag = (
                (fleet_newest - agent_latest).total_seconds()
                if agent_latest is not None
                else None
            )
            if lag is not None and lag < self.config.pin_divergence_seconds:
                continue
            findings.append(Finding(
                fingerprint="fleet_pin_divergence:%s" % agent_id,
                kind="fleet_pin_divergence",
                summary=(
                    "agent %s heartbeats but its repo-update trail %s the fleet's newest application"
                    % (agent_id,
                       "lags %.1f hours behind" % (lag / 3600.0) if lag is not None
                       else "is absent entirely from")
                ),
                detail={
                    "agent_id": agent_id,
                    "lag_seconds": lag,
                    "fleet_newest_update": fleet_newest.isoformat(),
                    "agent_latest_update": (
                        agent_latest.isoformat() if agent_latest else None
                    ),
                    "remediation": (
                        "The agent's agentbus control consumer is likely wedged: "
                        "it heartbeats and claims work but never processes "
                        "repo-update streams, so it runs stale code "
                        "indefinitely. On its host, check the worker log for "
                        "control-stream errors, run git -C ~/.mac/src/mac "
                        "log -1 to confirm the stale pin, then git pull "
                        "--ff-only to the FLEET pin (not past it) and restart "
                        "the worker service. Verify a fresh "
                        "worker.agentbus.repo_update event appears."
                    ),
                },
            ))
        return findings

    def _check_agent_unhealthy(self) -> List[Finding]:
        """An agent that stopped heartbeating, or heartbeats while reporting
        degraded/offline (a crash-looping worker registers on each boot),
        is invisible to dispatch's defensive filters — they skip it, nothing
        restores it."""
        findings: List[Finding] = []
        now = _utcnow()
        for agent in self.control_plane.list_agents():
            if getattr(agent, "dispatch_hold", False):
                continue  # deliberately benched; stuck_quarantine covers auto-holds
            agent_id = str(getattr(agent, "id", "") or "")
            last_seen = _parse_ts(getattr(agent, "last_seen_at", None))
            age = None if last_seen is None else (now - last_seen).total_seconds()
            stale = age is None or age > self.config.agent_silence_seconds
            health = str(getattr(agent, "health_status", "") or "")
            status = str(getattr(agent, "status", "") or "")
            sick = health not in ("", "healthy") or status == "offline"
            if not (stale or sick):
                continue
            symptom = (
                "silent for %.0f minutes" % (age / 60.0)
                if stale and age is not None
                else "never seen" if stale
                else "heartbeating but %s/%s" % (health or "?", status or "?")
            )
            findings.append(Finding(
                fingerprint="agent_unhealthy:%s" % agent_id,
                kind="agent_unhealthy",
                summary="agent %s is %s" % (agent_id, symptom),
                detail={
                    "agent_id": agent_id,
                    "last_seen_age_seconds": age,
                    "health_status": health,
                    "status": status,
                    "remediation": (
                        "Diagnose the agent host: is the worker service "
                        "running (systemd/launchd/supervisord)? A crash-loop "
                        "shows repeated registrations with degraded/offline "
                        "state — read the service's startup/self-test log for "
                        "the failing check. Fix the host or, if the agent is "
                        "intentionally retired, remove its registration or "
                        "set a dispatch hold with a reason."
                    ),
                },
            ))
        return findings

    # -- plan / act / verify -------------------------------------------------

    def _act_on(self, finding: Finding, *, actor: str) -> Dict[str, Any]:
        active_task, completed_attempts, newest_completed = self._history_for(
            finding.fingerprint
        )
        if active_task is not None:
            return {"action": "in_progress", "task_id": getattr(active_task, "id", None)}
        attempt = completed_attempts + 1
        if attempt > self.config.max_attempts:
            # Autonomy has failed max_attempts times; only now involve
            # humans — and only once per standing occurrence, not per cycle.
            if finding.fingerprint not in self._escalated_fingerprints:
                self._escalated_fingerprints.add(finding.fingerprint)
                self._notify_escalation(finding, completed_attempts)
                return {"action": "escalated", "attempts": completed_attempts}
            return {"action": "escalated_previously", "attempts": completed_attempts}
        try:
            task = self._file_fix_task(
                finding, attempt=attempt, actor=actor,
                prior_task=newest_completed,
            )
        except Exception as exc:  # noqa: BLE001 - isolate one finding's failure.
            return {"action": "error", "error": str(exc)[:500]}
        return {"action": "task_filed", "task_id": getattr(task, "id", None),
                "attempt": attempt}

    def _history_for(self, fingerprint: str):
        """Return (active fix task, completed attempt count, newest completed)."""
        active = None
        completed: List[Any] = []
        for task in self.control_plane.list_tasks():
            metadata = getattr(task, "metadata", None) or {}
            if not isinstance(metadata, Mapping):
                continue
            origin = metadata.get("origin")
            if not (isinstance(origin, Mapping) and origin.get("type") == SELF_HEAL_ORIGIN_TYPE):
                continue
            if str(metadata.get("self_heal_fingerprint") or "") != fingerprint:
                continue
            state = str(getattr(task, "state", "") or "")
            if state in _ACTIVE_STATES:
                active = task
            elif state == "completed":
                completed.append(task)
        newest_completed = None
        if completed:
            newest_completed = max(
                completed,
                key=lambda t: _parse_ts(getattr(t, "created_at", None)) or _utcnow(),
            )
        return active, len(completed), newest_completed

    def _file_fix_task(self, finding: Finding, *, attempt: int, actor: str,
                       prior_task: Any = None) -> Any:
        title = "Self-heal: %s" % finding.summary
        if len(title) > 140:
            title = title[:137] + "..."
        verify_clause = (
            "\n\nVERIFY BEFORE CLOSING: after applying the fix, re-check the "
            "violated invariant from the detail above and state the evidence "
            "that it now holds. A closed task whose symptom recurs will be "
            "re-filed automatically with an incremented attempt count."
        )
        rollback_clause = ""
        if prior_task is not None:
            rollback_clause = (
                "\n\nATTEMPT %d — the previous fix (task %s) completed but the "
                "symptom RECURRED. Do not repeat that approach: read its "
                "evidence, decide whether its change should be rolled back, "
                "and re-plan from the new information."
                % (attempt, getattr(prior_task, "id", "?"))
            )
        description = (
            "Automated self-healing finding (%s).\n\nInvariant violated: %s\n\n"
            "Detail:\n%s%s%s"
            % (
                finding.kind,
                finding.summary,
                "\n".join("- %s: %s" % (k, v) for k, v in sorted(finding.detail.items())),
                rollback_clause,
                verify_clause,
            )
        )
        metadata: Dict[str, Any] = {
            "origin": {"type": SELF_HEAL_ORIGIN_TYPE, "kind": finding.kind},
            "self_heal_fingerprint": finding.fingerprint,
            "self_heal_attempt": attempt,
            "evidence_type": "investigation",
        }
        if finding.target_agent_id:
            metadata["target_agent_id"] = finding.target_agent_id
        if prior_task is not None:
            metadata["self_heal_prior_task_id"] = getattr(prior_task, "id", None)
        return self.control_plane.create_task(
            title,
            description=description,
            metadata=metadata,
            actor=actor,
        )

    def _notify_escalation(self, finding: Finding, attempts: int) -> None:
        try:
            self.control_plane.record_notification(
                "self_heal.escalated",
                "Self-healing exhausted for: %s" % finding.kind,
                (
                    "%s\n\n%d autonomous fix attempts completed without the "
                    "invariant holding. Human judgment needed."
                    % (finding.summary, attempts)
                ),
                subject_type="self_heal",
                subject_id=finding.fingerprint,
                channels=["dashboard", "hermes"],
                metadata={"finding": finding.to_dict(), "attempts": attempts},
            )
        except Exception:  # noqa: BLE001 - escalation must not kill the cycle.
            _log.warning("self-heal escalation notification failed", exc_info=True)

    # -- telemetry ----------------------------------------------------------

    def _observe(self, event_type: str, level: str, detail: Dict[str, Any]) -> None:
        try:
            self.control_plane.record_log(event_type, level=level, detail=detail)
        except Exception:  # noqa: BLE001 - telemetry must never break the cycle.
            _log.debug("self-heal observability write failed", exc_info=True)
