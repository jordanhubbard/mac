"""Bounded HGX capacity planning, creation, and spare-session retirement.

The controller in this module deliberately stops at *attested provider
capacity*.  A successful ``hgx create`` only proves that the provider accepted
the request; readiness requires a subsequent nonce-bearing
``HgxProvider.attest_ssh(session_id)`` call.  Turning that machine into a MAC
agent remains the reviewed fungible-onboarding flow in
``deploy/deploy-mac-fleet.sh --prepare-fungible-onboarding``.

``plan`` and ``status`` are read-only. ``execute`` creates explicit
``standard-dind`` sessions in small bounded steps. ``retire_spare`` may delete
only controller-created sessions that never became registered MAC agents;
registered-worker retirement remains the responsibility of the fleet lifecycle
transaction. ``mark_onboarded`` is a receipt-only transition after real agent
registration.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from mac.hgx_provider import (
    STANDARD_DIND_FLAVOR,
    HgxCommandError,
    HgxError,
    HgxProvider,
    HgxSession,
)
from mac.models import MACError, ValidationError


CAPACITY_SCHEMA = "mac.hgx_elastic_capacity.v1"
DEFAULT_STATE_PATH = "~/.mac/hgx-elastic-capacity.json"

_TERMINAL_PROVIDER_STATES = frozenset(
    {"dead", "deleted", "error", "failed", "stopped", "terminated"}
)


class HgxCapacityError(MACError):
    """A safe, operator-facing capacity-controller failure."""


class _Provider(Protocol):
    def list(self) -> List[HgxSession]: ...

    def status(self, session_id: str) -> HgxSession: ...

    def create_standard_dind(
        self, *, name: Optional[str] = None, extra_args: Optional[List[str]] = None
    ) -> HgxSession: ...

    def attest_ssh(self, session_id: str) -> str: ...

    def delete(self, session_id: str) -> str: ...


@dataclass(frozen=True)
class HgxCapacityPolicy:
    """Explicit bounds for one controller invocation."""

    min_ready: int = 0
    max_sessions: int = 10
    headroom: int = 0
    cluster: str = "gke-newhouse"
    gpu_count: int = 1
    memory_gib: int = 64
    cpu_count: int = 8
    max_create_per_run: int = 1
    cooldown_seconds: float = 300.0
    wait_timeout_seconds: float = 300.0
    poll_interval_seconds: float = 5.0
    # Verbatim extra ``hgx create`` arguments, appended after the shape flags
    # this controller owns.
    #
    # A container session that has to run real networking (tailscale, a VPN)
    # needs /dev/net/tun and CAP_NET_ADMIN, and ``hgx create`` exposes no
    # first-class flag for either today.  Rather than guess at a provider flag
    # name that may not exist, the controller carries a bounded, explicit
    # pass-through so an operator can request whatever the provider does
    # expose without waiting for a mac release.  When the provider grants
    # nothing, ``deploy/install-tailscale.sh`` still joins the mesh in
    # userspace-relay mode, so this is an upgrade path and not a prerequisite.
    create_extra_args: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("min_ready", "max_sessions", "headroom", "max_create_per_run"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValidationError("%s must be an integer" % name)
            if value < 0:
                raise ValidationError("%s must be non-negative" % name)
        if self.max_sessions < 1:
            raise ValidationError("max_sessions must be at least 1")
        if self.max_create_per_run < 1:
            raise ValidationError("max_create_per_run must be at least 1")
        if self.min_ready > self.max_sessions:
            raise ValidationError("min_ready must not exceed max_sessions")
        cluster = str(self.cluster or "").strip()
        allowed_cluster_chars = (
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789-_"
        )
        if (
            not cluster
            or len(cluster) > 64
            or any(ch not in allowed_cluster_chars for ch in cluster)
        ):
            raise ValidationError(
                "cluster must be 1..64 letters, digits, '-' or '_'"
            )
        object.__setattr__(self, "cluster", cluster)
        for name, minimum, maximum in (
            ("gpu_count", 0, 8),
            ("memory_gib", 8, 256),
            ("cpu_count", 1, 64),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValidationError("%s must be an integer" % name)
            if not minimum <= value <= maximum:
                raise ValidationError(
                    "%s must be within %d..%d" % (name, minimum, maximum)
                )
        for name in (
            "cooldown_seconds",
            "wait_timeout_seconds",
            "poll_interval_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError("%s must be a number" % name)
            if value < 0:
                raise ValidationError("%s must be non-negative" % name)
        if self.wait_timeout_seconds <= 0:
            raise ValidationError("wait_timeout_seconds must be greater than zero")
        if self.poll_interval_seconds <= 0:
            raise ValidationError("poll_interval_seconds must be greater than zero")
        object.__setattr__(
            self, "create_extra_args", _validated_create_extra_args(self.create_extra_args)
        )

    def desired_ready(self, pending_request_count: int) -> int:
        pending = _pending_count(pending_request_count)
        return min(
            self.max_sessions,
            max(self.min_ready, pending + self.headroom),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "min_ready": self.min_ready,
            "max_sessions": self.max_sessions,
            "headroom": self.headroom,
            "cluster": self.cluster,
            "gpu_count": self.gpu_count,
            "memory_gib": self.memory_gib,
            "cpu_count": self.cpu_count,
            "max_create_per_run": self.max_create_per_run,
            "cooldown_seconds": self.cooldown_seconds,
            "wait_timeout_seconds": self.wait_timeout_seconds,
            "poll_interval_seconds": self.poll_interval_seconds,
            "create_extra_args": list(self.create_extra_args),
        }


# Shape flags the controller supplies itself. An operator override for one of
# these would silently contradict the declared policy — the provider would see
# the flag twice and pick one — so they are refused rather than merged.
_CONTROLLER_OWNED_CREATE_FLAGS = frozenset(
    {"--cluster", "--gpu", "--memory", "--cpu", "--name", "--flavor", "--json"}
)
_CREATE_EXTRA_ARG_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "-_=.,:/+@"
)
_MAX_CREATE_EXTRA_ARGS = 16
_MAX_CREATE_EXTRA_ARG_LENGTH = 128


def _validated_create_extra_args(value: Any) -> Tuple[str, ...]:
    """Bound the verbatim ``hgx create`` pass-through.

    These strings become argv for a provider subprocess, so there is no shell
    to quote for; the bounds exist so a typo cannot turn into an unbounded or
    unreadable provider command, and so the flags the controller owns cannot be
    contradicted from the outside.
    """

    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValidationError("create_extra_args must be a sequence of strings")
    items = list(value)
    if len(items) > _MAX_CREATE_EXTRA_ARGS:
        raise ValidationError(
            "create_extra_args must contain at most %d arguments" % _MAX_CREATE_EXTRA_ARGS
        )
    validated: List[str] = []
    for item in items:
        if not isinstance(item, str) or not item:
            raise ValidationError("create_extra_args entries must be non-empty strings")
        if len(item) > _MAX_CREATE_EXTRA_ARG_LENGTH:
            raise ValidationError(
                "create_extra_args entries must be at most %d characters"
                % _MAX_CREATE_EXTRA_ARG_LENGTH
            )
        if any(ch not in _CREATE_EXTRA_ARG_CHARS for ch in item):
            raise ValidationError(
                "create_extra_args entries may only contain letters, digits and -_=.,:/+@"
            )
        if item.split("=", 1)[0] in _CONTROLLER_OWNED_CREATE_FLAGS:
            raise ValidationError(
                "create_extra_args must not override the controller-owned flag %r"
                % item.split("=", 1)[0]
            )
        validated.append(item)
    return tuple(validated)


def count_pending_provisioning_requests(requests: Iterable[Any]) -> int:
    """Count unique pending rows from ``ProvisioningService.list_requests``.

    This small adapter keeps provider capacity tied to the durable provisioning
    signal without confusing an HGX session with a registered MAC agent.
    """

    identifiers: set[str] = set()
    anonymous = 0
    for request in requests:
        if isinstance(request, Mapping):
            status = request.get("status")
            request_id = request.get("id")
        else:
            status = getattr(request, "status", None)
            request_id = getattr(request, "id", None)
        if str(status or "").strip().lower() != "pending":
            continue
        if request_id:
            identifiers.add(str(request_id))
        else:
            anonymous += 1
    return len(identifiers) + anonymous


def normalize_registered_fungible_agents(
    registered_agents: Optional[Any],
) -> Dict[str, str]:
    """Index registered fungible agents by their immutable HGX session ID.

    Elastic capacity planning must not mistake an already-onboarded HGX session
    for spare provider quota.  The fleet registry is the durable record of which
    immutable ``hgx`` session backs which registered fungible MAC agent, so the
    controller reconciles that mapping against live provider inventory *by
    immutable session identity* before it proposes any new capacity.

    Accepts either a mapping of ``session_id -> agent_id`` or an iterable of
    records exposing ``session_id`` and ``agent_id`` (mapping keys or
    attributes).  Blank identities are ignored; a single immutable session may
    not claim two different registered agents.
    """

    if registered_agents is None:
        return {}
    if isinstance(registered_agents, Mapping):
        pairs: Iterable[tuple[Any, Any]] = registered_agents.items()
    else:
        pairs = _iter_registered_agent_records(registered_agents)
    indexed: Dict[str, str] = {}
    for raw_session_id, raw_agent_id in pairs:
        session_id = str(raw_session_id or "").strip()
        agent_id = str(raw_agent_id or "").strip()
        if not session_id or not agent_id:
            continue
        existing = indexed.get(session_id)
        if existing is not None and existing != agent_id:
            raise ValidationError(
                "session %s is claimed by two registered agents (%s, %s)"
                % (session_id, existing, agent_id)
            )
        indexed[session_id] = agent_id
    return indexed


def _iter_registered_agent_records(
    records: Any,
) -> Iterator[tuple[Any, Any]]:
    if isinstance(records, (str, bytes)):
        raise ValidationError("registered_agents must not be a bare string")
    for record in records:
        if isinstance(record, Mapping):
            session_id = record.get("session_id") or record.get("hgx_session_id")
            agent_id = record.get("agent_id") or record.get("agent")
        elif isinstance(record, (tuple, list)) and len(record) == 2:
            session_id, agent_id = record
        else:
            session_id = getattr(record, "session_id", None) or getattr(
                record, "hgx_session_id", None
            )
            agent_id = getattr(record, "agent_id", None) or getattr(
                record, "agent", None
            )
        yield session_id, agent_id


class HgxElasticCapacityController:
    """Inspect or explicitly add bounded, nonce-attested HGX capacity."""

    def __init__(
        self,
        *,
        provider: Optional[_Provider] = None,
        policy: Optional[HgxCapacityPolicy] = None,
        state_path: str | Path = DEFAULT_STATE_PATH,
        name_prefix: str = "mac-fungible",
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider or HgxProvider()
        self.policy = policy or HgxCapacityPolicy()
        self.state_path = Path(state_path).expanduser()
        prefix = str(name_prefix or "").strip()
        allowed_name_chars = (
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789-_"
        )
        if not prefix or any(ch not in allowed_name_chars for ch in prefix):
            raise ValidationError(
                "name_prefix must contain only letters, digits, '-' or '_'"
            )
        self.name_prefix = prefix
        self._clock = clock
        self._sleep = sleeper

    # Read-only surfaces -------------------------------------------------

    def status(
        self,
        *,
        pending_request_count: int = 0,
        registered_agents: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Return current provider/state inventory without changing either."""

        return self._inspect(
            mode="status",
            pending_request_count=pending_request_count,
            registered_agents=registered_agents,
        )

    def plan(
        self,
        *,
        pending_request_count: int = 0,
        registered_agents: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Return the bounded next action without changing provider or state."""

        return self._inspect(
            mode="plan",
            pending_request_count=pending_request_count,
            registered_agents=registered_agents,
        )

    def _inspect(
        self,
        *,
        mode: str,
        pending_request_count: int,
        registered_agents: Optional[Any] = None,
    ) -> Dict[str, Any]:
        pending = _pending_count(pending_request_count)
        indexed_agents = normalize_registered_fungible_agents(registered_agents)
        state = self._load_state()
        inventory = self._list_sessions()
        reconciled = self._reconcile_registered_agents(
            inventory=inventory, state=state, registered_agents=indexed_agents
        )
        now = self._clock()
        snapshot = self._snapshot(
            inventory=inventory,
            state=state,
            pending_request_count=pending,
            now=now,
        )
        snapshot["reconciled_onboarded_session_ids"] = reconciled
        actions: List[Dict[str, Any]] = []
        if snapshot["unattested_session_ids"]:
            actions.append(
                {
                    "action": "attest_existing",
                    "session_ids": snapshot["unattested_session_ids"],
                    "requires_execute": True,
                }
            )
        if snapshot["create_count"] > 0:
            actions.append(
                {
                    "action": "create_standard_dind",
                    "count": snapshot["create_count"],
                    "requires_execute": True,
                }
            )
        if snapshot["cooldown_remaining_seconds"] > 0 and snapshot["ready_gap"] > 0:
            actions.append(
                {
                    "action": "wait_for_cooldown",
                    "seconds": snapshot["cooldown_remaining_seconds"],
                }
            )
        return {
            "schema": CAPACITY_SCHEMA,
            "mode": mode,
            "read_only": True,
            "state_path": str(self.state_path),
            "policy": self.policy.to_dict(),
            "pending_request_count": pending,
            **snapshot,
            "actions": actions,
            "deletion": {
                "automatic": False,
                "reason": (
                    "plan/status never mutate; the autoscaler may retire only "
                    "aged controller-created sessions that were never onboarded"
                ),
            },
        }

    # Explicit mutation surface -----------------------------------------

    def execute(
        self,
        *,
        pending_request_count: int = 0,
        registered_agents: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Attest existing capacity, then create within the configured bounds."""

        indexed_agents = normalize_registered_fungible_agents(registered_agents)
        with self._execution_lock():
            return self._execute_locked(
                pending_request_count=pending_request_count,
                registered_agents=indexed_agents,
            )

    def mark_onboarded(
        self, session_id: str, *, agent_id: str
    ) -> Dict[str, Any]:
        """Consume attested supply after it becomes a registered MAC agent."""

        immutable_id = str(session_id or "").strip()
        registered_agent_id = str(agent_id or "").strip()
        if not immutable_id:
            raise ValidationError("session_id must not be empty")
        if not registered_agent_id:
            raise ValidationError("agent_id must not be empty")
        with self._execution_lock():
            state = self._load_state()
            record = state["sessions"].get(immutable_id)
            if not isinstance(record, dict):
                raise HgxCapacityError(
                    "session %s has no controller receipt" % immutable_id
                )
            if record.get("created_by_controller") is not True:
                raise HgxCapacityError(
                    "session %s is not controller-created capacity" % immutable_id
                )
            if record.get("attestation_status") != "passed":
                raise HgxCapacityError(
                    "session %s has not passed SSH attestation" % immutable_id
                )
            prior_agent_id = str(record.get("onboarded_agent_id") or "").strip()
            if prior_agent_id and prior_agent_id != registered_agent_id:
                raise HgxCapacityError(
                    "session %s is already consumed by agent %s"
                    % (immutable_id, prior_agent_id)
                )
            now = self._clock()
            record.update(
                {
                    "onboarding_status": "onboarded",
                    "onboarded_agent_id": registered_agent_id,
                    "onboarded_at": record.get("onboarded_at") or _timestamp(now),
                    "next_action": None,
                }
            )
            self._write_state(state)
            return {
                "schema": CAPACITY_SCHEMA,
                "mode": "mark_onboarded",
                "state_path": str(self.state_path),
                "session_id": immutable_id,
                "agent_id": registered_agent_id,
                "available_for_pending_supply": False,
                "provider_mutation": False,
                "idempotent": bool(prior_agent_id),
            }

    def retire_spare(
        self,
        *,
        pending_request_count: int = 0,
        min_age_seconds: float = 3600.0,
        max_delete_count: int = 1,
        registered_agents: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Delete old surplus sessions that never became registered agents.

        The exact immutable provider IDs must be controller-owned and absent
        from the registered-agent mapping. Onboarded sessions are never
        candidates: retiring one of those also requires draining leases,
        tombstoning credentials, and updating ``fleets.yaml``.
        """

        pending = _pending_count(pending_request_count)
        if isinstance(min_age_seconds, bool) or not isinstance(
            min_age_seconds, (int, float)
        ):
            raise ValidationError("min_age_seconds must be a number")
        if min_age_seconds < 0:
            raise ValidationError("min_age_seconds must be non-negative")
        if (
            isinstance(max_delete_count, bool)
            or not isinstance(max_delete_count, int)
            or max_delete_count < 1
        ):
            raise ValidationError("max_delete_count must be a positive integer")
        indexed_agents = normalize_registered_fungible_agents(registered_agents)

        with self._execution_lock():
            state = self._load_state()
            inventory = self._list_sessions()
            reconciled = self._reconcile_registered_agents(
                inventory=inventory,
                state=state,
                registered_agents=indexed_agents,
            )
            now = self._clock()
            state_sessions = state.setdefault("sessions", {})
            live = [session for session in inventory if not _is_terminal(session.state)]
            live_ids = {session.session_id for session in live}
            onboarded = {
                str(session_id)
                for session_id, record in state_sessions.items()
                if str(session_id) in live_ids and _is_onboarded(record)
            }
            desired = self.policy.desired_ready(pending)
            keep_healthy_spares = max(0, desired - len(onboarded))

            spare_rows: List[tuple[HgxSession, Dict[str, Any], float]] = []
            for session in live:
                record = state_sessions.get(session.session_id)
                if (
                    not isinstance(record, dict)
                    or record.get("created_by_controller") is not True
                    or _is_onboarded(record)
                ):
                    continue
                created_epoch = _record_epoch(record, "created_at")
                age = max(0.0, now - created_epoch) if created_epoch is not None else 0.0
                spare_rows.append((session, record, age))

            unhealthy = [
                row for row in spare_rows if row[1].get("attestation_status") != "passed"
            ]
            healthy = [
                row for row in spare_rows if row[1].get("attestation_status") == "passed"
            ]
            oldest_first = lambda row: (  # noqa: E731 - compact deterministic key
                _record_epoch(row[1], "created_at") or now,
                row[0].session_id,
            )
            unhealthy.sort(key=oldest_first)
            healthy.sort(key=oldest_first)
            healthy_surplus = max(0, len(healthy) - keep_healthy_spares)
            candidates = unhealthy + healthy[:healthy_surplus]

            retired: List[str] = []
            failed: List[str] = []
            for session, record, age in candidates:
                if len(retired) + len(failed) >= max_delete_count:
                    break
                if age < float(min_age_seconds):
                    continue
                try:
                    self.provider.delete(session.session_id)
                except HgxError:
                    failed.append(session.session_id)
                    record["retirement_status"] = "provider_delete_failed"
                    record["retirement_failure_class"] = "provider_delete_failed"
                    record["retirement_attempted_at"] = _timestamp(now)
                    continue
                retired.append(session.session_id)
                record.update(
                    {
                        "provider_state": "deleted",
                        "retirement_status": "retired",
                        "retirement_reason": "sustained_surplus_unonboarded_capacity",
                        "retired_at": _timestamp(now),
                        "next_action": None,
                    }
                )

            result = {
                "schema": CAPACITY_SCHEMA,
                "mode": "retire_spare",
                "read_only": False,
                "pending_request_count": pending,
                "desired_ready": desired,
                "protected_onboarded_session_ids": sorted(onboarded),
                "reconciled_onboarded_session_ids": reconciled,
                "candidate_session_ids": [row[0].session_id for row in candidates],
                "retired_session_ids": retired,
                "failed_retirement_session_ids": failed,
                "deletion": {
                    "automatic": True,
                    "performed": bool(retired),
                    "scope": "controller_created_unonboarded_sessions_only",
                },
            }
            state["last_retirement_result"] = {
                **result,
                "recorded_at": _timestamp(now),
            }
            self._write_state(state)
            return result

    def _execute_locked(
        self,
        *,
        pending_request_count: int = 0,
        registered_agents: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        pending = _pending_count(pending_request_count)
        state = self._load_state()
        inventory = self._list_sessions()
        inventory_by_id = {session.session_id: session for session in inventory}
        reconciled = self._reconcile_registered_agents(
            inventory=inventory,
            state=state,
            registered_agents=registered_agents or {},
        )
        if reconciled:
            # Persist the identity reconciliation before any provider mutation so
            # an already-onboarded session is durably counted as healthy supply
            # and never re-attested or double-created on the next invocation.
            self._write_state(state)
        attested: List[str] = []
        failed_attestations: List[str] = []
        created: List[str] = []

        # Only controller-created, not-yet-onboarded sessions are pending
        # provisioning supply. Untracked standard-dind sessions may be busy
        # workers: they consume provider quota but are neither re-attested nor
        # allowed to satisfy new demand.
        for session in sorted(
            (
                item
                for item in inventory
                if self._is_available_capacity_session(item, state)
                and not _is_terminal(item.state)
            ),
            key=lambda item: item.session_id,
        ):
            if self._attest_once(session.session_id, state):
                attested.append(session.session_id)
            else:
                failed_attestations.append(session.session_id)
        self._write_state(state)

        desired = self.policy.desired_ready(pending)
        live_ids = {
            session.session_id
            for session in inventory_by_id.values()
            if self._session_is_live(session, state)
        }
        healthy_onboarded = self._registry_onboarded_live_ids(state, live_ids)
        # Already-onboarded, healthy sessions matched to registered fungible
        # agents are counted as satisfied capacity, so the controller stops
        # short of proposing (and quota-exhausting on) redundant new sessions.
        target = max(0, desired - len(healthy_onboarded))
        ready_ids = set(attested)
        cooldown_remaining = self._cooldown_remaining(state, self._clock())
        create_failure_class: Optional[str] = None
        initial_live_count = sum(
            1
            for session in inventory_by_id.values()
            if self._session_is_live(session, state)
        )
        create_budget = min(
            self.policy.max_create_per_run,
            max(0, self.policy.max_sessions - initial_live_count),
        )

        # Cooldown limits independent scale-up invocations, not the bounded
        # batch within a single explicit execute command.
        if cooldown_remaining <= 0:
            while len(ready_ids) < target and len(created) < create_budget:
                live_count = sum(
                    1
                    for session in inventory_by_id.values()
                    if self._session_is_live(session, state)
                )
                if live_count >= self.policy.max_sessions:
                    break
                try:
                    session = self.provider.create_standard_dind(
                        name=self._new_session_name(len(created) + 1),
                        extra_args=[
                            "--cluster",
                            self.policy.cluster,
                            "--gpu",
                            str(self.policy.gpu_count),
                            "--memory",
                            "%dGi" % self.policy.memory_gib,
                            "--cpu",
                            str(self.policy.cpu_count),
                            *self.policy.create_extra_args,
                        ],
                    )
                except HgxCommandError as exc:
                    create_failure_class = _classify_create_failure(exc)
                    break
                except HgxError:
                    create_failure_class = "provider_create_failed"
                    break
                if not session.session_id:
                    create_failure_class = "provider_create_invalid_response"
                    break
                created.append(session.session_id)
                inventory_by_id[session.session_id] = session
                now = self._clock()
                state["last_create_at"] = now
                state["sessions"][session.session_id] = {
                    "session_id": session.session_id,
                    "created_by_controller": True,
                    "provider_flavor": STANDARD_DIND_FLAVOR,
                    "provider_state": session.state or None,
                    "creation_status": "provider_accepted",
                    "created_at": _timestamp(now),
                    "attestation_status": "pending",
                    "onboarding_status": "not_onboarded",
                    "next_action": {
                        "action": "wait_for_ssh_attestation",
                        "session_id": session.session_id,
                    },
                }
                # Persist immediately: a crash after create must not lose the
                # immutable ID and cause the next invocation to create blindly.
                self._write_state(state)
                if self._wait_for_attestation(session.session_id, state):
                    ready_ids.add(session.session_id)
                    attested.append(session.session_id)
                else:
                    failed_attestations.append(session.session_id)
                self._write_state(state)

        desired_gap = max(0, target - len(ready_ids))
        final_live_count = sum(
            1
            for session in inventory_by_id.values()
            if self._session_is_live(session, state)
        )
        capacity_bound_reached = final_live_count >= self.policy.max_sessions
        next_actions = self._next_actions(
            ready_ids=sorted(ready_ids),
            failed_ids=sorted(set(failed_attestations)),
            desired_gap=desired_gap,
            cooldown_remaining=self._cooldown_remaining(state, self._clock()),
            create_failure_class=create_failure_class,
            capacity_bound_reached=capacity_bound_reached,
        )
        state["last_result"] = {
            "recorded_at": _timestamp(self._clock()),
            "desired_ready": desired,
            "attested_session_ids": sorted(ready_ids),
            "created_session_ids": list(created),
            "ready_gap": desired_gap,
            "next_actions": next_actions,
        }
        self._write_state(state)

        if desired_gap == 0:
            outcome = (
                "attested_capacity_requires_onboarding"
                if ready_ids
                else "capacity_satisfied"
            )
        elif create_failure_class:
            outcome = create_failure_class
        elif capacity_bound_reached:
            outcome = "capacity_bound_reached"
        elif cooldown_remaining > 0:
            outcome = "cooldown"
        else:
            outcome = "capacity_not_ready"
        return {
            "schema": CAPACITY_SCHEMA,
            "mode": "execute",
            "read_only": False,
            "outcome": outcome,
            "state_path": str(self.state_path),
            "policy": self.policy.to_dict(),
            "pending_request_count": pending,
            "desired_ready": desired,
            "attested_session_ids": sorted(ready_ids),
            "created_session_ids": created,
            "reconciled_onboarded_session_ids": reconciled,
            "failed_attestation_session_ids": sorted(set(failed_attestations)),
            "provider_create_failure_class": create_failure_class,
            "ready_gap": desired_gap,
            "next_actions": next_actions,
            "deletion": {
                "automatic": False,
                "performed": False,
            },
        }

    # Provider and state helpers ----------------------------------------

    def _list_sessions(self) -> List[HgxSession]:
        try:
            sessions = self.provider.list()
        except HgxError as exc:
            raise HgxCapacityError(
                "unable to list HGX sessions; provider inventory is unavailable"
            ) from exc
        ids: set[str] = set()
        for session in sessions:
            if not session.session_id:
                raise HgxCapacityError(
                    "HGX inventory included a session without an immutable ID"
                )
            if session.session_id in ids:
                raise HgxCapacityError(
                    "HGX inventory repeated immutable session ID %s"
                    % session.session_id
                )
            ids.add(session.session_id)
        return sessions

    def _attest_once(self, session_id: str, state: Dict[str, Any]) -> bool:
        now = self._clock()
        record = state["sessions"].setdefault(
            session_id,
            {
                "session_id": session_id,
                "created_by_controller": False,
            },
        )
        try:
            session = self.provider.status(session_id)
            record["provider_state"] = session.state or None
            if _is_terminal(session.state):
                return self._record_attestation_failure(
                    record, now, "terminal_provider_state"
                )
            proven_id = self.provider.attest_ssh(session_id)
            if proven_id != session_id:
                return self._record_attestation_failure(
                    record, now, "immutable_id_mismatch"
                )
        except HgxError:
            return self._record_attestation_failure(
                record, now, "ssh_attestation_failed"
            )
        record.update(
            {
                "attestation_status": "passed",
                "attested_at": _timestamp(now),
                "failure_class": None,
                "next_action": _onboarding_action(session_id),
            }
        )
        return True

    def _wait_for_attestation(
        self, session_id: str, state: Dict[str, Any]
    ) -> bool:
        deadline = self._clock() + self.policy.wait_timeout_seconds
        while True:
            if self._attest_once(session_id, state):
                return True
            now = self._clock()
            if now >= deadline:
                record = state["sessions"][session_id]
                record["attestation_status"] = "failed"
                record["failure_class"] = "ssh_attestation_timeout"
                record["next_action"] = {
                    "action": "retry_or_retire_explicitly",
                    "session_id": session_id,
                    "reason": (
                        "provider creation completed but nonce SSH attestation "
                        "did not pass before the deadline"
                    ),
                    "automatic_deletion": False,
                }
                return False
            self._sleep(
                min(self.policy.poll_interval_seconds, max(0.0, deadline - now))
            )

    @staticmethod
    def _record_attestation_failure(
        record: Dict[str, Any], now: float, failure_class: str
    ) -> bool:
        record.update(
            {
                "attestation_status": "failed",
                "last_attestation_attempt_at": _timestamp(now),
                "failure_class": failure_class,
                "next_action": {
                    "action": "retry_ssh_attestation",
                    "session_id": record["session_id"],
                },
            }
        )
        return False

    def _reconcile_registered_agents(
        self,
        *,
        inventory: List[HgxSession],
        state: Dict[str, Any],
        registered_agents: Mapping[str, str],
    ) -> List[str]:
        """Match live HGX sessions to registered fungible agents by identity.

        Live capacity planning previously classified every session without a
        local controller receipt as *untracked* and, seeing no counted supply,
        tried to create more — only stopping at ``provider_quota_exhausted``.
        The fleet registry already knows which immutable ``hgx`` session backs
        which registered fungible MAC agent, so reconcile that mapping onto the
        durable receipt store: an already-onboarded, healthy session becomes
        counted onboarded supply instead of phantom untracked capacity.

        Only live (non-terminal) provider sessions are reconciled; a registered
        agent whose session the provider has dropped or terminated is not
        counted as healthy onboarded capacity here. Returns the immutable IDs
        reconciled during this call.
        """

        if not registered_agents:
            return []
        sessions = state.setdefault("sessions", {})
        reconciled: List[str] = []
        for session in inventory:
            session_id = session.session_id
            agent_id = registered_agents.get(session_id)
            if not agent_id:
                continue
            if _is_terminal(session.state):
                continue
            record = sessions.get(session_id)
            if not isinstance(record, dict):
                record = {"session_id": session_id}
                sessions[session_id] = record
            already = (
                _is_onboarded(record)
                and str(record.get("onboarded_agent_id") or "").strip() == agent_id
            )
            record["session_id"] = session_id
            record["created_by_controller"] = True
            record["provider_state"] = session.state or record.get("provider_state")
            record.setdefault("provider_flavor", session.flavor or None)
            record["onboarding_status"] = "onboarded"
            record["onboarded_agent_id"] = agent_id
            record["reconciled_from_registry"] = True
            record.setdefault("onboarded_at", _timestamp(self._clock()))
            record["next_action"] = None
            if not already:
                reconciled.append(session_id)
        return sorted(reconciled)

    @staticmethod
    def _registry_onboarded_live_ids(
        state: Mapping[str, Any], live_ids: set[str]
    ) -> List[str]:
        """Immutable IDs of healthy, registry-reconciled onboarded sessions.

        Only sessions still present in live provider inventory count as healthy
        capacity; a registry receipt whose provider session has terminated does
        not offset new demand.
        """

        sessions = state.get("sessions", {})
        return sorted(
            str(session_id)
            for session_id, record in sessions.items()
            if isinstance(record, Mapping)
            and _is_onboarded(record)
            and bool(record.get("reconciled_from_registry"))
            and str(session_id) in live_ids
        )

    def _snapshot(
        self,
        *,
        inventory: List[HgxSession],
        state: Mapping[str, Any],
        pending_request_count: int,
        now: float,
    ) -> Dict[str, Any]:
        state_sessions = state.get("sessions", {})
        live = [item for item in inventory if not _is_terminal(item.state)]
        available_capacity = [
            item
            for item in live
            if self._is_available_capacity_session(item, state)
        ]
        known_attested = [
            item.session_id
            for item in available_capacity
            if isinstance(state_sessions.get(item.session_id), Mapping)
            and state_sessions[item.session_id].get("attestation_status") == "passed"
        ]
        live_ids = {item.session_id for item in live}
        registry_onboarded = self._registry_onboarded_live_ids(state, live_ids)
        desired = self.policy.desired_ready(pending_request_count)
        healthy_supply = len(known_attested) + len(registry_onboarded)
        gap = max(0, desired - healthy_supply)
        slots = max(0, self.policy.max_sessions - len(live))
        cooldown = self._cooldown_remaining(state, now)
        unattested = sorted(
            item.session_id
            for item in available_capacity
            if item.session_id not in known_attested
        )
        tracked_ids = {
            str(session_id)
            for session_id, record in state_sessions.items()
            if isinstance(record, Mapping)
            and record.get("created_by_controller") is True
        }
        untracked_live_ids = sorted(
            item.session_id
            for item in live
            if item.session_id not in tracked_ids
        )
        onboarded_ids = sorted(
            str(session_id)
            for session_id, record in state_sessions.items()
            if isinstance(record, Mapping) and _is_onboarded(record)
        )
        create_gap = max(0, gap - len(unattested))
        return {
            "desired_ready": desired,
            "known_attested_session_ids": sorted(known_attested),
            "unattested_session_ids": unattested,
            "onboarded_session_ids": onboarded_ids,
            "registry_onboarded_session_ids": registry_onboarded,
            "untracked_live_session_ids": untracked_live_ids,
            "live_provider_session_count": len(live),
            "available_capacity_session_count": len(available_capacity),
            "provider_sessions": [item.observable() for item in inventory],
            "ready_gap": gap,
            "available_session_slots": slots,
            "cooldown_remaining_seconds": cooldown,
            "create_count": (
                0
                if cooldown > 0
                else min(create_gap, slots, self.policy.max_create_per_run)
            ),
        }

    @staticmethod
    def _is_available_capacity_session(
        session: HgxSession, state: Mapping[str, Any]
    ) -> bool:
        record = state.get("sessions", {}).get(session.session_id, {})
        return bool(
            isinstance(record, Mapping)
            and record.get("created_by_controller") is True
            and not _is_onboarded(record)
        )

    @staticmethod
    def _session_is_live(
        session: HgxSession, state: Mapping[str, Any]
    ) -> bool:
        record = state.get("sessions", {}).get(session.session_id, {})
        observed_state = (
            record.get("provider_state")
            if isinstance(record, Mapping)
            else None
        )
        return not _is_terminal(observed_state or session.state)

    def _cooldown_remaining(
        self, state: Mapping[str, Any], now: float
    ) -> float:
        last_create = state.get("last_create_at")
        if not isinstance(last_create, (int, float)):
            return 0.0
        return round(
            max(0.0, float(last_create) + self.policy.cooldown_seconds - now),
            3,
        )

    def _new_session_name(self, ordinal: int) -> str:
        stamp = datetime.fromtimestamp(
            self._clock(), tz=timezone.utc
        ).strftime("%Y%m%d-%H%M%S")
        return "%s-%s-%02d" % (self.name_prefix, stamp, ordinal)

    def _load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {
                "schema": CAPACITY_SCHEMA,
                "sessions": {},
                "last_create_at": None,
            }
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HgxCapacityError(
                "HGX capacity state is unreadable: %s" % self.state_path
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema") != CAPACITY_SCHEMA:
            raise HgxCapacityError(
                "HGX capacity state has an unsupported schema: %s"
                % self.state_path
            )
        if not isinstance(payload.get("sessions"), dict):
            raise HgxCapacityError(
                "HGX capacity state has invalid sessions: %s" % self.state_path
            )
        return payload

    def _write_state(self, state: Mapping[str, Any]) -> None:
        parent = self.state_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        payload = dict(state)
        payload["schema"] = CAPACITY_SCHEMA
        payload["updated_at"] = _timestamp(self._clock())
        encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        fd, temporary = tempfile.mkstemp(
            dir=str(parent), prefix=".%s." % self.state_path.name
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @contextmanager
    def _execution_lock(self) -> Iterator[None]:
        """Fail fast when another mutating controller invocation is active."""

        parent = self.state_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HgxCapacityError(
                    "another HGX capacity execute command is already active"
                ) from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @staticmethod
    def _next_actions(
        *,
        ready_ids: List[str],
        failed_ids: List[str],
        desired_gap: int,
        cooldown_remaining: float,
        create_failure_class: Optional[str],
        capacity_bound_reached: bool,
    ) -> List[Dict[str, Any]]:
        actions = [_onboarding_action(session_id) for session_id in ready_ids]
        actions.extend(
            {
                "action": "retry_or_retire_explicitly",
                "session_id": session_id,
                "automatic_deletion": False,
            }
            for session_id in failed_ids
        )
        if desired_gap > 0 and create_failure_class:
            actions.append(
                {
                    "action": (
                        "wait_for_provider_quota_or_raise_bound"
                        if create_failure_class == "provider_quota_exhausted"
                        else "repair_provider_create_path_then_execute"
                    ),
                    "ready_gap": desired_gap,
                    "failure_class": create_failure_class,
                }
            )
        elif desired_gap > 0 and capacity_bound_reached:
            actions.append(
                {
                    "action": "review_failed_sessions_or_capacity_bound",
                    "ready_gap": desired_gap,
                    "automatic_deletion": False,
                }
            )
        elif desired_gap > 0 and cooldown_remaining > 0:
            actions.append(
                {
                    "action": "execute_after_cooldown",
                    "seconds": cooldown_remaining,
                }
            )
        elif desired_gap > 0:
            actions.append(
                {
                    "action": "review_capacity_bound_then_execute",
                    "ready_gap": desired_gap,
                }
            )
        return actions


def _pending_count(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError("pending_request_count must be a non-negative integer")
    return value


def _timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _record_epoch(record: Mapping[str, Any], field: str) -> Optional[float]:
    value = record.get(field)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_terminal(state: str) -> bool:
    return str(state or "").strip().lower() in _TERMINAL_PROVIDER_STATES


def _is_onboarded(record: Any) -> bool:
    if not isinstance(record, Mapping):
        return False
    return bool(
        str(record.get("onboarding_status") or "").strip().lower()
        in {"consumed", "onboarded"}
        or str(record.get("onboarded_agent_id") or "").strip()
    )


def _classify_create_failure(error: HgxCommandError) -> str:
    """Reduce provider stderr to a secret-free actionable failure class."""

    detail = str(error.stderr or "").lower()
    quota_markers = (
        "429",
        "quota",
        "resource exhausted",
        "resource_exhausted",
        "limit exceeded",
        "too many requests",
    )
    if any(marker in detail for marker in quota_markers):
        return "provider_quota_exhausted"
    return "provider_create_failed"


def _onboarding_action(session_id: str) -> Dict[str, Any]:
    return {
        "action": "prepare_fungible_onboarding",
        "session_id": session_id,
        "reason": (
            "nonce SSH attestation passed, but an HGX session is not yet a "
            "registered MAC agent"
        ),
        "required_inputs": [
            "fleet_agent_name",
            "hub_agent",
            "reviewed_fungible_placeholder",
            "endpoint_bound_worker_credentials",
        ],
        "planner": "deploy/deploy-mac-fleet.sh --prepare-fungible-onboarding",
        "automatic_fulfillment": False,
    }
