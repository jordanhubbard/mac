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
    RuntimeEnvironment,
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
                    SET sbom_uri = ?, signers = ?, metadata = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_sbom,
                        json_dumps(merged_signers),
                        json_dumps(merged_meta),
                        now,
                        existing_row["id"],
                    ),
                )
                return self.get_artifact(existing_row["id"])
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
        return [
            self._environment_from_row(row)
            for row in self.store.query_all(sql, tuple(params))
        ]

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
        row = self.store.query_one(
            "SELECT * FROM deployments WHERE id = ?", (deployment_id,)
        )
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

    def create_runtime_run(
        self, task_id: str, agent_id: str, environment_id: str
    ) -> RuntimeRun:
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
            local_path = _P(uri[len("file://"):])
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
            if path and path[-1].lower() in {"image", "container_image"} and "@sha256:" not in stripped:
                raise ValidationError(
                    "runtime manifest image at %s must include a sha256 digest"
                    % ".".join(path)
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
