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


# ---------------------------------------------------------------------------
# Additional coverage for previously-uncovered branches
# ---------------------------------------------------------------------------


def test_inline_max_bytes_default_when_env_unset(monkeypatch):
    """inline_max_bytes returns the default constant when the env var is absent."""
    monkeypatch.delenv(evidence_blobs.INLINE_MAX_ENV, raising=False)
    assert evidence_blobs.inline_max_bytes() == evidence_blobs.DEFAULT_INLINE_MAX_BYTES


def test_inline_max_bytes_default_on_non_integer_value(monkeypatch):
    """inline_max_bytes falls back to the default when the env var is not a valid integer."""
    monkeypatch.setenv(evidence_blobs.INLINE_MAX_ENV, "not-a-number")
    assert evidence_blobs.inline_max_bytes() == evidence_blobs.DEFAULT_INLINE_MAX_BYTES


def test_digest_hex_raises_on_invalid_digest():
    """_digest_hex raises ValueError for strings that are not 64 hex chars."""
    with pytest.raises(ValueError, match="invalid sha256 digest"):
        evidence_blobs._digest_hex("tooshort")


def test_read_blob_raises_on_wrong_uri_scheme(tmp_path):
    """read_blob raises ValueError when the URI does not start with 'evidence-blob:'."""
    with pytest.raises(ValueError, match="unsupported evidence blob uri"):
        evidence_blobs.read_blob(tmp_path, "file:///tmp/something")


def test_read_blob_raises_on_missing_sha256_prefix(tmp_path):
    """read_blob raises ValueError when the scheme segment is not 'sha256/'."""
    with pytest.raises(ValueError, match="unsupported evidence blob uri"):
        evidence_blobs.read_blob(tmp_path, "evidence-blob:md5/abc123")


def test_read_blob_raises_on_expected_sha256_mismatch(tmp_path):
    """read_blob raises BlobIntegrityError when expected_sha256 differs from the stored digest."""
    content = b"correct payload"
    uri = evidence_blobs.store_blob(tmp_path, content)
    wrong_digest = "a" * 64  # valid hex but wrong digest
    with pytest.raises(evidence_blobs.BlobIntegrityError):
        evidence_blobs.read_blob(tmp_path, uri, expected_sha256=wrong_digest)


def test_store_blob_cleanup_on_write_error(tmp_path, monkeypatch):
    """store_blob removes the temp file when an error occurs during write."""
    import os as _os

    original_fdopen = _os.fdopen
    call_count = {"n": 0}

    def failing_fdopen(fd, mode):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("simulated write failure")
        return original_fdopen(fd, mode)

    monkeypatch.setattr(_os, "fdopen", failing_fdopen)
    content = b"payload that will fail"
    with pytest.raises(OSError):
        evidence_blobs.store_blob(tmp_path, content)
    # No temp .blob- files should remain in the parent directory
    fanout = tmp_path / hashlib.sha256(content).hexdigest()[:2]
    leftover = list(fanout.glob(".blob-*")) if fanout.exists() else []
    assert leftover == [], "Temp file was not cleaned up after write error"
