from __future__ import annotations

from mac.work_package_evidence import (
    ATTEMPT_ARTIFACT_MANIFEST_SCHEMA,
    attempt_artifact_manifest,
    attempt_artifact_manifest_digest,
)


ATTEMPT_REF = "refs/mac/attempts/wp-test/epoch-1/change/attempt-1/lease-test"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
EFFECTS_DIGEST = "sha256:" + ("c" * 64)


def _verification(*, head_sha: str = HEAD_SHA) -> dict[str, object]:
    return {
        "status": "complete",
        "schema": "mac.worker_evidence.v1",
        "evidence_type": "repo_change",
        "repo": {
            "remote_ref": ATTEMPT_REF,
            "base_sha": BASE_SHA,
            "head_sha": head_sha,
            "pushed": True,
            # Location data is intentionally not part of the secret-free
            # artifact identity.
            "remote_url": "https://token@example.invalid/private.git",
        },
        "checks": [{"name": "tests", "output": "may contain worker logs"}],
    }


def _artifact(
    *,
    name: str = "worker-result.json",
    digest_char: str = "d",
) -> dict[str, object]:
    return {
        "id": "controller-random-id",
        "evidence_id": "controller-random-evidence-id",
        "name": name,
        "artifact_type": "worker_result",
        "source_uri": "file:///credential-bearing/location",
        "content_uri": "blob:///controller-specific/location",
        "content_type": "application/json",
        "encoding": "base64",
        "size_bytes": 123,
        "sha256": "sha256:" + (digest_char * 64),
        "truncated": False,
        "metadata": {"secret": "not identity material"},
        "created_at": "controller-specific-time",
    }


def _digest(
    *,
    verification: dict[str, object] | None = None,
    artifacts: list[dict[str, object]] | None = None,
) -> str:
    return attempt_artifact_manifest_digest(
        verification=verification or _verification(),
        attempt_ref=ATTEMPT_REF,
        attempt_base_sha=BASE_SHA,
        attempt_head_sha=str(
            ((verification or _verification()).get("repo") or {}).get(
                "head_sha", HEAD_SHA
            )
        ),
        declared_effects_digest=EFFECTS_DIGEST,
        artifacts=artifacts or [_artifact()],
    )


def test_attempt_artifact_manifest_digest_is_canonical_and_secret_free() -> None:
    first_artifact = _artifact()
    second_artifact = _artifact(name="stdout.txt", digest_char="e")
    first = _digest(artifacts=[first_artifact, second_artifact])

    relocated_first = {
        **first_artifact,
        "id": "different-random-id",
        "source_uri": "https://other-secret@example.invalid/result",
        "content_uri": "blob:///other-controller/location",
        "metadata": {"secret": "different"},
        "created_at": "different-time",
    }
    reordered_verification = {
        "checks": [{"name": "different presentation data"}],
        "repo": {
            "remote_url": "ssh://another-secret@example.invalid/private.git",
            "pushed": True,
            "head_sha": HEAD_SHA,
        },
        "evidence_type": "repo_change",
        "schema": "mac.worker_evidence.v1",
        "status": "COMPLETE",
    }
    second = _digest(
        verification=reordered_verification,
        artifacts=[second_artifact, relocated_first],
    )

    assert first == second
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64
    manifest = attempt_artifact_manifest(
        verification=_verification(),
        attempt_ref=ATTEMPT_REF,
        attempt_base_sha=BASE_SHA,
        attempt_head_sha=HEAD_SHA,
        declared_effects_digest=EFFECTS_DIGEST,
        artifacts=[first_artifact],
    )
    assert manifest["schema"] == ATTEMPT_ARTIFACT_MANIFEST_SCHEMA
    assert "remote_url" not in str(manifest)
    assert "source_uri" not in str(manifest)
    assert "content_uri" not in str(manifest)


def test_attempt_artifact_manifest_digest_detects_exact_output_tampering() -> None:
    original = _digest()
    changed_head = _verification(head_sha="f" * 40)

    assert _digest(verification=changed_head) != original
    assert _digest(artifacts=[_artifact(digest_char="0")]) != original
    assert _digest(artifacts=[_artifact(name="different-result.json")]) != original
