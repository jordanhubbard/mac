"""Published source releases and monotonic fleet desired-source state."""

from __future__ import annotations

import re
from typing import Any, List, Optional
from urllib.parse import urlsplit

from mac.models import (
    DesiredSourcePolicy,
    FleetDesiredSourceState,
    JsonDict,
    NotFoundError,
    SourceRelease,
    ValidationError,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_RELEASE_STATUSES = {"draft", "reviewed", "published", "retracted"}


class SourceReleaseService:
    """Production write path for immutable releases and desired generations."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def register_release(
        self,
        *,
        repository_id: str,
        repository_name: str,
        canonical_remote_url: str,
        commit_sha: str,
        canonical_ref: str,
        tree_digest: str,
        created_by: str,
        status: str = "draft",
        artifact_digest: Optional[str] = None,
        image_digest: Optional[str] = None,
        created_by_task_id: Optional[str] = None,
        review_evidence_id: Optional[str] = None,
        publication_evidence_id: Optional[str] = None,
        metadata: Optional[JsonDict] = None,
    ) -> SourceRelease:
        values = {
            "repository_id": str(repository_id or "").strip(),
            "repository_name": str(repository_name or "").strip(),
            "canonical_remote_url": str(canonical_remote_url or "").strip(),
            "commit_sha": str(commit_sha or "").strip(),
            "canonical_ref": str(canonical_ref or "").strip(),
            "tree_digest": str(tree_digest or "").strip(),
            "created_by": str(created_by or "").strip(),
            "status": str(status or "").strip().lower(),
        }
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise ValidationError("source release requires %s" % ", ".join(sorted(missing)))
        if values["status"] not in _RELEASE_STATUSES:
            raise ValidationError("unsupported source release status: %s" % values["status"])
        if not _DIGEST.fullmatch(values["tree_digest"]):
            raise ValidationError("tree_digest must be sha256:<64 lowercase hex>")
        for optional_digest, name in (
            (artifact_digest, "artifact_digest"),
            (image_digest, "image_digest"),
        ):
            if optional_digest and not _DIGEST.fullmatch(str(optional_digest)):
                raise ValidationError("%s must be sha256:<64 lowercase hex>" % name)
        self._reject_credentialed_remote(values["canonical_remote_url"])
        release_metadata = dict(metadata or {})
        if values["status"] == "published":
            self._validate_publication_evidence(
                release_metadata,
                review_evidence_id=review_evidence_id,
                publication_evidence_id=publication_evidence_id,
            )

        now = utcnow()
        candidate = SourceRelease(
            id=new_id("release"),
            repository_id=values["repository_id"],
            repository_name=values["repository_name"],
            canonical_remote_url=values["canonical_remote_url"],
            commit_sha=values["commit_sha"],
            canonical_ref=values["canonical_ref"],
            tree_digest=values["tree_digest"],
            artifact_digest=str(artifact_digest) if artifact_digest else None,
            image_digest=str(image_digest) if image_digest else None,
            created_by=values["created_by"],
            created_by_task_id=created_by_task_id,
            review_evidence_id=review_evidence_id,
            publication_evidence_id=publication_evidence_id,
            status=values["status"],
            metadata=release_metadata,
            created_at=now,
            updated_at=now,
        )
        existing = self.store.query_one(
            "SELECT * FROM source_releases WHERE repository_id = ? AND commit_sha = ?",
            (candidate.repository_id, candidate.commit_sha),
        )
        if existing is not None:
            result = self._release_from_row(existing)
            immutable = (
                result.canonical_remote_url,
                result.canonical_ref,
                result.tree_digest,
                result.artifact_digest,
                result.image_digest,
            )
            requested = (
                candidate.canonical_remote_url,
                candidate.canonical_ref,
                candidate.tree_digest,
                candidate.artifact_digest,
                candidate.image_digest,
            )
            if immutable != requested:
                raise ValidationError(
                    "source release already exists with different immutable material"
                )
            return result
        self.store.execute(
            """
            INSERT INTO source_releases (
                id, repository_id, repository_name, canonical_remote_url,
                commit_sha, canonical_ref, tree_digest, artifact_digest,
                image_digest, created_by, created_by_task_id,
                review_evidence_id, publication_evidence_id, status, metadata,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.id,
                candidate.repository_id,
                candidate.repository_name,
                candidate.canonical_remote_url,
                candidate.commit_sha,
                candidate.canonical_ref,
                candidate.tree_digest,
                candidate.artifact_digest,
                candidate.image_digest,
                candidate.created_by,
                candidate.created_by_task_id,
                candidate.review_evidence_id,
                candidate.publication_evidence_id,
                candidate.status,
                json_dumps(candidate.metadata),
                candidate.created_at,
                candidate.updated_at,
            ),
        )
        return self.get_release(candidate.id)

    def get_release(self, release_id: str) -> SourceRelease:
        row = self.store.query_one("SELECT * FROM source_releases WHERE id = ?", (release_id,))
        if row is None:
            raise NotFoundError("source release not found: %s" % release_id)
        return self._release_from_row(row)

    def list_releases(
        self,
        *,
        repository_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[SourceRelease]:
        clauses: List[str] = []
        params: List[Any] = []
        if repository_id:
            clauses.append("repository_id = ?")
            params.append(repository_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        params.append(max(1, min(int(limit), 1000)))
        rows = self.store.query_all(
            "SELECT * FROM source_releases%s ORDER BY created_at DESC, id DESC LIMIT ?"
            % ((" WHERE " + " AND ".join(clauses)) if clauses else ""),
            tuple(params),
        )
        return [self._release_from_row(row) for row in rows]

    def set_desired_source(
        self,
        *,
        release_id: str,
        actor: str,
        reason: str,
        request_id: str,
        fleet_id: Optional[str] = None,
        environment_id: Optional[str] = None,
        rollout_policy: str = DesiredSourcePolicy.IMMEDIATE.value,
        paused: bool = False,
        expected_generation: Optional[int] = None,
    ) -> FleetDesiredSourceState:
        if bool(fleet_id) == bool(environment_id):
            raise ValidationError("exactly one of fleet_id or environment_id is required")
        if not actor or not request_id:
            raise ValidationError("actor and request_id are required")
        if rollout_policy not in {item.value for item in DesiredSourcePolicy}:
            raise ValidationError("unsupported desired-source rollout policy")
        release = self.get_release(release_id)
        if release.status != "published":
            raise ValidationError("desired source requires a published release")
        scope_column = "fleet_id" if fleet_id else "environment_id"
        scope_id = str(fleet_id or environment_id)
        scope_key = "%s:%s" % (scope_column, scope_id)
        now = utcnow()
        with self.store.transaction() as conn:
            prior_request = conn.execute(
                """
                SELECT d.* FROM fleet_desired_source_idempotency i
                JOIN fleet_desired_source_states d ON d.id = i.desired_source_state_id
                WHERE i.scope_key = ? AND i.request_id = ?
                """,
                (scope_key, request_id),
            ).fetchone()
            if prior_request is not None:
                return self._desired_from_row(prior_request)
            scope_table = "fleets" if fleet_id else "environments"
            scope_exists = conn.execute(
                "SELECT id FROM %s WHERE id = ?" % scope_table, (scope_id,)
            ).fetchone()
            if scope_exists is None:
                raise NotFoundError(
                    "%s not found: %s" % (scope_column.removesuffix("_id"), scope_id)
                )
            current = conn.execute(
                "SELECT * FROM fleet_desired_source_states WHERE %s = ?" % scope_column,
                (scope_id,),
            ).fetchone()
            prior_generation = int(current["generation"]) if current is not None else None
            if expected_generation is not None and prior_generation != expected_generation:
                raise ValidationError(
                    "desired-source generation changed: expected %s, found %s"
                    % (expected_generation, prior_generation)
                )
            generation = (prior_generation or 0) + 1
            state_id = str(current["id"]) if current is not None else new_id("dss")
            if current is None:
                conn.execute(
                    """
                    INSERT INTO fleet_desired_source_states (
                        id, fleet_id, environment_id, generation, release_id,
                        rollout_policy, actor, reason, prior_generation, paused,
                        request_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                    """,
                    (
                        state_id,
                        fleet_id,
                        environment_id,
                        generation,
                        release_id,
                        rollout_policy,
                        actor,
                        reason or "",
                        int(paused),
                        request_id,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE fleet_desired_source_states
                    SET generation = ?, release_id = ?, rollout_policy = ?,
                        actor = ?, reason = ?, prior_generation = ?, paused = ?,
                        request_id = ?, updated_at = ?
                    WHERE id = ? AND generation = ?
                    """,
                    (
                        generation,
                        release_id,
                        rollout_policy,
                        actor,
                        reason or "",
                        prior_generation,
                        int(paused),
                        request_id,
                        now,
                        state_id,
                        prior_generation,
                    ),
                )
            conn.execute(
                """
                INSERT INTO fleet_desired_source_transitions (
                    id, desired_source_state_id, from_generation, to_generation,
                    release_id, rollout_policy, actor, reason, request_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("dsstransition"),
                    state_id,
                    prior_generation,
                    generation,
                    release_id,
                    rollout_policy,
                    actor,
                    reason or "",
                    request_id,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO fleet_desired_source_idempotency (
                    id, scope_key, request_id, desired_source_state_id,
                    generation, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (new_id("dssidempotency"), scope_key, request_id, state_id, generation, now),
            )
            row = conn.execute(
                "SELECT * FROM fleet_desired_source_states WHERE id = ?", (state_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - transaction invariant
            raise RuntimeError("desired-source update committed without a state row")
        return self._desired_from_row(row)

    @staticmethod
    def _validate_publication_evidence(
        metadata: JsonDict,
        *,
        review_evidence_id: Optional[str],
        publication_evidence_id: Optional[str],
    ) -> None:
        if not review_evidence_id or not publication_evidence_id:
            raise ValidationError(
                "published source release requires review and publication evidence"
            )
        ci = metadata.get("ci")
        local = metadata.get("local_contract_tests")
        if not isinstance(ci, dict) or ci.get("known") is not True:
            raise ValidationError("published source release requires known CI evidence")
        if ci.get("pending") or ci.get("failed"):
            raise ValidationError("published source release CI is not green")
        if not isinstance(local, dict) or local.get("status") != "passed":
            raise ValidationError("published source release requires passing local contract tests")

    @staticmethod
    def _reject_credentialed_remote(remote: str) -> None:
        parsed = urlsplit(remote)
        if parsed.scheme and (parsed.username or parsed.password):
            raise ValidationError("canonical_remote_url must not embed credentials")
        if "@" in remote and "://" in remote and parsed.username:
            raise ValidationError("canonical_remote_url must not embed credentials")

    @staticmethod
    def _release_from_row(row: Any) -> SourceRelease:
        return SourceRelease(
            id=str(row["id"]),
            repository_id=str(row["repository_id"]),
            repository_name=str(row["repository_name"]),
            canonical_remote_url=str(row["canonical_remote_url"]),
            commit_sha=str(row["commit_sha"]),
            canonical_ref=str(row["canonical_ref"]),
            tree_digest=str(row["tree_digest"]),
            artifact_digest=row["artifact_digest"],
            image_digest=row["image_digest"],
            created_by=str(row["created_by"]),
            created_by_task_id=row["created_by_task_id"],
            review_evidence_id=row["review_evidence_id"],
            publication_evidence_id=row["publication_evidence_id"],
            status=str(row["status"]),
            metadata=json_loads(row["metadata"], {}),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _desired_from_row(row: Any) -> FleetDesiredSourceState:
        return FleetDesiredSourceState(
            id=str(row["id"]),
            fleet_id=row["fleet_id"],
            environment_id=row["environment_id"],
            generation=int(row["generation"]),
            release_id=str(row["release_id"]),
            rollout_policy=str(row["rollout_policy"]),
            actor=str(row["actor"]),
            reason=str(row["reason"]),
            prior_generation=(
                int(row["prior_generation"]) if row["prior_generation"] is not None else None
            ),
            paused=bool(row["paused"]),
            request_id=row["request_id"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
