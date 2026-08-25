"""Close the curiosity quarantine loop with fleet-task adjudication.

The curiosity sidecar deliberately withholds the approve/reject verb from the
submitting agent — a candidate needs *external* judgment before it becomes
durable workspace memory. But nothing shipped ever exercised that verb, while
an install-time cron told every agent to submit a candidate every six hours:
submissions were mandatory and approval was impossible, so quarantines grew
monotonically.

This reviewer files ONE adjudication task per OpenClaw-gateway agent on a slow
cadence. The task is pinned to that agent via ``metadata.target_agent_id``
(the ledger lives on the agent's own host, inside its sandbox), and its prompt
directs the executor to list quarantined candidates and approve/reject each
with an actor, reason, and the task id as the external approval id — giving
every promotion an auditable trail in both the curiosity ledger and the MAC
task history. The submitting persona still cannot approve its own candidates
from chat; adjudication happens in the reviewed task-execution context.

No-op unless ``MAC_CURIOSITY_REVIEW_ENABLED`` is set.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Mapping, Optional

from mac.config_coercion import bounded_env_number, parse_timestamp as _parse_ts

CURIOSITY_REVIEWER_SCHEMA = "mac.curiosity_reviewer.v1"
ADJUDICATION_ORIGIN_TYPE = "curiosity_adjudication"

MIN_INTERVAL_SECONDS = 300.0
DEFAULT_INTERVAL_SECONDS = 6 * 60 * 60.0
DEFAULT_INITIAL_DELAY_SECONDS = 300.0
DEFAULT_COOLDOWN_SECONDS = 24 * 60 * 60.0
#: Filed tasks need a project so an approved one can resolve a publication
#: target and complete rather than parking in REVIEWING (task_ce6c8ea3).
DEFAULT_PROJECT = "mac"

_log = logging.getLogger("mac.curiosity_reviewer")

_ACTIVE_STATES = frozenset(
    {"open", "waiting", "blocked", "claimed", "running", "needs_review", "reviewing"}
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_adjudication_description(agent_name: str, task_ref: str) -> str:
    """Build the task description for adjudicating quarantined curiosity candidates."""
    return (
        "Adjudicate this host's quarantined curiosity candidates.\n\n"
        "Agent %(name)s accumulates evidence-linked learning hypotheses in a "
        "quarantine ledger. Candidates only become durable workspace memory "
        "after an explicit, audited decision — which is this task.\n\n"
        "USE THE HUB-MEDIATED VERBS (`mac admin curiosity ...`), NOT a local "
        "`curiosity` binary. The ledger lives inside the agent's OpenClaw "
        "sandbox; this task executes in a different mac-task-* sandbox that "
        "cannot reach it, which is why every adjudication task filed before "
        "2026-08-06 failed. `mac admin curiosity` reaches it through the hub and "
        "works from any sandbox.\n\n"
        "Steps:\n"
        "1. Run `mac admin curiosity list --status quarantined` and read each "
        "candidate in full: hypothesis, question, test, evidence, "
        "counterevidence, unknowns, confidence.\n"
        "2. For each candidate, decide conservatively:\n"
        "   - APPROVE only when the cited evidence genuinely supports the "
        "hypothesis, the proposed test is coherent, and the content contains "
        "no secrets, tokens, or personal data.\n"
        "   - REJECT when the hypothesis is unsupported, stale, duplicative, "
        "or unsafe to persist.\n"
        "   - LEAVE QUARANTINED (no decision) when genuinely uncertain — "
        "deferring is always acceptable; wrongly approved memory is not.\n"
        "3. Record each decision with the full audit trail:\n"
        "   `mac admin curiosity approve <id> --actor %(actor)s --reason "
        '"<specific reason>" --approval-id %(task)s`\n'
        "   (or `mac admin curiosity reject` with the same flags).\n"
        "4. Summarize the outcomes (approved / rejected / deferred counts and "
        "one-line reasons) in mac-evidence.json.\n\n"
        "Do NOT approve candidates wholesale; each decision needs its own "
        "reason grounded in that candidate's evidence."
    ) % {"name": agent_name, "actor": "fleet_reviewer", "task": task_ref}


@dataclass(frozen=True)
class CuriosityReviewerConfig:
    enabled: bool = False
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    #: Project the filed task belongs to. A task with no project resolves no
    #: publication target, so an approved one parks in REVIEWING instead of
    #: completing (task_ce6c8ea3). Configurable because the destination is a
    #: fleet decision, not a property of curiosity.
    project: str = DEFAULT_PROJECT
    #: Agents whose ledger the hub can actually serve. `mac admin curiosity` proxies
    #: the hub's HOST-LOCAL wrapper, so a task filed for any other agent would
    #: adjudicate this host's candidates under that agent's name -- silently
    #: wrong, which is worse than the old outright failure.
    #:
    #: Today this is exactly the hub's own agent. It is a set, not a single id,
    #: because it grows the moment the hub can route to another agent's host,
    #: and the per-agent dedupe/cooldown logic below is already written for
    #: more than one.
    servable_agent_ids: FrozenSet[str] = frozenset()
    configuration_error: str = ""

    @property
    def active(self) -> bool:
        return self.enabled and not self.configuration_error

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "active": self.active}

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "CuriosityReviewerConfig":
        env = os.environ if environ is None else environ
        errors: List[str] = []
        enabled = str(env.get("MAC_CURIOSITY_REVIEW_ENABLED") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        def _num(name: str, default: float, low: float, high: float) -> float:
            return bounded_env_number(env, name, default, low, high, errors=errors)

        interval = _num(
            "MAC_CURIOSITY_REVIEW_INTERVAL_SECONDS",
            DEFAULT_INTERVAL_SECONDS,
            MIN_INTERVAL_SECONDS,
            7 * 24 * 60 * 60.0,
        )
        initial_delay = _num(
            "MAC_CURIOSITY_REVIEW_INITIAL_DELAY_SECONDS",
            DEFAULT_INITIAL_DELAY_SECONDS,
            0.0,
            60 * 60.0,
        )
        cooldown = _num(
            "MAC_CURIOSITY_REVIEW_COOLDOWN_SECONDS",
            DEFAULT_COOLDOWN_SECONDS,
            MIN_INTERVAL_SECONDS,
            30 * 24 * 60 * 60.0,
        )
        project = str(env.get("MAC_CURIOSITY_REVIEW_PROJECT") or "").strip() or DEFAULT_PROJECT
        own = str(env.get("MAC_AGENT_ID") or "").strip()
        servable = frozenset({own}) if own else frozenset()
        return cls(
            enabled=enabled,
            interval_seconds=interval,
            initial_delay_seconds=initial_delay,
            cooldown_seconds=cooldown,
            project=project,
            servable_agent_ids=servable,
            configuration_error="; ".join(errors),
        )


class CuriosityReviewer:
    """Periodically file pinned adjudication tasks for OpenClaw agents."""

    def __init__(self, control_plane: Any, config: CuriosityReviewerConfig) -> None:
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
                    "curiosity.review.configuration_invalid",
                    "warning",
                    {"error": self.config.configuration_error},
                )
            return False
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            thread = threading.Thread(target=self._loop, name="mac-curiosity-reviewer", daemon=True)
            self._thread = thread
            thread.start()
        self._observe("curiosity.review.started", "info", {"config": self.config.to_dict()})
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._observe("curiosity.review.stopped", "info", {})
        return stopped

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            thread = self._thread
            last_report = copy.deepcopy(self._last_report)
        return {
            "schema": CURIOSITY_REVIEWER_SCHEMA,
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
            except Exception:  # noqa: BLE001 - the next tick must still run.
                _log.warning("curiosity review tick failed", exc_info=True)
            if self._stop_event.wait(max(0.01, self.config.interval_seconds)):
                return

    # -- core ---------------------------------------------------------------

    def run_once(
        self, *, actor: str = "curiosity-reviewer", trigger: str = "operator"
    ) -> Dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {
                "schema": CURIOSITY_REVIEWER_SCHEMA,
                "status": "busy",
                "trigger": trigger,
                "agents": [],
            }
        results: List[Dict[str, Any]] = []
        run_id = "curev_%s" % uuid.uuid4().hex
        try:
            open_for, latest_for = self._adjudication_snapshot()
            for agent in self._openclaw_agents():
                results.append(
                    self._review_agent(
                        agent,
                        actor=actor,
                        open_for=open_for,
                        latest_for=latest_for,
                    )
                )
        finally:
            self._run_lock.release()
        report = {
            "schema": CURIOSITY_REVIEWER_SCHEMA,
            "run_id": run_id,
            "status": "ok",
            "trigger": trigger,
            "filed_count": sum(1 for r in results if r.get("filed")),
            "agents": results,
        }
        with self._state_lock:
            self._last_report = report
        level = "warning" if any(r.get("error") for r in results) else "info"
        self._observe("curiosity.review.run", level, report)
        return report

    def _openclaw_agents(self) -> List[Any]:
        try:
            agents = list(self.control_plane.list_agents())
        except Exception as exc:  # noqa: BLE001
            _log.warning("curiosity review could not list agents: %s", exc)
            return []
        selected = []
        for agent in agents:
            resources = getattr(agent, "resources", None) or {}
            gateway = resources.get("chat_gateway") if isinstance(resources, Mapping) else None
            if isinstance(gateway, Mapping) and gateway.get("implementation") == "openclaw":
                selected.append(agent)
        return selected

    def _adjudication_snapshot(self):
        """Return (agents with an active adjudication task, newest per agent)."""
        open_for: Dict[str, bool] = {}
        latest_for: Dict[str, datetime] = {}
        try:
            tasks = list(self.control_plane.list_tasks())
        except Exception as exc:  # noqa: BLE001
            _log.warning("curiosity review could not list tasks: %s", exc)
            return open_for, latest_for
        for task in tasks:
            metadata = getattr(task, "metadata", None) or {}
            origin = metadata.get("origin") if isinstance(metadata, Mapping) else None
            if not (isinstance(origin, Mapping) and origin.get("type") == ADJUDICATION_ORIGIN_TYPE):
                continue
            # Key on origin.agent_id, not target_agent_id. The task is no
            # longer pinned, and keying dedupe/cooldown on the pin would make
            # both silently stop working -- the reviewer would refile on every
            # tick and duplicates would pile up, which is what the quarantine
            # already suffered.
            target = str(origin.get("agent_id") or "")
            if not target:
                target = str(metadata.get("target_agent_id") or "")
            if not target:
                continue
            if str(getattr(task, "state", "") or "") in _ACTIVE_STATES:
                open_for[target] = True
            ts = _parse_ts(getattr(task, "created_at", None))
            if ts is not None and (target not in latest_for or ts > latest_for[target]):
                latest_for[target] = ts
        return open_for, latest_for

    def _review_agent(
        self, agent: Any, *, actor: str, open_for: Dict[str, bool], latest_for: Dict[str, datetime]
    ) -> Dict[str, Any]:
        agent_id = str(getattr(agent, "id", "") or "")
        agent_name = str(getattr(agent, "name", "") or agent_id)
        result: Dict[str, Any] = {"agent_id": agent_id, "filed": False}
        servable = self.config.servable_agent_ids
        if servable and agent_id not in servable:
            # `mac admin curiosity` proxies the HUB's host-local wrapper, so a task
            # filed for another agent would read and adjudicate this host's
            # ledger under that agent's name. Filing it would be silently
            # wrong, not merely unsatisfiable, so decline until the hub can
            # route to the owning agent's host.
            result["skipped_reason"] = (
                "hub can only serve %s; adjudicating %s needs per-agent routing"
                % (", ".join(sorted(servable)), agent_id)
            )
            return result
        if not servable:
            result["skipped_reason"] = (
                "hub agent identity is unknown (MAC_AGENT_ID unset); refusing to "
                "file an adjudication task that may read another host's ledger"
            )
            return result
        if open_for.get(agent_id):
            result["skipped_reason"] = "adjudication task already open"
            return result
        last = latest_for.get(agent_id)
        if last is not None:
            age = (_utcnow() - last).total_seconds()
            if age < self.config.cooldown_seconds:
                result["skipped_reason"] = "adjudicated %.0fs ago (< %.0fs)" % (
                    age,
                    self.config.cooldown_seconds,
                )
                return result
        try:
            task = self._create_adjudication_task(agent_id, agent_name, actor)
        except Exception as exc:  # noqa: BLE001 - isolate one agent's failure.
            result["error"] = str(exc)[:500]
            return result
        result["filed"] = True
        result["task_id"] = getattr(task, "id", None)
        return result

    def _create_adjudication_task(self, agent_id: str, agent_name: str, actor: str) -> Any:
        task_ref = "curiosity-adjudication-%s" % uuid.uuid4().hex[:12]
        metadata = {
            "origin": {"type": ADJUDICATION_ORIGIN_TYPE, "agent_id": agent_id},
            # No target_agent_id. Pinning used to be necessary because the
            # ledger is host-local, and it was never sufficient: a pinned task
            # still executes in a mac-task-* sandbox that cannot reach the
            # OpenClaw sandbox holding the ledger, so pinned tasks failed on
            # the correct host. `mac admin curiosity` now reaches it through the hub
            # from any sandbox, so pinning would only shrink the dispatch pool
            # for no gain.
            #
            # Judged on the written adjudication record, not code substance.
            "evidence_type": "investigation",
            "curiosity_approval_ref": task_ref,
        }
        return self.control_plane.create_task(
            "Adjudicate quarantined curiosity candidates on %s" % agent_name,
            description=build_adjudication_description(agent_name, task_ref),
            metadata=metadata,
            # A task with no project resolves no publication target, so an
            # APPROVED one parks in REVIEWING for ever instead of completing
            # (task_ce6c8ea3) -- which is what happened to the four children
            # cancelled on 2026-08-05. Give it a project so it can finish.
            project=self.config.project,
            actor=actor,
        )

    # -- telemetry ----------------------------------------------------------

    def _observe(self, event_type: str, level: str, detail: Dict[str, Any]) -> None:
        try:
            self.control_plane.record_log(event_type, level=level, detail=detail)
        except Exception:  # noqa: BLE001 - telemetry must never break the tick.
            _log.debug("curiosity review observability write failed", exc_info=True)
