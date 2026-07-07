"""Runtime-neutral public identities and durable OpenClaw delivery.

Internal fleet agents are intentionally not public chat identities.  A small,
stable set of logical identities (for example ``mac-hive``) owns provider
accounts and represents any number of workers.  Provider credentials never
appear in this service: accounts store vault *references* only, while a fenced
gateway lease selects the one OpenClaw sandbox allowed to consume an account.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from mac.models import (
    CommunicationAccount,
    CommunicationIdentity,
    GatewayIdentityLease,
    HumanMessageDelivery,
    NotFoundError,
    RepresentationBinding,
    TransitionError,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)


IDENTITY_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)
CHANNELS = {
    "discord",
    "googlechat",
    "imessage",
    "matrix",
    "mattermost",
    "msteams",
    "signal",
    "slack",
    "telegram",
    "whatsapp",
}
SUBJECT_KINDS = {"agent", "role", "project", "fleet"}
REPRESENTATION_MODES = {"direct", "delegated", "internal_only"}
DELIVERY_TERMINAL = {"delivered", "failed", "cancelled"}


def _clean_name(value: str, field: str) -> str:
    cleaned = str(value or "").strip().lower()
    if not cleaned:
        raise ValidationError("%s is required" % field)
    if len(cleaned) > 128 or any(char not in IDENTITY_NAME_CHARS for char in cleaned):
        raise ValidationError(
            "%s must contain only letters, numbers, dot, dash, or underscore" % field
        )
    return cleaned


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class CommunicationService:
    """Identity registry, representation resolver, gateway leases, and outbox."""

    def __init__(
        self,
        store: Any,
        *,
        get_agent: Callable[[str], Any],
        get_task: Callable[[str], Any],
        record_log: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.store = store
        self._get_agent = get_agent
        self._get_task = get_task
        self._record_log = record_log

    # Identities ---------------------------------------------------------

    def configure_identity(
        self,
        name: str,
        *,
        display_name: str = "",
        description: str = "",
        is_default: bool = False,
        enabled: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
        identity_id: Optional[str] = None,
    ) -> CommunicationIdentity:
        name_value = _clean_name(name, "identity name")
        row = self.store.query_one(
            "SELECT id FROM communication_identities WHERE name = ?", (name_value,)
        )
        iid = str(row["id"]) if row is not None else identity_id or new_id("commid")
        now = utcnow()
        with self.store.transaction() as conn:
            if is_default:
                conn.execute(
                    "UPDATE communication_identities SET is_default = 0, updated_at = ? WHERE is_default = 1 AND id != ?",
                    (now, iid),
                )
            conn.execute(
                """
                INSERT INTO communication_identities (
                    id, name, display_name, description, is_default, enabled,
                    metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    display_name = excluded.display_name,
                    description = excluded.description,
                    is_default = excluded.is_default,
                    enabled = excluded.enabled,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (
                    iid,
                    name_value,
                    str(display_name or name_value).strip() or name_value,
                    str(description or "").strip(),
                    1 if is_default else 0,
                    1 if enabled else 0,
                    json_dumps(ensure_json_object(metadata)),
                    now,
                    now,
                ),
            )
        self._observe("communication.identity.configured", iid, {"name": name_value})
        return self.get_identity(iid)

    def get_identity(self, identity_id_or_name: str) -> CommunicationIdentity:
        row = self.store.query_one(
            "SELECT * FROM communication_identities WHERE id = ? OR name = ?",
            (identity_id_or_name, str(identity_id_or_name).strip().lower()),
        )
        if row is None:
            raise NotFoundError("communication identity not found: %s" % identity_id_or_name)
        return self._identity_from_row(row)

    def list_identities(self, enabled: Optional[bool] = None) -> List[CommunicationIdentity]:
        if enabled is None:
            rows = self.store.query_all(
                "SELECT * FROM communication_identities ORDER BY is_default DESC, name"
            )
        else:
            rows = self.store.query_all(
                "SELECT * FROM communication_identities WHERE enabled = ? ORDER BY is_default DESC, name",
                (1 if enabled else 0,),
            )
        return [self._identity_from_row(row) for row in rows]

    def delete_identity(self, identity_id_or_name: str) -> None:
        identity = self.get_identity(identity_id_or_name)
        delivery = self.store.query_one(
            "SELECT id FROM human_message_deliveries WHERE identity_id = ? LIMIT 1",
            (identity.id,),
        )
        if delivery is not None:
            raise TransitionError(
                "identity %s has human-message delivery history; disable it instead"
                % identity.name
            )
        active_lease = self.store.query_one(
            """
            SELECT l.id FROM gateway_identity_leases l
            JOIN communication_accounts a ON a.id = l.account_id
            WHERE a.identity_id = ? AND l.leased_until > ? LIMIT 1
            """,
            (identity.id, utcnow()),
        )
        if active_lease is not None:
            raise TransitionError("identity has an active gateway lease")
        self.store.execute("DELETE FROM communication_identities WHERE id = ?", (identity.id,))
        self._observe("communication.identity.deleted", identity.id, {"name": identity.name})

    # Provider accounts --------------------------------------------------

    def configure_account(
        self,
        identity_id: str,
        channel: str,
        account_id: str = "default",
        *,
        credential_refs: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None,
        enabled: bool = True,
        record_id: Optional[str] = None,
    ) -> CommunicationAccount:
        identity = self.get_identity(identity_id)
        channel_value = _clean_name(channel, "channel")
        if channel_value not in CHANNELS:
            raise ValidationError("unsupported OpenClaw channel: %s" % channel)
        account_value = _clean_name(account_id or "default", "account id")
        row = self.store.query_one(
            "SELECT id FROM communication_accounts WHERE identity_id = ? AND channel = ? AND account_id = ?",
            (identity.id, channel_value, account_value),
        )
        rid = str(row["id"]) if row is not None else record_id or new_id("commacct")
        now = utcnow()
        self.store.execute(
            """
            INSERT INTO communication_accounts (
                id, identity_id, channel, account_id, credential_refs, config,
                enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                identity_id = excluded.identity_id,
                channel = excluded.channel,
                account_id = excluded.account_id,
                credential_refs = excluded.credential_refs,
                config = excluded.config,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (
                rid,
                identity.id,
                channel_value,
                account_value,
                json_dumps(ensure_json_object(credential_refs)),
                json_dumps(ensure_json_object(config)),
                1 if enabled else 0,
                now,
                now,
            ),
        )
        self._observe(
            "communication.account.configured",
            rid,
            {"identity_id": identity.id, "channel": channel_value, "account_id": account_value},
        )
        return self.get_account(rid)

    def get_account(self, account_record_id: str) -> CommunicationAccount:
        row = self.store.query_one(
            "SELECT * FROM communication_accounts WHERE id = ?", (account_record_id,)
        )
        if row is None:
            raise NotFoundError("communication account not found: %s" % account_record_id)
        return self._account_from_row(row)

    def list_accounts(
        self,
        *,
        identity_id: Optional[str] = None,
        channel: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> List[CommunicationAccount]:
        clauses: List[str] = []
        params: List[Any] = []
        if identity_id:
            identity_id = self.get_identity(identity_id).id
            clauses.append("identity_id = ?")
            params.append(identity_id)
        if channel:
            clauses.append("channel = ?")
            params.append(str(channel).strip().lower())
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(1 if enabled else 0)
        sql = "SELECT * FROM communication_accounts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY identity_id, channel, account_id"
        return [self._account_from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def delete_account(self, account_record_id: str) -> None:
        account = self.get_account(account_record_id)
        delivery = self.store.query_one(
            "SELECT id FROM human_message_deliveries WHERE account_id = ? LIMIT 1",
            (account.id,),
        )
        if delivery is not None:
            raise TransitionError(
                "communication account has delivery history; disable it instead"
            )
        active_lease = self.store.query_one(
            "SELECT id FROM gateway_identity_leases WHERE account_id = ? AND leased_until > ? LIMIT 1",
            (account.id, utcnow()),
        )
        if active_lease is not None:
            raise TransitionError("communication account has an active gateway lease")
        self.store.execute("DELETE FROM communication_accounts WHERE id = ?", (account.id,))

    # Representation ----------------------------------------------------

    def configure_representation(
        self,
        subject_kind: str,
        subject_id: str,
        *,
        identity_id: Optional[str] = None,
        mode: str = "delegated",
        priority: int = 100,
        enabled: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
        binding_id: Optional[str] = None,
    ) -> RepresentationBinding:
        kind = str(subject_kind or "").strip().lower()
        if kind not in SUBJECT_KINDS:
            raise ValidationError("unsupported representation subject_kind: %s" % subject_kind)
        subject = str(subject_id or "").strip()
        if kind == "fleet" and not subject:
            subject = "default"
        if not subject:
            raise ValidationError("representation subject_id is required")
        mode_value = str(mode or "delegated").strip().lower()
        if mode_value not in REPRESENTATION_MODES:
            raise ValidationError("unsupported representation mode: %s" % mode)
        resolved_identity_id: Optional[str] = None
        if mode_value != "internal_only":
            if not identity_id:
                raise ValidationError("identity_id is required unless mode=internal_only")
            resolved_identity_id = self.get_identity(identity_id).id
        elif identity_id:
            raise ValidationError("internal_only representation cannot specify identity_id")
        if kind == "agent":
            self._get_agent(subject)
        row = self.store.query_one(
            "SELECT id FROM representation_bindings WHERE subject_kind = ? AND subject_id = ?",
            (kind, subject),
        )
        bid = str(row["id"]) if row is not None else binding_id or new_id("repr")
        now = utcnow()
        self.store.execute(
            """
            INSERT INTO representation_bindings (
                id, subject_kind, subject_id, identity_id, mode, priority,
                enabled, metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                subject_kind = excluded.subject_kind,
                subject_id = excluded.subject_id,
                identity_id = excluded.identity_id,
                mode = excluded.mode,
                priority = excluded.priority,
                enabled = excluded.enabled,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                bid,
                kind,
                subject,
                resolved_identity_id,
                mode_value,
                int(priority),
                1 if enabled else 0,
                json_dumps(ensure_json_object(metadata)),
                now,
                now,
            ),
        )
        return self.get_representation(bid)

    def get_representation(self, binding_id: str) -> RepresentationBinding:
        row = self.store.query_one(
            "SELECT * FROM representation_bindings WHERE id = ?", (binding_id,)
        )
        if row is None:
            raise NotFoundError("representation binding not found: %s" % binding_id)
        return self._binding_from_row(row)

    def list_representations(
        self,
        *,
        subject_kind: Optional[str] = None,
        identity_id: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> List[RepresentationBinding]:
        clauses: List[str] = []
        params: List[Any] = []
        if subject_kind:
            clauses.append("subject_kind = ?")
            params.append(str(subject_kind).strip().lower())
        if identity_id:
            clauses.append("identity_id = ?")
            params.append(self.get_identity(identity_id).id)
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(1 if enabled else 0)
        sql = "SELECT * FROM representation_bindings"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority, subject_kind, subject_id"
        return [self._binding_from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def delete_representation(self, binding_id: str) -> None:
        self.get_representation(binding_id)
        self.store.execute("DELETE FROM representation_bindings WHERE id = ?", (binding_id,))

    def resolve_representation(
        self,
        agent_id: str,
        *,
        project: Optional[str] = None,
        role: Optional[str] = None,
        fleet: str = "default",
    ) -> Dict[str, Any]:
        agent = self._get_agent(agent_id)
        candidates = [
            ("agent", agent.id),
            ("role", str(role or "").strip()),
            ("project", str(project or "").strip()),
            ("fleet", str(fleet or "default").strip() or "default"),
        ]
        for kind, subject in candidates:
            if not subject:
                continue
            row = self.store.query_one(
                """
                SELECT * FROM representation_bindings
                WHERE subject_kind = ? AND subject_id = ? AND enabled = 1
                ORDER BY priority, id LIMIT 1
                """,
                (kind, subject),
            )
            if row is not None:
                binding = self._binding_from_row(row)
                return self._resolved_representation(agent.id, binding)
        row = self.store.query_one(
            "SELECT * FROM communication_identities WHERE is_default = 1 AND enabled = 1 LIMIT 1"
        )
        if row is None:
            return {
                "agent_id": agent.id,
                "mode": "internal_only",
                "identity": None,
                "binding": None,
                "reason": "no_enabled_representation_or_default_identity",
            }
        identity = self._identity_from_row(row)
        return {
            "agent_id": agent.id,
            "mode": "delegated",
            "identity": identity.to_dict(),
            "binding": None,
            "reason": "default_identity",
        }

    # Gateway account leases --------------------------------------------

    def acquire_gateway_lease(
        self,
        account_id: str,
        agent_id: str,
        *,
        lease_seconds: int = 90,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> GatewayIdentityLease:
        account = self.get_account(account_id)
        if not account.enabled:
            raise TransitionError("communication account is disabled: %s" % account.id)
        self._get_agent(agent_id)
        ttl = min(max(15, int(lease_seconds)), 3600)
        now = utcnow()
        leased_until = _future(ttl)
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM gateway_identity_leases WHERE account_id = ?",
                (account.id,),
            ).fetchone()
            if row is not None and str(row["leased_until"]) > now and row["agent_id"] != agent_id:
                raise TransitionError(
                    "channel account lease is held by %s until %s"
                    % (row["agent_id"], row["leased_until"])
                )
            if row is not None and row["agent_id"] == agent_id:
                lease_id = str(row["id"])
                fencing_token = str(row["fencing_token"])
                created_at = str(row["created_at"])
            else:
                lease_id = new_id("gwlease")
                fencing_token = new_id("fence")
                created_at = now
            conn.execute(
                """
                INSERT INTO gateway_identity_leases (
                    id, account_id, agent_id, fencing_token, leased_until,
                    metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    id = excluded.id,
                    agent_id = excluded.agent_id,
                    fencing_token = excluded.fencing_token,
                    leased_until = excluded.leased_until,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (
                    lease_id,
                    account.id,
                    agent_id,
                    fencing_token,
                    leased_until,
                    json_dumps(ensure_json_object(metadata)),
                    created_at,
                    now,
                ),
            )
        self._observe(
            "communication.gateway_lease.acquired",
            lease_id,
            {"account_id": account.id, "agent_id": agent_id, "leased_until": leased_until},
        )
        return self.get_gateway_lease(lease_id)

    def renew_gateway_lease(
        self,
        lease_id: str,
        agent_id: str,
        fencing_token: str,
        *,
        lease_seconds: int = 90,
    ) -> GatewayIdentityLease:
        lease = self.get_gateway_lease(lease_id)
        if lease.agent_id != agent_id or lease.fencing_token != fencing_token:
            raise TransitionError("gateway lease ownership or fencing token mismatch")
        ttl = min(max(15, int(lease_seconds)), 3600)
        self.store.execute(
            "UPDATE gateway_identity_leases SET leased_until = ?, updated_at = ? WHERE id = ? AND agent_id = ? AND fencing_token = ?",
            (_future(ttl), utcnow(), lease.id, agent_id, fencing_token),
        )
        return self.get_gateway_lease(lease.id)

    def release_gateway_lease(
        self, lease_id: str, agent_id: str, fencing_token: str
    ) -> None:
        lease = self.get_gateway_lease(lease_id)
        if lease.agent_id != agent_id or lease.fencing_token != fencing_token:
            raise TransitionError("gateway lease ownership or fencing token mismatch")
        self.store.execute("DELETE FROM gateway_identity_leases WHERE id = ?", (lease.id,))

    def get_gateway_lease(self, lease_id: str) -> GatewayIdentityLease:
        row = self.store.query_one(
            "SELECT * FROM gateway_identity_leases WHERE id = ?", (lease_id,)
        )
        if row is None:
            raise NotFoundError("gateway identity lease not found: %s" % lease_id)
        return self._lease_from_row(row)

    def list_gateway_leases(
        self, *, agent_id: Optional[str] = None, active_only: bool = False
    ) -> List[GatewayIdentityLease]:
        clauses: List[str] = []
        params: List[Any] = []
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if active_only:
            clauses.append("leased_until > ?")
            params.append(utcnow())
        sql = "SELECT * FROM gateway_identity_leases"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY account_id"
        return [self._lease_from_row(row) for row in self.store.query_all(sql, tuple(params))]

    # Durable human-message outbox --------------------------------------

    def enqueue_delivery(
        self,
        target: str,
        body: str,
        *,
        origin_agent_id: Optional[str] = None,
        identity_id: Optional[str] = None,
        account_id: Optional[str] = None,
        channel: Optional[str] = None,
        task_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        max_attempts: int = 5,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> HumanMessageDelivery:
        target_value = str(target or "").strip()
        body_value = str(body or "").strip()
        if not target_value or not body_value:
            raise ValidationError("human delivery target and body are required")
        if origin_agent_id:
            self._get_agent(origin_agent_id)
        if task_id:
            self._get_task(task_id)
        if identity_id:
            identity = self.get_identity(identity_id)
        elif origin_agent_id:
            resolution = self.resolve_representation(origin_agent_id)
            if not resolution.get("identity"):
                raise TransitionError("origin agent is internal-only and has no representative")
            identity = self.get_identity(str(resolution["identity"]["id"]))
        else:
            default = self.store.query_one(
                "SELECT * FROM communication_identities WHERE is_default = 1 AND enabled = 1 LIMIT 1"
            )
            if default is None:
                raise TransitionError("no default communication identity is configured")
            identity = self._identity_from_row(default)
        if not identity.enabled:
            raise TransitionError("communication identity is disabled: %s" % identity.name)
        account = self._resolve_account(identity.id, account_id=account_id, channel=channel)
        key = str(idempotency_key or new_id("humanmsg")).strip()
        existing = self.store.query_one(
            "SELECT * FROM human_message_deliveries WHERE idempotency_key = ?", (key,)
        )
        if existing is not None:
            return self._delivery_from_row(existing)
        now = utcnow()
        delivery_id = new_id("delivery")
        self.store.execute(
            """
            INSERT INTO human_message_deliveries (
                id, identity_id, account_id, channel, target, body,
                origin_agent_id, task_id, idempotency_key, status,
                attempt_count, max_attempts, delivery_agent_id,
                delivery_lease_id, leased_until, provider_message_id,
                last_error, metadata, created_at, updated_at, delivered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL,
                      NULL, NULL, NULL, ?, ?, ?, NULL)
            """,
            (
                delivery_id,
                identity.id,
                account.id,
                account.channel,
                target_value,
                body_value,
                origin_agent_id,
                task_id,
                key,
                min(max(1, int(max_attempts)), 20),
                json_dumps(ensure_json_object(metadata)),
                now,
                now,
            ),
        )
        self._observe(
            "communication.delivery.queued",
            delivery_id,
            {"identity_id": identity.id, "account_id": account.id, "origin_agent_id": origin_agent_id},
        )
        return self.get_delivery(delivery_id)

    def claim_deliveries(
        self,
        agent_id: str,
        *,
        limit: int = 20,
        lease_seconds: int = 60,
    ) -> List[HumanMessageDelivery]:
        self._get_agent(agent_id)
        now = utcnow()
        expires = _future(min(max(15, int(lease_seconds)), 600))
        claimed: List[str] = []
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE human_message_deliveries
                SET status = 'pending', delivery_agent_id = NULL,
                    delivery_lease_id = NULL, leased_until = NULL,
                    updated_at = ?
                WHERE status = 'delivering' AND leased_until < ?
                  AND attempt_count < max_attempts
                """,
                (now, now),
            )
            rows = conn.execute(
                """
                SELECT d.id, l.id AS gateway_lease_id
                FROM human_message_deliveries d
                JOIN communication_accounts a ON a.id = d.account_id
                JOIN gateway_identity_leases l ON l.account_id = a.id
                WHERE d.status = 'pending' AND a.enabled = 1
                  AND l.agent_id = ? AND l.leased_until > ?
                ORDER BY d.created_at, d.id
                LIMIT ?
                """,
                (agent_id, now, min(max(1, int(limit)), 100)),
            ).fetchall()
            for row in rows:
                cursor = conn.execute(
                    """
                    UPDATE human_message_deliveries
                    SET status = 'delivering', attempt_count = attempt_count + 1,
                        delivery_agent_id = ?, delivery_lease_id = ?,
                        leased_until = ?, updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (agent_id, row["gateway_lease_id"], expires, now, row["id"]),
                )
                if cursor.rowcount == 1:
                    claimed.append(str(row["id"]))
        return [self.get_delivery(item) for item in claimed]

    def acknowledge_delivery(
        self,
        delivery_id: str,
        agent_id: str,
        *,
        provider_message_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> HumanMessageDelivery:
        delivery = self.get_delivery(delivery_id)
        if delivery.status == "delivered":
            return delivery
        if delivery.status != "delivering" or delivery.delivery_agent_id != agent_id:
            raise TransitionError("human delivery is not leased to agent %s" % agent_id)
        metadata = dict(delivery.metadata)
        if detail:
            metadata["provider_receipt"] = ensure_json_object(detail)
        now = utcnow()
        self.store.execute(
            """
            UPDATE human_message_deliveries
            SET status = 'delivered', provider_message_id = ?, metadata = ?,
                leased_until = NULL, updated_at = ?, delivered_at = ?
            WHERE id = ? AND status = 'delivering' AND delivery_agent_id = ?
            """,
            (provider_message_id, json_dumps(metadata), now, now, delivery.id, agent_id),
        )
        self._observe("communication.delivery.delivered", delivery.id, {"agent_id": agent_id})
        return self.get_delivery(delivery.id)

    def fail_delivery(
        self,
        delivery_id: str,
        agent_id: str,
        error: str,
        *,
        retryable: bool = True,
    ) -> HumanMessageDelivery:
        delivery = self.get_delivery(delivery_id)
        if delivery.status in DELIVERY_TERMINAL:
            return delivery
        if delivery.status != "delivering" or delivery.delivery_agent_id != agent_id:
            raise TransitionError("human delivery is not leased to agent %s" % agent_id)
        status = (
            "pending"
            if retryable and delivery.attempt_count < delivery.max_attempts
            else "failed"
        )
        now = utcnow()
        self.store.execute(
            """
            UPDATE human_message_deliveries
            SET status = ?, last_error = ?, delivery_agent_id = NULL,
                delivery_lease_id = NULL, leased_until = NULL, updated_at = ?
            WHERE id = ? AND status = 'delivering' AND delivery_agent_id = ?
            """,
            (status, str(error or "delivery failed")[:2000], now, delivery.id, agent_id),
        )
        self._observe(
            "communication.delivery.failed" if status == "failed" else "communication.delivery.retry",
            delivery.id,
            {"agent_id": agent_id, "attempt_count": delivery.attempt_count},
        )
        return self.get_delivery(delivery.id)

    def get_delivery(self, delivery_id: str) -> HumanMessageDelivery:
        row = self.store.query_one(
            "SELECT * FROM human_message_deliveries WHERE id = ?", (delivery_id,)
        )
        if row is None:
            raise NotFoundError("human message delivery not found: %s" % delivery_id)
        return self._delivery_from_row(row)

    def list_deliveries(
        self,
        *,
        status: Optional[str] = None,
        identity_id: Optional[str] = None,
        origin_agent_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[HumanMessageDelivery]:
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(str(status).strip().lower())
        if identity_id:
            clauses.append("identity_id = ?")
            params.append(self.get_identity(identity_id).id)
        if origin_agent_id:
            clauses.append("origin_agent_id = ?")
            params.append(origin_agent_id)
        sql = "SELECT * FROM human_message_deliveries"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(min(max(1, int(limit)), 1000))
        return [self._delivery_from_row(row) for row in self.store.query_all(sql, tuple(params))]

    # Helpers ------------------------------------------------------------

    def _resolve_account(
        self,
        identity_id: str,
        *,
        account_id: Optional[str],
        channel: Optional[str],
    ) -> CommunicationAccount:
        if account_id:
            account = self.get_account(account_id)
            if account.identity_id != identity_id:
                raise ValidationError("communication account does not belong to identity")
            if channel and account.channel != str(channel).strip().lower():
                raise ValidationError("communication account channel mismatch")
            if not account.enabled:
                raise TransitionError("communication account is disabled")
            return account
        clauses = ["identity_id = ?", "enabled = 1"]
        params: List[Any] = [identity_id]
        if channel:
            clauses.append("channel = ?")
            params.append(str(channel).strip().lower())
        rows = self.store.query_all(
            "SELECT * FROM communication_accounts WHERE %s ORDER BY account_id, id"
            % " AND ".join(clauses),
            tuple(params),
        )
        if not rows:
            raise TransitionError("identity has no enabled matching communication account")
        accounts = [self._account_from_row(row) for row in rows]
        defaults = [item for item in accounts if bool(item.config.get("default"))]
        if defaults:
            return defaults[0]
        if len(accounts) > 1 and not channel:
            raise ValidationError("channel or account_id is required for a multi-channel identity")
        return accounts[0]

    def _resolved_representation(
        self, agent_id: str, binding: RepresentationBinding
    ) -> Dict[str, Any]:
        identity = (
            self.get_identity(binding.identity_id).to_dict()
            if binding.identity_id
            else None
        )
        return {
            "agent_id": agent_id,
            "mode": binding.mode,
            "identity": identity,
            "binding": binding.to_dict(),
            "reason": "explicit_%s_binding" % binding.subject_kind,
        }

    def _observe(self, name: str, subject_id: str, detail: Mapping[str, Any]) -> None:
        if self._record_log is None:
            return
        self._record_log(
            name,
            layer="control_plane",
            source="communication",
            subject_type="communication",
            subject_id=subject_id,
            detail=dict(detail),
        )

    @staticmethod
    def _identity_from_row(row: Any) -> CommunicationIdentity:
        return CommunicationIdentity(
            str(row["id"]),
            str(row["name"]),
            str(row["display_name"]),
            str(row["description"]),
            bool(row["is_default"]),
            bool(row["enabled"]),
            json_loads(row["metadata"], {}),
            str(row["created_at"]),
            str(row["updated_at"]),
        )

    @staticmethod
    def _account_from_row(row: Any) -> CommunicationAccount:
        return CommunicationAccount(
            str(row["id"]),
            str(row["identity_id"]),
            str(row["channel"]),
            str(row["account_id"]),
            json_loads(row["credential_refs"], {}),
            json_loads(row["config"], {}),
            bool(row["enabled"]),
            str(row["created_at"]),
            str(row["updated_at"]),
        )

    @staticmethod
    def _binding_from_row(row: Any) -> RepresentationBinding:
        return RepresentationBinding(
            str(row["id"]),
            str(row["subject_kind"]),
            str(row["subject_id"]),
            str(row["identity_id"]) if row["identity_id"] is not None else None,
            str(row["mode"]),
            int(row["priority"]),
            bool(row["enabled"]),
            json_loads(row["metadata"], {}),
            str(row["created_at"]),
            str(row["updated_at"]),
        )

    @staticmethod
    def _lease_from_row(row: Any) -> GatewayIdentityLease:
        return GatewayIdentityLease(
            str(row["id"]),
            str(row["account_id"]),
            str(row["agent_id"]),
            str(row["fencing_token"]),
            str(row["leased_until"]),
            json_loads(row["metadata"], {}),
            str(row["created_at"]),
            str(row["updated_at"]),
        )

    @staticmethod
    def _delivery_from_row(row: Any) -> HumanMessageDelivery:
        return HumanMessageDelivery(
            str(row["id"]),
            str(row["identity_id"]),
            str(row["account_id"]) if row["account_id"] is not None else None,
            str(row["channel"]) if row["channel"] is not None else None,
            str(row["target"]),
            str(row["body"]),
            str(row["origin_agent_id"]) if row["origin_agent_id"] is not None else None,
            str(row["task_id"]) if row["task_id"] is not None else None,
            str(row["idempotency_key"]),
            str(row["status"]),
            int(row["attempt_count"]),
            int(row["max_attempts"]),
            str(row["delivery_agent_id"]) if row["delivery_agent_id"] is not None else None,
            str(row["delivery_lease_id"]) if row["delivery_lease_id"] is not None else None,
            str(row["leased_until"]) if row["leased_until"] is not None else None,
            str(row["provider_message_id"]) if row["provider_message_id"] is not None else None,
            str(row["last_error"]) if row["last_error"] is not None else None,
            json_loads(row["metadata"], {}),
            str(row["created_at"]),
            str(row["updated_at"]),
            str(row["delivered_at"]) if row["delivered_at"] is not None else None,
        )
