"""Transactional admission and materialization for versioned work packages.

The planner is deliberately outside this trust boundary.  It may propose a
``mac.work_package.plan.v1`` document, but only this service may turn the
compiled document into canonical tasks.  Admission is atomic, base-pinned,
idempotent, and held by default; no worker can observe half of a DAG or claim
new package work merely because a planner emitted it.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Tuple

from mac.models import (
    JsonDict,
    TransitionError,
    ValidationError,
    WorkPackage,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.repository_namespace import attest_git_tree_resource_namespace
from mac.repository_contract import resolve_repository_canonical_remote
from mac.store import Store
from mac.task_dependencies import replace_task_edges
from mac.work_package_models import (
    CompiledWorkPackagePlan,
    WorkPackageNodeSpec,
    compile_work_package_plan,
    validate_executable_work_package_effects,
    validate_supported_work_package_topology,
)
from mac.work_package_telemetry import WorkPackageTelemetryService


WORK_PACKAGE_MATERIALIZER_VERSION = "work-package-materializer-v1"
_CANONICAL_TASK_ID_RE = re.compile(r"^task_[0-9a-f]{32}$")


def _epoch_ref(value: Any) -> Optional[int]:
    """A history row's epoch pointer, or None for a package that has no epoch.

    work_package_history has a composite foreign key to
    (package_id, epoch, plan_version). A draft package has neither yet, and its
    counters read 0 -- writing those would point at a row that does not exist.
    The columns are nullable together for precisely this case.
    """
    number = int(value or 0)
    return number if number >= 1 else None


def _append_history(
    conn: Any,
    *,
    package_id: str,
    event_type: str,
    actor: str,
    plan_version: Optional[int],
    epoch: Optional[int],
    detail: Mapping[str, Any],
    now: str,
) -> None:
    """Append one package history row.

    ``seq`` is UNIQUE per package and computed here rather than passed in, so a
    caller cannot skip or reuse one and silently break the ordering the audit
    trail depends on.
    """
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


def _required(value: Any, field: str) -> str:
    """A non-empty trimmed string, or a refusal that names the field."""
    text = str(value or "").strip()
    if not text:
        raise ValidationError("%s is required" % field)
    return text


@dataclass(frozen=True)
class RepositoryBaseAttestation:
    """Controller observation proving that a planning ref named an exact SHA."""

    repository_id: str
    planning_base_ref: str
    planning_base_sha: str
    canonical_ref_sha: str
    source_kind: str
    verified_at: str
    resource_namespace: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return {
            "repository_id": self.repository_id,
            "planning_base_ref": self.planning_base_ref,
            "planning_base_sha": self.planning_base_sha,
            "canonical_ref_sha": self.canonical_ref_sha,
            "source_kind": self.source_kind,
            "verified_at": self.verified_at,
            "resource_namespace": dict(self.resource_namespace),
        }


class RepositoryBaseVerifier(Protocol):
    """External repository observation injected into the admission boundary."""

    def verify(
        self,
        repository: Mapping[str, Any],
        *,
        planning_base_ref: str,
        planning_base_sha: str,
    ) -> RepositoryBaseAttestation: ...


class GitRepositoryBaseVerifier:
    """Verify the exact advertised canonical ref without interactive auth.

    The repository contract's canonical remote is authoritative.  ``source``
    is used only for a legacy registry row with no contract.  A local checkout
    is useful corroboration when present, but is not treated as canonical
    because a hub checkout may lag its remote.  Authentication failures and
    unavailable remotes fail closed; admission never silently substitutes a
    local branch.
    """

    def __init__(self, *, timeout_seconds: int = 30) -> None:
        timeout = int(timeout_seconds)
        if timeout < 1 or timeout > 300:
            raise ValidationError(
                "repository verification timeout must be 1..300 seconds"
            )
        self.timeout_seconds = timeout

    def verify(
        self,
        repository: Mapping[str, Any],
        *,
        planning_base_ref: str,
        planning_base_sha: str,
    ) -> RepositoryBaseAttestation:
        repo = dict(repository)
        try:
            canonical = resolve_repository_canonical_remote(repo)
        except ValueError as exc:
            raise ValidationError(
                "repository admission requires a valid canonical remote"
            ) from exc
        repository_id = canonical.repository_id
        source = canonical.url

        result = self._run_git(
            ["git", "ls-remote", "--exit-code", source, planning_base_ref]
        )
        advertised = []
        for line in result.stdout.splitlines():
            fields = line.strip().split()
            if len(fields) == 2 and fields[1] == planning_base_ref:
                advertised.append(fields[0].lower())
        advertised = sorted(set(advertised))
        if len(advertised) != 1:
            raise ValidationError(
                "canonical repository did not advertise exactly one requested planning ref"
            )
        canonical_sha = advertised[0]
        if canonical_sha != planning_base_sha.lower():
            raise ValidationError(
                "planning base is stale: canonical ref does not equal the proposed SHA"
            )

        # If this controller has a checkout, ensure the pinned object is also
        # materializable there.  Missing hub checkouts remain valid because a
        # worker may clone from the attested canonical source.
        local_path = Path(str(repo.get("path") or "")).expanduser()
        if local_path.is_dir() and (local_path / ".git").exists():
            self._run_git(
                [
                    "git",
                    "-C",
                    str(local_path),
                    "cat-file",
                    "-e",
                    "%s^{commit}" % planning_base_sha,
                ]
            )

        return RepositoryBaseAttestation(
            repository_id=repository_id,
            planning_base_ref=planning_base_ref,
            planning_base_sha=planning_base_sha.lower(),
            canonical_ref_sha=canonical_sha,
            source_kind="git-ls-remote",
            verified_at=utcnow(),
            resource_namespace=attest_git_tree_resource_namespace(
                repository,
                planning_base_sha=planning_base_sha,
                timeout_seconds=self.timeout_seconds,
            ),
        )

    def _run_git(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "Never",
                "SSH_ASKPASS": "",
            }
        )
        try:
            result = subprocess.run(
                list(command),
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError("canonical repository verification failed") from exc
        if result.returncode != 0:
            # Git output can contain authenticated URLs or credential-helper
            # diagnostics.  Preserve the failure class without echoing it.
            raise ValidationError("canonical repository verification failed")
        return result


@dataclass(frozen=True)
class WorkPackageAdmissionResult:
    package: WorkPackage
    plan_digest: str
    plan_version: int
    epoch: int
    task_ids: Tuple[str, ...]
    created: bool
    held: bool
    base_attestation: RepositoryBaseAttestation

    def to_dict(self) -> JsonDict:
        return {
            "package": self.package.to_dict(),
            "plan_digest": self.plan_digest,
            "plan_version": self.plan_version,
            "epoch": self.epoch,
            "task_ids": list(self.task_ids),
            "created": self.created,
            "held": self.held,
            "base_attestation": self.base_attestation.to_dict(),
        }


ExternalLineageVerifier = Callable[[Any, Mapping[str, Any]], None]
TaskMetadataEnricher = Callable[[str, Optional[str]], Mapping[str, Any]]


class WorkPackageService:
    """Compile, attest, and atomically materialize one immutable plan version."""

    def __init__(
        self,
        store: Store,
        *,
        repository_verifier: Optional[RepositoryBaseVerifier] = None,
        external_lineage_verifier: Optional[ExternalLineageVerifier] = None,
        telemetry: Optional[WorkPackageTelemetryService] = None,
        task_metadata_enricher: Optional[TaskMetadataEnricher] = None,
    ) -> None:
        self.store = store
        self.repository_verifier = repository_verifier or GitRepositoryBaseVerifier()
        self.external_lineage_verifier = external_lineage_verifier
        self.telemetry = telemetry or WorkPackageTelemetryService(store)
        self.task_metadata_enricher = task_metadata_enricher

    def get(self, package_id: str) -> WorkPackage:
        """Return one exact package identity and its current pointers."""

        return self._get_package(str(package_id or "").strip())

    def update(
        self,
        package_id: str,
        *,
        goal: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        actor: str = "human",
    ) -> WorkPackage:
        """Change a package's DESCRIPTIVE fields. Never its plan.

        The plan belongs to `replan`, which installs a compiled replacement
        into a paused package. Letting `update` touch it would aim the most
        predictable verb in the vocabulary at the most consequential operation
        -- the same trap that kept `task update` from being an alias of `edit`.

        So this writes goal and metadata only, and it is CAS-free on purpose:
        neither field participates in plan-version or epoch invariants, and
        requiring a caller to quote a plan version to fix a typo in a goal
        would be ceremony without a guarantee.
        """
        package_value = _required(package_id, "work package id")
        actor_value = _required(actor, "work package update actor")
        if goal is None and metadata is None:
            raise ValidationError(
                "work package update needs at least one of goal or metadata"
            )
        goal_value = None if goal is None else _required(goal, "work package goal")
        now = utcnow()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM work_packages WHERE id = ?", (package_value,)
            ).fetchone()
            if row is None:
                raise ValidationError("work package not found: %s" % package_value)
            if row["state"] in {"completed", "cancelled"}:
                # A finished package is a record. Editing its stated goal after
                # the fact would rewrite what the work was for.
                raise TransitionError(
                    "cannot update a %s work package" % row["state"]
                )
            merged = dict(json_loads(row["metadata"], {}) or {})
            if metadata is not None:
                merged.update(dict(metadata))
            conn.execute(
                "UPDATE work_packages SET goal = ?, metadata = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    goal_value if goal_value is not None else row["goal"],
                    json_dumps(merged),
                    now,
                    package_value,
                ),
            )
            _append_history(
                conn,
                package_id=package_value,
                event_type="work_package.updated",
                actor=actor_value,
                # A draft package has no epoch row yet, and history carries a
                # foreign key to (package_id, epoch, plan_version). Writing 0/0
                # would violate it; the columns are nullable together for
                # exactly this case.
                plan_version=_epoch_ref(row["current_plan_version"]),
                epoch=_epoch_ref(row["current_epoch"]),
                detail={
                    "goal_changed": goal_value is not None,
                    "metadata_keys": sorted(dict(metadata or {})),
                },
                now=now,
            )
        return self._get_package(package_value)

    def cancel(
        self,
        package_id: str,
        *,
        actor: str = "human",
        reason: str,
    ) -> WorkPackage:
        """Terminally abandon a package. This is `delete` for a first-class
        object that is an audited record.

        Nothing hard-deletes a package, exactly as nothing hard-deletes a task.
        The row stays; the state becomes terminal.

        A COMPLETED package is refused: cancelling work that already landed
        would misdescribe history, and there is no undo that makes it true
        again.
        """
        package_value = _required(package_id, "work package id")
        actor_value = _required(actor, "work package cancel actor")
        reason_value = _required(reason, "work package cancel reason")
        now = utcnow()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM work_packages WHERE id = ?", (package_value,)
            ).fetchone()
            if row is None:
                raise ValidationError("work package not found: %s" % package_value)
            if row["state"] == "cancelled":
                return self._get_package(package_value)
            if row["state"] == "completed":
                raise TransitionError(
                    "cannot cancel a completed work package: it already landed"
                )
            conn.execute(
                "UPDATE work_packages SET state = 'cancelled', updated_at = ? "
                "WHERE id = ? AND state = ?",
                (now, package_value, row["state"]),
            )
            _append_history(
                conn,
                package_id=package_value,
                event_type="work_package.cancelled",
                actor=actor_value,
                plan_version=_epoch_ref(row["current_plan_version"]),
                epoch=_epoch_ref(row["current_epoch"]),
                detail={"reason": reason_value, "from_state": row["state"]},
                now=now,
            )
        return self._get_package(package_value)

    def list(
        self,
        *,
        state: Optional[str] = None,
        project: Optional[str] = None,
        limit: int = 100,
        after_id: Optional[str] = None,
        order_by_id: bool = False,
    ) -> Tuple[WorkPackage, ...]:
        clauses = []
        params: list[Any] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(str(state).strip())
        if project is not None:
            clauses.append("project = ?")
            params.append(str(project).strip())
        if after_id is not None and str(after_id).strip():
            # Inclusive keyset traversal lets an inventory resume within a
            # package that contains more than one integration-group item.
            clauses.append("id >= ?")
            params.append(str(after_id).strip())
        limit_value = max(1, min(int(limit), 1000))
        sql = "SELECT * FROM work_packages"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += (
            " ORDER BY id LIMIT ?"
            if order_by_id
            else " ORDER BY updated_at DESC, id LIMIT ?"
        )
        params.append(limit_value)
        return tuple(
            self._package_from_row(row)
            for row in self.store.query_all(sql, tuple(params))
        )

    def describe(self, package_id: str) -> JsonDict:
        """Project the complete controller-owned state of one work package."""

        package = self._get_package(str(package_id or "").strip())
        plan = self.store.query_one(
            "SELECT * FROM work_package_plan_versions WHERE package_id = ? "
            "AND version = ?",
            (package.id, package.current_plan_version),
        )
        epoch = self.store.query_one(
            "SELECT * FROM work_package_epochs WHERE package_id = ? AND epoch = ?",
            (package.id, package.current_epoch),
        )
        nodes = self.store.query_all(
            "SELECT link.*, task.title, task.state AS task_state, "
            "task.owner_agent_id, task.lease_id, task.dependencies, task.metadata "
            ", task.required_capabilities "
            "FROM work_package_task_links AS link "
            "JOIN tasks AS task ON task.id = link.task_id "
            "WHERE link.package_id = ? AND link.plan_version = ? AND link.epoch = ? "
            "ORDER BY link.node_key",
            (package.id, package.current_plan_version, package.current_epoch),
        )
        candidates = self.store.query_all(
            "SELECT * FROM work_package_node_candidates WHERE package_id = ? "
            "AND plan_version = ? AND epoch = ? ORDER BY submitted_at, id",
            (package.id, package.current_plan_version, package.current_epoch),
        )
        wip = self.store.query_all(
            "SELECT * FROM work_package_wip_tokens WHERE package_id = ? "
            "AND plan_version = ? AND epoch = ? ORDER BY acquired_at, id",
            (package.id, package.current_plan_version, package.current_epoch),
        )
        batches = self.store.query_all(
            "SELECT * FROM work_package_integration_batches WHERE package_id = ? "
            "AND plan_version = ? AND epoch = ? ORDER BY created_at, id",
            (package.id, package.current_plan_version, package.current_epoch),
        )
        certifications = self.store.query_all(
            "SELECT certification.* FROM work_package_certifications AS certification "
            "JOIN work_package_integration_batches AS batch "
            "ON batch.id = certification.batch_id "
            "WHERE batch.package_id = ? AND batch.plan_version = ? AND batch.epoch = ? "
            "ORDER BY certification.created_at, certification.id",
            (package.id, package.current_plan_version, package.current_epoch),
        )
        history = self.store.query_all(
            "SELECT * FROM work_package_history WHERE package_id = ? ORDER BY seq",
            (package.id,),
        )

        def decoded(row: Mapping[str, Any], *json_fields: str) -> JsonDict:
            value = dict(row)
            for field_name in json_fields:
                value[field_name] = json_loads(value.get(field_name), {})
            return value

        return {
            "package": package.to_dict(),
            "plan": decoded(plan, "definition") if plan is not None else None,
            "epoch": dict(epoch) if epoch is not None else None,
            "nodes": [
                decoded(row, "dependencies", "metadata", "required_capabilities")
                for row in nodes
            ],
            "candidates": [dict(row) for row in candidates],
            "wip": [dict(row) for row in wip],
            "batches": [decoded(row, "metadata") for row in batches],
            "certifications": [
                decoded(row, "commands", "evidence") for row in certifications
            ],
            "history": [decoded(row, "detail") for row in history],
        }

    def admit(
        self,
        raw_plan: Mapping[str, Any],
        *,
        actor: str,
        reason: str,
        tenant_id: Optional[str] = None,
        root_task_id: Optional[str] = None,
        _controller_task_identity: Optional[Tuple[str, str]] = None,
        _cohort_assignment: Optional[Mapping[str, Any]] = None,
    ) -> WorkPackageAdmissionResult:
        """Atomically persist a compiled DAG as held canonical tasks.

        Admission creates plan version 1 and epoch 1.  Repeating the exact
        request is idempotent and returns the already committed package;
        reusing a package id for a different digest fails closed.
        """

        admission_started_at = utcnow()
        compiled = compile_work_package_plan(raw_plan)
        validate_executable_work_package_effects(compiled)
        materialization_map = self._materialization_map(
            compiled,
            controller_task_identity=_controller_task_identity,
        )
        actor_value = str(actor or "").strip()
        reason_value = str(reason or "").strip()
        if not actor_value:
            raise ValidationError("work package admission actor is required")
        if not reason_value:
            raise ValidationError("work package admission reason is required")
        if _controller_task_identity is not None and root_task_id is not None:
            raise ValidationError(
                "controller task identity and external root task are mutually exclusive"
            )

        definition = compiled.definition
        package_id = str(definition["package_id"])
        repository_id = str(definition["repository_id"])
        task_metadata_overlay = (
            dict(
                self.task_metadata_enricher(
                    repository_id,
                    str(definition.get("project") or "") or None,
                )
            )
            if self.task_metadata_enricher is not None
            else {}
        )
        existing = self.store.query_one(
            "SELECT * FROM work_packages WHERE id = ?", (package_id,)
        )
        if existing is not None:
            stored_attestation = self._stored_base_attestation(existing)
            self._validate_attestation(stored_attestation, definition)
            stored_materialization_map = self._materialization_map(
                compiled,
                controller_task_identity=_controller_task_identity,
            )
            if _controller_task_identity is not None:
                linked = self.store.query_one(
                    "SELECT task_id FROM work_package_task_links "
                    "WHERE package_id = ? AND node_key = ? AND plan_version = 1 "
                    "AND epoch = 1",
                    (package_id, _controller_task_identity[0]),
                )
                if (
                    linked is None
                    or str(linked["task_id"]) != _controller_task_identity[1]
                ):
                    raise ValidationError(
                        "work package id already belongs to different task identities"
                    )
            if materialization_map != stored_materialization_map:
                raise ValidationError(
                    "work package id already belongs to different task identities"
                )
            return self._idempotent_result(
                compiled,
                materialization_map=stored_materialization_map,
            )

        repository_row = self.store.query_one(
            "SELECT * FROM project_repositories WHERE id = ?", (repository_id,)
        )
        if repository_row is None:
            raise ValidationError("work package repository is not registered")
        repository = dict(repository_row)
        self._validate_repository_contract(repository, definition)
        attestation = self.repository_verifier.verify(
            repository,
            planning_base_ref=str(definition["planning_base_ref"]),
            planning_base_sha=str(definition["planning_base_sha"]),
        )
        self._validate_attestation(attestation, definition)

        now = utcnow()
        task_ids = tuple(
            str(materialization_map[key]["task_id"])
            for key in compiled.topological_order
        )
        with self.store.transaction() as conn:
            # Lock the repository registry row and ensure the external
            # attestation still describes the registry identity we inspected.
            lock = conn.execute(
                "UPDATE project_repositories SET updated_at = updated_at WHERE id = ?",
                (repository_id,),
            )
            if lock.rowcount != 1:
                raise ValidationError(
                    "work package repository disappeared during admission"
                )
            current_repository_row = conn.execute(
                "SELECT * FROM project_repositories WHERE id = ?", (repository_id,)
            ).fetchone()
            if current_repository_row is None:
                raise ValidationError(
                    "work package repository disappeared during admission"
                )
            current_repository = dict(current_repository_row)
            for field_name in (
                "id",
                "source",
                "path",
                "project",
                "enabled",
                "updated_at",
            ):
                if current_repository.get(field_name) != repository.get(field_name):
                    raise ValidationError(
                        "work package repository changed during base attestation"
                    )

            concurrent = conn.execute(
                "SELECT id FROM work_packages WHERE id = ?", (package_id,)
            ).fetchone()
            if concurrent is not None:
                # The repository-row lock above serializes admissions. Seeing
                # the package here means an exact peer request committed while
                # this caller was attesting the base. Validate every immutable
                # plan/materialization field and return that same result; a
                # transport retry must not surface a spurious failure merely
                # because it overlapped the original request.
                return self._idempotent_result(
                    compiled,
                    materialization_map=materialization_map,
                    conn=conn,
                )
            if root_task_id:
                self._require_task(conn, root_task_id, "root task")

            self._validate_external_dependencies(conn, compiled)
            conn.execute(
                "INSERT INTO work_packages ("
                "id, tenant_id, project, repository_id, root_task_id, goal, state, "
                "current_plan_version, current_epoch, metadata, created_by, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    package_id,
                    tenant_id,
                    definition.get("project"),
                    repository_id,
                    root_task_id,
                    definition["goal"],
                    "draft",
                    0,
                    0,
                    json_dumps(
                        {
                            "schema": "mac.work_package.instance.v1",
                            "materializer_version": WORK_PACKAGE_MATERIALIZER_VERSION,
                            "base_attestation": attestation.to_dict(),
                            "plan_metadata": definition.get("metadata") or {},
                            "controller_task_identity": (
                                {
                                    "node_key": _controller_task_identity[0],
                                    "task_id": _controller_task_identity[1],
                                }
                                if _controller_task_identity is not None
                                else None
                            ),
                        }
                    ),
                    actor_value,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO work_package_plan_versions ("
                "package_id, version, parent_version, definition, plan_digest, reason, "
                "created_by, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    package_id,
                    1,
                    None,
                    json_dumps(definition),
                    compiled.plan_digest,
                    reason_value,
                    actor_value,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO work_package_epochs ("
                "package_id, epoch, plan_version, planning_base_ref, planning_base_sha, "
                "status, reason, created_by, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    package_id,
                    1,
                    1,
                    definition["planning_base_ref"],
                    definition["planning_base_sha"],
                    "active",
                    reason_value,
                    actor_value,
                    now,
                ),
            )
            for node in compiled.task_specs:
                self._insert_materialized_task(
                    conn,
                    compiled=compiled,
                    node=node,
                    project=definition.get("project"),
                    actor=actor_value,
                    now=now,
                    materialized=materialization_map[node.node_key],
                    materialization_map=materialization_map,
                    task_metadata_overlay=task_metadata_overlay,
                )
            if _controller_task_identity is not None:
                root_update = conn.execute(
                    "UPDATE work_packages SET root_task_id = ? WHERE id = ? "
                    "AND root_task_id IS NULL",
                    (_controller_task_identity[1], package_id),
                )
                if root_update.rowcount != 1:
                    raise ValidationError(
                        "managed single-task package root linkage failed"
                    )
            updated = conn.execute(
                "UPDATE work_packages SET state = ?, current_plan_version = ?, "
                "current_epoch = ?, updated_at = ? "
                "WHERE id = ? AND state = ? AND current_plan_version = 0 AND current_epoch = 0",
                ("admitted", 1, 1, now, package_id, "draft"),
            )
            if updated.rowcount != 1:
                raise ValidationError("work package admission CAS failed")
            self._append_package_history(
                conn,
                package_id=package_id,
                event_type="work_package.admitted",
                actor=actor_value,
                plan_version=1,
                epoch=1,
                detail={
                    "plan_digest": compiled.plan_digest,
                    "task_ids": list(task_ids),
                    "held": True,
                    "reason": reason_value,
                    "base_attestation": attestation.to_dict(),
                },
                now=now,
            )
            rollout = conn.execute(
                "SELECT revision FROM managed_task_publication_rollout "
                "WHERE singleton_key = ?",
                ("fleet",),
            ).fetchone()
            rollout_revision = int(rollout["revision"]) if rollout is not None else 0
            cohort = dict(_cohort_assignment or {})
            cohort_route = str(cohort.get("treatment_route") or "managed_synchronized")
            if cohort_route != "managed_synchronized":
                raise ValidationError(
                    "an admitted work package requires managed cohort treatment"
                )
            cohort_eligibility = str(cohort.get("eligibility") or "ineligible")
            cohort_revision = int(cohort.get("rollout_revision") or rollout_revision)
            cohort_key = str(
                cohort.get("cohort_key") or "managed_nonprimary_r%d" % cohort_revision
            )
            cohort_reason = str(
                cohort.get("reason") or "managed_plan_excluded_primary_cohort"
            )
            cohort_detail = dict(cohort.get("detail") or {})
            cohort_detail.setdefault("schema", "mac.execution_cohort.prospective.v3")
            cohort_detail.setdefault("primary_analysis_eligible", False)
            cohort_detail.setdefault(
                "eligibility_contract", "non_atomic_managed_plan_excluded_v1"
            )
            cohort_detail.setdefault("randomization", None)
            # Atomic task randomization is persisted by the controller before
            # treatment-specific managed admission begins.  Keep the task row's
            # immutable payload byte-for-byte compatible with that prospective
            # assignment; package-only planning evidence is attached to the
            # separate package assignment below.
            task_cohort_detail = dict(cohort_detail)
            cohort_detail.update(
                {
                    "plan_digest": compiled.plan_digest,
                    "planning_base_sha": definition["planning_base_sha"],
                }
            )
            self.telemetry.assign_cohort(
                task_id=None,
                package_id=package_id,
                eligibility=cohort_eligibility,
                treatment_route=cohort_route,
                rollout_revision=cohort_revision,
                cohort_key=cohort_key,
                reason=cohort_reason,
                actor="execution-cohort-controller",
                detail=cohort_detail,
                assigned_at=now,
                conn=conn,
            )
            # The package is the managed treatment authority.  A root task is
            # merely lineage and must never have an earlier immutable cohort
            # rewritten.  The controller-created atomic mutation task is a
            # separate randomization unit and receives its own matching row.
            if _controller_task_identity is not None:
                self.telemetry.assign_cohort(
                    task_id=_controller_task_identity[1],
                    package_id=None,
                    eligibility=cohort_eligibility,
                    treatment_route=cohort_route,
                    rollout_revision=cohort_revision,
                    cohort_key=cohort_key,
                    reason=cohort_reason,
                    actor="execution-cohort-controller",
                    detail=task_cohort_detail,
                    assigned_at=now,
                    conn=conn,
                )
            self.telemetry.record_station_attempt(
                package_id=package_id,
                station="admission",
                operation="materialize",
                attempted=True,
                terminal_status="succeeded",
                queued_at=admission_started_at,
                started_at=admission_started_at,
                completed_at=now,
                actor=actor_value,
                plan_version=1,
                epoch=1,
                pipeline_run_id="admission:%s:1:1:materialize" % package_id,
                outcome_index=0,
                reason_code="work_package_admitted_held",
                detail={
                    "held": True,
                    "task_ids": list(task_ids),
                    "reason": reason_value,
                },
                conn=conn,
            )

        package = self._get_package(package_id)
        return WorkPackageAdmissionResult(
            package=package,
            plan_digest=compiled.plan_digest,
            plan_version=1,
            epoch=1,
            task_ids=task_ids,
            created=True,
            held=True,
            base_attestation=attestation,
        )

    def activate(
        self,
        package_id: str,
        *,
        expected_plan_version: int,
        expected_epoch: int,
        actor: str,
    ) -> WorkPackage:
        """Release only dependency-free roots and activate the package by CAS.

        The operator-facing ControlPlane route first proves credential,
        scheduler, certification, and landing readiness.  This lower-level
        transaction remains responsible for the final generation CAS and
        release; it is not an authorization surface by itself.
        """

        actor_value = str(actor or "").strip()
        if not actor_value:
            raise ValidationError("work package activation actor is required")
        now = utcnow()
        with self.store.transaction() as conn:
            lock = conn.execute(
                "UPDATE work_packages SET updated_at = updated_at WHERE id = ?",
                (package_id,),
            )
            if lock.rowcount != 1:
                raise ValidationError("work package not found")
            package = conn.execute(
                "SELECT * FROM work_packages WHERE id = ?", (package_id,)
            ).fetchone()
            if package is None:
                raise ValidationError("work package not found")
            if int(package["current_plan_version"]) != int(
                expected_plan_version
            ) or int(package["current_epoch"]) != int(expected_epoch):
                raise ValidationError("work package activation CAS did not match")
            plan_row = conn.execute(
                "SELECT definition FROM work_package_plan_versions "
                "WHERE package_id = ? AND version = ?",
                (package_id, expected_plan_version),
            ).fetchone()
            if plan_row is None:
                raise ValidationError("work package activation plan is missing")
            validate_supported_work_package_topology(
                json_loads(plan_row["definition"], {})
            )
            if package["state"] == "active":
                return self._package_from_row(package)
            if package["state"] != "admitted":
                raise ValidationError("only an admitted work package can be activated")

            root_rows = conn.execute(
                "SELECT link.task_id, link.node_key, task.metadata "
                "FROM work_package_task_links AS link "
                "JOIN tasks AS task ON task.id = link.task_id "
                "WHERE link.package_id = ? AND link.plan_version = ? AND link.epoch = ? "
                "AND link.node_state = ? AND task.dependencies = ? "
                "ORDER BY link.node_key",
                (
                    package_id,
                    expected_plan_version,
                    expected_epoch,
                    "planned",
                    "[]",
                ),
            ).fetchall()
            if not root_rows:
                raise ValidationError(
                    "work package has no dependency-free activation roots"
                )
            released_task_ids = []
            controller_task_ids = []
            for row in root_rows:
                metadata = json_loads(row["metadata"], {})
                package_projection = metadata.get("work_package")
                if not isinstance(package_projection, dict):
                    raise ValidationError(
                        "activation root lacks its work-package projection"
                    )
                node_type = str(package_projection.get("node_type") or "mutation")
                controller_owned = node_type in {"integration", "certification"}
                if controller_owned:
                    if metadata.get("no_dispatch") is not True:
                        raise ValidationError(
                            "controller-owned activation root lost its dispatch hold"
                        )
                    controller_task_ids.append(str(row["task_id"]))
                else:
                    metadata.pop("no_dispatch", None)
                    released_task_ids.append(str(row["task_id"]))
                task_update = conn.execute(
                    "UPDATE tasks SET metadata = ?, updated_at = ? "
                    "WHERE id = ? AND state = ? AND dependencies = ?",
                    (json_dumps(metadata), now, row["task_id"], "open", "[]"),
                )
                if task_update.rowcount != 1:
                    raise ValidationError("activation root task changed during release")
                link_update = conn.execute(
                    "UPDATE work_package_task_links SET node_state = ? "
                    "WHERE task_id = ? AND node_state = ?",
                    ("ready", row["task_id"], "planned"),
                )
                if link_update.rowcount != 1:
                    raise ValidationError("activation root link changed during release")
            state_update = conn.execute(
                "UPDATE work_packages SET state = ?, updated_at = ? "
                "WHERE id = ? AND state = ? AND current_plan_version = ? AND current_epoch = ?",
                (
                    "active",
                    now,
                    package_id,
                    "admitted",
                    expected_plan_version,
                    expected_epoch,
                ),
            )
            if state_update.rowcount != 1:
                raise ValidationError("work package activation CAS failed")
            self._append_package_history(
                conn,
                package_id=package_id,
                event_type="work_package.activated",
                actor=actor_value,
                plan_version=expected_plan_version,
                epoch=expected_epoch,
                detail={
                    "released_task_ids": released_task_ids,
                    "controller_ready_task_ids": controller_task_ids,
                },
                now=now,
            )
        return self._get_package(package_id)

    def _insert_materialized_task(
        self,
        conn: Any,
        *,
        compiled: CompiledWorkPackagePlan,
        node: WorkPackageNodeSpec,
        project: Optional[str],
        actor: str,
        now: str,
        materialized: Mapping[str, Any],
        materialization_map: Mapping[str, Mapping[str, Any]],
        task_metadata_overlay: Mapping[str, Any],
    ) -> None:
        task_id = str(materialized["task_id"])
        internal_ids = [
            str(materialization_map[key]["task_id"]) for key in node.depends_on
        ]
        external_ids = [str(item["task_id"]) for item in node.external_dependencies]
        dependencies = sorted(set(internal_ids + external_ids))
        task_metadata = dict(node.metadata)
        for key, value in task_metadata_overlay.items():
            task_metadata.setdefault(str(key), value)
        task_metadata["no_dispatch"] = True
        task_metadata["work_package"] = {
            "schema": "mac.work_package.task.v1",
            "package_id": compiled.definition["package_id"],
            "plan_version": 1,
            "epoch": 1,
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
        }
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
                "waiting" if dependencies else "open",
                json_dumps(list(node.required_capabilities)),
                json_dumps(dependencies),
                json_dumps(task_metadata),
                0,
                node.max_attempts,
                now,
                now,
            ),
        )
        replace_task_edges(
            conn,
            task_id=task_id,
            dependency_ids=dependencies,
            updated_at=now,
        )
        conn.execute(
            "INSERT INTO task_history ("
            "id, task_id, event_type, actor, from_state, to_state, detail, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("history"),
                task_id,
                "task.materialized",
                actor,
                None,
                "waiting" if dependencies else "open",
                json_dumps(
                    {
                        "package_id": compiled.definition["package_id"],
                        "plan_digest": compiled.plan_digest,
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
                compiled.definition["package_id"],
                1,
                1,
                node.node_key,
                materialized["node_generation"],
                node.effects_digest,
                node.contract_digest,
                node.input_digest,
                "planned",
                now,
            ),
        )

    def _validate_repository_contract(
        self, repository: Mapping[str, Any], definition: Mapping[str, Any]
    ) -> None:
        if not bool(repository.get("enabled")):
            raise ValidationError("work package repository is disabled")
        plan_project = definition.get("project")
        if plan_project and str(repository.get("project") or "") != str(plan_project):
            raise ValidationError("work package project does not own the repository")

    def _validate_attestation(
        self,
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
            raise ValidationError(
                "repository base attestation does not match compiled plan"
            )
        planned_namespace = definition.get("resource_namespace") or {}
        if planned_namespace.get("status") == "resolved":
            attested_namespace = attestation.resource_namespace or {}
            namespace_identity = (
                "case_sensitive",
                "unicode_normalization",
                "symlink_resolution",
            )
            if attested_namespace.get("status") != "resolved" or any(
                attested_namespace.get(field_name) != planned_namespace.get(field_name)
                for field_name in namespace_identity
            ):
                raise ValidationError(
                    "resolved resource namespace lacks a matching controller attestation"
                )

    def _validate_external_dependencies(
        self, conn: Any, compiled: CompiledWorkPackagePlan
    ) -> None:
        seen = set()
        for node in compiled.task_specs:
            for dependency in node.external_dependencies:
                task_id = str(dependency["task_id"])
                if task_id not in seen:
                    self._require_task(conn, task_id, "external dependency")
                    seen.add(task_id)
                if dependency.get("lineage_status") == "resolved":
                    if self.external_lineage_verifier is None:
                        raise ValidationError(
                            "resolved external lineage requires a controller verifier"
                        )
                    self.external_lineage_verifier(conn, dependency)

    @staticmethod
    def _require_task(conn: Any, task_id: str, label: str) -> None:
        if (
            conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            is None
        ):
            raise ValidationError("work package %s was not found" % label)

    def _idempotent_result(
        self,
        compiled: CompiledWorkPackagePlan,
        *,
        materialization_map: Mapping[str, Mapping[str, Any]],
        conn: Optional[Any] = None,
    ) -> WorkPackageAdmissionResult:
        if conn is None:

            def query_one(sql: str, params: Sequence[Any] = ()) -> Any:
                return self.store.query_one(sql, params)

            def query_all(sql: str, params: Sequence[Any] = ()) -> Any:
                return self.store.query_all(sql, params)
        else:

            def query_one(sql: str, params: Sequence[Any] = ()) -> Any:
                return conn.execute(sql, params).fetchone()

            def query_all(sql: str, params: Sequence[Any] = ()) -> Any:
                return conn.execute(sql, params).fetchall()

        package_id = str(compiled.definition["package_id"])
        plan = query_one(
            "SELECT plan_digest FROM work_package_plan_versions "
            "WHERE package_id = ? AND version = ?",
            (package_id, 1),
        )
        if plan is None or plan["plan_digest"] != compiled.plan_digest:
            raise ValidationError("work package id already belongs to a different plan")
        epoch = query_one(
            "SELECT planning_base_ref, planning_base_sha FROM work_package_epochs "
            "WHERE package_id = ? AND epoch = ? AND plan_version = ?",
            (package_id, 1, 1),
        )
        if epoch is None or (
            epoch["planning_base_ref"] != compiled.definition["planning_base_ref"]
            or epoch["planning_base_sha"] != compiled.definition["planning_base_sha"]
        ):
            raise ValidationError("work package id has an incoherent initial epoch")
        rows = query_all(
            "SELECT task_id, node_key, contract_digest, input_digest, "
            "declared_effects_digest FROM work_package_task_links "
            "WHERE package_id = ? AND plan_version = ? AND epoch = ? ORDER BY node_key",
            (package_id, 1, 1),
        )
        expected = sorted(
            (
                str(materialization_map[node.node_key]["task_id"]),
                node.node_key,
                node.contract_digest,
                node.input_digest,
                node.effects_digest,
            )
            for node in compiled.task_specs
        )
        observed = sorted(
            (
                row["task_id"],
                row["node_key"],
                row["contract_digest"],
                row["input_digest"],
                row["declared_effects_digest"],
            )
            for row in rows
        )
        if observed != expected:
            raise ValidationError(
                "work package materialization is incomplete or incoherent"
            )
        package_row = query_one(
            "SELECT * FROM work_packages WHERE id = ?", (package_id,)
        )
        if package_row is None:
            raise ValidationError("work package idempotent result is missing")
        package = self._package_from_row(package_row)
        stored_attestation = self._stored_base_attestation(package_row)
        self._validate_attestation(stored_attestation, compiled.definition)
        if controller_identity := next(
            (
                str(materialization_map[key]["task_id"])
                for key in compiled.topological_order
                if str(materialization_map[key]["task_id"])
                != str(compiled.materialization_map[key]["task_id"])
            ),
            None,
        ):
            if package.root_task_id != controller_identity:
                raise ValidationError(
                    "work package root task identity is incomplete or incoherent"
                )
        return WorkPackageAdmissionResult(
            package=package,
            plan_digest=compiled.plan_digest,
            plan_version=1,
            epoch=1,
            task_ids=tuple(
                str(materialization_map[node_key]["task_id"])
                for node_key in compiled.topological_order
            ),
            created=False,
            held=package.state == "admitted",
            base_attestation=stored_attestation,
        )

    @staticmethod
    def _stored_base_attestation(
        package_row: Mapping[str, Any],
    ) -> RepositoryBaseAttestation:
        metadata = json_loads(package_row["metadata"], {})
        raw_attestation = metadata.get("base_attestation")
        if not isinstance(raw_attestation, Mapping):
            raise ValidationError(
                "existing work package has no durable base attestation"
            )
        try:
            return RepositoryBaseAttestation(
                repository_id=str(raw_attestation["repository_id"]),
                planning_base_ref=str(raw_attestation["planning_base_ref"]),
                planning_base_sha=str(raw_attestation["planning_base_sha"]),
                canonical_ref_sha=str(raw_attestation["canonical_ref_sha"]),
                source_kind=str(raw_attestation["source_kind"]),
                verified_at=str(raw_attestation["verified_at"]),
                resource_namespace=dict(
                    raw_attestation.get("resource_namespace") or {}
                ),
            )
        except KeyError as exc:
            raise ValidationError(
                "existing work package has an incomplete base attestation"
            ) from exc

    @staticmethod
    def _materialization_map(
        compiled: CompiledWorkPackagePlan,
        *,
        controller_task_identity: Optional[Tuple[str, str]],
    ) -> JsonDict:
        """Apply controller-owned task identities outside the planner schema.

        Ordinary single-task admission allocates the public task id before it
        resolves the remote base.  Preserving that id makes ``POST /tasks``
        atomic and backward compatible, while keeping ids out of the
        planner-controlled plan and its semantic digest.  Only this service
        accepts the override and persists it with the package instance.
        """

        result: JsonDict = {
            key: dict(value) for key, value in compiled.materialization_map.items()
        }
        override_node = ""
        override_task_id = ""
        if controller_task_identity is not None:
            if (
                not isinstance(controller_task_identity, tuple)
                or len(controller_task_identity) != 2
            ):
                raise ValidationError(
                    "controller task identity must be a node/task pair"
                )
            override_node = str(controller_task_identity[0] or "").strip()
            override_task_id = str(controller_task_identity[1] or "").strip()
            node_specs = {node.node_key: node for node in compiled.task_specs}
            node = node_specs.get(override_node)
            if node is None:
                raise ValidationError(
                    "controller task identity references an unknown node"
                )
            if node.node_type != "mutation":
                raise ValidationError(
                    "controller task identity may only preserve a mutation node"
                )
            if not _CANONICAL_TASK_ID_RE.fullmatch(override_task_id):
                raise ValidationError(
                    "controller task identity must use a canonical task id"
                )
        task_ids = set()
        for node_key in compiled.topological_order:
            if node_key == override_node:
                result[node_key]["task_id"] = override_task_id
            task_id = str(result[node_key]["task_id"])
            if task_id in task_ids:
                raise ValidationError("work package task ids must be unique")
            task_ids.add(task_id)
        return result

    @staticmethod
    def _append_package_history(
        conn: Any,
        *,
        package_id: str,
        event_type: str,
        actor: str,
        plan_version: Optional[int],
        epoch: Optional[int],
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

    def _get_package(self, package_id: str) -> WorkPackage:
        row = self.store.query_one(
            "SELECT * FROM work_packages WHERE id = ?", (package_id,)
        )
        if row is None:
            raise ValidationError("work package not found")
        return self._package_from_row(row)

    @staticmethod
    def _package_from_row(row: Mapping[str, Any]) -> WorkPackage:
        value = dict(row)
        value["metadata"] = json_loads(value.get("metadata"), {})
        return WorkPackage(**value)


__all__ = [
    "GitRepositoryBaseVerifier",
    "RepositoryBaseAttestation",
    "RepositoryBaseVerifier",
    "WORK_PACKAGE_MATERIALIZER_VERSION",
    "WorkPackageAdmissionResult",
    "WorkPackageService",
]
