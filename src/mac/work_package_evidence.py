"""Content-address worker submissions at the work-package evidence boundary.

The attribution row written by :meth:`ControlPlane.add_evidence` is immutable,
so it must carry an artifact identity when it is first appended.  The identity
below deliberately includes only the output facts needed to reproduce that
identity: the exact Git attempt, the worker's protocol declaration, and the
digests plus normalized descriptors of any durable artifacts.

Evidence URIs, artifact source/content URIs, arbitrary metadata, summaries,
database IDs, timestamps, and artifact bytes are excluded.  Those values are
either presentation/location data, volatile controller data, or may contain
credentials.  The resulting value is therefore a secret-free ``sha256:``
identifier, not a second mutable verification result.  Repository authority
still comes from the controller's independent output-verification receipt.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from mac.models import JsonDict, ensure_json_object, json_dumps


ATTEMPT_ARTIFACT_MANIFEST_SCHEMA = "mac.work_package.attempt_artifact_manifest.v1"


def attempt_artifact_manifest(
    *,
    verification: Mapping[str, Any],
    attempt_ref: str,
    attempt_base_sha: str,
    attempt_head_sha: str,
    declared_effects_digest: str,
    artifacts: Sequence[Mapping[str, Any]] = (),
) -> JsonDict:
    """Return the canonical, secret-free identity of one worker output.

    Artifact order is not meaningful at the evidence API boundary, so durable
    artifact identities are sorted by their canonical JSON representation.
    Duplicate identities remain duplicates and therefore remain observable.
    """

    declaration = ensure_json_object(verification)
    repo = ensure_json_object(declaration.get("repo"))
    artifact_identities = [
        {
            "name": str(item.get("name") or ""),
            "artifact_type": str(item.get("artifact_type") or ""),
            "content_type": str(item.get("content_type") or ""),
            "encoding": str(item.get("encoding") or ""),
            "size_bytes": int(item.get("size_bytes") or 0),
            "sha256": str(item.get("sha256") or ""),
            "truncated": bool(item.get("truncated")),
        }
        for item in artifacts
    ]
    artifact_identities.sort(key=json_dumps)
    return {
        "schema": ATTEMPT_ARTIFACT_MANIFEST_SCHEMA,
        "attempt": {
            "ref": str(attempt_ref),
            "base_sha": str(attempt_base_sha),
            "head_sha": str(attempt_head_sha),
            "declared_effects_digest": str(declared_effects_digest),
        },
        # This allowlist binds the protocol-level worker claim without copying
        # authenticated remotes, command output, signatures, or other arbitrary
        # evidence metadata into the content-addressed identity.
        "worker_declaration": {
            "schema": str(declaration.get("schema") or ""),
            "status": str(declaration.get("status") or "").strip().lower(),
            "evidence_type": str(declaration.get("evidence_type") or "")
            .strip()
            .lower(),
            "pushed": repo.get("pushed") is True,
        },
        "durable_artifacts": artifact_identities,
    }


def attempt_artifact_manifest_digest(**kwargs: Any) -> str:
    """Return the canonical ``sha256:`` digest for one attempt manifest."""

    canonical = json_dumps(attempt_artifact_manifest(**kwargs)).encode("utf-8")
    return "sha256:%s" % hashlib.sha256(canonical).hexdigest()
