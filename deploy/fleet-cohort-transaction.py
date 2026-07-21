#!/usr/bin/env python3
"""Durable, fenced transaction journal for synchronized fleet deployments.

The coordinator is deliberately a shell program, but its cohort state must
survive shell exits, controller crashes, and machine restarts.  This helper is
the single writer for that state.  It provides:

* an immutable epoch/cohort binding;
* compare-and-swap transitions with operation-id retry deduplication;
* live-controller fencing and explicit dead-controller adoption;
* durable atomic writes in an owner-private directory; and
* deterministic recovery discovery in reverse deployment order.

Only deployment identities, adapter-typed endpoint authority, and evidence
digests are retained.  Host targets, credentials, environment values, raw
machine identifiers, and receipt bodies are intentionally outside the schema.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


SCHEMA = "mac.fleet_cohort_transaction.v2"
VERSION = 2
TERMINAL_STATES = frozenset({"finalized", "aborted"})
GLOBAL_STATES = frozenset(
    {
        "preparing",
        "commit_intent",
        "hub_committed",
        "aborting",
        *TERMINAL_STATES,
    }
)
PHASES = frozenset(
    {
        "routing",
        "arming_phase1",
        "hub_opening",
        "quiescing",
        "quiesced",
        "arming_phase2",
        "deploying",
        "hub_proving",
        "aborting",
        "commit_intent",
        "finalizing",
        *TERMINAL_STATES,
    }
)
NODE_STATES = frozenset(
    {
        "planned",
        "route_bound",
        "phase1_prepare_started",
        "phase1_armed",
        "quiesce_started",
        "quiesced",
        "phase2_armed",
        "phase2_started",
        "prepared",
        "aborting",
        "aborted",
        "finalizing",
        "finalized",
    }
)
ROLLBACK_NODE_STATES = frozenset(
    {
        "phase1_prepare_started",
        "phase1_armed",
        "quiesce_started",
        "quiesced",
        "phase2_armed",
        "phase2_started",
        "prepared",
        "aborting",
    }
)
MAX_JOURNAL_BYTES = 1024 * 1024
MAX_EVIDENCE_BYTES = 256 * 1024
MAX_COHORT_NODES = 256
MAX_OPERATIONS = 8192
MAX_TEXT_BYTES = 512
MAX_HOLD_BYTES = 512
JOURNAL_PREFIX = "transaction-"
JOURNAL_SUFFIX = ".json"
LOCK_NAME = ".cohort-transaction.lock"
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]*$")
HEX_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")

TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "version",
        "epoch_id",
        "source_commit",
        "deploy_ts",
        "fleet",
        "hub_agent",
        "successor_hold",
        "require_release_all_selected",
        "binding_sha256",
        "hub_route_identity",
        "hub_state",
        "hub_open_plan",
        "hub_open_evidence",
        "hub_prove_plan",
        "hub_proved_evidence",
        "hub_commit_evidence",
        "hub_abort_evidence",
        "release_plan",
        "commit_not_applied_evidence",
        "state",
        "phase",
        "revision",
        "owner",
        "cohort",
        "operations",
        "created_at",
        "updated_at",
    }
)
OWNER_KEYS = frozenset(
    {"nonce", "pid", "boot_id_sha256", "process_start_sha256", "acquired_at"}
)
NODE_KEYS = frozenset(
    {
        "ordinal",
        "name",
        "stable_id",
        "generation",
        "deployment_id",
        "os",
        "supervisor",
        "report_executor_required",
        "state",
        "abort_kind",
        "abort_from_state",
        "route_identity",
        "restore_contract_sha256",
        "phase1_arm_evidence",
        "quiescence_evidence",
        "rollback_intent_sha256",
        "finalizer_sha256",
        "phase2_arm_evidence",
        "prepared_evidence",
        "abort_evidence",
        "finalize_evidence",
    }
)
EVIDENCE_KEYS = frozenset({"schema", "sha256", "size", "generation"})
RELEASE_PLAN_KEYS = frozenset({"filename", "schema", "epoch_id", "sha256", "size"})
HUB_PLAN_KEYS = frozenset({"filename", "schema", "epoch_id", "sha256", "size"})
HUB_OPEN_PLAN_KEYS = HUB_PLAN_KEYS | {
    "ownership_sha256",
    "desired_worker_credential_mode",
}
HUB_RECEIPT_EVIDENCE_KEYS = frozenset(
    {
        "schema",
        "status",
        "sha256",
        "size",
        "epoch_id",
        "hub_authority_id_sha256",
        "identity_sha256",
        "proof_sha256",
        "release_plan_sha256",
    }
)
HUB_RECEIPT_BASE_KEYS = frozenset(
    {
        "schema",
        "status",
        "epoch_id",
        "hub_authority_id",
        "identity_sha256",
        "cohort_size",
        "successor_hold_reason",
        "desired_worker_credential_mode",
        "prepared_at",
        "agents",
    }
)
HUB_RECEIPT_AGENT_KEYS = frozenset(
    {
        "agent_id",
        "prior_dispatch_hold",
        "prior_hold_reason",
        "prior_hold_at",
        "epoch_hold_reason",
        "epoch_hold_at",
        "generation",
        "principal_id",
        "principal_version",
        "principal_fingerprint",
        "attestation_candidate_fingerprint",
        "report_executor_action",
    }
)
ENDPOINT_IDENTITY_KEYS = frozenset({"schema", "adapter", "authority", "observation"})
SSH_AUTHORITY_KEYS = frozenset(
    {"ssh_host_key_sha256", "instance_id_kind", "instance_id_sha256"}
)
SSH_HUB_AUTHORITY_KEYS = SSH_AUTHORITY_KEYS | {"durable_store_uuid_sha256"}
K8S_AUTHORITY_KEYS = frozenset(
    {"cluster_uid_sha256", "workload_kind", "workload_uid_sha256"}
)
K8S_HUB_AUTHORITY_KEYS = K8S_AUTHORITY_KEYS | {"durable_store_uuid_sha256"}
K8S_OBSERVATION_KEYS = frozenset({"pod_uid_sha256"})
HUB_STATES = frozenset(
    {
        "unopened",
        "open_intent",
        "open",
        "prove_intent",
        "proved",
        "aborted",
        "committed",
    }
)
OPERATION_KEYS = frozenset({"operation_id", "action", "fingerprint", "revision", "at"})
COHORT_INPUT_KEYS = frozenset(
    {
        "name",
        "stable_id",
        "generation",
        "deployment_id",
        "os",
        "supervisor",
        "report_executor_required",
    }
)
COHORT_REQUIRED_KEYS = frozenset({"name", "stable_id", "generation"})


class JournalError(Exception):
    """A stable, JSON-reportable journal failure."""

    def __init__(self, code: str, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _require_exact_keys(
    value: dict[str, Any], keys: frozenset[str], context: str
) -> None:
    actual = frozenset(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise JournalError(
            "invalid_schema",
            f"{context} keys differ from schema (missing={missing}, extra={extra})",
        )


def _text(
    value: Any,
    field: str,
    *,
    max_bytes: int = MAX_TEXT_BYTES,
    token: bool = False,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise JournalError("invalid_input", f"{field} must be a string")
    if not value and not allow_empty:
        raise JournalError("invalid_input", f"{field} must not be empty")
    encoded = value.encode("utf-8")
    if len(encoded) > max_bytes:
        raise JournalError("invalid_input", f"{field} exceeds {max_bytes} UTF-8 bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise JournalError("invalid_input", f"{field} contains a control character")
    if token and value and not SAFE_TOKEN.fullmatch(value):
        raise JournalError("invalid_input", f"{field} is not a safe identifier")
    return value


def _epoch(value: Any) -> str:
    return _text(value, "epoch_id", max_bytes=256, token=True)


def _successor_hold(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return _text(value, "successor_hold", max_bytes=MAX_HOLD_BYTES)


def _release_epoch_matches(journal_epoch: str, release_epoch: Any) -> bool:
    if not isinstance(release_epoch, str):
        return False
    if release_epoch == journal_epoch:
        return True
    prefix = journal_epoch + ":"
    suffix = release_epoch.removeprefix(prefix)
    return release_epoch.startswith(prefix) and bool(HEX_SHA256.fullmatch(suffix))


def _deployment_identity(value: Any, field: str, *, max_bytes: int = 512) -> str:
    parsed = _text(value, field, max_bytes=max_bytes)
    if "://" in parsed.lower() or "@" in parsed or "/" in parsed or "\\" in parsed:
        raise JournalError(
            "invalid_input", f"{field} must be an identity, not a host target"
        )
    return parsed


def _commit(value: Any) -> str:
    commit = _text(value, "source_commit", max_bytes=64)
    if not HEX_COMMIT.fullmatch(commit):
        raise JournalError(
            "invalid_input",
            "source_commit must be a lowercase 40- or 64-hex Git object id",
        )
    return commit


def _digest(value: Any, field: str) -> str:
    parsed = _text(value, field, max_bytes=64)
    if not HEX_SHA256.fullmatch(parsed):
        raise JournalError("invalid_input", f"{field} must be lowercase 64-hex")
    return parsed


def _pid(value: Any, field: str = "owner pid") -> int:
    if isinstance(value, bool):
        raise JournalError("invalid_input", f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise JournalError("invalid_input", f"{field} must be an integer") from exc
    if parsed < 1 or parsed > 2**31 - 1:
        raise JournalError("invalid_input", f"{field} is outside the supported range")
    return parsed


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _identity_probe(argv: list[str], context: str) -> str:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise JournalError("identity_unavailable", f"cannot inspect {context}") from exc
    value = " ".join(result.stdout.split())
    if result.returncode != 0 or not value:
        raise JournalError("identity_unavailable", f"cannot inspect {context}")
    return value


def _boot_id_sha256() -> str:
    try:
        value = (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        )
    except OSError:
        value = _identity_probe(["ps", "-o", "lstart=", "-p", "1"], "boot identity")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _process_start_sha256(pid: int) -> str:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        value = fields[21]
    except (OSError, IndexError):
        value = _identity_probe(
            ["ps", "-o", "lstart=", "-p", str(pid)], "controller process identity"
        )
    material = f"{_boot_id_sha256()}:{pid}:{value}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _owner_alive(owner: dict[str, Any]) -> bool:
    pid = int(owner["pid"])
    if not _process_alive(pid):
        return False
    try:
        return owner["boot_id_sha256"] == _boot_id_sha256() and owner[
            "process_start_sha256"
        ] == _process_start_sha256(pid)
    except JournalError:
        return False


def _owner(nonce: Any, pid: Any) -> dict[str, Any]:
    parsed_nonce = _text(nonce, "owner nonce", max_bytes=256, token=True)
    parsed_pid = _pid(pid)
    if not _process_alive(parsed_pid):
        raise JournalError("owner_not_live", f"controller pid {parsed_pid} is not live")
    return {
        "nonce": parsed_nonce,
        "pid": parsed_pid,
        "boot_id_sha256": _boot_id_sha256(),
        "process_start_sha256": _process_start_sha256(parsed_pid),
        "acquired_at": _utc_now(),
    }


def _journal_name(epoch_id: str) -> str:
    digest = hashlib.sha256(epoch_id.encode()).hexdigest()
    return f"{JOURNAL_PREFIX}{digest}{JOURNAL_SUFFIX}"


def _release_plan_name(epoch_id: str) -> str:
    digest = hashlib.sha256(epoch_id.encode()).hexdigest()
    return f"release-plan-{digest}.json"


def _hub_open_plan_name(epoch_id: str) -> str:
    digest = hashlib.sha256(epoch_id.encode()).hexdigest()
    return f"hub-open-plan-{digest}.json"


def _hub_prove_plan_name(epoch_id: str) -> str:
    digest = hashlib.sha256(epoch_id.encode()).hexdigest()
    return f"hub-prove-plan-{digest}.json"


def _binding_projection(journal: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "epoch_id": journal["epoch_id"],
        "source_commit": journal["source_commit"],
        "deploy_ts": journal["deploy_ts"],
        "fleet": journal["fleet"],
        "hub_agent": journal["hub_agent"],
        "successor_hold": journal["successor_hold"],
        "require_release_all_selected": journal["require_release_all_selected"],
        "cohort": [
            {
                key: node[key]
                for key in (
                    "ordinal",
                    "name",
                    "stable_id",
                    "generation",
                    "deployment_id",
                    "os",
                    "supervisor",
                    "report_executor_required",
                )
            }
            for node in journal["cohort"]
        ],
    }


def _validate_evidence(value: Any, context: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise JournalError(
            "invalid_schema", f"{context} evidence must be an object or null"
        )
    _require_exact_keys(value, EVIDENCE_KEYS, f"{context} evidence")
    _text(value["schema"], f"{context} evidence schema", allow_empty=True)
    if not isinstance(value["sha256"], str) or not HEX_SHA256.fullmatch(
        value["sha256"]
    ):
        raise JournalError("invalid_schema", f"{context} evidence sha256 is invalid")
    if not isinstance(value["size"], int) or isinstance(value["size"], bool):
        raise JournalError("invalid_schema", f"{context} evidence size is invalid")
    if value["size"] < 2 or value["size"] > MAX_EVIDENCE_BYTES:
        raise JournalError(
            "invalid_schema", f"{context} evidence size is outside bounds"
        )
    _text(value["generation"], f"{context} evidence generation")


def _validate_endpoint_identity(value: Any, context: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise JournalError("invalid_schema", f"{context} identity must be an object")
    _require_exact_keys(value, ENDPOINT_IDENTITY_KEYS, f"{context} identity")
    if value["schema"] != "mac.fleet_endpoint_identity.v1":
        raise JournalError(
            "invalid_schema", f"{context} identity schema is unsupported"
        )
    adapter = value["adapter"]
    authority = value["authority"]
    observation = value["observation"]
    if adapter in {"ssh-machine", "ssh-hub"}:
        if not isinstance(authority, dict) or not isinstance(observation, dict):
            raise JournalError("invalid_schema", f"{context} SSH identity is malformed")
        _require_exact_keys(
            authority,
            SSH_HUB_AUTHORITY_KEYS if adapter == "ssh-hub" else SSH_AUTHORITY_KEYS,
            f"{context} SSH authority",
        )
        _require_exact_keys(observation, frozenset(), f"{context} SSH observation")
        _digest(authority["ssh_host_key_sha256"], f"{context} SSH host key")
        _text(
            authority["instance_id_kind"],
            f"{context} instance identity kind",
            max_bytes=64,
            token=True,
        )
        _digest(authority["instance_id_sha256"], f"{context} instance identity")
        if adapter == "ssh-hub":
            _digest(
                authority["durable_store_uuid_sha256"],
                f"{context} durable store uuid",
            )
    elif adapter in {"kubernetes-workload", "kubernetes-hub"}:
        if not isinstance(authority, dict) or not isinstance(observation, dict):
            raise JournalError(
                "invalid_schema", f"{context} Kubernetes identity is malformed"
            )
        _require_exact_keys(
            authority,
            K8S_HUB_AUTHORITY_KEYS
            if adapter == "kubernetes-hub"
            else K8S_AUTHORITY_KEYS,
            f"{context} Kubernetes authority",
        )
        _require_exact_keys(
            observation, K8S_OBSERVATION_KEYS, f"{context} Kubernetes observation"
        )
        _digest(authority["cluster_uid_sha256"], f"{context} cluster uid")
        _text(
            authority["workload_kind"],
            f"{context} workload kind",
            max_bytes=64,
            token=True,
        )
        _digest(authority["workload_uid_sha256"], f"{context} workload uid")
        if adapter == "kubernetes-hub":
            _digest(
                authority["durable_store_uuid_sha256"],
                f"{context} durable store uuid",
            )
        _digest(observation["pod_uid_sha256"], f"{context} pod uid")
    else:
        raise JournalError(
            "invalid_schema", f"{context} identity adapter is unsupported"
        )


def _endpoint_identity(path: str) -> dict[str, Any]:
    raw = _read_secure_file(Path(path), MAX_EVIDENCE_BYTES, "endpoint identity file")
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalError(
            "invalid_evidence", "endpoint identity is not valid UTF-8 JSON"
        ) from exc
    _validate_endpoint_identity(parsed, "endpoint")
    return parsed


def _validate_release_plan_metadata(value: Any, epoch_id: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise JournalError(
            "invalid_schema", "release plan metadata must be an object or null"
        )
    _require_exact_keys(value, RELEASE_PLAN_KEYS, "release plan metadata")
    expected_filename = _release_plan_name(epoch_id)
    if value["filename"] != expected_filename:
        raise JournalError(
            "invalid_schema", "release plan filename is not bound to the epoch"
        )
    if value["schema"] != "mac.fleet_release_epoch.v1" or not _release_epoch_matches(
        epoch_id, value["epoch_id"]
    ):
        raise JournalError(
            "invalid_schema", "release plan metadata identity is invalid"
        )
    if not isinstance(value["sha256"], str) or not HEX_SHA256.fullmatch(
        value["sha256"]
    ):
        raise JournalError("invalid_schema", "release plan digest is invalid")
    if not isinstance(value["size"], int) or isinstance(value["size"], bool):
        raise JournalError("invalid_schema", "release plan size is invalid")
    if value["size"] < 2 or value["size"] > MAX_EVIDENCE_BYTES:
        raise JournalError("invalid_schema", "release plan size is outside bounds")


def _validate_hub_plan_metadata(
    value: Any,
    epoch_id: str,
    *,
    phase: str,
) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise JournalError(
            "invalid_schema", f"hub {phase} plan metadata must be an object or null"
        )
    _require_exact_keys(
        value,
        HUB_OPEN_PLAN_KEYS if phase == "open" else HUB_PLAN_KEYS,
        f"hub {phase} plan metadata",
    )
    expected_name = (
        _hub_open_plan_name(epoch_id)
        if phase == "open"
        else _hub_prove_plan_name(epoch_id)
    )
    if value["filename"] != expected_name:
        raise JournalError(
            "invalid_schema", f"hub {phase} plan filename is not bound to the epoch"
        )
    expected_schema = f"mac.fleet_epoch_{phase}_intent.v1"
    if value["schema"] != expected_schema or value["epoch_id"] != epoch_id:
        raise JournalError(
            "invalid_schema", f"hub {phase} plan metadata identity is invalid"
        )
    _digest(value["sha256"], f"hub {phase} plan digest")
    if phase == "open":
        _digest(value["ownership_sha256"], "hub open ownership digest")
        if value["desired_worker_credential_mode"] not in {
            None,
            "compatibility",
            "enforced",
        }:
            raise JournalError("invalid_schema", "hub open policy mode is invalid")
    if not isinstance(value["size"], int) or isinstance(value["size"], bool):
        raise JournalError("invalid_schema", f"hub {phase} plan size is invalid")
    if value["size"] < 2 or value["size"] > MAX_EVIDENCE_BYTES:
        raise JournalError("invalid_schema", f"hub {phase} plan size is outside bounds")


def _hub_digest(value: Any, field: str) -> str:
    parsed = _text(value, field, max_bytes=71)
    if not parsed.startswith("sha256:") or not HEX_SHA256.fullmatch(parsed[7:]):
        raise JournalError("invalid_schema", f"{field} must be sha256 lowercase hex")
    return parsed


def _validate_hub_receipt_evidence(value: Any, context: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise JournalError(
            "invalid_schema", f"{context} evidence must be an object or null"
        )
    _require_exact_keys(value, HUB_RECEIPT_EVIDENCE_KEYS, f"{context} evidence")
    if value["schema"] != "mac.fleet_hub_receipt_evidence.v1":
        raise JournalError("invalid_schema", f"{context} evidence schema is invalid")
    if value["status"] not in {"open", "proved", "committed", "aborted"}:
        raise JournalError("invalid_schema", f"{context} evidence status is invalid")
    _digest(value["sha256"], f"{context} receipt digest")
    if not isinstance(value["size"], int) or isinstance(value["size"], bool):
        raise JournalError("invalid_schema", f"{context} evidence size is invalid")
    if value["size"] < 2 or value["size"] > MAX_EVIDENCE_BYTES:
        raise JournalError(
            "invalid_schema", f"{context} evidence size is outside bounds"
        )
    _epoch(value["epoch_id"])
    _digest(value["hub_authority_id_sha256"], f"{context} hub authority")
    _hub_digest(value["identity_sha256"], f"{context} hub identity")
    if value["proof_sha256"] is not None:
        _hub_digest(value["proof_sha256"], f"{context} hub proof")
    if value["release_plan_sha256"] is not None:
        _digest(value["release_plan_sha256"], f"{context} release plan")


def _validate_journal(
    journal: Any, *, expected_epoch: str | None = None
) -> dict[str, Any]:
    if not isinstance(journal, dict):
        raise JournalError("invalid_schema", "journal root must be an object")
    _require_exact_keys(journal, TOP_LEVEL_KEYS, "journal")
    if journal["schema"] != SCHEMA or journal["version"] != VERSION:
        raise JournalError("invalid_schema", "journal schema or version is unsupported")
    epoch_id = _epoch(journal["epoch_id"])
    if expected_epoch is not None and epoch_id != expected_epoch:
        raise JournalError("invalid_schema", "journal filename does not bind its epoch")
    _commit(journal["source_commit"])
    _text(journal["deploy_ts"], "deploy_ts", max_bytes=64, token=True)
    _text(journal["fleet"], "fleet", max_bytes=128, token=True)
    _text(journal["hub_agent"], "hub_agent", max_bytes=128, token=True)
    _successor_hold(journal["successor_hold"])
    if not isinstance(journal["require_release_all_selected"], bool):
        raise JournalError(
            "invalid_schema", "require_release_all_selected must be a boolean"
        )
    if (
        journal["successor_hold"] is not None
        and not journal["require_release_all_selected"]
    ):
        raise JournalError(
            "invalid_schema", "successor hold requires exact full-cohort release"
        )
    _validate_endpoint_identity(journal["hub_route_identity"], "hub route")
    if journal["hub_state"] not in HUB_STATES:
        raise JournalError("invalid_schema", "journal hub state is invalid")
    _validate_hub_plan_metadata(journal["hub_open_plan"], epoch_id, phase="open")
    _validate_hub_receipt_evidence(journal["hub_open_evidence"], "hub open")
    _validate_hub_plan_metadata(journal["hub_prove_plan"], epoch_id, phase="prove")
    _validate_hub_receipt_evidence(journal["hub_proved_evidence"], "hub proved")
    _validate_hub_receipt_evidence(journal["hub_commit_evidence"], "hub commit")
    _validate_hub_receipt_evidence(journal["hub_abort_evidence"], "hub abort")
    _validate_release_plan_metadata(journal["release_plan"], epoch_id)
    _validate_hub_receipt_evidence(
        journal["commit_not_applied_evidence"], "commit not applied"
    )
    if journal["state"] not in GLOBAL_STATES or journal["phase"] not in PHASES:
        raise JournalError("invalid_schema", "journal state or phase is invalid")
    if not isinstance(journal["revision"], int) or isinstance(
        journal["revision"], bool
    ):
        raise JournalError("invalid_schema", "journal revision must be an integer")
    if journal["revision"] < 0:
        raise JournalError("invalid_schema", "journal revision must not be negative")
    if not isinstance(journal["owner"], dict):
        raise JournalError("invalid_schema", "journal owner must be an object")
    _require_exact_keys(journal["owner"], OWNER_KEYS, "journal owner")
    _text(journal["owner"]["nonce"], "journal owner nonce", max_bytes=256, token=True)
    _pid(journal["owner"]["pid"], "journal owner pid")
    _digest(journal["owner"]["boot_id_sha256"], "journal owner boot identity")
    _digest(
        journal["owner"]["process_start_sha256"],
        "journal owner process identity",
    )
    _text(journal["owner"]["acquired_at"], "journal owner acquired_at")
    _text(journal["created_at"], "journal created_at")
    _text(journal["updated_at"], "journal updated_at")

    cohort = journal["cohort"]
    if not isinstance(cohort, list) or not 1 <= len(cohort) <= MAX_COHORT_NODES:
        raise JournalError("invalid_schema", "journal cohort size is outside bounds")
    seen_names: set[str] = set()
    seen_ids: set[str] = set()
    for ordinal, node in enumerate(cohort):
        if not isinstance(node, dict):
            raise JournalError(
                "invalid_schema", f"cohort node {ordinal} must be an object"
            )
        _require_exact_keys(node, NODE_KEYS, f"cohort node {ordinal}")
        if node["ordinal"] != ordinal:
            raise JournalError(
                "invalid_schema", "cohort ordinals must be exact and contiguous"
            )
        name = _text(
            node["name"], f"cohort node {ordinal} name", max_bytes=128, token=True
        )
        stable_id = _text(
            node["stable_id"],
            f"cohort node {ordinal} stable_id",
            max_bytes=256,
            token=True,
        )
        if name in seen_names or stable_id in seen_ids:
            raise JournalError(
                "invalid_schema", "cohort names and stable ids must be unique"
            )
        seen_names.add(name)
        seen_ids.add(stable_id)
        _deployment_identity(
            node["generation"], f"cohort node {ordinal} generation", max_bytes=256
        )
        _deployment_identity(
            node["deployment_id"], f"cohort node {ordinal} deployment_id", max_bytes=512
        )
        _text(node["os"], f"cohort node {ordinal} os", max_bytes=64, token=True)
        _text(
            node["supervisor"],
            f"cohort node {ordinal} supervisor",
            max_bytes=64,
            token=True,
        )
        if not isinstance(node["report_executor_required"], bool):
            raise JournalError(
                "invalid_schema",
                f"cohort node {ordinal} report executor requirement is invalid",
            )
        if node["state"] not in NODE_STATES:
            raise JournalError(
                "invalid_schema", f"cohort node {ordinal} state is invalid"
            )
        if node["abort_kind"] not in (
            None,
            "cleanup_only",
            "phase1_restore",
            "phase2_rollback",
        ):
            raise JournalError(
                "invalid_schema", f"cohort node {ordinal} abort_kind is invalid"
            )
        if node["abort_from_state"] not in (None, *NODE_STATES):
            raise JournalError(
                "invalid_schema", f"cohort node {ordinal} abort source state is invalid"
            )
        _validate_endpoint_identity(
            node["route_identity"], f"cohort node {ordinal} route"
        )
        restore_digest = node["restore_contract_sha256"]
        if restore_digest is not None and (
            not isinstance(restore_digest, str)
            or not HEX_SHA256.fullmatch(restore_digest)
        ):
            raise JournalError(
                "invalid_schema",
                f"cohort node {ordinal} restore contract digest is invalid",
            )
        rollback_digest = node["rollback_intent_sha256"]
        if rollback_digest is not None and (
            not isinstance(rollback_digest, str)
            or not HEX_SHA256.fullmatch(rollback_digest)
        ):
            raise JournalError(
                "invalid_schema",
                f"cohort node {ordinal} rollback intent digest is invalid",
            )
        finalizer_digest = node["finalizer_sha256"]
        if finalizer_digest is not None and (
            not isinstance(finalizer_digest, str)
            or not HEX_SHA256.fullmatch(finalizer_digest)
        ):
            raise JournalError(
                "invalid_schema",
                f"cohort node {ordinal} finalizer digest is invalid",
            )
        _validate_evidence(
            node["phase1_arm_evidence"], f"cohort node {ordinal} phase1 arm"
        )
        _validate_evidence(
            node["quiescence_evidence"], f"cohort node {ordinal} quiescence"
        )
        _validate_evidence(
            node["phase2_arm_evidence"], f"cohort node {ordinal} phase2 arm"
        )
        _validate_evidence(node["prepared_evidence"], f"cohort node {ordinal} prepared")
        _validate_evidence(node["abort_evidence"], f"cohort node {ordinal} abort")
        _validate_evidence(node["finalize_evidence"], f"cohort node {ordinal} finalize")
        _validate_node_consistency(node, ordinal)

    operations = journal["operations"]
    if not isinstance(operations, list) or len(operations) > MAX_OPERATIONS:
        raise JournalError("invalid_schema", "journal operations are outside bounds")
    operation_ids: set[str] = set()
    for expected_revision, operation in enumerate(operations, 1):
        if not isinstance(operation, dict):
            raise JournalError("invalid_schema", "journal operation must be an object")
        _require_exact_keys(operation, OPERATION_KEYS, "journal operation")
        operation_id = _text(
            operation["operation_id"], "operation id", max_bytes=256, token=True
        )
        if operation_id in operation_ids:
            raise JournalError("invalid_schema", "journal operation ids must be unique")
        operation_ids.add(operation_id)
        _text(operation["action"], "operation action", max_bytes=64, token=True)
        if not isinstance(operation["fingerprint"], str) or not HEX_SHA256.fullmatch(
            operation["fingerprint"]
        ):
            raise JournalError(
                "invalid_schema", "journal operation fingerprint is invalid"
            )
        if operation["revision"] != expected_revision:
            raise JournalError(
                "invalid_schema", "journal operation revisions are not contiguous"
            )
        _text(operation["at"], "operation timestamp")
    if journal["revision"] != len(operations):
        raise JournalError(
            "invalid_schema", "journal revision does not match its operation log"
        )

    expected_binding = _sha256(_binding_projection(journal))
    if journal["binding_sha256"] != expected_binding:
        raise JournalError(
            "invalid_schema", "journal immutable binding digest does not match"
        )
    _validate_state_consistency(journal)
    return journal


_NODE_PROGRESS = {
    "planned": 0,
    "route_bound": 1,
    "phase1_prepare_started": 2,
    "phase1_armed": 3,
    "quiesce_started": 4,
    "quiesced": 5,
    "phase2_armed": 6,
    "phase2_started": 7,
    "prepared": 8,
}


def _validate_node_consistency(node: dict[str, Any], ordinal: int) -> None:
    state = node["state"]
    if state in {"aborting", "aborted"}:
        source = node["abort_from_state"]
        if source not in ROLLBACK_NODE_STATES - {"aborting"}:
            raise JournalError(
                "invalid_schema", f"cohort node {ordinal} has an invalid abort source"
            )
        expected_action = {
            "phase1_prepare_started": "cleanup_only",
            "phase1_armed": "cleanup_only",
            "quiesce_started": "phase1_restore",
            "quiesced": "phase1_restore",
            "phase2_armed": "phase1_restore",
            "phase2_started": "phase2_rollback",
            "prepared": "phase2_rollback",
        }[source]
        if node["abort_kind"] != expected_action:
            raise JournalError(
                "invalid_schema", f"cohort node {ordinal} recovery action is ambiguous"
            )
        if state == "aborting" and node["abort_evidence"] is not None:
            raise JournalError(
                "invalid_schema", f"cohort node {ordinal} abort evidence arrived early"
            )
        if state == "aborted" and node["abort_evidence"] is None:
            raise JournalError(
                "invalid_schema", f"cohort node {ordinal} lacks abort evidence"
            )
        effective = source
    else:
        if any(
            value is not None
            for value in (
                node["abort_kind"],
                node["abort_from_state"],
                node["abort_evidence"],
            )
        ):
            raise JournalError(
                "invalid_schema", f"cohort node {ordinal} has stray abort state"
            )
        effective = "prepared" if state in {"finalizing", "finalized"} else state

    rank = _NODE_PROGRESS[effective]
    staged_fields = (
        (1, "route_identity"),
        (3, "restore_contract_sha256"),
        (3, "phase1_arm_evidence"),
        (5, "quiescence_evidence"),
        (6, "rollback_intent_sha256"),
        (6, "finalizer_sha256"),
        (6, "phase2_arm_evidence"),
        (8, "prepared_evidence"),
    )
    for required_rank, field in staged_fields:
        present = node[field] is not None
        if present != (rank >= required_rank):
            raise JournalError(
                "invalid_schema",
                f"cohort node {ordinal} {field} does not match its durable state",
            )

    if state == "finalizing":
        if node["finalize_evidence"] is not None:
            raise JournalError(
                "invalid_schema",
                f"cohort node {ordinal} finalized evidence arrived early",
            )
    elif state == "finalized":
        if node["finalize_evidence"] is None:
            raise JournalError(
                "invalid_schema", f"cohort node {ordinal} lacks finalize evidence"
            )
    elif node["finalize_evidence"] is not None:
        raise JournalError(
            "invalid_schema", f"cohort node {ordinal} has stray finalize evidence"
        )


def _validate_state_consistency(journal: dict[str, Any]) -> None:
    states = [node["state"] for node in journal["cohort"]]
    state = journal["state"]
    phase = journal["phase"]
    hub_state = journal["hub_state"]
    open_plan = journal["hub_open_plan"]
    opened = journal["hub_open_evidence"]
    prove_plan = journal["hub_prove_plan"]
    proved = journal["hub_proved_evidence"]
    committed = journal["hub_commit_evidence"]
    hub_aborted = journal["hub_abort_evidence"]

    if hub_state == "unopened":
        valid_hub = (
            open_plan is None
            and opened is None
            and prove_plan is None
            and proved is None
            and committed is None
            and hub_aborted is None
        )
    elif hub_state == "open_intent":
        valid_hub = (
            open_plan is not None
            and opened is None
            and prove_plan is None
            and proved is None
            and committed is None
            and hub_aborted is None
        )
    elif hub_state == "open":
        valid_hub = (
            open_plan is not None
            and opened is not None
            and prove_plan is None
            and proved is None
            and committed is None
            and hub_aborted is None
        )
    elif hub_state == "prove_intent":
        valid_hub = (
            open_plan is not None
            and opened is not None
            and prove_plan is not None
            and proved is None
            and committed is None
            and hub_aborted is None
        )
    elif hub_state == "proved":
        valid_hub = (
            open_plan is not None
            and opened is not None
            and prove_plan is not None
            and proved is not None
            and committed is None
            and hub_aborted is None
        )
    elif hub_state == "aborted":
        valid_hub = (
            open_plan is not None
            and opened is not None
            and committed is None
            and hub_aborted is not None
            and (
                (prove_plan is None and proved is None)
                or (prove_plan is not None and proved is not None)
            )
        )
    else:
        valid_hub = (
            open_plan is not None
            and opened is not None
            and prove_plan is not None
            and proved is not None
            and committed is not None
            and hub_aborted is None
        )
    if not valid_hub:
        raise JournalError("invalid_schema", "hub participant state is inconsistent")
    if open_plan is not None and journal["hub_route_identity"] is None:
        raise JournalError("invalid_schema", "hub open intent lacks route identity")

    for evidence, expected_status in (
        (opened, "open"),
        (proved, "proved"),
        (committed, "committed"),
        (hub_aborted, "aborted"),
    ):
        if evidence is not None and evidence["status"] != expected_status:
            raise JournalError(
                "invalid_schema", "hub receipt status differs from state"
            )
    if isinstance(opened, dict) and (
        opened["proof_sha256"] is not None or opened["release_plan_sha256"] is not None
    ):
        raise JournalError("invalid_schema", "hub open receipt has later-phase binding")
    if isinstance(proved, dict) and (
        proved["proof_sha256"] is None or proved["release_plan_sha256"] is not None
    ):
        raise JournalError("invalid_schema", "hub proved receipt binding is incomplete")
    if isinstance(opened, dict):
        for evidence in (proved, committed, hub_aborted):
            if evidence is not None and (
                evidence["epoch_id"] != opened["epoch_id"]
                or evidence["hub_authority_id_sha256"]
                != opened["hub_authority_id_sha256"]
                or evidence["identity_sha256"] != opened["identity_sha256"]
            ):
                raise JournalError("invalid_schema", "hub receipt identity changed")
    if isinstance(proved, dict):
        for evidence in (committed, hub_aborted):
            if (
                evidence is not None
                and evidence["proof_sha256"] != proved["proof_sha256"]
            ):
                raise JournalError("invalid_schema", "hub receipt proof changed")

    release_plan = journal["release_plan"]
    not_applied = journal["commit_not_applied_evidence"]
    if not_applied is not None and release_plan is None:
        raise JournalError(
            "invalid_schema", "commit-not-applied proof lacks a release plan"
        )
    if not_applied is not None:
        if (
            not_applied["status"] != "proved"
            or not_applied["release_plan_sha256"] != release_plan["sha256"]
            or not isinstance(proved, dict)
            or not_applied["proof_sha256"] != proved["proof_sha256"]
            or not_applied["identity_sha256"] != proved["identity_sha256"]
            or not_applied["hub_authority_id_sha256"]
            != proved["hub_authority_id_sha256"]
        ):
            raise JournalError(
                "invalid_schema", "commit-not-applied proof differs from plan"
            )
    if committed is not None and (
        release_plan is None
        or committed["release_plan_sha256"] != release_plan["sha256"]
    ):
        raise JournalError("invalid_schema", "hub commit proof lacks release plan")
    if hub_aborted is not None and hub_aborted["release_plan_sha256"] != (
        release_plan["sha256"] if release_plan is not None else None
    ):
        raise JournalError(
            "invalid_schema", "hub abort proof differs from release intent"
        )

    if state == "finalized":
        if not (
            phase == "finalized"
            and hub_state == "committed"
            and committed is not None
            and all(item == "finalized" for item in states)
            and release_plan is not None
            and not_applied is None
        ):
            raise JournalError("invalid_schema", "finalized journal is inconsistent")
        return
    if state == "hub_committed":
        if not (
            phase == "finalizing"
            and hub_state == "committed"
            and committed is not None
            and all(item in {"prepared", "finalizing", "finalized"} for item in states)
            and release_plan is not None
            and not_applied is None
        ):
            raise JournalError("invalid_schema", "committed journal is inconsistent")
        return
    if state == "commit_intent":
        if not (
            phase == "commit_intent"
            and hub_state == "proved"
            and all(item == "prepared" for item in states)
            and release_plan is not None
            and not_applied is None
        ):
            raise JournalError("invalid_schema", "commit intent is inconsistent")
        return
    if state == "aborted":
        if not (
            phase == "aborted"
            and hub_state in {"unopened", "aborted"}
            and all(item in {"planned", "route_bound", "aborted"} for item in states)
            and (release_plan is None) == (not_applied is None)
        ):
            raise JournalError("invalid_schema", "aborted journal is inconsistent")
        return
    if state == "aborting":
        if (
            phase != "aborting"
            or hub_state == "committed"
            or any(item in {"finalizing", "finalized"} for item in states)
        ):
            raise JournalError("invalid_schema", "aborting journal is inconsistent")
        if release_plan is not None and not_applied is None:
            raise JournalError(
                "invalid_schema",
                "commit intent cannot roll back without a proved not-applied receipt",
            )
        return

    if state != "preparing" or release_plan is not None or not_applied is not None:
        raise JournalError("invalid_schema", "preparing journal is inconsistent")
    if any(
        item in {"aborting", "aborted", "finalizing", "finalized"} for item in states
    ):
        raise JournalError(
            "invalid_schema", "preparing journal has terminal node state"
        )
    if hub_state in {"aborted", "committed"}:
        raise JournalError("invalid_schema", "preparing journal has terminal hub state")
    if any(
        item
        in {
            "quiesce_started",
            "quiesced",
            "phase2_armed",
            "phase2_started",
            "prepared",
        }
        for item in states
    ) and hub_state not in {"open", "prove_intent", "proved"}:
        raise JournalError("invalid_schema", "node mutation lacks open hub epoch")


def _read_bounded_owner_file(
    descriptor: int,
    maximum: int,
    context: str,
    *,
    insecure_code: str,
    invalid_code: str,
    require_mode: bool = True,
) -> bytes:
    """Read one already-open file after the common owner/privacy checks."""
    file_stat = os.fstat(descriptor)
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid != os.getuid()
        or file_stat.st_nlink != 1
        or (require_mode and stat.S_IMODE(file_stat.st_mode) != 0o600)
    ):
        mode = " mode 0600" if require_mode else ""
        raise JournalError(
            insecure_code,
            f"{context} must be owner-owned regular{mode} with one link",
        )
    if file_stat.st_size < 2 or file_stat.st_size > maximum:
        raise JournalError(invalid_code, f"{context} size is outside bounds")
    chunks: list[bytes] = []
    remaining = file_stat.st_size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 65536))
        if not chunk:
            raise JournalError(invalid_code, f"{context} was truncated while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise JournalError(invalid_code, f"{context} grew while reading")
    return b"".join(chunks)


class JournalDirectory:
    """Secure directory, advisory lock, and durable journal I/O."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self.directory_fd = -1
        self.lock_fd = -1

    def __enter__(self) -> JournalDirectory:
        if not self.path.is_absolute():
            raise JournalError(
                "insecure_directory", "journal directory must be absolute"
            )
        created = False
        try:
            os.mkdir(self.path, 0o700)
            created = True
            os.chmod(self.path, 0o700)
            self._fsync_parent()
        except FileNotFoundError as exc:
            raise JournalError(
                "insecure_directory", "journal directory parent does not exist"
            ) from exc
        except FileExistsError:
            pass
        except OSError as exc:
            raise JournalError(
                "insecure_directory", f"cannot create journal directory: {exc}"
            ) from exc

        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            self.directory_fd = os.open(self.path, flags)
        except OSError as exc:
            raise JournalError(
                "insecure_directory", f"cannot securely open journal directory: {exc}"
            ) from exc
        directory_stat = os.fstat(self.directory_fd)
        path_stat = os.lstat(self.path)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or (directory_stat.st_dev, directory_stat.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
            or directory_stat.st_uid != os.getuid()
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            self.close()
            suffix = " after creation" if created else ""
            raise JournalError(
                "insecure_directory",
                f"journal directory must be owner-owned mode 0700 and not a symlink{suffix}",
            )
        self.lock_fd = self._open_lock()
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX)
        except OSError as exc:
            self.close()
            raise JournalError(
                "lock_failed", f"cannot lock journal directory: {exc}"
            ) from exc
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        if self.lock_fd >= 0:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self.lock_fd)
                self.lock_fd = -1
        if self.directory_fd >= 0:
            os.close(self.directory_fd)
            self.directory_fd = -1

    def _fsync_parent(self) -> None:
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        parent_fd = os.open(self.path.parent, flags)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    def _open_lock(self) -> int:
        base_flags = (
            os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(
                LOCK_NAME,
                base_flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=self.directory_fd,
            )
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.fsync(self.directory_fd)
        except FileExistsError:
            try:
                descriptor = os.open(LOCK_NAME, base_flags, dir_fd=self.directory_fd)
            except OSError as exc:
                raise JournalError(
                    "insecure_lock", f"cannot securely open journal lock: {exc}"
                ) from exc
        except OSError as exc:
            raise JournalError(
                "insecure_lock", f"cannot securely open journal lock: {exc}"
            ) from exc
        lock_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.getuid()
            or lock_stat.st_nlink != 1
            or stat.S_IMODE(lock_stat.st_mode) != 0o600
        ):
            os.close(descriptor)
            raise JournalError(
                "insecure_lock",
                "journal lock must be owner-owned regular mode 0600 with one link",
            )
        return descriptor

    def names(self) -> list[str]:
        names = []
        for name in os.listdir(self.directory_fd):
            if name.startswith(JOURNAL_PREFIX) and name.endswith(JOURNAL_SUFFIX):
                names.append(name)
        return sorted(names)

    def read_name(
        self, name: str, *, expected_epoch: str | None = None
    ) -> dict[str, Any]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=self.directory_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise JournalError(
                "insecure_journal", f"cannot securely open {name}: {exc}"
            ) from exc
        try:
            raw = _read_bounded_owner_file(
                descriptor,
                MAX_JOURNAL_BYTES,
                name,
                insecure_code="insecure_journal",
                invalid_code="invalid_schema",
            )
        finally:
            os.close(descriptor)
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JournalError(
                "invalid_schema", f"{name} is not valid UTF-8 JSON"
            ) from exc
        return _validate_journal(decoded, expected_epoch=expected_epoch)

    def read_epoch(self, epoch_id: str) -> dict[str, Any]:
        try:
            return self.read_name(_journal_name(epoch_id), expected_epoch=epoch_id)
        except FileNotFoundError as exc:
            raise JournalError(
                "not_found", f"cohort epoch {epoch_id!r} does not exist"
            ) from exc

    def all(self) -> list[dict[str, Any]]:
        return [self.read_name(name) for name in self.names()]

    def read_auxiliary(self, name: str) -> bytes:
        if name != os.path.basename(name) or not (
            name.startswith("release-plan-")
            or name.startswith("hub-open-plan-")
            or name.startswith("hub-prove-plan-")
        ):
            raise JournalError("invalid_schema", "auxiliary plan filename is invalid")
        if name.startswith("hub-open-plan-"):
            context = "hub open plan"
        elif name.startswith("hub-prove-plan-"):
            context = "hub prove plan"
        else:
            context = "release plan"
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=self.directory_fd)
        except FileNotFoundError as exc:
            raise JournalError("plan_not_found", f"{context} does not exist") from exc
        except OSError as exc:
            raise JournalError(
                "insecure_release_plan", f"cannot securely open {context}: {exc}"
            ) from exc
        try:
            return _read_bounded_owner_file(
                descriptor,
                MAX_EVIDENCE_BYTES,
                context,
                insecure_code="insecure_release_plan",
                invalid_code="invalid_release_plan",
            )
        finally:
            os.close(descriptor)

    def _atomic_replace(
        self,
        name: str,
        payload: bytes,
        context: str,
        readback: Callable[[], bytes],
    ) -> None:
        """Replace one validated owner-private file and fsync both boundaries."""
        temporary = f".tmp.{os.getpid()}.{secrets.token_hex(16)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(temporary, flags, 0o600, dir_fd=self.directory_fd)
            os.fchmod(descriptor, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise JournalError(
                        "write_failed", f"{context} write made no progress"
                    )
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(
                temporary,
                name,
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
            )
            if readback() != payload:
                raise JournalError("write_failed", f"{context} readback differs")
            os.fsync(self.directory_fd)
        except JournalError:
            raise
        except OSError as exc:
            raise JournalError(
                "write_failed", f"cannot durably write {context}: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass

    def write_auxiliary(self, name: str, payload: bytes) -> None:
        if (
            name != os.path.basename(name)
            or not (
                name.startswith("release-plan-")
                or name.startswith("hub-open-plan-")
                or name.startswith("hub-prove-plan-")
            )
            or not name.endswith(".json")
        ):
            raise JournalError(
                "invalid_release_plan", "auxiliary plan filename is invalid"
            )
        if name.startswith("hub-open-plan-"):
            context = "hub open plan"
        elif name.startswith("hub-prove-plan-"):
            context = "hub prove plan"
        else:
            context = "release plan"
        if len(payload) < 2 or len(payload) > MAX_EVIDENCE_BYTES:
            raise JournalError(
                "invalid_release_plan", "release plan size is outside bounds"
            )
        try:
            existing = self.read_auxiliary(name)
        except JournalError as exc:
            if exc.code != "plan_not_found":
                raise
        else:
            if existing == payload:
                return
            raise JournalError(
                "release_plan_conflict",
                f"epoch {context} already exists with different bytes",
            )
        self._atomic_replace(name, payload, context, lambda: self.read_auxiliary(name))

    def write(self, journal: dict[str, Any]) -> None:
        _validate_journal(journal, expected_epoch=journal["epoch_id"])
        payload = _canonical(journal)
        if len(payload) > MAX_JOURNAL_BYTES:
            raise JournalError(
                "journal_too_large", "journal exceeds its durable size bound"
            )
        name = _journal_name(journal["epoch_id"])
        try:
            self.read_name(name, expected_epoch=journal["epoch_id"])
        except FileNotFoundError:
            pass
        self._atomic_replace(
            name,
            payload,
            "journal",
            lambda: _canonical(
                self.read_name(name, expected_epoch=journal["epoch_id"])
            ),
        )


def _parse_cohort(path: str) -> list[dict[str, Any]]:
    if path == "-":
        raw = sys.stdin.buffer.read(MAX_EVIDENCE_BYTES + 1)
    else:
        raw = _read_secure_file(
            Path(path), MAX_EVIDENCE_BYTES, "cohort file", require_mode=False
        )
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise JournalError("invalid_input", "cohort file exceeds its size bound")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalError(
            "invalid_input", "cohort file is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_COHORT_NODES:
        raise JournalError(
            "invalid_input", "cohort must be a non-empty bounded JSON array"
        )
    result: list[dict[str, Any]] = []
    names: set[str] = set()
    stable_ids: set[str] = set()
    for ordinal, raw_node in enumerate(value):
        if not isinstance(raw_node, dict):
            raise JournalError(
                "invalid_input", f"cohort node {ordinal} must be an object"
            )
        actual = frozenset(raw_node)
        if not COHORT_REQUIRED_KEYS <= actual <= COHORT_INPUT_KEYS:
            raise JournalError(
                "invalid_input",
                f"cohort node {ordinal} has missing or unsupported fields",
            )
        name = _text(
            raw_node["name"], f"cohort node {ordinal} name", max_bytes=128, token=True
        )
        stable_id = _text(
            raw_node["stable_id"],
            f"cohort node {ordinal} stable_id",
            max_bytes=256,
            token=True,
        )
        if name in names or stable_id in stable_ids:
            raise JournalError(
                "invalid_input", "cohort names and stable ids must be unique"
            )
        names.add(name)
        stable_ids.add(stable_id)
        report_executor_required = raw_node.get("report_executor_required", False)
        if not isinstance(report_executor_required, bool):
            raise JournalError(
                "invalid_input",
                f"cohort node {ordinal} report executor requirement must be boolean",
            )
        result.append(
            {
                "ordinal": ordinal,
                "name": name,
                "stable_id": stable_id,
                "generation": _deployment_identity(
                    raw_node["generation"],
                    f"cohort node {ordinal} generation",
                    max_bytes=256,
                ),
                "deployment_id": _deployment_identity(
                    raw_node.get("deployment_id", raw_node["generation"]),
                    f"cohort node {ordinal} deployment_id",
                    max_bytes=512,
                ),
                "os": _text(
                    raw_node.get("os", "unknown"),
                    f"cohort node {ordinal} os",
                    max_bytes=64,
                    token=True,
                ),
                "supervisor": _text(
                    raw_node.get("supervisor", "unknown"),
                    f"cohort node {ordinal} supervisor",
                    max_bytes=64,
                    token=True,
                ),
                "report_executor_required": report_executor_required,
                "state": "planned",
                "abort_kind": None,
                "abort_from_state": None,
                "route_identity": None,
                "restore_contract_sha256": None,
                "phase1_arm_evidence": None,
                "quiescence_evidence": None,
                "rollback_intent_sha256": None,
                "finalizer_sha256": None,
                "phase2_arm_evidence": None,
                "prepared_evidence": None,
                "abort_evidence": None,
                "finalize_evidence": None,
            }
        )
    return result


def _read_secure_file(
    path: Path, maximum: int, context: str, *, require_mode: bool = True
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise JournalError(
            "insecure_evidence", f"cannot securely open {context}: {exc}"
        ) from exc
    try:
        return _read_bounded_owner_file(
            descriptor,
            maximum,
            context,
            insecure_code="insecure_evidence",
            invalid_code="invalid_evidence",
            require_mode=require_mode,
        )
    finally:
        os.close(descriptor)


def _evidence(path: str, generation: str) -> dict[str, Any]:
    raw = _read_secure_file(Path(path), MAX_EVIDENCE_BYTES, "evidence file")
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalError(
            "invalid_evidence", "evidence file is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise JournalError("invalid_evidence", "evidence file root must be an object")
    schema = parsed.get("schema", "")
    if not isinstance(schema, str):
        raise JournalError(
            "invalid_evidence", "evidence schema must be a string when present"
        )
    _text(schema, "evidence schema", max_bytes=256, allow_empty=True)
    return {
        "schema": schema,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "generation": generation,
    }


def _reject_secret_or_target_fields(value: Any, context: str = "release plan") -> None:
    """Fail closed if an otherwise opaque proof tries to become a secret store."""
    forbidden = {
        "target",
        "ssh_target",
        "token",
        "bearer",
        "authorization",
        "secret",
        "password",
        "credentials",
        "api_key",
        "private_key",
        "authenticated_url",
        "host",
        "hostname",
        "address",
        "url",
        "uri",
        "remote",
        "endpoint",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise JournalError(
                    "invalid_release_plan", f"{context} has a non-string key"
                )
            normalized = key.lower().replace("-", "_")
            if normalized in forbidden or normalized.endswith("_target"):
                raise JournalError(
                    "sensitive_release_plan",
                    f"release plan may not retain field {key!r}",
                )
            _reject_secret_or_target_fields(child, context)
    elif isinstance(value, list):
        for child in value:
            _reject_secret_or_target_fields(child, context)
    elif isinstance(value, str):
        lowered = value.lower()
        if "://" in lowered or lowered.startswith("git@"):
            raise JournalError(
                "sensitive_release_plan",
                "release plan may not retain URL or host target values",
            )


def _release_plan(
    raw: bytes, journal: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalError(
            "invalid_release_plan", "release plan is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise JournalError(
            "invalid_release_plan", "release plan root must be an object"
        )
    if frozenset(parsed) != frozenset(
        {
            "schema",
            "epoch_id",
            "source_commit",
            "require_release_all_selected",
            "successor_hold_reason",
            "agents",
        }
    ):
        raise JournalError(
            "invalid_release_plan", "release plan top-level schema is not exact"
        )
    if (
        parsed["schema"] != "mac.fleet_release_epoch.v1"
        or not _release_epoch_matches(journal["epoch_id"], parsed["epoch_id"])
        or parsed["source_commit"] != journal["source_commit"]
        or parsed["successor_hold_reason"] != journal["successor_hold"]
        or parsed["require_release_all_selected"]
        is not journal["require_release_all_selected"]
    ):
        raise JournalError(
            "release_plan_binding_conflict", "release plan differs from cohort epoch"
        )
    agents = parsed["agents"]
    if not isinstance(agents, list) or len(agents) != len(journal["cohort"]):
        raise JournalError(
            "release_plan_binding_conflict", "release plan cohort size differs"
        )
    if any(not isinstance(item, dict) for item in agents):
        raise JournalError(
            "invalid_release_plan", "release plan agents must be objects"
        )
    expected = {
        node["stable_id"]: (node["generation"], node["deployment_id"])
        for node in journal["cohort"]
    }
    observed: dict[str, tuple[Any, Any]] = {}
    for item in agents:
        agent_id = item.get("agent_id")
        if not isinstance(agent_id, str) or agent_id in observed:
            raise JournalError(
                "release_plan_binding_conflict", "release plan agent ids are invalid"
            )
        observed[agent_id] = (item.get("generation"), item.get("deployment_id"))
    if observed != expected:
        raise JournalError(
            "release_plan_binding_conflict",
            "release plan agent generations differ from the exact cohort",
        )
    _reject_secret_or_target_fields(parsed)
    metadata = {
        "filename": _release_plan_name(journal["epoch_id"]),
        "schema": parsed["schema"],
        "epoch_id": parsed["epoch_id"],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }
    return parsed, metadata


def _hub_plan(
    raw: bytes,
    journal: dict[str, Any],
    *,
    phase: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalError(
            "invalid_release_plan", f"hub {phase} plan is not valid UTF-8 JSON"
        ) from exc
    common = {"schema", "epoch_id", "source_commit", "agents"}
    required = frozenset(
        common
        | (
            {
                "require_release_all_selected",
                "successor_hold_reason",
                "desired_worker_credential_mode",
            }
            if phase == "open"
            else {"identity_sha256"}
        )
    )
    if not isinstance(parsed, dict) or frozenset(parsed) != required:
        raise JournalError(
            "invalid_release_plan",
            f"hub {phase} plan top-level schema is not exact",
        )
    if (
        parsed["schema"] != f"mac.fleet_epoch_{phase}_intent.v1"
        or parsed["epoch_id"] != journal["epoch_id"]
        or parsed["source_commit"] != journal["source_commit"]
    ):
        raise JournalError(
            "release_plan_binding_conflict",
            f"hub {phase} plan differs from cohort epoch",
        )
    if phase == "open" and (
        parsed["successor_hold_reason"] != journal["successor_hold"]
        or parsed["require_release_all_selected"]
        is not journal["require_release_all_selected"]
        or parsed["desired_worker_credential_mode"]
        not in {None, "compatibility", "enforced"}
    ):
        raise JournalError(
            "release_plan_binding_conflict", "hub open plan differs from cohort hold"
        )
    if phase == "prove":
        opened = journal["hub_open_evidence"]
        if (
            not isinstance(opened, dict)
            or parsed["identity_sha256"] != opened["identity_sha256"]
        ):
            raise JournalError(
                "release_plan_binding_conflict",
                "hub prove plan differs from opened hub identity",
            )
    agents = parsed["agents"]
    if not isinstance(agents, list) or len(agents) != len(journal["cohort"]):
        raise JournalError(
            "release_plan_binding_conflict", f"hub {phase} cohort size differs"
        )
    expected: dict[str, tuple[Any, ...]] = {}
    for node in journal["cohort"]:
        values: tuple[Any, ...] = (node["generation"], node["deployment_id"])
        if phase == "prove":
            evidence = node["prepared_evidence"]
            if not isinstance(evidence, dict):
                raise JournalError(
                    "invalid_transition", "hub prove requires every node prepared"
                )
            values += (evidence["sha256"],)
        expected[node["stable_id"]] = values
    observed: dict[str, tuple[Any, ...]] = {}
    agent_keys = {"agent_id", "generation", "deployment_id"}
    if phase == "prove":
        agent_keys.add("prepared_evidence_sha256")
    else:
        agent_keys.update(
            {
                "expected_dispatch_hold",
                "expected_hold_reason",
                "expected_hold_at",
            }
        )
    ownership: list[dict[str, Any]] = []
    for item in agents:
        if not isinstance(item, dict) or frozenset(item) != frozenset(agent_keys):
            raise JournalError(
                "invalid_release_plan", f"hub {phase} agent schema is not exact"
            )
        agent_id = item["agent_id"]
        if not isinstance(agent_id, str) or agent_id in observed:
            raise JournalError(
                "release_plan_binding_conflict",
                f"hub {phase} agent ids are invalid",
            )
        values = (item["generation"], item["deployment_id"])
        if phase == "prove":
            _digest(
                item["prepared_evidence_sha256"],
                "hub prove prepared evidence digest",
            )
            values += (item["prepared_evidence_sha256"],)
        else:
            expected_hold = item["expected_dispatch_hold"]
            reason = item["expected_hold_reason"]
            held_at = item["expected_hold_at"]
            if not isinstance(expected_hold, bool):
                raise JournalError(
                    "invalid_release_plan", "expected dispatch hold must be boolean"
                )
            if expected_hold:
                _text(reason, "expected hold reason")
                timestamp = _text(held_at, "expected hold timestamp", max_bytes=128)
                try:
                    dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise JournalError(
                        "invalid_release_plan", "expected hold timestamp is invalid"
                    ) from exc
            elif reason is not None or held_at is not None:
                raise JournalError(
                    "invalid_release_plan",
                    "unheld agent has unexpected hold ownership",
                )
            ownership.append(
                {
                    "agent_id": agent_id,
                    "expected_dispatch_hold": expected_hold,
                    "expected_hold_reason": reason,
                    "expected_hold_at": held_at,
                }
            )
        observed[agent_id] = values
    if observed != expected:
        raise JournalError(
            "release_plan_binding_conflict",
            f"hub {phase} agent evidence differs from the exact cohort",
        )
    _reject_secret_or_target_fields(parsed, f"hub {phase} plan")
    metadata = {
        "filename": (
            _hub_open_plan_name(journal["epoch_id"])
            if phase == "open"
            else _hub_prove_plan_name(journal["epoch_id"])
        ),
        "schema": parsed["schema"],
        "epoch_id": parsed["epoch_id"],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }
    if phase == "open":
        metadata["ownership_sha256"] = _sha256(
            sorted(ownership, key=lambda item: item["agent_id"])
        )
        metadata["desired_worker_credential_mode"] = parsed[
            "desired_worker_credential_mode"
        ]
    return parsed, metadata


def _verified_hub_plan_path(
    directory: JournalDirectory,
    journal: dict[str, Any],
    *,
    phase: str,
) -> str | None:
    metadata = journal[f"hub_{phase}_plan"]
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise JournalError("invalid_schema", f"hub {phase} plan metadata is invalid")
    raw = directory.read_auxiliary(metadata["filename"])
    _parsed, observed = _hub_plan(raw, journal, phase=phase)
    if observed != metadata:
        raise JournalError(
            "invalid_release_plan", f"durable hub {phase} plan metadata changed"
        )
    return str(directory.path / metadata["filename"])


def _verified_release_plan_path(
    directory: JournalDirectory, journal: dict[str, Any]
) -> str | None:
    metadata = journal["release_plan"]
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise JournalError("invalid_schema", "release plan metadata is invalid")
    raw = directory.read_auxiliary(metadata["filename"])
    _parsed, observed = _release_plan(raw, journal)
    if observed != metadata:
        raise JournalError(
            "invalid_release_plan", "durable release plan metadata changed"
        )
    return str(directory.path / metadata["filename"])


def _hub_receipt_evidence(
    path: str,
    journal: dict[str, Any],
    *,
    expected_status: str,
    bind_release_plan: bool = False,
) -> dict[str, Any]:
    raw = _read_secure_file(Path(path), MAX_EVIDENCE_BYTES, "hub receipt file")
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalError(
            "invalid_evidence", "hub receipt is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise JournalError("invalid_evidence", "hub receipt root must be an object")
    status_fields = {
        "open": frozenset(),
        "proved": frozenset({"proof_sha256", "proved_at"}),
        "committed": frozenset({"proof_sha256", "proved_at", "committed_at"}),
    }
    if expected_status == "aborted":
        allowed = {
            HUB_RECEIPT_BASE_KEYS | {"aborted_at", "abort_reason"},
            HUB_RECEIPT_BASE_KEYS
            | {"proof_sha256", "proved_at", "aborted_at", "abort_reason"},
        }
        if frozenset(parsed) not in allowed:
            raise JournalError(
                "invalid_evidence", "aborted hub receipt schema is not exact"
            )
    else:
        expected_keys = HUB_RECEIPT_BASE_KEYS | status_fields[expected_status]
        if frozenset(parsed) != expected_keys:
            raise JournalError("invalid_evidence", "hub receipt schema is not exact")
    if (
        parsed["schema"] != "mac.fleet_release_epoch_receipt.v1"
        or parsed["status"] != expected_status
        or parsed["epoch_id"] != journal["epoch_id"]
    ):
        raise JournalError(
            "evidence_binding_conflict", "hub receipt differs from the cohort epoch"
        )
    try:
        authority_id = str(uuid.UUID(str(parsed["hub_authority_id"])))
    except (TypeError, ValueError, AttributeError) as exc:
        raise JournalError(
            "invalid_evidence", "hub authority id is not a UUID"
        ) from exc
    authority_digest = hashlib.sha256(authority_id.lower().encode()).hexdigest()
    route = journal["hub_route_identity"]
    if (
        not isinstance(route, dict)
        or route["authority"].get("durable_store_uuid_sha256") != authority_digest
    ):
        raise JournalError(
            "evidence_binding_conflict", "hub receipt belongs to another authority"
        )
    identity_sha256 = _hub_digest(parsed["identity_sha256"], "hub receipt identity")
    opened = journal["hub_open_evidence"]
    if isinstance(opened, dict) and identity_sha256 != opened["identity_sha256"]:
        raise JournalError("evidence_binding_conflict", "hub receipt identity changed")
    proof_sha256 = parsed.get("proof_sha256")
    if proof_sha256 is not None:
        proof_sha256 = _hub_digest(proof_sha256, "hub receipt proof")
    proved = journal["hub_proved_evidence"]
    if isinstance(proved, dict) and proof_sha256 != proved["proof_sha256"]:
        raise JournalError("evidence_binding_conflict", "hub receipt proof changed")
    if expected_status == "aborted" and proved is None and proof_sha256 is not None:
        raise JournalError(
            "evidence_binding_conflict",
            "aborted hub receipt proves an unrecorded prove transition",
        )
    if expected_status in {"proved", "committed"} and proof_sha256 is None:
        raise JournalError("invalid_evidence", "hub receipt lacks cohort proof")
    if expected_status == "open" and proof_sha256 is not None:
        raise JournalError("invalid_evidence", "open hub receipt contains proof")
    if parsed["cohort_size"] != len(journal["cohort"]):
        raise JournalError(
            "evidence_binding_conflict", "hub receipt cohort size differs"
        )
    if parsed["successor_hold_reason"] != journal["successor_hold"]:
        raise JournalError(
            "evidence_binding_conflict", "hub receipt successor hold differs"
        )
    open_plan = journal["hub_open_plan"]
    if not isinstance(open_plan, dict):
        raise JournalError("invalid_transition", "hub receipt lacks open intent")
    if parsed["desired_worker_credential_mode"] not in {
        None,
        "compatibility",
        "enforced",
    }:
        raise JournalError("invalid_evidence", "hub receipt policy mode is invalid")
    if (
        parsed["desired_worker_credential_mode"]
        != open_plan["desired_worker_credential_mode"]
    ):
        raise JournalError(
            "evidence_binding_conflict", "hub receipt policy mode differs"
        )
    _text(parsed["prepared_at"], "hub prepared timestamp", max_bytes=128)
    for timestamp in ("proved_at", "committed_at", "aborted_at"):
        if timestamp in parsed:
            _text(parsed[timestamp], f"hub {timestamp}", max_bytes=128)
    if "abort_reason" in parsed:
        _text(parsed["abort_reason"], "hub abort reason", max_bytes=1024)
    agents = parsed["agents"]
    if not isinstance(agents, list) or len(agents) != len(journal["cohort"]):
        raise JournalError("evidence_binding_conflict", "hub receipt agents differ")
    expected_agents = {
        node["stable_id"]: node["generation"] for node in journal["cohort"]
    }
    observed_agents: dict[str, str] = {}
    ownership: list[dict[str, Any]] = []
    for item in agents:
        if not isinstance(item, dict) or frozenset(item) != HUB_RECEIPT_AGENT_KEYS:
            raise JournalError(
                "invalid_evidence", "hub receipt agent schema is not exact"
            )
        agent_id = item["agent_id"]
        if not isinstance(agent_id, str) or agent_id in observed_agents:
            raise JournalError("invalid_evidence", "hub receipt agent id is invalid")
        observed_agents[agent_id] = item["generation"]
        if not isinstance(item["prior_dispatch_hold"], bool):
            raise JournalError("invalid_evidence", "hub receipt hold state is invalid")
        if item["prior_dispatch_hold"]:
            _text(item["prior_hold_reason"], "hub receipt prior hold reason")
            _text(
                item["prior_hold_at"],
                "hub receipt prior hold timestamp",
                max_bytes=128,
            )
        elif item["prior_hold_reason"] is not None or item["prior_hold_at"] is not None:
            raise JournalError(
                "invalid_evidence", "unheld hub receipt agent has prior ownership"
            )
        ownership.append(
            {
                "agent_id": agent_id,
                "expected_dispatch_hold": item["prior_dispatch_hold"],
                "expected_hold_reason": item["prior_hold_reason"],
                "expected_hold_at": item["prior_hold_at"],
            }
        )
        if not isinstance(item["principal_version"], int) or isinstance(
            item["principal_version"], bool
        ):
            raise JournalError(
                "invalid_evidence", "hub receipt principal version is invalid"
            )
        for field in (
            "epoch_hold_reason",
            "epoch_hold_at",
            "generation",
            "principal_id",
            "principal_fingerprint",
            "report_executor_action",
        ):
            _text(item[field], f"hub receipt agent {field}")
    if observed_agents != expected_agents:
        raise JournalError("evidence_binding_conflict", "hub receipt cohort differs")
    if (
        _sha256(sorted(ownership, key=lambda item: item["agent_id"]))
        != open_plan["ownership_sha256"]
    ):
        raise JournalError(
            "evidence_binding_conflict", "hub receipt prior hold ownership differs"
        )
    release_plan_sha256: str | None = None
    if bind_release_plan:
        metadata = journal["release_plan"]
        if not isinstance(metadata, dict):
            raise JournalError("invalid_transition", "hub receipt lacks release intent")
        release_plan_sha256 = metadata["sha256"]
    return {
        "schema": "mac.fleet_hub_receipt_evidence.v1",
        "status": expected_status,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "epoch_id": journal["epoch_id"],
        "hub_authority_id_sha256": authority_digest,
        "identity_sha256": identity_sha256,
        "proof_sha256": proof_sha256,
        "release_plan_sha256": release_plan_sha256,
    }


def _node(journal: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    for candidate in journal["cohort"]:
        if candidate["name"] == args.agent_name:
            if (
                candidate["stable_id"] != args.stable_id
                or candidate["generation"] != args.generation
            ):
                raise JournalError(
                    "node_binding_conflict",
                    "agent name does not match the bound stable id and generation",
                )
            return candidate
    raise JournalError("node_binding_conflict", "agent is not part of the bound cohort")


def _operation_fingerprint(action: str, details: dict[str, Any]) -> str:
    return _sha256({"action": action, "details": details})


def _mutate(
    directory: JournalDirectory,
    args: argparse.Namespace,
    action: str,
    details: dict[str, Any],
    transition: Callable[[dict[str, Any]], bool],
    *,
    check_owner: bool = True,
) -> tuple[dict[str, Any], bool]:
    journal = directory.read_epoch(_epoch(args.epoch_id))
    operation_id = _text(args.operation_id, "operation id", max_bytes=256, token=True)
    fingerprint = _operation_fingerprint(action, details)
    if (journal["state"], action) in {("finalized", "finalize"), ("aborted", "abort")}:
        return journal, False
    if check_owner:
        supplied_nonce = _text(
            args.owner_nonce, "owner nonce", max_bytes=256, token=True
        )
        if supplied_nonce != journal["owner"]["nonce"]:
            raise JournalError(
                "owner_fenced",
                "controller nonce no longer owns this epoch",
                exit_code=3,
            )
        if not _owner_alive(journal["owner"]):
            raise JournalError(
                "owner_dead",
                "journal owner is not live; adopt it before recovery",
                exit_code=3,
            )
    for operation in journal["operations"]:
        if operation["operation_id"] == operation_id:
            if operation["action"] != action or operation["fingerprint"] != fingerprint:
                raise JournalError(
                    "operation_conflict",
                    "operation id was already used for different input",
                    exit_code=3,
                )
            return journal, False
    if args.expected_revision != journal["revision"]:
        raise JournalError(
            "cas_conflict",
            f"expected revision {args.expected_revision}, found {journal['revision']}",
            exit_code=3,
        )
    if len(journal["operations"]) >= MAX_OPERATIONS:
        raise JournalError("journal_too_large", "operation log reached its safe bound")
    changed = transition(journal)
    if not changed:
        return journal, False
    revision = journal["revision"] + 1
    journal["revision"] = revision
    now = _utc_now()
    journal["updated_at"] = now
    journal["operations"].append(
        {
            "operation_id": _text(
                operation_id, "operation id", max_bytes=256, token=True
            ),
            "action": action,
            "fingerprint": fingerprint,
            "revision": revision,
            "at": now,
        }
    )
    directory.write(journal)
    return journal, True


def _require_nonterminal(journal: dict[str, Any]) -> None:
    if journal["state"] in TERMINAL_STATES:
        raise JournalError(
            "terminal_journal", "terminal cohort journal is immutable", exit_code=3
        )


def _require_forward_direction(journal: dict[str, Any]) -> None:
    _require_nonterminal(journal)
    if journal["phase"] == "aborting":
        raise JournalError(
            "invalid_transition",
            "cohort recovery has begun; forward deployment is fenced",
        )


def _refresh_phase(journal: dict[str, Any]) -> None:
    states = [node["state"] for node in journal["cohort"]]
    if journal["state"] in TERMINAL_STATES:
        journal["phase"] = journal["state"]
    elif journal["state"] == "hub_committed":
        journal["phase"] = "finalizing"
    elif journal["state"] == "commit_intent":
        journal["phase"] = "commit_intent"
    elif journal["state"] == "aborting" or any(
        state in {"aborting", "aborted"} for state in states
    ):
        journal["state"] = "aborting"
        journal["phase"] = "aborting"
    elif journal["hub_state"] == "open_intent":
        journal["phase"] = "hub_opening"
    elif journal["hub_state"] == "prove_intent":
        journal["phase"] = "hub_proving"
    elif any(state in {"phase2_started", "prepared"} for state in states):
        journal["phase"] = "deploying"
    elif any(state == "phase2_armed" for state in states):
        journal["phase"] = "arming_phase2"
    elif all(state == "quiesced" for state in states):
        journal["phase"] = "quiesced"
    elif any(state in {"quiesce_started", "quiesced"} for state in states):
        journal["phase"] = "quiescing"
    elif any(
        state in {"phase1_prepare_started", "phase1_armed"} for state in states
    ):
        journal["phase"] = "arming_phase1"
    else:
        journal["phase"] = "routing"


def _rollback_candidates(journal: dict[str, Any]) -> list[dict[str, Any]]:
    if journal["state"] in {"commit_intent", "hub_committed", "finalized"}:
        return []
    candidates: list[dict[str, Any]] = []
    for node in reversed(journal["cohort"]):
        if node["state"] not in ROLLBACK_NODE_STATES:
            continue
        action = node["abort_kind"]
        if action is None:
            action = {
                "phase1_prepare_started": "cleanup_only",
                "phase1_armed": "cleanup_only",
                "quiesce_started": "phase1_restore",
                "quiesced": "phase1_restore",
                "phase2_armed": "phase1_restore",
                "phase2_started": "phase2_rollback",
                "prepared": "phase2_rollback",
            }[node["state"]]
        candidates.append(
            {
                "ordinal": node["ordinal"],
                "agent_name": node["name"],
                "stable_id": node["stable_id"],
                "generation": node["generation"],
                "deployment_id": node["deployment_id"],
                "deploy_ts": journal["deploy_ts"],
                "source_commit": journal["source_commit"],
                "state": node["state"],
                "recovery_action": action,
                "route_identity": node["route_identity"],
                "restore_contract_sha256": node["restore_contract_sha256"],
                "rollback_intent_sha256": node["rollback_intent_sha256"],
                "finalizer_sha256": node["finalizer_sha256"],
                "prepared_evidence": node["prepared_evidence"],
            }
        )
    return candidates


def _finalization_candidates(journal: dict[str, Any]) -> list[dict[str, Any]]:
    if journal["state"] != "hub_committed":
        return []
    return [
        {
            "ordinal": node["ordinal"],
            "agent_name": node["name"],
            "stable_id": node["stable_id"],
            "generation": node["generation"],
            "deployment_id": node["deployment_id"],
            "source_commit": journal["source_commit"],
            "deploy_ts": journal["deploy_ts"],
            "fleet": journal["fleet"],
            "os": node["os"],
            "supervisor": node["supervisor"],
            "report_executor_required": node["report_executor_required"],
            "state": node["state"],
            "finalization_action": "cleanup_commit",
            "route_identity": node["route_identity"],
            "finalizer_sha256": node["finalizer_sha256"],
        }
        for node in journal["cohort"]
        if node["state"] in {"prepared", "finalizing"}
    ]


def _summary(journal: dict[str, Any]) -> dict[str, Any]:
    return {
        "epoch_id": journal["epoch_id"],
        "source_commit": journal["source_commit"],
        "state": journal["state"],
        "phase": journal["phase"],
        "revision": journal["revision"],
        "terminal": journal["state"] in TERMINAL_STATES,
        "owner": {
            **journal["owner"],
            "alive": _owner_alive(journal["owner"]),
        },
        "cohort_size": len(journal["cohort"]),
        "hub_state": journal["hub_state"],
        "rollback_candidates": len(_rollback_candidates(journal)),
        "finalization_candidates": len(_finalization_candidates(journal)),
        "hub_open_plan": journal["hub_open_plan"],
        "hub_prove_plan": journal["hub_prove_plan"],
        "release_plan": journal["release_plan"],
        "commit_not_applied_evidence": journal["commit_not_applied_evidence"],
        "updated_at": journal["updated_at"],
    }


def command_init(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    epoch_id = _epoch(args.epoch_id)
    cohort = _parse_cohort(args.cohort_file)
    now = _utc_now()
    journal: dict[str, Any] = {
        "schema": SCHEMA,
        "version": VERSION,
        "epoch_id": epoch_id,
        "source_commit": _commit(args.source_commit),
        "deploy_ts": _text(args.deploy_ts, "deploy_ts", max_bytes=64, token=True),
        "fleet": _text(args.fleet, "fleet", max_bytes=128, token=True),
        "hub_agent": _text(args.hub_agent, "hub_agent", max_bytes=128, token=True),
        "successor_hold": _successor_hold(args.successor_hold),
        "require_release_all_selected": args.require_release_all_selected,
        "binding_sha256": "",
        "hub_route_identity": None,
        "hub_state": "unopened",
        "hub_open_plan": None,
        "hub_open_evidence": None,
        "hub_prove_plan": None,
        "hub_proved_evidence": None,
        "hub_commit_evidence": None,
        "hub_abort_evidence": None,
        "release_plan": None,
        "commit_not_applied_evidence": None,
        "state": "preparing",
        "phase": "routing",
        "revision": 0,
        "owner": _owner(args.owner_nonce, args.owner_pid),
        "cohort": cohort,
        "operations": [],
        "created_at": now,
        "updated_at": now,
    }
    journal["binding_sha256"] = _sha256(_binding_projection(journal))
    _validate_journal(journal, expected_epoch=epoch_id)
    journals = directory.all()
    for existing in journals:
        _verified_hub_plan_path(directory, existing, phase="open")
        _verified_hub_plan_path(directory, existing, phase="prove")
        _verified_release_plan_path(directory, existing)
    for existing in journals:
        if existing["epoch_id"] == epoch_id:
            if existing["binding_sha256"] != journal["binding_sha256"]:
                raise JournalError(
                    "binding_conflict",
                    "epoch already exists with a different immutable binding",
                    exit_code=3,
                )
            same_owner = (
                existing["owner"]["nonce"] == journal["owner"]["nonce"]
                and existing["owner"]["pid"] == journal["owner"]["pid"]
                and existing["owner"]["boot_id_sha256"]
                == journal["owner"]["boot_id_sha256"]
                and existing["owner"]["process_start_sha256"]
                == journal["owner"]["process_start_sha256"]
            )
            if existing["state"] not in TERMINAL_STATES and not same_owner:
                raise JournalError(
                    "owner_conflict",
                    "active epoch already has a different controller owner",
                    exit_code=3,
                )
            return existing, False
    incomplete = [item for item in journals if item["state"] not in TERMINAL_STATES]
    if incomplete:
        raise JournalError(
            "active_epoch_conflict",
            f"incomplete epoch {incomplete[0]['epoch_id']!r} must be recovered first",
            exit_code=3,
        )
    directory.write(journal)
    return journal, True


def command_hub_route_bound(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    identity = _endpoint_identity(args.identity_file)
    if identity["adapter"] not in {"ssh-hub", "kubernetes-hub"}:
        raise JournalError(
            "invalid_evidence", "hub route identity must bind durable store authority"
        )

    def transition(journal: dict[str, Any]) -> bool:
        _require_forward_direction(journal)
        if journal["state"] != "preparing" or journal["hub_state"] != "unopened":
            raise JournalError("invalid_transition", "hub route must bind before open")
        if journal["hub_route_identity"] is not None:
            if journal["hub_route_identity"] != identity:
                raise JournalError(
                    "evidence_binding_conflict", "hub route identity changed"
                )
            return False
        journal["hub_route_identity"] = identity
        _refresh_phase(journal)
        return True

    return _mutate(
        directory,
        args,
        "hub-route-bound",
        {"identity_sha256": _sha256(identity)},
        transition,
    )


def command_route_bound(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    identity = _endpoint_identity(args.identity_file)
    if identity["adapter"] not in {"ssh-machine", "kubernetes-workload"}:
        raise JournalError("invalid_evidence", "node route identity uses a hub adapter")

    def transition(journal: dict[str, Any]) -> bool:
        _require_forward_direction(journal)
        if journal["hub_route_identity"] is None or journal["hub_state"] != "unopened":
            raise JournalError(
                "invalid_transition", "node routes bind after the hub route"
            )
        node = _node(journal, args)
        if node["state"] == "route_bound":
            if node["route_identity"] != identity:
                raise JournalError(
                    "evidence_binding_conflict", "node route identity changed"
                )
            return False
        if node["state"] != "planned":
            raise JournalError(
                "invalid_transition", "route binding requires planned node"
            )
        earlier = journal["cohort"][: node["ordinal"]]
        later = journal["cohort"][node["ordinal"] + 1 :]
        if any(item["state"] != "route_bound" for item in earlier) or any(
            item["state"] != "planned" for item in later
        ):
            raise JournalError("invalid_transition", "routes must bind in cohort order")
        node["route_identity"] = identity
        node["state"] = "route_bound"
        _refresh_phase(journal)
        return True

    return _mutate(
        directory,
        args,
        "route-bound",
        {**_node_details(args), "identity_sha256": _sha256(identity)},
        transition,
    )


def command_phase1_prepare_started(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    def transition(journal: dict[str, Any]) -> bool:
        _require_forward_direction(journal)
        node = _node(journal, args)
        if node["state"] == "phase1_prepare_started":
            return False
        if node["state"] != "route_bound":
            raise JournalError(
                "invalid_transition", "phase1 prepare requires a bound route"
            )
        earlier = journal["cohort"][: node["ordinal"]]
        later = journal["cohort"][node["ordinal"] + 1 :]
        if any(
            item["state"] not in {"phase1_prepare_started", "phase1_armed"}
            for item in earlier
        ) or any(
            item["state"] != "route_bound" for item in later
        ):
            raise JournalError(
                "invalid_transition", "phase1 prepares must follow cohort order"
            )
        node["state"] = "phase1_prepare_started"
        _refresh_phase(journal)
        return True

    return _mutate(
        directory,
        args,
        "phase1-prepare-start",
        _node_details(args),
        transition,
    )


def command_phase1_armed(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    evidence_record = _evidence(args.evidence_file, args.generation)
    restore_digest = _digest(args.restore_contract_sha256, "restore contract sha256")

    def transition(journal: dict[str, Any]) -> bool:
        _require_forward_direction(journal)
        node = _node(journal, args)
        if node["state"] == "phase1_armed":
            if (
                node["restore_contract_sha256"] != restore_digest
                or node["phase1_arm_evidence"] != evidence_record
            ):
                raise JournalError(
                    "evidence_binding_conflict", "phase1 arm binding changed"
                )
            return False
        if node["state"] != "phase1_prepare_started":
            raise JournalError(
                "invalid_transition", "phase1 arm requires durable prepare intent"
            )
        earlier = journal["cohort"][: node["ordinal"]]
        later = journal["cohort"][node["ordinal"] + 1 :]
        if any(item["state"] != "phase1_armed" for item in earlier) or any(
            item["state"] not in {"route_bound", "phase1_prepare_started"}
            for item in later
        ):
            raise JournalError(
                "invalid_transition", "phase1 arms must follow cohort order"
            )
        node["phase1_arm_evidence"] = evidence_record
        node["restore_contract_sha256"] = restore_digest
        node["state"] = "phase1_armed"
        _refresh_phase(journal)
        return True

    details = {
        **_node_details(args),
        "evidence_sha256": evidence_record["sha256"],
        "restore_contract_sha256": restore_digest,
    }
    return _mutate(directory, args, "phase1-armed", details, transition)


def command_hub_open_start(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    raw = _read_secure_file(
        Path(args.open_plan_file), MAX_EVIDENCE_BYTES, "hub open plan"
    )
    current = directory.read_epoch(_epoch(args.epoch_id))
    _parsed, metadata = _hub_plan(raw, current, phase="open")

    def transition(journal: dict[str, Any]) -> bool:
        _require_forward_direction(journal)
        if any(node["state"] != "phase1_armed" for node in journal["cohort"]):
            raise JournalError(
                "invalid_transition", "hub open requires every phase1 restore arm"
            )
        if journal["hub_state"] == "open_intent":
            if journal["hub_open_plan"] != metadata:
                raise JournalError("evidence_binding_conflict", "hub open plan changed")
            return False
        if journal["hub_state"] != "unopened":
            raise JournalError("invalid_transition", "hub participant already advanced")
        # Persist the immutable request only after owner fencing and the CAS check
        # in _mutate have succeeded.  A stale controller must not be able to
        # strand an orphan plan that blocks the live owner.
        directory.write_auxiliary(metadata["filename"], raw)
        journal["hub_open_plan"] = metadata
        journal["hub_state"] = "open_intent"
        _refresh_phase(journal)
        return True

    return _mutate(
        directory,
        args,
        "hub-open-start",
        {"plan_sha256": metadata["sha256"]},
        transition,
    )


def command_hub_opened(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    current = directory.read_epoch(_epoch(args.epoch_id))
    evidence_record = _hub_receipt_evidence(
        args.evidence_file, current, expected_status="open"
    )

    def transition(journal: dict[str, Any]) -> bool:
        _require_nonterminal(journal)
        if journal["hub_state"] == "open":
            if journal["hub_open_evidence"] != evidence_record:
                raise JournalError(
                    "evidence_binding_conflict", "hub open receipt changed"
                )
            return False
        if journal["hub_state"] != "open_intent":
            raise JournalError("invalid_transition", "hub open receipt lacks intent")
        _verified_hub_plan_path(directory, journal, phase="open")
        journal["hub_open_evidence"] = evidence_record
        journal["hub_state"] = "open"
        _refresh_phase(journal)
        return True

    return _mutate(
        directory,
        args,
        "hub-opened",
        {"evidence_sha256": evidence_record["sha256"]},
        transition,
    )


def command_quiesce_start(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    def transition(journal: dict[str, Any]) -> bool:
        _require_forward_direction(journal)
        if journal["hub_state"] != "open":
            raise JournalError("invalid_transition", "quiesce requires open hub epoch")
        node = _node(journal, args)
        if node["state"] == "quiesce_started":
            return False
        if node["state"] != "phase1_armed":
            raise JournalError(
                "invalid_transition", "quiesce start requires phase1 arm"
            )
        earlier = journal["cohort"][: node["ordinal"]]
        later = journal["cohort"][node["ordinal"] + 1 :]
        if any(item["state"] != "quiesced" for item in earlier) or any(
            item["state"] != "phase1_armed" for item in later
        ):
            raise JournalError(
                "invalid_transition", "nodes must quiesce in cohort order"
            )
        node["state"] = "quiesce_started"
        _refresh_phase(journal)
        return True

    return _mutate(directory, args, "quiesce-start", _node_details(args), transition)


def command_quiesced(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    evidence_record = _evidence(args.evidence_file, args.generation)

    def transition(journal: dict[str, Any]) -> bool:
        _require_forward_direction(journal)
        node = _node(journal, args)
        if node["state"] == "quiesced":
            if node["quiescence_evidence"] != evidence_record:
                raise JournalError(
                    "evidence_binding_conflict", "quiescence proof changed"
                )
            return False
        if node["state"] != "quiesce_started":
            raise JournalError("invalid_transition", "quiescence proof lacks intent")
        node["quiescence_evidence"] = evidence_record
        node["state"] = "quiesced"
        _refresh_phase(journal)
        return True

    return _mutate(
        directory,
        args,
        "quiesced",
        {**_node_details(args), "evidence_sha256": evidence_record["sha256"]},
        transition,
    )


def command_phase2_armed(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    evidence_record = _evidence(args.evidence_file, args.generation)
    intent_digest = _digest(args.rollback_intent_sha256, "rollback intent sha256")
    finalizer_digest = _digest(args.finalizer_sha256, "finalizer sha256")

    def transition(journal: dict[str, Any]) -> bool:
        _require_forward_direction(journal)
        node = _node(journal, args)
        if node["state"] == "phase2_armed":
            if (
                node["rollback_intent_sha256"] != intent_digest
                or node["finalizer_sha256"] != finalizer_digest
                or node["phase2_arm_evidence"] != evidence_record
            ):
                raise JournalError("evidence_binding_conflict", "phase2 arm changed")
            return False
        if node["state"] != "quiesced":
            raise JournalError("invalid_transition", "phase2 arm requires quiescence")
        earlier = journal["cohort"][: node["ordinal"]]
        later = journal["cohort"][node["ordinal"] + 1 :]
        if any(item["state"] != "phase2_armed" for item in earlier) or any(
            item["state"] != "quiesced" for item in later
        ):
            raise JournalError(
                "invalid_transition", "phase2 arms must follow cohort order"
            )
        node["rollback_intent_sha256"] = intent_digest
        node["finalizer_sha256"] = finalizer_digest
        node["phase2_arm_evidence"] = evidence_record
        node["state"] = "phase2_armed"
        _refresh_phase(journal)
        return True

    return _mutate(
        directory,
        args,
        "phase2-armed",
        {
            **_node_details(args),
            "evidence_sha256": evidence_record["sha256"],
            "rollback_intent_sha256": intent_digest,
            "finalizer_sha256": finalizer_digest,
        },
        transition,
    )


def command_phase2_start(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    def transition(journal: dict[str, Any]) -> bool:
        _require_forward_direction(journal)
        node = _node(journal, args)
        if node["state"] == "phase2_started":
            return False
        if node["state"] != "phase2_armed":
            raise JournalError(
                "invalid_transition", "phase2 start lacks rollback intent"
            )
        earlier = journal["cohort"][: node["ordinal"]]
        later = journal["cohort"][node["ordinal"] + 1 :]
        if any(item["state"] != "prepared" for item in earlier) or any(
            item["state"] != "phase2_armed" for item in later
        ):
            raise JournalError(
                "invalid_transition", "nodes must deploy in cohort order"
            )
        node["state"] = "phase2_started"
        _refresh_phase(journal)
        return True

    return _mutate(directory, args, "phase2-start", _node_details(args), transition)


def command_prepared(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    evidence_record = _evidence(args.evidence_file, args.generation)

    def transition(journal: dict[str, Any]) -> bool:
        _require_forward_direction(journal)
        node = _node(journal, args)
        if node["state"] == "prepared":
            if node["prepared_evidence"] != evidence_record:
                raise JournalError(
                    "evidence_binding_conflict", "prepared proof changed"
                )
            return False
        if node["state"] != "phase2_started":
            raise JournalError(
                "invalid_transition", "prepared proof lacks phase2 intent"
            )
        node["prepared_evidence"] = evidence_record
        node["state"] = "prepared"
        _refresh_phase(journal)
        return True

    return _mutate(
        directory,
        args,
        "prepared",
        {**_node_details(args), "evidence_sha256": evidence_record["sha256"]},
        transition,
    )


def command_hub_prove_start(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    raw = _read_secure_file(
        Path(args.prove_plan_file), MAX_EVIDENCE_BYTES, "hub prove plan"
    )
    current = directory.read_epoch(_epoch(args.epoch_id))
    _parsed, metadata = _hub_plan(raw, current, phase="prove")

    def transition(journal: dict[str, Any]) -> bool:
        _require_forward_direction(journal)
        if any(node["state"] != "prepared" for node in journal["cohort"]):
            raise JournalError(
                "invalid_transition", "hub prove requires every node prepared"
            )
        if journal["hub_state"] == "prove_intent":
            if journal["hub_prove_plan"] != metadata:
                raise JournalError(
                    "evidence_binding_conflict", "hub prove plan changed"
                )
            return False
        if journal["hub_state"] != "open":
            raise JournalError("invalid_transition", "hub prove requires open epoch")
        directory.write_auxiliary(metadata["filename"], raw)
        journal["hub_prove_plan"] = metadata
        journal["hub_state"] = "prove_intent"
        _refresh_phase(journal)
        return True

    return _mutate(
        directory,
        args,
        "hub-prove-start",
        {"plan_sha256": metadata["sha256"]},
        transition,
    )


def command_hub_proved(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    current = directory.read_epoch(_epoch(args.epoch_id))
    evidence_record = _hub_receipt_evidence(
        args.evidence_file, current, expected_status="proved"
    )

    def transition(journal: dict[str, Any]) -> bool:
        _require_nonterminal(journal)
        if journal["hub_state"] == "proved":
            if journal["hub_proved_evidence"] != evidence_record:
                raise JournalError(
                    "evidence_binding_conflict", "hub proved receipt changed"
                )
            return False
        if journal["hub_state"] != "prove_intent":
            raise JournalError("invalid_transition", "hub proved receipt lacks intent")
        _verified_hub_plan_path(directory, journal, phase="prove")
        journal["hub_proved_evidence"] = evidence_record
        journal["hub_state"] = "proved"
        _refresh_phase(journal)
        return True

    return _mutate(
        directory,
        args,
        "hub-proved",
        {"evidence_sha256": evidence_record["sha256"]},
        transition,
    )


def command_abort_start(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    def transition(journal: dict[str, Any]) -> bool:
        _require_nonterminal(journal)
        node = _node(journal, args)
        if node["state"] == "aborting":
            return False
        candidates = _rollback_candidates(journal)
        if not candidates or candidates[0]["agent_name"] != node["name"]:
            raise JournalError(
                "invalid_transition", "nodes must recover in reverse mutation order"
            )
        action = candidates[0]["recovery_action"]
        if args.recovery_action is not None and args.recovery_action != action:
            raise JournalError(
                "evidence_binding_conflict",
                "requested recovery contradicts durable intent",
            )
        node["abort_from_state"] = node["state"]
        node["abort_kind"] = action
        node["state"] = "aborting"
        journal["state"] = "aborting"
        _refresh_phase(journal)
        return True

    details = {**_node_details(args), "recovery_action": args.recovery_action}
    return _mutate(directory, args, "abort-start", details, transition)


def command_aborted_node(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    evidence_record = _evidence(args.evidence_file, args.generation)

    def transition(journal: dict[str, Any]) -> bool:
        _require_nonterminal(journal)
        node = _node(journal, args)
        if node["state"] == "aborted":
            if node["abort_evidence"] != evidence_record:
                raise JournalError("evidence_binding_conflict", "abort proof changed")
            return False
        if node["state"] != "aborting":
            raise JournalError(
                "invalid_transition", "abort proof lacks recovery intent"
            )
        node["abort_evidence"] = evidence_record
        node["state"] = "aborted"
        _refresh_phase(journal)
        return True

    return _mutate(
        directory,
        args,
        "aborted-node",
        {**_node_details(args), "evidence_sha256": evidence_record["sha256"]},
        transition,
    )


def command_hub_aborted(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    current = directory.read_epoch(_epoch(args.epoch_id))
    evidence_record = _hub_receipt_evidence(
        args.evidence_file,
        current,
        expected_status="aborted",
        bind_release_plan=current["release_plan"] is not None,
    )

    def transition(journal: dict[str, Any]) -> bool:
        if journal["hub_state"] == "aborted":
            if journal["hub_abort_evidence"] != evidence_record:
                raise JournalError(
                    "evidence_binding_conflict", "hub abort proof changed"
                )
            return False
        _require_nonterminal(journal)
        if journal["state"] in {"commit_intent", "hub_committed"}:
            raise JournalError(
                "invalid_transition",
                "hub abort requires an exact proved not-applied receipt",
            )
        if journal["hub_state"] not in {"open", "proved"}:
            raise JournalError(
                "invalid_transition", "hub participant has no abort intent"
            )
        journal["hub_abort_evidence"] = evidence_record
        journal["hub_state"] = "aborted"
        journal["state"] = "aborting"
        _refresh_phase(journal)
        return True

    return _mutate(
        directory,
        args,
        "hub-aborted",
        {"evidence_sha256": evidence_record["sha256"]},
        transition,
    )


def command_commit_start(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    raw = _read_secure_file(
        Path(args.release_plan_file), MAX_EVIDENCE_BYTES, "release plan file"
    )
    raw_digest = hashlib.sha256(raw).hexdigest()

    def transition(journal: dict[str, Any]) -> bool:
        _require_forward_direction(journal)
        if journal["state"] == "commit_intent":
            if (
                journal["release_plan"]
                and journal["release_plan"]["sha256"] == raw_digest
            ):
                return False
            raise JournalError("release_plan_conflict", "commit intent plan changed")
        if (
            journal["state"] != "preparing"
            or journal["hub_state"] != "proved"
            or any(node["state"] != "prepared" for node in journal["cohort"])
        ):
            raise JournalError(
                "invalid_transition",
                "commit intent requires every node and the hub proved",
            )
        _parsed, metadata = _release_plan(raw, journal)
        directory.write_auxiliary(metadata["filename"], raw)
        journal["release_plan"] = metadata
        journal["state"] = "commit_intent"
        journal["phase"] = "commit_intent"
        return True

    return _mutate(
        directory,
        args,
        "commit-start",
        {"release_plan_sha256": raw_digest},
        transition,
    )


def command_commit_not_applied(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    current = directory.read_epoch(_epoch(args.epoch_id))
    evidence_record = _hub_receipt_evidence(
        args.evidence_file,
        current,
        expected_status="proved",
        bind_release_plan=True,
    )

    def transition(journal: dict[str, Any]) -> bool:
        _require_nonterminal(journal)
        if journal["state"] != "commit_intent":
            raise JournalError(
                "invalid_transition",
                "proved commit-not-applied receipt requires commit intent",
            )
        _verified_release_plan_path(directory, journal)
        journal["commit_not_applied_evidence"] = evidence_record
        journal["state"] = "aborting"
        journal["phase"] = "aborting"
        return True

    return _mutate(
        directory,
        args,
        "commit-not-applied",
        {"evidence_sha256": evidence_record["sha256"]},
        transition,
    )


def command_commit(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    current = directory.read_epoch(_epoch(args.epoch_id))
    evidence_record = _hub_receipt_evidence(
        args.evidence_file,
        current,
        expected_status="committed",
        bind_release_plan=True,
    )

    def transition(journal: dict[str, Any]) -> bool:
        if journal["state"] in {"hub_committed", "finalized"}:
            if journal["hub_commit_evidence"] != evidence_record:
                raise JournalError(
                    "evidence_binding_conflict", "hub commit receipt changed"
                )
            return False
        _require_nonterminal(journal)
        if journal["state"] != "commit_intent" or any(
            node["state"] != "prepared" for node in journal["cohort"]
        ):
            raise JournalError(
                "invalid_transition", "commit requires a durable exact commit intent"
            )
        _verified_release_plan_path(directory, journal)
        if journal["hub_state"] != "proved":
            raise JournalError("invalid_transition", "hub commit lacks proved epoch")
        journal["hub_commit_evidence"] = evidence_record
        journal["state"] = "hub_committed"
        journal["hub_state"] = "committed"
        journal["phase"] = "finalizing"
        return True

    return _mutate(
        directory,
        args,
        "commit",
        {"evidence_sha256": evidence_record["sha256"]},
        transition,
    )


def command_finalize_start(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    def transition(journal: dict[str, Any]) -> bool:
        if journal["state"] != "hub_committed":
            raise JournalError(
                "invalid_transition", "finalization requires committed hub"
            )
        node = _node(journal, args)
        if node["state"] == "finalizing":
            return False
        if node["state"] != "prepared":
            raise JournalError("invalid_transition", "node cannot begin finalization")
        earlier = journal["cohort"][: node["ordinal"]]
        later = journal["cohort"][node["ordinal"] + 1 :]
        if any(item["state"] != "finalized" for item in earlier) or any(
            item["state"] != "prepared" for item in later
        ):
            raise JournalError(
                "invalid_transition", "nodes must finalize in exact cohort order"
            )
        node["state"] = "finalizing"
        _refresh_phase(journal)
        return True

    return _mutate(directory, args, "finalize-start", _node_details(args), transition)


def command_finalized_node(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    evidence_record = _evidence(args.evidence_file, args.generation)

    def transition(journal: dict[str, Any]) -> bool:
        if journal["state"] != "hub_committed":
            raise JournalError("invalid_transition", "node finalization lacks commit")
        node = _node(journal, args)
        if node["state"] == "finalized":
            if node["finalize_evidence"] != evidence_record:
                raise JournalError(
                    "evidence_binding_conflict", "finalize proof changed"
                )
            return False
        if node["state"] != "finalizing":
            raise JournalError("invalid_transition", "finalize proof lacks intent")
        node["finalize_evidence"] = evidence_record
        node["state"] = "finalized"
        _refresh_phase(journal)
        return True

    return _mutate(
        directory,
        args,
        "finalized-node",
        {**_node_details(args), "evidence_sha256": evidence_record["sha256"]},
        transition,
    )


def command_finalize(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    def transition(journal: dict[str, Any]) -> bool:
        if journal["state"] == "finalized":
            return False
        _require_nonterminal(journal)
        if journal["state"] != "hub_committed" or any(
            node["state"] != "finalized" for node in journal["cohort"]
        ):
            raise JournalError(
                "invalid_transition", "every committed node must finalize first"
            )
        journal["state"] = "finalized"
        journal["phase"] = "finalized"
        return True

    return _mutate(directory, args, "finalize", {}, transition)


def command_abort(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    def transition(journal: dict[str, Any]) -> bool:
        if journal["state"] == "aborted":
            return False
        _require_nonterminal(journal)
        if _rollback_candidates(journal):
            raise JournalError(
                "invalid_transition", "all recovery candidates must be aborted first"
            )
        if journal["hub_state"] not in {"unopened", "aborted"}:
            raise JournalError(
                "invalid_transition", "hub epoch cleanup must be proved before abort"
            )
        if any(
            node["state"] not in {"planned", "route_bound", "aborted"}
            for node in journal["cohort"]
        ):
            raise JournalError(
                "invalid_transition", "every mutated cohort node needs recovery proof"
            )
        journal["state"] = "aborted"
        journal["phase"] = "aborted"
        return True

    return _mutate(directory, args, "abort", {}, transition)


def command_adopt(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    new_owner = _owner(args.new_owner_nonce, args.new_owner_pid)
    details = {
        "previous_owner_nonce": args.previous_owner_nonce,
        "new_owner_nonce": new_owner["nonce"],
        "new_owner_pid": new_owner["pid"],
        "new_owner_boot_id_sha256": new_owner["boot_id_sha256"],
        "new_owner_process_start_sha256": new_owner["process_start_sha256"],
    }

    def transition(journal: dict[str, Any]) -> bool:
        _require_nonterminal(journal)
        previous = _text(
            args.previous_owner_nonce, "previous owner nonce", max_bytes=256, token=True
        )
        if journal["owner"]["nonce"] != previous:
            raise JournalError(
                "owner_fenced", "previous owner nonce does not match", exit_code=3
            )
        if _owner_alive(journal["owner"]):
            raise JournalError(
                "owner_live", "live journal owner cannot be adopted", exit_code=3
            )
        journal["owner"] = new_owner
        return True

    return _mutate(
        directory,
        args,
        "adopt",
        details,
        transition,
        check_owner=False,
    )


def command_status(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    journal = directory.read_epoch(_epoch(args.epoch_id))
    hub_open_plan_path = _verified_hub_plan_path(directory, journal, phase="open")
    hub_prove_plan_path = _verified_hub_plan_path(directory, journal, phase="prove")
    release_plan_path = _verified_release_plan_path(directory, journal)
    return {
        "summary": _summary(journal),
        "journal": journal,
        "hub_open_plan_path": hub_open_plan_path,
        "hub_prove_plan_path": hub_prove_plan_path,
        "release_plan_path": release_plan_path,
    }, False


def command_discover(
    directory: JournalDirectory, _args: argparse.Namespace
) -> tuple[Any, bool]:
    journals = directory.all()
    for journal in journals:
        _verified_hub_plan_path(directory, journal, phase="open")
        _verified_hub_plan_path(directory, journal, phase="prove")
        _verified_release_plan_path(directory, journal)
    active = [
        journal for journal in journals if journal["state"] not in TERMINAL_STATES
    ]
    if len(active) > 1:
        raise JournalError(
            "multiple_active_epochs", "more than one incomplete cohort epoch exists"
        )
    return {
        "active": _summary(active[0]) if active else None,
        "journals": [_summary(journal) for journal in journals],
    }, False


def command_recovery(
    directory: JournalDirectory, args: argparse.Namespace
) -> tuple[Any, bool]:
    if args.epoch_id:
        journal = directory.read_epoch(_epoch(args.epoch_id))
    else:
        active = [
            item for item in directory.all() if item["state"] not in TERMINAL_STATES
        ]
        if not active:
            return {
                "recovery_required": False,
                "epoch": None,
                "direction": "none",
                "hub_recovery": None,
                "candidates": [],
                "finalization_candidates": [],
            }, False
        if len(active) > 1:
            raise JournalError(
                "multiple_active_epochs", "more than one incomplete epoch exists"
            )
        journal = active[0]
    hub_open_plan_path = _verified_hub_plan_path(directory, journal, phase="open")
    hub_prove_plan_path = _verified_hub_plan_path(directory, journal, phase="prove")
    verified_plan_path = _verified_release_plan_path(directory, journal)
    commit_intent = journal["state"] == "commit_intent"
    if commit_intent:
        direction = "resolve_commit"
    elif journal["state"] == "hub_committed":
        direction = "finalize"
    elif journal["state"] in TERMINAL_STATES:
        direction = "none"
    else:
        direction = "rollback"
    hub_action = {
        "unopened": "none",
        "open_intent": "resolve_open",
        "open": "abort_epoch" if direction == "rollback" else "none",
        "prove_intent": "resolve_prove",
        "proved": ("abort_epoch" if direction == "rollback" else "resolve_commit"),
        "aborted": "none",
        "committed": "none",
    }[journal["hub_state"]]
    return {
        "recovery_required": journal["state"] not in TERMINAL_STATES,
        "epoch": _summary(journal),
        "direction": direction,
        "replay_release_plan": commit_intent,
        "release_plan_path": verified_plan_path if commit_intent else None,
        "hub_recovery": {
            "action": hub_action,
            "agent_name": journal["hub_agent"],
            "route_identity": journal["hub_route_identity"],
            "open_plan_path": hub_open_plan_path,
            "prove_plan_path": hub_prove_plan_path,
            "identity_sha256": (
                journal["hub_open_evidence"]["identity_sha256"]
                if journal["hub_open_evidence"] is not None
                else None
            ),
            "proof_sha256": (
                journal["hub_proved_evidence"]["proof_sha256"]
                if journal["hub_proved_evidence"] is not None
                else None
            ),
        },
        "candidates": _rollback_candidates(journal),
        "finalization_candidates": _finalization_candidates(journal),
    }, False


def _node_details(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "agent_name": args.agent_name,
        "stable_id": args.stable_id,
        "generation": args.generation,
    }


def _add_epoch(parser: argparse.ArgumentParser, *, optional: bool = False) -> None:
    parser.add_argument("--epoch", "--epoch-id", dest="epoch_id", required=not optional)


def _add_operation(parser: argparse.ArgumentParser, *, owner: bool = True) -> None:
    _add_epoch(parser)
    parser.add_argument("--expected-revision", required=True, type=int)
    parser.add_argument("--operation-id", required=True)
    if owner:
        parser.add_argument(
            "--owner-nonce",
            default=os.environ.get("MAC_DEPLOY_CONTROLLER_NONCE"),
            required=False,
        )


def _add_node(parser: argparse.ArgumentParser, *, evidence: bool = False) -> None:
    parser.add_argument("--agent-name", required=True)
    parser.add_argument("--stable-id", required=True)
    parser.add_argument("--generation", required=True)
    if evidence:
        parser.add_argument("--evidence-file", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory",
        default=os.environ.get(
            "MAC_FLEET_COHORT_JOURNAL_DIR",
            str(Path.home() / ".mac" / "fleet-cohort-transactions"),
        ),
        help="owner-private journal directory (default: ~/.mac/fleet-cohort-transactions)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    _add_epoch(init)
    init.add_argument("--source-commit", required=True)
    init.add_argument("--deploy-ts", required=True)
    init.add_argument("--fleet", required=True)
    init.add_argument("--hub-agent", required=True)
    init.add_argument("--successor-hold", default="")
    init.add_argument("--require-release-all-selected", action="store_true")
    init.add_argument("--cohort-file", required=True)
    init.add_argument(
        "--owner-nonce",
        default=os.environ.get("MAC_DEPLOY_CONTROLLER_NONCE"),
        required=False,
    )
    init.add_argument(
        "--owner-pid",
        default=os.environ.get("MAC_DEPLOY_CONTROLLER_PID"),
        required=False,
    )
    init.set_defaults(handler=command_init)

    hub_route = commands.add_parser("hub-route-bound")
    _add_operation(hub_route)
    hub_route.add_argument("--identity-file", required=True)
    hub_route.set_defaults(handler=command_hub_route_bound)

    route = commands.add_parser("route-bound")
    _add_operation(route)
    _add_node(route)
    route.add_argument("--identity-file", required=True)
    route.set_defaults(handler=command_route_bound)

    phase1_prepare = commands.add_parser("phase1-prepare-start")
    _add_operation(phase1_prepare)
    _add_node(phase1_prepare)
    phase1_prepare.set_defaults(handler=command_phase1_prepare_started)

    phase1 = commands.add_parser("phase1-armed")
    _add_operation(phase1)
    _add_node(phase1, evidence=True)
    phase1.add_argument("--restore-contract-sha256", required=True)
    phase1.set_defaults(handler=command_phase1_armed)

    hub_open_start = commands.add_parser("hub-open-start")
    _add_operation(hub_open_start)
    hub_open_start.add_argument("--open-plan-file", required=True)
    hub_open_start.set_defaults(handler=command_hub_open_start)

    hub_opened = commands.add_parser("hub-opened")
    _add_operation(hub_opened)
    hub_opened.add_argument("--evidence-file", required=True)
    hub_opened.set_defaults(handler=command_hub_opened)

    quiesce_start = commands.add_parser("quiesce-start")
    _add_operation(quiesce_start)
    _add_node(quiesce_start)
    quiesce_start.set_defaults(handler=command_quiesce_start)

    quiesced = commands.add_parser("quiesced")
    _add_operation(quiesced)
    _add_node(quiesced, evidence=True)
    quiesced.set_defaults(handler=command_quiesced)

    phase2_armed = commands.add_parser("phase2-armed")
    _add_operation(phase2_armed)
    _add_node(phase2_armed, evidence=True)
    phase2_armed.add_argument("--rollback-intent-sha256", required=True)
    phase2_armed.add_argument("--finalizer-sha256", required=True)
    phase2_armed.set_defaults(handler=command_phase2_armed)

    phase2_start = commands.add_parser("phase2-start")
    _add_operation(phase2_start)
    _add_node(phase2_start)
    phase2_start.set_defaults(handler=command_phase2_start)

    prepared = commands.add_parser("prepared")
    _add_operation(prepared)
    _add_node(prepared, evidence=True)
    prepared.set_defaults(handler=command_prepared)

    hub_prove_start = commands.add_parser("hub-prove-start")
    _add_operation(hub_prove_start)
    hub_prove_start.add_argument("--prove-plan-file", required=True)
    hub_prove_start.set_defaults(handler=command_hub_prove_start)

    hub_proved = commands.add_parser("hub-proved")
    _add_operation(hub_proved)
    hub_proved.add_argument("--evidence-file", required=True)
    hub_proved.set_defaults(handler=command_hub_proved)

    abort_start = commands.add_parser("abort-start")
    _add_operation(abort_start)
    _add_node(abort_start)
    abort_start.add_argument(
        "--recovery-action",
        choices=("cleanup_only", "phase1_restore", "phase2_rollback"),
    )
    abort_start.set_defaults(handler=command_abort_start)

    aborted_node = commands.add_parser("aborted-node")
    _add_operation(aborted_node)
    _add_node(aborted_node, evidence=True)
    aborted_node.set_defaults(handler=command_aborted_node)

    hub_aborted = commands.add_parser("hub-aborted")
    _add_operation(hub_aborted)
    hub_aborted.add_argument("--evidence-file", required=True)
    hub_aborted.set_defaults(handler=command_hub_aborted)

    commit_start = commands.add_parser("commit-start")
    _add_operation(commit_start)
    commit_start.add_argument("--release-plan-file", required=True)
    commit_start.set_defaults(handler=command_commit_start)

    commit_not_applied = commands.add_parser("commit-not-applied")
    _add_operation(commit_not_applied)
    commit_not_applied.add_argument("--evidence-file", required=True)
    commit_not_applied.set_defaults(handler=command_commit_not_applied)

    commit = commands.add_parser("commit")
    _add_operation(commit)
    commit.add_argument("--evidence-file", required=True)
    commit.set_defaults(handler=command_commit)

    finalize_start = commands.add_parser("finalize-start")
    _add_operation(finalize_start)
    _add_node(finalize_start)
    finalize_start.set_defaults(handler=command_finalize_start)

    finalized_node = commands.add_parser("finalized-node")
    _add_operation(finalized_node)
    _add_node(finalized_node, evidence=True)
    finalized_node.set_defaults(handler=command_finalized_node)

    finalize = commands.add_parser("finalize")
    _add_operation(finalize)
    finalize.set_defaults(handler=command_finalize)

    abort = commands.add_parser("abort")
    _add_operation(abort)
    abort.set_defaults(handler=command_abort)

    adopt = commands.add_parser("adopt")
    _add_operation(adopt, owner=False)
    adopt.add_argument("--previous-owner-nonce", required=True)
    adopt.add_argument("--new-owner-nonce", required=True)
    adopt.add_argument("--new-owner-pid", required=True)
    adopt.set_defaults(handler=command_adopt)

    status = commands.add_parser("status")
    _add_epoch(status)
    status.set_defaults(handler=command_status)

    discover = commands.add_parser("discover")
    discover.set_defaults(handler=command_discover)

    recovery = commands.add_parser("recovery")
    _add_epoch(recovery, optional=True)
    recovery.set_defaults(handler=command_recovery)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if hasattr(args, "owner_nonce") and args.owner_nonce is None:
            raise JournalError("invalid_input", "owner nonce is required")
        if args.command == "init" and args.owner_pid is None:
            raise JournalError("invalid_input", "owner pid is required")
        directory_path = Path(args.directory)
        with JournalDirectory(directory_path) as directory:
            value, changed = args.handler(directory, args)
        mutation_commands = {
            "init",
            "hub-route-bound",
            "route-bound",
            "phase1-prepare-start",
            "phase1-armed",
            "hub-open-start",
            "hub-opened",
            "quiesce-start",
            "quiesced",
            "phase2-armed",
            "phase2-start",
            "prepared",
            "hub-prove-start",
            "hub-proved",
            "abort-start",
            "aborted-node",
            "hub-aborted",
            "commit-start",
            "commit-not-applied",
            "commit",
            "finalize-start",
            "finalized-node",
            "finalize",
            "abort",
            "adopt",
        }
        if args.command in mutation_commands:
            output = {"ok": True, "changed": changed, "journal": value}
        else:
            output = {"ok": True, "changed": changed, **value}
        print(json.dumps(output, sort_keys=True))
        return 0
    except JournalError as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"code": exc.code, "message": str(exc)}},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return exc.exit_code
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "interrupted",
                        "message": "operation interrupted",
                    },
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
