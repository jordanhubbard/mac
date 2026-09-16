"""Fenced deployment helpers for worker attestation-key recovery.

The worker side of this protocol never calls a rotation endpoint and never
prints key material.  It emits only an HMAC challenge probe, or an explicit
``missing`` result.  An administrator-owned deployment controller may then
request a conditional recovery rotation and relay the one-use manifest through
owner-only files before atomically installing it on the target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from mac.deploy_env import env_file_lock, parse_env_text, write_env_file
from mac.services import sign_verification_manifest


PROBE_SCHEMA = "mac.agent_attestation_key_probe.v1"
CHALLENGE_SCHEMA = "mac.agent_attestation_challenge.v1"
RECOVERY_MANIFEST_SCHEMA = "mac.agent_attestation_key_recovery.v1"
INSTALL_RECEIPT_SCHEMA = "mac.agent_attestation_key_install_receipt.v1"
CANDIDATE_PROOF_SCHEMA = "mac.fleet_release_attestation_candidate_proof.v1"
CANDIDATE_PROOF_PURPOSE = "synchronized-fleet-release-candidate"
MAX_PRIVATE_BYTES = 1024 * 1024


class DeploymentAttestationError(ValueError):
    """The fenced attestation-key handoff contract was violated."""


def _required(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text or "\x00" in text:
        raise DeploymentAttestationError("%s is required" % label)
    return text


def _private_regular_file(path: Path, *, label: str) -> os.stat_result:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise DeploymentAttestationError("%s is unreadable" % label) from exc
    if not stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        raise DeploymentAttestationError("%s must be a regular file" % label)
    if observed.st_uid != os.getuid():
        raise DeploymentAttestationError("%s must be owned by this user" % label)
    if observed.st_nlink != 1:
        raise DeploymentAttestationError("%s must have one hard link" % label)
    if stat.S_IMODE(observed.st_mode) & 0o077:
        raise DeploymentAttestationError("%s must be owner-only" % label)
    if not 1 <= observed.st_size <= MAX_PRIVATE_BYTES:
        raise DeploymentAttestationError("%s must be bounded" % label)
    return observed


def _private_bytes(path: Path, *, label: str) -> tuple[bytes, os.stat_result]:
    before = _private_regular_file(path, label=label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DeploymentAttestationError("%s is unreadable" % label) from exc
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise DeploymentAttestationError("%s changed while opening" % label)
        raw = bytearray()
        while len(raw) < opened.st_size:
            chunk = os.read(descriptor, min(64 * 1024, opened.st_size - len(raw)))
            if not chunk:
                raise DeploymentAttestationError("%s was truncated" % label)
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise DeploymentAttestationError("%s grew while reading" % label)
        after = os.fstat(descriptor)

        def identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if identity(opened) != identity(after):
            raise DeploymentAttestationError("%s changed while reading" % label)
        return bytes(raw), after
    finally:
        os.close(descriptor)


def _private_json(path: Path, *, label: str) -> tuple[dict[str, Any], os.stat_result]:
    raw, identity = _private_bytes(path, label=label)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentAttestationError("%s is unreadable" % label) from exc
    if not isinstance(value, dict):
        raise DeploymentAttestationError("%s is malformed" % label)
    return value, identity


def _private_env(path: Path, *, label: str) -> Dict[str, str]:
    raw, _identity = _private_bytes(path, label=label)
    try:
        return parse_env_text(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DeploymentAttestationError("%s is unreadable" % label) from exc


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise DeploymentAttestationError("output directory must be owner-only")
    descriptor, raw = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(value), stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def build_key_probe(
    agent_id: str,
    deployment_id: str,
    env_file: Path,
    *,
    nonce: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a secret-free signed probe from the target's installed key."""

    exact_agent = _required(agent_id, "agent id")
    exact_deployment = _required(deployment_id, "deployment id")
    path = env_file.expanduser()
    values = _private_env(path, label="attestation environment") if path.exists() else {}
    key = values.get("MAC_ATTESTATION_KEY", "")
    if not key:
        return {
            "schema": PROBE_SCHEMA,
            "state": "missing",
            "agent_id": exact_agent,
            "deployment_id": exact_deployment,
            "challenge": {},
            "signature": "",
        }
    challenge = {
        "schema": CHALLENGE_SCHEMA,
        "purpose": "fleet-deploy-attestation-key-proof",
        "agent_id": exact_agent,
        "deployment_id": exact_deployment,
        "nonce": nonce or secrets.token_urlsafe(32),
    }
    return {
        "schema": PROBE_SCHEMA,
        "state": "present",
        "agent_id": exact_agent,
        "deployment_id": exact_deployment,
        "challenge": challenge,
        "signature": sign_verification_manifest(key, challenge),
    }


