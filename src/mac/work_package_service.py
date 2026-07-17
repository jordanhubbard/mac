"""Transactional admission and materialization for versioned work packages.

The planner is deliberately outside this trust boundary.  It may propose a
``mac.work_package.plan.v1`` document, but only this service may turn the
compiled document into canonical tasks.  Admission is atomic, base-pinned,
idempotent, and held by default; no worker can observe half of a DAG or claim
new package work merely because a planner emitted it.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Tuple

from mac.models import (
    JsonDict,
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
from mac.work_package_models import (
    CompiledWorkPackagePlan,
    WorkPackageNodeSpec,
    compile_work_package_plan,
    validate_executable_work_package_effects,
    validate_supported_work_package_topology,
)


WORK_PACKAGE_MATERIALIZER_VERSION = "work-package-materializer-v1"


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
            raise ValidationError("repository verification timeout must be 1..300 seconds")
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


class WorkPackageService:
    """Compile, attest, and atomically materialize one immutable plan version."""

    def __init__(
        self,
        store: Store,
        *,
        repository_verifier: Optional[RepositoryBaseVerifier] = None,
        external_lineage_verifier: Optional[ExternalLineageVerifier] = None,
    ) -> None:
        self.store = store
        self.repository_verifier = repository_verifier or GitRepositoryBaseVerifier()
        self.external_lineage_verifier = external_lineage_verifier

    def get(self, package_id: str) -> WorkPackage:
        """Return one exact package identity and its current pointers."""

        return self._get_package(str(package_id or "").strip())

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
        sql += " ORDER BY id LIMIT ?" if order_by_id else " ORDER BY updated_at DESC, id LIMIT ?"
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
    ) -> WorkPackageAdmissionResult:
        """Atomically persist a compiled DAG as held canonical tasks.

        Admission creates plan version 1 and epoch 1.  Repeating the exact
        request is idempotent and returns the already committed package;
        reusing a package id for a different digest fails closed.
        """

        compiled = compile_work_package_plan(raw_plan)
        validate_executable_work_package_effects(compiled)
        actor_value = str(actor or "").strip()
        reason_value = str(reason or "").strip()
        if not actor_value:
            raise ValidationError("work package admission actor is required")
        if not reason_value:
            raise ValidationError("work package admission reason is required")

        definition = compiled.definition
        package_id = str(definition["package_id"])
        repository_id = str(definition["repository_id"])
        existing = self.store.query_one(
            "SELECT * FROM work_packages WHERE id = ?", (package_id,)
        )
        if existing is not None:
            metadata = json_loads(existing["metadata"], {})
            raw_attestation = metadata.get("base_attestation")
            if not isinstance(raw_attestation, Mapping):
                raise ValidationError(
                    "existing work package has no durable base attestation"
                )
            try:
                stored_attestation = RepositoryBaseAttestation(
                    repository_id=str(raw_attestation["repository_id"]),
                    planning_base_ref=str(raw_attestation["planning_base_ref"]),
                    planning_base_sha=str(raw_attestation["planning_base_sha"]),
                    canonical_ref_sha=str(raw_attestation["canonical_ref_sha"]),
                    source_kind=str(raw_attestation["source_kind"]),
                    verified_at=str(raw_attestation["verified_at"]),
                    resource_namespace=(
                        dict(raw_attestation.get("resource_namespace") or {})
                    ),
                )
            except KeyError as exc:
                raise ValidationError(
                    "existing work package has an incomplete base attestation"
                ) from exc
            self._validate_attestation(stored_attestation, definition)
            return self._idempotent_result(compiled, stored_attestation)

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
            str(compiled.materialization_map[key]["task_id"])
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
                raise ValidationError("work package repository disappeared during admission")
            current_repository_row = conn.execute(
                "SELECT * FROM project_repositories WHERE id = ?", (repository_id,)
            ).fetchone()
            if current_repository_row is None:
                raise ValidationError("work package repository disappeared during admission")
            current_repository = dict(current_repository_row)
            for field_name in ("id", "source", "path", "project", "enabled", "updated_at"):
                if current_repository.get(field_name) != repository.get(field_name):
                    raise ValidationError(
                        "work package repository changed during base attestation"
                    )

            concurrent = conn.execute(
                "SELECT id FROM work_packages WHERE id = ?", (package_id,)
            ).fetchone()
            if concurrent is not None:
                raise ValidationError("work package id was admitted concurrently; retry")
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
            if (
                int(package["current_plan_version"]) != int(expected_plan_version)
                or int(package["current_epoch"]) != int(expected_epoch)
            ):
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
                raise ValidationError("work package has no dependency-free activation roots")
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
    ) -> None:
        materialized = compiled.materialization_map[node.node_key]
        task_id = str(materialized["task_id"])
        internal_ids = [
            str(compiled.materialization_map[key]["task_id"])
            for key in node.depends_on
        ]
        external_ids = [str(item["task_id"]) for item in node.external_dependencies]
        dependencies = sorted(set(internal_ids + external_ids))
        task_metadata = dict(node.metadata)
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
            raise ValidationError("repository base attestation does not match compiled plan")
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
        if conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            raise ValidationError("work package %s was not found" % label)

    def _idempotent_result(
        self,
        compiled: CompiledWorkPackagePlan,
        attestation: RepositoryBaseAttestation,
    ) -> WorkPackageAdmissionResult:
        package_id = str(compiled.definition["package_id"])
        plan = self.store.query_one(
            "SELECT plan_digest FROM work_package_plan_versions "
            "WHERE package_id = ? AND version = ?",
            (package_id, 1),
        )
        if plan is None or plan["plan_digest"] != compiled.plan_digest:
            raise ValidationError("work package id already belongs to a different plan")
        epoch = self.store.query_one(
            "SELECT planning_base_ref, planning_base_sha FROM work_package_epochs "
            "WHERE package_id = ? AND epoch = ? AND plan_version = ?",
            (package_id, 1, 1),
        )
        if epoch is None or (
            epoch["planning_base_ref"] != compiled.definition["planning_base_ref"]
            or epoch["planning_base_sha"] != compiled.definition["planning_base_sha"]
        ):
            raise ValidationError("work package id has an incoherent initial epoch")
        rows = self.store.query_all(
            "SELECT task_id, node_key, contract_digest, input_digest, "
            "declared_effects_digest FROM work_package_task_links "
            "WHERE package_id = ? AND plan_version = ? AND epoch = ? ORDER BY node_key",
            (package_id, 1, 1),
        )
        expected = sorted(
            (
                str(compiled.materialization_map[node.node_key]["task_id"]),
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
            raise ValidationError("work package materialization is incomplete or incoherent")
        package = self._get_package(package_id)
        return WorkPackageAdmissionResult(
            package=package,
            plan_digest=compiled.plan_digest,
            plan_version=1,
            epoch=1,
            task_ids=tuple(item[0] for item in expected),
            created=False,
            held=package.state == "admitted",
            base_attestation=attestation,
        )

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
        row = self.store.query_one("SELECT * FROM work_packages WHERE id = ?", (package_id,))
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
