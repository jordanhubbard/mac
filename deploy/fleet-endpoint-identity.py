#!/usr/bin/env python3
"""Build and compare secret-free fleet endpoint identity records.

Fleet names and SSH targets are routing hints, not resource ownership.  This
helper converts adapter-specific, read-only observations into bounded records
that a deployment journal may retain safely and compare after a controller
restart.  It deliberately stores only public-key/instance identifier digests;
hostnames, targets, URLs, credentials, and raw platform identifiers are never
written to the record.

``ssh-machine`` authority is exact: both the negotiated SSH host key and the
machine/instance identifier must match.  ``kubernetes-workload`` authority is
the cluster plus workload object UID.  A pod UID is an observation of that
authority, so a new pod is reported as observation drift and may only be acted
on through the Kubernetes workload adapter, never as an SSH-machine match.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Any


IDENTITY_SCHEMA = "mac.fleet_endpoint_identity.v1"
COMPARISON_SCHEMA = "mac.fleet_endpoint_identity_comparison.v1"
MAX_RECORD_BYTES = 64 * 1024
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_KIND = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
SSH_AUTHORITY_KEYS = frozenset({"ssh_host_key_sha256", "instance_id_kind", "instance_id_sha256"})
SSH_HUB_AUTHORITY_KEYS = SSH_AUTHORITY_KEYS | {"durable_store_uuid_sha256"}
K8S_AUTHORITY_KEYS = frozenset({"cluster_uid_sha256", "workload_kind", "workload_uid_sha256"})
K8S_HUB_AUTHORITY_KEYS = K8S_AUTHORITY_KEYS | {"durable_store_uuid_sha256"}
K8S_OBSERVATION_KEYS = frozenset({"pod_uid_sha256"})
TOP_LEVEL_KEYS = frozenset({"schema", "adapter", "authority", "observation"})


class IdentityError(ValueError):
    """A stable, user-facing endpoint identity validation failure."""


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest_identifier(value: str, field: str, *, case_insensitive: bool = False) -> str:
    normalized = str(value or "").strip()
    if case_insensitive:
        normalized = normalized.lower()
    if not normalized:
        raise IdentityError(f"{field} must not be empty")
    if len(normalized.encode("utf-8")) > 512:
        raise IdentityError(f"{field} is too long")
    if any(ord(character) < 33 or ord(character) == 127 for character in normalized):
        raise IdentityError(f"{field} contains whitespace or a control character")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _host_key_digest(value: str) -> str:
    candidate = str(value or "").strip()
    if HEX_SHA256.fullmatch(candidate):
        return candidate
    if not candidate.startswith("SHA256:"):
        raise IdentityError("SSH host-key fingerprint must be lowercase hex or OpenSSH SHA256 form")
    encoded = candidate.removeprefix("SHA256:")
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise IdentityError("SSH host-key fingerprint is malformed") from exc
    if len(raw) != 32:
        raise IdentityError("SSH host-key fingerprint must encode one SHA-256 digest")
    return raw.hex()


def _kind(value: str, field: str) -> str:
    parsed = str(value or "").strip().lower()
    if SAFE_KIND.fullmatch(parsed) is None:
        raise IdentityError(f"{field} is not a safe adapter identifier")
    return parsed


def build_ssh_machine(
    *,
    host_key_fingerprint: str,
    instance_id_kind: str,
    instance_id: str,
    durable_store_uuid: str = "",
) -> dict[str, Any]:
    parsed_kind = _kind(instance_id_kind, "instance id kind")
    authority = {
        "ssh_host_key_sha256": _host_key_digest(host_key_fingerprint),
        "instance_id_kind": parsed_kind,
        "instance_id_sha256": _digest_identifier(
            instance_id,
            "instance id",
            case_insensitive=parsed_kind in {"linux-machine-id", "darwin-platform-uuid"},
        ),
    }
    adapter = "ssh-machine"
    if durable_store_uuid:
        adapter = "ssh-hub"
        authority["durable_store_uuid_sha256"] = _digest_identifier(
            durable_store_uuid, "durable store uuid", case_insensitive=True
        )
    return {
        "schema": IDENTITY_SCHEMA,
        "adapter": adapter,
        "authority": authority,
        "observation": {},
    }


def build_kubernetes_workload(
    *,
    cluster_uid: str,
    workload_kind: str,
    workload_uid: str,
    pod_uid: str,
    durable_store_uuid: str = "",
) -> dict[str, Any]:
    authority = {
        "cluster_uid_sha256": _digest_identifier(cluster_uid, "cluster uid", case_insensitive=True),
        "workload_kind": _kind(workload_kind, "workload kind"),
        "workload_uid_sha256": _digest_identifier(
            workload_uid, "workload uid", case_insensitive=True
        ),
    }
    adapter = "kubernetes-workload"
    if durable_store_uuid:
        adapter = "kubernetes-hub"
        authority["durable_store_uuid_sha256"] = _digest_identifier(
            durable_store_uuid, "durable store uuid", case_insensitive=True
        )
    return {
        "schema": IDENTITY_SCHEMA,
        "adapter": adapter,
        "authority": authority,
        "observation": {
            "pod_uid_sha256": _digest_identifier(pod_uid, "pod uid", case_insensitive=True),
        },
    }


def _exact_keys(value: Any, keys: frozenset[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise IdentityError(f"{context} keys differ from the identity schema")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or HEX_SHA256.fullmatch(value) is None:
        raise IdentityError(f"{field} must be lowercase 64-hex")
    return value


def validate_identity(value: Any) -> dict[str, Any]:
    record = _exact_keys(value, TOP_LEVEL_KEYS, "endpoint identity")
    if record["schema"] != IDENTITY_SCHEMA:
        raise IdentityError("endpoint identity schema is unsupported")
    adapter = record["adapter"]
    authority = record["authority"]
    observation = record["observation"]
    if adapter in {"ssh-machine", "ssh-hub"}:
        authority = _exact_keys(
            authority,
            SSH_HUB_AUTHORITY_KEYS if adapter == "ssh-hub" else SSH_AUTHORITY_KEYS,
            "SSH authority",
        )
        _sha(authority["ssh_host_key_sha256"], "SSH host-key digest")
        _kind(authority["instance_id_kind"], "instance id kind")
        _sha(authority["instance_id_sha256"], "instance id digest")
        if adapter == "ssh-hub":
            _sha(authority["durable_store_uuid_sha256"], "durable store uuid digest")
        _exact_keys(observation, frozenset(), "SSH observation")
    elif adapter in {"kubernetes-workload", "kubernetes-hub"}:
        authority = _exact_keys(
            authority,
            K8S_HUB_AUTHORITY_KEYS if adapter == "kubernetes-hub" else K8S_AUTHORITY_KEYS,
            "Kubernetes authority",
        )
        _sha(authority["cluster_uid_sha256"], "cluster uid digest")
        _kind(authority["workload_kind"], "workload kind")
        _sha(authority["workload_uid_sha256"], "workload uid digest")
        if adapter == "kubernetes-hub":
            _sha(authority["durable_store_uuid_sha256"], "durable store uuid digest")
        observation = _exact_keys(observation, K8S_OBSERVATION_KEYS, "Kubernetes observation")
        _sha(observation["pod_uid_sha256"], "pod uid digest")
    else:
        raise IdentityError("endpoint identity adapter is unsupported")
    # Canonical round-trip returns detached builtins and prevents caller mutation.
    return json.loads(_canonical(record))


def compare_identities(expected: Any, observed: Any) -> dict[str, Any]:
    left = validate_identity(expected)
    right = validate_identity(observed)
    mismatches: list[str] = []
    if left["adapter"] != right["adapter"]:
        mismatches.append("adapter")
        same_resource = False
        same_observation = False
    else:
        for key in sorted(left["authority"]):
            if left["authority"].get(key) != right["authority"].get(key):
                mismatches.append(f"authority.{key}")
        same_resource = not mismatches
        same_observation = left["observation"] == right["observation"]
        if same_resource and not same_observation:
            mismatches.extend(
                f"observation.{key}"
                for key in sorted(set(left["observation"]) | set(right["observation"]))
                if left["observation"].get(key) != right["observation"].get(key)
            )
    return {
        "schema": COMPARISON_SCHEMA,
        "adapter": left["adapter"],
        "same_resource": same_resource,
        "same_observation": same_observation,
        "recovery_allowed": same_resource,
        "generic_route_recovery_allowed": bool(
            same_resource
            and not (left["adapter"].startswith("kubernetes-") and not same_observation)
        ),
        "requires_workload_adapter": bool(
            same_resource and left["adapter"].startswith("kubernetes-") and not same_observation
        ),
        "mismatches": mismatches,
    }


def _open_private_directory(path: Path, *, create: bool = False) -> int:
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        before = path.lstat()
    except OSError as exc:
        raise IdentityError(f"cannot inspect endpoint identity directory: {exc}") from exc
    if (
        not stat.S_ISDIR(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) & 0o077
    ):
        raise IdentityError("endpoint identity directory must be owner-private and nonsymlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise IdentityError(f"cannot securely open endpoint identity directory: {exc}") from exc
    after = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(after.st_mode)
        or after.st_uid != os.getuid()
        or stat.S_IMODE(after.st_mode) & 0o077
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
    ):
        os.close(descriptor)
        raise IdentityError("endpoint identity directory changed while opening")
    return descriptor


def _read_private_json(path: Path) -> Any:
    directory_fd = _open_private_directory(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path.name, flags, dir_fd=directory_fd)
    except OSError as exc:
        os.close(directory_fd)
        raise IdentityError(f"cannot securely open endpoint identity: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size < 2
            or before.st_size > MAX_RECORD_BYTES
        ):
            raise IdentityError("endpoint identity must be an owner-private regular file")
        raw = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if len(raw) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise IdentityError("endpoint identity changed while reading")
    finally:
        os.close(descriptor)
        os.close(directory_fd)
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdentityError("endpoint identity is not valid UTF-8 JSON") from exc


def _write_private_json(path: Path, value: Any) -> None:
    directory_fd = _open_private_directory(path.parent, create=True)
    try:
        existing = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode)
        or existing.st_uid != os.getuid()
        or existing.st_nlink != 1
        or stat.S_IMODE(existing.st_mode) != 0o600
    ):
        os.close(directory_fd)
        raise IdentityError("existing endpoint identity is not an owner-private file")
    temporary = f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(16)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _emit(value: Any) -> None:
    sys.stdout.buffer.write(_canonical(value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    ssh = commands.add_parser("build-ssh")
    ssh.add_argument("--host-key-fingerprint", required=True)
    ssh.add_argument("--instance-id-kind", required=True)
    ssh.add_argument("--instance-id", required=True)
    ssh.add_argument("--durable-store-uuid", default="")
    ssh.add_argument("--output", required=True)

    k8s = commands.add_parser("build-k8s")
    k8s.add_argument("--cluster-uid", required=True)
    k8s.add_argument("--workload-kind", required=True)
    k8s.add_argument("--workload-uid", required=True)
    k8s.add_argument("--pod-uid", required=True)
    k8s.add_argument("--durable-store-uuid", default="")
    k8s.add_argument("--output", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("identity_file")

    compare = commands.add_parser("compare")
    compare.add_argument("--expected", required=True)
    compare.add_argument("--observed", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build-ssh":
            value = build_ssh_machine(
                host_key_fingerprint=args.host_key_fingerprint,
                instance_id_kind=args.instance_id_kind,
                instance_id=args.instance_id,
                durable_store_uuid=args.durable_store_uuid,
            )
            _write_private_json(Path(args.output), value)
        elif args.command == "build-k8s":
            value = build_kubernetes_workload(
                cluster_uid=args.cluster_uid,
                workload_kind=args.workload_kind,
                workload_uid=args.workload_uid,
                pod_uid=args.pod_uid,
                durable_store_uuid=args.durable_store_uuid,
            )
            _write_private_json(Path(args.output), value)
        elif args.command == "validate":
            value = validate_identity(_read_private_json(Path(args.identity_file)))
        else:
            value = compare_identities(
                _read_private_json(Path(args.expected)),
                _read_private_json(Path(args.observed)),
            )
        _emit(
            {"ok": True, "identity": value}
            if args.command != "compare"
            else {"ok": True, "comparison": value}
        )
        return 0
    except (IdentityError, OSError) as exc:
        _emit({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
