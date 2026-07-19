#!/usr/bin/env python3
"""Build journal-safe plans and private hub requests for fleet release epochs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence


MAX_BYTES = 1024 * 1024
HEX = re.compile(r"^[0-9a-f]{64}$")
HUB_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
OPEN_MATERIAL_SCHEMA = "mac.fleet_epoch_open_material.v1"
PROVE_MATERIAL_SCHEMA = "mac.fleet_epoch_prove_material.v1"
RELEASE_MATERIAL_SCHEMA = "mac.fleet_epoch_release_material.v1"


class MaterialError(ValueError):
    pass


def _read(path: Path) -> dict[str, Any]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise MaterialError("material file is unreadable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) & 0o077
        or not 1 <= before.st_size <= MAX_BYTES
    ):
        raise MaterialError("material file is not a bounded owner-private regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        observed = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (observed.st_dev, observed.st_ino)
            or observed.st_nlink != 1
            or observed.st_uid != os.getuid()
        ):
            raise MaterialError("material file changed while opening")
        raw = bytearray()
        while len(raw) < observed.st_size:
            chunk = os.read(
                descriptor, min(64 * 1024, observed.st_size - len(raw))
            )
            if not chunk:
                raise MaterialError("material file was truncated")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise MaterialError("material file grew while reading")
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
            raise MaterialError("material file changed while reading")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(bytes(raw))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaterialError("material file is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise MaterialError("material root must be an object")
    return value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    destination = path.expanduser()
    if not destination.is_absolute():
        raise MaterialError("output path must be absolute")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = destination.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise MaterialError("output directory must be owner-private")
    descriptor, raw = tempfile.mkstemp(prefix="." + destination.name + ".", dir=destination.parent)
    temporary = Path(raw)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(value), stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        parent_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _exact(value: Any, keys: Iterable[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise MaterialError(f"{label} schema is not exact")
    return value


def _text(value: Any, label: str, maximum: int = 512) -> str:
    result = str(value or "").strip()
    if not result or len(result.encode()) > maximum or any(not char.isprintable() for char in result):
        raise MaterialError(f"{label} is invalid")
    return result


def _sha(value: Any, label: str) -> str:
    result = str(value or "")
    if not HEX.fullmatch(result):
        raise MaterialError(f"{label} is invalid")
    return result


def _hub_sha(value: Any, label: str) -> str:
    result = str(value or "")
    if not HUB_DIGEST.fullmatch(result):
        raise MaterialError(f"{label} is invalid")
    return result


def _commit(value: Any) -> str:
    result = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", result):
        raise MaterialError("source commit is invalid")
    return result


def _cohort_agents(raw: Any, *, prove: bool = False) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw or len(raw) > 256:
        raise MaterialError("material agents must be a non-empty bounded list")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in raw:
        base = {"agent_id", "generation", "deployment_id"}
        if prove:
            base |= {
                "prepared_evidence_sha256",
                "install_receipt",
                "attestation_proof",
                "report_executor_startup_timestamp",
            }
        agent = _exact(item, base, "material agent")
        agent_id = _text(agent["agent_id"], "agent id")
        if agent_id in seen:
            raise MaterialError("material contains a duplicate agent")
        seen.add(agent_id)
        normalized = {
            "agent_id": agent_id,
            "generation": _text(agent["generation"], "generation"),
            "deployment_id": _text(agent["deployment_id"], "deployment id"),
        }
        if prove:
            normalized["prepared_evidence_sha256"] = _sha(
                agent["prepared_evidence_sha256"], "prepared evidence digest"
            )
            receipt = agent["install_receipt"]
            if not isinstance(receipt, dict) or receipt.get("schema") != "mac.worker_credential_install_receipt.v1":
                raise MaterialError("worker install receipt is unsupported")
            proof = agent["attestation_proof"]
            if proof is not None and (
                not isinstance(proof, dict)
                or set(proof) != {"challenge", "signature"}
                or not isinstance(proof["challenge"], dict)
                or not isinstance(proof["signature"], str)
            ):
                raise MaterialError("attestation candidate proof is malformed")
            timestamp = agent["report_executor_startup_timestamp"]
            if timestamp is not None:
                timestamp = _text(timestamp, "report executor startup timestamp", 128)
            normalized.update(
                {
                    "install_receipt": receipt,
                    "attestation_proof": proof,
                    "report_executor_startup_timestamp": timestamp,
                }
            )
        result.append(normalized)
    return result


def build_open(material: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    value = _exact(
        material,
        {
            "schema",
            "epoch_id",
            "source_commit",
            "require_release_all_selected",
            "successor_hold_reason",
            "desired_worker_credential_mode",
            "agents",
        },
        "open material",
    )
    if value["schema"] != OPEN_MATERIAL_SCHEMA:
        raise MaterialError("open material schema is unsupported")
    epoch = _text(value["epoch_id"], "epoch id")
    source = _commit(value["source_commit"])
    require_all = value["require_release_all_selected"]
    if not isinstance(require_all, bool):
        raise MaterialError("require release all selected must be boolean")
    successor = value["successor_hold_reason"]
    if successor is not None:
        successor = _text(successor, "successor hold reason")
    mode = value["desired_worker_credential_mode"]
    if mode not in {None, "compatibility", "enforced"}:
        raise MaterialError("desired worker credential mode is invalid")
    raw_agents = value["agents"]
    if not isinstance(raw_agents, list) or not raw_agents or len(raw_agents) > 256:
        raise MaterialError("open material agents must be a non-empty bounded list")
    plan_agents: list[dict[str, Any]] = []
    request_agents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_agents:
        item = _exact(
            raw,
            {
                "agent_id",
                "generation",
                "deployment_id",
                "participant_state",
                "principal_id",
                "attestation_candidate_key",
                "report_executor_action",
                "report_executor_attestation",
            },
            "open material agent",
        )
        agent_id = _text(item["agent_id"], "agent id")
        if agent_id in seen:
            raise MaterialError("open material contains a duplicate agent")
        seen.add(agent_id)
        generation = _text(item["generation"], "generation")
        deployment_id = _text(item["deployment_id"], "deployment id")
        state = _exact(
            item["participant_state"],
            {
                "schema",
                "agent_id",
                "baseline_seen",
                "expected_dispatch_hold",
                "expected_hold_reason",
                "expected_hold_at",
            },
            "participant state",
        )
        if state["schema"] != "mac.fleet_release_participant_state.v1" or state["agent_id"] != agent_id:
            raise MaterialError("participant state belongs to another agent")
        held = state["expected_dispatch_hold"]
        reason = state["expected_hold_reason"]
        held_at = state["expected_hold_at"]
        if not isinstance(held, bool) or (held and (not reason or not held_at)) or (not held and (reason is not None or held_at is not None)):
            raise MaterialError("participant hold ownership is malformed")
        baseline = _text(state["baseline_seen"], "participant heartbeat baseline", 128)
        principal = _text(item["principal_id"], "principal id")
        candidate = item["attestation_candidate_key"]
        if candidate is not None:
            candidate = _text(candidate, "attestation candidate", 8192)
            if len(candidate) < 32 or any(char.isspace() for char in candidate):
                raise MaterialError("attestation candidate has an unsafe shape")
        action = item["report_executor_action"]
        if action not in {"preserve", "approve", "revoke"}:
            raise MaterialError("report executor action is invalid")
        attestation = item["report_executor_attestation"]
        if action == "approve" and not isinstance(attestation, dict):
            raise MaterialError("report executor approval lacks exact attestation")
        if action != "approve" and attestation is not None:
            raise MaterialError("non-approval report action contains attestation")
        plan_agents.append(
            {
                "agent_id": agent_id,
                "generation": generation,
                "deployment_id": deployment_id,
                "expected_dispatch_hold": held,
                "expected_hold_reason": reason,
                "expected_hold_at": held_at,
            }
        )
        request_agents.append(
            {
                "agent_id": agent_id,
                "expected_dispatch_hold": held,
                "expected_hold_reason": reason,
                "expected_hold_at": held_at,
                "generation": generation,
                "baseline_seen": baseline,
                "principal_id": principal,
                "attestation_candidate": ({"key": candidate} if candidate is not None else None),
                "report_executor_action": action,
                "report_executor_attestation": attestation,
            }
        )
    plan = {
        "schema": "mac.fleet_epoch_open_intent.v1",
        "epoch_id": epoch,
        "source_commit": source,
        "require_release_all_selected": require_all,
        "successor_hold_reason": successor,
        "desired_worker_credential_mode": mode,
        "agents": plan_agents,
    }
    request = {
        "epoch_id": epoch,
        "participants": request_agents,
        "successor_hold_reason": successor,
        "desired_worker_credential_mode": mode,
    }
    return plan, request


def build_prove(material: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    value = _exact(
        material,
        {"schema", "epoch_id", "source_commit", "identity_sha256", "agents"},
        "prove material",
    )
    if value["schema"] != PROVE_MATERIAL_SCHEMA:
        raise MaterialError("prove material schema is unsupported")
    epoch = _text(value["epoch_id"], "epoch id")
    identity = _hub_sha(value["identity_sha256"], "hub identity digest")
    agents = _cohort_agents(value["agents"], prove=True)
    plan = {
        "schema": "mac.fleet_epoch_prove_intent.v1",
        "epoch_id": epoch,
        "source_commit": _commit(value["source_commit"]),
        "identity_sha256": identity,
        "agents": [
            {
                "agent_id": item["agent_id"],
                "generation": item["generation"],
                "deployment_id": item["deployment_id"],
                "prepared_evidence_sha256": item["prepared_evidence_sha256"],
            }
            for item in agents
        ],
    }
    request = {
        "identity_sha256": identity,
        "proofs": [
            {
                "agent_id": item["agent_id"],
                "install_receipt": item["install_receipt"],
                "attestation_proof": item["attestation_proof"],
                "report_executor_startup_timestamp": item[
                    "report_executor_startup_timestamp"
                ],
            }
            for item in agents
        ],
    }
    return plan, request


def build_release(material: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    value = _exact(
        material,
        {
            "schema",
            "epoch_id",
            "source_commit",
            "identity_sha256",
            "require_release_all_selected",
            "successor_hold_reason",
            "agents",
        },
        "release material",
    )
    if value["schema"] != RELEASE_MATERIAL_SCHEMA:
        raise MaterialError("release material schema is unsupported")
    require_all = value["require_release_all_selected"]
    if not isinstance(require_all, bool):
        raise MaterialError("require release all selected must be boolean")
    successor = value["successor_hold_reason"]
    if successor is not None:
        successor = _text(successor, "successor hold reason")
    agents = _cohort_agents(value["agents"])
    plan = {
        "schema": "mac.fleet_release_epoch.v1",
        "epoch_id": _text(value["epoch_id"], "epoch id"),
        "source_commit": _commit(value["source_commit"]),
        "require_release_all_selected": require_all,
        "successor_hold_reason": successor,
        "agents": agents,
    }
    request = {"identity_sha256": _hub_sha(value["identity_sha256"], "hub identity digest")}
    return plan, request


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("open", "prove", "release"):
        command = sub.add_parser(name)
        command.add_argument("--material", type=Path, required=True)
        command.add_argument("--plan-out", type=Path, required=True)
        command.add_argument("--request-out", type=Path, required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        material = _read(args.material.expanduser())
        if args.command == "open":
            plan, request = build_open(material)
        elif args.command == "prove":
            plan, request = build_prove(material)
        else:
            plan, request = build_release(material)
        _write(args.plan_out, plan)
        _write(args.request_out, request)
        print(
            json.dumps(
                {
                    "status": "ready",
                    "plan_sha256": hashlib.sha256(
                        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    "outputs_written": True,
                },
                sort_keys=True,
            )
        )
        return 0
    except MaterialError as exc:
        print(f"fleet release material error: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
