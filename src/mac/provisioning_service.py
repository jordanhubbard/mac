"""Agent provisioning request service.

When the dispatcher or the default-review workflow can't find an eligible
agent for a task, it emits a provisioning request: a durable row that
says "the swarm needs an agent with these characteristics." A future
provisioner (k8s operator, nomad job, local spawner) polls
``list_pending_requests()`` and fulfills them by registering the
requested agent.

The durable signal is the ``agent_provisioning_requests`` row plus an
observability event (``provisioning.agent_requested``). Operators can fulfill
requests manually with ``fulfill_request(request_id, agent_id)`` or cancel
them. Background capacity controllers may subscribe to request notifications;
the durable row remains authoritative if a notification is lost.

A future ``register_provisioner`` hook lets a runtime plug in
auto-fulfillment without changing this service.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional

from mac.models import (
    AgentProvisioningRequest,
    JsonDict,
    NotFoundError,
    PROVISIONING_TERMINAL_STATES,
    ProvisioningStatus,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.observability_service import ObservabilityService
from mac.resource_inventory import agent_resource_command_names as _agent_resource_command_names

# A provisioner callable receives the just-created request and may
# either fulfill it synchronously (return an agent_id) or no-op and
# let an external poller handle it. Async fulfillment is the default
# path — the hook is here so future inline provisioners (e.g., a
# dev-mode auto-spawner) can plug in without touching dispatch.
ProvisionerHook = Callable[[AgentProvisioningRequest], Optional[str]]
ProvisioningRequestListener = Callable[[AgentProvisioningRequest], None]


def _metadata_string_list(value: Any) -> List[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if not isinstance(value, Iterable) or isinstance(value, dict):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _json_object(value: Any) -> JsonDict:
    return value if isinstance(value, dict) else {}


def _request_required_commands(request: AgentProvisioningRequest) -> List[str]:
    required: List[str] = []
    seen: set[str] = set()
    detail = ensure_json_object(request.detail)
    containers = [
        detail,
        _json_object(detail.get("toolchain_requirements")),
    ]
    for container in containers:
        for command in _metadata_string_list(container.get("required_commands")):
            if command not in seen:
                seen.add(command)
                required.append(command)
    return required


class ProvisioningService:
    def __init__(self, store: Any, observability: ObservabilityService) -> None:
        self.store = store
        self.observability = observability
        self._provisioner: Optional[ProvisionerHook] = None
        self._request_listeners: List[ProvisioningRequestListener] = []

    # Hook registration -------------------------------------------------

    def register_provisioner(self, hook: ProvisionerHook) -> None:
        """Register a callable that will be invoked synchronously after
        each new request lands. The hook may return an ``agent_id`` to
        mark the request fulfilled, or ``None`` to leave it pending."""
        self._provisioner = hook

    def register_request_listener(self, listener: ProvisioningRequestListener) -> None:
        """Subscribe a non-authoritative wake-up listener.

        Listeners must not perform provider work inline: request creation is a
        dispatcher path. They are intended to wake a background reconciler,
        which then polls the durable request ledger. Duplicate registrations
        are ignored.
        """

        if listener not in self._request_listeners:
            self._request_listeners.append(listener)

    def unregister_request_listener(self, listener: ProvisioningRequestListener) -> None:
        """Remove a previously registered wake-up listener."""

        try:
            self._request_listeners.remove(listener)
        except ValueError:
            pass

    def _notify_request_listeners(self, request: AgentProvisioningRequest) -> None:
        for listener in tuple(self._request_listeners):
            try:
                listener(request)
            except Exception:  # noqa: BLE001 - wake-up loss cannot abort dispatch
                self.observability.record_log(
                    "provisioning.listener_failed",
                    level="error",
                    layer="control_plane",
                    source="provisioning",
                    subject_type="agent_provisioning_request",
                    subject_id=request.id,
                    detail={"reason": "exception in request listener"},
                )

    # Public API --------------------------------------------------------

    def request_agent(
        self,
        *,
        reason: str,
        role_slug: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
        hardware: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        requested_by: Optional[str] = None,
    ) -> AgentProvisioningRequest:
        """Emit a provisioning request and run any registered hook.

        Idempotent on (reason, role_slug, task_id, tenant_id, pending):
        if a pending request already matches, return it instead of
        opening a duplicate. This keeps the dispatcher from creating a
        new row on every tick when the underlying shortage persists.
        """
        reason_value = (reason or "").strip()
        if not reason_value:
            raise ValidationError("provisioning request requires a reason")
        capabilities_list = sorted({str(c).strip() for c in (capabilities or []) if str(c).strip()})
        hardware_obj = ensure_json_object(hardware)
        detail_obj = ensure_json_object(detail)

        # SQLite accepts ``col IS ?`` (matches NULL or value); Postgres
        # rejects it ("syntax error at or near $N"). Split into IS NULL
        # vs = ? branches so the SQL works on both backends.
        clauses = ["status = ?", "reason = ?"]
        params: List[Any] = [ProvisioningStatus.PENDING.value, reason_value]
        for col, value in (
            ("role_slug", role_slug),
            ("task_id", task_id),
            ("tenant_id", tenant_id),
        ):
            if value is None:
                clauses.append("%s IS NULL" % col)
            else:
                clauses.append("%s = ?" % col)
                params.append(value)
        existing = self.store.query_one(
            "SELECT * FROM agent_provisioning_requests WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, id DESC LIMIT 1",
            tuple(params),
        )
        if existing is not None:
            # Refresh updated_at + detail so subsequent ticks show the
            # signal is still live, without minting a new row.
            self.store.execute(
                """
                UPDATE agent_provisioning_requests
                SET detail = ?, updated_at = ?
                WHERE id = ?
                """,
                (json_dumps(detail_obj), utcnow(), existing["id"]),
            )
            request = self.get_request(existing["id"])
            self._notify_request_listeners(request)
            return request

        rid = new_id("prov")
        now = utcnow()
        self.store.execute(
            """
            INSERT INTO agent_provisioning_requests (
                id, status, reason, role_slug, capabilities, hardware,
                task_id, tenant_id, detail, fulfilled_agent_id,
                requested_by, created_at, updated_at, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL)
            """,
            (
                rid,
                ProvisioningStatus.PENDING.value,
                reason_value,
                role_slug,
                json_dumps(capabilities_list),
                json_dumps(hardware_obj),
                task_id,
                tenant_id,
                json_dumps(detail_obj),
                (requested_by or "").strip() or None,
                now,
                now,
            ),
        )
        self.observability.record_log(
            "provisioning.agent_requested",
            level="warning",
            layer="control_plane",
            source="provisioning",
            subject_type="agent_provisioning_request",
            subject_id=rid,
            detail={
                "reason": reason_value,
                "role_slug": role_slug,
                "capabilities": capabilities_list,
                "hardware": hardware_obj,
                "task_id": task_id,
                "tenant_id": tenant_id,
                **detail_obj,
            },
        )
        request = self.get_request(rid)
        self._notify_request_listeners(request)
        if self._provisioner is not None:
            try:
                fulfilled_agent_id = self._provisioner(request)
            except Exception:  # noqa: BLE001 - provisioner failures must not abort dispatch
                fulfilled_agent_id = None
                self.observability.record_log(
                    "provisioning.hook_failed",
                    level="error",
                    layer="control_plane",
                    source="provisioning",
                    subject_type="agent_provisioning_request",
                    subject_id=rid,
                    detail={"reason": "exception in provisioner hook"},
                )
            if fulfilled_agent_id:
                # Provisioner hook is trusted by configuration; it ran
                # in-process to satisfy the request it just observed.
                # Skip the two-party check (the hook IS the second party
                # by design) and the capability re-check. Still validate
                # resource-level toolchain requirements before closing the
                # request; otherwise a buggy provisioner can hide an unmet
                # command shortage while dispatch continues to reject it.
                try:
                    request = self.fulfill_request(rid, fulfilled_agent_id, allow_self_fulfill=True)
                except Exception as exc:  # noqa: BLE001 - keep provisioning signal pending
                    self.observability.record_log(
                        "provisioning.hook_failed",
                        level="error",
                        layer="control_plane",
                        source="provisioning",
                        subject_type="agent_provisioning_request",
                        subject_id=rid,
                        detail={
                            "reason": "provisioner returned unsuitable agent",
                            "agent_id": fulfilled_agent_id,
                            "error": str(exc),
                        },
                    )
                    request = self.get_request(rid)
        return request

    def get_request(self, request_id: str) -> AgentProvisioningRequest:
        row = self.store.query_one(
            "SELECT * FROM agent_provisioning_requests WHERE id = ?", (request_id,)
        )
        if row is None:
            raise NotFoundError("provisioning request not found: %s" % request_id)
        return self._from_row(row)

    def list_requests(
        self,
        *,
        status: Optional[str] = None,
        role_slug: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[AgentProvisioningRequest]:
        clauses: List[str] = []
        params: List[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if role_slug is not None:
            clauses.append("role_slug = ?")
            params.append(role_slug)
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        sql = "SELECT * FROM agent_provisioning_requests"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        return [self._from_row(r) for r in self.store.query_all(sql, tuple(params))]

    def list_pending_requests(self, **kwargs: Any) -> List[AgentProvisioningRequest]:
        return self.list_requests(status=ProvisioningStatus.PENDING.value, **kwargs)

    def fulfill_request(
        self,
        request_id: str,
        agent_id: str,
        *,
        fulfilled_by: Optional[str] = None,
        allow_self_fulfill: bool = False,
    ) -> AgentProvisioningRequest:
        """Mark a provisioning request fulfilled by ``agent_id``.

        mac-1oi4: ``fulfilled_by`` records who approved the request and is
        compared against ``requested_by`` — same-actor fulfillment is
        refused unless the caller passes ``allow_self_fulfill=True``
        (intended for auto-fulfill hooks that drive both ends from the
        same trusted control-plane process). The agent's capabilities
        and role are also checked against the request requirements so a
        compromised dispatcher cannot satisfy an arbitrary request with
        an unrelated attacker-controlled agent.
        """
        request = self.get_request(request_id)
        if not allow_self_fulfill:
            requester = (request.requested_by or "").strip()
            approver = (fulfilled_by or "").strip()
            if requester and approver and requester == approver:
                raise ValidationError(
                    "two-party check: %s requested and may not also fulfill request %s"
                    % (requester, request_id)
                )
        # Capability + role match. Skip those checks when the caller opted into
        # a self-fulfill flow (typically an auto-fulfill hook that just
        # registered the agent), but always enforce resource-level command
        # requirements so unmet toolchain shortages cannot be closed.
        if not allow_self_fulfill:
            self._assert_agent_matches_request(request, agent_id)
        else:
            self._assert_agent_commands_match_request(request, agent_id)
        return self._close_request(
            request_id,
            ProvisioningStatus.FULFILLED.value,
            fulfilled_agent_id=agent_id,
            detail_patch={"fulfilled_by": (fulfilled_by or "").strip() or None}
            if fulfilled_by
            else None,
        )

    def _assert_agent_matches_request(
        self, request: AgentProvisioningRequest, agent_id: str
    ) -> None:
        """Refuse to fulfill when the proposed agent doesn't satisfy the
        declared role/capability requirements (mac-1oi4)."""
        agent_row = self.store.query_one(
            "SELECT capabilities, role_id, resources FROM agents WHERE id = ?", (agent_id,)
        )
        if agent_row is None:
            raise NotFoundError("agent not found: %s" % agent_id)
        try:
            agent_caps = set(json_loads(agent_row["capabilities"], []))
        except Exception:
            agent_caps = set()
        missing = set(request.capabilities) - agent_caps
        if missing:
            raise ValidationError(
                "agent %s lacks required capabilities: %s" % (agent_id, sorted(missing))
            )
        if request.role_slug:
            assigned_role_id = agent_row["role_id"]
            if not assigned_role_id:
                raise ValidationError(
                    "agent %s has no assigned role; request needs %r"
                    % (agent_id, request.role_slug)
                )
            role_row = self.store.query_one(
                "SELECT slug FROM agent_roles WHERE id = ?", (assigned_role_id,)
            )
            if role_row is None or role_row["slug"] != request.role_slug:
                raise ValidationError(
                    "agent %s role %r does not match required %r"
                    % (
                        agent_id,
                        role_row["slug"] if role_row else None,
                        request.role_slug,
                    )
                )
        self._assert_agent_commands_match_request(request, agent_id, agent_row=agent_row)

    def _assert_agent_commands_match_request(
        self,
        request: AgentProvisioningRequest,
        agent_id: str,
        *,
        agent_row: Optional[Any] = None,
    ) -> None:
        required_commands = _request_required_commands(request)
        if not required_commands:
            return
        if agent_row is None:
            agent_row = self.store.query_one(
                "SELECT resources FROM agents WHERE id = ?", (agent_id,)
            )
            if agent_row is None:
                raise NotFoundError("agent not found: %s" % agent_id)
        raw_resources = json_loads(agent_row["resources"], {})
        available = _agent_resource_command_names(_json_object(raw_resources))
        missing = set(required_commands) - available
        if missing:
            raise ValidationError(
                "agent %s lacks required commands: %s" % (agent_id, sorted(missing))
            )

    def fail_request(self, request_id: str, *, reason: str) -> AgentProvisioningRequest:
        return self._close_request(
            request_id,
            ProvisioningStatus.FAILED.value,
            detail_patch={"failure_reason": reason},
        )

    def cancel_request(
        self, request_id: str, *, reason: str = "operator-cancelled"
    ) -> AgentProvisioningRequest:
        return self._close_request(
            request_id,
            ProvisioningStatus.CANCELLED.value,
            detail_patch={"cancel_reason": reason},
        )

    # Internal ----------------------------------------------------------

    def _close_request(
        self,
        request_id: str,
        new_status: str,
        *,
        fulfilled_agent_id: Optional[str] = None,
        detail_patch: Optional[Dict[str, Any]] = None,
    ) -> AgentProvisioningRequest:
        request = self.get_request(request_id)
        if request.status in PROVISIONING_TERMINAL_STATES:
            return request
        now = utcnow()
        merged = dict(request.detail)
        if detail_patch:
            merged.update(detail_patch)
        self.store.execute(
            """
            UPDATE agent_provisioning_requests
            SET status = ?, fulfilled_agent_id = ?, detail = ?, updated_at = ?, closed_at = ?
            WHERE id = ?
            """,
            (
                new_status,
                fulfilled_agent_id,
                json_dumps(merged),
                now,
                now,
                request.id,
            ),
        )
        self.observability.record_log(
            "provisioning.%s" % new_status,
            level="info",
            layer="control_plane",
            source="provisioning",
            subject_type="agent_provisioning_request",
            subject_id=request.id,
            detail={"fulfilled_agent_id": fulfilled_agent_id, **merged},
        )
        return self.get_request(request.id)

    def _from_row(self, row: Any) -> AgentProvisioningRequest:
        # sqlite Row supports .keys() — guard against migrated DBs where
        # requested_by may not yet exist.
        try:
            requested_by = row["requested_by"]
        except (IndexError, KeyError):
            requested_by = None
        return AgentProvisioningRequest(
            id=row["id"],
            status=row["status"],
            reason=row["reason"],
            role_slug=row["role_slug"],
            capabilities=json_loads(row["capabilities"], []),
            hardware=json_loads(row["hardware"], {}),
            task_id=row["task_id"],
            tenant_id=row["tenant_id"],
            detail=json_loads(row["detail"], {}),
            fulfilled_agent_id=row["fulfilled_agent_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            closed_at=row["closed_at"],
            requested_by=requested_by,
        )
