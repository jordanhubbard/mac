"""Service-role election: media services as leased role-claims (media-01).

A SERVICE (image/audio/video/ASR generation) is a leased role a capable host
CLAIMS — the same sticky-until-timeout-then-reclaimable lifecycle a task lease
has. ``service_roles`` is the desired state (what ops the cluster wants served);
``service_claims`` is the leased holder (mirrors ``leases``). Pool model: many
hosts may hold the same op; the DB unique-active index only stops one host from
double-holding it. Eligibility + capacity policy lives in the control plane
(services.py); this module is the pure CRUD + the atomic claim/renew/expire that
mirrors claim_task/renew_lease/expire_leases.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, List, Optional

from mac.models import (
    JsonDict,
    NotFoundError,
    ServiceClaim,
    ServiceClaimStatus,
    ServiceRole,
    coerce_list,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    parse_time,
    utcnow,
)
from mac.observability_service import ObservabilityService
from mac.store import StoreError


class ServiceRoleService:
    def __init__(self, store: Any, observability: ObservabilityService) -> None:
        self.store = store
        self.observability = observability

    # --- roles (desired services) --------------------------------------

    def upsert_role(
        self,
        op: str,
        *,
        slug: Optional[str] = None,
        model_id: Optional[str] = None,
        required_capabilities: Optional[List[str]] = None,
        hardware_requirements: Optional[JsonDict] = None,
        enabled: bool = True,
        tenant_id: Optional[str] = None,
        metadata: Optional[JsonDict] = None,
    ) -> ServiceRole:
        op = (op or "").strip()
        if not op:
            raise ValueError("service role op is required")
        slug = (slug or ("media:%s" % op)).strip()
        now = utcnow()
        rid = new_id("srole")
        self.store.execute(
            """
            INSERT INTO service_roles (
                id, op, slug, model_id, required_capabilities, hardware_requirements,
                enabled, tenant_id, metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug, tenant_id) DO UPDATE SET
                op = excluded.op,
                model_id = excluded.model_id,
                required_capabilities = excluded.required_capabilities,
                hardware_requirements = excluded.hardware_requirements,
                enabled = excluded.enabled,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                rid,
                op,
                slug,
                model_id,
                json_dumps(coerce_list(required_capabilities or [])),
                json_dumps(ensure_json_object(hardware_requirements or {})),
                1 if enabled else 0,
                tenant_id,
                json_dumps(ensure_json_object(metadata or {})),
                now,
                now,
            ),
        )
        return self.get_role_by_slug(slug, tenant_id=tenant_id)

    def get_role(self, role_id: str) -> ServiceRole:
        row = self.store.query_one("SELECT * FROM service_roles WHERE id = ?", (role_id,))
        if row is None:
            raise NotFoundError("service role %r not found" % role_id)
        return self._role_from_row(row)

    def get_role_by_slug(self, slug: str, *, tenant_id: Optional[str] = None) -> ServiceRole:
        # ``tenant_id IS ?`` is a SQLite null-safe comparison; Postgres rejects
        # it outright ("syntax error at or near $N"). Split into IS NULL / = ?,
        # matching provisioning_service and workflow_service.
        if tenant_id is None:
            row = self.store.query_one(
                "SELECT * FROM service_roles WHERE slug = ? AND tenant_id IS NULL",
                (slug,),
            )
        else:
            row = self.store.query_one(
                "SELECT * FROM service_roles WHERE slug = ? AND tenant_id = ?",
                (slug, tenant_id),
            )
        if row is None:
            raise NotFoundError("service role %r not found" % slug)
        return self._role_from_row(row)

    def desired_services(self, *, tenant_id: Optional[str] = None) -> List[ServiceRole]:
        # Same split as get_role_by_slug. With no tenant the second disjunct
        # already covers it, so the clause collapses to IS NULL.
        if tenant_id is None:
            rows = self.store.query_all(
                "SELECT * FROM service_roles WHERE enabled = 1 AND tenant_id IS NULL"
            )
        else:
            rows = self.store.query_all(
                "SELECT * FROM service_roles WHERE enabled = 1 "
                "AND (tenant_id = ? OR tenant_id IS NULL)",
                (tenant_id,),
            )
        return [self._role_from_row(r) for r in rows]

    # --- claims (leased holders) ---------------------------------------

    def claim_service(self, role_id: str, agent_id: str, lease_seconds: int = 1800) -> ServiceClaim:
        """Atomically claim (or renew) a host's hold on a service role. The
        unique-active index makes the INSERT the split-brain guard: a second
        active claim by the SAME agent for the SAME role is rejected (and renewed
        instead). Other agents may hold the same role (pool model)."""
        existing = self._active_claim(role_id, agent_id)
        if existing is not None:
            return self.renew_service_claim(existing.id, agent_id, lease_seconds)
        now = utcnow()
        expires_at = (parse_time(now) + timedelta(seconds=int(lease_seconds))).isoformat()
        cid = new_id("sclaim")
        # Two callers can both read no active claim above and both reach this
        # INSERT; the partial unique index (service_role_id, agent_id) WHERE
        # status = 'active' lets exactly one win. The loser must return the
        # winner's claim, because losing that race is normal pool behaviour and
        # not an error.
        #
        # This used to be an `except sqlite3.IntegrityError`. Under Postgres the
        # loser raises psycopg's UniqueViolation, which PostgresStore wraps as
        # StoreError, so that arm could never run: the benign race surfaced as a
        # hard failure of the split-brain guard instead of a renewal. Inferring
        # the same partial index in ON CONFLICT keeps the resolution inside the
        # statement, which is also the codebase's existing idiom.
        result = self.store.execute(
            """
            INSERT INTO service_claims (
                id, service_role_id, agent_id, status, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (service_role_id, agent_id) WHERE status = 'active'
            DO NOTHING
            """,
            (cid, role_id, agent_id, ServiceClaimStatus.ACTIVE.value, expires_at, now, now),
        )
        if not getattr(result, "rowcount", 1):
            existing = self._active_claim(role_id, agent_id)
            if existing is not None:
                return existing
            # ON CONFLICT fired but no active claim is visible, so the holder
            # released it in between. The index predicate and _active_claim's
            # filter are the same condition, so this is only ever a transient;
            # bounded rather than recursive so it cannot spin.
            raise StoreError(
                "service claim for role %s by agent %s conflicted with an active "
                "claim that was released before it could be read; retry the claim"
                % (role_id, agent_id)
            )
        return self._claim(cid)

    def renew_service_claim(
        self, claim_id: str, agent_id: str, lease_seconds: int = 1800
    ) -> ServiceClaim:
        now = utcnow()
        expires_at = (parse_time(now) + timedelta(seconds=int(lease_seconds))).isoformat()
        self.store.execute(
            "UPDATE service_claims SET expires_at = ?, updated_at = ? "
            "WHERE id = ? AND agent_id = ? AND status = ?",
            (expires_at, now, claim_id, agent_id, ServiceClaimStatus.ACTIVE.value),
        )
        return self._claim(claim_id)

    def release_service_claim(
        self, claim_id: str, agent_id: Optional[str] = None, *, reason: str = "released"
    ) -> None:
        now = utcnow()
        if agent_id is None:
            self.store.execute(
                "UPDATE service_claims SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (ServiceClaimStatus.RELEASED.value, now, claim_id, ServiceClaimStatus.ACTIVE.value),
            )
        else:
            self.store.execute(
                "UPDATE service_claims SET status = ?, updated_at = ? "
                "WHERE id = ? AND agent_id = ? AND status = ?",
                (
                    ServiceClaimStatus.RELEASED.value,
                    now,
                    claim_id,
                    agent_id,
                    ServiceClaimStatus.ACTIVE.value,
                ),
            )

    def expire_service_claims(
        self, now: Optional[str] = None, *, grace_seconds: int = 60
    ) -> List[ServiceClaim]:
        """Sweep active claims past expiry (holder stopped renewing = silent or
        overloaded) → status=expired, slot reopens. Race-guarded on status."""
        cutoff = (parse_time(utcnow()) - timedelta(seconds=int(grace_seconds))).isoformat()
        rows = self.store.query_all(
            "SELECT * FROM service_claims WHERE status = ? AND expires_at <= ?",
            (ServiceClaimStatus.ACTIVE.value, cutoff),
        )
        expired: List[ServiceClaim] = []
        stamp = utcnow()
        for row in rows:
            claim = self._claim_from_row(row)
            self.store.execute(
                "UPDATE service_claims SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (
                    ServiceClaimStatus.EXPIRED.value,
                    stamp,
                    claim.id,
                    ServiceClaimStatus.ACTIVE.value,
                ),
            )
            expired.append(claim)
        return expired

    def expire_agent_claims(
        self, agent_id: str, *, reason: str = "agent_offline"
    ) -> List[ServiceClaim]:
        """Expire all of an agent's active service claims (e.g. it went offline)."""
        rows = self.store.query_all(
            "SELECT * FROM service_claims WHERE agent_id = ? AND status = ?",
            (agent_id, ServiceClaimStatus.ACTIVE.value),
        )
        now = utcnow()
        out: List[ServiceClaim] = []
        for row in rows:
            claim = self._claim_from_row(row)
            self.store.execute(
                "UPDATE service_claims SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (ServiceClaimStatus.EXPIRED.value, now, claim.id, ServiceClaimStatus.ACTIVE.value),
            )
            out.append(claim)
        return out

    def list_active_claims(
        self, *, role_id: Optional[str] = None, agent_id: Optional[str] = None
    ) -> List[ServiceClaim]:
        sql = "SELECT * FROM service_claims WHERE status = ?"
        params: List[Any] = [ServiceClaimStatus.ACTIVE.value]
        if role_id is not None:
            sql += " AND service_role_id = ?"
            params.append(role_id)
        if agent_id is not None:
            sql += " AND agent_id = ?"
            params.append(agent_id)
        return [self._claim_from_row(r) for r in self.store.query_all(sql, tuple(params))]

    def held_ops_for_agent(self, agent_id: str) -> List[str]:
        rows = self.store.query_all(
            """
            SELECT r.op AS op FROM service_claims c
            JOIN service_roles r ON r.id = c.service_role_id
            WHERE c.agent_id = ? AND c.status = ?
            """,
            (agent_id, ServiceClaimStatus.ACTIVE.value),
        )
        return [str(r["op"]) for r in rows]

    # --- row mappers ---------------------------------------------------

    def _active_claim(self, role_id: str, agent_id: str) -> Optional[ServiceClaim]:
        row = self.store.query_one(
            "SELECT * FROM service_claims WHERE service_role_id = ? AND agent_id = ? AND status = ?",
            (role_id, agent_id, ServiceClaimStatus.ACTIVE.value),
        )
        return self._claim_from_row(row) if row is not None else None

    def _claim(self, claim_id: str) -> ServiceClaim:
        row = self.store.query_one("SELECT * FROM service_claims WHERE id = ?", (claim_id,))
        if row is None:
            raise NotFoundError("service claim %r not found" % claim_id)
        return self._claim_from_row(row)

    def _role_from_row(self, row: Any) -> ServiceRole:
        return ServiceRole(
            id=row["id"],
            op=row["op"],
            slug=row["slug"],
            model_id=row["model_id"],
            required_capabilities=json_loads(row["required_capabilities"], []),
            hardware_requirements=json_loads(row["hardware_requirements"], {}),
            enabled=bool(row["enabled"]),
            tenant_id=row["tenant_id"],
            metadata=json_loads(row["metadata"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _claim_from_row(self, row: Any) -> ServiceClaim:
        return ServiceClaim(
            id=row["id"],
            service_role_id=row["service_role_id"],
            agent_id=row["agent_id"],
            status=row["status"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