def recovery_manifest(
    agent_id: str,
    deployment_id: str,
    attestation_key: str,
) -> Dict[str, Any]:
    key = _required(attestation_key, "attestation key")
    if len(key) < 32 or any(character.isspace() for character in key):
        raise DeploymentAttestationError("attestation key has an unsafe shape")
    return {
        "schema": RECOVERY_MANIFEST_SCHEMA,
        "agent_id": _required(agent_id, "agent id"),
        "deployment_id": _required(deployment_id, "deployment id"),
        "attestation_key": key,
        "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def install_recovery_manifest(
    manifest_path: Path,
    env_file: Path,
    *,
    expected_agent_id: str,
    expected_deployment_id: str,
) -> Dict[str, Any]:
    """Consume one private manifest and atomically install its key."""

    source = manifest_path.expanduser()
    manifest, source_identity = _private_json(source, label="attestation recovery manifest")
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema",
        "agent_id",
        "deployment_id",
        "attestation_key",
        "issued_at",
    }:
        raise DeploymentAttestationError("attestation recovery manifest is malformed")
    if manifest.get("schema") != RECOVERY_MANIFEST_SCHEMA:
        raise DeploymentAttestationError("attestation recovery manifest schema is unsupported")
    if manifest.get("agent_id") != _required(expected_agent_id, "expected agent id"):
        raise DeploymentAttestationError("attestation recovery manifest agent does not match")
    if manifest.get("deployment_id") != _required(expected_deployment_id, "expected deployment id"):
        raise DeploymentAttestationError("attestation recovery manifest deployment does not match")
    key = _required(manifest.get("attestation_key"), "attestation key")
    if len(key) < 32 or any(character.isspace() for character in key):
        raise DeploymentAttestationError("attestation key has an unsafe shape")

    destination = env_file.expanduser()
    if destination.exists():
        _private_regular_file(destination, label="attestation environment")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = destination.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise DeploymentAttestationError("attestation environment directory must be owner-only")
    with env_file_lock(destination):
        values = (
            _private_env(destination, label="attestation environment")
            if destination.exists()
            else {}
        )
        values["MAC_ATTESTATION_KEY"] = key
        descriptor, raw = tempfile.mkstemp(
            prefix=destination.name + ".", dir=str(destination.parent)
        )
        os.close(descriptor)
        temporary = Path(raw)
        try:
            write_env_file(temporary, values)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            directory_fd = os.open(str(destination.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    # Successful installation consumes the worker-side raw-key copy. The hub
    # copy remains until the controller observes a second signed proof.
    current = _private_regular_file(source, label="attestation recovery manifest")
    if (current.st_dev, current.st_ino) != (
        source_identity.st_dev,
        source_identity.st_ino,
    ):
        raise DeploymentAttestationError("attestation recovery manifest changed before consumption")
    source.unlink()
    return {
        "schema": INSTALL_RECEIPT_SCHEMA,
        "agent_id": expected_agent_id,
        "deployment_id": expected_deployment_id,
        "destination": str(destination),
        "key_fingerprint": fingerprint,
        "installed": True,
    }


def build_candidate_proof(challenge_path: Path, env_file: Path) -> Dict[str, Any]:
    """Sign one exact hub-epoch challenge with the installed candidate key."""

    source = challenge_path.expanduser()
    challenge, _identity = _private_json(source, label="candidate challenge")
    required = {
        "schema",
        "purpose",
        "epoch_id",
        "agent_id",
        "generation",
        "principal_id",
        "candidate_fingerprint",
        "nonce",
    }
    if not isinstance(challenge, dict) or set(challenge) != required:
        raise DeploymentAttestationError("candidate challenge schema is not exact")
    if (
        challenge.get("schema") != CANDIDATE_PROOF_SCHEMA
        or challenge.get("purpose") != CANDIDATE_PROOF_PURPOSE
    ):
        raise DeploymentAttestationError("candidate challenge purpose is unsupported")
    for field in required - {"schema", "purpose"}:
        _required(challenge.get(field), f"candidate challenge {field}")

    environment = env_file.expanduser()
    key = _required(
        _private_env(environment, label="attestation environment").get("MAC_ATTESTATION_KEY", ""),
        "installed attestation key",
    )
    fingerprint = "sha256:" + hashlib.sha256(key.encode()).hexdigest()
    if fingerprint != challenge["candidate_fingerprint"]:
        raise DeploymentAttestationError(
            "installed attestation key differs from the candidate challenge"
        )
    return {
        "challenge": challenge,
        "signature": sign_verification_manifest(key, challenge),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe")
    probe.add_argument("--agent-id", required=True)
    probe.add_argument("--deployment-id", required=True)
    probe.add_argument("--env-file", required=True)
    probe.add_argument("--output")
    install = sub.add_parser("install")
    install.add_argument("--manifest", required=True)
    install.add_argument("--env-file", required=True)
    install.add_argument("--agent-id", required=True)
    install.add_argument("--deployment-id", required=True)
    install.add_argument("--receipt-out", required=True)
    candidate = sub.add_parser("prove-candidate")
    candidate.add_argument("--challenge", required=True)
    candidate.add_argument("--env-file", required=True)
    candidate.add_argument("--output", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "probe":
            result = build_key_probe(
                args.agent_id,
                args.deployment_id,
                Path(args.env_file),
            )
            if args.output:
                _atomic_private_json(Path(args.output), result)
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "install":
            result = install_recovery_manifest(
                Path(args.manifest),
                Path(args.env_file),
                expected_agent_id=args.agent_id,
                expected_deployment_id=args.deployment_id,
            )
            _atomic_private_json(Path(args.receipt_out), result)
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "prove-candidate":
            result = build_candidate_proof(
                Path(args.challenge),
                Path(args.env_file),
            )
            _atomic_private_json(Path(args.output), result)
            print(json.dumps({"status": "proved", "proof_written": True}, sort_keys=True))
            return 0
    except DeploymentAttestationError as exc:
        print("deployment attestation error: %s" % exc, file=os.sys.stderr)
        return 2
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
