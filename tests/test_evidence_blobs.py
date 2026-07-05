"""Evidence artifact byte externalization (mac.evidence_blobs).

The ledger row keeps digest/size/metadata + content_uri; bytes live in the
hub-local content-addressed blob store. Reads materialize content through the
existing secret-scoped path so callers see the identical response shape.
"""
from __future__ import annotations

import base64
import hashlib

import pytest

from mac import evidence_blobs
from mac.models import ValidationError
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _artifact(content: bytes, name: str = "stdout.txt") -> dict:
    return {
        "name": name,
        "artifact_type": "stdout",
        "source_uri": "file:///tmp/%s" % name,
        "content_type": "text/plain; charset=utf-8",
        "encoding": "base64",
        "size_bytes": len(content),
        "sha256": "sha256:%s" % hashlib.sha256(content).hexdigest(),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def test_store_and_read_blob_roundtrip(tmp_path):
    content = b"artifact payload " * 100
    uri = evidence_blobs.store_blob(tmp_path, content)
    assert uri.startswith("evidence-blob:sha256/")
    # Idempotent: same content, same uri, no error.
    assert evidence_blobs.store_blob(tmp_path, content) == uri
    assert evidence_blobs.read_blob(tmp_path, uri) == content


def test_store_blob_sets_mode_0600(tmp_path):
    """store_blob must leave the blob file at mode 0600 (owner-read/write only).

    This asserts the documented security guarantee: evidence payloads are not
    readable by other local users on the hub host.
    """
    content = b"sensitive evidence payload"
    uri = evidence_blobs.store_blob(tmp_path, content)
    digest = hashlib.sha256(content).hexdigest()
    path = evidence_blobs.blob_path(tmp_path, digest)
    actual_mode = path.stat().st_mode & 0o777
    assert actual_mode == 0o600, (
        "Expected blob file mode 0600, got %04o. "
        "evidence_blobs.store_blob must call chmod(0o600) after writing." % actual_mode
    )


def test_read_blob_fails_closed_on_corruption(tmp_path):
    content = b"payload"
    uri = evidence_blobs.store_blob(tmp_path, content)
    digest = hashlib.sha256(content).hexdigest()
    path = evidence_blobs.blob_path(tmp_path, digest)
    path.chmod(0o600)
    path.write_bytes(b"tampered")
    with pytest.raises(evidence_blobs.BlobIntegrityError):
        evidence_blobs.read_blob(tmp_path, uri)


def test_large_artifact_externalizes_and_reads_back(cp, tmp_path, monkeypatch):
    monkeypatch.setenv(evidence_blobs.BLOB_DIR_ENV, str(tmp_path / "blobs"))
    monkeypatch.setenv(evidence_blobs.INLINE_MAX_ENV, "64")
    task = cp.create_task("externalized artifact")
    content = b"x" * 4096

    evidence = cp.add_evidence(
        task.id, "log", "file:///tmp/result.json", "done", "agent",
        artifacts=[_artifact(content)],
    )

    # Ledger row carries the uri, not the bytes.
    row = cp.store.query_one(
        "SELECT content_base64, content_uri, size_bytes FROM evidence_artifacts "
        "WHERE evidence_id = ?",
        (evidence.id,),
    )
    assert row["content_base64"] == ""
    assert row["content_uri"].startswith("evidence-blob:sha256/")
    assert int(row["size_bytes"]) == len(content)

    # The secret-scoped read materializes identical content transparently.
    listed = cp.list_evidence_artifacts(evidence.id)
    artifact = cp.get_evidence_artifact(evidence.id, listed[0]["id"])
    assert base64.b64decode(artifact["content_base64"]) == content


def test_small_artifact_stays_inline(cp, tmp_path, monkeypatch):
    monkeypatch.setenv(evidence_blobs.BLOB_DIR_ENV, str(tmp_path / "blobs"))
    monkeypatch.setenv(evidence_blobs.INLINE_MAX_ENV, "4096")
    task = cp.create_task("inline artifact")
    content = b"short output"

    evidence = cp.add_evidence(
        task.id, "log", "file:///tmp/result.json", "done", "agent",
        artifacts=[_artifact(content)],
    )
    row = cp.store.query_one(
        "SELECT content_base64, content_uri FROM evidence_artifacts WHERE evidence_id = ?",
        (evidence.id,),
    )
    assert base64.b64decode(row["content_base64"]) == content
    assert row["content_uri"] == ""


def test_unconfigured_blob_store_keeps_inline_behavior(cp, monkeypatch):
    monkeypatch.delenv(evidence_blobs.BLOB_DIR_ENV, raising=False)
    task = cp.create_task("default inline")
    content = b"y" * 200000

    evidence = cp.add_evidence(
        task.id, "log", "file:///tmp/result.json", "done", "agent",
        artifacts=[_artifact(content)],
    )
    listed = cp.list_evidence_artifacts(evidence.id)
    artifact = cp.get_evidence_artifact(evidence.id, listed[0]["id"])
    assert base64.b64decode(artifact["content_base64"]) == content


def test_missing_blob_fails_closed_with_clear_error(cp, tmp_path, monkeypatch):
    monkeypatch.setenv(evidence_blobs.BLOB_DIR_ENV, str(tmp_path / "blobs"))
    monkeypatch.setenv(evidence_blobs.INLINE_MAX_ENV, "1")
    task = cp.create_task("missing blob")
    content = b"z" * 1024
    evidence = cp.add_evidence(
        task.id, "log", "file:///tmp/result.json", "done", "agent",
        artifacts=[_artifact(content)],
    )
    listed = cp.list_evidence_artifacts(evidence.id)
    digest = hashlib.sha256(content).hexdigest()
    evidence_blobs.blob_path(tmp_path / "blobs", digest).unlink()

    from mac.models import NotFoundError

    with pytest.raises(NotFoundError, match="blob is missing"):
        cp.get_evidence_artifact(evidence.id, listed[0]["id"])


def test_corrupted_blob_fails_closed_on_read(cp, tmp_path, monkeypatch):
    monkeypatch.setenv(evidence_blobs.BLOB_DIR_ENV, str(tmp_path / "blobs"))
    monkeypatch.setenv(evidence_blobs.INLINE_MAX_ENV, "1")
    task = cp.create_task("corrupted blob")
    content = b"w" * 1024
    evidence = cp.add_evidence(
        task.id, "log", "file:///tmp/result.json", "done", "agent",
        artifacts=[_artifact(content)],
    )
    listed = cp.list_evidence_artifacts(evidence.id)
    digest = hashlib.sha256(content).hexdigest()
    path = evidence_blobs.blob_path(tmp_path / "blobs", digest)
    path.chmod(0o600)
    path.write_bytes(b"not the payload")

    with pytest.raises(ValidationError):
        cp.get_evidence_artifact(evidence.id, listed[0]["id"])


def test_identical_content_across_evidence_deduplicates(cp, tmp_path, monkeypatch):
    blob_dir = tmp_path / "blobs"
    monkeypatch.setenv(evidence_blobs.BLOB_DIR_ENV, str(blob_dir))
    monkeypatch.setenv(evidence_blobs.INLINE_MAX_ENV, "1")
    content = b"shared payload " * 64
    for title in ("first", "second"):
        task = cp.create_task(title)
        cp.add_evidence(
            task.id, "log", "file:///tmp/result.json", "done", "agent",
            artifacts=[_artifact(content)],
        )
    blobs = [p for p in blob_dir.rglob("*") if p.is_file()]
    assert len(blobs) == 1
