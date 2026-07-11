"""Hub-local content-addressed blob store for evidence artifact bytes.

Evidence artifacts historically rode inside the ledger as base64 rows
(``evidence_artifacts.content_base64``), which couples ledger database growth
to artifact volume — every stdout capture and build product inflates the same
SQLite/Postgres store that coordinates the fleet. This module externalizes the
BYTES while the ledger keeps what it is actually authoritative for: the
digest, size, metadata, and a ``content_uri`` pointing here.

Design constraints:

- **Opt-in.** With ``MAC_EVIDENCE_BLOB_DIR`` unset the ledger inlines bytes
  exactly as before. The fleet deploy sets it on the hub; standalone/dev
  hubs keep the zero-config behavior.
- **Security model unchanged.** Blobs live in a private hub directory, NOT
  under the public WebDAV publish root. The only read path is the existing
  secret-scoped ``GET /evidence/{id}/artifacts/{artifact_id}`` endpoint,
  which materializes ``content_base64`` from the blob transparently, so
  callers cannot tell (and do not care) where the bytes live.
- **Content-addressed + verified.** Blobs are stored at
  ``<root>/<aa>/<sha256hex>`` (fanout on the first two hex chars), written
  atomically (tmp + rename), deduplicated by digest, and re-hashed on read —
  a corrupted or substituted blob fails closed instead of returning wrong
  bytes under a valid-looking digest.
- **Append-only.** Ledger rows CASCADE-delete with their task; blobs are
  deduplicated across rows so deletion needs refcounting the ledger does not
  carry. Operators reclaim space with an age-based sweep of the blob root
  (files are never rewritten, so mtime is trustworthy).
- **File permissions.** Every blob file is stored with mode 0600
  (owner-read/write only) to prevent other local users from reading
  evidence payloads. ``store_blob`` applies ``chmod(0o600)`` to the temp
  path before the atomic rename, eliminating the TOCTOU window between
  rename and a post-rename chmod. The post-rename ``path.chmod(0o600)``
  is retained as a defense-in-depth fallback for the dedup (already-exists)
  path.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Optional

BLOB_DIR_ENV = "MAC_EVIDENCE_BLOB_DIR"
INLINE_MAX_ENV = "MAC_EVIDENCE_INLINE_MAX_BYTES"

# Bytes at or below this stay inline in the ledger row even when a blob root
# is configured: tiny payloads (result JSON, short logs) are cheaper to keep
# next to their metadata than to fan out as files.
DEFAULT_INLINE_MAX_BYTES = 65536

URI_SCHEME = "evidence-blob:"


class BlobIntegrityError(RuntimeError):
    """Stored blob bytes no longer match the digest recorded in the ledger."""


def blob_root(environ: Optional[dict] = None) -> Optional[Path]:
    env = os.environ if environ is None else environ
    raw = (env.get(BLOB_DIR_ENV) or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def inline_max_bytes(environ: Optional[dict] = None) -> int:
    env = os.environ if environ is None else environ
    raw = (env.get(INLINE_MAX_ENV) or "").strip()
    if not raw:
        return DEFAULT_INLINE_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_INLINE_MAX_BYTES
    return max(0, value)


def _digest_hex(digest: str) -> str:
    """Normalize a ``sha256:<hex>`` or bare-hex digest to lowercase hex."""
    value = str(digest or "").strip().lower()
    if value.startswith("sha256:"):
        value = value[len("sha256:") :]
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("invalid sha256 digest: %r" % digest)
    return value


def blob_path(root: Path, digest: str) -> Path:
    hex_digest = _digest_hex(digest)
    return root / hex_digest[:2] / hex_digest


def blob_uri(digest: str) -> str:
    """Location-independent URI recorded in the ledger.

    Deliberately NOT a ``file://`` path: the blob root can move (or the
    ledger can be restored on a standby hub with a different layout) without
    invalidating every row. Readers resolve the digest against the currently
    configured root.
    """
    return URI_SCHEME + "sha256/" + _digest_hex(digest)


def store_blob(root: Path, content: bytes) -> str:
    """Write ``content`` into the store; returns the ledger ``content_uri``.

    Idempotent per digest: an existing blob is trusted (content-addressed
    names cannot collide across different bytes) and not rewritten.
    """
    digest = hashlib.sha256(content).hexdigest()
    path = blob_path(root, digest)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".blob-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            Path(tmp_name).chmod(0o600)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return blob_uri(digest)


def read_blob(root: Path, uri: str, *, expected_sha256: Optional[str] = None) -> bytes:
    """Read and verify a blob referenced by a ledger ``content_uri``.

    Raises ``FileNotFoundError`` when the blob is missing and
    ``BlobIntegrityError`` when the bytes do not match the digest — never
    returns unverified content.
    """
    value = str(uri or "").strip()
    if not value.startswith(URI_SCHEME):
        raise ValueError("unsupported evidence blob uri: %r" % uri)
    remainder = value[len(URI_SCHEME) :]
    if not remainder.startswith("sha256/"):
        raise ValueError("unsupported evidence blob uri: %r" % uri)
    digest = remainder[len("sha256/") :]
    path = blob_path(root, digest)
    content = path.read_bytes()
    actual = hashlib.sha256(content).hexdigest()
    if actual != _digest_hex(digest):
        raise BlobIntegrityError(
            "evidence blob %s does not match its digest (got sha256:%s)" % (path, actual)
        )
    if expected_sha256:
        if _digest_hex(expected_sha256) != actual:
            raise BlobIntegrityError(
                "evidence blob %s does not match the ledger digest %s" % (path, expected_sha256)
            )
    return content
