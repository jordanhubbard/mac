"""Hub-local, independently revocable MAC client credentials.

Enrollment is intentionally a local command.  A remote client invokes it over
an already-authenticated SSH session, so no bootstrap HTTP credential is
needed.  The registry stores only SHA-256 token hashes and scoped principal
metadata.  Live token material is returned once in the enrollment manifest and
is never written to the registry or audit log.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mac import mac_paths
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple


REGISTRY_SCHEMA = "mac.client_principals.v1"
MANIFEST_SCHEMA = "mac.client_enrollment.v1"
DEFAULT_SCOPES = ("dispatch", "read", "write")
KNOWN_SCOPES = frozenset(
    {"admin", "agent", "deploy", "dispatch", "read", "roles", "secret", "workflow", "write"}
)
ELEVATED_SCOPES = frozenset({"admin", "deploy", "secret"})
_CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LOG = logging.getLogger("mac.client_principals")


class ClientPrincipalError(ValueError):
    """Raised when issuance input or registry state is invalid."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Optional[datetime] = None) -> str:
    return (value or _now()).astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ClientPrincipalError("invalid timestamp %r" % text) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def mac_home() -> Path:
    """Return the MAC home directory."""
    return mac_paths.mac_home()


def default_registry_path() -> Path:
    """Return the default client-principals registry file path."""
    configured = os.environ.get("MAC_CLIENT_PRINCIPALS_FILE")
    return Path(configured).expanduser() if configured else mac_home() / "client-principals.json"


def default_audit_path(registry_path: Path) -> Path:
    """Return the default client-principals audit log path."""
    configured = os.environ.get("MAC_CLIENT_PRINCIPALS_AUDIT_FILE")
    return (
        Path(configured).expanduser()
        if configured
        else registry_path.with_name("client-principals.audit.jsonl")
    )


def _validate_id(value: str) -> str:
    client_id = str(value or "").strip()
    if not _CLIENT_ID.fullmatch(client_id):
        raise ClientPrincipalError(
            "client id must match %s" % _CLIENT_ID.pattern
        )
    return client_id


def _validate_agent_id(value: str) -> str:
    """Validate an actor binding without changing the actor's exact identity.

    Agent ids are durable ledger identifiers rather than display names.  They
    may be longer than client-profile ids, but whitespace/control characters
    would make deployment manifests and audit records ambiguous and are never
    valid actor ids in MAC.
    """

    agent_id = str(value or "")
    if not agent_id or len(agent_id) > 256:
        raise ClientPrincipalError("agent id must contain 1 to 256 characters")
    if agent_id != agent_id.strip() or any(ord(ch) < 33 or ch.isspace() for ch in agent_id):
        raise ClientPrincipalError("agent id must not contain whitespace or control characters")
    return agent_id


def normalize_scopes(
    scopes: Optional[Iterable[str]], *, allow_elevated: bool = False
) -> List[str]:
    """Validate and normalize a set of client scopes."""
    values = sorted({str(scope).strip().lower() for scope in (scopes or DEFAULT_SCOPES) if str(scope).strip()})
    if not values:
        raise ClientPrincipalError("at least one client scope is required")
    unknown = sorted(set(values) - KNOWN_SCOPES)
    if unknown:
        raise ClientPrincipalError("unknown client scope(s): %s" % ", ".join(unknown))
    elevated = sorted(set(values) & ELEVATED_SCOPES)
    if elevated and not allow_elevated:
        raise ClientPrincipalError(
            "elevated scope(s) %s require --allow-elevated"
            % ", ".join(elevated)
        )
    return values


