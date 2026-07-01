"""Portable, least-privilege client profiles and credential references.

Profiles contain connection and SSH routing metadata only.  The bearer token is
stored in a separate mode-0600 credential record and is never returned by
normal list/show operations.  Credential replacement is committed before the
profile pointer, so an interrupted install leaves either the previous complete
profile or an unreferenced new credential, never a profile that points at a
missing secret.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlsplit

import yaml

from mac.client_principals import MANIFEST_SCHEMA, mac_home


PROFILE_SCHEMA = "mac.client_profile.v1"
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ALLOWED_TOP = {
    "schema",
    "client_id",
    "display_name",
    "profile",
    "fleet",
    "connection",
    "ssh",
    "credential",
    "capabilities",
}
_ALLOWED_STORED_TOP = _ALLOWED_TOP | {"installed_at"}
_ALLOWED_CONNECTION = {
    "api_url",
    "mode",
    "local_port",
    "remote_host",
    "remote_port",
}
_ALLOWED_SSH = {
    "target",
    "port",
    "proxy_jump",
    "identity_file",
    "identity_ref",
    "known_hosts_file",
    "host_key_policy",
    "host_key_fingerprint",
    "host_ca",
}
_ALLOWED_CREDENTIAL = {"id", "token", "scopes", "issued_at", "expires_at"}
_ALLOWED_STORED_CREDENTIAL = {
    "id",
    "path",
    "scopes",
    "issued_at",
    "expires_at",
}


class ClientProfileError(ValueError):
    """Raised for malformed, secret-bearing, or insecure profile input."""


def clients_root() -> Path:
    configured = os.environ.get("MAC_CLIENT_PROFILES_DIR")
    return Path(configured).expanduser() if configured else mac_home() / "clients"


def credentials_root() -> Path:
    configured = os.environ.get("MAC_CLIENT_CREDENTIALS_DIR")
    return (
        Path(configured).expanduser()
        if configured
        else mac_home() / "credentials" / "clients"
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _name(value: Any, *, field: str = "profile") -> str:
    text = str(value or "").strip()
    if not _PROFILE_NAME.fullmatch(text):
        raise ClientProfileError("%s must match %s" % (field, _PROFILE_NAME.pattern))
    return text


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _assert_private(path: Path) -> None:
    if os.name == "nt" or not path.exists():
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ClientProfileError(
            "%s permissions are %04o; expected 0600 or stricter" % (path, mode)
        )


def _atomic_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    _ensure_dir(path.parent)
    descriptor, raw_tmp = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    tmp = Path(raw_tmp)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            path.chmod(mode)
        except OSError:
            pass
    finally:
        if tmp.exists():
            tmp.unlink()


def _mapping(value: Any, *, field: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ClientProfileError("%s must be an object" % field)
    return dict(value)


def _reject_unknown(value: Mapping[str, Any], allowed: set, *, field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ClientProfileError(
            "%s contains unsupported field(s): %s" % (field, ", ".join(unknown))
        )


def _validate_api_url(value: Any) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ClientProfileError("connection.api_url must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ClientProfileError(
            "connection.api_url must not contain credentials, query parameters, or fragments"
        )
    return text.rstrip("/")


def _optional_port(value: Any, *, field: str) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ClientProfileError("%s must be an integer" % field) from exc
    if port <= 0 or port > 65535:
        raise ClientProfileError("%s must be between 1 and 65535" % field)
    return port


def _credential_filename(credential_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", credential_id).strip("._-") or "credential"
    digest = hashlib.sha256(credential_id.encode("utf-8")).hexdigest()[:12]
    return "%s-%s.token" % (safe[:48], digest)


def validate_enrollment_manifest(
    raw: Mapping[str, Any], *, profile_override: Optional[str] = None
) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ClientProfileError("enrollment manifest must be an object")
    manifest = dict(raw)
    _reject_unknown(manifest, _ALLOWED_TOP, field="manifest")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ClientProfileError("manifest schema must be %s" % MANIFEST_SCHEMA)

    profile_name = _name(profile_override or manifest.get("profile"))
    client_id = _name(manifest.get("client_id"), field="client_id")
    connection = _mapping(manifest.get("connection"), field="connection")
    ssh = _mapping(manifest.get("ssh"), field="ssh")
    credential = _mapping(manifest.get("credential"), field="credential")
    _reject_unknown(connection, _ALLOWED_CONNECTION, field="connection")
    _reject_unknown(ssh, _ALLOWED_SSH, field="ssh")
    _reject_unknown(credential, _ALLOWED_CREDENTIAL, field="credential")

    api_url = _validate_api_url(connection.get("api_url"))
    mode = str(connection.get("mode") or ("ssh-tunnel" if ssh.get("target") else "direct")).strip()
    if mode not in {"direct", "ssh-tunnel"}:
        raise ClientProfileError("connection.mode must be direct or ssh-tunnel")
    if mode == "ssh-tunnel" and not str(ssh.get("target") or "").strip():
        raise ClientProfileError("ssh-tunnel profiles require ssh.target")

    policy = str(ssh.get("host_key_policy") or "strict").strip().lower()
    if policy not in {"strict", "accept-new", "insecure"}:
        raise ClientProfileError("ssh.host_key_policy is invalid")
    if mode == "ssh-tunnel" and policy == "strict" and not (
        ssh.get("known_hosts_file") or ssh.get("host_key_fingerprint") or ssh.get("host_ca")
    ):
        raise ClientProfileError(
            "strict SSH profiles require known_hosts_file, host_key_fingerprint, or host_ca"
        )

    token = str(credential.get("token") or "")
    if len(token) < 20 or any(char.isspace() for char in token):
        raise ClientProfileError("credential.token is missing or malformed")
    credential_id = str(credential.get("id") or "").strip()
    if not credential_id:
        raise ClientProfileError("credential.id is required")
    scopes = sorted(
        {str(scope).strip() for scope in credential.get("scopes") or [] if str(scope).strip()}
    )
    if not scopes:
        raise ClientProfileError("credential.scopes must be non-empty")

    normalized_connection: Dict[str, Any] = {"api_url": api_url, "mode": mode}
    for key in ("local_port", "remote_port"):
        port = _optional_port(connection.get(key), field="connection.%s" % key)
        if port is not None:
            normalized_connection[key] = port
    if connection.get("remote_host"):
        normalized_connection["remote_host"] = str(connection["remote_host"]).strip()

    normalized_ssh: Dict[str, Any] = {}
    for key in _ALLOWED_SSH:
        value = ssh.get(key)
        if value not in (None, ""):
            normalized_ssh[key] = value
    if normalized_ssh:
        normalized_ssh["host_key_policy"] = policy
        port = _optional_port(normalized_ssh.get("port"), field="ssh.port")
        if port is None:
            normalized_ssh.pop("port", None)
        else:
            normalized_ssh["port"] = port

    return {
        "profile_name": profile_name,
        "token": token,
        "profile": {
            "schema": PROFILE_SCHEMA,
            "profile": profile_name,
            "client_id": client_id,
            "display_name": str(manifest.get("display_name") or client_id).strip(),
            "fleet": str(manifest.get("fleet") or "").strip(),
            "connection": normalized_connection,
            "ssh": normalized_ssh,
            "credential": {
                "id": credential_id,
                "scopes": scopes,
                "issued_at": str(credential.get("issued_at") or "").strip(),
                "expires_at": str(credential.get("expires_at") or "").strip(),
            },
            "capabilities": sorted(
                {str(item).strip() for item in manifest.get("capabilities") or [] if str(item).strip()}
            ),
        },
    }


def _profile_path(name: str) -> Path:
    return clients_root() / (name + ".yaml")


def _read_yaml(path: Path) -> Dict[str, Any]:
    _assert_private(path)
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ClientProfileError("could not read %s: %s" % (path, exc)) from exc
    if not isinstance(value, dict) or value.get("schema") != PROFILE_SCHEMA:
        raise ClientProfileError("%s is not a %s profile" % (path, PROFILE_SCHEMA))
    _reject_unknown(value, _ALLOWED_STORED_TOP, field="profile")
    connection = _mapping(value.get("connection"), field="connection")
    ssh = _mapping(value.get("ssh"), field="ssh")
    credential = _mapping(value.get("credential"), field="credential")
    _reject_unknown(connection, _ALLOWED_CONNECTION, field="connection")
    _reject_unknown(ssh, _ALLOWED_SSH, field="ssh")
    _reject_unknown(
        credential,
        _ALLOWED_STORED_CREDENTIAL,
        field="credential",
    )
    _name(value.get("profile"))
    _name(value.get("client_id"), field="client_id")
    _validate_api_url(connection.get("api_url"))
    return value


def _credential_path_from_profile(profile: Mapping[str, Any]) -> Path:
    credential = _mapping(profile.get("credential"), field="credential")
    ref = str(credential.get("path") or "").strip()
    if not ref:
        raise ClientProfileError("client profile has no credential reference")
    path = (clients_root() / ref).resolve()
    root = credentials_root().resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ClientProfileError("credential reference escapes the credential store") from exc
    return path


def install_enrollment_manifest(
    raw: Mapping[str, Any],
    *,
    profile_override: Optional[str] = None,
    activate: bool = True,
) -> Dict[str, Any]:
    validated = validate_enrollment_manifest(raw, profile_override=profile_override)
    profile = validated["profile"]
    name = validated["profile_name"]
    token = validated["token"]
    profile_path = _profile_path(name)
    credential_path = credentials_root() / _credential_filename(profile["credential"]["id"])
    relative_credential = os.path.relpath(credential_path, clients_root())
    profile["credential"]["path"] = relative_credential

    existing: Optional[Dict[str, Any]] = None
    old_token = ""
    if profile_path.exists():
        existing = _read_yaml(profile_path)
        old_path = _credential_path_from_profile(existing)
        try:
            if old_path.is_file():
                _assert_private(old_path)
                old_token = old_path.read_text(encoding="utf-8").strip()
        except OSError:
            old_token = ""
        comparable = dict(existing)
        comparable.pop("installed_at", None)
        if comparable == profile and old_token == token:
            if activate:
                activate_profile(name)
            return {
                "profile": name,
                "changed": False,
                "active": active_profile_name() == name,
                "profile_path": str(profile_path),
            }

    _ensure_dir(mac_home())
    _ensure_dir(clients_root())
    _ensure_dir(credentials_root())
    backup_dir: Optional[Path] = None
    if existing is not None:
        backup_dir = clients_root() / "backups" / (name + "." + _stamp())
        _ensure_dir(backup_dir)
        shutil.copy2(profile_path, backup_dir / profile_path.name)
        (backup_dir / profile_path.name).chmod(0o600)
        old_credential = _credential_path_from_profile(existing)
        if old_credential.is_file():
            shutil.copy2(old_credential, backup_dir / old_credential.name)
            (backup_dir / old_credential.name).chmod(0o600)

    # Logical commit: the credential exists before the profile references it.
    _atomic_text(credential_path, token + "\n")
    profile["installed_at"] = _timestamp()
    _atomic_text(profile_path, yaml.safe_dump(profile, sort_keys=False))
    if activate:
        activate_profile(name)

    # The old credential is no longer referenced.  A secure backup exists when
    # this was an update; otherwise remove it after the profile commit.
    if existing is not None:
        old_credential = _credential_path_from_profile(existing)
        if old_credential != credential_path.resolve() and old_credential.is_file():
            old_credential.unlink()

    return {
        "profile": name,
        "changed": True,
        "active": active_profile_name() == name,
        "profile_path": str(profile_path),
        "backup": str(backup_dir) if backup_dir else None,
    }


def active_profile_name() -> Optional[str]:
    path = clients_root() / "current"
    if not path.is_file():
        return None
    _assert_private(path)
    value = path.read_text(encoding="utf-8").strip()
    return _name(value) if value else None


def activate_profile(name: str) -> Dict[str, Any]:
    name = _name(name)
    if not _profile_path(name).is_file():
        raise ClientProfileError("client profile %r does not exist" % name)
    _atomic_text(clients_root() / "current", name + "\n")
    return {"profile": name, "active": True}


def list_profiles() -> List[Dict[str, Any]]:
    root = clients_root()
    active = active_profile_name()
    if not root.is_dir():
        return []
    result = []
    for path in sorted(root.glob("*.yaml")):
        profile = _read_yaml(path)
        result.append(
            {
                "profile": profile.get("profile"),
                "client_id": profile.get("client_id"),
                "fleet": profile.get("fleet"),
                "api_url": (profile.get("connection") or {}).get("api_url"),
                "scopes": (profile.get("credential") or {}).get("scopes") or [],
                "expires_at": (profile.get("credential") or {}).get("expires_at"),
                "active": profile.get("profile") == active,
            }
        )
    return result


def load_profile(
    name: Optional[str] = None, *, include_token: bool = False
) -> Dict[str, Any]:
    selected = _name(name) if name else active_profile_name()
    if selected is None:
        profiles = list_profiles()
        if len(profiles) == 1:
            selected = str(profiles[0]["profile"])
        else:
            raise ClientProfileError(
                "no active client profile; select one with `mac client profile activate`"
            )
    profile = _read_yaml(_profile_path(selected))
    if include_token:
        path = _credential_path_from_profile(profile)
        if not path.is_file():
            raise ClientProfileError("credential record is missing for profile %r" % selected)
        _assert_private(path)
        profile = json.loads(json.dumps(profile))
        profile["credential"]["token"] = path.read_text(encoding="utf-8").strip()
    return profile


def show_profile(name: Optional[str] = None) -> Dict[str, Any]:
    profile = load_profile(name, include_token=False)
    result = json.loads(json.dumps(profile))
    result["active"] = active_profile_name() == result.get("profile")
    result.get("credential", {}).pop("path", None)
    result.get("credential", {})["stored"] = True
    return result


def remove_profile(name: str) -> Dict[str, Any]:
    name = _name(name)
    profile = load_profile(name)
    credential_path = _credential_path_from_profile(profile)
    path = _profile_path(name)
    if path.exists():
        path.unlink()
    if credential_path.is_file():
        credential_path.unlink()
    if active_profile_name() == name:
        current = clients_root() / "current"
        if current.exists():
            current.unlink()
    return {"profile": name, "removed": True}


def read_manifest(path: str) -> Dict[str, Any]:
    if path == "-":
        import sys

        raw = sys.stdin.read()
    else:
        raw = Path(path).expanduser().read_text(encoding="utf-8")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClientProfileError("enrollment manifest must be JSON: %s" % exc) from exc
    if not isinstance(value, dict):
        raise ClientProfileError("enrollment manifest must be an object")
    return value


def migrate_legacy_profile(
    *,
    fleet: str,
    profile: Optional[str] = None,
    fleets_config: Optional[str] = None,
    env_file: Optional[str] = None,
    allow_legacy_admin_token: bool = False,
    activate: bool = True,
) -> Dict[str, Any]:
    """Import one legacy fleets.yaml/.env connection without copying ~/.mac.

    A flat/scoped legacy ``MAC_API_TOKEN`` is an administrator token under the
    historical server contract.  Import therefore requires an explicit
    elevation acknowledgement; SSH enrollment is the preferred least-privilege
    path.
    """

    if not allow_legacy_admin_token:
        raise ClientProfileError(
            "legacy MAC_API_TOKEN is administrator authority; pass "
            "--allow-legacy-admin-token to import it temporarily, or use SSH enrollment"
        )
    from mac.fleet_env import parse_env_file, resolve
    from mac.fleet_ssh import load_fleet_config, resolve_fleet_ssh

    cfg_path = Path(
        fleets_config
        or os.environ.get("MAC_FLEETS_CONFIG")
        or (mac_home() / "fleets.yaml")
    ).expanduser()
    env_path = Path(env_file or (mac_home() / ".env")).expanduser()
    config = load_fleet_config(str(cfg_path))
    spec = resolve_fleet_ssh(config, fleet)
    env = parse_env_file(env_path)
    token = resolve("MAC_API_TOKEN", fleet=fleet, env=env)
    if not token:
        raise ClientProfileError("no fleet-scoped MAC_API_TOKEN found for %r" % fleet)
    fleet_cfg = config["fleets"][spec.fleet]
    api_url = str(fleet_cfg.get("hub_url") or "http://127.0.0.1:%d" % spec.control_port)
    profile_name = profile or fleet
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "client_id": _name(profile_name, field="client_id"),
        "display_name": "Legacy %s client" % fleet,
        "profile": profile_name,
        "fleet": fleet,
        "connection": {
            "api_url": api_url,
            "mode": "direct",
        },
        "ssh": {
            key: value
            for key, value in {
                "target": spec.target,
                "port": spec.port,
                "proxy_jump": spec.proxy_jump,
                "identity_file": spec.identity_file,
                "identity_ref": spec.identity_ref,
                "known_hosts_file": spec.known_hosts_file,
                "host_key_policy": spec.host_key_policy,
                "host_key_fingerprint": spec.host_key_fingerprint,
                "host_ca": spec.host_ca,
            }.items()
            if value not in (None, "")
        },
        "credential": {
            "id": "legacy.%s.v1" % profile_name,
            "token": token,
            "scopes": ["admin"],
            "issued_at": "",
            "expires_at": "",
        },
        "capabilities": [],
    }
    result = install_enrollment_manifest(
        manifest, profile_override=profile_name, activate=activate
    )
    # Migration does not mutate the legacy files. On the first import, retain a
    # secure provenance/rollback snapshot; an idempotent retry creates nothing.
    if result.get("changed"):
        backup_root = clients_root() / "migration-backups" / _stamp()
        _ensure_dir(backup_root)
        for source in (cfg_path, env_path):
            if source.is_file():
                target = backup_root / source.name
                shutil.copy2(source, target)
                target.chmod(0o600)
        result["legacy_backup"] = str(backup_root)
    result["legacy_admin"] = True
    return result
