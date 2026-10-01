"""Artifact registry service.

Owns the ``artifacts`` table: the canonical record of a deliverable blob, keyed
by digest. Re-registering augments signers/metadata; uri+kind are pinned on
first write. AgentBus artifact publication (``mac agentbus artifact-publish``)
records what it publishes here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from mac.models import (
    Artifact,
    NotFoundError,
    ValidationError,
    coerce_list,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.observability_service import ObservabilityService


class ArtifactRegistryService:
    def __init__(self, store: Any, observability: ObservabilityService) -> None:
        self.store = store
        self.observability = observability

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

    # mac-0a8o: recompute the artifact digest for local-file URIs so a
    # caller-supplied digest can't lie about what's actually on disk.
    # Remote URIs are out of scope for now (would require network IO
    # and per-scheme handling); we log them so the gap is visible.
    def _verify_artifact_digest_if_local(self, uri: str, declared_digest: str) -> None:
        if not declared_digest.startswith("sha256:"):
            # Unknown algorithm: nothing to recompute here.
            return
        local_path: Optional[Path] = None
        if uri.startswith("file://"):
            local_path = Path(uri[len("file://") :])
        elif uri.startswith("/"):
            local_path = Path(uri)
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
        h = hashlib.sha256()
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
