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

from mac.deploy_env import read_env_file, write_env_file
from mac.services import sign_verification_manifest


PROBE_SCHEMA = "mac.agent_attestation_key_probe.v1"
CHALLENGE_SCHEMA = "mac.agent_attestation_challenge.v1"
RECOVERY_MANIFEST_SCHEMA = "mac.agent_attestation_key_recovery.v1"
INSTALL_RECEIPT_SCHEMA = "mac.agent_attestation_key_install_receipt.v1"


class DeploymentAttestationError(ValueError):
    """The fenced attestation-key handoff contract was violated."""


def _required(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text or "\x00" in text:
        raise DeploymentAttestationError("%s is required" % label)
    return text


def _private_regular_file(path: Path, *, label: str) -> None:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise DeploymentAttestationError("%s is unreadable" % label) from exc
    if not stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        raise DeploymentAttestationError("%s must be a regular file" % label)
    if stat.S_IMODE(observed.st_mode) & 0o077:
        raise DeploymentAttestationError("%s must be owner-only" % label)


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
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
    if path.exists():
        _private_regular_file(path, label="attestation environment")
    key = read_env_file(path).get("MAC_ATTESTATION_KEY", "")
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
    _private_regular_file(source, label="attestation recovery manifest")
    try:
        manifest = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise DeploymentAttestationError(
            "attestation recovery manifest is unreadable"
        ) from exc
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
    if manifest.get("deployment_id") != _required(
        expected_deployment_id, "expected deployment id"
    ):
        raise DeploymentAttestationError(
            "attestation recovery manifest deployment does not match"
        )
    key = _required(manifest.get("attestation_key"), "attestation key")
    if len(key) < 32 or any(character.isspace() for character in key):
        raise DeploymentAttestationError("attestation key has an unsafe shape")

    destination = env_file.expanduser()
    if destination.exists():
        _private_regular_file(destination, label="attestation environment")
    destination.parent.mkdir(parents=True, exist_ok=True)
    values = read_env_file(destination)
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
    source.unlink()
    return {
        "schema": INSTALL_RECEIPT_SCHEMA,
        "agent_id": expected_agent_id,
        "deployment_id": expected_deployment_id,
        "destination": str(destination),
        "key_fingerprint": fingerprint,
        "installed": True,
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
    except DeploymentAttestationError as exc:
        print("deployment attestation error: %s" % exc, file=os.sys.stderr)
        return 2
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
