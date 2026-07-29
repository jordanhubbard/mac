"""Typed static-host OpenShell runtime refresh.

Synchronized *static* deployments install host source and then re-attest. The
typed static phase deliberately consumes already-proven infrastructure receipts
and forbids OpenShell mutation, so a deployment that also carried a **requested
immutable runtime identity** could pass every startup/agent/OpenClaw/Slack
health gate while its ``report_repository_executor_attestation`` still named the
superseded runtime digest. Nothing in the static phase was wrong; there simply
was no explicit path that was *allowed* to change the pinned runtime.

This module is that path. It is a separate, explicitly requested, typed
refresh — never an implicit side effect of a static deploy:

``prove_sandbox_inventory``
    Read the sandbox inventory and prove quiescence. A busy sandbox is a hard
    stop *before* any mutation: the refresh replaces sandboxes, and replacing a
    sandbox that is executing a task destroys in-flight work.
``preserve_openclaw_state``
    Checkpoint active OpenClaw sessions before they are touched, so sandbox
    replacement is state-preserving rather than state-destroying.
``install_runtime_image``
    Install the reviewed **multi-architecture** digest. This is the mutation
    boundary: the effects contract requires ``install_runtime_image`` to raise
    *before* writing the pin and to return only *after* the immutable pin is
    installed, so the receipt can say truthfully whether the host was mutated.
``replace_sandboxes``
    Restart or replace every sandbox that is not already on the requested
    digest, then re-read the inventory to prove the replacement landed.
``restore_openclaw_state``
    Restore exactly the sessions preserved in the pre-mutation phase.
``verify_report_executor_attestation``
    The terminal gate. The post-deploy report-executor attestation must be a
    complete, valid attestation whose ``runtime_image_ref`` **equals** the
    requested digest. A refresh that ends on the old digest is a failure, not a
    success with a stale field.

Recovery is **fix-forward only**. Once the immutable pin has moved, re-pinning
the superseded digest would reintroduce the very identity the review replaced,
so a post-mutation failure emits typed recovery evidence naming the residual
drift and the retry action instead of rolling back.

The module is pure and effect-injected: every host interaction goes through
:class:`StaticRuntimeRefreshEffects`, so the whole path — including both
failure sides of the mutation boundary — is exercisable without touching real
infrastructure.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from mac.models import valid_read_only_report_repository_executor_attestation


REFRESH_REQUEST_SCHEMA = "mac.openshell_static_runtime_refresh_request.v1"
REFRESH_PLAN_SCHEMA = "mac.openshell_static_runtime_refresh_plan.v1"
REFRESH_RECEIPT_SCHEMA = "mac.openshell_static_runtime_refresh_receipt.v1"
INVENTORY_PROOF_SCHEMA = "mac.openshell_static_sandbox_inventory_proof.v1"
RECOVERY_SCHEMA = "mac.openshell_static_runtime_refresh_recovery.v1"

# The only runtime identity a static host may be refreshed to. Matching the
# executor-side guard keeps a mistyped or third-party reference from becoming
# an immutable host pin.
RUNTIME_IMAGE_REF_RE = re.compile(
    r"ghcr\.io/jordanhubbard/mac-openshell-runtime@sha256:[0-9a-f]{64}"
)
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")

PHASE_INVENTORY = "prove_sandbox_inventory"
PHASE_PRESERVE = "preserve_openclaw_state"
PHASE_INSTALL = "install_runtime_image"
PHASE_REPLACE = "replace_sandboxes"
PHASE_RESTORE = "restore_openclaw_state"
PHASE_ATTEST = "verify_report_executor_attestation"

# Phases that may change host state. Everything before the first of these is
# freely abortable; everything from here on requires fix-forward recovery.
MUTATING_PHASES: Tuple[str, ...] = (PHASE_INSTALL, PHASE_REPLACE, PHASE_RESTORE)

OUTCOME_UNCHANGED = "unchanged"
OUTCOME_UPGRADED = "upgraded"
OUTCOME_FAILED_BEFORE_MUTATION = "failed_before_mutation"
OUTCOME_FAILED_AFTER_MUTATION = "failed_after_mutation"

# A sandbox in any of these states owns work that a replacement would destroy.
BUSY_SANDBOX_STATES = frozenset({"creating", "starting", "running", "stopping"})
# Reviewed runtimes are published as a manifest list; a static fleet spans
# both architectures, so a single-arch install is a partial refresh.
DEFAULT_PLATFORMS: Tuple[str, ...] = ("linux/amd64", "linux/arm64")


class StaticRuntimeRefreshError(ValueError):
    """The typed static-host runtime refresh contract was violated."""


def _required(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text or "\x00" in text:
        raise StaticRuntimeRefreshError("%s is required" % label)
    return text


def _runtime_image_ref(value: Any, label: str) -> str:
    text = _required(value, label)
    if not RUNTIME_IMAGE_REF_RE.fullmatch(text):
        raise StaticRuntimeRefreshError(
            "%s must be the managed immutable mac-openshell-runtime@sha256 reference"
            % label
        )
    return text


def _sha256(value: Any, label: str) -> str:
    text = _required(value, label)
    if not SHA256_RE.fullmatch(text):
        raise StaticRuntimeRefreshError("%s must be a sha256:<hex> digest" % label)
    return text


def runtime_digest(image_ref: str) -> str:
    """Return the ``sha256:<hex>`` digest carried by a runtime image reference."""

    return _runtime_image_ref(image_ref, "runtime image reference").split("@", 1)[1]


@dataclass(frozen=True)
class SandboxRecord:
    """One observed OpenShell sandbox on the static host."""

    sandbox_id: str
    state: str
    runtime_image_ref: str
    agent_id: str = ""
    task_id: str = ""
    openclaw_session_id: str = ""

    @property
    def busy(self) -> bool:
        """Whether replacing this sandbox would destroy in-flight work."""

        return str(self.state or "").strip().lower() in BUSY_SANDBOX_STATES or bool(
            str(self.task_id or "").strip()
        )

    def as_json(self) -> Dict[str, Any]:
        return {
            "sandbox_id": self.sandbox_id,
            "state": self.state,
            "runtime_image_ref": self.runtime_image_ref,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "openclaw_session_id": self.openclaw_session_id,
            "busy": self.busy,
        }


def sandbox_record_from_json(value: Any) -> SandboxRecord:
    """Build a :class:`SandboxRecord` from an inventory mapping."""

    if not isinstance(value, Mapping):
        raise StaticRuntimeRefreshError("sandbox inventory entry must be a mapping")
    return SandboxRecord(
        sandbox_id=_required(value.get("sandbox_id"), "sandbox id"),
        state=_required(value.get("state"), "sandbox state"),
        runtime_image_ref=_runtime_image_ref(
            value.get("runtime_image_ref"), "sandbox runtime image reference"
        ),
        agent_id=str(value.get("agent_id") or ""),
        task_id=str(value.get("task_id") or ""),
        openclaw_session_id=str(value.get("openclaw_session_id") or ""),
    )


@dataclass(frozen=True)
class StaticRuntimeRefreshRequest:
    """An explicit, reviewed request to move a static host's runtime identity."""

    host: str
    agent_id: str
    requested_runtime_image_ref: str
    frozen_inputs_sha256: str
    source_commit: str
    review_id: str
    platforms: Tuple[str, ...] = DEFAULT_PLATFORMS

    def validate(self) -> "StaticRuntimeRefreshRequest":
        """Return self after proving every field is exactly shaped."""

        _required(self.host, "host")
        _required(self.agent_id, "agent id")
        _runtime_image_ref(
            self.requested_runtime_image_ref, "requested runtime image reference"
        )
        _sha256(self.frozen_inputs_sha256, "frozen inputs digest")
        _required(self.source_commit, "source commit")
        # Mutation of an immutable runtime pin is review-gated: without a
        # reviewed identity this is exactly the implicit mutation the typed
        # static phase forbids.
        _required(self.review_id, "review id")
        if not self.platforms:
            raise StaticRuntimeRefreshError("at least one platform is required")
        for platform in self.platforms:
            _required(platform, "platform")
        return self

    def as_json(self) -> Dict[str, Any]:
        return {
            "schema": REFRESH_REQUEST_SCHEMA,
            "host": self.host,
            "agent_id": self.agent_id,
            "requested_runtime_image_ref": self.requested_runtime_image_ref,
            "frozen_inputs_sha256": self.frozen_inputs_sha256,
            "source_commit": self.source_commit,
            "review_id": self.review_id,
            "platforms": list(self.platforms),
        }


