#!/usr/bin/env python3
"""Create and validate secret-free fleet prerequisite receipts.

Synchronized cut-over must not install packages, rotate keys, rewrite shared
service configuration, or repair durable application state.  Those resources
belong to independently operated prerequisite participants.  This helper runs
the deliberately small read-only probe vocabulary used by those participants
and seals the observations into an owner-private receipt bundle.

Contracts may contain local paths and loopback health endpoints, so contracts
and bundles are private controller inputs.  Receipts never retain paths, URLs,
response bodies, environment values, or command output: only bounded names,
status, timestamps, and SHA-256 digests are journal-safe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


CONTRACT_SCHEMA = "mac.fleet_prerequisite_contract.v1"
RECEIPT_SCHEMA = "mac.fleet_prerequisite_receipt.v1"
BUNDLE_SCHEMA = "mac.fleet_prerequisite_bundle.v1"
EXPECTATIONS_SCHEMA = "mac.fleet_prerequisite_expectations.v1"
SUMMARY_SCHEMA = "mac.fleet_prerequisite_bundle_summary.v1"

REQUIRED_PARTICIPANTS = (
    "machine-onboarding",
    "route-tunnel",
    "openshell",
    "qdrant",
    "firecrawl",
    "webdav",
    "hermes",
    "service-topology",
)

MAX_PRIVATE_FILE_BYTES = 1024 * 1024
MAX_HTTP_BODY_BYTES = 1024 * 1024
MAX_CHECKS = 128
MAX_NAME_BYTES = 96
MAX_PATH_BYTES = 4096
MAX_TIMEOUT_SECONDS = 10.0
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,95}$")
SAFE_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")

CONTRACT_KEYS = frozenset(
    {"schema", "participant", "agent_id", "node_identity_sha256", "checks"}
)
RECEIPT_KEYS = frozenset(
    {
        "schema",
        "participant",
        "agent_id",
        "node_identity_sha256",
        "contract_sha256",
        "observed_at_epoch",
        "status",
        "checks",
    }
)
BUNDLE_KEYS = frozenset(
    {"schema", "agent_id", "node_identity_sha256", "created_at_epoch", "receipts"}
)
EXPECTATIONS_KEYS = frozenset(
    {"schema", "agent_id", "node_identity_sha256", "contracts"}
)
RECEIPT_CHECK_KEYS = frozenset({"name", "kind", "evidence_sha256"})


class PrerequisiteError(ValueError):
    """A stable, fail-closed prerequisite validation error."""


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _sha256(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical(value)
    return hashlib.sha256(raw).hexdigest()


def _exact_dict(value: Any, keys: frozenset[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise PrerequisiteError(f"{context} keys differ from the schema")
    return value


def _safe_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or SAFE_NAME.fullmatch(value) is None:
        raise PrerequisiteError(f"{field} is not a safe identifier")
    return value


def _agent_id(value: Any) -> str:
    if not isinstance(value, str) or SAFE_AGENT.fullmatch(value) is None:
        raise PrerequisiteError("agent_id is invalid")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or HEX_SHA256.fullmatch(value) is None:
        raise PrerequisiteError(f"{field} must be lowercase 64-hex")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PrerequisiteError(f"{field} must be numeric")
    result = float(value)
    if not 0 < result <= MAX_TIMEOUT_SECONDS:
        raise PrerequisiteError(
            f"{field} must be greater than zero and at most {MAX_TIMEOUT_SECONDS:g}"
        )
    return result


def _private_regular_file(path: Path, context: str) -> os.stat_result:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise PrerequisiteError(f"{context} is unavailable") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise PrerequisiteError(f"{context} must be a regular non-symlink file")
    if before.st_nlink != 1:
        raise PrerequisiteError(f"{context} must not have multiple hard links")
    if before.st_uid != os.geteuid():
        raise PrerequisiteError(f"{context} must be owned by the current user")
    if stat.S_IMODE(before.st_mode) & 0o077:
        raise PrerequisiteError(f"{context} must be owner-private")
    if not 1 <= before.st_size <= MAX_PRIVATE_FILE_BYTES:
        raise PrerequisiteError(f"{context} is too large")
    return before


def _read_private_json(path: Path, context: str) -> Any:
    before = _private_regular_file(path, context)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        after = os.fstat(fd)
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or after.st_nlink != 1
            or after.st_uid != os.geteuid()
        ):
            raise PrerequisiteError(f"{context} changed while opening")
        raw = bytearray()
        while len(raw) < after.st_size:
            chunk = os.read(fd, min(64 * 1024, after.st_size - len(raw)))
            if not chunk:
                raise PrerequisiteError(f"{context} was truncated")
            raw.extend(chunk)
        if os.read(fd, 1):
            raise PrerequisiteError(f"{context} grew while reading")
        final = os.fstat(fd)
        if (
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            final.st_dev,
            final.st_ino,
            final.st_nlink,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ):
            raise PrerequisiteError(f"{context} changed while reading")
    finally:
        os.close(fd)
    try:
        return json.loads(bytes(raw))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrerequisiteError(f"{context} is not valid UTF-8 JSON") from exc


def _atomic_private_json(path: Path, value: Any) -> None:
    destination = path.expanduser()
    if not destination.is_absolute():
        raise PrerequisiteError("output path must be absolute")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = os.lstat(destination.parent)
    if not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode):
        raise PrerequisiteError("output parent must be a real directory")
    if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise PrerequisiteError("output parent must be owner-private")
    fd, raw_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(raw_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _absolute_path(value: Any) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PrerequisiteError("check path is invalid")
    if len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise PrerequisiteError("check path is too long")
    result = Path(value).expanduser()
    if not result.is_absolute():
        raise PrerequisiteError("check path must be absolute")
    return result


def _validate_path_check(value: dict[str, Any]) -> dict[str, Any]:
    keys = frozenset({"name", "kind", "path", "file_type", "expected_mode", "sha256"})
    check = _exact_dict(value, keys, "path check")
    _safe_name(check["name"], "check name")
    if check["kind"] != "path":
        raise PrerequisiteError("path check kind is invalid")
    _absolute_path(check["path"])
    if check["file_type"] not in {"file", "directory", "executable"}:
        raise PrerequisiteError("path check file_type is invalid")
    mode = check["expected_mode"]
    if mode is not None and (
        isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o777
    ):
        raise PrerequisiteError("path check expected_mode is invalid")
    digest = check["sha256"]
    if check["file_type"] in {"file", "executable"}:
        _digest(digest, "path check sha256")
    elif digest is not None:
        raise PrerequisiteError("directory checks cannot declare sha256")
    return check


def _validate_loopback_host(value: Any) -> str:
    if not isinstance(value, str) or value not in {"127.0.0.1", "::1", "localhost"}:
        raise PrerequisiteError("network checks are restricted to loopback")
    return value


def _validate_tcp_check(value: dict[str, Any]) -> dict[str, Any]:
    check = _exact_dict(
        value,
        frozenset({"name", "kind", "host", "port", "timeout_seconds"}),
        "TCP check",
    )
    _safe_name(check["name"], "check name")
    if check["kind"] != "tcp":
        raise PrerequisiteError("TCP check kind is invalid")
    _validate_loopback_host(check["host"])
    port = check["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise PrerequisiteError("TCP check port is invalid")
    _number(check["timeout_seconds"], "TCP timeout")
    return check


def _validate_http_check(value: dict[str, Any]) -> dict[str, Any]:
    check = _exact_dict(
        value,
        frozenset(
            {
                "name",
                "kind",
                "url",
                "method",
                "expected_status",
                "body_sha256",
                "timeout_seconds",
            }
        ),
        "HTTP check",
    )
    _safe_name(check["name"], "check name")
    if check["kind"] != "http":
        raise PrerequisiteError("HTTP check kind is invalid")
    if check["method"] not in {"GET", "HEAD", "OPTIONS"}:
        raise PrerequisiteError("HTTP check method is not read-only")
    if not isinstance(check["url"], str):
        raise PrerequisiteError("HTTP check URL is invalid")
    parsed = urllib.parse.urlsplit(check["url"])
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise PrerequisiteError(
            "HTTP check URL must be an unauthenticated loopback http URL without query or fragment"
        )
    _validate_loopback_host(parsed.hostname)
    try:
        port = parsed.port
    except ValueError as exc:
        raise PrerequisiteError("HTTP check URL port is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise PrerequisiteError("HTTP check URL port is invalid")
    statuses = check["expected_status"]
    if (
        not isinstance(statuses, list)
        or not statuses
        or len(statuses) > 16
        or any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 100 <= item <= 599
            for item in statuses
        )
        or len(set(statuses)) != len(statuses)
    ):
        raise PrerequisiteError("HTTP expected_status is invalid")
    if check["body_sha256"] is not None:
        _digest(check["body_sha256"], "HTTP body_sha256")
    _number(check["timeout_seconds"], "HTTP timeout")
    return check


def _validate_contract(value: Any) -> dict[str, Any]:
    contract = _exact_dict(value, CONTRACT_KEYS, "prerequisite contract")
    if contract["schema"] != CONTRACT_SCHEMA:
        raise PrerequisiteError("prerequisite contract schema is unsupported")
    participant = _safe_name(contract["participant"], "participant")
    if participant not in REQUIRED_PARTICIPANTS:
        raise PrerequisiteError("prerequisite participant is unsupported")
    _agent_id(contract["agent_id"])
    _digest(contract["node_identity_sha256"], "node identity digest")
    checks = contract["checks"]
    if not isinstance(checks, list) or not checks or len(checks) > MAX_CHECKS:
        raise PrerequisiteError("prerequisite checks must be a non-empty bounded list")
    seen: set[str] = set()
    for raw in checks:
        if not isinstance(raw, dict):
            raise PrerequisiteError("prerequisite check must be an object")
        kind = raw.get("kind")
        if kind == "path":
            check = _validate_path_check(raw)
        elif kind == "tcp":
            check = _validate_tcp_check(raw)
        elif kind == "http":
            check = _validate_http_check(raw)
        else:
            raise PrerequisiteError("prerequisite check kind is unsupported")
        if check["name"] in seen:
            raise PrerequisiteError("prerequisite check names must be unique")
        seen.add(check["name"])
    return json.loads(_canonical(contract))


def _hash_file(path: Path, initial: os.stat_result) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (initial.st_dev, initial.st_ino) != (opened.st_dev, opened.st_ino):
            raise PrerequisiteError("path changed while opening for hashing")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        final = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ):
            raise PrerequisiteError("path changed while hashing")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _probe_path(check: dict[str, Any]) -> dict[str, Any]:
    path = _absolute_path(check["path"])
    try:
        initial = os.lstat(path)
    except OSError as exc:
        raise PrerequisiteError(f"path check {check['name']} is unavailable") from exc
    if stat.S_ISLNK(initial.st_mode):
        raise PrerequisiteError(f"path check {check['name']} must not be a symlink")
    file_type = check["file_type"]
    if file_type == "directory":
        if not stat.S_ISDIR(initial.st_mode):
            raise PrerequisiteError(f"path check {check['name']} is not a directory")
        actual_sha = None
    else:
        if not stat.S_ISREG(initial.st_mode):
            raise PrerequisiteError(f"path check {check['name']} is not a regular file")
        if file_type == "executable" and initial.st_mode & 0o111 == 0:
            raise PrerequisiteError(f"path check {check['name']} is not executable")
        try:
            actual_sha = _hash_file(path, initial)
        except OSError as exc:
            raise PrerequisiteError(
                f"path check {check['name']} could not be hashed safely"
            ) from exc
        if actual_sha != check["sha256"]:
            raise PrerequisiteError(f"path check {check['name']} digest differs")
        final = os.lstat(path)
        if (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns) != (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
        ):
            raise PrerequisiteError(f"path check {check['name']} changed while hashing")
    actual_mode = stat.S_IMODE(initial.st_mode)
    if check["expected_mode"] is not None and actual_mode != check["expected_mode"]:
        raise PrerequisiteError(f"path check {check['name']} mode differs")
    return {
        "file_type": file_type,
        "mode": actual_mode,
        "sha256": actual_sha,
        "uid": initial.st_uid,
        "gid": initial.st_gid,
    }


def _probe_tcp(check: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with socket.create_connection(
            (check["host"], check["port"]), check["timeout_seconds"]
        ):
            pass
    except OSError as exc:
        raise PrerequisiteError(f"TCP check {check['name']} failed") from exc
    return {"connected": True, "elapsed_ms": int((time.monotonic() - started) * 1000)}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect refused", headers, fp
        )


def _probe_http(check: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(check["url"], method=check["method"])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    started = time.monotonic()
    try:
        with opener.open(request, timeout=check["timeout_seconds"]) as response:
            status_code = response.status
            raw = response.read(MAX_HTTP_BODY_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise PrerequisiteError(f"HTTP check {check['name']} failed") from exc
    if len(raw) > MAX_HTTP_BODY_BYTES:
        raise PrerequisiteError(f"HTTP check {check['name']} response is too large")
    if status_code not in check["expected_status"]:
        raise PrerequisiteError(f"HTTP check {check['name']} status differs")
    body_sha256 = hashlib.sha256(raw).hexdigest()
    if check["body_sha256"] is not None and body_sha256 != check["body_sha256"]:
        raise PrerequisiteError(f"HTTP check {check['name']} body digest differs")
    return {
        "status": status_code,
        "body_sha256": body_sha256,
        "body_bytes": len(raw),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def verify_contract(contract: Any, *, now: float | None = None) -> dict[str, Any]:
    parsed = _validate_contract(contract)
    evidence: list[dict[str, str]] = []
    for check in parsed["checks"]:
        if check["kind"] == "path":
            observation = _probe_path(check)
        elif check["kind"] == "tcp":
            observation = _probe_tcp(check)
        else:
            observation = _probe_http(check)
        evidence.append(
            {
                "name": check["name"],
                "kind": check["kind"],
                "evidence_sha256": _sha256(observation),
            }
        )
    return {
        "schema": RECEIPT_SCHEMA,
        "participant": parsed["participant"],
        "agent_id": parsed["agent_id"],
        "node_identity_sha256": parsed["node_identity_sha256"],
        "contract_sha256": _sha256(parsed),
        "observed_at_epoch": float(time.time() if now is None else now),
        "status": "ready",
        "checks": evidence,
    }


def validate_receipt(value: Any) -> dict[str, Any]:
    receipt = _exact_dict(value, RECEIPT_KEYS, "prerequisite receipt")
    if receipt["schema"] != RECEIPT_SCHEMA:
        raise PrerequisiteError("prerequisite receipt schema is unsupported")
    participant = _safe_name(receipt["participant"], "participant")
    if participant not in REQUIRED_PARTICIPANTS:
        raise PrerequisiteError("prerequisite receipt participant is unsupported")
    _agent_id(receipt["agent_id"])
    _digest(receipt["node_identity_sha256"], "node identity digest")
    _digest(receipt["contract_sha256"], "contract digest")
    observed = receipt["observed_at_epoch"]
    if (
        isinstance(observed, bool)
        or not isinstance(observed, (int, float))
        or observed <= 0
    ):
        raise PrerequisiteError("receipt observation time is invalid")
    if receipt["status"] != "ready":
        raise PrerequisiteError("prerequisite receipt is not ready")
    checks = receipt["checks"]
    if not isinstance(checks, list) or not checks or len(checks) > MAX_CHECKS:
        raise PrerequisiteError("receipt checks must be a non-empty bounded list")
    seen: set[str] = set()
    for raw in checks:
        check = _exact_dict(raw, RECEIPT_CHECK_KEYS, "receipt check")
        name = _safe_name(check["name"], "receipt check name")
        if name in seen:
            raise PrerequisiteError("receipt check names must be unique")
        seen.add(name)
        if check["kind"] not in {"path", "tcp", "http"}:
            raise PrerequisiteError("receipt check kind is unsupported")
        _digest(check["evidence_sha256"], "receipt evidence digest")
    return json.loads(_canonical(receipt))


def build_bundle(
    receipts: Iterable[Any],
    *,
    agent_id: str,
    node_identity_sha256: str,
    now: float | None = None,
) -> dict[str, Any]:
    expected_agent = _agent_id(agent_id)
    expected_identity = _digest(node_identity_sha256, "node identity digest")
    parsed = [validate_receipt(value) for value in receipts]
    by_participant: dict[str, dict[str, Any]] = {}
    for receipt in parsed:
        participant = receipt["participant"]
        if participant in by_participant:
            raise PrerequisiteError(
                "prerequisite bundle contains a duplicate participant"
            )
        if receipt["agent_id"] != expected_agent:
            raise PrerequisiteError("prerequisite receipt agent differs")
        if receipt["node_identity_sha256"] != expected_identity:
            raise PrerequisiteError("prerequisite receipt node identity differs")
        by_participant[participant] = receipt
    if frozenset(by_participant) != frozenset(REQUIRED_PARTICIPANTS):
        raise PrerequisiteError("prerequisite bundle participant set is incomplete")
    return {
        "schema": BUNDLE_SCHEMA,
        "agent_id": expected_agent,
        "node_identity_sha256": expected_identity,
        "created_at_epoch": float(time.time() if now is None else now),
        "receipts": [by_participant[name] for name in REQUIRED_PARTICIPANTS],
    }


def validate_expectations(value: Any) -> dict[str, Any]:
    expectations = _exact_dict(value, EXPECTATIONS_KEYS, "prerequisite expectations")
    if expectations["schema"] != EXPECTATIONS_SCHEMA:
        raise PrerequisiteError("prerequisite expectations schema is unsupported")
    _agent_id(expectations["agent_id"])
    _digest(expectations["node_identity_sha256"], "node identity digest")
    contracts = expectations["contracts"]
    if not isinstance(contracts, dict) or frozenset(contracts) != frozenset(
        REQUIRED_PARTICIPANTS
    ):
        raise PrerequisiteError("prerequisite expected contract set is incomplete")
    for participant in REQUIRED_PARTICIPANTS:
        _digest(contracts[participant], f"{participant} contract digest")
    return json.loads(_canonical(expectations))


def validate_bundle(
    value: Any,
    *,
    agent_id: str,
    node_identity_sha256: str,
    max_age_seconds: float,
    expectations: Any | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    bundle = _exact_dict(value, BUNDLE_KEYS, "prerequisite bundle")
    if bundle["schema"] != BUNDLE_SCHEMA:
        raise PrerequisiteError("prerequisite bundle schema is unsupported")
    expected_agent = _agent_id(agent_id)
    expected_identity = _digest(node_identity_sha256, "node identity digest")
    if bundle["agent_id"] != expected_agent:
        raise PrerequisiteError("prerequisite bundle agent differs")
    if bundle["node_identity_sha256"] != expected_identity:
        raise PrerequisiteError("prerequisite bundle node identity differs")
    if not 0 < max_age_seconds <= 3600:
        raise PrerequisiteError("max receipt age must be positive and at most one hour")
    current = float(time.time() if now is None else now)
    created = bundle["created_at_epoch"]
    if (
        isinstance(created, bool)
        or not isinstance(created, (int, float))
        or created <= 0
    ):
        raise PrerequisiteError("prerequisite bundle creation time is invalid")
    if created > current + 5 or current - created > max_age_seconds:
        raise PrerequisiteError("prerequisite bundle is stale or from the future")
    rebuilt = build_bundle(
        bundle["receipts"],
        agent_id=expected_agent,
        node_identity_sha256=expected_identity,
        now=float(created),
    )
    for receipt in rebuilt["receipts"]:
        observed = float(receipt["observed_at_epoch"])
        if observed > current + 5 or current - observed > max_age_seconds:
            raise PrerequisiteError(
                f"prerequisite receipt {receipt['participant']} is stale or from the future"
            )
        if observed > created + 5:
            raise PrerequisiteError("prerequisite receipt postdates its bundle")
    if rebuilt != bundle:
        raise PrerequisiteError("prerequisite bundle is not canonical")
    if expectations is not None:
        expected = validate_expectations(expectations)
        if expected["agent_id"] != expected_agent:
            raise PrerequisiteError("prerequisite expectations agent differs")
        if expected["node_identity_sha256"] != expected_identity:
            raise PrerequisiteError("prerequisite expectations node identity differs")
        for receipt in rebuilt["receipts"]:
            if (
                receipt["contract_sha256"]
                != expected["contracts"][receipt["participant"]]
            ):
                raise PrerequisiteError(
                    f"prerequisite receipt {receipt['participant']} uses an unexpected contract"
                )
    return rebuilt


def bundle_summary(
    bundle: dict[str, Any], *, expectations: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = {
        "schema": SUMMARY_SCHEMA,
        "agent_id": bundle["agent_id"],
        "node_identity_sha256": bundle["node_identity_sha256"],
        "bundle_sha256": _sha256(bundle),
        "participants": [
            {
                "participant": receipt["participant"],
                "receipt_sha256": _sha256(receipt),
                "contract_sha256": receipt["contract_sha256"],
            }
            for receipt in bundle["receipts"]
        ],
    }
    if expectations is not None:
        result["expectations_sha256"] = _sha256(expectations)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser("verify")
    verify.add_argument("--contract", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)

    bundle = commands.add_parser("bundle")
    bundle.add_argument("--receipt", type=Path, action="append", required=True)
    bundle.add_argument("--agent-id", required=True)
    bundle.add_argument("--node-identity-sha256", required=True)
    bundle.add_argument("--output", type=Path, required=True)

    validate = commands.add_parser("validate-bundle")
    validate.add_argument("--bundle", type=Path, required=True)
    validate.add_argument("--expectations", type=Path, required=True)
    validate.add_argument("--agent-id", required=True)
    validate.add_argument("--node-identity-sha256", required=True)
    validate.add_argument("--max-age-seconds", type=float, default=300.0)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            contract = _read_private_json(
                args.contract.expanduser(), "prerequisite contract"
            )
            receipt = verify_contract(contract)
            _atomic_private_json(args.output, receipt)
            result: Any = receipt
        elif args.command == "bundle":
            receipts = [
                _read_private_json(path.expanduser(), "prerequisite receipt")
                for path in args.receipt
            ]
            result = build_bundle(
                receipts,
                agent_id=args.agent_id,
                node_identity_sha256=args.node_identity_sha256,
            )
            _atomic_private_json(args.output, result)
        else:
            raw = _read_private_json(args.bundle.expanduser(), "prerequisite bundle")
            expectations = _read_private_json(
                args.expectations.expanduser(), "prerequisite expectations"
            )
            bundle = validate_bundle(
                raw,
                agent_id=args.agent_id,
                node_identity_sha256=args.node_identity_sha256,
                max_age_seconds=args.max_age_seconds,
                expectations=expectations,
            )
            result = bundle_summary(
                bundle, expectations=validate_expectations(expectations)
            )
    except PrerequisiteError as exc:
        print(f"fleet prerequisite error: {exc}", file=sys.stderr)
        return 4
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
