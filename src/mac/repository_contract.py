"""Shared primitives for repository evidence and remote-ref contracts.

The project repository registry has two historical remote fields.  New
registrations carry the authoritative endpoint in
``metadata.repository_contract.canonical_remote_url``; old registrations only
have ``source``.  Managed work must never choose between those fields locally:
doing so can make planning inspect one repository while integration or landing
publishes to another.  :func:`resolve_repository_canonical_remote` is the one
selection and validation boundary for controller-owned Git operations.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from mac.gitops import validate_git_remote_url


_SAFE_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,127}$")
_SCP_REMOTE_RE = re.compile(
    r"^(?:(?P<user>[^@/:]+)@)?(?P<host>[^@/:]+):(?P<path>.+)$"
)


@dataclass(frozen=True)
class CanonicalRepositoryRemote:
    """One validated, secret-free canonical endpoint and comparison identity."""

    repository_id: str
    url: str
    identity: str
    source_kind: str


def validate_secret_free_git_remote(value: Any) -> str:
    """Validate a Git endpoint that is safe to persist and pass to Git.

    SSH usernames such as ``git`` are transport identities, not credentials,
    and remain valid.  Passwords, HTTP userinfo, query credentials, fragments,
    control characters, and SCP-style ``user:password@host:path`` spellings
    fail closed.  Controller credential injection happens out of band.
    """

    remote = str(value or "").strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in remote):
        raise ValueError("canonical Git remote contains control characters")
    validate_git_remote_url(remote)

    if "://" in remote:
        parsed = urlsplit(remote)
        if parsed.password is not None:
            raise ValueError("canonical Git remote must not embed a password")
        if parsed.scheme in {"http", "https", "git", "file"} and parsed.username:
            raise ValueError("canonical Git remote must not embed userinfo")
        if parsed.fragment:
            raise ValueError("canonical Git remote must not contain a fragment")
        if parsed.query:
            raise ValueError("canonical Git remote must not contain query data")
    else:
        before_at, separator, _ = remote.partition("@")
        if separator and ":" in before_at:
            raise ValueError("canonical Git remote must not embed SCP credentials")
        if "?" in remote or "#" in remote:
            raise ValueError("canonical Git remote must not contain query or fragment data")
    return remote


def canonical_git_remote_identity(value: Any) -> str:
    """Return a stable secret-free identity for an already validated endpoint."""

    remote = validate_secret_free_git_remote(value)
    if remote.startswith("/"):
        return "path:%s" % Path(remote).expanduser().resolve()
    if "://" in remote:
        parsed = urlsplit(remote)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        if scheme == "file":
            return "file:%s" % Path(parsed.path).expanduser().resolve()
        if not host:
            raise ValueError("canonical Git remote has no host")
        port = parsed.port
        if (scheme == "ssh" and port == 22) or (
            scheme == "https" and port == 443
        ) or (scheme in {"http", "git"} and port in {80, 9418}):
            port = None
        host_identity = "%s:%d" % (host, port) if port is not None else host
        path = parsed.path.strip("/")
    else:
        match = _SCP_REMOTE_RE.fullmatch(remote)
        if match is None:
            raise ValueError("canonical Git remote identity is not parseable")
        host_identity = str(match.group("host") or "").lower()
        path = str(match.group("path") or "").strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host_identity or not path:
        raise ValueError("canonical Git remote identity is incomplete")
    return "%s/%s" % (host_identity, path)


def resolve_repository_canonical_remote(
    repository: Mapping[str, Any],
    *,
    allow_legacy_source: bool = True,
) -> CanonicalRepositoryRemote:
    """Resolve one registry row to its authoritative canonical Git remote.

    Presence of ``repository_contract`` makes that contract authoritative.  A
    malformed contract or one without ``canonical_remote_url`` never falls
    through to ``source``.  ``source`` is accepted only for a truly legacy row
    with no contract key at all.
    """

    row = dict(repository)
    repository_id = str(row.get("id") or row.get("repository_id") or "").strip()
    if not repository_id:
        raise ValueError("canonical repository identity is missing")
    raw_metadata = row.get("metadata")
    if raw_metadata in (None, ""):
        metadata: Mapping[str, Any] = {}
    elif isinstance(raw_metadata, Mapping):
        metadata = raw_metadata
    elif isinstance(raw_metadata, str):
        try:
            decoded = json.loads(raw_metadata)
        except (TypeError, ValueError) as exc:
            raise ValueError("canonical repository metadata is malformed") from exc
        if not isinstance(decoded, Mapping):
            raise ValueError("canonical repository metadata is malformed")
        metadata = decoded
    else:
        raise ValueError("canonical repository metadata is malformed")

    if "repository_contract" in metadata:
        contract = metadata.get("repository_contract")
        if not isinstance(contract, Mapping):
            raise ValueError("canonical repository contract is malformed")
        remote = str(contract.get("canonical_remote_url") or "").strip()
        if not remote:
            raise ValueError("canonical repository contract has no canonical remote")
        source_kind = "repository_contract"
    else:
        if not allow_legacy_source:
            raise ValueError("canonical repository contract is required")
        remote = str(row.get("source") or "").strip()
        if not remote:
            raise ValueError("legacy repository has no canonical source")
        source_kind = "legacy_source"

    url = validate_secret_free_git_remote(remote)
    return CanonicalRepositoryRemote(
        repository_id=repository_id,
        url=url,
        identity=canonical_git_remote_identity(url),
        source_kind=source_kind,
    )


def normalize_repo_relative_path(value: Any) -> str:
    """Return the canonical slash-separated form used in repository evidence."""

    path = str(value or "").strip().replace("\\", "/")
    path = re.sub(r"/+", "/", path)
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/")


def repo_path_satisfies_requirement(changed_path: str, required_path: str) -> bool:
    """Match one changed path against an exact or glob evidence requirement."""

    changed = normalize_repo_relative_path(changed_path)
    required = normalize_repo_relative_path(required_path)
    if not changed or not required:
        return False
    if any(char in required for char in "*?["):
        return fnmatch.fnmatchcase(changed, required)
    return changed == required


def remote_branch_from_ref(remote_ref: str) -> str:
    """Extract a safe branch name from common remote-reference spellings."""

    ref = str(remote_ref or "").strip()
    if not ref:
        return ""
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix) :]
            break
    if ref.startswith("origin/"):
        ref = ref[len("origin/") :]
    if (
        ref
        and not ref.startswith("-")
        and not ref.startswith("refs/")
        and _SAFE_GIT_REF_RE.fullmatch(ref)
    ):
        return ref
    return ""
