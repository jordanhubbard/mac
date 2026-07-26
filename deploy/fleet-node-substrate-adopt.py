#!/usr/bin/env python3
"""Sealed, fail-forward adoption of a retained phase-zero substrate.

When a fungible node is machine-onboarded (``deploy/fleet-node-machine-onboard.py``)
its canonical source and venv are the *phase-zero* rollback baseline.  The first
normal typed deployment moves that canonical source/venv into generation backups
(``$MAC_HOME/backups/mac-src.<agent>.<ts>`` / ``venv.<agent>.<ts>``) before the
new generation commits.  If ``apply-phase2`` then fails, retain-forward recovery
(``MAC_DEPLOY_RECOVERY_POLICY=retain-forward``) correctly keeps the failed
successor in place for in-place diagnosis and does *not* roll back.  The live
``$MAC_HOME/src/mac`` and ``$MAC_HOME/venv`` are therefore the failed generation,
not the pristine phase-zero baseline, and the next repair attempt no longer has
complete canonical source/venv directories to arm as its own rollback base.

This helper is the single reviewed authority for adopting that exact state.  It
is a *pre-cohort* operation: it never installs, stops, starts, activates, or
inspects a live MAC service, and it never performs an implicit rollback.  It:

* validates the terminal retained-forward receipt and the sealed rollback intent
  it references, proving both belong to the same failed first deployment;
* binds the immutable phase-zero onboarding generation and its archived
  source/venv backup paths from the machine-onboarding receipt;
* rejects ambiguous, missing, writable-by-others, or identity-mismatched state;
* refuses to activate the archived substrate as a live generation; and
* publishes a sealed *adoption contract* that lets the next deployment use those
  archives ONLY as the predecessor recovery contract while it installs a fresh
  top-of-tree generation.

Publication is receipt-atomic and owner-private, mirroring the machine-onboarding
helper: the adoption receipt is the durable commit marker and any validation
failure retains the newest failed generation with its diagnostics untouched.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence


ONBOARDING_RECEIPT_SCHEMA = "mac.fleet_machine_onboarding_receipt.v1"
RETAIN_FORWARD_RECEIPT_SCHEMA = "mac.fleet_node_retain_forward_receipt.v1"
ROLLBACK_INTENT_SCHEMA = "mac.fleet_node_rollback_intent.v1"
ADOPTION_RECEIPT_SCHEMA = "mac.fleet_node_substrate_adoption.v1"
STATUS_SCHEMA = "mac.fleet_node_substrate_adoption_status.v1"
MAX_JSON_BYTES = 4 * 1024 * 1024
SAFE_GENERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,511}$")
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
HEX_SHA1 = re.compile(r"^[0-9a-f]{40}$")


class AdoptionError(RuntimeError):
    """The retained phase-zero substrate cannot be adopted safely."""


@dataclass(frozen=True)
class Layout:
    home: Path
    mac_home: Path
    source: Path
    venv: Path
    onboarding_receipt: Path
    adoption_receipt: Path
    lock: Path

    @classmethod
    def for_home(cls, home: Path, mac_home: Path | None = None) -> "Layout":
        home = home.expanduser().absolute()
        root = (mac_home or home / ".mac").expanduser().absolute()
        return cls(
            home=home,
            mac_home=root,
            source=root / "src" / "mac",
            venv=root / "venv",
            onboarding_receipt=root / "machine-onboarding-receipt.json",
            adoption_receipt=root / "substrate-adoption-receipt.json",
            lock=root / ".substrate-adoption.lock",
        )


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise AdoptionError(f"directory is not owner-controlled: {path}")
    os.chmod(path, 0o700)


@contextlib.contextmanager
def adoption_lock(layout: Layout) -> Iterator[None]:
    _ensure_private_directory(layout.mac_home)
    descriptor = os.open(
        layout.lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _private_json_bytes(path: Path, description: str) -> bytes:
    """Read an owner-private, group/other-unwritable, bounded regular file."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise AdoptionError(f"{description} is missing") from exc
    except OSError as exc:
        raise AdoptionError(f"{description} is unreadable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or (stat.S_IMODE(before.st_mode) & 0o022)
            or not 1 <= before.st_size <= MAX_JSON_BYTES
        ):
            raise AdoptionError(f"{description} is not owner-private and bounded")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise AdoptionError(f"{description} was truncated")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise AdoptionError(f"{description} grew while reading")
        after = os.fstat(descriptor)
        if (
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
            raise AdoptionError(f"{description} changed while reading")
    finally:
        os.close(descriptor)
    return bytes(raw)


def _private_json(path: Path, expected_schema: str, description: str) -> tuple[dict[str, Any], str]:
    raw = _private_json_bytes(path, description)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise AdoptionError(f"{description} is malformed") from exc
    if not isinstance(value, dict) or value.get("schema") != expected_schema:
        raise AdoptionError(f"{description} schema is invalid")
    return value, hashlib.sha256(raw).hexdigest()


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    body = _canonical(payload)
    if not 1 <= len(body) <= MAX_JSON_BYTES:
        raise AdoptionError("adoption JSON exceeds its safe bound")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_generation(value: Any, description: str) -> str:
    if not isinstance(value, str) or SAFE_GENERATION.fullmatch(value) is None:
        raise AdoptionError(f"{description} is invalid")
    return value


def _require_hex_sha256(value: Any, description: str) -> str:
    if not isinstance(value, str) or HEX_SHA256.fullmatch(value) is None:
        raise AdoptionError(f"{description} is not a sha256 digest")
    return value


def _archive_directory(path: Path, description: str) -> os.stat_result:
    """Prove an archived substrate directory exists and is owner-private."""

    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise AdoptionError(f"{description} is missing") from exc
    except OSError as exc:
        raise AdoptionError(f"{description} is unreadable") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise AdoptionError(f"{description} is a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise AdoptionError(f"{description} is not a directory")
    if metadata.st_uid != os.getuid():
        raise AdoptionError(f"{description} is not owner-controlled")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise AdoptionError(f"{description} is writable by group or other")
    try:
        next(iter(os.scandir(path)))
    except StopIteration as exc:
        raise AdoptionError(f"{description} is empty") from exc
    except OSError as exc:
        raise AdoptionError(f"{description} is unreadable") from exc
    return metadata


def _validate_onboarding_receipt(layout: Layout) -> tuple[dict[str, Any], str]:
    receipt, digest = _private_json(
        layout.onboarding_receipt,
        ONBOARDING_RECEIPT_SCHEMA,
        "machine-onboarding receipt",
    )
    for key, expected in (
        ("status", "published"),
        ("instance_kind", "fungible"),
    ):
        if receipt.get(key) != expected:
            raise AdoptionError(
                f"machine-onboarding receipt is not a published fungible baseline: {key}"
            )
    _safe_generation(receipt.get("generation"), "onboarding generation")
    paths = receipt.get("paths")
    if not isinstance(paths, dict):
        raise AdoptionError("machine-onboarding receipt paths are invalid")
    if paths.get("source") != str(layout.source) or paths.get("venv") != str(layout.venv):
        raise AdoptionError(
            "machine-onboarding receipt does not describe this node's source/venv layout"
        )
    return receipt, digest


def _validate_rollback_intent(
    path: Path, *, onboarding: dict[str, Any], layout: Layout
) -> tuple[dict[str, Any], str]:
    intent, digest = _private_json(path, ROLLBACK_INTENT_SCHEMA, "sealed rollback intent")
    if intent.get("status") != "armed":
        raise AdoptionError("sealed rollback intent is not in the armed terminal state")
    if intent.get("rollback_capable") is not True:
        raise AdoptionError("sealed rollback intent is not rollback-capable")
    # The failed first deployment's predecessor is the phase-zero onboarding
    # generation.  Binding it here proves the archived backups are the pristine
    # baseline rather than an arbitrary older generation.
    if intent.get("prior_generation") != onboarding.get("generation"):
        raise AdoptionError(
            "rollback intent predecessor is not the phase-zero onboarding generation"
        )
    if intent.get("agent") != onboarding.get("agent"):
        raise AdoptionError("rollback intent agent identity differs from onboarding")
    _safe_generation(intent.get("generation"), "failed generation")
    if intent.get("generation") == onboarding.get("generation"):
        raise AdoptionError("rollback intent failed generation equals the phase-zero baseline")
    revision = intent.get("revision")
    if not isinstance(revision, str) or HEX_SHA1.fullmatch(revision) is None:
        raise AdoptionError("rollback intent revision is invalid")

    artifacts = intent.get("artifacts")
    if not isinstance(artifacts, dict):
        raise AdoptionError("rollback intent artifacts are invalid")
    for label, live in (("source", layout.source), ("venv", layout.venv)):
        entry = artifacts.get(label)
        if not isinstance(entry, dict):
            raise AdoptionError(f"rollback intent {label} artifact is invalid")
        if entry.get("path") != str(live):
            raise AdoptionError(f"rollback intent {label} path differs from this node's layout")
        backup = entry.get("backup")
        if not isinstance(backup, str) or not backup:
            raise AdoptionError(
                f"rollback intent {label} backup is missing; there is no archived"
                " phase-zero substrate to adopt"
            )
    return intent, digest


def _validate_retain_forward_receipt(
    path: Path, *, intent_digest: str, onboarding: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    receipt, digest = _private_json(path, RETAIN_FORWARD_RECEIPT_SCHEMA, "retained-forward receipt")
    if receipt.get("status") != "retained_forward":
        raise AdoptionError("retained-forward receipt is not in its terminal state")
    if receipt.get("recovery_policy") != "retain-forward":
        raise AdoptionError("retained-forward receipt did not bind retain-forward")
    if receipt.get("rolled_back") is not False:
        raise AdoptionError(
            "retained-forward receipt claims a rollback occurred; refusing to adopt"
        )
    if receipt.get("agent") != onboarding.get("agent"):
        raise AdoptionError("retained-forward receipt agent differs from onboarding")
    _safe_generation(receipt.get("failed_generation"), "retained-forward failed generation")
    bound = receipt.get("rollback_intent")
    if not isinstance(bound, dict):
        raise AdoptionError("retained-forward receipt lacks a sealed rollback intent")
    if not isinstance(bound.get("path"), str) or not bound.get("path"):
        raise AdoptionError("retained-forward receipt rollback intent path is invalid")
    if (
        _require_hex_sha256(bound.get("sha256"), "retained-forward receipt rollback intent digest")
        != intent_digest
    ):
        raise AdoptionError("retained-forward receipt does not seal this exact rollback intent")
    return receipt, digest


def _reject_live_activation(layout: Layout, intent: dict[str, Any]) -> None:
    """Refuse to run if the archived substrate has been re-activated as live."""

    artifacts = intent["artifacts"]
    for label, live in (("source", layout.source), ("venv", layout.venv)):
        backup = Path(artifacts[label]["backup"])
        try:
            live_stat = live.lstat()
        except FileNotFoundError:
            # The failed generation has already been removed; the live path is
            # absent.  Adoption still binds the archive as the recovery contract.
            continue
        except OSError as exc:
            raise AdoptionError(f"live {label} path is unreadable") from exc
        if stat.S_ISLNK(live_stat.st_mode):
            raise AdoptionError(f"live {label} path is a symlink into the archive")
        try:
            backup_stat = backup.lstat()
        except OSError:
            continue
        if (live_stat.st_dev, live_stat.st_ino) == (
            backup_stat.st_dev,
            backup_stat.st_ino,
        ):
            raise AdoptionError(
                f"live {label} path already resolves to the archived substrate;"
                " refusing to treat the archive as a live generation"
            )


def adopt(
    layout: Layout,
    *,
    retain_forward_receipt: Path,
    rollback_intent: Path,
) -> dict[str, Any]:
    """Publish a sealed predecessor-recovery adoption contract."""

    with adoption_lock(layout):
        onboarding, onboarding_digest = _validate_onboarding_receipt(layout)
        intent, intent_digest = _validate_rollback_intent(
            rollback_intent, onboarding=onboarding, layout=layout
        )
        receipt, receipt_digest = _validate_retain_forward_receipt(
            retain_forward_receipt,
            intent_digest=intent_digest,
            onboarding=onboarding,
        )
        _reject_live_activation(layout, intent)

        artifacts = intent["artifacts"]
        source_backup = Path(artifacts["source"]["backup"])
        venv_backup = Path(artifacts["venv"]["backup"])
        _archive_directory(source_backup, "archived phase-zero source backup")
        _archive_directory(venv_backup, "archived phase-zero venv backup")

        existing = _existing_adoption(layout)
        payload = {
            "schema": ADOPTION_RECEIPT_SCHEMA,
            "status": "adopted",
            "activation": "predecessor_recovery_contract",
            "activated_as_generation": False,
            "implicit_rollback": False,
            "agent": onboarding["agent"],
            "onboarding_generation": onboarding["generation"],
            "failed_generation": intent["generation"],
            "failed_revision": intent["revision"],
            "predecessor_recovery": {
                "source_backup": str(source_backup),
                "venv_backup": str(venv_backup),
                "role": "rollback_base_for_next_generation",
            },
            "sealed_inputs": {
                "onboarding_receipt": {
                    "path": str(layout.onboarding_receipt),
                    "sha256": onboarding_digest,
                },
                "retain_forward_receipt": {
                    "path": str(retain_forward_receipt),
                    "sha256": receipt_digest,
                },
                "rollback_intent": {
                    "path": str(rollback_intent),
                    "sha256": intent_digest,
                },
            },
        }
        if existing is not None:
            # Idempotent re-run: an identical prior adoption is authoritative and
            # is not rewritten.  A conflicting one is a hard error so we never
            # silently rebind a different failed generation.
            comparable = {k: v for k, v in existing.items() if k != "adopted_at"}
            if comparable != payload:
                raise AdoptionError(
                    "an existing adoption receipt binds a different failed generation"
                )
            return existing
        payload["adopted_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        _atomic_private_json(layout.adoption_receipt, payload)
        return payload


def _existing_adoption(layout: Layout) -> dict[str, Any] | None:
    try:
        layout.adoption_receipt.lstat()
    except FileNotFoundError:
        return None
    receipt, _ = _private_json(
        layout.adoption_receipt, ADOPTION_RECEIPT_SCHEMA, "existing adoption receipt"
    )
    return receipt


def inspect(layout: Layout) -> dict[str, Any]:
    # Deliberately do not take the adoption lock or create ~/.mac: classification
    # must never mutate remote state.
    checks: dict[str, Any] = {
        "onboarding_receipt_present": _exists(layout.onboarding_receipt),
        "adoption_receipt_present": _exists(layout.adoption_receipt),
        "live_source_present": _exists(layout.source),
        "live_venv_present": _exists(layout.venv),
    }
    status = "eligible"
    if checks["adoption_receipt_present"]:
        status = "adopted"
    elif not checks["onboarding_receipt_present"]:
        status = "not_onboarded"
    return {
        "schema": STATUS_SCHEMA,
        "status": status,
        "checks": checks,
    }


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sealed, fail-forward adoption of a retained phase-zero substrate as"
            " the predecessor recovery contract for the next deployment"
        )
    )
    parser.add_argument("action", choices=("inspect", "adopt"))
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--mac-home")
    parser.add_argument("--retain-forward-receipt")
    parser.add_argument("--rollback-intent")
    return parser.parse_args(argv)


def _required(args: argparse.Namespace, *names: str) -> None:
    missing = [name for name in names if not getattr(args, name.replace("-", "_"), None)]
    if missing:
        raise AdoptionError("missing required arguments: " + ",".join(missing))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    layout = Layout.for_home(Path(args.home), Path(args.mac_home) if args.mac_home else None)
    try:
        if args.action == "inspect":
            payload = inspect(layout)
        else:
            _required(args, "retain-forward-receipt", "rollback-intent")
            payload = adopt(
                layout,
                retain_forward_receipt=Path(args.retain_forward_receipt),
                rollback_intent=Path(args.rollback_intent),
            )
    except (AdoptionError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(_canonical(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
