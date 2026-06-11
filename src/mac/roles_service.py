"""Agent role catalog + assignment service.

Roles are persona templates that bundle a system prompt, capability
defaults, and (optionally) hardware requirements. An agent's
``role_id`` references a row in this catalog; the dispatcher consults
the role to enforce role-required capabilities and hardware
constraints (see ``services.py`` for dispatch integration).

Seeding: ``seed_defaults()`` reads ``src/mac/data/roles/loom_seed.json``
(generated from loom by ``scripts/import_loom_roles.py``) and upserts
each row under ``tenant_id IS NULL``. Operators can also POST custom
roles, tenant-scoped or global.

Hardware shape (machine.hardware vs. role.hardware_requirements) is
documented in ``docs/agent-roles.md``; the matcher here is permissive
on unknown keys so new hardware traits stay forward-compatible.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from mac.models import (
    Agent,
    AgentRole,
    HermesInstance,
    JsonDict,
    Machine,
    NotFoundError,
    Persona,
    ROLE_LEVELS,
    Tenant,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.observability_service import ObservabilityService

ROLE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
SEED_CATALOG = Path(__file__).resolve().parent / "data" / "roles" / "loom_seed.json"

_NUMERIC_MINS = {"cpu_count_min", "memory_gb_min", "disk_gb_min"}


class RolesService:
    def __init__(
        self,
        store: Any,
        observability: ObservabilityService,
        *,
        get_tenant: Callable[[str], Tenant],
        get_agent: Callable[[str], Agent],
        get_machine: Callable[[str], Machine],
        get_hermes_instance: Optional[Callable[[str], HermesInstance]] = None,
        get_persona: Optional[Callable[[str], Persona]] = None,
    ) -> None:
        self.store = store
        self.observability = observability
        self._get_tenant = get_tenant
        self._get_agent = get_agent
        self._get_machine = get_machine
        # Injected so RolesService can enforce soul-role compatibility
        # without depending on IdentityService directly. ControlPlane
        # wires these in; tests that don't touch souls can leave them
        # None.
        self._get_hermes_instance = get_hermes_instance
        self._get_persona = get_persona

    # CRUD ---------------------------------------------------------------

    def create_role(
        self,
        slug: str,
        name: str,
        description: str,
        system_prompt: str,
        level: str,
        *,
        display_name: Optional[str] = None,
        reports_to: Optional[str] = None,
        specialties: Optional[Iterable[str]] = None,
        default_capabilities: Optional[Iterable[str]] = None,
        required_capabilities: Optional[Iterable[str]] = None,
        hardware_requirements: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
        is_default: bool = False,
        role_id: Optional[str] = None,
    ) -> AgentRole:
        slug_value = self._validate_slug(slug)
        name_value = (name or "").strip()
        if not name_value:
            raise ValidationError("role name is required")
        description_value = (description or "").strip()
        if not description_value:
            raise ValidationError("role description is required")
        system_prompt_value = (system_prompt or "").strip()
        if not system_prompt_value:
            raise ValidationError("role system_prompt is required")
        level_value = (level or "").strip().lower()
        if level_value not in ROLE_LEVELS:
            raise ValidationError(
                "unsupported role level: %s (allowed: %s)"
                % (level, ", ".join(sorted(ROLE_LEVELS)))
            )
        if tenant_id is not None:
            self._get_tenant(tenant_id)
        if reports_to is not None:
            # Resolve to confirm the parent exists in the same tenant.
            parent = self.get_role(reports_to, tenant_id=tenant_id)
            reports_to = parent.id
        if tenant_id is None:
            existing = self.store.query_one(
                "SELECT id FROM agent_roles WHERE slug = ? AND tenant_id IS NULL",
                (slug_value,),
            )
        else:
            existing = self.store.query_one(
                "SELECT id FROM agent_roles WHERE slug = ? AND tenant_id = ?",
                (slug_value, tenant_id),
            )
        if existing is not None and role_id is None:
            role_id = existing["id"]
        rid = role_id or new_id("role")
        now = utcnow()
        specialties_list = [str(s).strip() for s in (specialties or []) if str(s).strip()]
        default_caps_list = [str(c).strip() for c in (default_capabilities or []) if str(c).strip()]
        required_caps_list = [str(c).strip() for c in (required_capabilities or []) if str(c).strip()]
        hw = self._validate_hardware_requirements(hardware_requirements or {})
        self.store.execute(
            """
            INSERT INTO agent_roles (
                id, slug, name, display_name, description, system_prompt,
                level, reports_to, specialties, default_capabilities,
                required_capabilities, hardware_requirements, metadata,
                is_default, tenant_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                slug = excluded.slug,
                name = excluded.name,
                display_name = excluded.display_name,
                description = excluded.description,
                system_prompt = excluded.system_prompt,
                level = excluded.level,
                reports_to = excluded.reports_to,
                specialties = excluded.specialties,
                default_capabilities = excluded.default_capabilities,
                required_capabilities = excluded.required_capabilities,
                hardware_requirements = excluded.hardware_requirements,
                metadata = excluded.metadata,
                is_default = excluded.is_default,
                tenant_id = excluded.tenant_id,
                updated_at = excluded.updated_at
            """,
            (
                rid,
                slug_value,
                name_value,
                display_name,
                description_value,
                system_prompt_value,
                level_value,
                reports_to,
                json_dumps(specialties_list),
                json_dumps(default_caps_list),
                json_dumps(required_caps_list),
                json_dumps(hw),
                json_dumps(ensure_json_object(metadata)),
                1 if is_default else 0,
                tenant_id,
                now,
                now,
            ),
        )
        return self.get_role(rid)

    def update_role(self, role_id: str, **patch: Any) -> AgentRole:
        role = self.get_role(role_id)
        updates = dict(
            slug=role.slug,
            name=role.name,
            description=role.description,
            system_prompt=role.system_prompt,
            level=role.level,
            display_name=role.display_name,
            reports_to=role.reports_to,
            specialties=role.specialties,
            default_capabilities=role.default_capabilities,
            required_capabilities=role.required_capabilities,
            hardware_requirements=role.hardware_requirements,
            metadata=role.metadata,
            tenant_id=role.tenant_id,
            is_default=role.is_default,
        )
        updates.update({k: v for k, v in patch.items() if v is not None})
        return self.create_role(role_id=role.id, **updates)

    def get_role(
        self,
        role_id_or_slug: str,
        *,
        tenant_id: Optional[str] = None,
    ) -> AgentRole:
        """Resolve by id first; fall back to slug.

        Slug lookup prefers the caller's ``tenant_id`` (if set) and falls
        back to the global row (``tenant_id IS NULL``) so tenant-specific
        overrides naturally shadow defaults.
        """
        row = self.store.query_one(
            "SELECT * FROM agent_roles WHERE id = ?", (role_id_or_slug,)
        )
        if row is None and tenant_id is not None:
            row = self.store.query_one(
                "SELECT * FROM agent_roles WHERE slug = ? AND tenant_id = ?",
                (role_id_or_slug, tenant_id),
            )
        if row is None:
            row = self.store.query_one(
                "SELECT * FROM agent_roles WHERE slug = ? AND tenant_id IS NULL",
                (role_id_or_slug,),
            )
        if row is None:
            raise NotFoundError("role not found: %s" % role_id_or_slug)
        return self._from_row(row)

    def list_roles(
        self,
        *,
        tenant_id: Optional[str] = None,
        level: Optional[str] = None,
        include_defaults: bool = True,
    ) -> List[AgentRole]:
        clauses: List[str] = []
        params: List[Any] = []
        if tenant_id is not None and include_defaults:
            clauses.append("(tenant_id = ? OR tenant_id IS NULL)")
            params.append(tenant_id)
        elif tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if level is not None:
            clauses.append("level = ?")
            params.append(level.lower())
        sql = "SELECT * FROM agent_roles"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY tenant_id, slug"
        rows = self.store.query_all(sql, tuple(params))
        return [self._from_row(row) for row in rows]

    def delete_role(self, role_id: str) -> None:
        role = self.get_role(role_id)
        assigned = self.store.query_one(
            "SELECT id FROM agents WHERE role_id = ? LIMIT 1", (role.id,)
        )
        if assigned is not None:
            raise ValidationError(
                "role %s cannot be deleted while agents are assigned to it" % role.slug
            )
        self.store.execute("DELETE FROM agent_roles WHERE id = ?", (role.id,))

    # Assignment --------------------------------------------------------

    def assign_role(self, agent_id: str, role_id_or_slug: str) -> Agent:
        agent = self._get_agent(agent_id)
        role = self.get_role(role_id_or_slug)
        # Soul takes precedence over role. An agent without a hermes
        # instance (no soul) cannot take any role — they're free workers
        # without an identity to attach a persona to. An agent whose
        # soul's allowed_role_slugs doesn't include this role is also
        # refused; agents come and go, the task simply waits for one
        # whose soul accepts the role.
        allowed = self._allowed_role_slugs_for(agent)
        if allowed is None:
            raise ValidationError(
                "agent %s has no soul (hermes_instance_id); assign one before "
                "binding a role" % agent.id
            )
        if role.slug not in allowed:
            raise ValidationError(
                "soul does not accept role %s (allowed: %s)"
                % (role.slug, ", ".join(sorted(allowed)) or "<none>")
            )
        machine = self._get_machine(agent.machine_id)
        ok, reasons = self.validate_hardware(role, machine)
        if not ok:
            raise ValidationError(
                "machine %s does not satisfy role %s hardware requirements: %s"
                % (machine.hostname, role.slug, "; ".join(reasons))
            )
        # Merge role's default capabilities into the agent's set, preserving
        # the agent's own caps. We don't auto-add ``required_capabilities``
        # — those gate dispatch, not registration.
        merged = sorted(set(agent.capabilities) | set(role.default_capabilities))
        now = utcnow()
        detail = {
            "actor": "human",
            "agent_id": agent.id,
            "role_id": role.id,
            "role_slug": role.slug,
            "added_capabilities": sorted(set(merged) - set(agent.capabilities)),
        }
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE agents
                SET role_id = ?, capabilities = ?, updated_at = ?
                WHERE id = ?
                """,
                (role.id, json_dumps(merged), now, agent.id),
            )
            conn.execute(
                """
                INSERT INTO agent_lifecycle_events (id, agent_id, event_type, actor, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (new_id("alce"), agent.id, "agent.role_assigned", "human", json_dumps(detail), now),
            )
            self.observability.insert_observation(
                conn,
                "log",
                "agent.role_assigned",
                "control_plane",
                "roles",
                "info",
                None,
                "",
                "agent",
                agent.id,
                detail,
                now,
            )
        return self._get_agent(agent.id)

    def unassign_role(self, agent_id: str) -> Agent:
        agent = self._get_agent(agent_id)
        if agent.role_id is None:
            return agent
        now = utcnow()
        detail = {
            "actor": "human",
            "agent_id": agent.id,
            "previous_role_id": agent.role_id,
        }
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE agents SET role_id = NULL, updated_at = ? WHERE id = ?",
                (now, agent.id),
            )
            conn.execute(
                """
                INSERT INTO agent_lifecycle_events (id, agent_id, event_type, actor, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (new_id("alce"), agent.id, "agent.role_unassigned", "human", json_dumps(detail), now),
            )
            self.observability.insert_observation(
                conn,
                "log",
                "agent.role_unassigned",
                "control_plane",
                "roles",
                "info",
                None,
                "",
                "agent",
                agent.id,
                detail,
                now,
            )
        return self._get_agent(agent.id)

    # Soul-role compatibility ------------------------------------------

    def _allowed_role_slugs_for(self, agent: Agent) -> Optional[List[str]]:
        """Return the role slugs an agent's soul accepts.

        ``None`` means the agent has no soul (no ``hermes_instance_id``)
        and is therefore refused for any role per the layering rule.
        An empty list means the soul exists but accepts no roles.
        Otherwise, the persona's ``metadata.role_slugs`` is honored, and
        if absent the persona's name (slugified) is the default — loom
        personas map 1-to-1 to roles by name, so the default is exactly
        right for the seeded fleet.
        """
        if not agent.hermes_instance_id:
            return None
        if self._get_hermes_instance is None or self._get_persona is None:
            # No identity wiring (tests, dev). Be permissive when an
            # operator has gone out of their way to set a soul but the
            # service wasn't given identity callables.
            return []  # type: ignore[return-value]
        try:
            instance = self._get_hermes_instance(agent.hermes_instance_id)
        except NotFoundError:
            return None
        if not instance.persona_id:
            return []
        try:
            persona = self._get_persona(instance.persona_id)
        except NotFoundError:
            return []
        explicit = persona.metadata.get("role_slugs") if isinstance(persona.metadata, dict) else None
        if isinstance(explicit, list) and explicit:
            return [str(s).strip().lower() for s in explicit if str(s).strip()]
        # Default: derive from persona name (loom seed convention).
        default = persona.name.strip().lower().replace(" ", "-").replace("_", "-")
        return [default] if default else []

    def soul_accepts_role(self, agent: Agent, role: AgentRole) -> bool:
        allowed = self._allowed_role_slugs_for(agent)
        if allowed is None:
            return False
        return role.slug in allowed

    # Hardware matcher --------------------------------------------------

    def validate_hardware(
        self, role: AgentRole, machine: Machine
    ) -> Tuple[bool, List[str]]:
        """Pure-function check: does ``machine.hardware`` satisfy
        ``role.hardware_requirements``? Returns ``(ok, reasons)``."""
        return machine_hardware_satisfies(role.hardware_requirements, machine.hardware)

    # Seeding -----------------------------------------------------------

    def seed_defaults(
        self,
        *,
        replace: bool = False,
        source: Optional[Path] = None,
    ) -> List[AgentRole]:
        """Idempotently insert the loom-derived role catalog.

        Without ``replace``, existing rows (same slug, ``tenant_id IS NULL``)
        keep their current state. With ``replace=True``, default-marked
        fields (system_prompt, specialties, default_capabilities,
        description, hardware_requirements) are overwritten — operator-edited
        fields like ``required_capabilities`` are still preserved.
        """
        path = source or SEED_CATALOG
        if not path.exists():
            raise NotFoundError("role seed catalog missing: %s" % path)
        catalog = json.loads(path.read_text(encoding="utf-8"))
        # Two passes: insert all roles first with ``reports_to=None``, then
        # set the parent links. This lets the catalog reference parents that
        # appear later in the list.
        ids: Dict[str, str] = {}
        for entry in catalog:
            existing = self.store.query_one(
                "SELECT * FROM agent_roles WHERE slug = ? AND tenant_id IS NULL",
                (entry["slug"],),
            )
            if existing is not None and not replace:
                ids[entry["slug"]] = existing["id"]
                continue
            row = self.create_role(
                slug=entry["slug"],
                name=entry.get("name", entry["slug"]),
                description=entry.get("description") or entry.get("name", entry["slug"]),
                system_prompt=entry.get("system_prompt") or entry.get("name", entry["slug"]),
                level=entry.get("level", "ic"),
                display_name=entry.get("display_name"),
                specialties=entry.get("specialties") or [],
                default_capabilities=entry.get("default_capabilities") or [],
                required_capabilities=entry.get("required_capabilities") or [],
                hardware_requirements=entry.get("hardware_requirements") or {},
                metadata=entry.get("metadata") or {"source": "loom"},
                tenant_id=None,
                is_default=True,
            )
            ids[entry["slug"]] = row.id
        for entry in catalog:
            parent_slug = entry.get("reports_to")
            if not parent_slug or parent_slug not in ids:
                continue
            self.store.execute(
                "UPDATE agent_roles SET reports_to = ?, updated_at = ? WHERE id = ?",
                (ids[parent_slug], utcnow(), ids[entry["slug"]]),
            )
        return [self.get_role(rid) for rid in ids.values()]

    # Validation helpers -----------------------------------------------

    def _validate_slug(self, slug: str) -> str:
        value = (slug or "").strip().lower()
        if not ROLE_SLUG_RE.match(value):
            raise ValidationError("invalid role slug: %s" % slug)
        return value

    def _validate_hardware_requirements(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError("hardware_requirements must be an object")
        # Permissive: keep unknown keys so the schema stays forward-compatible.
        for key, value in raw.items():
            if key in _NUMERIC_MINS:
                if not isinstance(value, (int, float)) or float(value) < 0:
                    raise ValidationError(
                        "hardware_requirements.%s must be a non-negative number" % key
                    )
            elif key in {"os", "cpu_arch"}:
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ValidationError(
                        "hardware_requirements.%s must be a list of strings" % key
                    )
            elif key == "tags_all":
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ValidationError(
                        "hardware_requirements.tags_all must be a list of strings"
                    )
            elif key == "accelerators":
                if not isinstance(value, list):
                    raise ValidationError(
                        "hardware_requirements.accelerators must be a list"
                    )
        return raw

    # Row hydration -----------------------------------------------------

    def _from_row(self, row: Any) -> AgentRole:
        return AgentRole(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            display_name=row["display_name"],
            description=row["description"],
            system_prompt=row["system_prompt"],
            level=row["level"],
            reports_to=row["reports_to"],
            specialties=json_loads(row["specialties"], []),
            default_capabilities=json_loads(row["default_capabilities"], []),
            required_capabilities=json_loads(row["required_capabilities"], []),
            hardware_requirements=json_loads(row["hardware_requirements"], {}),
            metadata=json_loads(row["metadata"], {}),
            is_default=bool(row["is_default"]),
            tenant_id=row["tenant_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# Module-level pure function so dispatch (services.py) can call without
# instantiating RolesService. Kept here so the hardware-shape contract
# lives in one place.

def machine_hardware_satisfies(
    requirements: JsonDict, hardware: JsonDict
) -> Tuple[bool, List[str]]:
    """Return ``(ok, reasons)``: does ``hardware`` satisfy ``requirements``?

    Absent constraints match. Unknown constraint keys are ignored (forward
    compatibility). Reasons list every miss so dispatch can surface them.
    """
    reasons: List[str] = []
    if not isinstance(requirements, dict) or not requirements:
        return True, reasons
    hw = hardware if isinstance(hardware, dict) else {}

    os_list = requirements.get("os")
    if isinstance(os_list, list) and os_list:
        if hw.get("os") not in os_list:
            reasons.append("os %r not in %s" % (hw.get("os"), os_list))

    arch_list = requirements.get("cpu_arch")
    if isinstance(arch_list, list) and arch_list:
        if hw.get("cpu_arch") not in arch_list:
            reasons.append("cpu_arch %r not in %s" % (hw.get("cpu_arch"), arch_list))

    for key in _NUMERIC_MINS:
        if key not in requirements:
            continue
        try:
            need = float(requirements[key])
        except (TypeError, ValueError):
            continue
        hw_key = key[: -len("_min")]
        have = hw.get(hw_key)
        try:
            have_f = float(have) if have is not None else None
        except (TypeError, ValueError):
            have_f = None
        if have_f is None or have_f < need:
            reasons.append("%s=%s < required %s" % (hw_key, have, need))

    tags_all = requirements.get("tags_all")
    if isinstance(tags_all, list) and tags_all:
        have_tags = set(hw.get("tags") or [])
        missing = [t for t in tags_all if t not in have_tags]
        if missing:
            reasons.append("missing tags: %s" % missing)

    acc_constraints = requirements.get("accelerators")
    if isinstance(acc_constraints, list) and acc_constraints:
        have_acc = hw.get("accelerators") or []
        if not isinstance(have_acc, list):
            have_acc = []
        for constraint in acc_constraints:
            if not isinstance(constraint, dict):
                continue
            if not any(_accelerator_matches(constraint, candidate) for candidate in have_acc):
                reasons.append("no accelerator matches %s" % constraint)

    return (not reasons), reasons


def _accelerator_matches(constraint: Dict[str, Any], candidate: Any) -> bool:
    if not isinstance(candidate, dict):
        return False
    for key in ("kind", "vendor", "model"):
        want = constraint.get(key)
        if want is not None and candidate.get(key) != want:
            return False
    memory_min = constraint.get("memory_gb_min")
    if memory_min is not None:
        try:
            if float(candidate.get("memory_gb") or 0) < float(memory_min):
                return False
        except (TypeError, ValueError):
            return False
    count_min = constraint.get("count_min")
    if count_min is not None:
        try:
            if int(candidate.get("count") or 0) < int(count_min):
                return False
        except (TypeError, ValueError):
            return False
    return True
