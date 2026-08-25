#!/usr/bin/env python3
"""Finalize one committed fleet-node generation from its immutable artifacts.

The helper is installed before phase-2 mutation and its SHA-256 is recorded in
the cohort journal.  Normal execution and crash recovery therefore run the same
reviewed bytes instead of reconstructing finalization from a newer checkout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any


SCHEMA = "mac.fleet_node_finalize.v1"
SAFE_TIMESTAMP = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024


class FinalizeError(ValueError):
    """The committed generation cannot be finalized safely."""


def _private_bytes(path: Path, maximum: int, description: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FinalizeError(f"{description} is unreadable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 1 <= before.st_size <= maximum
        ):
            raise FinalizeError(f"{description} is not owner-private and bounded")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise FinalizeError(f"{description} was truncated")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise FinalizeError(f"{description} grew while reading")
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

        if identity(before) != identity(after):
            raise FinalizeError(f"{description} changed while reading")
        return bytes(raw)
    finally:
        os.close(descriptor)


def _json(raw: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalizeError(f"{description} is malformed") from exc
    if not isinstance(value, dict):
        raise FinalizeError(f"{description} must be an object")
    return value


def _required(value: str, description: str, maximum: int = 512) -> str:
    parsed = str(value or "").strip()
    if (
        not parsed
        or len(parsed.encode()) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in parsed)
    ):
        raise FinalizeError(f"{description} is invalid")
    return parsed


def _atomic_create(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise FinalizeError("finalize receipt directory must not be a symlink")
    os.chmod(path.parent, 0o700)
    descriptor, temporary_raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_raw)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise FinalizeError("finalize receipt appeared concurrently") from exc
        temporary.unlink()
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def finalize(
    *,
    mac_home: Path,
    agent: str,
    fleet: str,
    generation: str,
    revision: str,
    deploy_ts: str,
) -> dict[str, Any]:
    agent = _required(agent, "agent", 128)
    fleet = _required(fleet, "fleet", 128)
    generation = _required(generation, "generation")
    revision = _required(revision, "revision")
    if SAFE_TIMESTAMP.fullmatch(deploy_ts or "") is None:
        raise FinalizeError("deploy timestamp is invalid")
    root = mac_home.expanduser().resolve(strict=True)
    logs = root / "logs"
    post_path = logs / f"deploy-manifest-{deploy_ts}-post.json"
    intent_path = logs / f"rollback-{deploy_ts}-intent.json"
    output = logs / f"deploy-{deploy_ts}-finalize.json"
    post_raw = _private_bytes(post_path, MAX_MANIFEST_BYTES, "post manifest")
    intent_raw = _private_bytes(intent_path, MAX_MANIFEST_BYTES, "rollback intent")
    post = _json(post_raw, "post manifest")
    intent = _json(intent_raw, "rollback intent")
    post_rollback = post.get("rollback")
    intent_rollback = intent.get("rollback")
    post_intent = post_rollback.get("intent") if isinstance(post_rollback, dict) else None
    if (
        post.get("stage") != "post"
        or (post.get("deploy") or {}).get("generation") != generation
        or (post.get("deploy") or {}).get("mac_git_rev") != revision
        or intent.get("schema") != "mac.fleet_node_rollback_intent.v1"
        or intent.get("status") != "armed"
        or intent.get("generation") != generation
        or intent.get("revision") != revision
        or not isinstance(post_rollback, dict)
        or not isinstance(intent_rollback, dict)
        or post_rollback.get("status") != "armed"
        or post_rollback.get("authority") != "pre_mutation_intent"
        or post_rollback.get("path") != intent_rollback.get("path")
        or post_rollback.get("sha256") != intent_rollback.get("sha256")
        or not isinstance(post_intent, dict)
        or post_intent.get("path") != str(intent_path)
        or post_intent.get("sha256") != hashlib.sha256(intent_raw).hexdigest()
    ):
        raise FinalizeError("post manifest does not finalize the armed generation")
    expected = {
        "schema": SCHEMA,
        "status": "finalized",
        "agent": agent,
        "fleet": fleet,
        "generation": generation,
        "revision": revision,
        "post_manifest": {
            "path": str(post_path),
            "sha256": hashlib.sha256(post_raw).hexdigest(),
        },
        "rollback_intent": {
            "path": str(intent_path),
            "sha256": hashlib.sha256(intent_raw).hexdigest(),
        },
    }
    if output.exists() or output.is_symlink():
        receipt = _json(
            _private_bytes(output, MAX_RECEIPT_BYTES, "finalize receipt"),
            "finalize receipt",
        )
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise FinalizeError("finalize receipt belongs to another generation")
        return receipt
    payload = {
        **expected,
        "finalized_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _atomic_create(output, payload)
    return _json(
        _private_bytes(output, MAX_RECEIPT_BYTES, "finalize receipt"),
        "finalize receipt",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mac-home", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--fleet", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--deploy-ts", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        receipt = finalize(
            mac_home=Path(args.mac_home),
            agent=args.agent,
            fleet=args.fleet,
            generation=args.generation,
            revision=args.revision,
            deploy_ts=args.deploy_ts,
        )
    except FinalizeError as exc:
        print(f"fleet node finalize error: {exc}", file=os.sys.stderr)
        return 2
    os.write(1, (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
