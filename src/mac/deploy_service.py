"""Artifact, environment, deployment, and runtime service.

Owns the deploy-infrastructure tables:

* ``artifacts`` — canonical record of a deliverable blob, keyed by digest.
  Re-registering augments signers/metadata; uri+kind are pinned on first
  write.
* ``environments`` + ``deployments`` + ``environment_events`` — where
  artifacts run. ``deploy_artifact`` is the only path that flips the
  active deployment, and it does the retire+insert atomically.
* ``runtime_environments`` + ``runtime_runs`` — typed execution sandboxes
  for tasks. Manifests are scanned to refuse ``:latest`` pins, raw secret
  fields, and unpinned dependencies before the row is written.

The runtime-manifest scanner uses the shared SECRET_FIELD_HINTS list so the
"no raw secret in a manifest" rule stays consistent with the message
validator.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from mac.models import (
    Agent,
    Artifact,
    Deployment,
    DeploymentStatus,
    Environment,
    Evidence,
    JsonDict,
    NotFoundError,
    RuntimeDeltaStatus,
    RuntimeEnvironment,
    RuntimeEnvironmentDelta,
    RuntimeRun,
    RuntimeRunStatus,
    Task,
    Tenant,
    ValidationError,
    coerce_list,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.observability_service import ObservabilityService

SECRET_FIELD_HINTS = (
    "secret",
    "token",
    "password",
    "private_key",
    "credential",
    "api_key",
    "auth",
)

ALLOWED_RUNTIME_DELTA_PACKAGE_MANAGERS = {"pip", "uv", "npm", "pnpm"}
RUNTIME_DELTA_SHA256_RE = "sha256:"


def _list_of_strings(value: Any, *, field_name: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError("%s must be a list" % field_name)
    out: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _list_of_json_values(value: Any, *, field_name: str) -> List[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError("%s must be a list" % field_name)
    out: List[Any] = []
    for item in value:
        if isinstance(item, (str, int, float, bool)) or item is None:
            text = str(item or "").strip()
            if text:
                out.append(text)
            continue
        if isinstance(item, dict):
            out.append(ensure_json_object(item))
            continue
        raise ValidationError("%s items must be strings or objects" % field_name)
    return out


def _sha256_digest(value: Optional[str]) -> bool:
    if not value:
        return False
    text = str(value).strip()
    if not text.startswith(RUNTIME_DELTA_SHA256_RE):
        return False
    suffix = text[len(RUNTIME_DELTA_SHA256_RE) :]
    return len(suffix) == 64 and all(ch in "0123456789abcdef" for ch in suffix)


def _state_value(state: Any) -> str:
    return state.value if hasattr(state, "value") else str(state)


def _hash_manifest(manifest: JsonDict) -> str:
    return hashlib.sha256(json_dumps(manifest).encode("utf-8")).hexdigest()


class DeployService:
    def __init__(
        self,
        store: Any,
        observability: ObservabilityService,
        *,
        get_tenant: Callable[[str], Tenant],
        get_task: Callable[[str], Task],
        get_agent: Callable[[str], Agent],
        get_evidence: Callable[[str], Evidence],
    ) -> None:
        self.store = store
        self.observability = observability
        self._get_tenant = get_tenant
        self._get_task = get_task
        self._get_agent = get_agent
        self._get_evidence = get_evidence

    # Artifacts ---------------------------------------------------------

    def register_artifact(
        self,
        kind: str,
        digest: str,
        uri: str,
        created_by: str,
        sbom_uri: Optional[str] = None,
        signers: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Artifact:
        kind = (kind or "").strip()
        digest = (digest or "").strip()
        uri = (uri or "").strip()
        if not kind:
            raise ValidationError("artifact kind is required")
        if not digest:
            raise ValidationError("artifact digest is required")
        if not uri:
            raise ValidationError("artifact uri is required")
        signer_list = coerce_list(signers)
        # mac-0a8o: for local file:// URIs (or bare absolute paths) we
        # can actually recompute the digest and reject mismatches. For
        # remote schemes (https/ssh/git/registry) full verification
        # requires fetching the artifact and is left for a follow-up;
        # we log the gap so operators can audit it.
        self._verify_artifact_digest_if_local(uri, digest)
        now = utcnow()
        # mac-vaze: re-register-with-new-signers used to do SELECT then
        # UPDATE outside a transaction, so two concurrent callers could
        # each read the same ``existing_signers`` and the later UPDATE
        # would silently drop the racer's additions. Wrap the
        # read+merge+write in one transaction; re-read inside.
        with self.store.transaction() as conn:
            existing_row = conn.execute(
                "SELECT * FROM artifacts WHERE digest = ?", (digest,)
            ).fetchone()
            if existing_row is not None:
                existing_signers = json_loads(existing_row["signers"], [])
                merged_signers = coerce_list(list(existing_signers) + signer_list)
                existing_meta = json_loads(existing_row["metadata"], {})
                merged_meta = dict(existing_meta)
                if metadata:
                    merged_meta.update(metadata)
                new_sbom = sbom_uri if sbom_uri is not None else existing_row["sbom_uri"]
                conn.execute(
                    """
                    UPDATE artifacts
                    SET uri = ?, sbom_uri = ?, signers = ?, metadata = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        uri,
                        new_sbom,
                        json_dumps(merged_signers),
                        json_dumps(merged_meta),
                        now,
                        existing_row["id"],
                    ),
                )
                # Read back on the transaction's OWN connection. get_artifact()
                # borrows a different pooled connection, which on Postgres
                # cannot see this not-yet-committed UPDATE -- so the caller was
                # handed the pre-update row while the database held the new
                # one. SQLite never showed it (one serialized connection), so
                # POST /artifacts returned stale uri/signers in production only.
                updated_row = conn.execute(
                    "SELECT * FROM artifacts WHERE id = ?", (existing_row["id"],)
                ).fetchone()
                return self._artifact_from_row(updated_row)
            # No existing row inside the same transaction → insert.
            artifact_id = new_id("art")
            conn.execute(
                """
                INSERT INTO artifacts (
                    id, kind, digest, uri, sbom_uri, signers, metadata,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    kind,
                    digest,
                    uri,
                    sbom_uri,
                    json_dumps(signer_list),
                    json_dumps(ensure_json_object(metadata)),
                    created_by,
                    now,
                    now,
                ),
            )
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id_or_digest: str) -> Artifact:
        row = self.store.query_one(
            "SELECT * FROM artifacts WHERE id = ? OR digest = ?",
            (artifact_id_or_digest, artifact_id_or_digest),
        )
        if row is None:
            raise NotFoundError("artifact not found: %s" % artifact_id_or_digest)
        return self._artifact_from_row(row)

    def list_artifacts(self, kind: Optional[str] = None) -> List[Artifact]:
        if kind:
            rows = self.store.query_all(
                "SELECT * FROM artifacts WHERE kind = ? ORDER BY created_at, id",
                (kind,),
            )
        else:
            rows = self.store.query_all("SELECT * FROM artifacts ORDER BY created_at, id")
        return [self._artifact_from_row(row) for row in rows]

    def delete_artifact(
        self, artifact_id_or_digest: str, actor: str = "operator"
    ) -> Dict[str, Any]:
        artifact = self.get_artifact(artifact_id_or_digest)
        deployment = self.store.query_one(
            "SELECT id FROM deployments WHERE artifact_id = ? LIMIT 1",
            (artifact.id,),
        )
        if deployment is not None:
            raise ValidationError("artifact is referenced by deployment %s" % deployment["id"])
        self.store.execute("DELETE FROM artifacts WHERE id = ?", (artifact.id,))
        self.observability.record_log(
            "artifact.deleted",
            layer="deploy",
            source=actor,
            subject_type="artifact",
            subject_id=artifact.id,
            detail={
                "digest": artifact.digest,
                "kind": artifact.kind,
                "uri": artifact.uri,
            },
        )
        return {"deleted": True, "artifact": artifact.to_dict()}

    # Environments + deployments ---------------------------------------

    def register_environment(
        self,
        name: str,
        tenant_id: Optional[str] = None,
        channel: str = "fleet",
        promotes_from: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_by: str = "human",
    ) -> Environment:
        name = (name or "").strip()
        if not name:
            raise ValidationError("environment name is required")
        if tenant_id is not None:
            self._get_tenant(tenant_id)
        channel = (channel or "fleet").strip() or "fleet"
        if promotes_from is not None:
            self.get_environment(promotes_from)
        now = utcnow()
        env_id = new_id("env")
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO environments (
                    id, name, tenant_id, channel, promotes_from, metadata,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    env_id,
                    name,
                    tenant_id,
                    channel,
                    promotes_from,
                    json_dumps(ensure_json_object(metadata)),
                    created_by,
                    now,
                    now,
                ),
            )
            self.insert_environment_event(
                conn,
                env_id,
                "environment.created",
                created_by,
                {
                    "name": name,
                    "tenant_id": tenant_id,
                    "channel": channel,
                    "promotes_from": promotes_from,
                },
                now,
            )
        return self.get_environment(env_id)

    def get_environment(self, env_id_or_name: str) -> Environment:
        row = self.store.query_one(
            "SELECT * FROM environments WHERE id = ? OR name = ?",
            (env_id_or_name, env_id_or_name),
        )
        if row is None:
            raise NotFoundError("environment not found: %s" % env_id_or_name)
        return self._environment_from_row(row)

    def list_environments(
        self,
        tenant_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> List[Environment]:
        clauses: List[str] = []
        params: List[Any] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if channel is not None:
            clauses.append("channel = ?")
            params.append(channel)
        sql = "SELECT * FROM environments"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY channel, name"
        return [self._environment_from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def deploy_artifact(
        self,
        environment_id: str,
        artifact_id: str,
        actor: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Deployment:
        """Atomically retire the current deployment in ``environment_id`` and
        record ``artifact_id`` as the new active deployment. Two writers
        cannot race because BEGIN IMMEDIATE serializes the retire+insert
        pair.
        """
        environment = self.get_environment(environment_id)
        artifact = self.get_artifact(artifact_id)
        now = utcnow()
        deployment_id = new_id("deploy")
        with self.store.transaction() as conn:
            prior = conn.execute(
                """
                SELECT id, artifact_id FROM deployments
                WHERE environment_id = ? AND retired_at IS NULL
                """,
                (environment.id,),
            ).fetchall()
            for row in prior:
                conn.execute(
                    "UPDATE deployments SET status = ?, retired_at = ? WHERE id = ?",
                    (DeploymentStatus.RETIRED.value, now, row["id"]),
                )
                self.insert_environment_event(
                    conn,
                    environment.id,
                    "environment.retired",
                    actor,
                    {"deployment_id": row["id"], "artifact_id": row["artifact_id"]},
                    now,
                )
            conn.execute(
                """
                INSERT INTO deployments (
                    id, environment_id, artifact_id, status, deployed_by,
                    deployed_at, retired_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    deployment_id,
                    environment.id,
                    artifact.id,
                    DeploymentStatus.ACTIVE.value,
                    actor,
                    now,
                    json_dumps(ensure_json_object(metadata)),
                ),
            )
            self.insert_environment_event(
                conn,
                environment.id,
                "environment.deployed",
                actor,
                {
                    "deployment_id": deployment_id,
                    "artifact_id": artifact.id,
                    "artifact_digest": artifact.digest,
                },
                now,
            )
        return self.get_deployment(deployment_id)

    def get_deployment(self, deployment_id: str) -> Deployment:
        row = self.store.query_one("SELECT * FROM deployments WHERE id = ?", (deployment_id,))
        if row is None:
            raise NotFoundError("deployment not found: %s" % deployment_id)
        return self._deployment_from_row(row)

    def current_deployment(self, environment_id: str) -> Optional[Deployment]:
        env = self.get_environment(environment_id)
        row = self.store.query_one(
            """
            SELECT * FROM deployments
            WHERE environment_id = ? AND retired_at IS NULL
            ORDER BY deployed_at DESC, id DESC
            LIMIT 1
            """,
            (env.id,),
        )
        return self._deployment_from_row(row) if row is not None else None

    def list_deployments(self, environment_id: str) -> List[Deployment]:
        env = self.get_environment(environment_id)
        rows = self.store.query_all(
            "SELECT * FROM deployments WHERE environment_id = ? ORDER BY deployed_at, id",
            (env.id,),
        )
        return [self._deployment_from_row(row) for row in rows]

    # Runtime environments + runs --------------------------------------

    def create_runtime(
        self, name: str, manifest: Dict[str, Any], created_by: str
    ) -> RuntimeEnvironment:
        if not name:
            raise ValidationError("runtime name is required")
        manifest_dict = ensure_json_object(manifest)
        self._validate_runtime_manifest(manifest_dict)
        now = utcnow()
        runtime_id = new_id("runtime")
        digest = _hash_manifest(manifest_dict)
        self.store.execute(
            """
            INSERT INTO runtime_environments (id, name, manifest, digest, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (runtime_id, name, json_dumps(manifest_dict), digest, created_by, now),
        )
        return self.get_runtime(runtime_id)

    def get_runtime(self, runtime_id_or_name: str) -> RuntimeEnvironment:
        row = self.store.query_one(
            "SELECT * FROM runtime_environments WHERE id = ? OR name = ?",
            (runtime_id_or_name, runtime_id_or_name),
        )
        if row is None:
            raise NotFoundError("runtime not found: %s" % runtime_id_or_name)
        return self._runtime_from_row(row)

    def list_runtimes(self) -> List[RuntimeEnvironment]:
        rows = self.store.query_all("SELECT * FROM runtime_environments ORDER BY name")
        return [self._runtime_from_row(row) for row in rows]

    # Runtime environment deltas --------------------------------------

    def propose_runtime_delta(
        self,
        task_id: str,
        agent_id: str,
        package_manager: str,
        commands: List[str],
        added_dependencies: List[Any],
        reason: str,
        *,
        project: Optional[str] = None,
        base_runtime_id: Optional[str] = None,
        base_runtime_digest: Optional[str] = None,
        lockfile_path: Optional[str] = None,
        lockfile_digest: Optional[str] = None,
        evidence_id: Optional[str] = None,
    ) -> RuntimeEnvironmentDelta:
        task = self._get_task(task_id)
        agent = self._get_agent(agent_id)
        if evidence_id:
            evidence = self._get_evidence(evidence_id)
            if evidence.task_id != task.id:
                raise ValidationError("runtime delta evidence must belong to task")
        package_manager = str(package_manager or "").strip().lower()
        if not package_manager:
            raise ValidationError("runtime delta package_manager is required")
        command_list = _list_of_strings(commands, field_name="commands")
        dependency_list = _list_of_json_values(added_dependencies, field_name="added_dependencies")
        reason = str(reason or "").strip()
        if not reason:
            raise ValidationError("runtime delta reason is required")
        resolved_runtime_id = str(base_runtime_id or "").strip() or None
        resolved_runtime_digest = str(base_runtime_digest or "").strip() or None
        if resolved_runtime_id:
            runtime = self.get_runtime(resolved_runtime_id)
            resolved_runtime_id = runtime.id
            resolved_runtime_digest = resolved_runtime_digest or runtime.digest
        if not resolved_runtime_digest:
            resolved_runtime_digest = getattr(agent, "running_digest", None)
        now = utcnow()
        delta_id = new_id("rtdelta")
        self.store.execute(
            """
            INSERT INTO runtime_environment_deltas (
                id, task_id, agent_id, project, base_runtime_id, base_runtime_digest,
                package_manager, commands, added_dependencies, lockfile_path,
                lockfile_digest, reason, status, validation, evidence_id,
                promoted_runtime_environment_id, created_at, updated_at,
                validated_at, promoted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, NULL)
            """,
            (
                delta_id,
                task.id,
                agent.id,
                str(project or task.project or "").strip() or None,
                resolved_runtime_id,
                resolved_runtime_digest,
                package_manager,
                json_dumps(command_list),
                json_dumps(dependency_list),
                str(lockfile_path or "").strip() or None,
                str(lockfile_digest or "").strip() or None,
                reason,
                RuntimeDeltaStatus.PROPOSED.value,
                json_dumps({}),
                evidence_id,
                now,
                now,
            ),
        )
        return self.get_runtime_delta(delta_id)

    def get_runtime_delta(self, delta_id: str) -> RuntimeEnvironmentDelta:
        row = self.store.query_one(
            "SELECT * FROM runtime_environment_deltas WHERE id = ?", (delta_id,)
        )
        if row is None:
            raise NotFoundError("runtime delta not found: %s" % delta_id)
        return self._runtime_delta_from_row(row)

    def list_runtime_deltas(
        self,
        *,
        status: Optional[str] = None,
        task_id: Optional[str] = None,
        project: Optional[str] = None,
        limit: int = 200,
    ) -> List[RuntimeEnvironmentDelta]:
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            status_value = _state_value(status)
            try:
                RuntimeDeltaStatus(status_value)
            except ValueError:
                raise ValidationError("unsupported runtime delta status: %s" % status_value)
            clauses.append("status = ?")
            params.append(status_value)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if project:
            clauses.append("project = ?")
            params.append(project)
        sql = "SELECT * FROM runtime_environment_deltas"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        rows = self.store.query_all(sql, params)
        return [self._runtime_delta_from_row(row) for row in rows]

    def validate_runtime_delta(
        self,
        delta_id: str,
        actor: str,
    ) -> RuntimeEnvironmentDelta:
        delta = self.get_runtime_delta(delta_id)
        if delta.status == RuntimeDeltaStatus.PROMOTED.value:
            raise ValidationError("promoted runtime deltas cannot be revalidated")
        problems = self._runtime_delta_validation_problems(delta)
        now = utcnow()
        status = (
            RuntimeDeltaStatus.REJECTED.value if problems else RuntimeDeltaStatus.VALIDATED.value
        )
        validation = {
            "schema": "mac.runtime_delta_validation.v1",
            "status": status,
            "actor": actor,
            "problems": problems,
            "checked_at": now,
        }
        self.store.execute(
            """
            UPDATE runtime_environment_deltas
               SET status = ?, validation = ?, updated_at = ?, validated_at = ?
             WHERE id = ?
            """,
            (status, json_dumps(validation), now, now, delta.id),
        )
        return self.get_runtime_delta(delta.id)

    def reject_runtime_delta(
        self,
        delta_id: str,
        actor: str,
        reason: str,
    ) -> RuntimeEnvironmentDelta:
        delta = self.get_runtime_delta(delta_id)
        if delta.status == RuntimeDeltaStatus.PROMOTED.value:
            raise ValidationError("promoted runtime deltas cannot be rejected")
        reason = str(reason or "").strip()
        if not reason:
            raise ValidationError("runtime delta rejection reason is required")
        now = utcnow()
        validation = {
            "schema": "mac.runtime_delta_validation.v1",
            "status": RuntimeDeltaStatus.REJECTED.value,
            "actor": actor,
            "problems": [reason],
            "checked_at": now,
            "manual_rejection": True,
        }
        self.store.execute(
            """
            UPDATE runtime_environment_deltas
               SET status = ?, validation = ?, updated_at = ?, validated_at = ?
             WHERE id = ?
            """,
            (
                RuntimeDeltaStatus.REJECTED.value,
                json_dumps(validation),
                now,
                now,
                delta.id,
            ),
        )
        return self.get_runtime_delta(delta.id)

    def promote_runtime_delta(
        self,
        delta_id: str,
        actor: str,
        *,
        runtime_name: Optional[str] = None,
    ) -> RuntimeEnvironmentDelta:
        delta = self.get_runtime_delta(delta_id)
        if delta.status != RuntimeDeltaStatus.VALIDATED.value:
            raise ValidationError("runtime delta must be validated before promotion")
        base = self._runtime_for_delta(delta)
        manifest = self._promoted_runtime_manifest(base, delta, actor)
        name = str(runtime_name or "").strip() or "%s+%s" % (
            base.name,
            delta.id.split("_", 1)[-1][:12],
        )
        runtime = self.create_runtime(name, manifest, actor)
        now = utcnow()
        validation = dict(delta.validation or {})
        validation["promoted_by"] = actor
        validation["promoted_at"] = now
        validation["promoted_runtime_environment_id"] = runtime.id
        self.store.execute(
            """
            UPDATE runtime_environment_deltas
               SET status = ?, validation = ?, promoted_runtime_environment_id = ?,
                   updated_at = ?, promoted_at = ?
             WHERE id = ?
            """,
            (
                RuntimeDeltaStatus.PROMOTED.value,
                json_dumps(validation),
                runtime.id,
                now,
                now,
                delta.id,
            ),
        )
        return self.get_runtime_delta(delta.id)

    def create_runtime_run(self, task_id: str, agent_id: str, environment_id: str) -> RuntimeRun:
        self._get_task(task_id)
        self._get_agent(agent_id)
        runtime = self.get_runtime(environment_id)
        now = utcnow()
        run_id = new_id("run")
        self.store.execute(
            """
            INSERT INTO runtime_runs (id, task_id, agent_id, environment_id, status, evidence_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (run_id, task_id, agent_id, runtime.id, RuntimeRunStatus.RUNNING.value, now, now),
        )
        return self.get_runtime_run(run_id)

    def complete_runtime_run(
        self,
        run_id: str,
        evidence_id: str,
        status: str = RuntimeRunStatus.COMPLETED.value,
    ) -> RuntimeRun:
        status_value = _state_value(status)
        try:
            RuntimeRunStatus(status_value)
        except ValueError:
            raise ValidationError("unsupported runtime_run status: %s" % status_value)
        if status_value == RuntimeRunStatus.RUNNING.value:
            raise ValidationError("complete_runtime_run cannot transition back to running")
        run = self.get_runtime_run(run_id)
        evidence = self._get_evidence(evidence_id)
        if evidence.task_id != run.task_id:
            raise ValidationError("runtime evidence must belong to run task")
        now = utcnow()
        self.store.execute(
            "UPDATE runtime_runs SET status = ?, evidence_id = ?, updated_at = ? WHERE id = ?",
            (status_value, evidence_id, now, run_id),
        )
        return self.get_runtime_run(run_id)

    def get_runtime_run(self, run_id: str) -> RuntimeRun:
        row = self.store.query_one("SELECT * FROM runtime_runs WHERE id = ?", (run_id,))
        if row is None:
            raise NotFoundError("runtime run not found: %s" % run_id)
        return self._runtime_run_from_row(row)

    def list_runtime_runs(self) -> List[RuntimeRun]:
        rows = self.store.query_all("SELECT * FROM runtime_runs ORDER BY created_at, id")
        return [self._runtime_run_from_row(row) for row in rows]

    def _runtime_for_delta(self, delta: RuntimeEnvironmentDelta) -> RuntimeEnvironment:
        if delta.base_runtime_id:
            return self.get_runtime(delta.base_runtime_id)
        if delta.base_runtime_digest:
            row = self.store.query_one(
                """
                SELECT * FROM runtime_environments
                 WHERE digest = ?
                 ORDER BY created_at DESC, name
                 LIMIT 1
                """,
                (delta.base_runtime_digest,),
            )
            if row is not None:
                return self._runtime_from_row(row)
        raise ValidationError("runtime delta has no registered base runtime")

    def _runtime_delta_validation_problems(self, delta: RuntimeEnvironmentDelta) -> List[str]:
        problems: List[str] = []
        if delta.package_manager not in ALLOWED_RUNTIME_DELTA_PACKAGE_MANAGERS:
            problems.append(
                "package_manager must be one of %s"
                % ", ".join(sorted(ALLOWED_RUNTIME_DELTA_PACKAGE_MANAGERS))
            )
        if not delta.commands:
            problems.append("commands must include the task-local install steps")
        if not delta.added_dependencies:
            problems.append("added_dependencies must include at least one dependency")
        if not delta.base_runtime_id and not delta.base_runtime_digest:
            problems.append("base_runtime_id or base_runtime_digest is required")
        if delta.base_runtime_id:
            try:
                runtime = self.get_runtime(delta.base_runtime_id)
                if delta.base_runtime_digest and delta.base_runtime_digest != runtime.digest:
                    problems.append("base_runtime_digest does not match base_runtime_id")
            except NotFoundError:
                problems.append("base_runtime_id does not reference a registered runtime")
        elif delta.base_runtime_digest:
            row = self.store.query_one(
                "SELECT 1 FROM runtime_environments WHERE digest = ? LIMIT 1",
                (delta.base_runtime_digest,),
            )
            if row is None:
                problems.append("base_runtime_digest is not registered")
        if not delta.lockfile_path:
            problems.append("lockfile_path is required")
        elif str(delta.lockfile_path).startswith("/") or ".." in str(delta.lockfile_path).split(
            "/"
        ):
            problems.append("lockfile_path must be relative to the task worktree")
        if not _sha256_digest(delta.lockfile_digest):
            problems.append("lockfile_digest must be sha256:<64 lowercase hex chars>")
        problems.extend(self._runtime_delta_command_problems(delta))
        problems.extend(self._runtime_delta_dependency_problems(delta))
        try:
            self._scan_runtime_manifest(
                {
                    "package_manager": delta.package_manager,
                    "commands": delta.commands,
                    "dependencies": delta.added_dependencies,
                    "lockfile_path": delta.lockfile_path,
                    "reason": delta.reason,
                },
                ("runtime_delta",),
            )
        except ValidationError as exc:
            problems.append(str(exc))
        return problems

    def _runtime_delta_command_problems(self, delta: RuntimeEnvironmentDelta) -> List[str]:
        problems: List[str] = []
        forbidden = (
            " sudo ",
            " apt-get ",
            " apt install ",
            " brew install ",
            " yum install ",
            " dnf install ",
            " apk add ",
            " conda install ",
            " pipx install ",
            " uv tool install ",
            " yarn global ",
            " /usr/local/",
            " /opt/homebrew/",
        )
        for command in delta.commands:
            lowered = " %s " % str(command or "").strip().lower()
            for marker in forbidden:
                if marker in lowered:
                    problems.append("command mutates host/shared environment: %s" % command)
                    break
            if any(
                marker in lowered
                for marker in (
                    " token=",
                    " api_key=",
                    " apikey=",
                    " password=",
                    " secret=",
                    " bearer ",
                )
            ):
                problems.append("command appears to include raw secret material")
            if any(
                marker in lowered
                for marker in (
                    " npm install -g ",
                    " npm i -g ",
                    " pnpm add -g ",
                    " pnpm install -g ",
                )
            ):
                problems.append("node dependency commands must not install globally")
            pip_install = " pip install " in lowered or " python -m pip install " in lowered
            if pip_install and delta.package_manager == "pip":
                if (
                    ".venv" not in lowered
                    and "virtual_env" not in lowered
                    and "venv/bin" not in lowered
                ):
                    problems.append("pip installs must target a task-local virtualenv")
        return problems

    def _runtime_delta_dependency_problems(self, delta: RuntimeEnvironmentDelta) -> List[str]:
        problems: List[str] = []
        for dependency in delta.added_dependencies:
            if isinstance(dependency, dict):
                requirement = str(dependency.get("requirement") or "").strip()
                if requirement:
                    if not self._runtime_delta_dependency_pinned(
                        requirement, delta.package_manager
                    ):
                        problems.append("dependency is not pinned: %s" % requirement)
                    continue
                name = str(dependency.get("name") or "").strip()
                version = str(
                    dependency.get("version")
                    or dependency.get("specifier")
                    or dependency.get("resolved")
                    or ""
                ).strip()
                if not name or not version:
                    problems.append("dependency objects require name and version/specifier")
                    continue
                if version in {"*", "latest"} or version.endswith("*"):
                    problems.append("dependency is not pinned: %s" % name)
                continue
            text = str(dependency or "").strip()
            if not self._runtime_delta_dependency_pinned(text, delta.package_manager):
                problems.append("dependency is not pinned: %s" % text)
        return problems

    def _runtime_delta_dependency_pinned(self, dependency: str, package_manager: str) -> bool:
        text = str(dependency or "").strip()
        if not text:
            return False
        lowered = text.lower()
        if "*" in text or lowered.endswith("@latest") or lowered == "latest":
            return False
        if package_manager in {"pip", "uv"}:
            return (
                "==" in text
                or "===" in text
                or " @ " in text
                or "#sha256=" in lowered
                or "--hash=sha256:" in lowered
            )
        if package_manager in {"npm", "pnpm"}:
            if text.startswith("@"):
                return text.count("@") >= 2 and not lowered.endswith("@latest")
            return "@" in text and not lowered.endswith("@latest")
        return False

    def _promoted_runtime_manifest(
        self,
        base: RuntimeEnvironment,
        delta: RuntimeEnvironmentDelta,
        actor: str,
    ) -> JsonDict:
        manifest = json_loads(json_dumps(base.manifest), {})
        dependencies = manifest.get("dependencies")
        merged_dependencies = list(dependencies) if isinstance(dependencies, list) else []
        for dependency in delta.added_dependencies:
            if dependency not in merged_dependencies:
                merged_dependencies.append(dependency)
        if merged_dependencies:
            manifest["dependencies"] = merged_dependencies
        manifest["derived_from"] = {
            "runtime_environment_id": base.id,
            "digest": base.digest,
        }
        deltas = manifest.get("runtime_deltas")
        if not isinstance(deltas, list):
            deltas = []
        deltas.append(
            {
                "id": delta.id,
                "task_id": delta.task_id,
                "agent_id": delta.agent_id,
                "package_manager": delta.package_manager,
                "commands": delta.commands,
                "dependencies": delta.added_dependencies,
                "lockfile_path": delta.lockfile_path,
                "lockfile_digest": delta.lockfile_digest,
                "reason": delta.reason,
                "promoted_by": actor,
            }
        )
        manifest["runtime_deltas"] = deltas
        return ensure_json_object(manifest)

    # Audit (shared by environments, exposed for rollouts) -------------

    def insert_environment_event(
        self,
        conn: Any,
        environment_id: str,
        event_type: str,
        actor: str,
        detail: Dict[str, Any],
        when: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO environment_events (id, environment_id, event_type, actor, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("envevt"), environment_id, event_type, actor, json_dumps(detail), when),
        )
        self.observability.insert_observation(
            conn,
            "log",
            event_type,
            "control_plane",
            "environment",
            "info",
            None,
            "",
            "environment",
            environment_id,
            {"actor": actor, **detail},
            when,
        )

    # mac-0a8o: recompute the artifact digest for local-file URIs so a
    # caller-supplied digest can't lie about what's actually on disk.
    # Remote URIs are out of scope for now (would require network IO
    # and per-scheme handling); we log them so the gap is visible.
    def _verify_artifact_digest_if_local(self, uri: str, declared_digest: str) -> None:
        import hashlib as _hashlib
        from pathlib import Path as _P

        if not declared_digest.startswith("sha256:"):
            # Unknown algo — caller will see the deeper rollout-side
            # _validate_artifact_hash check; nothing to recompute here.
            return
        local_path: Optional[_P] = None
        if uri.startswith("file://"):
            local_path = _P(uri[len("file://") :])
        elif uri.startswith("/"):
            local_path = _P(uri)
        if local_path is None:
            # Remote scheme: log the gap and trust the caller.
            self.observability.record_log(
                "artifact.digest_unverified_remote",
                level="warning",
                layer="control_plane",
                source="deploy",
                subject_type="artifact",
                subject_id=declared_digest,
                detail={
                    "uri": uri,
                    "note": "mac-0a8o: remote artifact digests are not yet recomputed; "
                    "operator-driven verification required",
                },
            )
            return
        if not local_path.exists() or not local_path.is_file():
            # Path doesn't resolve. Don't reject (manifest may
            # legitimately point at a not-yet-published artifact);
            # just log.
            self.observability.record_log(
                "artifact.digest_unverified_missing",
                level="warning",
                layer="control_plane",
                source="deploy",
                subject_type="artifact",
                subject_id=declared_digest,
                detail={"uri": uri, "path": str(local_path)},
            )
            return
        h = _hashlib.sha256()
        with local_path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 16)
                if not chunk:
                    break
                h.update(chunk)
        actual = "sha256:" + h.hexdigest()
        if actual != declared_digest:
            raise ValidationError(
                "artifact digest %s does not match recomputed %s for %s"
                % (declared_digest, actual, uri)
            )

    # Runtime manifest validation --------------------------------------

    def _validate_runtime_manifest(self, manifest: JsonDict) -> None:
        self._scan_runtime_manifest(manifest, ())

    def _scan_runtime_manifest(self, value: Any, path: Sequence[str]) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                key_str = str(key)
                key_lower = key_str.lower()
                if any(hint in key_lower for hint in SECRET_FIELD_HINTS) and key_lower not in {
                    "secret_refs",
                    "secret_ref",
                }:
                    raise ValidationError(
                        "runtime manifest cannot include raw secret field: %s"
                        % ".".join(path + (key_str,))
                    )
                self._scan_runtime_manifest(nested, path + (key_str,))
            return
        if isinstance(value, list):
            in_dependencies = path and path[-1].lower() == "dependencies"
            for index, nested in enumerate(value):
                if in_dependencies and isinstance(nested, str) and nested.strip().endswith("*"):
                    raise ValidationError("runtime dependencies must be pinned")
                self._scan_runtime_manifest(nested, path + (str(index),))
            return
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.endswith(":latest"):
                raise ValidationError(
                    "runtime manifest field at %s pins :latest; pin a digest"
                    % (".".join(path) or "(root)")
                )
            if (
                path
                and path[-1].lower() in {"image", "container_image"}
                and "@sha256:" not in stripped
            ):
                raise ValidationError(
                    "runtime manifest image at %s must include a sha256 digest" % ".".join(path)
                )

    # Row hydration ----------------------------------------------------

    def _artifact_from_row(self, row: Any) -> Artifact:
        return Artifact(
            row["id"],
            row["kind"],
            row["digest"],
            row["uri"],
            row["sbom_uri"],
            json_loads(row["signers"], []),
            json_loads(row["metadata"], {}),
            row["created_by"],
            row["created_at"],
            row["updated_at"],
        )

    def _environment_from_row(self, row: Any) -> Environment:
        return Environment(
            row["id"],
            row["name"],
            row["tenant_id"],
            row["channel"],
            row["promotes_from"],
            json_loads(row["metadata"], {}),
            row["created_by"],
            row["created_at"],
            row["updated_at"],
        )

    def _deployment_from_row(self, row: Any) -> Deployment:
        return Deployment(
            row["id"],
            row["environment_id"],
            row["artifact_id"],
            row["status"],
            row["deployed_by"],
            row["deployed_at"],
            row["retired_at"],
            json_loads(row["metadata"], {}),
        )

    def _runtime_from_row(self, row: Any) -> RuntimeEnvironment:
        return RuntimeEnvironment(
            row["id"],
            row["name"],
            json_loads(row["manifest"], {}),
            row["digest"],
            row["created_by"],
            row["created_at"],
        )

    def _runtime_delta_from_row(self, row: Any) -> RuntimeEnvironmentDelta:
        return RuntimeEnvironmentDelta(
            row["id"],
            row["task_id"],
            row["agent_id"],
            row["project"],
            row["base_runtime_id"],
            row["base_runtime_digest"],
            row["package_manager"],
            json_loads(row["commands"], []),
            json_loads(row["added_dependencies"], []),
            row["lockfile_path"],
            row["lockfile_digest"],
            row["reason"],
            row["status"],
            json_loads(row["validation"], {}),
            row["evidence_id"],
            row["promoted_runtime_environment_id"],
            row["created_at"],
            row["updated_at"],
            row["validated_at"],
            row["promoted_at"],
        )

    def _runtime_run_from_row(self, row: Any) -> RuntimeRun:
        return RuntimeRun(
            row["id"],
            row["task_id"],
            row["agent_id"],
            row["environment_id"],
            row["status"],
            row["evidence_id"],
            row["created_at"],
            row["updated_at"],
        )
