#!/usr/bin/env python3
"""Fail-closed local client for the synchronized fleet release epoch API.

The deployment controller runs this helper on the hub host and talks only to
the hub's loopback listener.  Bearers and request bodies are accepted through
owner-private regular files; response receipts are written atomically with
mode 0600.  Error messages deliberately omit response bodies and credentials.
A small allowlist admits only bounded, plain-text fleet-release validation
details so operators can distinguish which rollout invariant failed.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import string
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


MAX_INPUT_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
AUTHORITY_SCHEMA = "mac.fleet_release_hub_authority.v1"
PARTICIPANT_STATE_SCHEMA = "mac.fleet_release_participant_state.v1"
RECEIPT_SCHEMA = "mac.fleet_release_epoch_receipt.v1"
SAFE_STATUSES = frozenset({"absent", "mismatch", "open", "proved", "committed", "aborted"})
SAFE_ERROR_DETAIL_PREFIXES = (
    "aborted ",
    "attestation ",
    "committed ",
    "fleet release ",
    "report executor ",
    "staged ",
    "worker ",
)
SAFE_ERROR_DETAIL_CHARACTERS = frozenset(
    string.ascii_letters + string.digits + " ._:-"
)


class ClientError(ValueError):
    """The local epoch-client contract was violated."""


def _private_regular(path: Path, label: str) -> os.stat_result:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise ClientError(f"{label} is unreadable") from exc
    if (
        not stat.S_ISREG(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or observed.st_nlink != 1
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) & 0o077
        or observed.st_size < 1
        or observed.st_size > MAX_INPUT_BYTES
    ):
        raise ClientError(f"{label} is not a bounded owner-private regular file")
    return observed


def _read_private(path: Path, label: str) -> bytes:
    before = _private_regular(path, label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        observed = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (observed.st_dev, observed.st_ino)
            or observed.st_nlink != 1
            or observed.st_uid != os.getuid()
        ):
            raise ClientError(f"{label} identity changed while opening")
        raw = bytearray()
        while len(raw) < observed.st_size:
            chunk = os.read(
                descriptor, min(64 * 1024, observed.st_size - len(raw))
            )
            if not chunk:
                raise ClientError(f"{label} was truncated")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise ClientError(f"{label} grew while reading")
        after = os.fstat(descriptor)
        if (
            observed.st_dev,
            observed.st_ino,
            observed.st_nlink,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ClientError(f"{label} changed while reading")
        return bytes(raw)
    finally:
        os.close(descriptor)


def _json_private(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_private(path, label))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ClientError(f"{label} root must be an object")
    return value


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = path.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = destination.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise ClientError("receipt directory is not owner-private")
    descriptor, raw = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    temporary = Path(raw)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(value), stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
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


def _hub_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ClientError("hub URL must be an unauthenticated loopback HTTP origin")
    try:
        parsed.port
    except ValueError as exc:
        raise ClientError("hub URL port is invalid") from exc
    return value


def _token(path: Path) -> str:
    try:
        token = _read_private(path, "token file").decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ClientError("token file is not valid UTF-8") from exc
    if not token or len(token.encode()) > 8192 or any(character.isspace() for character in token):
        raise ClientError("token file has an unsafe token shape")
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise ClientError("hub API redirects are forbidden")


def _safe_http_error_detail(raw: bytes) -> str:
    """Return one secret-safe fleet-release validation detail, or nothing."""

    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(value, dict) or set(value) != {"detail"}:
        return ""
    detail = value.get("detail")
    if (
        not isinstance(detail, str)
        or not 1 <= len(detail.encode("utf-8")) <= 512
        or not detail.startswith(SAFE_ERROR_DETAIL_PREFIXES)
        or any(character not in SAFE_ERROR_DETAIL_CHARACTERS for character in detail)
    ):
        return ""
    return detail


def _request(
    hub_url: str,
    token: str,
    method: str,
    path: str,
    body: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    data = None
    if body is not None:
        data = json.dumps(dict(body), sort_keys=True, separators=(",", ":")).encode()
        if len(data) > MAX_INPUT_BYTES:
            raise ClientError("hub request exceeds its size bound")
    request = urllib.request.Request(
        hub_url + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # Consume a bounded amount so the connection can close. Only an exact,
        # allowlisted plain-text validation detail may reach controller logs;
        # arbitrary server bodies and structured validation payloads stay
        # opaque because they can contain request fragments or credentials.
        detail = _safe_http_error_detail(exc.read(4096))
        suffix = f": {detail}" if detail else ""
        raise ClientError(
            f"hub API {method} request failed with HTTP {exc.code}{suffix}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ClientError(f"hub API {method} request failed before a response") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ClientError("hub API response exceeds its size bound")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientError("hub API response is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ClientError("hub API response root must be an object")
    return value


def _digest(value: Any, label: str) -> str:
    text = str(value or "")
    if (
        len(text) != 71
        or not text.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in text[7:])
    ):
        raise ClientError(f"{label} is not a canonical SHA-256 digest")
    return text


def _epoch(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text.encode()) > 512 or any(not char.isprintable() for char in text):
        raise ClientError("epoch id is invalid")
    return text


def _authority(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {"schema", "hub_authority_id"} or value.get("schema") != AUTHORITY_SCHEMA:
        raise ClientError("hub authority response schema is not exact")
    try:
        authority_id = str(uuid.UUID(str(value.get("hub_authority_id")))).lower()
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClientError("hub authority response does not contain a UUID") from exc
    return {"schema": AUTHORITY_SCHEMA, "hub_authority_id": authority_id}


def _participant_state(value: Mapping[str, Any], expected_agent_id: str) -> dict[str, Any]:
    if value.get("id") != expected_agent_id:
        raise ClientError("hub returned the wrong participant")
    baseline = str(value.get("last_seen_at") or "").strip()
    if not baseline or len(baseline.encode()) > 128:
        raise ClientError("hub participant lacks a bounded heartbeat baseline")
    held = value.get("dispatch_hold")
    if not isinstance(held, bool):
        raise ClientError("hub participant hold state is not boolean")
    reason = value.get("dispatch_hold_reason")
    held_at = value.get("dispatch_hold_at")
    if held:
        if not isinstance(reason, str) or not reason.strip() or not isinstance(held_at, str) or not held_at.strip():
            raise ClientError("held hub participant lacks exact ownership")
    elif reason is not None or held_at is not None:
        raise ClientError("unheld hub participant has stray hold ownership")
    return {
        "schema": PARTICIPANT_STATE_SCHEMA,
        "agent_id": expected_agent_id,
        "baseline_seen": baseline,
        "expected_dispatch_hold": held,
        "expected_hold_reason": reason,
        "expected_hold_at": held_at,
    }


def _receipt(
    value: Mapping[str, Any],
    *,
    expected_epoch: str,
    expected_status: str,
    expected_identity: str = "",
) -> dict[str, Any]:
    if value.get("schema") != RECEIPT_SCHEMA:
        raise ClientError("hub epoch response schema is unsupported")
    if value.get("status") != expected_status or value.get("epoch_id") != expected_epoch:
        raise ClientError("hub epoch response differs from the requested transition")
    try:
        str(uuid.UUID(str(value.get("hub_authority_id"))))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClientError("hub epoch response authority is invalid") from exc
    identity = _digest(value.get("identity_sha256"), "hub epoch identity")
    if expected_identity and identity != expected_identity:
        raise ClientError("hub epoch identity changed")
    if expected_status in {"proved", "committed"}:
        _digest(value.get("proof_sha256"), "hub epoch proof")
    elif expected_status == "open" and "proof_sha256" in value:
        raise ClientError("open hub epoch response contains a proof")
    agents = value.get("agents")
    if not isinstance(agents, list) or value.get("cohort_size") != len(agents):
        raise ClientError("hub epoch response cohort is malformed")
    return dict(value)


def _status(
    value: Mapping[str, Any], *, expected_epoch: str, expected_identity: str
) -> dict[str, Any]:
    status = str(value.get("status") or "")
    if status not in SAFE_STATUSES or value.get("epoch_id") != expected_epoch:
        raise ClientError("hub epoch status response is invalid")
    if value.get("identity_sha256") != expected_identity:
        raise ClientError("hub epoch status identity changed")
    if status in {"absent", "mismatch"}:
        if set(value) != {"status", "epoch_id", "hub_authority_id", "identity_sha256"}:
            raise ClientError("hub absent or mismatch response schema is not exact")
        try:
            str(uuid.UUID(str(value.get("hub_authority_id"))))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ClientError("hub epoch status authority is invalid") from exc
        return dict(value)
    return _receipt(
        value,
        expected_epoch=expected_epoch,
        expected_status=status,
        expected_identity=expected_identity,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub-url", default="http://127.0.0.1:8789")
    parser.add_argument("--token-file", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    authority = sub.add_parser("authority")
    authority.add_argument("--output", required=True)
    participant = sub.add_parser("participant-state")
    participant.add_argument("--agent-id", required=True)
    participant.add_argument("--output", required=True)
    for command in ("open", "prove", "commit", "abort"):
        action = sub.add_parser(command)
        action.add_argument("--epoch", required=True)
        action.add_argument("--request-file", required=True)
        action.add_argument("--output", required=True)
    status = sub.add_parser("status")
    status.add_argument("--epoch", required=True)
    status.add_argument("--identity-sha256", required=True)
    status.add_argument("--output", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        hub = _hub_url(args.hub_url)
        token = _token(Path(args.token_file))
        if args.command == "authority":
            result = _authority(_request(hub, token, "GET", "/agents/dispatch-hold/authority"))
        elif args.command == "participant-state":
            agent_id = str(args.agent_id or "").strip()
            if not agent_id or len(agent_id.encode()) > 512:
                raise ClientError("participant agent id is invalid")
            result = _participant_state(
                _request(
                    hub,
                    token,
                    "GET",
                    "/agents/" + urllib.parse.quote(agent_id, safe=""),
                ),
                agent_id,
            )
        elif args.command == "status":
            epoch = _epoch(args.epoch)
            identity = _digest(args.identity_sha256, "expected hub epoch identity")
            query = urllib.parse.urlencode({"identity_sha256": identity})
            result = _status(
                _request(
                    hub,
                    token,
                    "GET",
                    "/agents/dispatch-hold/epochs/" + urllib.parse.quote(epoch, safe="") + "?" + query,
                ),
                expected_epoch=epoch,
                expected_identity=identity,
            )
        else:
            epoch = _epoch(args.epoch)
            request_body = _json_private(Path(args.request_file), "request file")
            if args.command == "open":
                if request_body.get("epoch_id") != epoch:
                    raise ClientError("open request epoch does not match")
                path = "/agents/dispatch-hold/epochs/open"
                expected_identity = ""
            else:
                expected_identity = _digest(
                    request_body.get("identity_sha256"), "request identity"
                )
                path = (
                    "/agents/dispatch-hold/epochs/"
                    + urllib.parse.quote(epoch, safe="")
                    + "/"
                    + args.command
                )
            result = _receipt(
                _request(hub, token, "POST", path, request_body),
                expected_epoch=epoch,
                expected_status={"open": "open", "prove": "proved", "commit": "committed", "abort": "aborted"}[args.command],
                expected_identity=expected_identity,
            )
        _atomic_private_json(Path(args.output), result)
        print(json.dumps({"status": result.get("status", "ready"), "receipt_written": True}, sort_keys=True))
        return 0
    except ClientError as exc:
        print(f"fleet release epoch client error: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
