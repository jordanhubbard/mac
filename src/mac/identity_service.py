"""Identity / Hermes-boundary service.

Owns ``tenants``, ``users``, ``personas``, ``hermes_instances``, and
``platform_bindings``. The Hermes context endpoint also lives here — it
returns the operational provenance contract that mac records while leaving
personality and user-memory authority with Hermes.

``create_interaction_task`` (which spans identity + task creation) stays on
``ControlPlane`` because it composes ``create_task``; this service exposes
the identity lookups it needs.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from mac.models import (
    HermesInstance,
    HermesInstanceStatus,
    JsonDict,
    NotFoundError,
    Persona,
    PlatformBinding,
    Tenant,
    User,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)


def _state_value(state: Any) -> str:
    return state.value if hasattr(state, "value") else str(state)


class IdentityService:
    def __init__(self, store: Any) -> None:
        self.store = store

    # Tenants -----------------------------------------------------------

    def register_tenant(
        self,
        name: str,
        metadata: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
    ) -> Tenant:
        name = name.strip()
        if not name:
            raise ValidationError("tenant name is required")
        existing = self.store.query_one("SELECT id FROM tenants WHERE name = ?", (name,))
        if existing is not None and tenant_id is None:
            tenant_id = existing["id"]
        now = utcnow()
        tid = tenant_id or new_id("tenant")
        metadata_json = self._resolved_json_column("tenants", "metadata", tid, metadata)
        self.store.execute(
            """
            INSERT INTO tenants (id, name, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (tid, name, metadata_json, now, now),
        )
        return self.get_tenant(tid)

    def get_tenant(self, tenant_id_or_name: str) -> Tenant:
        row = self.store.query_one(
            "SELECT * FROM tenants WHERE id = ? OR name = ?",
            (tenant_id_or_name, tenant_id_or_name),
        )
        if row is None:
            raise NotFoundError("tenant not found: %s" % tenant_id_or_name)
        return self._tenant_from_row(row)

    def list_tenants(self) -> List[Tenant]:
        rows = self.store.query_all("SELECT * FROM tenants ORDER BY name")
        return [self._tenant_from_row(row) for row in rows]

    # Users -------------------------------------------------------------

    def register_user(
        self,
        tenant_id: str,
        handle: str,
        display_name: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
    ) -> User:
        self.get_tenant(tenant_id)
        handle = handle.strip()
        if not handle:
            raise ValidationError("user handle is required")
        existing = self.store.query_one(
            "SELECT id FROM users WHERE tenant_id = ? AND handle = ?",
            (tenant_id, handle),
        )
        if existing is not None and user_id is None:
            user_id = existing["id"]
        now = utcnow()
        uid = user_id or new_id("user")
        metadata_json = self._resolved_json_column("users", "metadata", uid, metadata)
        self.store.execute(
            """
            INSERT INTO users (id, tenant_id, handle, display_name, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                handle = excluded.handle,
                display_name = excluded.display_name,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                uid,
                tenant_id,
                handle,
                display_name or handle,
                metadata_json,
                now,
                now,
            ),
        )
        return self.get_user(uid)

    def get_user(self, user_id: str) -> User:
        row = self.store.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None:
            raise NotFoundError("user not found: %s" % user_id)
        return self._user_from_row(row)

    def list_users(self, tenant_id: Optional[str] = None) -> List[User]:
        if tenant_id:
            rows = self.store.query_all(
                "SELECT * FROM users WHERE tenant_id = ? ORDER BY handle",
                (tenant_id,),
            )
        else:
            rows = self.store.query_all("SELECT * FROM users ORDER BY tenant_id, handle")
        return [self._user_from_row(row) for row in rows]

    # Personas ----------------------------------------------------------

    def register_persona(
        self,
        tenant_id: str,
        name: str,
        soul_ref: str,
        memory_scope: str,
        metadata: Optional[Dict[str, Any]] = None,
        persona_id: Optional[str] = None,
    ) -> Persona:
        self.get_tenant(tenant_id)
        if not name.strip():
            raise ValidationError("persona name is required")
        if not soul_ref.strip():
            raise ValidationError("persona soul_ref is required")
        if not memory_scope.strip():
            raise ValidationError("persona memory_scope is required")
        name = name.strip()
        existing = self.store.query_one(
            "SELECT id FROM personas WHERE tenant_id = ? AND name = ?",
            (tenant_id, name),
        )
        if existing is not None and persona_id is None:
            persona_id = existing["id"]
        now = utcnow()
        pid = persona_id or new_id("persona")
        metadata_json = self._resolved_json_column("personas", "metadata", pid, metadata)
        self.store.execute(
            """
            INSERT INTO personas (
                id, tenant_id, name, soul_ref, memory_scope, metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                name = excluded.name,
                soul_ref = excluded.soul_ref,
                memory_scope = excluded.memory_scope,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                pid,
                tenant_id,
                name,
                soul_ref.strip(),
                memory_scope.strip(),
                metadata_json,
                now,
                now,
            ),
        )
        return self.get_persona(pid)

    def get_persona(self, persona_id: str) -> Persona:
        row = self.store.query_one("SELECT * FROM personas WHERE id = ?", (persona_id,))
        if row is None:
            raise NotFoundError("persona not found: %s" % persona_id)
        return self._persona_from_row(row)

    def list_personas(self, tenant_id: Optional[str] = None) -> List[Persona]:
        if tenant_id:
            rows = self.store.query_all(
                "SELECT * FROM personas WHERE tenant_id = ? ORDER BY name",
                (tenant_id,),
            )
        else:
            rows = self.store.query_all("SELECT * FROM personas ORDER BY tenant_id, name")
        return [self._persona_from_row(row) for row in rows]

    # Hermes instances --------------------------------------------------

    def register_hermes_instance(
        self,
        tenant_id: str,
        name: str,
        persona_id: Optional[str] = None,
        home_ref: str = "",
        status: str = HermesInstanceStatus.ACTIVE.value,
        metadata: Optional[Dict[str, Any]] = None,
        instance_id: Optional[str] = None,
    ) -> HermesInstance:
        self.get_tenant(tenant_id)
        if persona_id:
            persona = self.get_persona(persona_id)
            if persona.tenant_id != tenant_id:
                raise ValidationError("persona must belong to hermes instance tenant")
        name = name.strip()
        if not name:
            raise ValidationError("hermes instance name is required")
        existing = self.store.query_one(
            "SELECT id FROM hermes_instances WHERE tenant_id = ? AND name = ?",
            (tenant_id, name),
        )
        if existing is not None and instance_id is None:
            instance_id = existing["id"]
        status_value = _state_value(status)
        try:
            HermesInstanceStatus(status_value)
        except ValueError:
            raise ValidationError("unsupported hermes instance status: %s" % status_value)
        now = utcnow()
        hid = instance_id or new_id("hermes")
        metadata_json = self._resolved_json_column("hermes_instances", "metadata", hid, metadata)
        self.store.execute(
            """
            INSERT INTO hermes_instances (
                id, tenant_id, name, persona_id, home_ref, status,
                metadata, created_at, updated_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                name = excluded.name,
                persona_id = excluded.persona_id,
                home_ref = excluded.home_ref,
                status = excluded.status,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at,
                last_seen_at = excluded.last_seen_at
            """,
            (
                hid,
                tenant_id,
                name,
                persona_id,
                home_ref,
                status_value,
                metadata_json,
                now,
                now,
                now,
            ),
        )
        return self.get_hermes_instance(hid)

    def get_hermes_instance(self, instance_id: str) -> HermesInstance:
        row = self.store.query_one(
            "SELECT * FROM hermes_instances WHERE id = ?", (instance_id,)
        )
        if row is None:
            raise NotFoundError("hermes instance not found: %s" % instance_id)
        return self._hermes_instance_from_row(row)

    def list_hermes_instances(self, tenant_id: Optional[str] = None) -> List[HermesInstance]:
        if tenant_id:
            rows = self.store.query_all(
                "SELECT * FROM hermes_instances WHERE tenant_id = ? ORDER BY name",
                (tenant_id,),
            )
        else:
            rows = self.store.query_all(
                "SELECT * FROM hermes_instances ORDER BY tenant_id, name"
            )
        return [self._hermes_instance_from_row(row) for row in rows]

    # Platform bindings ------------------------------------------------

    def register_platform_binding(
        self,
        tenant_id: str,
        hermes_instance_id: str,
        platform: str,
        external_id: str,
        display_name: str = "",
        scopes: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        binding_id: Optional[str] = None,
    ) -> PlatformBinding:
        self.get_tenant(tenant_id)
        instance = self.get_hermes_instance(hermes_instance_id)
        if instance.tenant_id != tenant_id:
            raise ValidationError("platform binding must belong to hermes instance tenant")
        if not platform.strip() or not external_id.strip():
            raise ValidationError("platform and external_id are required")
        platform = platform.strip()
        external_id = external_id.strip()
        existing = self.store.query_one(
            "SELECT id FROM platform_bindings WHERE tenant_id = ? AND platform = ? AND external_id = ?",
            (tenant_id, platform, external_id),
        )
        if existing is not None and binding_id is None:
            binding_id = existing["id"]
        now = utcnow()
        bid = binding_id or new_id("binding")
        scopes_json = self._resolved_json_column("platform_bindings", "scopes", bid, scopes)
        metadata_json = self._resolved_json_column("platform_bindings", "metadata", bid, metadata)
        self.store.execute(
            """
            INSERT INTO platform_bindings (
                id, tenant_id, hermes_instance_id, platform, external_id,
                display_name, scopes, metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                hermes_instance_id = excluded.hermes_instance_id,
                platform = excluded.platform,
                external_id = excluded.external_id,
                display_name = excluded.display_name,
                scopes = excluded.scopes,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                bid,
                tenant_id,
                hermes_instance_id,
                platform,
                external_id,
                display_name or external_id,
                scopes_json,
                metadata_json,
                now,
                now,
            ),
        )
        return self.get_platform_binding(bid)

    def get_platform_binding(self, binding_id: str) -> PlatformBinding:
        row = self.store.query_one(
            "SELECT * FROM platform_bindings WHERE id = ?", (binding_id,)
        )
        if row is None:
            raise NotFoundError("platform binding not found: %s" % binding_id)
        return self._platform_binding_from_row(row)

    def list_platform_bindings(
        self,
        tenant_id: Optional[str] = None,
        hermes_instance_id: Optional[str] = None,
    ) -> List[PlatformBinding]:
        clauses = []
        params: List[Any] = []
        if tenant_id:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if hermes_instance_id:
            clauses.append("hermes_instance_id = ?")
            params.append(hermes_instance_id)
        sql = "SELECT * FROM platform_bindings"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY platform, external_id"
        rows = self.store.query_all(sql, tuple(params))
        return [self._platform_binding_from_row(row) for row in rows]

    # Hermes operational context ---------------------------------------

    def hermes_context(self, hermes_instance_id: str) -> JsonDict:
        instance = self.get_hermes_instance(hermes_instance_id)
        persona = self.get_persona(instance.persona_id) if instance.persona_id else None
        return {
            "tenant": self.get_tenant(instance.tenant_id).to_dict(),
            "hermes_instance": instance.to_dict(),
            "persona": persona.to_dict() if persona else None,
            "platform_bindings": [
                binding.to_dict()
                for binding in self.list_platform_bindings(
                    tenant_id=instance.tenant_id,
                    hermes_instance_id=instance.id,
                )
            ],
            "memory_contract": {
                "personality_authority": "hermes",
                "user_memory_authority": "hermes",
                "operational_provenance_authority": "mac",
                "soul_ref": persona.soul_ref if persona else None,
                "memory_scope": persona.memory_scope if persona else None,
            },
        }

    # Shared JSON-column upsert helper --------------------------------

    def _resolved_json_column(
        self,
        table: str,
        column: str,
        row_id: str,
        value: Optional[Dict[str, Any]],
    ) -> str:
        """Resolve a JSON column for register-style upserts.

        If the caller explicitly passed a value, use it. Otherwise preserve
        the existing row's value (so re-registering with no metadata does
        not wipe previously-stored metadata). Defaults to {} for new rows.
        """
        if value is not None:
            return json_dumps(ensure_json_object(value))
        row = self.store.query_one(
            "SELECT %s AS value FROM %s WHERE id = ?" % (column, table),
            (row_id,),
        )
        if row is None or row["value"] is None:
            return json_dumps({})
        return row["value"]

    # Row hydration ----------------------------------------------------

    def _tenant_from_row(self, row: Any) -> Tenant:
        return Tenant(
            row["id"],
            row["name"],
            json_loads(row["metadata"], {}),
            row["created_at"],
            row["updated_at"],
        )

    def _user_from_row(self, row: Any) -> User:
        return User(
            row["id"],
            row["tenant_id"],
            row["handle"],
            row["display_name"],
            json_loads(row["metadata"], {}),
            row["created_at"],
            row["updated_at"],
        )

    def _persona_from_row(self, row: Any) -> Persona:
        return Persona(
            row["id"],
            row["tenant_id"],
            row["name"],
            row["soul_ref"],
            row["memory_scope"],
            json_loads(row["metadata"], {}),
            row["created_at"],
            row["updated_at"],
        )

    def _hermes_instance_from_row(self, row: Any) -> HermesInstance:
        return HermesInstance(
            row["id"],
            row["tenant_id"],
            row["name"],
            row["persona_id"],
            row["home_ref"],
            row["status"],
            json_loads(row["metadata"], {}),
            row["created_at"],
            row["updated_at"],
            row["last_seen_at"],
        )

    def _platform_binding_from_row(self, row: Any) -> PlatformBinding:
        return PlatformBinding(
            row["id"],
            row["tenant_id"],
            row["hermes_instance_id"],
            row["platform"],
            row["external_id"],
            row["display_name"],
            json_loads(row["scopes"], {}),
            json_loads(row["metadata"], {}),
            row["created_at"],
            row["updated_at"],
        )
