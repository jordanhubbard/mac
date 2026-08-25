"""Secrets domain service.

Owns the ``secrets`` and ``secret_access_audit`` tables and the encryption
boundary. Plaintext values cross the boundary exactly twice per lifecycle:
once at ``create_secret``/``rotate_secret`` (caller → ciphertext) and once
at ``reveal_secret`` (ciphertext → caller, single-use, time-limited).

The service does not own the Fernet key — ``ControlPlane`` derives it from
``MAC_SECRET_KEY`` and passes it in. Agent/machine lookups are also injected
as callables so the service can stay decoupled from the rest of the control
plane.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional

from cryptography.fernet import Fernet, InvalidToken

from mac.models import (
    Agent,
    AuthorizationError,
    JsonDict,
    MACError,
    Machine,
    NotFoundError,
    SecretAccess,
    SecretAuditResult,
    SecretHandle,
    SecretRecord,
    ValidationError,
    json_dumps,
    json_loads,
    new_id,
    parse_time,
    utcnow,
)
from mac.observability_service import ObservabilityService

SECRET_HANDLE_DEFAULT_TTL_SECONDS = 300


class SecretsService:
    def __init__(
        self,
        store: Any,
        observability: ObservabilityService,
        fernet: Fernet,
        *,
        get_agent: Callable[[str], Agent],
        get_machine: Callable[[str], Machine],
        machine_allows_tenant: Callable[[Machine, Optional[str]], bool],
    ) -> None:
        self.store = store
        self.observability = observability
        self._fernet = fernet
        self._get_agent = get_agent
        self._get_machine = get_machine
        self._machine_allows_tenant = machine_allows_tenant

    # Public API ---------------------------------------------------------

    def create_secret(
        self,
        name: str,
        value: str,
        scopes: Dict[str, Any],
        created_by: str,
    ) -> SecretRecord:
        if not name or not value:
            raise ValidationError("secret name and value are required")
        if not scopes:
            raise ValidationError("secret scopes are required")
        now = utcnow()
        secret_id = new_id("secret")
        self.store.execute(
            """
            INSERT INTO secrets (id, name, scopes, ciphertext, created_by, created_at, updated_at, rotated_at, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 1)
            """,
            (secret_id, name, json_dumps(scopes), self._encrypt(value), created_by, now, now),
        )
        return self.get_secret(secret_id)

    def get_secret(self, secret_id_or_name: str) -> SecretRecord:
        row = self.store.query_one(
            "SELECT * FROM secrets WHERE id = ? OR name = ?",
            (secret_id_or_name, secret_id_or_name),
        )
        if row is None:
            raise NotFoundError("secret not found: %s" % secret_id_or_name)
        return self._secret_from_row(row)

    def list_secrets(self) -> List[SecretRecord]:
        rows = self.store.query_all("SELECT * FROM secrets ORDER BY name")
        return [self._secret_from_row(row) for row in rows]

    def request_secret(
        self,
        secret_id_or_name: str,
        accessor_agent_id: str,
        purpose: str,
        ttl_seconds: int = SECRET_HANDLE_DEFAULT_TTL_SECONDS,
    ) -> SecretHandle:
        secret = self.get_secret(secret_id_or_name)
        agent = self._get_agent(accessor_agent_id)
        machine = self._get_machine(agent.machine_id)
        granted = bool(
            secret.enabled and machine.trusted and self._scope_allows(secret.scopes, agent)
        )
        expires_at: Optional[str] = None
        if granted:
            ttl = max(1, int(ttl_seconds))
            expires_at = (parse_time(utcnow()) + timedelta(seconds=ttl)).isoformat(
                timespec="microseconds"
            )
        audit = self.record_access(
            secret.id,
            accessor_agent_id,
            purpose,
            SecretAuditResult.GRANTED.value if granted else SecretAuditResult.DENIED.value,
            expires_at=expires_at,
        )
        if not granted:
            raise AuthorizationError("secret access denied")
        return SecretHandle(secret.id, audit.id, "secret://%s#%s" % (secret.id, audit.id), True)

    def rotate_secret(self, secret_id_or_name: str, value: str, actor: str) -> SecretRecord:
        if not value:
            raise ValidationError("rotation requires a new secret value")
        secret = self.get_secret(secret_id_or_name)
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE secrets SET ciphertext = ?, updated_at = ?, rotated_at = ? WHERE id = ?",
                (self._encrypt(value), now, now, secret.id),
            )
            conn.execute(
                """
                INSERT INTO secret_access_audit (
                    id, secret_id, accessor_agent_id, purpose, result, expires_at, revealed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    new_id("audit"),
                    secret.id,
                    actor or "unspecified",
                    "rotate",
                    SecretAuditResult.ROTATED.value,
                    now,
                ),
            )
        return self.get_secret(secret.id)

    def delete_secret(self, secret_id_or_name: str, actor: str = "operator") -> Dict[str, Any]:
        """Hard-delete a secret: scrub its ciphertext + remove the row so the value
        is gone and the name can be reused (used to clean up stale/decommissioned
        secrets, e.g. a spoke's now-unused router key after key centralization).
        Raises NotFoundError if absent. Its access-audit rows cascade away; the
        deletion is recorded by the caller (CLI/API log)."""
        secret = self.get_secret(secret_id_or_name)
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM secrets WHERE id = ?", (secret.id,))
        return {
            "id": secret.id,
            "name": secret.name,
            "deleted": True,
            "deleted_by": actor or "operator",
        }

    def list_audits(self, secret_id: Optional[str] = None) -> List[SecretAccess]:
        if secret_id:
            rows = self.store.query_all(
                "SELECT * FROM secret_access_audit WHERE secret_id = ? ORDER BY created_at, id",
                (secret_id,),
            )
        else:
            rows = self.store.query_all("SELECT * FROM secret_access_audit ORDER BY created_at, id")
        return [self._access_from_row(row) for row in rows]

    def reveal_secret(self, secret_id: str, audit_id: str, accessor_agent_id: str) -> str:
        """Single-use, time-limited secret reveal.

        The grant audit row must (1) name the same agent that is asking,
        (2) still be within its TTL, and (3) not already have been revealed.
        On success the audit row is marked revealed so the same handle cannot
        be redeemed twice.
        """
        now = utcnow()
        with self.store.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE secret_access_audit
                SET revealed_at = ?
                WHERE id = ?
                  AND secret_id = ?
                  AND accessor_agent_id = ?
                  AND result = ?
                  AND revealed_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (
                    now,
                    audit_id,
                    secret_id,
                    accessor_agent_id,
                    SecretAuditResult.GRANTED.value,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise AuthorizationError(
                    "secret handle is expired, already used, or not granted to this agent"
                )
            row = conn.execute(
                "SELECT ciphertext FROM secrets WHERE id = ? AND enabled = 1",
                (secret_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("secret not found or disabled: %s" % secret_id)
        return self._decrypt(row["ciphertext"])

    def resolve_secret_value(
        self,
        name: str,
        *,
        purpose: str = "router",
        accessor: str = "router",
    ) -> Optional[str]:
        """Audited, in-process resolution of a secret's plaintext for a consumer
        co-located with the control plane on the hub — specifically the model
        router (th-merge-04) fetching an upstream provider key.

        ``request_secret``/``reveal_secret`` gate *remote* agents behind a
        single-use, time-limited handle. This is different: the hub is
        decrypting a secret it already owns, so there is no handle dance — but it
        is still audited (every upstream-key use is traceable) and respects the
        ``enabled`` flag. Returns ``None`` when the secret is absent or disabled
        so the caller can fall back to env or fail the provider, rather than
        raising into the request path."""
        try:
            secret = self.get_secret(name)
        except NotFoundError:
            return None
        if not secret.enabled:
            return None
        self.record_access(secret.id, accessor, purpose, SecretAuditResult.GRANTED.value)
        row = self.store.query_one(
            "SELECT ciphertext FROM secrets WHERE id = ? AND enabled = 1", (secret.id,)
        )
        if row is None:
            return None
        return self._decrypt(row["ciphertext"])

    # Audit + scope helpers ---------------------------------------------

    def record_access(
        self,
        secret_id: str,
        accessor_agent_id: str,
        purpose: str,
        result: str,
        expires_at: Optional[str] = None,
    ) -> SecretAccess:
        audit_id = new_id("audit")
        when = utcnow()
        self.store.execute(
            """
            INSERT INTO secret_access_audit (
                id, secret_id, accessor_agent_id, purpose, result, expires_at, revealed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                audit_id,
                secret_id,
                accessor_agent_id,
                purpose or "unspecified",
                result,
                expires_at,
                when,
            ),
        )
        self.observability.insert_observation(
            self.store,
            "log",
            "secret.%s" % result,
            "control_plane",
            "secret",
            "warning" if result == SecretAuditResult.DENIED.value else "info",
            None,
            "",
            "secret",
            secret_id,
            {
                "accessor_agent_id": accessor_agent_id,
                "purpose": purpose or "unspecified",
                "expires_at": expires_at,
            },
            when,
        )
        row = self.store.query_one("SELECT * FROM secret_access_audit WHERE id = ?", (audit_id,))
        if row is None:
            raise NotFoundError("secret audit not found: %s" % audit_id)
        return self._access_from_row(row)

    def _scope_allows(self, scopes: JsonDict, agent: Agent) -> bool:
        agents = set(scopes.get("agents") or [])
        capabilities = set(scopes.get("capabilities") or [])
        tenant_scope = set(scopes.get("tenant_ids") or [])
        if scopes.get("tenant_id"):
            tenant_scope.add(str(scopes["tenant_id"]))
        if tenant_scope:
            machine = self._get_machine(agent.machine_id)
            if not any(
                self._machine_allows_tenant(machine, tenant_id) for tenant_id in tenant_scope
            ):
                return False
        if agent.id in agents:
            return True
        if capabilities and capabilities.intersection(set(agent.capabilities)):
            return True
        # Tenant-only scope: if the caller scoped solely by tenant, the tenant
        # check above is the entire gate. Without this, tenant-only secrets
        # are unreachable.
        if tenant_scope and not agents and not capabilities:
            return True
        return False

    # Encryption boundary -----------------------------------------------

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise MACError("secret ciphertext failed authentication") from exc

    # Row hydration ------------------------------------------------------

    def _secret_from_row(self, row: Any) -> SecretRecord:
        return SecretRecord(
            row["id"],
            row["name"],
            json_loads(row["scopes"], {}),
            row["created_by"],
            row["created_at"],
            row["updated_at"],
            row["rotated_at"],
            bool(row["enabled"]),
        )

    def _access_from_row(self, row: Any) -> SecretAccess:
        return SecretAccess(
            row["id"],
            row["secret_id"],
            row["accessor_agent_id"],
            row["purpose"],
            row["result"],
            row["expires_at"],
            row["revealed_at"],
            row["created_at"],
        )