def _token_hash(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _assert_private_file(path: Path) -> None:
    if os.name == "nt" or not path.exists():
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ClientPrincipalError(
            "%s permissions are %04o; expected 0600 or stricter" % (path, mode)
        )


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    fd, raw_tmp = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if tmp.exists():
            tmp.unlink()


@dataclass(frozen=True)
class IssuedCredential:
    record: Dict[str, Any]
    token: str


class ClientPrincipalStore:
    """Atomic JSON registry plus secret-free append-only audit trail."""

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        audit_path: Optional[Path] = None,
    ) -> None:
        self.path = Path(path or default_registry_path()).expanduser()
        self.audit_path = Path(audit_path or default_audit_path(self.path)).expanduser()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        _ensure_private_dir(self.path.parent)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                os.fchmod(descriptor, 0o600)
            except OSError:
                pass
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - supported targets are POSIX
                pass
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover
                pass
            os.close(descriptor)

    def _read_unlocked(self, *, require_private: bool = True) -> Dict[str, Any]:
        if not self.path.exists():
            return {"schema": REGISTRY_SCHEMA, "clients": {}}
        if require_private:
            _assert_private_file(self.path)
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ClientPrincipalError("could not read %s: %s" % (self.path, exc)) from exc
        if not isinstance(value, dict) or value.get("schema") != REGISTRY_SCHEMA:
            raise ClientPrincipalError("%s is not a %s registry" % (self.path, REGISTRY_SCHEMA))
        clients = value.get("clients")
        if not isinstance(clients, dict):
            raise ClientPrincipalError("%s clients must be an object" % self.path)
        return value

    def read(self) -> Dict[str, Any]:
        with self._lock():
            return self._read_unlocked()

    def _audit(self, event: str, record: Mapping[str, Any], *, actor: str) -> None:
        _ensure_private_dir(self.audit_path.parent)
        entry = {
            "schema": "mac.client_principal_audit.v1",
            "event": event,
            "client_id": record.get("id"),
            "principal_kind": record.get("principal_kind") or "client",
            "agent_id": record.get("agent_id"),
            "credential_version": record.get("credential_version"),
            "fleet": record.get("fleet"),
            "scopes": list(record.get("scopes") or []),
            "actor": str(actor or "operator"),
            "at": _timestamp(),
        }
        descriptor = os.open(
            self.audit_path,
            os.O_CREAT | os.O_APPEND | os.O_WRONLY,
            0o600,
        )
        try:
            try:
                os.fchmod(descriptor, 0o600)
            except OSError:
                pass
            raw = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
            os.write(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _issue_locked(
        self,
        registry: Dict[str, Any],
        client_id: str,
        *,
        display_name: str,
        fleet: str,
        profile: str,
        scopes: Iterable[str],
        expires_in: int,
        api_url: str,
        ssh_host_key_fingerprint: str,
        ssh_host_ca: str,
        capabilities: Iterable[str],
        agent_id: str = "",
        principal_kind: str = "client",
        credential_metadata: Optional[Mapping[str, Any]] = None,
        token_prefix: str = "mac_client_",
        actor: str,
        event: str,
    ) -> IssuedCredential:
        now = _now()
        bound_agent_id = _validate_agent_id(agent_id) if agent_id else ""
        normalized_kind = str(principal_kind or "client").strip().lower()
        if normalized_kind not in {"client", "worker"}:
            raise ClientPrincipalError("principal kind must be client or worker")
        if normalized_kind == "worker" and not bound_agent_id:
            raise ClientPrincipalError("worker principal requires an exact agent id binding")
        if token_prefix not in {"mac_client_", "mac_worker_"}:
            raise ClientPrincipalError("unsupported credential token prefix")
        token = token_prefix + secrets.token_urlsafe(32)
        clients = registry["clients"]
        previous = clients.get(client_id) if isinstance(clients, dict) else None
        version = int((previous or {}).get("credential_version") or 0) + 1
        record = {
            "id": client_id,
            "display_name": display_name or client_id,
            "fleet": str(fleet or "").strip(),
            "profile": str(profile or client_id).strip(),
            "token_hash": _token_hash(token),
            "token_fingerprint": _token_fingerprint(token),
            "scopes": sorted(set(scopes)),
            "issued_at": _timestamp(now),
            "expires_at": _timestamp(now + timedelta(seconds=expires_in)),
            "revoked_at": None,
            "credential_version": version,
            "api_url": str(api_url or "http://127.0.0.1:8789").strip(),
            "ssh_host_key_fingerprint": str(ssh_host_key_fingerprint or "").strip(),
            "ssh_host_ca": str(ssh_host_ca or "").strip(),
            "capabilities": sorted(
                {str(item).strip() for item in capabilities if str(item).strip()}
            ),
            "principal_kind": normalized_kind,
        }
        if bound_agent_id:
            record["agent_id"] = bound_agent_id
        if credential_metadata:
            # Metadata is deliberately secret-free lifecycle state.  Callers
            # must never place bearer material here: unlike ``token`` it is
            # persisted in the hub registry and copied into audit/readiness
            # views.
            record["credential_metadata"] = json.loads(
                json.dumps(dict(credential_metadata), sort_keys=True)
            )
        clients[client_id] = record
        registry["updated_at"] = _timestamp(now)
        _atomic_json(self.path, registry)
        self._audit(event, record, actor=actor)
        return IssuedCredential(record=dict(record), token=token)

    def enroll(
        self,
        client_id: str,
        *,
        display_name: str = "",
        fleet: str = "",
        profile: str = "",
        scopes: Optional[Iterable[str]] = None,
        expires_in: int = 30 * 24 * 60 * 60,
        api_url: str = "http://127.0.0.1:8789",
        ssh_host_key_fingerprint: str = "",
        ssh_host_ca: str = "",
        capabilities: Iterable[str] = (),
        agent_id: str = "",
        principal_kind: str = "client",
        credential_metadata: Optional[Mapping[str, Any]] = None,
        token_prefix: str = "mac_client_",
        allow_elevated: bool = False,
        rotate: bool = False,
        actor: str = "operator",
    ) -> IssuedCredential:
        client_id = _validate_id(client_id)
        if int(expires_in) < 60:
            raise ClientPrincipalError("expires-in must be at least 60 seconds")
        normalized = normalize_scopes(scopes, allow_elevated=allow_elevated)
        with self._lock():
            registry = self._read_unlocked()
            existing = registry["clients"].get(client_id)
            if existing and not rotate:
                raise ClientPrincipalError(
                    "client %r already exists; use `mac admin client renew` or --rotate"
                    % client_id
                )
            return self._issue_locked(
                registry,
                client_id,
                display_name=display_name,
                fleet=fleet,
                profile=profile,
                scopes=normalized,
                expires_in=int(expires_in),
                api_url=api_url,
                ssh_host_key_fingerprint=ssh_host_key_fingerprint,
                ssh_host_ca=ssh_host_ca,
                capabilities=capabilities,
                agent_id=agent_id,
                principal_kind=principal_kind,
                credential_metadata=credential_metadata,
                token_prefix=token_prefix,
                actor=actor,
                event="client.rotated" if existing else "client.enrolled",
            )

    def renew(
        self,
        client_id: str,
        *,
        expires_in: int = 30 * 24 * 60 * 60,
        actor: str = "operator",
    ) -> IssuedCredential:
        client_id = _validate_id(client_id)
        if int(expires_in) < 60:
            raise ClientPrincipalError("expires-in must be at least 60 seconds")
        with self._lock():
            registry = self._read_unlocked()
            existing = registry["clients"].get(client_id)
            if not isinstance(existing, dict):
                raise ClientPrincipalError("client %r does not exist" % client_id)
            if existing.get("revoked_at"):
                raise ClientPrincipalError(
                    "client %r is revoked; enroll a new client identity" % client_id
                )
            return self._issue_locked(
                registry,
                client_id,
                display_name=str(existing.get("display_name") or client_id),
                fleet=str(existing.get("fleet") or ""),
                profile=str(existing.get("profile") or client_id),
                scopes=list(existing.get("scopes") or DEFAULT_SCOPES),
                expires_in=int(expires_in),
                api_url=str(existing.get("api_url") or "http://127.0.0.1:8789"),
                ssh_host_key_fingerprint=str(existing.get("ssh_host_key_fingerprint") or ""),
                ssh_host_ca=str(existing.get("ssh_host_ca") or ""),
                capabilities=list(existing.get("capabilities") or []),
                agent_id=str(existing.get("agent_id") or ""),
                principal_kind=str(existing.get("principal_kind") or "client"),
                credential_metadata=(
                    existing.get("credential_metadata")
                    if isinstance(existing.get("credential_metadata"), Mapping)
                    else None
                ),
                token_prefix=(
                    "mac_worker_"
                    if existing.get("principal_kind") == "worker"
                    else "mac_client_"
                ),
                actor=actor,
                event="client.renewed",
            )

    def revoke(self, client_id: str, *, actor: str = "operator") -> Dict[str, Any]:
        client_id = _validate_id(client_id)
        with self._lock():
            registry = self._read_unlocked()
            record = registry["clients"].get(client_id)
            if not isinstance(record, dict):
                raise ClientPrincipalError("client %r does not exist" % client_id)
            if not record.get("revoked_at"):
                record["revoked_at"] = _timestamp()
                registry["updated_at"] = record["revoked_at"]
                _atomic_json(self.path, registry)
                self._audit("client.revoked", record, actor=actor)
            return safe_record(record)

    def list(self) -> List[Dict[str, Any]]:
        registry = self.read()
        return [
            safe_record(record)
            for _client_id, record in sorted(registry["clients"].items())
            if isinstance(record, dict)
        ]


def safe_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Return operator metadata without the full stored token hash."""

    return {
        key: value
        for key, value in record.items()
        if key != "token_hash" and value not in (None, "")
    }


def enrollment_manifest(issued: IssuedCredential) -> Dict[str, Any]:
    """Build a client enrollment manifest from an issued credential."""
    record = issued.record
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "client_id": record["id"],
        "display_name": record["display_name"],
        "profile": record["profile"],
        "fleet": record["fleet"],
        "connection": {"api_url": record["api_url"]},
        "ssh": {
            key: value
            for key, value in {
                "host_key_fingerprint": record.get("ssh_host_key_fingerprint"),
                "host_ca": record.get("ssh_host_ca"),
            }.items()
            if value
        },
        "credential": {
            "id": "%s.v%d" % (record["id"], record["credential_version"]),
            "token": issued.token,
            "scopes": list(record["scopes"]),
            "issued_at": record["issued_at"],
            "expires_at": record["expires_at"],
        },
        "capabilities": list(record.get("capabilities") or []),
    }
    if record.get("agent_id"):
        manifest["principal"] = {
            "kind": record.get("principal_kind") or "worker",
            "agent_id": record["agent_id"],
        }
    return manifest


def _active_mapping_from_registry(
    registry: Mapping[str, Any], *, now: Optional[datetime] = None
) -> Dict[str, Dict[str, Any]]:
    instant = (now or _now()).astimezone(timezone.utc)
    result: Dict[str, Dict[str, Any]] = {}
    clients = registry.get("clients") if isinstance(registry, Mapping) else None
    if not isinstance(clients, Mapping):
        return result
    for record in clients.values():
        if not isinstance(record, Mapping) or record.get("revoked_at"):
            continue
        try:
            expires_at = _parse_timestamp(record.get("expires_at"))
        except ClientPrincipalError:
            continue
        if expires_at is None or expires_at <= instant:
            continue
        token_hash = str(record.get("token_hash") or "")
        scopes = [str(scope) for scope in record.get("scopes") or []]
        if not token_hash.startswith("sha256:") or not scopes:
            continue
        result[token_hash] = {
            "scopes": scopes,
            "client_id": str(record.get("id") or "") or None,
        }
    return result


class ClientPrincipalProvider:
    """Mtime-cached registry reader; expiry/revocation is evaluated per request."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or default_registry_path()).expanduser()
        self._signature: Optional[Tuple[int, int, int, int]] = None
        self._registry: Dict[str, Any] = {"schema": REGISTRY_SCHEMA, "clients": {}}
        self._lock = threading.Lock()

    def _file_signature(self) -> Optional[Tuple[int, int, int, int]]:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_ino, stat.st_mtime_ns, stat.st_size, stat.st_mode & 0o777)

    def tokens(self, *, now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
        signature = self._file_signature()
        with self._lock:
            if signature != self._signature:
                if signature is None:
                    self._registry = {"schema": REGISTRY_SCHEMA, "clients": {}}
                else:
                    try:
                        self._registry = ClientPrincipalStore(self.path)._read_unlocked()
                    except ClientPrincipalError as exc:
                        # Fail closed for dynamic clients while keeping static
                        # admin recovery tokens usable.
                        _LOG.error("ignoring invalid client-principal registry: %s", exc)
                        self._registry = {"schema": REGISTRY_SCHEMA, "clients": {}}
                self._signature = signature
            return _active_mapping_from_registry(self._registry, now=now)
