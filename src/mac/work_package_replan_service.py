"""Fail-closed pause and immutable replan control for work packages.

The normal task lifecycle cannot safely rewrite a package DAG in place.  This
service compiles a complete replacement plan, previews the affected cone, and
atomically installs a new plan version and execution epoch while preserving
the old plan, tasks, candidates, and evidence as audit records.

The current schema deliberately does not let a candidate from one task become
the accepted candidate of another task.  Consequently this implementation
never pretends that lineage alone is executable carry-forward authority.  It
records an explicit blocked carry decision and safely reruns the node in the
new epoch.  A future schema can add an exact carried-candidate receipt without
weakening this contract.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Tuple

from mac.models import (
    JsonDict,
    TransitionError,
    ValidationError,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.store import Store
from mac.work_package_models import (
    CompiledWorkPackagePlan,
    WorkPackageNodeSpec,
    compile_work_package_plan,
    validate_executable_work_package_effects,
)
from mac.work_package_service import (
    GitRepositoryBaseVerifier,
    RepositoryBaseAttestation,
    RepositoryBaseVerifier,
    WORK_PACKAGE_MATERIALIZER_VERSION,
)


WORK_PACKAGE_REPLAN_SERVICE_VERSION = "work-package-replan-v1"
_SUPERSEDABLE_NODE_STATES = {
    "planned",
    "ready",
    "candidate_submitted",
    "candidate_accepted",
    "integrated",
}
_REWORK_CONSUMING_NODE_STATES = {
    "candidate_submitted",
    "candidate_accepted",
    "integrated",
}
_REPOSITORY_IDENTITY_FIELDS = (
    "id",
    "source",
    "path",
    "project",
    "enabled",
    "updated_at",
)


@dataclass(frozen=True)
class BaseDeltaAttestation:
    """Controller observation of paths changed between two exact bases."""

    repository_id: str
    old_base_sha: str
    new_base_sha: str
    changed_paths: Tuple[str, ...]
    verifier: str
    verified_at: str


class BaseDeltaVerifier(Protocol):
    def verify(
        self,
        repository: Mapping[str, Any],
        *,
        old_base_sha: str,
        new_base_sha: str,
    ) -> BaseDeltaAttestation: ...


ExternalLineageVerifier = Callable[[Any, Mapping[str, Any]], None]
FailureInjector = Callable[[str], None]


@dataclass(frozen=True)
class WorkPackageCarryDecision:
    node_key: str
    status: str
    reason: str
    source_task_id: Optional[str] = None
    source_evidence_id: Optional[str] = None
    checks: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return {
            "node_key": self.node_key,
            "status": self.status,
            "reason": self.reason,
            "source_task_id": self.source_task_id,
            "source_evidence_id": self.source_evidence_id,
            "checks": dict(self.checks),
        }


@dataclass(frozen=True)
class WorkPackageReplanProposal:
    package_id: str
    expected_plan_version: int
    expected_epoch: int
    proposed_plan_version: int
    proposed_epoch: int
    compiled: CompiledWorkPackagePlan
    base_attestation: RepositoryBaseAttestation
    actor: str
    reason: str
    repository_snapshot: Tuple[Tuple[str, Any], ...] = field(repr=False)

    @property
    def proposal_digest(self) -> str:
        payload = {
            "package_id": self.package_id,
            "expected_plan_version": self.expected_plan_version,
            "expected_epoch": self.expected_epoch,
            "proposed_plan_version": self.proposed_plan_version,
            "proposed_epoch": self.proposed_epoch,
            "plan_digest": self.compiled.plan_digest,
            "planning_base_ref": self.compiled.definition["planning_base_ref"],
            "planning_base_sha": self.compiled.definition["planning_base_sha"],
        }
        return (
            "sha256:%s"
            % hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()
        )

    def to_dict(self) -> JsonDict:
        return {
            "package_id": self.package_id,
            "expected_plan_version": self.expected_plan_version,
            "expected_epoch": self.expected_epoch,
            "proposed_plan_version": self.proposed_plan_version,
            "proposed_epoch": self.proposed_epoch,
            "plan_digest": self.compiled.plan_digest,
            "proposal_digest": self.proposal_digest,
            "base_attestation": self.base_attestation.to_dict(),
            "actor": self.actor,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WorkPackageReplanPreview:
    package_id: str
    expected_plan_version: int
    expected_epoch: int
    current_plan_version: Optional[int]
    current_epoch: Optional[int]
    current_state: Optional[str]
    proposed_plan_version: int
    proposed_epoch: int
    plan_digest: str
    affected_node_keys: Tuple[str, ...]
    invalidated_node_keys: Tuple[str, ...]
    new_node_keys: Tuple[str, ...]
    carry_decisions: Tuple[WorkPackageCarryDecision, ...]
    blockers: Tuple[str, ...]

    @property
    def can_apply(self) -> bool:
        return not self.blockers

    def to_dict(self) -> JsonDict:
        return {
            "package_id": self.package_id,
            "expected_plan_version": self.expected_plan_version,
            "expected_epoch": self.expected_epoch,
            "current_plan_version": self.current_plan_version,
            "current_epoch": self.current_epoch,
            "current_state": self.current_state,
            "proposed_plan_version": self.proposed_plan_version,
            "proposed_epoch": self.proposed_epoch,
            "plan_digest": self.plan_digest,
            "affected_node_keys": list(self.affected_node_keys),
            "invalidated_node_keys": list(self.invalidated_node_keys),
            "new_node_keys": list(self.new_node_keys),
            "carry_decisions": [item.to_dict() for item in self.carry_decisions],
            "blockers": list(self.blockers),
            "can_apply": self.can_apply,
        }


@dataclass(frozen=True)
class WorkPackagePauseResult:
    package_id: str
    plan_version: int
    epoch: int
    state: str
    changed: bool
    reason: str

    def to_dict(self) -> JsonDict:
        return {
            "package_id": self.package_id,
            "plan_version": self.plan_version,
            "epoch": self.epoch,
            "state": self.state,
            "changed": self.changed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WorkPackageReplanResult:
    package_id: str
    plan_version: int
    epoch: int
    plan_digest: str
    task_ids: Tuple[str, ...]
    affected_node_keys: Tuple[str, ...]
    lineage_count: int
    state: str
    created: bool

    def to_dict(self) -> JsonDict:
        return {
            "package_id": self.package_id,
            "plan_version": self.plan_version,
            "epoch": self.epoch,
            "plan_digest": self.plan_digest,
            "task_ids": list(self.task_ids),
            "affected_node_keys": list(self.affected_node_keys),
            "lineage_count": self.lineage_count,
            "state": self.state,
            "created": self.created,
        }


class WorkPackageReplanService:
    """Pause, preview, and atomically replace one package execution epoch."""

    def __init__(
        self,
        store: Store,
        *,
        repository_verifier: Optional[RepositoryBaseVerifier] = None,
        base_delta_verifier: Optional[BaseDeltaVerifier] = None,
        external_lineage_verifier: Optional[ExternalLineageVerifier] = None,
        max_plan_versions: int = 11,
        failure_injector: Optional[FailureInjector] = None,
    ) -> None:
        if max_plan_versions < 2 or max_plan_versions > 100:
            raise ValidationError("max_plan_versions must be between 2 and 100")
        self.store = store
        self.repository_verifier = repository_verifier or GitRepositoryBaseVerifier()
        self.base_delta_verifier = base_delta_verifier
        self.external_lineage_verifier = external_lineage_verifier
        self.max_plan_versions = int(max_plan_versions)
        self.failure_injector = failure_injector

    def pause(
        self,
        package_id: str,
        *,
        expected_plan_version: int,
        expected_epoch: int,
        actor: str,
        reason: str,
    ) -> WorkPackagePauseResult:
        """Raise an Andon by CAS and stop all new package admission."""

        package_value = _required(package_id, "work package id")
        actor_value = _required(actor, "work package pause actor")
        reason_value = _required(reason, "work package pause reason")
        expected_plan_version = _positive_int(
            expected_plan_version, "expected plan version"
        )
        expected_epoch = _positive_int(expected_epoch, "expected epoch")
        now = utcnow()
        with self.store.transaction() as conn:
            package = conn.execute(
                "SELECT * FROM work_packages WHERE id = ?", (package_value,)
            ).fetchone()
            if package is None:
                raise ValidationError("work package not found: %s" % package_value)
            if int(package["current_plan_version"]) != int(
                expected_plan_version
            ) or int(package["current_epoch"]) != int(expected_epoch):
                raise TransitionError("work package pause CAS did not match")
            if package["state"] == "paused":
                return WorkPackagePauseResult(
                    package_id=package_value,
                    plan_version=expected_plan_version,
                    epoch=expected_epoch,
                    state="paused",
                    changed=False,
                    reason=reason_value,
                )
            if package["state"] not in {"admitted", "active"}:
                raise TransitionError(
                    "only an admitted or active work package can be paused"
                )
            cursor = conn.execute(
                "UPDATE work_packages SET state = ?, updated_at = ? "
                "WHERE id = ? AND state = ? AND current_plan_version = ? "
                "AND current_epoch = ?",
                (
                    "paused",
                    now,
                    package_value,
                    package["state"],
                    expected_plan_version,
                    expected_epoch,
                ),
            )
            if cursor.rowcount != 1:
                raise TransitionError("work package pause CAS was lost")
            self._hold_epoch_tasks(
                conn,
                package_id=package_value,
                plan_version=expected_plan_version,
                epoch=expected_epoch,
                now=now,
            )
            self._append_history(
                conn,
                package_id=package_value,
                event_type="work_package.andon_raised",
                actor=actor_value,
                plan_version=expected_plan_version,
                epoch=expected_epoch,
                detail={"reason": reason_value},
                now=now,
            )
        return WorkPackagePauseResult(
            package_id=package_value,
            plan_version=expected_plan_version,
            epoch=expected_epoch,
            state="paused",
            changed=True,
            reason=reason_value,
        )

    def propose(
        self,
        raw_plan: Mapping[str, Any],
        *,
        package_id: str,
        expected_plan_version: int,
        expected_epoch: int,
        actor: str,
        reason: str,
    ) -> WorkPackageReplanProposal:
        """Compile and attest plan N+1 without mutating package state."""

        package_value = _required(package_id, "work package id")
        actor_value = _required(actor, "work package replan actor")
        reason_value = _required(reason, "work package replan reason")
        expected_plan_version = _positive_int(
            expected_plan_version, "expected plan version"
        )
        expected_epoch = _positive_int(expected_epoch, "expected epoch")
        compiled = compile_work_package_plan(raw_plan)
        validate_executable_work_package_effects(compiled)
        definition = compiled.definition
        if definition["package_id"] != package_value:
            raise ValidationError("replacement plan package_id does not match")
        package = self.store.query_one(
            "SELECT * FROM work_packages WHERE id = ?", (package_value,)
        )
        if package is None:
            raise ValidationError("work package not found: %s" % package_value)
        if int(package["current_plan_version"]) != int(expected_plan_version) or int(
            package["current_epoch"]
        ) != int(expected_epoch):
            raise TransitionError("work package replan proposal CAS did not match")
        if package["repository_id"] != definition["repository_id"]:
            raise ValidationError("replacement plan changes the registered repository")
        if (package["project"] or None) != (definition.get("project") or None):
            raise ValidationError("replacement plan changes the package project")
        repository = self._registered_repository(str(definition["repository_id"]))
        self._validate_repository(repository, definition)
        attestation = self.repository_verifier.verify(
            repository,
            planning_base_ref=str(definition["planning_base_ref"]),
            planning_base_sha=str(definition["planning_base_sha"]),
        )
        self._validate_attestation(attestation, definition)
        return WorkPackageReplanProposal(
            package_id=package_value,
            expected_plan_version=int(expected_plan_version),
            expected_epoch=int(expected_epoch),
            proposed_plan_version=int(expected_plan_version) + 1,
            proposed_epoch=int(expected_epoch) + 1,
            compiled=compiled,
            base_attestation=attestation,
            actor=actor_value,
            reason=reason_value,
            repository_snapshot=tuple(
                (key, repository.get(key)) for key in _REPOSITORY_IDENTITY_FIELDS
            ),
        )

    def preview(self, proposal: WorkPackageReplanProposal) -> WorkPackageReplanPreview:
        """Return a non-mutating affected-cone and carry-forward decision."""

        self._validate_proposal_integrity(proposal)
        package = self.store.query_one(
            "SELECT * FROM work_packages WHERE id = ?", (proposal.package_id,)
        )
        if package is None:
            return self._missing_preview(proposal)
        return self._preview_from_reader(self.store, proposal, dict(package))

    def apply(
        self,
        proposal: WorkPackageReplanProposal,
        *,
        expected_plan_version: int,
        expected_epoch: int,
    ) -> WorkPackageReplanResult:
        """Atomically materialize plan N+1 and epoch E+1, leaving it paused."""

        expected_plan_version = _positive_int(
            expected_plan_version, "expected plan version"
        )
        expected_epoch = _positive_int(expected_epoch, "expected epoch")
        if (
            expected_plan_version != proposal.expected_plan_version
            or expected_epoch != proposal.expected_epoch
        ):
            raise TransitionError("apply CAS does not match the replan proposal")
        self._validate_proposal_integrity(proposal)

        idempotent = self._idempotent_result(proposal)
        if idempotent is not None:
            return idempotent

        # Repository I/O is never performed while a controller transaction is
        # held.  The registry identity and observation are checked again under
        # the transaction immediately below.
        repository = self._registered_repository(
            str(proposal.compiled.definition["repository_id"])
        )
        proposed_repository = dict(proposal.repository_snapshot)
        if any(
            repository.get(key) != proposed_repository.get(key)
            for key in _REPOSITORY_IDENTITY_FIELDS
        ):
            raise TransitionError("registered repository changed after replan proposal")
        self._validate_repository(repository, proposal.compiled.definition)
        attestation = self.repository_verifier.verify(
            repository,
            planning_base_ref=str(proposal.compiled.definition["planning_base_ref"]),
            planning_base_sha=str(proposal.compiled.definition["planning_base_sha"]),
        )
        self._validate_attestation(attestation, proposal.compiled.definition)
        delta_context = self._observe_base_delta(proposal)

        now = utcnow()
        with self.store.transaction() as conn:
            lock = conn.execute(
                "UPDATE work_packages SET updated_at = updated_at WHERE id = ?",
                (proposal.package_id,),
            )
            if lock.rowcount != 1:
                raise ValidationError(
                    "work package not found: %s" % proposal.package_id
                )
            package_row = conn.execute(
                "SELECT * FROM work_packages WHERE id = ?", (proposal.package_id,)
            ).fetchone()
            if package_row is None:
                raise ValidationError(
                    "work package not found: %s" % proposal.package_id
                )
            package = dict(package_row)
            preview = self._preview_from_reader(
                conn,
                proposal,
                package,
                delta_context=delta_context,
            )
            if preview.blockers:
                raise TransitionError(
                    "work package replan blocked: %s" % "; ".join(preview.blockers)
                )
            current_repository_row = conn.execute(
                "SELECT * FROM project_repositories WHERE id = ?",
                (proposal.compiled.definition["repository_id"],),
            ).fetchone()
            if current_repository_row is None:
                raise TransitionError("registered repository disappeared during replan")
            current_repository = dict(current_repository_row)
            if any(
                current_repository.get(key) != proposed_repository.get(key)
                for key in _REPOSITORY_IDENTITY_FIELDS
            ):
                raise TransitionError(
                    "registered repository changed after replan proposal"
                )
            if any(
                current_repository.get(key) != repository.get(key)
                for key in _REPOSITORY_IDENTITY_FIELDS
            ):
                raise TransitionError(
                    "registered repository changed during base attestation"
                )
            self._validate_external_dependencies(conn, proposal.compiled)

            conn.execute(
                "INSERT INTO work_package_plan_versions ("
                "package_id, version, parent_version, definition, plan_digest, reason, "
                "created_by, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal.package_id,
                    proposal.proposed_plan_version,
                    proposal.expected_plan_version,
                    json_dumps(proposal.compiled.definition),
                    proposal.compiled.plan_digest,
                    proposal.reason,
                    proposal.actor,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO work_package_epochs ("
                "package_id, epoch, plan_version, planning_base_ref, planning_base_sha, "
                "status, reason, created_by, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal.package_id,
                    proposal.proposed_epoch,
                    proposal.proposed_plan_version,
                    proposal.compiled.definition["planning_base_ref"],
                    proposal.compiled.definition["planning_base_sha"],
                    "staged",
                    proposal.reason,
                    proposal.actor,
                    now,
                ),
            )
            self._checkpoint("plan_staged")
            for node in proposal.compiled.task_specs:
                self._insert_materialized_task(
                    conn,
                    proposal=proposal,
                    node=node,
                    project=proposal.compiled.definition.get("project"),
                    now=now,
                )
            self._checkpoint("tasks_materialized")

            old_links = self._old_links(conn, proposal)
            carry_by_key = {item.node_key: item for item in preview.carry_decisions}
            new_nodes = {node.node_key: node for node in proposal.compiled.task_specs}
            for old_link in old_links:
                node_key = str(old_link["node_key"])
                new_node = new_nodes[node_key]
                new_task_id = str(
                    proposal.compiled.materialization_map[node_key]["task_id"]
                )
                relation = (
                    "invalidated"
                    if node_key in set(preview.affected_node_keys)
                    else "replaced"
                )
                conn.execute(
                    "INSERT INTO work_package_node_lineage ("
                    "id, package_id, from_plan_version, from_epoch, from_node_key, "
                    "from_task_id, to_plan_version, to_epoch, to_node_key, to_task_id, "
                    "relation, contract_digest, input_digest, source_evidence_id, "
                    "decision, created_by, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id("wpl"),
                        proposal.package_id,
                        proposal.expected_plan_version,
                        proposal.expected_epoch,
                        node_key,
                        old_link["task_id"],
                        proposal.proposed_plan_version,
                        proposal.proposed_epoch,
                        node_key,
                        new_task_id,
                        relation,
                        new_node.contract_digest,
                        new_node.input_digest,
                        None,
                        json_dumps(
                            {
                                "schema": "mac.work_package.replan_lineage.v1",
                                "service_version": WORK_PACKAGE_REPLAN_SERVICE_VERSION,
                                "proposal_digest": proposal.proposal_digest,
                                "affected": node_key in set(preview.affected_node_keys),
                                "source_node_state": old_link["node_state"],
                                "rework_consumed": old_link["node_state"]
                                in _REWORK_CONSUMING_NODE_STATES,
                                "carry": carry_by_key[node_key].to_dict(),
                                "reason": proposal.reason,
                            }
                        ),
                        proposal.actor,
                        now,
                    ),
                )
            self._checkpoint("lineage_written")

            state_cursor = conn.execute(
                "UPDATE work_packages SET state = ?, updated_at = ? "
                "WHERE id = ? AND state = ? AND current_plan_version = ? "
                "AND current_epoch = ?",
                (
                    "replanning",
                    now,
                    proposal.package_id,
                    "paused",
                    proposal.expected_plan_version,
                    proposal.expected_epoch,
                ),
            )
            if state_cursor.rowcount != 1:
                raise TransitionError("work package replan CAS was lost")

            conn.execute(
                "UPDATE work_package_node_candidates SET status = ? "
                "WHERE package_id = ? AND plan_version = ? AND epoch = ? "
                "AND status = ?",
                (
                    "superseded",
                    proposal.package_id,
                    proposal.expected_plan_version,
                    proposal.expected_epoch,
                    "submitted",
                ),
            )
            conn.execute(
                "UPDATE work_package_wip_tokens SET state = ?, released_at = ?, "
                "release_reason = ? WHERE package_id = ? AND plan_version = ? "
                "AND epoch = ? AND state = ?",
                (
                    "superseded",
                    now,
                    "replan:%s" % proposal.proposal_digest,
                    proposal.package_id,
                    proposal.expected_plan_version,
                    proposal.expected_epoch,
                    "held",
                ),
            )
            self._hold_epoch_tasks(
                conn,
                package_id=proposal.package_id,
                plan_version=proposal.expected_plan_version,
                epoch=proposal.expected_epoch,
                now=now,
            )
            for old_link in old_links:
                cursor = conn.execute(
                    "UPDATE work_package_task_links SET node_state = ? "
                    "WHERE task_id = ? AND node_state = ?",
                    ("superseded", old_link["task_id"], old_link["node_state"]),
                )
                if cursor.rowcount != 1:
                    raise TransitionError(
                        "old work-package node changed during replan: %s"
                        % old_link["node_key"]
                    )
            self._checkpoint("old_epoch_fenced")

            old_epoch = conn.execute(
                "UPDATE work_package_epochs SET status = ?, superseded_at = ? "
                "WHERE package_id = ? AND epoch = ? AND plan_version = ? "
                "AND status = ?",
                (
                    "superseded",
                    now,
                    proposal.package_id,
                    proposal.expected_epoch,
                    proposal.expected_plan_version,
                    "active",
                ),
            )
            if old_epoch.rowcount != 1:
                raise TransitionError("current work-package epoch was not active")
            new_epoch = conn.execute(
                "UPDATE work_package_epochs SET status = ? "
                "WHERE package_id = ? AND epoch = ? AND plan_version = ? "
                "AND status = ?",
                (
                    "active",
                    proposal.package_id,
                    proposal.proposed_epoch,
                    proposal.proposed_plan_version,
                    "staged",
                ),
            )
            if new_epoch.rowcount != 1:
                raise TransitionError("staged work-package epoch was not activated")
            package_cursor = conn.execute(
                "UPDATE work_packages SET state = ?, current_plan_version = ?, "
                "current_epoch = ?, updated_at = ? WHERE id = ? AND state = ? "
                "AND current_plan_version = ? AND current_epoch = ?",
                (
                    "paused",
                    proposal.proposed_plan_version,
                    proposal.proposed_epoch,
                    now,
                    proposal.package_id,
                    "replanning",
                    proposal.expected_plan_version,
                    proposal.expected_epoch,
                ),
            )
            if package_cursor.rowcount != 1:
                raise TransitionError("work package epoch activation CAS was lost")
            task_ids = tuple(
                str(proposal.compiled.materialization_map[key]["task_id"])
                for key in proposal.compiled.topological_order
            )
            self._append_history(
                conn,
                package_id=proposal.package_id,
                event_type="work_package.replanned",
                actor=proposal.actor,
                plan_version=proposal.proposed_plan_version,
                epoch=proposal.proposed_epoch,
                detail={
                    "proposal_digest": proposal.proposal_digest,
                    "parent_plan_version": proposal.expected_plan_version,
                    "superseded_epoch": proposal.expected_epoch,
                    "plan_digest": proposal.compiled.plan_digest,
                    "affected_node_keys": list(preview.affected_node_keys),
                    "task_ids": list(task_ids),
                    "state": "paused",
                    "reason": proposal.reason,
                },
                now=now,
            )
            self._checkpoint("before_commit")

        return WorkPackageReplanResult(
            package_id=proposal.package_id,
            plan_version=proposal.proposed_plan_version,
            epoch=proposal.proposed_epoch,
            plan_digest=proposal.compiled.plan_digest,
            task_ids=task_ids,
            affected_node_keys=preview.affected_node_keys,
            lineage_count=len(old_links),
            state="paused",
            created=True,
        )

    def _preview_from_reader(
        self,
        reader: Any,
        proposal: WorkPackageReplanProposal,
        package: Mapping[str, Any],
        *,
        delta_context: Optional[
            Tuple[Optional[BaseDeltaAttestation], Optional[str]]
        ] = None,
    ) -> WorkPackageReplanPreview:
        blockers = []
        current_version = int(package["current_plan_version"])
        current_epoch = int(package["current_epoch"])
        current_state = str(package["state"])
        if (
            current_version != proposal.expected_plan_version
            or current_epoch != proposal.expected_epoch
        ):
            blockers.append("stale plan-version/epoch CAS")
        if current_state != "paused":
            blockers.append("package must be paused by Andon before apply")
        try:
            plan_version_limit = self._plan_version_limit(reader, proposal.package_id)
        except ValidationError as exc:
            blockers.append(str(exc))
            plan_version_limit = 0
        if proposal.proposed_plan_version > plan_version_limit:
            blockers.append("package replan budget is exhausted")

        old_plan_row = _query_one(
            reader,
            "SELECT definition, plan_digest FROM work_package_plan_versions "
            "WHERE package_id = ? AND version = ?",
            (proposal.package_id, proposal.expected_plan_version),
        )
        if old_plan_row is None:
            blockers.append("expected parent plan version is missing")
            old_definition: JsonDict = {"nodes": []}
        else:
            old_definition = json_loads(old_plan_row["definition"], {})
        old_nodes = {
            str(node.get("node_key")): node
            for node in old_definition.get("nodes", [])
            if isinstance(node, Mapping) and node.get("node_key")
        }
        new_nodes = {node.node_key: node for node in proposal.compiled.task_specs}
        removed = sorted(set(old_nodes) - set(new_nodes))
        if removed:
            blockers.append(
                "schema cannot record lineage for removed nodes: %s"
                % ", ".join(removed)
            )

        old_links = _query_all(
            reader,
            "SELECT link.*, task.state AS task_state "
            "FROM work_package_task_links AS link "
            "JOIN tasks AS task ON task.id = link.task_id "
            "WHERE link.package_id = ? AND link.plan_version = ? "
            "AND link.epoch = ? ORDER BY link.node_key",
            (
                proposal.package_id,
                proposal.expected_plan_version,
                proposal.expected_epoch,
            ),
        )
        old_link_by_key = {str(row["node_key"]): dict(row) for row in old_links}
        if set(old_link_by_key) != set(old_nodes):
            blockers.append("current epoch task links do not match its immutable plan")
        active_leases = _query_all(
            reader,
            "SELECT lease.id FROM leases AS lease "
            "JOIN work_package_assignment_audit AS assignment "
            "ON assignment.lease_id = lease.id "
            "WHERE assignment.package_id = ? AND assignment.plan_version = ? "
            "AND assignment.epoch = ? AND lease.status = ? ORDER BY lease.id",
            (
                proposal.package_id,
                proposal.expected_plan_version,
                proposal.expected_epoch,
                "active",
            ),
        )
        if active_leases:
            blockers.append(
                "active package leases require the package-aware expiry finalizer"
            )
        unsupersedable = sorted(
            "%s:%s" % (row["node_key"], row["node_state"])
            for row in old_links
            if row["node_state"] not in _SUPERSEDABLE_NODE_STATES
        )
        if unsupersedable:
            blockers.append(
                "current nodes cannot be safely superseded: %s"
                % ", ".join(unsupersedable)
            )
        live_task_states = sorted(
            "%s:%s" % (row["node_key"], row["task_state"])
            for row in old_links
            if row["task_state"] in {"claimed", "running", "reviewing"}
        )
        if live_task_states:
            blockers.append(
                "current task lifecycle is still live: %s" % ", ".join(live_task_states)
            )

        previous_generation = max(
            (int(row["node_generation"]) for row in old_links), default=0
        )
        proposed_generation = int(
            proposal.compiled.definition.get("plan_generation") or 0
        )
        if proposed_generation != previous_generation + 1:
            blockers.append("replacement plan_generation must increment exactly once")

        direct_changes = set(new_nodes) - set(old_nodes)
        for node_key in sorted(set(old_nodes) & set(new_nodes)):
            old = old_nodes[node_key]
            new = new_nodes[node_key]
            if (
                old.get("contract_digest") != new.contract_digest
                or old.get("input_digest") != new.input_digest
                or tuple(old.get("depends_on") or ()) != new.depends_on
            ):
                direct_changes.add(node_key)
        old_base_sha = str(old_definition.get("planning_base_sha") or "")
        new_base_sha = str(proposal.compiled.definition["planning_base_sha"])
        if old_base_sha != new_base_sha:
            # Base-delta verification is used below for per-node carry safety.
            # Until carry can be materialized exactly, the conservative
            # execution cone treats every existing node as affected.
            direct_changes.update(set(old_nodes) & set(new_nodes))
        affected = _descendant_cone(new_nodes, direct_changes)

        carry_decisions = []
        delta_attestation: Optional[BaseDeltaAttestation] = None
        delta_error: Optional[str] = None
        if delta_context is not None:
            delta_attestation, delta_error = delta_context
        elif old_base_sha != new_base_sha and self.base_delta_verifier is not None:
            try:
                repository = self._registered_repository(
                    str(proposal.compiled.definition["repository_id"])
                )
                delta_attestation = self.base_delta_verifier.verify(
                    repository,
                    old_base_sha=old_base_sha,
                    new_base_sha=new_base_sha,
                )
                self._validate_delta_attestation(
                    delta_attestation,
                    repository_id=str(proposal.compiled.definition["repository_id"]),
                    old_base_sha=old_base_sha,
                    new_base_sha=new_base_sha,
                )
            except Exception as exc:  # verifier failures are decisions, not authority
                delta_error = str(exc) or exc.__class__.__name__
                delta_attestation = None

        for node_key in sorted(set(old_nodes) & set(new_nodes)):
            carry_decision = self._carry_decision(
                reader,
                proposal=proposal,
                old_definition=old_definition,
                old_node=old_nodes[node_key],
                new_node=new_nodes[node_key],
                old_link=old_link_by_key.get(node_key),
                affected=node_key in affected,
                delta_attestation=delta_attestation,
                delta_error=delta_error,
            )
            carry_decisions.append(carry_decision)
            if old_link_by_key.get(node_key, {}).get("node_state") in (
                _REWORK_CONSUMING_NODE_STATES
            ):
                prior_lineage = _query_all(
                    reader,
                    "SELECT decision FROM work_package_node_lineage "
                    "WHERE package_id = ? AND to_node_key = ? "
                    "AND relation IN (?, ?)",
                    (proposal.package_id, node_key, "replaced", "invalidated"),
                )
                cycles_used = sum(
                    1
                    for row in prior_lineage
                    if json_loads(row["decision"], {}).get("rework_consumed") is True
                )
                max_cycles = int(new_nodes[node_key].rework.get("max_cycles") or 0)
                if cycles_used + 1 > max_cycles:
                    blockers.append("node rework budget exhausted: %s" % node_key)

        return WorkPackageReplanPreview(
            package_id=proposal.package_id,
            expected_plan_version=proposal.expected_plan_version,
            expected_epoch=proposal.expected_epoch,
            current_plan_version=current_version,
            current_epoch=current_epoch,
            current_state=current_state,
            proposed_plan_version=proposal.proposed_plan_version,
            proposed_epoch=proposal.proposed_epoch,
            plan_digest=proposal.compiled.plan_digest,
            affected_node_keys=tuple(sorted(affected)),
            invalidated_node_keys=tuple(sorted(set(old_nodes) & affected)),
            new_node_keys=tuple(sorted(set(new_nodes) - set(old_nodes))),
            carry_decisions=tuple(carry_decisions),
            blockers=tuple(dict.fromkeys(blockers)),
        )

    def _carry_decision(
        self,
        reader: Any,
        *,
        proposal: WorkPackageReplanProposal,
        old_definition: Mapping[str, Any],
        old_node: Mapping[str, Any],
        new_node: WorkPackageNodeSpec,
        old_link: Optional[Mapping[str, Any]],
        affected: bool,
        delta_attestation: Optional[BaseDeltaAttestation],
        delta_error: Optional[str],
    ) -> WorkPackageCarryDecision:
        node_key = new_node.node_key
        checks: JsonDict = {
            "accepted_candidate": False,
            "output_receipt": False,
            "affected_cone": affected,
            "contract_identical": old_node.get("contract_digest")
            == new_node.contract_digest,
            "recursive_input_lineage_identical": (
                old_node.get("input_digest") == new_node.input_digest
                and tuple(old_node.get("depends_on") or ()) == new_node.depends_on
                # Internal predecessor outputs cannot yet be rebound to the
                # new task identities by an append-only receipt.  Mark such
                # recursive lineage unproven instead of inferring it from the
                # plan-level digest alone.
                and not new_node.depends_on
            ),
            "base_delta_scope_clear": False,
            "schema_can_materialize_exact_candidate": False,
        }
        if old_link is None:
            return WorkPackageCarryDecision(
                node_key=node_key,
                status="rerun_required",
                reason="old task link is missing",
                checks=checks,
            )
        candidate = _query_one(
            reader,
            "SELECT * FROM work_package_node_candidates WHERE package_id = ? "
            "AND plan_version = ? AND epoch = ? AND node_key = ? "
            "AND task_id = ? AND status = ? ORDER BY accepted_at DESC LIMIT 1",
            (
                proposal.package_id,
                proposal.expected_plan_version,
                proposal.expected_epoch,
                node_key,
                old_link["task_id"],
                "accepted",
            ),
        )
        if candidate is None:
            return WorkPackageCarryDecision(
                node_key=node_key,
                status="rerun_required",
                reason="no accepted old candidate",
                source_task_id=str(old_link["task_id"]),
                checks=checks,
            )
        checks["accepted_candidate"] = True
        receipt = _query_one(
            reader,
            "SELECT * FROM evidence_attempt_verifications WHERE evidence_id = ? "
            "AND task_id = ? AND lease_id = ? AND attempt_number = ?",
            (
                candidate["evidence_id"],
                old_link["task_id"],
                candidate["assignment_lease_id"],
                candidate["attempt_number"],
            ),
        )
        if receipt is None:
            return WorkPackageCarryDecision(
                node_key=node_key,
                status="blocked",
                reason="accepted candidate lacks an exact append-only output receipt",
                source_task_id=str(old_link["task_id"]),
                source_evidence_id=str(candidate["evidence_id"]),
                checks=checks,
            )
        checks["output_receipt"] = True
        if not checks["contract_identical"]:
            reason = "node contract changed"
        elif not checks["recursive_input_lineage_identical"]:
            reason = "recursive input lineage is not independently re-attested"
        else:
            old_base = str(old_definition.get("planning_base_sha") or "")
            new_base = str(proposal.compiled.definition["planning_base_sha"])
            if old_base == new_base:
                checks["base_delta_scope_clear"] = True
                reason = "schema cannot bind old evidence to the new task identity"
            elif self.base_delta_verifier is None:
                reason = "authoritative base-delta verifier is unavailable"
            elif delta_attestation is None:
                reason = "base-delta verification failed: %s" % (
                    delta_error or "unknown verifier failure"
                )
            else:
                observed_paths = tuple(json_loads(receipt["changed_paths"], []))
                declared = _declared_local_effects(new_node)
                overlap = _path_scope_overlap(
                    delta_attestation.changed_paths,
                    tuple(declared) + observed_paths,
                )
                checks["base_delta_scope_clear"] = not overlap
                reason = (
                    "base delta intersects observed or declared effects"
                    if overlap
                    else "schema cannot bind old evidence to the new task identity"
                )
        return WorkPackageCarryDecision(
            node_key=node_key,
            status="blocked",
            reason=reason,
            source_task_id=str(old_link["task_id"]),
            source_evidence_id=str(candidate["evidence_id"]),
            checks=checks,
        )

    def _idempotent_result(
        self, proposal: WorkPackageReplanProposal
    ) -> Optional[WorkPackageReplanResult]:
        package = self.store.query_one(
            "SELECT * FROM work_packages WHERE id = ?", (proposal.package_id,)
        )
        if package is None:
            return None
        if (
            int(package["current_plan_version"]) == proposal.expected_plan_version
            and int(package["current_epoch"]) == proposal.expected_epoch
        ):
            return None
        if (
            int(package["current_plan_version"]) != proposal.proposed_plan_version
            or int(package["current_epoch"]) != proposal.proposed_epoch
        ):
            return None
        plan = self.store.query_one(
            "SELECT plan_digest FROM work_package_plan_versions "
            "WHERE package_id = ? AND version = ?",
            (proposal.package_id, proposal.proposed_plan_version),
        )
        epoch = self.store.query_one(
            "SELECT * FROM work_package_epochs WHERE package_id = ? AND epoch = ? "
            "AND plan_version = ?",
            (
                proposal.package_id,
                proposal.proposed_epoch,
                proposal.proposed_plan_version,
            ),
        )
        if (
            plan is None
            or plan["plan_digest"] != proposal.compiled.plan_digest
            or epoch is None
            or epoch["status"] != "active"
            or package["state"] != "paused"
            or epoch["planning_base_ref"]
            != proposal.compiled.definition["planning_base_ref"]
            or epoch["planning_base_sha"]
            != proposal.compiled.definition["planning_base_sha"]
        ):
            raise TransitionError(
                "work package advanced to an incoherent or different replan"
            )
        expected_tasks = tuple(
            str(proposal.compiled.materialization_map[key]["task_id"])
            for key in proposal.compiled.topological_order
        )
        links = self.store.query_all(
            "SELECT task_id, node_key, contract_digest, input_digest, "
            "declared_effects_digest FROM work_package_task_links "
            "WHERE package_id = ? AND plan_version = ? AND epoch = ? "
            "ORDER BY node_key",
            (
                proposal.package_id,
                proposal.proposed_plan_version,
                proposal.proposed_epoch,
            ),
        )
        observed = {
            row["node_key"]: (
                row["task_id"],
                row["contract_digest"],
                row["input_digest"],
                row["declared_effects_digest"],
            )
            for row in links
        }
        expected = {
            node.node_key: (
                proposal.compiled.materialization_map[node.node_key]["task_id"],
                node.contract_digest,
                node.input_digest,
                node.effects_digest,
            )
            for node in proposal.compiled.task_specs
        }
        if observed != expected:
            raise TransitionError("idempotent replan materialization is incomplete")
        lineage_count = self.store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_node_lineage "
            "WHERE package_id = ? AND from_plan_version = ? AND from_epoch = ? "
            "AND to_plan_version = ? AND to_epoch = ?",
            (
                proposal.package_id,
                proposal.expected_plan_version,
                proposal.expected_epoch,
                proposal.proposed_plan_version,
                proposal.proposed_epoch,
            ),
        )
        old_count = self.store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_task_links "
            "WHERE package_id = ? AND plan_version = ? AND epoch = ?",
            (
                proposal.package_id,
                proposal.expected_plan_version,
                proposal.expected_epoch,
            ),
        )
        if int(lineage_count["n"]) != int(old_count["n"]):
            raise TransitionError("idempotent replan lineage is incomplete")
        return WorkPackageReplanResult(
            package_id=proposal.package_id,
            plan_version=proposal.proposed_plan_version,
            epoch=proposal.proposed_epoch,
            plan_digest=proposal.compiled.plan_digest,
            task_ids=expected_tasks,
            affected_node_keys=tuple(
                sorted(
                    row["to_node_key"]
                    for row in self.store.query_all(
                        "SELECT to_node_key, decision FROM work_package_node_lineage "
                        "WHERE package_id = ? AND from_plan_version = ? "
                        "AND from_epoch = ? AND to_plan_version = ? AND to_epoch = ?",
                        (
                            proposal.package_id,
                            proposal.expected_plan_version,
                            proposal.expected_epoch,
                            proposal.proposed_plan_version,
                            proposal.proposed_epoch,
                        ),
                    )
                    if json_loads(row["decision"], {}).get("affected")
                )
            ),
            lineage_count=int(lineage_count["n"]),
            state="paused",
            created=False,
        )

    def _insert_materialized_task(
        self,
        conn: Any,
        *,
        proposal: WorkPackageReplanProposal,
        node: WorkPackageNodeSpec,
        project: Optional[str],
        now: str,
    ) -> None:
        compiled = proposal.compiled
        materialized = compiled.materialization_map[node.node_key]
        task_id = str(materialized["task_id"])
        dependencies = sorted(
            {
                *(
                    str(compiled.materialization_map[key]["task_id"])
                    for key in node.depends_on
                ),
                *(str(item["task_id"]) for item in node.external_dependencies),
            }
        )
        metadata = dict(node.metadata)
        metadata["no_dispatch"] = True
        metadata["work_package"] = {
            "schema": "mac.work_package.task.v1",
            "package_id": proposal.package_id,
            "plan_version": proposal.proposed_plan_version,
            "epoch": proposal.proposed_epoch,
            "node_key": node.node_key,
            "node_generation": materialized["node_generation"],
            "node_type": node.node_type,
            "planning_base_ref": compiled.definition["planning_base_ref"],
            "planning_base_sha": compiled.definition["planning_base_sha"],
            "contract_digest": node.contract_digest,
            "input_digest": node.input_digest,
            "declared_effects_digest": node.effects_digest,
            "effects": node.effects.to_dict(),
            "expected_outputs": [dict(item) for item in node.expected_outputs],
            "verification": dict(node.verification),
            "materializer_version": WORK_PACKAGE_MATERIALIZER_VERSION,
            "replan_service_version": WORK_PACKAGE_REPLAN_SERVICE_VERSION,
            "proposal_digest": proposal.proposal_digest,
        }
        task_state = "waiting" if dependencies else "open"
        conn.execute(
            "INSERT INTO tasks ("
            "id, title, description, project, priority, state, required_capabilities, "
            "dependencies, metadata, attempt_count, max_attempts, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                node.title,
                node.description,
                project,
                node.priority,
                task_state,
                json_dumps(list(node.required_capabilities)),
                json_dumps(dependencies),
                json_dumps(metadata),
                0,
                node.max_attempts,
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO task_history ("
            "id, task_id, event_type, actor, from_state, to_state, detail, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("history"),
                task_id,
                "task.materialized",
                proposal.actor,
                None,
                task_state,
                json_dumps(
                    {
                        "package_id": proposal.package_id,
                        "plan_digest": compiled.plan_digest,
                        "plan_version": proposal.proposed_plan_version,
                        "epoch": proposal.proposed_epoch,
                        "node_key": node.node_key,
                        "held": True,
                    }
                ),
                now,
            ),
        )
        conn.execute(
            "INSERT INTO work_package_task_links ("
            "task_id, package_id, plan_version, epoch, node_key, node_generation, "
            "declared_effects_digest, contract_digest, input_digest, node_state, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                proposal.package_id,
                proposal.proposed_plan_version,
                proposal.proposed_epoch,
                node.node_key,
                materialized["node_generation"],
                node.effects_digest,
                node.contract_digest,
                node.input_digest,
                "planned",
                now,
            ),
        )

    def _validate_external_dependencies(
        self, conn: Any, compiled: CompiledWorkPackagePlan
    ) -> None:
        seen = set()
        for node in compiled.task_specs:
            for dependency in node.external_dependencies:
                task_id = str(dependency["task_id"])
                if task_id not in seen:
                    if (
                        conn.execute(
                            "SELECT id FROM tasks WHERE id = ?", (task_id,)
                        ).fetchone()
                        is None
                    ):
                        raise ValidationError(
                            "work package external dependency was not found"
                        )
                    seen.add(task_id)
                if dependency.get("lineage_status") == "resolved":
                    if self.external_lineage_verifier is None:
                        raise ValidationError(
                            "resolved external lineage requires a controller verifier"
                        )
                    self.external_lineage_verifier(conn, dependency)

    def _plan_version_limit(self, reader: Any, package_id: str) -> int:
        initial = _query_one(
            reader,
            "SELECT definition FROM work_package_plan_versions "
            "WHERE package_id = ? AND version = ?",
            (package_id, 1),
        )
        if initial is None:
            raise ValidationError("initial package policy is missing")
        definition = json_loads(initial["definition"], {})
        metadata = definition.get("metadata") or {}
        policy = metadata.get("replan_policy") or {}
        value = policy.get("max_plan_versions", 11)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 2 <= value <= 100
        ):
            raise ValidationError("durable package replan policy is invalid")
        return min(self.max_plan_versions, value)

    @staticmethod
    def _validate_proposal_integrity(proposal: WorkPackageReplanProposal) -> None:
        if (
            proposal.proposed_plan_version != proposal.expected_plan_version + 1
            or proposal.proposed_epoch != proposal.expected_epoch + 1
        ):
            raise ValidationError("replan proposal version or epoch is not monotonic")
        if proposal.compiled.definition.get("package_id") != proposal.package_id:
            raise ValidationError("replan proposal package identity is incoherent")
        canonical = json_dumps(proposal.compiled.definition)
        expected_digest = (
            "sha256:%s" % hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        )
        if proposal.compiled.plan_digest != expected_digest:
            raise ValidationError(
                "replan proposal definition was mutated after compile"
            )
        definition_nodes = proposal.compiled.definition.get("nodes") or []
        if not isinstance(definition_nodes, list):
            raise ValidationError("replan proposal has an invalid canonical node list")
        by_key = {
            str(item.get("node_key")): item
            for item in definition_nodes
            if isinstance(item, Mapping) and item.get("node_key")
        }
        if tuple(node.node_key for node in proposal.compiled.task_specs) != tuple(
            proposal.compiled.topological_order
        ) or set(by_key) != set(proposal.compiled.topological_order):
            raise ValidationError("replan proposal topology is incoherent")
        generation = int(proposal.compiled.definition.get("plan_generation") or 0)
        for node in proposal.compiled.task_specs:
            if node.to_dict() != by_key.get(node.node_key):
                raise ValidationError("replan proposal node was mutated after compile")
            materialized = proposal.compiled.materialization_map.get(node.node_key)
            if not isinstance(materialized, Mapping):
                raise ValidationError(
                    "replan proposal materialization map is incomplete"
                )
            task_id = (
                "task_wp_%s"
                % hashlib.sha256(
                    (
                        proposal.package_id
                        + "\x00"
                        + proposal.compiled.plan_digest
                        + "\x00"
                        + node.node_key
                    ).encode("utf-8")
                ).hexdigest()[:24]
            )
            expected = {
                "task_id": task_id,
                "node_generation": generation,
                "contract_digest": node.contract_digest,
                "input_digest": node.input_digest,
                "input_lineage_status": node.input_lineage_status,
                "carry_forward_eligible": node.carry_forward_eligible,
                "declared_effects_digest": node.effects_digest,
            }
            if dict(materialized) != expected:
                raise ValidationError(
                    "replan proposal materialization map was mutated after compile"
                )
        snapshot_keys = tuple(key for key, _value in proposal.repository_snapshot)
        if snapshot_keys != _REPOSITORY_IDENTITY_FIELDS:
            raise ValidationError("replan proposal repository snapshot is incoherent")
        WorkPackageReplanService._validate_attestation(
            proposal.base_attestation, proposal.compiled.definition
        )
        _required(proposal.actor, "work package replan actor")
        _required(proposal.reason, "work package replan reason")

    def _observe_base_delta(
        self, proposal: WorkPackageReplanProposal
    ) -> Tuple[Optional[BaseDeltaAttestation], Optional[str]]:
        plan = self.store.query_one(
            "SELECT definition FROM work_package_plan_versions "
            "WHERE package_id = ? AND version = ?",
            (proposal.package_id, proposal.expected_plan_version),
        )
        if plan is None:
            return None, "expected parent plan version is missing"
        old_definition = json_loads(plan["definition"], {})
        old_base_sha = str(old_definition.get("planning_base_sha") or "")
        new_base_sha = str(proposal.compiled.definition["planning_base_sha"])
        if old_base_sha == new_base_sha or self.base_delta_verifier is None:
            return None, None
        try:
            repository = self._registered_repository(
                str(proposal.compiled.definition["repository_id"])
            )
            attestation = self.base_delta_verifier.verify(
                repository,
                old_base_sha=old_base_sha,
                new_base_sha=new_base_sha,
            )
            self._validate_delta_attestation(
                attestation,
                repository_id=str(proposal.compiled.definition["repository_id"]),
                old_base_sha=old_base_sha,
                new_base_sha=new_base_sha,
            )
            return attestation, None
        except Exception as exc:  # external verifier failure is a blocked carry
            return None, str(exc) or exc.__class__.__name__

    def _registered_repository(self, repository_id: str) -> JsonDict:
        row = self.store.query_one(
            "SELECT * FROM project_repositories WHERE id = ?", (repository_id,)
        )
        if row is None:
            raise ValidationError("work package repository is not registered")
        return dict(row)

    @staticmethod
    def _validate_repository(
        repository: Mapping[str, Any], definition: Mapping[str, Any]
    ) -> None:
        if not bool(repository.get("enabled")):
            raise ValidationError("work package repository is disabled")
        if str(repository.get("id")) != str(definition.get("repository_id")):
            raise ValidationError("registered repository identity does not match plan")
        plan_project = definition.get("project")
        if plan_project and str(repository.get("project") or "") != str(plan_project):
            raise ValidationError("work package project does not own the repository")

    @staticmethod
    def _validate_attestation(
        attestation: RepositoryBaseAttestation,
        definition: Mapping[str, Any],
    ) -> None:
        expected = (
            str(definition["repository_id"]),
            str(definition["planning_base_ref"]),
            str(definition["planning_base_sha"]).lower(),
        )
        observed = (
            attestation.repository_id,
            attestation.planning_base_ref,
            attestation.planning_base_sha.lower(),
        )
        if observed != expected or attestation.canonical_ref_sha.lower() != expected[2]:
            raise ValidationError("repository base attestation does not match replan")
        namespace = definition.get("resource_namespace") or {}
        if namespace.get("status") == "resolved":
            attested = attestation.resource_namespace or {}
            fields = (
                "case_sensitive",
                "unicode_normalization",
                "symlink_resolution",
            )
            if attested.get("status") != "resolved" or any(
                attested.get(key) != namespace.get(key) for key in fields
            ):
                raise ValidationError(
                    "resolved resource namespace lacks a matching replan attestation"
                )

    @staticmethod
    def _validate_delta_attestation(
        attestation: BaseDeltaAttestation,
        *,
        repository_id: str,
        old_base_sha: str,
        new_base_sha: str,
    ) -> None:
        if (
            attestation.repository_id != repository_id
            or attestation.old_base_sha.lower() != old_base_sha.lower()
            or attestation.new_base_sha.lower() != new_base_sha.lower()
            or not attestation.verifier.strip()
            or not attestation.verified_at.strip()
        ):
            raise ValidationError("base-delta attestation identity does not match")
        for path in attestation.changed_paths:
            if not _safe_relative_path(path):
                raise ValidationError("base-delta attestation contains an unsafe path")

    @staticmethod
    def _hold_epoch_tasks(
        conn: Any,
        *,
        package_id: str,
        plan_version: int,
        epoch: int,
        now: str,
    ) -> None:
        rows = conn.execute(
            "SELECT task.id, task.metadata FROM work_package_task_links AS link "
            "JOIN tasks AS task ON task.id = link.task_id "
            "WHERE link.package_id = ? AND link.plan_version = ? AND link.epoch = ?",
            (package_id, plan_version, epoch),
        ).fetchall()
        for row in rows:
            metadata = json_loads(row["metadata"], {})
            if metadata.get("no_dispatch") is True:
                continue
            metadata["no_dispatch"] = True
            cursor = conn.execute(
                "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                (json_dumps(metadata), now, row["id"]),
            )
            if cursor.rowcount != 1:
                raise TransitionError("failed to hold package task during Andon")

    @staticmethod
    def _append_history(
        conn: Any,
        *,
        package_id: str,
        event_type: str,
        actor: str,
        plan_version: int,
        epoch: int,
        detail: Mapping[str, Any],
        now: str,
    ) -> None:
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
            "FROM work_package_history WHERE package_id = ?",
            (package_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO work_package_history ("
            "id, package_id, seq, event_type, actor, plan_version, epoch, detail, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("wph"),
                package_id,
                int(seq_row["next_seq"]),
                event_type,
                actor,
                plan_version,
                epoch,
                json_dumps(dict(detail)),
                now,
            ),
        )

    @staticmethod
    def _old_links(reader: Any, proposal: WorkPackageReplanProposal) -> list[Any]:
        return _query_all(
            reader,
            "SELECT link.*, task.state AS task_state "
            "FROM work_package_task_links AS link "
            "JOIN tasks AS task ON task.id = link.task_id "
            "WHERE link.package_id = ? AND link.plan_version = ? "
            "AND link.epoch = ? ORDER BY link.node_key",
            (
                proposal.package_id,
                proposal.expected_plan_version,
                proposal.expected_epoch,
            ),
        )

    def _checkpoint(self, stage: str) -> None:
        if self.failure_injector is not None:
            self.failure_injector(stage)

    @staticmethod
    def _missing_preview(
        proposal: WorkPackageReplanProposal,
    ) -> WorkPackageReplanPreview:
        return WorkPackageReplanPreview(
            package_id=proposal.package_id,
            expected_plan_version=proposal.expected_plan_version,
            expected_epoch=proposal.expected_epoch,
            current_plan_version=None,
            current_epoch=None,
            current_state=None,
            proposed_plan_version=proposal.proposed_plan_version,
            proposed_epoch=proposal.proposed_epoch,
            plan_digest=proposal.compiled.plan_digest,
            affected_node_keys=(),
            invalidated_node_keys=(),
            new_node_keys=(),
            carry_decisions=(),
            blockers=("work package not found",),
        )


def _required(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValidationError("%s is required" % label)
    return result


def _positive_int(value: Any, label: str) -> int:
    """Normalize a public generation fence without leaking conversion errors."""

    if isinstance(value, bool):
        raise ValidationError("%s must be a positive integer" % label)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError("%s must be a positive integer" % label) from exc
    if result < 1 or (isinstance(value, float) and not value.is_integer()):
        raise ValidationError("%s must be a positive integer" % label)
    return result


def _query_one(reader: Any, sql: str, params: Sequence[Any]) -> Optional[Any]:
    if hasattr(reader, "query_one"):
        return reader.query_one(sql, tuple(params))
    return reader.execute(sql, tuple(params)).fetchone()


def _query_all(reader: Any, sql: str, params: Sequence[Any]) -> list[Any]:
    if hasattr(reader, "query_all"):
        return list(reader.query_all(sql, tuple(params)))
    return list(reader.execute(sql, tuple(params)).fetchall())


def _descendant_cone(
    nodes: Mapping[str, WorkPackageNodeSpec], direct: set[str]
) -> set[str]:
    affected = set(direct)
    changed = True
    while changed:
        changed = False
        for key, node in nodes.items():
            if key not in affected and set(node.depends_on) & affected:
                affected.add(key)
                changed = True
    return affected


def _declared_local_effects(node: WorkPackageNodeSpec) -> Tuple[str, ...]:
    return tuple(
        sorted(
            set(node.effects.reads)
            | set(node.effects.writes)
            | set(node.effects.exclusive)
        )
    )


def _path_scope_overlap(paths: Sequence[str], scopes: Sequence[str]) -> bool:
    normalized_paths = tuple(_normalize_scope(item) for item in paths)
    normalized_scopes = tuple(_normalize_scope(item) for item in scopes)
    for path in normalized_paths:
        for scope in normalized_scopes:
            if scope in {"*", "repo:*"} or path in {"*", "repo:*"}:
                return True
            if (
                path == scope
                or path.startswith(scope + "/")
                or scope.startswith(path + "/")
            ):
                return True
    return False


def _normalize_scope(value: Any) -> str:
    result = str(value or "").strip().replace("\\", "/")
    if result.startswith("path:"):
        result = result[5:]
    while result.startswith("./"):
        result = result[2:]
    return result.rstrip("/")


def _safe_relative_path(value: Any) -> bool:
    path = _normalize_scope(value)
    return bool(
        path
        and not path.startswith("/")
        and path not in {".", ".."}
        and ".." not in path.split("/")
        and "\x00" not in path
    )


__all__ = [
    "BaseDeltaAttestation",
    "BaseDeltaVerifier",
    "WORK_PACKAGE_REPLAN_SERVICE_VERSION",
    "WorkPackageCarryDecision",
    "WorkPackagePauseResult",
    "WorkPackageReplanPreview",
    "WorkPackageReplanProposal",
    "WorkPackageReplanResult",
    "WorkPackageReplanService",
]