def request_from_json(value: Any) -> StaticRuntimeRefreshRequest:
    """Build a validated request from its JSON form."""

    if not isinstance(value, Mapping):
        raise StaticRuntimeRefreshError("refresh request must be a mapping")
    if value.get("schema") != REFRESH_REQUEST_SCHEMA:
        raise StaticRuntimeRefreshError("refresh request schema is unsupported")
    raw_platforms = value.get("platforms") or list(DEFAULT_PLATFORMS)
    if not isinstance(raw_platforms, Sequence) or isinstance(raw_platforms, (str, bytes)):
        raise StaticRuntimeRefreshError("platforms must be a list")
    return StaticRuntimeRefreshRequest(
        host=str(value.get("host") or ""),
        agent_id=str(value.get("agent_id") or ""),
        requested_runtime_image_ref=str(value.get("requested_runtime_image_ref") or ""),
        frozen_inputs_sha256=str(value.get("frozen_inputs_sha256") or ""),
        source_commit=str(value.get("source_commit") or ""),
        review_id=str(value.get("review_id") or ""),
        platforms=tuple(str(item) for item in raw_platforms),
    ).validate()


class StaticRuntimeRefreshEffects(Protocol):
    """Every host interaction the refresh performs.

    ``install_runtime_image`` carries the module's one ordering obligation: it
    must raise *before* the immutable pin is written and return only *after* it
    is installed, so the receipt's ``mutated`` flag is a fact rather than a
    guess.
    """

    def installed_runtime_image_ref(self) -> str:
        """Return the runtime reference currently pinned on the host."""

    def sandbox_inventory(self) -> Sequence[SandboxRecord]:
        """Return every sandbox currently known to the host supervisor."""

    def preserve_openclaw_state(self) -> Mapping[str, Any]:
        """Checkpoint active OpenClaw sessions before any mutation."""

    def install_runtime_image(self, image_ref: str) -> Mapping[str, Any]:
        """Install the reviewed multi-architecture digest and pin it."""

    def replace_sandboxes(
        self, sandbox_ids: Sequence[str], image_ref: str
    ) -> Mapping[str, Any]:
        """Restart or replace the named sandboxes onto the requested digest."""

    def restore_openclaw_state(
        self, preservation: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Restore exactly the sessions captured by the preservation receipt."""

    def report_executor_attestation(self) -> Mapping[str, Any]:
        """Return the post-deploy report-repository executor attestation."""


@dataclass
class _Progress:
    """Mutable bookkeeping shared by the phase runners."""

    completed: List[str] = field(default_factory=list)
    mutated: bool = False
    inventory_proof: Dict[str, Any] = field(default_factory=dict)
    preservation: Dict[str, Any] = field(default_factory=dict)
    install_receipt: Dict[str, Any] = field(default_factory=dict)
    replacement: Dict[str, Any] = field(default_factory=dict)
    restoration: Dict[str, Any] = field(default_factory=dict)
    attestation: Dict[str, Any] = field(default_factory=dict)


def build_inventory_proof(
    records: Sequence[SandboxRecord],
    *,
    requested_runtime_image_ref: str,
    installed_runtime_image_ref: str,
    require_quiescent: bool,
) -> Dict[str, Any]:
    """Return the typed inventory/quiescence proof for a static host."""

    requested = _runtime_image_ref(
        requested_runtime_image_ref, "requested runtime image reference"
    )
    installed = _runtime_image_ref(
        installed_runtime_image_ref, "installed runtime image reference"
    )
    busy = sorted(record.sandbox_id for record in records if record.busy)
    stale = sorted(
        record.sandbox_id
        for record in records
        if record.runtime_image_ref != requested
    )
    proof = {
        "schema": INVENTORY_PROOF_SCHEMA,
        "installed_runtime_image_ref": installed,
        "requested_runtime_image_ref": requested,
        "sandbox_count": len(records),
        "sandboxes": [record.as_json() for record in records],
        "busy_sandbox_ids": busy,
        "stale_sandbox_ids": stale,
        "quiescent": not busy,
        "openclaw_session_ids": sorted(
            {
                record.openclaw_session_id
                for record in records
                if record.openclaw_session_id
            }
        ),
    }
    if require_quiescent and busy:
        raise StaticRuntimeRefreshError(
            "static host is not quiescent; busy sandboxes: %s" % ", ".join(busy)
        )
    return proof


def plan_static_runtime_refresh(
    request: StaticRuntimeRefreshRequest,
    *,
    installed_runtime_image_ref: str,
    inventory: Sequence[SandboxRecord],
) -> Dict[str, Any]:
    """Return the typed plan for one static-host runtime refresh.

    The plan is decided entirely from observed state: a host already pinned to
    the requested digest with no stale sandboxes needs no mutation at all and
    only has to re-prove its attestation.
    """

    request.validate()
    installed = _runtime_image_ref(
        installed_runtime_image_ref, "installed runtime image reference"
    )
    requested = request.requested_runtime_image_ref
    runtime_change_required = installed != requested
    stale = sorted(
        record.sandbox_id
        for record in inventory
        if record.runtime_image_ref != requested
    )
    mutation_required = runtime_change_required or bool(stale)
    if mutation_required:
        phases = [
            PHASE_INVENTORY,
            PHASE_PRESERVE,
            PHASE_INSTALL,
            PHASE_REPLACE,
            PHASE_RESTORE,
            PHASE_ATTEST,
        ]
        if not runtime_change_required:
            # The pin is already correct; only drifted sandboxes need work.
            phases.remove(PHASE_INSTALL)
    else:
        phases = [PHASE_INVENTORY, PHASE_ATTEST]
    return {
        "schema": REFRESH_PLAN_SCHEMA,
        "host": request.host,
        "agent_id": request.agent_id,
        "installed_runtime_image_ref": installed,
        "requested_runtime_image_ref": requested,
        "requested_runtime_digest": runtime_digest(requested),
        "frozen_inputs_sha256": request.frozen_inputs_sha256,
        "source_commit": request.source_commit,
        "review_id": request.review_id,
        "platforms": list(request.platforms),
        "runtime_change_required": runtime_change_required,
        "stale_sandbox_ids": stale,
        "mutation_required": mutation_required,
        "phases": phases,
        "mutating_phases": [phase for phase in phases if phase in MUTATING_PHASES],
    }


def validate_install_receipt(
    receipt: Any,
    *,
    requested_runtime_image_ref: str,
    platforms: Sequence[str],
) -> Dict[str, Any]:
    """Prove one install receipt really installed the reviewed multi-arch digest."""

    if not isinstance(receipt, Mapping):
        raise StaticRuntimeRefreshError("runtime install receipt must be a mapping")
    installed = _runtime_image_ref(
        receipt.get("image_ref"), "installed runtime image reference"
    )
    if installed != requested_runtime_image_ref:
        raise StaticRuntimeRefreshError(
            "runtime install receipt names a different runtime image reference"
        )
    digests = receipt.get("platform_digests")
    if not isinstance(digests, Mapping):
        raise StaticRuntimeRefreshError(
            "runtime install receipt must carry per-platform digests"
        )
    missing = [platform for platform in platforms if platform not in digests]
    if missing:
        raise StaticRuntimeRefreshError(
            "runtime install receipt is not multi-architecture; missing: %s"
            % ", ".join(sorted(missing))
        )
    for platform in platforms:
        _sha256(digests.get(platform), "%s runtime digest" % platform)
    manifest_digest = _sha256(
        receipt.get("manifest_list_digest"), "runtime manifest list digest"
    )
    if manifest_digest != runtime_digest(requested_runtime_image_ref):
        raise StaticRuntimeRefreshError(
            "runtime install receipt manifest digest differs from the requested digest"
        )
    return {
        "image_ref": installed,
        "manifest_list_digest": manifest_digest,
        "platform_digests": {
            platform: str(digests[platform]) for platform in sorted(platforms)
        },
    }


def attestation_matches_requested_runtime(
    attestation: Any, requested_runtime_image_ref: str
) -> bool:
    """Whether a post-deploy attestation is valid *and* names the requested runtime."""

    if not valid_read_only_report_repository_executor_attestation(attestation):
        return False
    return attestation.get("runtime_image_ref") == requested_runtime_image_ref


def _preservation_receipt(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StaticRuntimeRefreshError("OpenClaw preservation receipt must be a mapping")
    sessions = value.get("session_ids")
    if not isinstance(sessions, Sequence) or isinstance(sessions, (str, bytes)):
        raise StaticRuntimeRefreshError(
            "OpenClaw preservation receipt must list preserved session ids"
        )
    checkpoint = _sha256(
        value.get("checkpoint_sha256"), "OpenClaw checkpoint digest"
    )
    return {
        "session_ids": sorted(str(item) for item in sessions),
        "checkpoint_sha256": checkpoint,
        "preserved": True,
    }


def _restoration_receipt(value: Any, *, expected_sessions: Sequence[str]) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StaticRuntimeRefreshError("OpenClaw restoration receipt must be a mapping")
    restored = value.get("restored_session_ids")
    if not isinstance(restored, Sequence) or isinstance(restored, (str, bytes)):
        raise StaticRuntimeRefreshError(
            "OpenClaw restoration receipt must list restored session ids"
        )
    observed = sorted(str(item) for item in restored)
    if observed != sorted(expected_sessions):
        raise StaticRuntimeRefreshError(
            "OpenClaw restoration did not return every preserved session"
        )
    return {"restored_session_ids": observed, "restored": True}


def _replacement_receipt(
    value: Any, *, expected_sandbox_ids: Sequence[str]
) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StaticRuntimeRefreshError("sandbox replacement receipt must be a mapping")
    replaced = value.get("replaced_sandbox_ids")
    if not isinstance(replaced, Sequence) or isinstance(replaced, (str, bytes)):
        raise StaticRuntimeRefreshError(
            "sandbox replacement receipt must list replaced sandbox ids"
        )
    failed = value.get("failed_sandbox_ids") or []
    if not isinstance(failed, Sequence) or isinstance(failed, (str, bytes)):
        raise StaticRuntimeRefreshError(
            "sandbox replacement receipt must list failed sandbox ids"
        )
    if failed:
        raise StaticRuntimeRefreshError(
            "sandbox replacement failed for: %s"
            % ", ".join(sorted(str(item) for item in failed))
        )
    observed = sorted(str(item) for item in replaced)
    if observed != sorted(expected_sandbox_ids):
        raise StaticRuntimeRefreshError(
            "sandbox replacement receipt does not cover every stale sandbox"
        )
    return {"replaced_sandbox_ids": observed, "failed_sandbox_ids": []}


def _probe(callable_: Any, *args: Any) -> Dict[str, Any]:
    """Run one recovery probe, converting any failure into typed evidence."""

    try:
        return {"ok": True, "value": callable_(*args)}
    except Exception as exc:  # noqa: BLE001 - recovery must never raise
        return {"ok": False, "error": str(exc)}


def build_fix_forward_recovery(
    request: StaticRuntimeRefreshRequest,
    effects: StaticRuntimeRefreshEffects,
    *,
    failed_phase: str,
    error: str,
    progress: _Progress,
) -> Dict[str, Any]:
    """Return typed fix-forward recovery evidence for a post-mutation failure.

    Rollback is deliberately absent: the reviewed digest has already replaced
    the superseded one, and re-pinning the old identity would restore exactly
    the drift this refresh exists to remove.
    """

    requested = request.requested_runtime_image_ref
    pin = _probe(effects.installed_runtime_image_ref)
    inventory = _probe(effects.sandbox_inventory)
    residual: List[str] = []
    observed_sandboxes: List[Dict[str, Any]] = []
    if inventory["ok"]:
        records = list(inventory["value"] or [])
        observed_sandboxes = [record.as_json() for record in records]
        residual = sorted(
            record.sandbox_id
            for record in records
            if record.runtime_image_ref != requested
        )
    restore: Dict[str, Any] = {"ok": True, "value": None, "skipped": True}
    if progress.preservation:
        restore = _probe(effects.restore_openclaw_state, dict(progress.preservation))
        restore["skipped"] = False
    attestation = _probe(effects.report_executor_attestation)
    observed_attestation = (
        dict(attestation["value"])
        if attestation["ok"] and isinstance(attestation["value"], Mapping)
        else {}
    )
    converged = (
        pin["ok"]
        and str(pin["value"]) == requested
        and not residual
        and attestation_matches_requested_runtime(observed_attestation, requested)
    )
    return {
        "schema": RECOVERY_SCHEMA,
        "strategy": "fix_forward",
        "rollback_performed": False,
        "rollback_forbidden_reason": (
            "re-pinning the superseded runtime digest would reintroduce the "
            "identity drift this refresh was reviewed to remove"
        ),
        "failed_phase": failed_phase,
        "error": error,
        "requested_runtime_image_ref": requested,
        "observed_runtime_image_ref": str(pin["value"]) if pin["ok"] else "",
        "runtime_pin_readable": bool(pin["ok"]),
        "sandbox_inventory_readable": bool(inventory["ok"]),
        "observed_sandboxes": observed_sandboxes,
        "residual_stale_sandbox_ids": residual,
        "openclaw_state_preserved": bool(progress.preservation),
        "openclaw_restore_attempted": not restore.get("skipped", False),
        "openclaw_restore_ok": bool(restore["ok"]),
        "observed_attestation_runtime_image_ref": str(
            observed_attestation.get("runtime_image_ref") or ""
        ),
        "attestation_matches_request": attestation_matches_requested_runtime(
            observed_attestation, requested
        ),
        "converged": converged,
        "next_action": (
            "verify_report_executor_attestation"
            if converged
            else "retry_static_runtime_refresh"
        ),
    }


def _receipt(
    request: StaticRuntimeRefreshRequest,
    plan: Mapping[str, Any],
    progress: _Progress,
    *,
    outcome: str,
    failed_phase: str = "",
    error: str = "",
    recovery: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    receipt: Dict[str, Any] = {
        "schema": REFRESH_RECEIPT_SCHEMA,
        "host": request.host,
        "agent_id": request.agent_id,
        "requested_runtime_image_ref": request.requested_runtime_image_ref,
        "requested_runtime_digest": runtime_digest(request.requested_runtime_image_ref),
        "installed_runtime_image_ref": plan.get("installed_runtime_image_ref", ""),
        "frozen_inputs_sha256": request.frozen_inputs_sha256,
        "source_commit": request.source_commit,
        "review_id": request.review_id,
        "plan": dict(plan),
        "outcome": outcome,
        "ok": outcome in (OUTCOME_UNCHANGED, OUTCOME_UPGRADED),
        "mutated": progress.mutated,
        "phases_completed": list(progress.completed),
        "failed_phase": failed_phase,
        "error": error,
        "inventory_proof": dict(progress.inventory_proof),
        "openclaw_preservation": dict(progress.preservation),
        "install_receipt": dict(progress.install_receipt),
        "sandbox_replacement": dict(progress.replacement),
        "openclaw_restoration": dict(progress.restoration),
        "attestation": dict(progress.attestation),
        "attestation_matches_request": attestation_matches_requested_runtime(
            progress.attestation, request.requested_runtime_image_ref
        ),
    }
    if recovery is not None:
        receipt["recovery"] = dict(recovery)
    return receipt


def execute_static_runtime_refresh(
    request: StaticRuntimeRefreshRequest,
    effects: StaticRuntimeRefreshEffects,
) -> Dict[str, Any]:
    """Run one explicit static-host runtime refresh and return its typed receipt.

    Never raises for an operational failure: a failure is reported as a receipt
    whose ``outcome`` states which side of the mutation boundary it fell on,
    with fix-forward recovery evidence attached when the host was already
    mutated. Only a malformed *request* raises, because that is a caller bug
    rather than a host condition.
    """

    request.validate()
    progress = _Progress()
    plan: Dict[str, Any] = {}
    phase = PHASE_INVENTORY
    try:
        installed = _runtime_image_ref(
            effects.installed_runtime_image_ref(), "installed runtime image reference"
        )
        inventory = list(effects.sandbox_inventory())
        plan = plan_static_runtime_refresh(
            request, installed_runtime_image_ref=installed, inventory=inventory
        )
        progress.inventory_proof = build_inventory_proof(
            inventory,
            requested_runtime_image_ref=request.requested_runtime_image_ref,
            installed_runtime_image_ref=installed,
            # Quiescence only gates mutation. A host that needs no change must
            # not be blocked from re-proving its attestation while it works.
            require_quiescent=bool(plan["mutation_required"]),
        )
        progress.completed.append(PHASE_INVENTORY)

        if plan["mutation_required"]:
            phase = PHASE_PRESERVE
            progress.preservation = _preservation_receipt(
                effects.preserve_openclaw_state()
            )
            progress.completed.append(PHASE_PRESERVE)

            if plan["runtime_change_required"]:
                phase = PHASE_INSTALL
                raw_install = effects.install_runtime_image(
                    request.requested_runtime_image_ref
                )
                # The pin is now installed: everything after this point is a
                # post-mutation failure even if validation rejects the receipt.
                progress.mutated = True
                progress.install_receipt = validate_install_receipt(
                    raw_install,
                    requested_runtime_image_ref=request.requested_runtime_image_ref,
                    platforms=request.platforms,
                )
                progress.completed.append(PHASE_INSTALL)

            phase = PHASE_REPLACE
            stale = list(plan["stale_sandbox_ids"])
            progress.mutated = True
            progress.replacement = _replacement_receipt(
                effects.replace_sandboxes(stale, request.requested_runtime_image_ref),
                expected_sandbox_ids=stale,
            )
            post_inventory = list(effects.sandbox_inventory())
            post_proof = build_inventory_proof(
                post_inventory,
                requested_runtime_image_ref=request.requested_runtime_image_ref,
                installed_runtime_image_ref=request.requested_runtime_image_ref,
                require_quiescent=False,
            )
            if post_proof["stale_sandbox_ids"]:
                raise StaticRuntimeRefreshError(
                    "sandboxes remain on the superseded runtime: %s"
                    % ", ".join(post_proof["stale_sandbox_ids"])
                )
            progress.replacement["post_inventory_proof"] = post_proof
            progress.completed.append(PHASE_REPLACE)

            phase = PHASE_RESTORE
            progress.restoration = _restoration_receipt(
                effects.restore_openclaw_state(dict(progress.preservation)),
                expected_sessions=progress.preservation["session_ids"],
            )
            progress.completed.append(PHASE_RESTORE)

        phase = PHASE_ATTEST
        attestation = effects.report_executor_attestation()
        progress.attestation = (
            dict(attestation) if isinstance(attestation, Mapping) else {}
        )
        if not attestation_matches_requested_runtime(
            progress.attestation, request.requested_runtime_image_ref
        ):
            raise StaticRuntimeRefreshError(
                "post-deploy report-executor attestation does not equal the "
                "requested runtime digest"
            )
        progress.completed.append(PHASE_ATTEST)
    except Exception as exc:  # noqa: BLE001 - every host failure becomes evidence
        if not plan:
            plan = {
                "schema": REFRESH_PLAN_SCHEMA,
                "host": request.host,
                "agent_id": request.agent_id,
                "installed_runtime_image_ref": "",
                "requested_runtime_image_ref": request.requested_runtime_image_ref,
                "phases": [],
                "mutating_phases": [],
                "mutation_required": False,
                "runtime_change_required": False,
                "stale_sandbox_ids": [],
            }
        if not progress.mutated:
            return _receipt(
                request,
                plan,
                progress,
                outcome=OUTCOME_FAILED_BEFORE_MUTATION,
                failed_phase=phase,
                error=str(exc),
            )
        recovery = build_fix_forward_recovery(
            request,
            effects,
            failed_phase=phase,
            error=str(exc),
            progress=progress,
        )
        return _receipt(
            request,
            plan,
            progress,
            outcome=OUTCOME_FAILED_AFTER_MUTATION,
            failed_phase=phase,
            error=str(exc),
            recovery=recovery,
        )
    return _receipt(
        request,
        plan,
        progress,
        outcome=OUTCOME_UPGRADED if progress.mutated else OUTCOME_UNCHANGED,
    )


def _load_json(path: str, label: str) -> Any:
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StaticRuntimeRefreshError("%s is unreadable" % label) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Static-host OpenShell runtime refresh")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="emit the typed refresh plan")
    plan.add_argument("--request", required=True)
    plan.add_argument("--installed-ref", required=True)
    plan.add_argument("--inventory", required=True)
    verify = sub.add_parser(
        "verify-attestation", help="prove an attestation equals the requested runtime"
    )
    verify.add_argument("--attestation", required=True)
    verify.add_argument("--requested-ref", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            request = request_from_json(_load_json(args.request, "refresh request"))
            raw_inventory = _load_json(args.inventory, "sandbox inventory")
            if not isinstance(raw_inventory, list):
                raise StaticRuntimeRefreshError("sandbox inventory must be a list")
            plan = plan_static_runtime_refresh(
                request,
                installed_runtime_image_ref=args.installed_ref,
                inventory=[sandbox_record_from_json(item) for item in raw_inventory],
            )
            print(json.dumps(plan, sort_keys=True))
            return 0
        attestation = _load_json(args.attestation, "executor attestation")
        requested = _runtime_image_ref(
            args.requested_ref, "requested runtime image reference"
        )
        matches = attestation_matches_requested_runtime(attestation, requested)
        print(
            json.dumps(
                {
                    "requested_runtime_image_ref": requested,
                    "attestation_matches_request": matches,
                },
                sort_keys=True,
            )
        )
        return 0 if matches else 1
    except StaticRuntimeRefreshError as exc:
        print("static runtime refresh error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
