"""DB-backed OpenShell policy and deployment status service."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from mac import mac_paths
from typing import Any, Dict, List, Optional

import yaml

from mac.models import (
    JsonDict,
    NotFoundError,
    OpenShellPolicy,
    OpenShellPolicyAssignment,
    OpenShellPolicyVersion,
    OpenShellStatus,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.openshell_policy import render_policy as render_template_policy
from mac.openshell_runtime import (
    DEFAULT_REQUIRED_AGENT_NAMES as DEFAULT_REQUIRED_AGENT_NAMES,
    openshell_required_for_identity,
)


def policy_checksum(policy_text: str) -> str:
    """Return the SHA-256 checksum of the OpenShell policy text."""
    return "sha256:%s" % hashlib.sha256(policy_text.encode("utf-8")).hexdigest()


def parse_policy_metadata(policy_text: str) -> JsonDict:
    """Parse the OpenShell policy YAML into summary metadata."""
    try:
        parsed = yaml.safe_load(policy_text) or {}
    except yaml.YAMLError as exc:
        raise ValidationError("invalid OpenShell policy YAML: %s" % exc) from exc
    if not isinstance(parsed, dict):
        raise ValidationError("OpenShell policy must parse to a YAML object")
    network = parsed.get("network_policies")
    filesystem = parsed.get("filesystem_policy") or parsed.get("landlock") or {}
    return {
        "yaml_version": parsed.get("version"),
        "network_policy_names": sorted(network.keys()) if isinstance(network, dict) else [],
        "filesystem_keys": sorted(filesystem.keys()) if isinstance(filesystem, dict) else [],
        "top_level_keys": sorted(str(key) for key in parsed.keys()),
    }


class OpenShellService:
    def __init__(self, store: Any, *, get_agent: Any) -> None:
        self.store = store
        self._get_agent = get_agent

    def create_policy(
        self,
        name: str,
        policy_text: str,
        *,
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        created_by: str = "human",
        policy_id: Optional[str] = None,
    ) -> OpenShellPolicy:
        name_value = self._validate_name(name)
        text_value = self._validate_policy_text(policy_text)
        parsed = parse_policy_metadata(text_value)
        parsed.update(ensure_json_object(metadata))
        checksum = policy_checksum(text_value)
        now = utcnow()
        row_id = policy_id or new_id("ospol")
        version_id = new_id("ospolv")
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO openshell_policies (
                    id, name, description, policy_text, parsed_metadata,
                    version, checksum, created_by, updated_by, active,
                    created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 1, ?, ?, NULL)
                """,
                (
                    row_id,
                    name_value,
                    str(description or ""),
                    text_value,
                    json_dumps(parsed),
                    checksum,
                    str(created_by or "human"),
                    str(created_by or "human"),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO openshell_policy_versions (
                    id, policy_id, version, policy_text, parsed_metadata,
                    checksum, created_by, created_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    row_id,
                    text_value,
                    json_dumps(parsed),
                    checksum,
                    str(created_by or "human"),
                    now,
                ),
            )
        return self.get_policy(row_id)

    def list_policies(self, *, include_deleted: bool = False) -> List[OpenShellPolicy]:
        sql = "SELECT * FROM openshell_policies"
        params: List[Any] = []
        if not include_deleted:
            sql += " WHERE active = 1"
        sql += " ORDER BY active DESC, name, version DESC"
        return [self._policy_from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def get_policy(self, policy_id_or_name: str, *, include_deleted: bool = False) -> OpenShellPolicy:
        row = self.store.query_one(
            """
            SELECT * FROM openshell_policies
            WHERE id = ? OR name = ?
            ORDER BY active DESC
            LIMIT 1
            """,
            (policy_id_or_name, policy_id_or_name),
        )
        if row is None or (not include_deleted and not bool(row["active"])):
            raise NotFoundError("OpenShell policy not found: %s" % policy_id_or_name)
        return self._policy_from_row(row)

    def update_policy(
        self,
        policy_id_or_name: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        policy_text: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        updated_by: str = "human",
    ) -> OpenShellPolicy:
        current = self.get_policy(policy_id_or_name)
        name_value = self._validate_name(name) if name is not None else current.name
        description_value = str(description) if description is not None else current.description
        text_value = (
            self._validate_policy_text(policy_text)
            if policy_text is not None
            else current.policy_text
        )
        parsed = parse_policy_metadata(text_value)
        if metadata is None:
            parsed.update(current.parsed_metadata)
            for key, value in parse_policy_metadata(text_value).items():
                parsed[key] = value
        else:
            parsed.update(ensure_json_object(metadata))
        checksum = policy_checksum(text_value)
        version = current.version
        bump_version = text_value != current.policy_text or checksum != current.checksum
        if bump_version:
            version += 1
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE openshell_policies
                SET name = ?, description = ?, policy_text = ?,
                    parsed_metadata = ?, version = ?, checksum = ?,
                    updated_by = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name_value,
                    description_value,
                    text_value,
                    json_dumps(parsed),
                    version,
                    checksum,
                    str(updated_by or "human"),
                    now,
                    current.id,
                ),
            )
            if bump_version:
                conn.execute(
                    """
                    INSERT INTO openshell_policy_versions (
                        id, policy_id, version, policy_text, parsed_metadata,
                        checksum, created_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("ospolv"),
                        current.id,
                        version,
                        text_value,
                        json_dumps(parsed),
                        checksum,
                        str(updated_by or "human"),
                        now,
                    ),
                )
        return self.get_policy(current.id)

    def delete_policy(self, policy_id_or_name: str, *, actor: str = "human") -> OpenShellPolicy:
        policy = self.get_policy(policy_id_or_name)
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE openshell_policies
                SET active = 0, deleted_at = ?, updated_at = ?, updated_by = ?
                WHERE id = ?
                """,
                (now, now, str(actor or "human"), policy.id),
            )
            conn.execute(
                """
                UPDATE openshell_policy_assignments
                SET active = 0, updated_at = ?
                WHERE policy_id = ? AND active = 1
                """,
                (now, policy.id),
            )
        return self.get_policy(policy.id, include_deleted=True)

    def versions(self, policy_id_or_name: str) -> List[OpenShellPolicyVersion]:
        policy = self.get_policy(policy_id_or_name, include_deleted=True)
        rows = self.store.query_all(
            """
            SELECT * FROM openshell_policy_versions
            WHERE policy_id = ?
            ORDER BY version DESC
            """,
            (policy.id,),
        )
        return [self._version_from_row(row) for row in rows]

    def render_policy(
        self,
        policy_id_or_name: str,
        *,
        agent_user: Optional[str] = None,
        hub_host: Optional[str] = None,
        hub_port: Optional[int] = None,
        model_gateway_host: Optional[str] = None,
        shared_services: Optional[Dict[str, int]] = None,
    ) -> JsonDict:
        policy = self.get_policy(policy_id_or_name)
        rendered = policy.policy_text
        if agent_user or hub_host or hub_port or model_gateway_host or shared_services:
            rendered = render_template_policy(
                policy.policy_text,
                agent_user=agent_user or os.environ.get("USER") or "mac",
                hub_host=hub_host or "127.0.0.1",
                hub_port=int(hub_port or 8789),
                model_gateway_host=model_gateway_host,
                shared_services=shared_services or {},
            )
        return {
            "schema": "mac.openshell.policy.render.v1",
            "policy_id": policy.id,
            "version": policy.version,
            "checksum": policy_checksum(rendered),
            "policy_text": rendered,
        }

    def assign_policy(
        self,
        policy_id_or_name: str,
        *,
        target_type: str,
        target_id: str,
        created_by: str = "human",
    ) -> OpenShellPolicyAssignment:
        policy = self.get_policy(policy_id_or_name)
        target_type_value = str(target_type or "agent").strip().lower()
        target_id_value = str(target_id or "").strip()
        if target_type_value not in {"agent", "fleet", "host"}:
            raise ValidationError("unsupported OpenShell assignment target_type: %s" % target_type)
        if not target_id_value:
            raise ValidationError("OpenShell assignment requires target_id")
        if target_type_value == "agent":
            self._get_agent(target_id_value)
        elif target_type_value == "fleet":
            # Store the fleet *id*, never the name: resolution joins
            # ``fleet_agents.fleet_id``, and a fleet can be renamed after the
            # assignment is written. Normalizing here keeps a rename from
            # silently disarming a confinement policy.
            target_id_value = self._resolve_fleet_id(target_id_value)
        else:
            # target_type == "host". Refused rather than stored: nothing
            # resolves a host to the agents running on it (``agents.machine_id``
            # points at ``machines``, whose ``hostname`` is not unique), so a
            # host assignment could only ever be a row nobody enforces. On a
            # confinement boundary an unenforceable assignment that lists as
            # "assigned" is worse than no assignment at all, so this fails loud.
            raise ValidationError(
                "host-scoped OpenShell assignments are not enforced and are refused; "
                "assign the policy to an agent or a fleet instead"
            )
        now = utcnow()
        assignment_id = new_id("ospola")
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE openshell_policy_assignments
                SET active = 0, updated_at = ?
                WHERE target_type = ? AND target_id = ? AND active = 1
                """,
                (now, target_type_value, target_id_value),
            )
            conn.execute(
                """
                INSERT INTO openshell_policy_assignments (
                    id, policy_id, policy_version, target_type, target_id,
                    active, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    assignment_id,
                    policy.id,
                    policy.version,
                    target_type_value,
                    target_id_value,
                    str(created_by or "human"),
                    now,
                    now,
                ),
            )
        return self.get_assignment(assignment_id)

    def get_assignment(self, assignment_id: str) -> OpenShellPolicyAssignment:
        row = self.store.query_one(
            "SELECT * FROM openshell_policy_assignments WHERE id = ?",
            (assignment_id,),
        )
        if row is None:
            raise NotFoundError("OpenShell assignment not found: %s" % assignment_id)
        return self._assignment_from_row(row)

    def list_assignments(
        self,
        *,
        policy_id: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        active_only: bool = True,
    ) -> List[OpenShellPolicyAssignment]:
        clauses: List[str] = []
        params: List[Any] = []
        if policy_id is not None:
            policy = self.get_policy(policy_id, include_deleted=True)
            clauses.append("policy_id = ?")
            params.append(policy.id)
        if target_type is not None:
            clauses.append("target_type = ?")
            params.append(str(target_type))
        if target_id is not None:
            clauses.append("target_id = ?")
            params.append(str(target_id))
        if active_only:
            clauses.append("active = 1")
        sql = "SELECT * FROM openshell_policy_assignments"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, id DESC"
        return [self._assignment_from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def _resolve_fleet_id(self, fleet_id_or_name: str) -> str:
        # Id before name, in two queries rather than one OR: a name that happens
        # to equal another fleet's id must not decide the target by row order.
        for sql in ("SELECT id FROM fleets WHERE id = ?", "SELECT id FROM fleets WHERE name = ?"):
            row = self.store.query_one(sql, (fleet_id_or_name,))
            if row is not None:
                return str(row["id"])
        raise NotFoundError("fleet not found: %s" % fleet_id_or_name)

    def _fleet_ids_for_agent(self, agent_id: str) -> List[str]:
        """Configured fleet membership for ``agent_id``.

        Deliberately ``fleet_agents`` (the reconciled, configured membership)
        and not ``fleet_agent_observations``: an observation is unmanaged
        runtime drift, so letting an agent *observe* itself into a fleet would
        let it choose its own confinement policy.
        """
        rows = self.store.query_all(
            "SELECT fleet_id FROM fleet_agents WHERE agent_id = ? ORDER BY fleet_id",
            (agent_id,),
        )
        return [str(row["fleet_id"]) for row in rows]

    def active_assignment_for_agent(self, agent_id: str) -> Optional[OpenShellPolicyAssignment]:
        """The assignment that actually governs ``agent_id``.

        Precedence is explicit, not emergent:

        1. **Agent-scoped wins over fleet-scoped.** The more specific target is
           the more deliberate one; an operator pinning one agent must be able
           to override the fleet default without editing the fleet.
        2. **Fleet-scoped applies to configured members.** Before this, a fleet
           assignment was accepted, listed, and enforced nothing — the agent
           quietly kept whatever local policy file it already had, so
           "assigned" and "enforced" were indistinguishable from outside.
        3. **Multiple fleets that disagree are an error, not a race.** An agent
           may belong to several fleets. Picking "whichever row sorts first"
           would make confinement depend on insertion order, so conflicting
           fleet assignments fail loud instead. Several fleets naming the same
           policy *and* version agree, so that resolves normally.
        4. **Deactivation falls through.** When a fleet assignment goes
           inactive the agent is simply back to the next rule that matches
           (agent-scoped, another fleet, or no hub policy at all) — the same
           behaviour deactivating an agent-scoped assignment has always had.
        """
        rows = self.list_assignments(target_type="agent", target_id=agent_id, active_only=True)
        if rows:
            return rows[0]
        fleet_ids = self._fleet_ids_for_agent(agent_id)
        candidates: List[OpenShellPolicyAssignment] = []
        for fleet_id in fleet_ids:
            candidates.extend(
                self.list_assignments(target_type="fleet", target_id=fleet_id, active_only=True)
            )
        if not candidates:
            return None
        distinct = {(item.policy_id, item.policy_version) for item in candidates}
        if len(distinct) > 1:
            raise ValidationError(
                "agent %s is in fleets with conflicting OpenShell policy assignments (%s); "
                "assign the policy to the agent directly to resolve it"
                % (
                    agent_id,
                    ", ".join(
                        sorted(
                            "%s@%s via fleet %s" % (item.policy_id, item.policy_version, item.target_id)
                            for item in candidates
                        )
                    ),
                )
            )
        # All remaining candidates name the same policy and version, so any of
        # them is the same answer; sort for a stable assignment id in output.
        return sorted(candidates, key=lambda item: item.id)[0]

    def report_agent_status(
        self,
        agent_id: str,
        *,
        status: str,
        required: Optional[bool] = None,
        active: bool = True,
        sandbox_id: Optional[str] = None,
        policy_id: Optional[str] = None,
        policy_version: Optional[int] = None,
        checksum: Optional[str] = None,
        supervisor_pid: Optional[int] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> OpenShellStatus:
        self._get_agent(agent_id)
        status_value = str(status or "").strip().lower()
        if status_value not in {"active", "starting", "inactive", "degraded", "failed", "unknown"}:
            raise ValidationError("unsupported OpenShell status: %s" % status)
        if policy_id:
            self.get_policy(policy_id, include_deleted=True)
        required_value = self.agent_requires_openshell(agent_id) if required is None else bool(required)
        now = utcnow()
        existing = self.store.query_one(
            "SELECT agent_id FROM openshell_agent_status WHERE agent_id = ?",
            (agent_id,),
        )
        values = (
            status_value,
            1 if required_value else 0,
            1 if active else 0,
            sandbox_id,
            policy_id,
            policy_version,
            checksum,
            supervisor_pid,
            json_dumps(ensure_json_object(detail)),
            now,
            agent_id,
        )
        if existing is None:
            self.store.execute(
                """
                INSERT INTO openshell_agent_status (
                    status, required, active, sandbox_id, policy_id,
                    policy_version, checksum, supervisor_pid, detail,
                    reported_at, agent_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        else:
            self.store.execute(
                """
                UPDATE openshell_agent_status
                SET status = ?, required = ?, active = ?, sandbox_id = ?,
                    policy_id = ?, policy_version = ?, checksum = ?,
                    supervisor_pid = ?, detail = ?, reported_at = ?
                WHERE agent_id = ?
                """,
                values,
            )
        row = self.store.query_one(
            "SELECT * FROM openshell_agent_status WHERE agent_id = ?",
            (agent_id,),
        )
        if row is None:
            raise NotFoundError("OpenShell status not found for agent: %s" % agent_id)
        return self._status_from_row(row)

    def agent_status(self, agent_id: str) -> JsonDict:
        agent = self._get_agent(agent_id)
        assignment = self.active_assignment_for_agent(agent_id)
        policy = self.get_policy(assignment.policy_id) if assignment else None
        row = self.store.query_one(
            "SELECT * FROM openshell_agent_status WHERE agent_id = ?",
            (agent_id,),
        )
        deployed = self._status_from_row(row) if row is not None else None
        required = self.agent_requires_openshell(agent_id)
        return {
            "schema": "mac.openshell.agent_status.v1",
            "agent_id": agent_id,
            "agent_name": getattr(agent, "name", None),
            "required": required,
            "assignment": assignment.to_dict() if assignment else None,
            "policy": policy.to_dict() if policy else None,
            "deployed_status": deployed.to_dict() if deployed else None,
            "effective": {
                "assigned": assignment is not None,
                "deployed": deployed is not None and deployed.status == "active",
                "fail_closed": required and not (deployed is not None and deployed.status == "active"),
            },
        }

    def assigned_policy(self, agent_id: str) -> JsonDict:
        """The policy text assigned to ``agent_id``, for self-install by the worker.

        Carries the text itself, not just its identity, so a worker with only an
        HTTP seam can converge onto its assigned policy. ``checksum`` lets the
        worker skip the write when it is already converged, keeping this cheap
        enough to call from the heartbeat path.
        """
        assignment = self.active_assignment_for_agent(agent_id)
        if assignment is None:
            raise NotFoundError("no OpenShell policy assigned to agent: %s" % agent_id)
        policy = self.get_policy(assignment.policy_id)
        return {
            "schema": "mac.openshell.assigned_policy.v1",
            "agent_id": agent_id,
            "policy_id": policy.id,
            "policy_name": policy.name,
            "version": policy.version,
            "checksum": policy.checksum,
            "policy_text": policy.policy_text,
        }

    def materialize_assigned_policy(self, agent_id: str, path: Path) -> JsonDict:
        """Write the assigned policy to ``path`` (hub-side CLI/operator convenience).

        The worker uses :meth:`assigned_policy` over HTTP instead — it holds no
        store handle. Both resolve the assignment the same way, so the two paths
        cannot disagree about which policy is current.
        """
        assigned = self.assigned_policy(agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(assigned["policy_text"], encoding="utf-8")
        path.chmod(0o600)
        return {
            "path": str(path),
            "policy_id": assigned["policy_id"],
            "version": assigned["version"],
            "checksum": assigned["checksum"],
        }

    def agent_requires_openshell(self, agent_id: str) -> bool:
        try:
            agent = self._get_agent(agent_id)
        except Exception:
            name = agent_id
            resources: JsonDict = {}
        else:
            name = str(getattr(agent, "name", "") or agent_id)
            resources = ensure_json_object(getattr(agent, "resources", {}) or {})
        return openshell_required_for_identity(
            agent_id=agent_id,
            agent_name=name,
            resources=resources,
        )

    def file_fallback_policy(self) -> Optional[Path]:
        explicit = os.environ.get("MAC_OPENSHELL_POLICY")
        if explicit and Path(explicit).is_file():
            return Path(explicit)
        deployed = mac_paths.mac_home() / "openshell-policy.yaml"
        if deployed.is_file():
            return deployed
        bundled = Path(__file__).resolve().parent / "openshell" / "default-policy.yaml"
        return bundled if bundled.is_file() else None

    def _validate_name(self, value: Optional[str]) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValidationError("OpenShell policy name is required")
        if len(text) > 120:
            raise ValidationError("OpenShell policy name is too long")
        return text

    def _validate_policy_text(self, value: Optional[str]) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValidationError("OpenShell policy text is required")
        parse_policy_metadata(text)
        return text

    def _policy_from_row(self, row: Any) -> OpenShellPolicy:
        return OpenShellPolicy(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            policy_text=row["policy_text"],
            parsed_metadata=json_loads(row["parsed_metadata"], {}),
            version=int(row["version"]),
            checksum=row["checksum"],
            created_by=row["created_by"],
            updated_by=row["updated_by"],
            active=bool(row["active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
        )

    def _version_from_row(self, row: Any) -> OpenShellPolicyVersion:
        return OpenShellPolicyVersion(
            id=row["id"],
            policy_id=row["policy_id"],
            version=int(row["version"]),
            policy_text=row["policy_text"],
            parsed_metadata=json_loads(row["parsed_metadata"], {}),
            checksum=row["checksum"],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    def _assignment_from_row(self, row: Any) -> OpenShellPolicyAssignment:
        return OpenShellPolicyAssignment(
            id=row["id"],
            policy_id=row["policy_id"],
            policy_version=int(row["policy_version"]),
            target_type=row["target_type"],
            target_id=row["target_id"],
            active=bool(row["active"]),
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _status_from_row(self, row: Any) -> OpenShellStatus:
        return OpenShellStatus(
            agent_id=row["agent_id"],
            status=row["status"],
            required=bool(row["required"]),
            active=bool(row["active"]),
            sandbox_id=row["sandbox_id"],
            policy_id=row["policy_id"],
            policy_version=row["policy_version"],
            checksum=row["checksum"],
            supervisor_pid=row["supervisor_pid"],
            detail=json_loads(row["detail"], {}),
            reported_at=row["reported_at"],
        )
