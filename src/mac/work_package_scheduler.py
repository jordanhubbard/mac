"""Authoritative work-package checks that run inside a task claim transaction.

The ordinary dispatcher may rank workers and tasks, but its proposal is not an
authorization.  Package claims use a prepare-before-CAS protocol: the active
lease, immutable assignment audit, WIP ownership, and executing link are
created first in the still-open transaction, then the ordinary task row moves
from OPEN to CLAIMED.  Database triggers can therefore reject mixed-version
hubs that try to claim a linked task without package authority.  Any failure
rolls the surrounding claim back as one unit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from mac.models import (
    JsonDict,
    TransitionError,
    ValidationError,
    json_dumps,
    json_loads,
    new_id,
)
from mac.work_package_models import (
    WorkPackageEffects,
    validate_supported_work_package_topology,
    work_package_effect_conflicts,
)


WORK_PACKAGE_ALLOCATOR_VERSION = "work-package-allocator-v1"
_EFFECT_KINDS = ("reads", "writes", "exclusive", "external")


@dataclass(frozen=True)
class WorkPackageClaimAdmission:
    package_id: str
    plan_version: int
    epoch: int
    node_key: str
    task_id: str
    agent_id: str
    lease_id: str
    attempt_number: int
    attempt_ref: str
    attempt_base_ref: str
    attempt_base_sha: str
    declared_effects_digest: str
    acquired_wip_token_ids: Tuple[str, ...]

    def to_dict(self) -> JsonDict:
        return {
            "package_id": self.package_id,
            "plan_version": self.plan_version,
            "epoch": self.epoch,
            "node_key": self.node_key,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "lease_id": self.lease_id,
            "attempt_number": self.attempt_number,
            "attempt_ref": self.attempt_ref,
            "attempt_base_ref": self.attempt_base_ref,
            "attempt_base_sha": self.attempt_base_sha,
            "declared_effects_digest": self.declared_effects_digest,
            "acquired_wip_token_ids": list(self.acquired_wip_token_ids),
        }


class WorkPackageClaimGate:
    """Package-specific claim admission, deliberately independent of services.py."""

    def admit_claim(
        self,
        conn: Any,
        *,
        task_id: str,
        agent_id: str,
        lease_id: str,
        attempt_number: int,
        now: str,
        allocator: str = "control-plane",
        allocator_version: str = WORK_PACKAGE_ALLOCATOR_VERSION,
        score: Optional[float] = None,
        rationale: str = "authoritative package claim",
        decision: Optional[Mapping[str, Any]] = None,
        prepared_task: bool = False,
    ) -> Optional[WorkPackageClaimAdmission]:
        """Admit a package claim or return ``None`` for an ordinary task.

        The caller owns the transaction.  The lease insert must already be
        visible through ``conn``.  By default the historical already-CLAIMED
        shape is required for direct gate callers.  The control plane sets
        ``prepared_task`` while the task is still OPEN so immutable package
        authority exists before its final task-state CAS.
        """

        link = conn.execute(
            "SELECT * FROM work_package_task_links WHERE task_id = ?", (task_id,)
        ).fetchone()
        if link is None:
            return None
        if int(attempt_number) < 1:
            raise ValidationError("work-package attempt number must be positive")

        # Credential/readiness is authorization, not dispatcher ranking. Run
        # it inside this same claim transaction after the task+lease writes so
        # hub dispatch, admin/internal claim, and worker claim-next all share
        # one fail-closed package boundary.
        from mac.worker_credentials import assert_package_worker_ready

        assert_package_worker_ready(conn, agent_id)

        package_id = str(link["package_id"])
        plan_version = int(link["plan_version"])
        epoch = int(link["epoch"])
        node_key = str(link["node_key"])

        package_lock = conn.execute(
            "UPDATE work_packages SET updated_at = updated_at WHERE id = ?",
            (package_id,),
        )
        if package_lock.rowcount != 1:
            raise TransitionError("work package disappeared during claim")
        package = conn.execute(
            "SELECT * FROM work_packages WHERE id = ?", (package_id,)
        ).fetchone()
        if package is None:
            raise TransitionError("work package disappeared during claim")
        repository_id = str(package["repository_id"] or "")
        if not repository_id:
            raise ValidationError("work package has no repository identity")

        # All claims in one repository serialize through this row before WIP
        # and effect-lock inspection.  That makes the subsequent scan and token
        # inserts one cross-package decision on both PostgreSQL and SQLite.
        repository_lock = conn.execute(
            "UPDATE project_repositories SET updated_at = updated_at WHERE id = ?",
            (repository_id,),
        )
        if repository_lock.rowcount != 1:
            raise TransitionError("work package repository is unavailable")

        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        lease = conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
        if task is None or lease is None:
            raise TransitionError("work-package claim lacks its task or lease")
        task_identity_exact = (
            (
                bool(prepared_task)
                and task["state"] == "open"
                and task["owner_agent_id"] is None
                and task["lease_id"] is None
                and int(task["attempt_count"]) + 1 == int(attempt_number)
            )
            or (
                not prepared_task
                and task["state"] == "claimed"
                and task["owner_agent_id"] == agent_id
                and task["lease_id"] == lease_id
                and int(task["attempt_count"]) == int(attempt_number)
            )
        )
        if (
            not task_identity_exact
            or lease["task_id"] != task_id
            or lease["agent_id"] != agent_id
            or lease["status"] != "active"
        ):
            raise TransitionError("work-package claim identity is not exact")
        if (
            package["state"] != "active"
            or int(package["current_plan_version"]) != plan_version
            or int(package["current_epoch"]) != epoch
            or link["node_state"] != "ready"
        ):
            raise TransitionError("work-package task is not ready in the current epoch")
        epoch_row = conn.execute(
            "SELECT * FROM work_package_epochs "
            "WHERE package_id = ? AND plan_version = ? AND epoch = ?",
            (package_id, plan_version, epoch),
        ).fetchone()
        if epoch_row is None or epoch_row["status"] != "active":
            raise TransitionError("work-package epoch is not active")

        plan_row = conn.execute(
            "SELECT definition FROM work_package_plan_versions "
            "WHERE package_id = ? AND version = ?",
            (package_id, plan_version),
        ).fetchone()
        if plan_row is None:
            raise TransitionError("work-package plan version is missing")
        definition = json_loads(plan_row["definition"], {})
        validate_supported_work_package_topology(definition)
        node = self._plan_node(definition, node_key)
        if (
            str(node.get("effects_digest") or "")
            != str(link["declared_effects_digest"])
        ):
            raise ValidationError("work-package node effects do not match its task link")

        node_kind = str(node.get("kind") or "mutation")
        if node_kind in {"integration", "certification"}:
            raise TransitionError(
                "work-package %s nodes are controller-owned stations and cannot "
                "be claimed by an execution worker" % node_kind
            )

        active_assignments = conn.execute(
            "SELECT COUNT(*) AS n FROM work_package_assignment_audit AS assignment "
            "JOIN leases AS lease ON lease.id = assignment.lease_id "
            "WHERE assignment.package_id = ? AND assignment.plan_version = ? "
            "AND assignment.epoch = ? AND lease.status = ?",
            (package_id, plan_version, epoch, "active"),
        ).fetchone()
        max_in_flight = int(definition.get("max_in_flight") or 1)
        if int(active_assignments["n"]) >= max_in_flight:
            raise TransitionError("work package execution capacity is exhausted")

        effects = self._effects_from_node(node)
        if effects.external:
            raise TransitionError(
                "work-package external effects lack a controller-owned fenced "
                "effector and cannot be worker-claimed"
            )
        existing_wip_tokens: Tuple[Mapping[str, Any], ...] = ()
        if node_kind == "mutation":
            existing_wip_tokens = self._existing_mutation_wip(
                conn,
                package_id=package_id,
                plan_version=plan_version,
                epoch=epoch,
                task_id=task_id,
                effects=effects,
            )
            mutation_wip = definition.get("mutation_wip") or {}
            max_tokens = int(mutation_wip.get("max_tokens") or 1)
            held_capacity = conn.execute(
                "SELECT COUNT(*) AS n FROM work_package_wip_tokens "
                "WHERE package_id = ? AND plan_version = ? AND epoch = ? "
                "AND token_kind = ? AND stage = ? AND state = ? AND task_id != ?",
                (
                    package_id,
                    plan_version,
                    epoch,
                    "mutation_capacity",
                    "mutation",
                    "held",
                    task_id,
                ),
            ).fetchone()
            if not existing_wip_tokens and int(held_capacity["n"]) >= max_tokens:
                raise TransitionError("work package mutation WIP is exhausted")
            conflicts = self._hard_repository_conflicts(
                conn,
                repository_id=repository_id,
                candidate_task_id=task_id,
                candidate_effects=effects,
            )
            if conflicts:
                raise TransitionError(
                    "work package hard effect conflict: %s" % ", ".join(conflicts)
                )

        attempt_ref = self._attempt_ref(
            package_id=package_id,
            epoch=epoch,
            node_key=node_key,
            attempt_number=int(attempt_number),
            lease_id=lease_id,
        )
        decision_value = dict(decision or {})
        decision_value.update(
            {
                "schema": "mac.work_package.assignment.v1",
                "package_state": package["state"],
                "plan_version": plan_version,
                "epoch": epoch,
                "node_key": node_key,
                "max_in_flight": max_in_flight,
            }
        )
        conn.execute(
            "INSERT INTO work_package_assignment_audit ("
            "lease_id, package_id, plan_version, epoch, node_key, task_id, agent_id, "
            "attempt_number, attempt_ref, attempt_base_ref, attempt_base_sha, "
            "declared_effects_digest, allocator, allocator_version, score, rationale, "
            "decision, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lease_id,
                package_id,
                plan_version,
                epoch,
                node_key,
                task_id,
                agent_id,
                int(attempt_number),
                attempt_ref,
                epoch_row["planning_base_ref"],
                epoch_row["planning_base_sha"],
                link["declared_effects_digest"],
                str(allocator or "control-plane"),
                str(allocator_version or WORK_PACKAGE_ALLOCATOR_VERSION),
                score,
                str(rationale or "authoritative package claim"),
                json_dumps(decision_value),
                now,
            ),
        )

        token_ids: list[str] = []
        if node_kind == "mutation":
            if existing_wip_tokens:
                token_ids.extend(
                    self._transfer_retry_mutation_wip(
                        conn,
                        package_id=package_id,
                        plan_version=plan_version,
                        epoch=epoch,
                        node_key=node_key,
                        task_id=task_id,
                        lease_id=lease_id,
                        tokens=existing_wip_tokens,
                        now=now,
                    )
                )
            else:
                token_ids.append(
                    self._insert_wip_token(
                        conn,
                        package_id=package_id,
                        plan_version=plan_version,
                        epoch=epoch,
                        node_key=node_key,
                        task_id=task_id,
                        lease_id=lease_id,
                        generation=int(attempt_number),
                        token_kind="mutation_capacity",
                        resource_key="capacity:%s" % node_key,
                        reservation_key=None,
                        now=now,
                    )
                )
                for effect_kind in _EFFECT_KINDS:
                    for resource in getattr(effects, effect_kind):
                        token_ids.append(
                            self._insert_wip_token(
                                conn,
                                package_id=package_id,
                                plan_version=plan_version,
                                epoch=epoch,
                                node_key=node_key,
                                task_id=task_id,
                                lease_id=lease_id,
                                generation=int(attempt_number),
                                token_kind=effect_kind,
                                resource_key=self._effect_token_key(
                                    node_key, effect_kind, resource
                                ),
                                reservation_key=resource,
                                now=now,
                            )
                        )

        link_update = conn.execute(
            "UPDATE work_package_task_links SET node_state = ? "
            "WHERE task_id = ? AND package_id = ? AND plan_version = ? "
            "AND epoch = ? AND node_state = ?",
            ("executing", task_id, package_id, plan_version, epoch, "ready"),
        )
        if link_update.rowcount != 1:
            raise TransitionError("work-package node readiness changed during claim")
        return WorkPackageClaimAdmission(
            package_id=package_id,
            plan_version=plan_version,
            epoch=epoch,
            node_key=node_key,
            task_id=task_id,
            agent_id=agent_id,
            lease_id=lease_id,
            attempt_number=int(attempt_number),
            attempt_ref=attempt_ref,
            attempt_base_ref=str(epoch_row["planning_base_ref"]),
            attempt_base_sha=str(epoch_row["planning_base_sha"]),
            declared_effects_digest=str(link["declared_effects_digest"]),
            acquired_wip_token_ids=tuple(token_ids),
        )

    @staticmethod
    def _existing_mutation_wip(
        conn: Any,
        *,
        package_id: str,
        plan_version: int,
        epoch: int,
        task_id: str,
        effects: WorkPackageEffects,
    ) -> Tuple[Mapping[str, Any], ...]:
        rows = conn.execute(
            "SELECT id, token_kind, stage, state, generation, capacity_units, "
            "resource_key, reservation_key, acquired_by_assignment_lease_id "
            "FROM work_package_wip_tokens WHERE package_id = ? "
            "AND plan_version = ? AND epoch = ? AND task_id = ? AND state = ? "
            "ORDER BY id",
            (package_id, plan_version, epoch, task_id, "held"),
        ).fetchall()
        if not rows:
            return ()
        if any(row["stage"] != "mutation" for row in rows):
            raise TransitionError(
                "work-package retry cannot reclaim WIP after candidate-stage transfer"
            )
        expected = [("mutation_capacity", None)]
        for effect_kind in _EFFECT_KINDS:
            expected.extend(
                (effect_kind, resource) for resource in getattr(effects, effect_kind)
            )
        observed = sorted(
            (str(row["token_kind"]), row["reservation_key"]) for row in rows
        )
        if observed != sorted(expected):
            raise ValidationError(
                "held work-package WIP does not match the immutable node effects"
            )
        return tuple(dict(row) for row in rows)

    def _transfer_retry_mutation_wip(
        self,
        conn: Any,
        *,
        package_id: str,
        plan_version: int,
        epoch: int,
        node_key: str,
        task_id: str,
        lease_id: str,
        tokens: Tuple[Mapping[str, Any], ...],
        now: str,
    ) -> Tuple[str, ...]:
        """Transfer retained mutation WIP to the exact retry assignment.

        Lease expiry deliberately retains product WIP, but a new execution
        attempt is a new fencing authority.  The old immutable tokens are
        therefore superseded and append-only successors are acquired for the
        new assignment in this same repository-locked transaction.  No other
        transaction can observe a capacity gap between those two writes.
        """

        source_lease_ids = {
            str(token["acquired_by_assignment_lease_id"]) for token in tokens
        }
        if len(source_lease_ids) != 1:
            raise ValidationError(
                "held work-package retry WIP has mixed assignment authority"
            )
        source_lease_id = next(iter(source_lease_ids))
        if source_lease_id == lease_id:
            raise TransitionError("work-package retry WIP already belongs to this lease")
        repair = conn.execute(
            "SELECT repair.id, repair.held_wip_ids, repair.held_wip_count, "
            "repair.target_task_state, repair.target_node_state, "
            "repair.wip_disposition, lease.status AS lease_status, "
            "lease.expiry_finalized_at "
            "FROM work_package_lease_expiry_repairs AS repair "
            "JOIN leases AS lease ON lease.id = repair.lease_id "
            "WHERE repair.lease_id = ? AND repair.package_id = ? "
            "AND repair.plan_version = ? AND repair.epoch = ? "
            "AND repair.node_key = ? AND repair.task_id = ?",
            (
                source_lease_id,
                package_id,
                plan_version,
                epoch,
                node_key,
                task_id,
            ),
        ).fetchone()
        if (
            repair is None
            or repair["lease_status"] != "expired"
            or repair["expiry_finalized_at"] is None
            or repair["target_task_state"] not in {"open", "waiting"}
            or repair["target_node_state"] != "ready"
            or repair["wip_disposition"] != "retain"
        ):
            raise TransitionError(
                "held work-package retry WIP lacks finalized lease-expiry authority"
            )
        raw_repair_ids = repair["held_wip_ids"]
        repair_ids_value = (
            raw_repair_ids
            if isinstance(raw_repair_ids, list)
            else json_loads(raw_repair_ids, [])
        )
        repair_ids = tuple(sorted(str(value) for value in repair_ids_value))
        token_ids = tuple(sorted(str(token["id"]) for token in tokens))
        if int(repair["held_wip_count"]) != len(tokens) or repair_ids != token_ids:
            raise TransitionError(
                "held work-package retry WIP differs from its expiry receipt"
            )

        successors = []
        release_reason = "retry_transfer:%s:%s" % (repair["id"], lease_id)
        for token in tokens:
            changed = conn.execute(
                "UPDATE work_package_wip_tokens SET state = ?, released_at = ?, "
                "release_reason = ? WHERE id = ? AND package_id = ? "
                "AND plan_version = ? AND epoch = ? AND node_key = ? "
                "AND task_id = ? AND state = ? "
                "AND acquired_by_assignment_lease_id = ?",
                (
                    "superseded",
                    now,
                    release_reason,
                    token["id"],
                    package_id,
                    plan_version,
                    epoch,
                    node_key,
                    task_id,
                    "held",
                    source_lease_id,
                ),
            )
            if changed.rowcount != 1:
                raise TransitionError(
                    "work-package retry WIP changed during fenced transfer"
                )
            successors.append(
                self._insert_wip_token(
                    conn,
                    package_id=package_id,
                    plan_version=plan_version,
                    epoch=epoch,
                    node_key=node_key,
                    task_id=task_id,
                    lease_id=lease_id,
                    generation=int(token["generation"]) + 1,
                    token_kind=str(token["token_kind"]),
                    resource_key=str(token["resource_key"]),
                    reservation_key=token["reservation_key"],
                    predecessor_token_id=str(token["id"]),
                    capacity_units=int(token["capacity_units"]),
                    now=now,
                )
            )
        return tuple(successors)

    @staticmethod
    def _plan_node(definition: Mapping[str, Any], node_key: str) -> Mapping[str, Any]:
        nodes = definition.get("nodes")
        if not isinstance(nodes, list):
            raise ValidationError("work-package plan has no node list")
        matches = [
            node
            for node in nodes
            if isinstance(node, Mapping) and node.get("node_key") == node_key
        ]
        if len(matches) != 1:
            raise ValidationError("work-package task link does not name exactly one plan node")
        return matches[0]

    @staticmethod
    def _effects_from_node(node: Mapping[str, Any]) -> WorkPackageEffects:
        raw = node.get("effects") or {}
        if not isinstance(raw, Mapping):
            raise ValidationError("work-package node effects are malformed")
        values = {}
        for effect_kind in _EFFECT_KINDS:
            items = raw.get(effect_kind) or []
            if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                raise ValidationError("work-package node %s effects are malformed" % effect_kind)
            values[effect_kind] = tuple(items)
        external_contract = raw.get("external_contract") or {}
        if not isinstance(external_contract, Mapping):
            raise ValidationError("work-package external effect contract is malformed")
        return WorkPackageEffects(
            reads=values["reads"],
            writes=values["writes"],
            exclusive=values["exclusive"],
            external=values["external"],
            external_contract=dict(external_contract),
        )

    def _hard_repository_conflicts(
        self,
        conn: Any,
        *,
        repository_id: str,
        candidate_task_id: str,
        candidate_effects: WorkPackageEffects,
    ) -> Tuple[str, ...]:
        rows = conn.execute(
            "SELECT token.task_id, token.token_kind, token.reservation_key "
            "FROM work_package_wip_tokens AS token "
            "JOIN work_packages AS package ON package.id = token.package_id "
            "WHERE package.repository_id = ? AND token.state = ? "
            "AND token.token_kind IN (?, ?, ?, ?)",
            (repository_id, "held", *_EFFECT_KINDS),
        ).fetchall()
        by_task: dict[str, dict[str, list[str]]] = {}
        for row in rows:
            if row["task_id"] == candidate_task_id:
                continue
            kind = str(row["token_kind"])
            resource = str(row["reservation_key"] or "")
            if not resource:
                raise ValidationError("held work-package effect token has no resource")
            by_task.setdefault(
                str(row["task_id"]), {name: [] for name in _EFFECT_KINDS}
            )[kind].append(resource)
        hard = set()
        for task_id, values in by_task.items():
            current = WorkPackageEffects(
                reads=tuple(sorted(set(values["reads"]))),
                writes=tuple(sorted(set(values["writes"]))),
                exclusive=tuple(sorted(set(values["exclusive"]))),
                external=tuple(sorted(set(values["external"]))),
            )
            for reason in work_package_effect_conflicts(candidate_effects, current):
                hard.add("%s:%s" % (task_id, reason))
        return tuple(sorted(hard))

    @staticmethod
    def _insert_wip_token(
        conn: Any,
        *,
        package_id: str,
        plan_version: int,
        epoch: int,
        node_key: str,
        task_id: str,
        lease_id: str,
        generation: int,
        token_kind: str,
        resource_key: str,
        reservation_key: Optional[str],
        predecessor_token_id: Optional[str] = None,
        capacity_units: int = 1,
        now: str,
    ) -> str:
        token_id = new_id("wpwip")
        conn.execute(
            "INSERT INTO work_package_wip_tokens ("
            "id, package_id, plan_version, epoch, node_key, task_id, resource_key, "
            "token_kind, stage, state, generation, capacity_units, reservation_key, "
            "predecessor_token_id, acquired_by_assignment_lease_id, acquired_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token_id,
                package_id,
                plan_version,
                epoch,
                node_key,
                task_id,
                resource_key,
                token_kind,
                "mutation",
                "held",
                generation,
                capacity_units,
                reservation_key,
                predecessor_token_id,
                lease_id,
                now,
            ),
        )
        return token_id

    @staticmethod
    def _attempt_ref(
        *,
        package_id: str,
        epoch: int,
        node_key: str,
        attempt_number: int,
        lease_id: str,
    ) -> str:
        package_token = hashlib.sha256(package_id.encode("utf-8")).hexdigest()[:16]
        lease_token = hashlib.sha256(lease_id.encode("utf-8")).hexdigest()[:12]
        return "refs/mac/attempts/%s/e%d/%s/a%d-%s" % (
            package_token,
            epoch,
            node_key,
            attempt_number,
            lease_token,
        )

    @staticmethod
    def _effect_token_key(node_key: str, effect_kind: str, resource: str) -> str:
        digest = hashlib.sha256(resource.encode("utf-8")).hexdigest()
        return "effect:%s:%s:%s" % (node_key, effect_kind, digest)


__all__ = [
    "WORK_PACKAGE_ALLOCATOR_VERSION",
    "WorkPackageClaimAdmission",
    "WorkPackageClaimGate",
]
