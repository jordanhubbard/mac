#!/usr/bin/env python3
"""Run and receipt the fleet's read-only pre-publication qualification."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import tempfile
import time
from typing import Any


UPSTREAM_SCHEMA = "mac.fleet_preflight_qualification.v1"
WRAPPER_SCHEMA = "mac.prepublication_fleet_qualification.v1"
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
FORBIDDEN_KEYS = (
    "token",
    "password",
    "secret",
    "private_key",
    "credential",
    "authorization",
)


class QualificationError(ValueError):
    """The local or upstream qualification evidence is unsafe."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso8601(value: dt.datetime) -> str:
    return (
        value.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_time(raw: Any) -> dt.datetime:
    if not isinstance(raw, str) or len(raw) > 64:
        raise QualificationError("fleet qualification timestamp is invalid")
    try:
        value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QualificationError("fleet qualification timestamp is invalid") from exc
    if value.tzinfo is None:
        raise QualificationError("fleet qualification timestamp lacks a timezone")
    return value.astimezone(dt.timezone.utc)


def private_bytes(path: Path, label: str, *, exact_mode: int = 0o600) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise QualificationError(f"{label} is unreadable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != exact_mode
            or before.st_size <= 0
            or before.st_size > MAX_RECEIPT_BYTES
        ):
            raise QualificationError(f"{label} is not an owner-private bounded file")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(1024 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise QualificationError(f"{label} changed while reading")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise QualificationError(f"{label} grew while reading")
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
            raise QualificationError(f"{label} changed while reading")
        return bytes(raw)
    finally:
        os.close(descriptor)


def controlled_bytes(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise QualificationError(f"{label} is unreadable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or before.st_size <= 0
            or before.st_size > MAX_RECEIPT_BYTES
        ):
            raise QualificationError(f"{label} is not an owner-controlled bounded file")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(1024 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise QualificationError(f"{label} changed while reading")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise QualificationError(f"{label} grew while reading")
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
            raise QualificationError(f"{label} changed while reading")
        return bytes(raw)
    finally:
        os.close(descriptor)


def atomic_private_json(path: Path, value: dict[str, Any]) -> None:
    destination = path.expanduser().absolute()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = destination.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise QualificationError(
            "prepublication receipt directory is not owner-private"
        )
    try:
        current = destination.lstat()
    except FileNotFoundError:
        current = None
    if current is not None and (
        not stat.S_ISREG(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or current.st_uid != os.getuid()
        or current.st_nlink != 1
    ):
        raise QualificationError("existing prepublication receipt path is unsafe")
    descriptor, raw = tempfile.mkstemp(
        prefix=destination.name + ".", dir=destination.parent
    )
    temporary = Path(raw)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_receipt(value: Any, path: str = "receipt") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise QualificationError(
                    "fleet qualification contains a non-string key"
                )
            lowered = key.lower()
            if any(forbidden in lowered for forbidden in FORBIDDEN_KEYS):
                raise QualificationError(
                    f"fleet qualification contains forbidden material at {path}.{key}"
                )
            _safe_receipt(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _safe_receipt(item, f"{path}[{index}]")
    elif isinstance(value, str):
        if len(value.encode()) > 64 * 1024 or "\x00" in value:
            raise QualificationError("fleet qualification contains an unsafe string")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise QualificationError("fleet qualification contains an unsupported value")


def _plain_digest(raw: Any, label: str) -> str:
    value = str(raw or "")
    if not DIGEST.fullmatch(value):
        raise QualificationError(f"{label} is not a SHA-256 digest")
    return value.removeprefix("sha256:")


def _agent_identity(value: Any, label: str) -> tuple[str, str, str]:
    if not isinstance(value, dict):
        raise QualificationError(f"{label} is not an object")
    name = str(value.get("name") or "")
    stable_id = str(value.get("stable_id") or "")
    generation = str(value.get("generation") or "")
    if (
        not SAFE_NAME.fullmatch(name)
        or not SAFE_NAME.fullmatch(stable_id)
        or not generation
        or len(generation.encode()) > 256
        or any(not character.isprintable() for character in generation)
    ):
        raise QualificationError(f"{label} identity is invalid")
    return name, stable_id, generation


def _validate_endpoint_identity(value: dict[str, Any], label: str) -> str:
    if set(value) != {"schema", "adapter", "authority", "observation"}:
        raise QualificationError(f"{label} keys differ from endpoint schema")
    if value.get("schema") != "mac.fleet_endpoint_identity.v1":
        raise QualificationError(f"{label} schema is unsupported")
    adapter = value.get("adapter")
    authority = value.get("authority")
    observation = value.get("observation")
    if not isinstance(authority, dict) or not isinstance(observation, dict):
        raise QualificationError(f"{label} authority or observation is invalid")
    if adapter in {"ssh-machine", "ssh-hub"}:
        keys = {"ssh_host_key_sha256", "instance_id_kind", "instance_id_sha256"}
        if adapter == "ssh-hub":
            keys.add("durable_store_uuid_sha256")
        if set(authority) != keys or observation:
            raise QualificationError(f"{label} SSH identity shape is invalid")
        digest_keys = keys - {"instance_id_kind"}
        if not SAFE_NAME.fullmatch(str(authority.get("instance_id_kind") or "")):
            raise QualificationError(f"{label} instance identity kind is invalid")
    elif adapter in {"kubernetes-workload", "kubernetes-hub"}:
        keys = {"cluster_uid_sha256", "workload_kind", "workload_uid_sha256"}
        if adapter == "kubernetes-hub":
            keys.add("durable_store_uuid_sha256")
        if set(authority) != keys or set(observation) != {"pod_uid_sha256"}:
            raise QualificationError(f"{label} Kubernetes identity shape is invalid")
        digest_keys = keys - {"workload_kind"}
        if not SAFE_NAME.fullmatch(str(authority.get("workload_kind") or "")):
            raise QualificationError(f"{label} workload kind is invalid")
        if not HEX_SHA256.fullmatch(str(observation.get("pod_uid_sha256") or "")):
            raise QualificationError(f"{label} pod identity digest is invalid")
    else:
        raise QualificationError(f"{label} adapter is unsupported")
    if any(
        not HEX_SHA256.fullmatch(str(authority.get(key) or "")) for key in digest_keys
    ):
        raise QualificationError(f"{label} authority digest is invalid")
    return digest(canonical({"adapter": adapter, "authority": authority}))


def _validate_reviewed_cli(probe: dict[str, Any]) -> None:
    reviewed = probe.get("reviewed_openshell_cli")
    if not isinstance(reviewed, dict):
        raise QualificationError("agent probe lacks reviewed OpenShell CLI evidence")
    if _plain_digest(
        probe.get("reviewed_openshell_cli_sha256"),
        "reviewed OpenShell CLI digest",
    ) != digest(canonical(reviewed)):
        raise QualificationError("reviewed OpenShell CLI payload digest differs")
    if (
        reviewed.get("schema") != "mac.reviewed_openshell_cli_preflight.v1"
        or reviewed.get("status") != "ready"
        or not isinstance(reviewed.get("required"), bool)
        or not isinstance(reviewed.get("managed_openclaw"), bool)
        or not SAFE_NAME.fullmatch(str(reviewed.get("expected_os") or ""))
        or not SAFE_NAME.fullmatch(str(reviewed.get("arch") or ""))
        or not SAFE_NAME.fullmatch(str(reviewed.get("version") or ""))
        or not re.fullmatch(
            r"openshell-[A-Za-z0-9._-]+\.tar\.gz",
            str(reviewed.get("asset") or ""),
        )
        or not HEX_SHA256.fullmatch(str(reviewed.get("asset_sha256") or ""))
    ):
        raise QualificationError("reviewed OpenShell CLI evidence is invalid")
    reason = reviewed.get("reason")
    if reason == "reviewed_cli_ready":
        if (
            reviewed.get("required") is not True
            or not HEX_SHA256.fullmatch(str(reviewed.get("cli_sha256") or ""))
            or not HEX_SHA256.fullmatch(str(reviewed.get("receipt_sha256") or ""))
        ):
            raise QualificationError("reviewed OpenShell CLI ready evidence is invalid")
    elif reason == "openclaw_not_managed":
        if (
            reviewed.get("required") is not False
            or reviewed.get("managed_openclaw") is not False
        ):
            raise QualificationError("optional OpenShell CLI evidence is inconsistent")
    else:
        raise QualificationError("reviewed OpenShell CLI reason is unsupported")


def validate_upstream(
    value: dict[str, Any],
    *,
    revision: str,
    hub: str,
    registry_sha256: str,
    requested_agents: list[str],
    max_age: int,
) -> dict[str, Any]:
    _safe_receipt(value)
    if (
        value.get("schema") != UPSTREAM_SCHEMA
        or value.get("status") != "passed"
        or value.get("read_only") is not True
        or value.get("authorizes_deployment") is not False
        or value.get("source_revision") != revision
        or value.get("hub_agent") != hub
        or not SAFE_NAME.fullmatch(str(value.get("fleet_name") or ""))
        or _plain_digest(value.get("fleet_registry_sha256"), "fleet registry digest")
        != registry_sha256
    ):
        raise QualificationError(
            "fleet qualification identity differs from local inputs"
        )
    for key in ("selected_specs_sha256", "probe_helper_sha256"):
        _plain_digest(value.get(key), key)
    qualified_at = parse_time(value.get("qualified_at"))
    now = utc_now()
    if qualified_at > now + dt.timedelta(
        seconds=30
    ) or qualified_at < now - dt.timedelta(seconds=max_age):
        raise QualificationError("fleet qualification is stale or from the future")

    hub_endpoint = value.get("hub_endpoint_identity")
    if not isinstance(hub_endpoint, dict) or not hub_endpoint:
        raise QualificationError("fleet qualification lacks hub endpoint identity")
    _validate_endpoint_identity(hub_endpoint, "hub endpoint identity")
    if hub_endpoint.get("adapter") not in {"ssh-hub", "kubernetes-hub"}:
        raise QualificationError("hub endpoint identity lacks hub authority")
    if _plain_digest(
        value.get("hub_endpoint_identity_sha256"),
        "hub endpoint identity digest",
    ) != digest(canonical(hub_endpoint)):
        raise QualificationError("hub endpoint identity payload digest differs")
    _plain_digest(
        value.get("hub_endpoint_identity_file_sha256"),
        "hub endpoint identity file digest",
    )

    agents = value.get("agents")
    if not isinstance(agents, list) or not agents:
        raise QualificationError("fleet qualification lacks selected agent evidence")
    selected_identities = [_agent_identity(item, "selected agent") for item in agents]
    if len(set(selected_identities)) != len(selected_identities):
        raise QualificationError("fleet qualification repeats a selected agent")
    selected_names = [identity[0] for identity in selected_identities]
    if len(set(requested_agents)) != len(requested_agents):
        raise QualificationError("requested agent selection contains duplicates")
    if requested_agents and set(selected_names) != set(requested_agents):
        raise QualificationError(
            "fleet qualification selection differs from requested agents"
        )
    endpoint_authorities: set[str] = set()
    for item, identity in zip(agents, selected_identities, strict=True):
        identity = _agent_identity(item, "agent evidence")
        endpoint = item.get("endpoint_identity") if isinstance(item, dict) else None
        probe = item.get("probe_evidence") if isinstance(item, dict) else None
        if (
            not isinstance(endpoint, dict)
            or not endpoint
            or not isinstance(probe, dict)
            or not probe
        ):
            raise QualificationError(
                "fleet qualification has incomplete per-agent evidence"
            )
        endpoint_digest = _plain_digest(
            item.get("endpoint_identity_sha256"), "endpoint identity digest"
        )
        _plain_digest(
            item.get("endpoint_identity_file_sha256"),
            "endpoint identity file digest",
        )
        if endpoint_digest != digest(canonical(endpoint)):
            raise QualificationError("endpoint identity payload digest differs")
        endpoint_authority = _validate_endpoint_identity(
            endpoint, f"{identity[0]} endpoint identity"
        )
        if endpoint_authority in endpoint_authorities:
            raise QualificationError("selected agents resolve to one physical endpoint")
        endpoint_authorities.add(endpoint_authority)
        if _plain_digest(
            item.get("probe_evidence_sha256"), "probe evidence digest"
        ) != digest(canonical(probe)):
            raise QualificationError("probe evidence payload digest differs")
        _plain_digest(
            item.get("probe_evidence_file_sha256"), "probe evidence file digest"
        )
        if (
            probe.get("schema") != "mac.fleet_preflight_node_probe.v1"
            or probe.get("status") != "passed"
            or probe.get("read_only") is not True
            or probe.get("agent") != identity[0]
            or probe.get("stable_id") != identity[1]
            or str(probe.get("generation") or "") != identity[2]
            or probe.get("source_revision") != revision
        ):
            raise QualificationError("agent probe differs from selected agent identity")
        checks = probe.get("checks")
        if (
            not isinstance(checks, dict)
            or not checks
            or any(result is not True for result in checks.values())
        ):
            raise QualificationError("agent probe contains a failed readiness check")
        _validate_reviewed_cli(probe)
        platform = probe.get("platform")
        reviewed = probe["reviewed_openshell_cli"]
        if not isinstance(platform, dict) or platform.get("configured") != reviewed.get(
            "expected_os"
        ):
            raise QualificationError("reviewed CLI platform differs from node probe")
    return value


def git_revision(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not SHA40.fullmatch(value):
        raise QualificationError("could not resolve the local Git revision")
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if dirty.returncode != 0 or dirty.stdout.strip():
        raise QualificationError(
            "tracked worktree changes are not frozen in the source revision"
        )
    return value


def run_bounded(
    command: list[str], *, cwd: Path, timeout: int
) -> subprocess.CompletedProcess[bytes]:
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise QualificationError(
            "could not start read-only fleet qualification"
        ) from exc
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QualificationError("read-only fleet qualification timed out")
            events = selector.select(min(remaining, 1.0))
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[key.data].extend(chunk)
                if sum(len(value) for value in buffers.values()) > MAX_CAPTURE_BYTES:
                    raise QualificationError(
                        "fleet preflight output exceeded its bound"
                    )
        returncode = process.wait(timeout=max(1, int(deadline - time.monotonic())))
        return subprocess.CompletedProcess(
            command,
            returncode,
            bytes(buffers["stdout"]),
            bytes(buffers["stderr"]),
        )
    except (subprocess.TimeoutExpired, QualificationError) as exc:
        process.kill()
        process.wait()
        if isinstance(exc, QualificationError):
            raise
        raise QualificationError("read-only fleet qualification timed out") from exc
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def run_qualification(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.expanduser().resolve()
    deploy = args.deploy_script or root / "deploy" / "deploy-mac-fleet.sh"
    deploy = deploy.expanduser().absolute()
    deploy_raw = controlled_bytes(deploy, "fleet deployment entrypoint")
    if not os.access(deploy, os.X_OK):
        raise QualificationError("fleet deployment entrypoint is not executable")
    registry = args.fleets_config or Path(
        os.environ.get("MAC_DEPLOY_FLEETS_CONFIG", "~/.mac/fleets.yaml")
    )
    registry = registry.expanduser().absolute()
    registry_raw = controlled_bytes(registry, "fleet registry")
    registry_digest = digest(registry_raw)
    revision = git_revision(root)
    output = args.output.expanduser().absolute()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = output.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise QualificationError(
            "prepublication receipt directory is not owner-private"
        )
    raw_receipt = output.parent / ("." + output.name + ".upstream")
    raw_receipt.unlink(missing_ok=True)
    command = [
        str(deploy),
        "--hub",
        args.hub,
        "--preflight-only",
        "--qualification-receipt",
        str(raw_receipt),
        "--fleets-config",
        str(registry),
        *args.agents,
    ]
    try:
        completed = run_bounded(command, cwd=root, timeout=args.timeout)
    except QualificationError:
        raw_receipt.unlink(missing_ok=True)
        raise
    if completed.returncode != 0:
        raw_receipt.unlink(missing_ok=True)
        raise QualificationError("read-only fleet qualification failed")
    try:
        upstream_raw = private_bytes(raw_receipt, "fleet qualification receipt")
        upstream = json.loads(upstream_raw)
        if not isinstance(upstream, dict):
            raise QualificationError(
                "fleet qualification receipt root is not an object"
            )
        validate_upstream(
            upstream,
            revision=revision,
            hub=args.hub,
            registry_sha256=registry_digest,
            requested_agents=args.agents,
            max_age=args.max_age,
        )
        input_material = {
            "source_revision": revision,
            "hub_agent": args.hub,
            "requested_agents": args.agents,
            "fleet_registry_sha256": registry_digest,
            "deploy_script_sha256": digest(deploy_raw),
        }
        receipt = {
            "schema": WRAPPER_SCHEMA,
            "status": "passed",
            "read_only": True,
            "source_revision": revision,
            "fleet_name": upstream["fleet_name"],
            "hub_agent": args.hub,
            "selected_agents": [
                {
                    "name": item["name"],
                    "stable_id": item["stable_id"],
                    "generation": item["generation"],
                }
                for item in upstream["agents"]
            ],
            "command_sha256": digest(canonical(command)),
            "inputs_sha256": digest(canonical(input_material)),
            "stdout_sha256": digest(completed.stdout),
            "stderr_sha256": digest(completed.stderr),
            "qualification_file_sha256": digest(upstream_raw),
            "qualification_payload_sha256": digest(canonical(upstream)),
            "qualification": upstream,
            "wrapped_at": iso8601(utc_now()),
        }
        atomic_private_json(output, receipt)
        return receipt
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationError(
            "fleet qualification receipt is not valid JSON"
        ) from exc
    finally:
        raw_receipt.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hub", required=True)
    parser.add_argument("--fleets-config", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--deploy-script", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-age", type=int, default=900)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("agents", nargs="*")
    args = parser.parse_args()
    if not SAFE_NAME.fullmatch(args.hub):
        parser.error("--hub is invalid")
    if args.max_age < 1 or args.max_age > 3600:
        parser.error("--max-age must be between 1 and 3600 seconds")
    if args.timeout < 1 or args.timeout > 1800:
        parser.error("--timeout must be between 1 and 1800 seconds")
    if any(not SAFE_NAME.fullmatch(agent) for agent in args.agents):
        parser.error("agent name is invalid")
    try:
        receipt = run_qualification(args)
    except QualificationError as exc:
        parser.error(str(exc))
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
