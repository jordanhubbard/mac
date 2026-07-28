"""MAC fleet worker runtime.

Implements the long-running worker process that leases and executes tasks,
managing git remotes, deployment barriers and heartbeats, subprocess execution,
and evidence handoff back to the control plane.
"""

from __future__ import annotations

import argparse
import base64
import copy
import fcntl
import hashlib
import json
import logging
import os
import pty
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import subprocess
import struct
import sys
import termios
import tempfile
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mac import mac_paths
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import quote, urlencode

from mac.agentbus_control import (
    DEBUG_TERMINAL_INPUT_SCHEMA,
    DEBUG_TERMINAL_OPEN_CONTENT_TYPE,
    DEBUG_TERMINAL_OPEN_SCHEMA,
    DEBUG_TERMINAL_OPEN_TOPIC,
    DEBUG_TERMINAL_OUTPUT_CONTENT_TYPE,
    HERMES_CONFIG_APPLY_CONTENT_TYPE,
    HERMES_CONFIG_APPLY_RESULT_CONTENT_TYPE,
    HERMES_CONFIG_APPLY_RESULT_SCHEMA,
    HERMES_CONFIG_APPLY_RESULT_TOPIC,
    HERMES_CONFIG_APPLY_SCHEMA,
    HERMES_CONFIG_APPLY_TOPIC,
    PEER_MESSAGE_CONTENT_TYPE,
    PEER_MESSAGE_TOPIC,
    REFLECT_REQUEST_CONTENT_TYPE,
    REFLECT_REQUEST_TOPIC,
    REFLECT_RESULT_CONTENT_TYPE,
    REFLECT_RESULT_TOPIC,
    REPO_UPDATE_CONTENT_TYPE,
    REPO_UPDATE_RESULT_CONTENT_TYPE,
    REPO_UPDATE_RESULT_SCHEMA,
    REPO_UPDATE_RESULT_TOPIC,
    REPO_UPDATE_SCHEMA,
    REPO_UPDATE_TOPIC,
    debug_terminal_output_payload,
    reflect_result_payload,
)
from mac.env_config import resolve_hub_agent
from mac.hub_load_shed import (
    BreakerState,
    HubLoadShedConfig,
    LoadShedBreaker,
    default_control_plane_sampler,
    is_hub_host,
)
from mac.codegraph_audit import (
    codegraph_audit_check,
    codegraph_audit_manifest_problems,
    run_codegraph_audit,
)
from mac.fleet_learning import (
    RepositoryAccessError,
    build_repository_access_learning,
    build_repository_access_memory_payload,
    classify_repository_access_failure,
    resolve_git_remote_access,
)
from mac.api_client import MacApiClient, MacApiError
from mac.repository_contract import (
    normalize_repo_relative_path as _normalize_repo_relative_path,
    remote_branch_from_ref as _remote_branch_from_ref,
    repo_path_satisfies_requirement as _repo_path_satisfies_requirement,
)
from mac.repository_access_env import read_only_repository_content_digest
from mac.trusted_artifact import (
    nofollow_regular_file_identity,
    nofollow_source_bundle_digest,
)
from mac.models import (
    REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY,
    REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY,
    REPORT_REPOSITORY_ACCESS_SCHEMA,
    REPORT_REPOSITORY_READ_ONLY_MODE,
    agent_has_read_only_report_repository_executor,
    read_only_report_repository_executor_attestation,
    declared_non_repository_outcome_evidence_type,
    metadata_declares_read_only_report_repository,
    metadata_declares_report_deliverable,
    utcnow,
)
from mac.hermes_config_surface import apply_hermes_surface_payload
from mac.gitops import (
    guarded_push,
    resolve_canonical_publication_target,
    strip_git_remote_auth,
    sync_worktree_with_canonical,
    validate_git_ref,
    validate_git_remote_url,
)
from mac.environment_contract import (
    derive_environment_contract,
    validate_environment_contract,
)


JsonDict = Dict[str, Any]

# ---------------------------------------------------------------------------
# Submodule imports – code extracted into focused modules for testability
# ---------------------------------------------------------------------------
from mac.worker_subprocess import (
    SubprocessExecutor,
    _terminate_process_tree,
)
from mac.worker_debug_terminal import (
    DebugTerminalMixin,
    DebugTerminalSession,
)
from mac.worker_reflect import ReflectMixin
from mac.worker_directable import DirectableMixin
from mac.worker_workspace_gc import WorkspaceGCMixin
from mac.agentbus_service import HUMAN_DIRECTIVE_TOPIC
from mac.worker_repo_prep import RepoPrepMixin
import mac.harness_recovery_reflex as _hrr
from mac.worker_runtime_deps import (
    REQUIRED_RUNTIME_PIP,
    RuntimeDepsMixin,
)

logger = logging.getLogger("mac.worker")
Executor = Callable[[JsonDict, Path], "WorkerExecution"]
CommandAuditSink = Callable[[JsonDict], None]
StatusUpdateSink = Callable[[JsonDict], JsonDict]
SAFE_GIT_REF_RE = r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,127}$"
SAFE_SYSTEMD_SERVICE_RE = r"^[A-Za-z0-9][A-Za-z0-9_.@:\-]{0,126}\.service$"
VERIFICATION_SCHEMA = "mac.worker_evidence.v1"
# EX_TEMPFAIL from sysexits.h. main() returns this so the supervising
# service manager (systemd/launchd wrapper) restarts the worker after a
# self-update swaps the code on disk, instead of treating the exit as a
# hard failure. Mirrors _hermes.gateway.restart.GATEWAY_SERVICE_RESTART_EXIT_CODE.
SELF_UPDATE_RESTART_EXIT_CODE = 75
GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
DEFAULT_COMMAND_INVENTORY_NAMES = (
    "bash",
    "cargo",
    "codegraph",
    "git",
    "gh",
    "make",
    "node",
    "npm",
    "pip",
    "python",
    "python3",
    "pytest",
    "rustc",
    "rustup",
    "sh",
    "uv",
)
DEFAULT_COMMAND_INVENTORY_MAX = 10000
DEFAULT_COMMAND_INVENTORY_INTERVAL_SECONDS = 300.0


def _validate_git_remote_url(value: str) -> str:
    return validate_git_remote_url(value)


def _validate_git_ref(value: str) -> str:
    return validate_git_ref(value)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _deployment_barrier_state() -> tuple[str, bool]:
    """Return the configured rollout generation and whether its barrier is live."""

    generation = (os.environ.get("MAC_WORKER_DEPLOY_GENERATION") or "").strip()
    barrier = os.environ.get("MAC_WORKER_DEPLOY_BARRIER_FILE") or ""
    if not generation or not barrier:
        return generation, False
    try:
        barrier_generation = Path(barrier).read_text(encoding="utf-8").strip()
    except OSError:
        barrier_generation = ""
    return generation, barrier_generation == generation


def _deployment_heartbeat_payload(
    status: str,
    *,
    resources: Optional[Mapping[str, Any]] = None,
    report_health: bool = False,
) -> JsonDict:
    """Fence every worker-originated status heartbeat behind the local barrier."""

    generation, barrier_active = _deployment_barrier_state()
    payload: JsonDict = {
        "status": "draining" if barrier_active else status,
    }
    if report_health or barrier_active:
        payload["health_status"] = "degraded" if barrier_active else "healthy"
    if resources is not None:
        stamped_resources = dict(resources)
        if generation:
            stamped_resources["deployment_generation"] = generation
        payload["resources"] = stamped_resources
    return payload


def _synchronize_directive_policy(
    client: MacApiClient,
    agent_id: str,
) -> JsonDict:
    """Fetch and acknowledge every pending policy epoch before claiming work."""

    path = "/agents/%s/directives/effective" % quote(agent_id, safe="")

    def fetch() -> Any:
        get = getattr(client, "get", None)
        if callable(get):
            return get(path)
        return client.request("GET", path, None)

    try:
        snapshot = fetch()
    except MacApiError as exc:
        # Rolling compatibility with a pre-directive hub is intentionally the
        # only soft failure. Current-hub transport/evaluation failures stay
        # fail-closed and prevent the worker from reaching claim-next.
        if "not found" in str(exc).lower():
            return {"schema": "mac.directive.snapshot.v1", "enabled": False}
        raise
    if not isinstance(snapshot, dict):
        raise MacApiError("hub returned an invalid directive policy snapshot")
    pending = snapshot.get("pending_activations") or []
    if not isinstance(pending, list):
        raise MacApiError("hub returned invalid pending directive activations")
    for activation in pending:
        if not isinstance(activation, dict):
            raise MacApiError("hub returned an invalid directive activation")
        activation_id = str(activation.get("activation_id") or "").strip()
        digest = str(activation.get("digest") or "").strip()
        document = activation.get("document")
        if not activation_id or not digest or not isinstance(document, dict):
            raise MacApiError("hub returned an incomplete directive activation")
        observed_digest = hashlib.sha256(
            json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        if observed_digest != digest:
            raise MacApiError("directive activation document digest mismatch")
        client.post(
            "/agents/%s/directive-activations/%s/ack"
            % (quote(agent_id, safe=""), quote(activation_id, safe="")),
            {"digest": digest},
        )
    if pending:
        confirmed = fetch()
        if not isinstance(confirmed, dict) or confirmed.get("pending_activations"):
            raise MacApiError("directive acknowledgement did not clear pending policy")
        snapshot = confirmed
    return snapshot


REQUIRED_CHANGED_FILE_KEYS = (
    "required_changed_files",
    "required_files",
    "required_repo_files",
)


@dataclass
class WorkerExecution:
    returncode: int
    summary: str
    stdout: str = ""
    stderr: str = ""
    metadata: JsonDict = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


@dataclass
class WorkerRunResult:
    status: str
    task: Optional[JsonDict] = None
    lease: Optional[JsonDict] = None
    evidence: Optional[JsonDict] = None
    error: Optional[str] = None

    def to_dict(self) -> JsonDict:
        return asdict(self)

def _build_willing_media_routes(host: str, hardware: Optional[JsonDict]) -> List[JsonDict]:
    """All media routes this host is configured (willing) to serve: catalog-
    derived + GPU-gated (advertised_media_routes returns [] off-GPU). One server
    per modality — image :8189, audio :8190, video :8191. Shared by registration
    and the service-claim sync (advertise-on-hold)."""
    try:
        from mac.local_gen_catalog import advertised_media_routes
    except Exception:  # noqa: BLE001
        return []
    gen_host = (os.environ.get("MAC_AGENT_GEN_HOST") or host).strip()

    def _base(base_env: str, port_env: str, default_port: str) -> str:
        explicit = (os.environ.get(base_env) or "").strip()
        if explicit:
            return explicit
        port = (os.environ.get(port_env) or default_port).strip()
        return "http://%s:%s/v1" % (gen_host, port)

    routes: List[JsonDict] = []
    img = (os.environ.get("MAC_AGENT_GEN_MODEL") or "").strip()
    if img:
        routes.extend(advertised_media_routes(
            img, _base("MAC_AGENT_GEN_BASE_URL", "MAC_AGENT_GEN_PORT", "8189"), hardware))
    for models_env, base_env, port_env, default_port in (
        ("MAC_AGENT_GEN_AUDIO_MODELS", "MAC_AGENT_GEN_AUDIO_BASE_URL", "MAC_AGENT_GEN_AUDIO_PORT", "8190"),
        ("MAC_AGENT_GEN_VIDEO_MODELS", "MAC_AGENT_GEN_VIDEO_BASE_URL", "MAC_AGENT_GEN_VIDEO_PORT", "8191"),
    ):
        base = _base(base_env, port_env, default_port)
        for model_id in (os.environ.get(models_env) or "").split(","):
            model_id = model_id.strip()
            if model_id:
                routes.extend(advertised_media_routes(model_id, base, hardware))
    return routes


def _detect_command_inventory() -> JsonDict:
    """Report executable command names visible in this worker process.

    Repository contracts use this as toolchain inventory. It deliberately lives
    in resources, not dispatch capabilities: commands are environmental facts,
    while capabilities describe the work an agent is allowed to perform.
    """
    available: set[str] = set()
    paths: Dict[str, str] = {}
    max_entries = _env_int(
        "MAC_WORKER_COMMAND_INVENTORY_MAX",
        DEFAULT_COMMAND_INVENTORY_MAX,
    )
    max_entries = max(1, max_entries)
    truncated = False

    for directory in os.get_exec_path():
        if not directory:
            continue
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for entry in entries:
            if not entry or entry in available:
                continue
            candidate = os.path.join(directory, entry)
            if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
                continue
            available.add(entry)
            if len(available) >= max_entries:
                truncated = True
                break
        if truncated:
            break

    extra = os.environ.get("MAC_WORKER_COMMAND_PROBES") or ""
    explicit_names = list(DEFAULT_COMMAND_INVENTORY_NAMES)
    explicit_names.extend(item.strip() for item in extra.split(",") if item.strip())
    for name in explicit_names:
        path = shutil.which(name)
        if path:
            available.add(name)
            paths[name] = path

    # Secondary file-based probe for well-known Rust tool locations.
    # launchd and other non-login shells may exclude ~/.cargo/bin from PATH;
    # this mirrors the codegraph detection pattern in services.py so Rust
    # tools installed via rustup are always visible in the command inventory.
    _RUST_TOOL_CANDIDATES: Dict[str, List[Path]] = {
        tool: [
            Path.home() / ".cargo" / "bin" / tool,
            Path("/usr/local/bin") / tool,
            Path("/opt/homebrew/bin") / tool,
        ]
        for tool in ("cargo", "rustc", "rustup")
    }
    for tool, candidates in _RUST_TOOL_CANDIDATES.items():
        if tool in paths:
            continue
        for candidate in candidates:
            try:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    available.add(tool)
                    paths[tool] = str(candidate)
                    break
            except OSError:
                continue

    return {
        "schema": "mac.command_inventory.v1",
        "source": "worker_path",
        "available": sorted(available),
        "paths": {name: paths[name] for name in sorted(paths)},
        "truncated": truncated,
        "refreshed_at": _utcnow(),
    }


def _resources_with_command_inventory(
    resources: Optional[JsonDict],
    coding_verification: Optional[JsonDict] = None,
    source_repo: Optional[Path] = None,
    agent_id: Optional[str] = None,
) -> JsonDict:
    merged = ensure_json_object(resources)
    merged["commands"] = _detect_command_inventory()
    if source_repo is not None:
        merged["source_state"] = _worker_source_state(source_repo)
    if agent_id:
        from mac.worker_credentials import credential_resource_from_env

        proof = credential_resource_from_env(agent_id)
        if proof:
            merged["worker_credential"] = proof
    # Coding-CLI auth status (secret-free) rides the same refresh cycle so the
    # hub — and `mac fleet creds status` on any workstation — can see which
    # agents have lost or never had claude/codex/cursor credentials and need a
    # sync from the operator's current environment.
    try:
        from mac.coding_agent import detect_all as _detect_coding_clis

        verification_by_agent: JsonDict = {}
        if isinstance(coding_verification, dict):
            reports = coding_verification.get("reports")
            if isinstance(reports, dict):
                for report in reports.values():
                    if not isinstance(report, dict):
                        continue
                    checked_agent = str(report.get("agent") or "")
                    if checked_agent:
                        verification_by_agent[checked_agent] = report
            else:
                # Backward compatibility with the original single-route report.
                checked_agent = str(coding_verification.get("agent") or "")
                if checked_agent:
                    verification_by_agent[checked_agent] = coding_verification
        merged["coding_clis"] = {
            "schema": "mac.coding_clis.v2",
            "refreshed_at": _utcnow(),
            "clis": _detect_coding_clis(verification=verification_by_agent),
        }
    except Exception:  # noqa: BLE001 - status is best-effort, never blocks registration
        pass
    # The hub owns resources["openshell_required"].  A deploy may seed it on
    # first registration through an explicit environment value, but an absent
    # local setting must not manufacture False and overwrite reconciled policy.
    explicit_openshell = os.environ.get("MAC_OPENSHELL_REQUIRED")
    if explicit_openshell is not None:
        try:
            from mac.openshell_runtime import openshell_required_for_identity

            merged.setdefault(
                "openshell_required",
                openshell_required_for_identity(explicit=explicit_openshell),
            )
        except Exception:  # noqa: BLE001 - resource stamping must never break registration
            pass
    return merged


def _read_only_report_executor_attestation(
    executor_argv: Optional[List[str]],
) -> Optional[JsonDict]:
    """Describe the hardened report executor only when it is usable *now*.

    The hub converts this worker-side claim into a separate controller-owned
    marker.  Keep the probe fail-closed: the legacy executor alias, ACP,
    supervisor-only confinement, retained sandboxes, unsafe create arguments,
    missing policy/binary, and unenforceable Landlock posture all remain
    ineligible for repository-bearing reports.
    """

    argv = list(executor_argv or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        return None
    if len(argv) != 1 or Path(argv[0]).name != "mac-task-executor":
        return None
    if not _env_truthy(os.environ.get("MAC_OPENSHELL_SANDBOX")):
        return None
    if (os.environ.get("MAC_EXECUTOR_BACKEND") or "hermes").strip().lower() != "hermes":
        return None
    if _env_truthy(os.environ.get("MAC_OPENSHELL_KEEP")):
        return None
    passthrough = {
        item.strip()
        for item in (os.environ.get("MAC_OPENSHELL_ENV_PASSTHROUGH") or "").split(",")
        if item.strip()
    }
    if "PATH" in passthrough:
        return None
    openshell_bin = (os.environ.get("MAC_OPENSHELL_BIN") or "openshell").strip()
    resolved_openshell_bin = shutil.which(openshell_bin) if openshell_bin else None
    if resolved_openshell_bin is None:
        return None
    try:
        from mac.executor_sandbox import (
            _kernel_has_landlock,
            _managed_openshell_runtime_image_ref,
            _read_only_report_environment_passthrough_valid,
            _read_only_report_extra_create_argv,
            _resolve_openshell_policy,
        )

        executor_path, executor_sha256 = nofollow_regular_file_identity(argv[0])
        openshell_bin_path, openshell_bin_sha256 = (
            nofollow_regular_file_identity(resolved_openshell_bin)
        )
        _policy_path, policy_sha256 = nofollow_regular_file_identity(
            _resolve_openshell_policy()
        )
        runtime_image_ref = _managed_openshell_runtime_image_ref()
        # Registration precedes controller approval. Validate the complete
        # local create contract without requiring the tuple this attestation
        # is asking the hub to approve.
        _read_only_report_extra_create_argv(require_approval=False)
        if not _read_only_report_environment_passthrough_valid():
            return None
        python_candidate = (
            os.environ.get("MAC_TASK_EXECUTOR_PYTHON") or sys.executable
        ).strip()
        python_path, python_sha256 = nofollow_regular_file_identity(
            Path(python_candidate).expanduser().resolve(strict=True)
        )
        script_candidate = (
            os.environ.get("MAC_TASK_EXECUTOR_SCRIPT")
            or str(Path(executor_path).with_name("mac-task-executor.py"))
        ).strip()
        executor_script_path, executor_script_sha256 = (
            nofollow_regular_file_identity(script_candidate)
        )
        source_candidate = (
            os.environ.get("MAC_SELF_UPDATE_REPO")
            or str(_default_self_update_repo())
        ).strip()
        source_root, source_bundle_sha256 = nofollow_source_bundle_digest(
            source_candidate
        )
        if sys.platform.startswith("linux") and _kernel_has_landlock():
            platform = "linux"
            isolation_posture = "landlock_enforced"
        elif sys.platform == "darwin" and _env_truthy(
            os.environ.get("MAC_OPENSHELL_ALLOW_NO_LANDLOCK")
        ):
            platform = "darwin"
            isolation_posture = "macos_docker_vm_seccomp_egress"
        else:
            return None
    except Exception:  # noqa: BLE001 - absence means no dispatch attestation
        return None
    return read_only_report_repository_executor_attestation(
        runtime_image_ref=runtime_image_ref,
        policy_sha256=policy_sha256,
        openshell_bin_path=openshell_bin_path,
        openshell_bin_sha256=openshell_bin_sha256,
        executor_path=executor_path,
        executor_sha256=executor_sha256,
        platform=platform,
        isolation_posture=isolation_posture,
        python_path=python_path,
        python_sha256=python_sha256,
        executor_script_path=executor_script_path,
        executor_script_sha256=executor_script_sha256,
        source_root=source_root,
        source_bundle_sha256=source_bundle_sha256,
    )


_REPORT_EXECUTOR_APPROVAL_ENV = {
    "runtime_image_ref": "MAC_REPORT_EXECUTOR_APPROVED_RUNTIME_IMAGE_REF",
    "policy_sha256": "MAC_REPORT_EXECUTOR_APPROVED_POLICY_SHA256",
    "openshell_bin_path": "MAC_REPORT_EXECUTOR_APPROVED_OPENSHELL_BIN_PATH",
    "openshell_bin_sha256": "MAC_REPORT_EXECUTOR_APPROVED_OPENSHELL_BIN_SHA256",
    "executor_path": "MAC_REPORT_EXECUTOR_APPROVED_HOST_EXECUTOR_PATH",
    "executor_sha256": "MAC_REPORT_EXECUTOR_APPROVED_HOST_EXECUTOR_SHA256",
    "platform": "MAC_REPORT_EXECUTOR_APPROVED_PLATFORM",
    "isolation_posture": "MAC_REPORT_EXECUTOR_APPROVED_ISOLATION_POSTURE",
    "python_path": "MAC_REPORT_EXECUTOR_APPROVED_PYTHON_PATH",
    "python_sha256": "MAC_REPORT_EXECUTOR_APPROVED_PYTHON_SHA256",
    "executor_script_path": "MAC_REPORT_EXECUTOR_APPROVED_EXECUTOR_SCRIPT_PATH",
    "executor_script_sha256": "MAC_REPORT_EXECUTOR_APPROVED_EXECUTOR_SCRIPT_SHA256",
    "source_root": "MAC_REPORT_EXECUTOR_APPROVED_SOURCE_ROOT",
    "source_bundle_sha256": "MAC_REPORT_EXECUTOR_APPROVED_SOURCE_BUNDLE_SHA256",
}
_REPORT_EXECUTOR_RUNTIME_PATH_ENV = {
    "python_path": "MAC_TASK_EXECUTOR_PYTHON",
    "executor_script_path": "MAC_TASK_EXECUTOR_SCRIPT",
    "source_root": "MAC_SELF_UPDATE_REPO",
}


def _apply_read_only_report_executor_approval(
    resources: Any, environ: Dict[str, str]
) -> bool:
    for name in _REPORT_EXECUTOR_APPROVAL_ENV.values():
        environ.pop(name, None)
    if not agent_has_read_only_report_repository_executor(resources):
        return False
    marker = resources[REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY]
    for key, name in _REPORT_EXECUTOR_APPROVAL_ENV.items():
        environ[name] = str(marker[key])
    # The generated fleet wrapper historically embedded these paths instead of
    # exporting them. Bind the actual spawned runtime to the exact same paths
    # the hub approved, including on a deployment-realistic environment where
    # none of the three variables existed before registration.
    for key, name in _REPORT_EXECUTOR_RUNTIME_PATH_ENV.items():
        environ[name] = str(marker[key])
    return True


def _worker_source_state(repo: Path) -> JsonDict:
    """Return secret-free source attestation used by the hub reconciler."""
    state: JsonDict = {
        "schema": "mac.worker_source_state.v1",
        "repo_path": str(repo.expanduser()),
        "repository_name": repo.expanduser().name,
        "commit_sha": "",
        "tree_sha": "",
        "dirty": False,
        "observed_at": _utcnow(),
    }
    repo = repo.expanduser()
    if not repo.is_dir():
        state["error"] = "repository_missing"
        return state
    head = _run_git(repo, ["rev-parse", "HEAD"])
    tree = _run_git(repo, ["rev-parse", "HEAD^{tree}"])
    dirty = _run_git(repo, ["status", "--porcelain"])
    if head.returncode != 0:
        state["error"] = "not_a_git_worktree"
        return state
    state["commit_sha"] = head.stdout.strip()
    state["tree_sha"] = tree.stdout.strip() if tree.returncode == 0 else ""
    state["dirty"] = dirty.returncode != 0 or bool(dirty.stdout.strip())
    return state


def _active_worker_deployment_generation() -> Optional[str]:
    """Return the exact generation only while its local start barrier exists."""
    generation = (os.environ.get("MAC_WORKER_DEPLOY_GENERATION") or "").strip()
    barrier = (os.environ.get("MAC_WORKER_DEPLOY_BARRIER_FILE") or "").strip()
    if not generation or not barrier:
        return None
    try:
        observed = Path(barrier).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return generation if observed == generation else None


def register_worker(
    client: MacApiClient,
    hostname: Optional[str] = None,
    agent_name: Optional[str] = None,
    capabilities: Optional[List[str]] = None,
    resources: Optional[JsonDict] = None,
    machine_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    hermes_instance_id: Optional[str] = None,
    executor_argv: Optional[List[str]] = None,
) -> JsonDict:
    """Register or refresh the machine and agent rows for this worker process."""
    host = (hostname or socket.gethostname()).strip()
    if not host:
        raise MacApiError("hostname is required for worker registration")
    name = (agent_name or host).strip()
    if not name:
        raise MacApiError("agent_name is required for worker registration")
    resolved_machine_id = machine_id or _stable_id("machine", host)
    resolved_agent_id = agent_id or _stable_id("agent", name)
    # Hardware self-reporting: detect the local accelerator/CPU/memory and record
    # it in the registry so the fleet OWNS a hardware inventory (vs hand notes
    # that drift and get an agent's silicon wrong). Best-effort — the probe never
    # blocks registration.
    try:
        from mac.hardware import detect_hardware

        resources = {**(resources or {}), "hardware": detect_hardware()}
    except Exception:  # noqa: BLE001 - hardware probe must never fail registration
        pass
    # media-01 capability self-advertisement: a GPU agent announces the media ops
    # it serves in resources["media_routes"]; the hub composes its /v1/media
    # routing table from live agents, so the fleet uses this agent with zero
    # operator config. Two ways to set it, both honored here:
    #   1. MAC_AGENT_MEDIA_ROUTES — explicit JSON list of route dicts (override).
    #   2. MAC_AGENT_GEN_MODEL — a catalog model id; the route is derived from the
    #      catalog + this agent's detected hardware (GPU-GATED: a CPU agent with
    #      the var set advertises nothing), self-addressed via MAC_AGENT_GEN_BASE_URL
    #      (or MAC_AGENT_GEN_HOST:MAC_AGENT_GEN_PORT). So a single global
    #      MAC_AGENT_GEN_MODEL can be set fleet-wide and only real GPU agents serve.
    _media_routes_env = (os.environ.get("MAC_AGENT_MEDIA_ROUTES") or "").strip()
    _routes = None
    if _media_routes_env:
        try:
            parsed = json.loads(_media_routes_env)
            if isinstance(parsed, list):
                _routes = parsed
        except ValueError:
            _routes = None
    else:
        # Catalog-derived, GPU-gated routes for each configured local gen server.
        # The worker's service-claim sync (advertise-on-hold) narrows this to the
        # ops the agent actually HOLDS; here we stamp the willing set initially.
        hw = resources.get("hardware") if isinstance(resources, dict) else None
        derived = _build_willing_media_routes(host, hw)
        if derived:
            _routes = derived
    if _routes:
        resources = {**(resources or {}), "media_routes": _routes}
    # This self-report is recomputed from the process that is about to claim
    # work.  Never trust a similarly named value supplied through --resources.
    resources = dict(resources or {})
    resources.pop(REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY, None)
    report_executor_attestation = _read_only_report_executor_attestation(
        executor_argv
    )
    if report_executor_attestation is not None:
        resources[REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY] = (
            report_executor_attestation
        )
    resources = _resources_with_command_inventory(
        resources,
        source_repo=_default_self_update_repo(),
        agent_id=resolved_agent_id,
    )
    machine = client.post(
        "/machines",
        {
            "hostname": host,
            "machine_id": resolved_machine_id,
            "labels": {"registered_by": "mac-agent"},
            "resources": resources or {},
            "trusted": True,
        },
    )
    _register_runtime_identity_for_worker(client, name, hermes_instance_id)
    agent_resources = dict(resources or {})
    deployment_generation = _active_worker_deployment_generation()
    if deployment_generation:
        agent_resources["deployment_generation"] = deployment_generation
    agent = client.post(
        "/agents",
        {
            "machine_id": machine["id"],
            "name": name,
            "agent_id": resolved_agent_id,
            "capabilities": capabilities or [],
            "resources": agent_resources,
            "hermes_instance_id": hermes_instance_id,
            "status": "draining" if deployment_generation else None,
            "health_status": "degraded" if deployment_generation else None,
        },
    )
    _ensure_worker_fleet_membership(client, agent_name=name, agent_id=str(agent["id"]))
    return agent


# Declarative manifest of pip dependencies every agent must have to function,
# as (name + version) specifiers. Reconciled at agent-lifecycle startup via
# MacWorker.reconcile_runtime_deps() with a version-aware probe+install, so a
# fresh or stale node converges to the right versions on demand WITHOUT waiting
# for a redeploy. Add fleet-wide runtime deps here; keep them pinned.

class MacWorker(DebugTerminalMixin, ReflectMixin, DirectableMixin, WorkspaceGCMixin, RepoPrepMixin, RuntimeDepsMixin):
    """Small worker harness for mac-owned tasks.

    This is intentionally narrower than ACC's deployed worker. It proves the
    claim/start/execute/evidence/review handoff without owning Hermes memory or
    pretending to be the final production daemon.
    """

    def __init__(
        self,
        client: MacApiClient,
        agent_id: str,
        workspace: Path,
        executor: Executor,
        lease_seconds: int = 900,
        running_digest: Optional[str] = None,
        poll_interval_seconds: float = 1.0,
        allowed_projects: Optional[List[str]] = None,
        required_metadata: Optional[JsonDict] = None,
        claim_only_canary_tasks: bool = False,
        lease_renew_interval_seconds: Optional[float] = None,
        agentbus_control_enabled: bool = True,
        self_update_repo: Optional[Path] = None,
        agentbus_control_state_path: Optional[Path] = None,
        attestation_key: Optional[str] = None,
        attestation_key_env_path: Optional[Path] = None,
        status_update_sink: Optional[StatusUpdateSink] = None,
    ) -> None:
        if not agent_id:
            raise MacApiError("agent_id is required")
        self.client = client
        self.agent_id = agent_id
        self.workspace = workspace
        self.executor = executor
        if isinstance(self.executor, SubprocessExecutor):
            self.executor.audit_sink = self._record_command_audit
        self.lease_seconds = lease_seconds
        self.running_digest = running_digest
        # Attestation key for signing verification manifests
        # (mac-ng2). Falls back to MAC_ATTESTATION_KEY when not passed.
        # Without a key the worker still writes evidence — but the
        # default-review workflow will reject it as "manifest_not_signed"
        # and refuse to publish. The CLI surfaces this in deploy via
        # MAC_ATTESTATION_KEY.
        self.attestation_key = attestation_key or os.environ.get("MAC_ATTESTATION_KEY")
        # Where a healed/rotated key is persisted so the NEXT process start
        # inherits it (mirrors the startup --attestation-key-env behavior).
        self.attestation_key_env_path = attestation_key_env_path
        # Rate-limit self-heal rotations: one per window, so a non-key cause of
        # signature rejections can never drive a rotation loop.
        self._last_attestation_heal_at = 0.0
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.allowed_projects = list(allowed_projects or [])
        self.required_metadata = dict(required_metadata or {})
        self.claim_only_canary_tasks = bool(claim_only_canary_tasks)
        self.lease_renew_interval_seconds = lease_renew_interval_seconds
        self.agentbus_control_enabled = bool(agentbus_control_enabled)
        self.self_update_repo = (self_update_repo or _default_self_update_repo()).expanduser().resolve()
        self.agentbus_control_state_path = (
            agentbus_control_state_path
            if agentbus_control_state_path is not None
            else self.workspace / ".mac-agentbus-control.json"
        )
        # Idle-gated deploys: a repo update that arrives while this agent is
        # mid-task is stashed here and applied on a later iteration, before
        # the next claim. Persisted so a pending update survives a worker
        # restart (the agentbus stream is already marked processed by then).
        self.pending_repo_update_path = (
            self.agentbus_control_state_path.parent
            / (self.agentbus_control_state_path.stem + "-pending-repo-update.json")
        )
        self.status_update_sink = status_update_sink or self._send_status_update_to_home_channels
        self._stop = False
        self._declared_digest = False
        self._declared_policy = False
        self._last_command_inventory_at = 0.0
        self._coding_route_probe_thread: Optional[threading.Thread] = None
        self._coding_route_probe_lock = threading.Lock()
        self._coding_route_report: JsonDict = {}
        self._coding_route_report_dirty = True
        self._last_coding_route_probe_at = 0.0
        # Workspace GC (task_02ebb6c4): prune completed-task worktrees on every
        # worker so an unbounded backlog never fills the disk and silently
        # breaks the coding-route probe. Runs off the poll thread.
        self._workspace_gc_lock = threading.Lock()
        self._workspace_gc_thread: Optional[threading.Thread] = None
        self._last_workspace_gc_at = 0.0
        self._last_gateway_lease_renew_at = 0.0
        self._last_dispatch_hold_reason: Optional[str] = None
        self._observation_post_failures = 0
        self._last_observation_failure_log_at = 0.0
        self.debug_terminal_enabled = _env_bool("MAC_DEBUG_TERMINAL_ENABLED", True)
        self._debug_terminal_sessions: Dict[str, DebugTerminalSession] = {}
        self._delivery_drain_lock = threading.Lock()
        self._delivery_drain_stop: Optional[threading.Event] = None
        self._delivery_drain_thread: Optional[threading.Thread] = None
        # Directable peer/directive turns (task_c6f02f06, MAC_WORKER_DIRECTABLE):
        # run off the poll thread so a 120s turn cannot starve heartbeats. The
        # in-flight set stops a second thread being spawned for the same stream
        # while its turn is still running (state is only marked on success).
        self._directable_state_lock = threading.Lock()
        self._directable_inflight: set[str] = set()
        try:
            self.delivery_drain_interval_seconds = float(
                os.environ.get("MAC_WORKER_DELIVERY_DRAIN_SECONDS", "20") or 20
            )
        except (TypeError, ValueError):
            self.delivery_drain_interval_seconds = 20.0
        # Bounded work on the co-located hub host (task_1bd5db4b): the hub runs
        # BOTH the control plane and this worker. A load-shed circuit-breaker
        # stops claiming and drains in-flight work when the control plane is
        # under pressure, then resumes when it recovers (hysteresis). Non-hub
        # workers get ``None`` here and are entirely unaffected.
        self._hub_load_shed: Optional[LoadShedBreaker] = None
        self._last_hub_shed_state: Optional[str] = None
        try:
            agent_name = str(
                getattr(self, "agent_name", "")
                or os.environ.get("MAC_AGENT_NAME", "")
                or ""
            )
            if is_hub_host(self.agent_id, agent_name):
                self._hub_load_shed = LoadShedBreaker(
                    HubLoadShedConfig.from_env(),
                    default_control_plane_sampler,
                )
        except Exception:  # noqa: BLE001 - breaker setup must never block startup.
            self._hub_load_shed = None

    def stop(self) -> None:
        """Signal the run loop to exit after the current task."""
        self._stop = True

    def run_forever(self, max_iterations: Optional[int] = None) -> List[WorkerRunResult]:
        """Loop run_once() with sleep on empty. Bounded by max_iterations for tests.

        Reacts to SIGTERM/SIGINT for graceful shutdown when running as a daemon.
        On exit, marks the agent offline so the control plane can requeue any
        active lease held by this worker. The signal handlers installed for the
        duration of this call are restored before return — the process-wide
        SIGTERM/SIGINT state is not mutated past the worker's lifetime.
        """
        prior_handlers = self._install_signal_handlers()
        results: List[WorkerRunResult] = []
        iterations = 0
        # Lifecycle self-update: in daemon mode (no max_iterations), probe+install
        # the declared runtime deps before serving so the agent converges to the
        # required versions on (re)start. Skipped for bounded test runs.
        if max_iterations is None:
            self._reconcile_runtime_deps_best_effort()
            # Daemon mode only: bounded test runs stay single-threaded and
            # deterministic; the loop-side drain still runs every iteration.
            self._start_delivery_drain_thread()
        try:
            while not self._stop and (max_iterations is None or iterations < max_iterations):
                iterations += 1
                try:
                    outcome = self.run_once()
                except Exception as exc:  # noqa: BLE001
                    # Loop resilience (loop-01): run_once best-effort-marks the
                    # task failed before any re-raise, so one task's error must
                    # not crash the worker and halt ALL autonomous work. Record
                    # it and continue to the next poll. (SIGTERM/SIGINT set
                    # self._stop via the handlers, not exceptions, so graceful
                    # shutdown is unaffected; KeyboardInterrupt/SystemExit are
                    # BaseException and still propagate.)
                    self._observe_log(
                        "worker.run_once.exception",
                        level="error",
                        detail={"error": str(exc), "iteration": iterations},
                    )
                    results.append(WorkerRunResult(status="error", error=str(exc)))
                    if max_iterations is None:
                        time.sleep(self.poll_interval_seconds)
                    continue
                if outcome.status in {"no_task", "held"}:
                    if max_iterations is None:
                        time.sleep(self.poll_interval_seconds)
                    continue
                results.append(outcome)
        finally:
            self._restore_signal_handlers(prior_handlers)
            self._stop_delivery_drain_thread()
            self._shutdown()
        return results

    def _install_signal_handlers(self) -> Dict[int, Any]:
        """Install graceful-stop signal handlers; return prior handlers so we
        can restore them. Returns an empty dict if signals can't be installed
        (e.g. when called outside the main thread)."""
        prior: Dict[int, Any] = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                prior[signum] = signal.signal(signum, lambda *_: self.stop())
            except (ValueError, AttributeError, OSError):
                # signal.signal raises if not in main thread or on platforms
                # without the signal. Tests bound execution via max_iterations.
                pass
        return prior

    def _restore_signal_handlers(self, prior: Dict[int, Any]) -> None:
        for signum, handler in prior.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, AttributeError, OSError):
                pass

    def _shutdown(self) -> None:
        self._close_all_debug_terminal_sessions()
        # Best-effort: mark offline so the control plane requeues any active
        # lease tied to this agent. Catch broadly: shutdown must not raise.
        try:
            self.client.post(
                "/agents/%s/heartbeat" % quote(self.agent_id, safe=""),
                {"status": "offline"},
            )
        except Exception:  # noqa: BLE001 — shutdown is a boundary
            pass

    def _maybe_hub_load_shed(self) -> Optional[WorkerRunResult]:
        """Load-shed on the co-located hub host before claiming new work.

        Returns a ``held`` result (so the caller skips the claim this tick) when
        the breaker is shedding or draining, else ``None`` (claim as usual). The
        breaker state and the triggering metric are logged/exposed so an operator
        can see WHY the hub host is or isn't working. Non-hub workers have no
        breaker and this is a no-op.
        """
        breaker = self._hub_load_shed
        if breaker is None:
            return None
        try:
            snapshot = breaker.snapshot()
        except Exception:  # noqa: BLE001 - a sampler failure must not wedge the worker.
            return None
        state = snapshot.state
        evidence = snapshot.to_dict()
        evidence["agent_id"] = self.agent_id
        if state is BreakerState.CLAIMING:
            if self._last_hub_shed_state and self._last_hub_shed_state != state.value:
                self._observe_log(
                    "worker.hub_load_shed.resumed",
                    level="info",
                    subject_type="agent",
                    subject_id=self.agent_id,
                    detail=evidence,
                )
            self._last_hub_shed_state = state.value
            return None
        # SHEDDING or DRAINING: refuse to claim this tick.
        if self._last_hub_shed_state != state.value:
            self._observe_log(
                "worker.hub_load_shed.shedding",
                level="warning",
                subject_type="agent",
                subject_id=self.agent_id,
                detail=evidence,
            )
            self._last_hub_shed_state = state.value
        reason = "hub load-shed %s: %s=%.3f >= high %.3f" % (
            state.value,
            snapshot.metric,
            snapshot.value,
            snapshot.high,
        )
        return WorkerRunResult(status="held", evidence=evidence, error=reason)

    def run_once(self) -> WorkerRunResult:
        deployment_generation, deployment_barrier_active = _deployment_barrier_state()
        if deployment_barrier_active:
            # A freshly restarted worker must not consume AgentBus controls before
            # the deployment controller authorizes this exact generation.
            self._heartbeat()
            evidence = {
                "schema": "mac.worker_deployment_barrier.v1",
                "agent_id": self.agent_id,
                "deployment_generation": deployment_generation,
            }
            self._observe_log(
                "worker.dispatch_held.deployment_barrier",
                level="info",
                subject_type="agent",
                subject_id=self.agent_id,
                detail=evidence,
            )
            return WorkerRunResult(
                status="held",
                evidence=evidence,
                error="deployment barrier is active",
            )
        heartbeat_error: Optional[MacApiError] = None
        try:
            self._heartbeat()
        except MacApiError as exc:
            # A signed repository-update control is the recovery path for a
            # worker whose current source can no longer heartbeat cleanly.
            # Remember the failure, but first prove there is no durable hold
            # and allow only that bounded control phase to request restart.
            heartbeat_error = exc
        if heartbeat_error is None:
            _synchronize_directive_policy(self.client, self.agent_id)
        current_hold = self._current_dispatch_hold()
        if current_hold is not None:
            reason = str(current_hold.get("dispatch_hold_reason") or "dispatch held")
            if reason != self._last_dispatch_hold_reason:
                self._observe_log(
                    "worker.dispatch_held",
                    level="info",
                    detail={"agent_id": self.agent_id, "reason": reason},
                )
                self._last_dispatch_hold_reason = reason
            return WorkerRunResult(
                status="held",
                evidence=current_hold,
                error=reason,
            )
        control_result = self._process_agentbus_control(
            repository_update_only=heartbeat_error is not None
        )
        if heartbeat_error is None:
            self._poll_debug_terminal_sessions()
        if control_result and control_result.get("restart_requested"):
            self.stop()
            return WorkerRunResult(
                status="self_update_restart",
                evidence=control_result,
                error=control_result.get("summary"),
            )
        if heartbeat_error is not None:
            raise heartbeat_error
        local_update_blocker = self._local_repo_update_dispatch_blocker()
        if local_update_blocker is not None:
            reason = str(
                local_update_blocker.get("reason")
                or "source/runtime consistency requires a fleet redeploy"
            )
            self._observe_log(
                "worker.dispatch_held.source_runtime_inconsistent",
                level="error",
                subject_type="agent",
                subject_id=self.agent_id,
                detail=local_update_blocker,
            )
            return WorkerRunResult(
                status="held",
                evidence=local_update_blocker,
                error=reason,
            )
        self._maybe_start_workspace_gc()
        self._maybe_sync_service_claims()
        self._maintain_openclaw_gateway_leases()
        self._process_human_delivery_outbox()
        review_result = self._process_review_nudges()
        if review_result is not None:
            return review_result
        # A deferred repo update applies here — after the previous task
        # finished, before the next claim — so no task ever starts on a
        # stale pin while an update is pending.
        pending_update = self.apply_pending_repo_update_if_idle()
        if pending_update and pending_update.get("restart_requested"):
            self.stop()
            return WorkerRunResult(
                status="self_update_restart",
                evidence=pending_update,
                error=pending_update.get("summary"),
            )
        self._observe_policy_once()
        shed_result = self._maybe_hub_load_shed()
        if shed_result is not None:
            return shed_result
        assignment = self._claim_next_for_agent()
        if assignment is None:
            hold = self._current_dispatch_hold()
            if hold is not None:
                reason = str(hold.get("dispatch_hold_reason") or "dispatch held")
                if reason != self._last_dispatch_hold_reason:
                    self._observe_log(
                        "worker.dispatch_held",
                        level="info",
                        detail={"agent_id": self.agent_id, "reason": reason},
                    )
                    self._last_dispatch_hold_reason = reason
                return WorkerRunResult(status="held", error=reason)
            self._last_dispatch_hold_reason = None
            self._observe_log("worker.no_task", level="debug", detail={"agent_id": self.agent_id})
            return WorkerRunResult(status="no_task")

        task = assignment["task"]
        lease = assignment["lease"]
        return self.execute_assignment(task, lease)

    def execute_assignment(self, task: JsonDict, lease: JsonDict) -> WorkerRunResult:
        """Execute a task whose lease is already claimed.

        Caller is responsible for the claim; this method only does
        start -> prepare -> execute -> record -> publish -> submit-for-review.
        Suitable for K8s-mode where the runner has pre-claimed the task
        and the Job pod just needs to execute it.
        """
        task_id = task["id"]
        lease_id = str(lease["id"])
        task_dir: Optional[Path] = None
        attempt_state: JsonDict = {"recovery_count": 0, "recovery_log": []}
        self._observe_log(
            "worker.task_claimed",
            subject_type="task",
            subject_id=task_id,
            detail={"lease_id": lease_id, "agent_id": self.agent_id},
        )
        try:
            self.client.post(
                "/tasks/%s/start?%s"
                % (
                    quote(task_id, safe=""),
                    urlencode(
                        {"agent_id": self.agent_id, "lease_id": lease_id}
                    ),
                ),
                {},
            )
            try:
                task_dir = self._prepare_task_workspace(task, lease)
            except Exception as _prep_exc:
                # Workspace preparation is an environment-prerequisite step:
                # it fetches/rebases the canonical repo and lays out the task
                # worktree. Historically only ``RuntimeError``/``OSError`` were
                # routed through the harness-recovery reflex, so any OTHER
                # exception (a git ``subprocess.CalledProcessError``, a
                # ``MacApiError`` from the fetch/rebase API round-trip, a
                # ``KeyError``/``TypeError`` from malformed task metadata) skipped
                # just-in-time recovery entirely and wedged the assignment into a
                # bare ``worker_exception`` -> blocked loop with no remediation
                # (observed live: three consecutive environment-class failures on
                # a dream-repair prerequisite, all with empty diagnostics). Triage
                # every prep failure through the reflex; the unrecovered branch
                # re-raises so the outer handler still captures the traceback.
                if isinstance(_prep_exc, OSError):
                    _step = "disk_io"
                elif any(kw in str(_prep_exc) for kw in ("fetch", "rebase", "clone", "checkout")):
                    _step = "fetch_rebase"
                else:
                    _step = "worktree_preparation"
                _wt_dir = self.workspace / _safe_path_component(task_id)
                _wt_dir.mkdir(parents=True, exist_ok=True)
                def _noop_dispatch(_action, _ctx):
                    pass
                _recovered, _choice, _msg = _hrr.try_recovery(
                    attempt_state,
                    str(_prep_exc),
                    _noop_dispatch,
                    lambda _s, _c, _r: self._emit_recovery_observability(
                        task_id, _s, _c, _r
                    ),
                )
                self._append_harness_recovery_log(
                    _wt_dir, _step, _choice, _msg
                )
                if _recovered:
                    task_dir = self._prepare_task_workspace(task, lease)
                else:
                    raise
            started = time.monotonic()
            execution = self._execute_with_lease_renewal(task, lease, task_dir)
            duration_ms = (time.monotonic() - started) * 1000.0
            self._observe_metric(
                "worker.execution.duration_ms",
                duration_ms,
                unit="ms",
                subject_type="task",
                subject_id=task_id,
                detail={"returncode": execution.returncode},
            )
            self._observe_log(
                "worker.execution.completed",
                level="info" if execution.succeeded else "error",
                subject_type="task",
                subject_id=task_id,
                detail={"returncode": execution.returncode, "summary": execution.summary},
            )
            if not self._assignment_is_current(task_id, lease_id):
                return self._stale_result(
                    task_id,
                    lease,
                    "assignment no longer current after executor completed",
                    execution=execution,
                )
            _bootstrap_meta = (execution.metadata.get("bootstrap") or {})
            _boot_failed = (
                isinstance(_bootstrap_meta, dict)
                and (
                    (_bootstrap_meta.get("returncode") not in (None, 0))
                    or bool(_bootstrap_meta.get("error"))
                    or bool(_bootstrap_meta.get("status"))
                )
            )
            if not execution.succeeded and _boot_failed:
                _boot_info = "bootstrap failed: %s" % (_bootstrap_meta.get("error") or _bootstrap_meta.get("status") or "unknown")
                def _noop_dispatch_b(_action, _ctx):
                    pass
                _b_recovered, _b_choice, _b_msg = _hrr.try_recovery(
                    attempt_state,
                    _boot_info,
                    _noop_dispatch_b,
                    lambda _s, _c, _r: self._emit_recovery_observability(
                        task_id, _s, _c, _r
                    ),
                )
                self._append_harness_recovery_log(
                    task_dir, "bootstrap", _b_choice, _b_msg
                )
                if _b_recovered:
                    started = time.monotonic()
                    execution = self._execute_with_lease_renewal(task, lease, task_dir)
            evidence = self._record_execution(
                task_id,
                task_dir,
                execution,
                lease_id=lease_id,
                attempt_state=attempt_state,
            )
            if execution.succeeded:
                evidence_metadata = ensure_json_object(evidence.get("metadata"))
                manifest = ensure_json_object(evidence_metadata.get("verification"))
                evidence_type = str(
                    manifest.get("evidence_type") or ""
                ).strip().lower()
                if evidence_type == "plan_decomposed":
                    submission_problems = _worker_verification_contract_problems(
                        manifest,
                        evidence_type,
                    )
                    if not submission_problems:
                        try:
                            decomposed = self.client.post(
                                "/tasks/%s/children" % quote(task_id, safe=""),
                                {
                                    "children": list(manifest.get("children") or []),
                                    "actor": self.agent_id,
                                    "lease_id": lease_id,
                                },
                            )
                        except MacApiError as exc:
                            submission_problems = [
                                "plan_decomposed evidence could not be routed to "
                                "durable child tasks: %s" % exc
                            ]
                        else:
                            parent = (
                                decomposed.get("parent", decomposed)
                                if isinstance(decomposed, dict)
                                else None
                            )
                            return WorkerRunResult(
                                status="decomposed",
                                task=parent,
                                lease=lease,
                                evidence=evidence,
                            )
                else:
                    submission_problems = self._execution_submission_problems(
                        task_dir, evidence
                    )
                if submission_problems:
                    self._observe_log(
                        "worker.execution.verification_failed",
                        level="error",
                        subject_type="task",
                        subject_id=task_id,
                        detail={
                            "evidence_id": evidence.get("id"),
                            "problems": submission_problems,
                        },
                    )
                    blocked_task = self.client.post(
                        "/tasks/%s/transition" % quote(task_id, safe=""),
                        {
                            "target_state": "blocked",
                            "actor": self.agent_id,
                            "lease_id": lease_id,
                            "detail": {
                                "reason": "verification_contract_failed",
                                "manual_repair_required": True,
                                "evidence_id": evidence.get("id"),
                                "problems": submission_problems,
                            },
                        },
                    )
                    return WorkerRunResult(
                        status="blocked",
                        task=blocked_task,
                        lease=lease,
                        evidence=evidence,
                        error="; ".join(submission_problems[:4]),
                    )
                submit_path = "/tasks/%s/submit-for-review?%s" % (
                    quote(task_id, safe=""),
                    urlencode(
                        {
                            "agent_id": self.agent_id,
                            "lease_id": lease_id,
                            "advance_default_workflow": "true",
                        }
                    ),
                )
                try:
                    reviewed_task = self.client.post(submit_path, {})
                except MacApiError as exc:
                    # Attestation-key desync self-heal (2026-07-14 churn root
                    # cause): a hub-side rotation while this process runs (e.g.
                    # a gateway deploy re-keying the agent) leaves the worker
                    # signing every manifest with a stale key — each finished
                    # task is then rejected here, blocks, retries the SAME
                    # deterministic failure, and dies at max attempts. On the
                    # signature rejection: re-validate our key with the hub,
                    # rotate+persist if stale, re-sign/re-record the evidence,
                    # and retry the submit once.
                    if (
                        "signature does not verify" not in str(exc)
                        or not self._heal_attestation_key()
                    ):
                        raise
                    evidence = self._record_execution(
                        task_id,
                        task_dir,
                        execution,
                        lease_id=lease_id,
                        attempt_state=attempt_state,
                    )
                    reviewed_task = self.client.post(submit_path, {})
                return WorkerRunResult(
                    status="submitted_for_review",
                    task=reviewed_task,
                    lease=lease,
                    evidence=evidence,
                )
            blocked_task = self.client.post(
                "/tasks/%s/transition" % quote(task_id, safe=""),
                {
                    "target_state": "blocked",
                    "actor": self.agent_id,
                    "lease_id": lease_id,
                    "detail": {
                        "reason": "executor_failed",
                        "manual_repair_required": True,
                        "returncode": execution.returncode,
                        "evidence_id": evidence["id"],
                    },
                },
            )
            return WorkerRunResult(
                status="blocked",
                task=blocked_task,
                lease=lease,
                evidence=evidence,
                error=execution.summary,
            )
        except subprocess.TimeoutExpired as exc:
            if not self._assignment_is_current(task_id, lease_id):
                return self._stale_result(task_id, lease, str(exc))
            stdout = _coerce_process_output(exc.stdout)
            stderr = _coerce_process_output(exc.stderr)
            execution = WorkerExecution(
                124,
                "executor timed out after %ss" % exc.timeout,
                stdout=stdout,
                stderr=stderr,
                metadata={
                    "timeout_seconds": exc.timeout,
                    "process_tree_terminated": True,
                },
            )
            evidence = (
                self._record_execution(
                    task_id,
                    task_dir,
                    execution,
                    lease_id=lease_id,
                )
                if task_dir is not None
                else None
            )
            blocked_task = self.client.post(
                "/tasks/%s/transition" % quote(task_id, safe=""),
                {
                    "target_state": "blocked",
                    "actor": self.agent_id,
                    "lease_id": lease_id,
                    "detail": {
                        "reason": "executor_timeout",
                        "manual_repair_required": True,
                        "timeout_seconds": exc.timeout,
                        "process_tree_terminated": True,
                        "evidence_id": evidence.get("id") if evidence else None,
                    },
                },
            )
            return WorkerRunResult(
                status="blocked",
                task=blocked_task,
                lease=lease,
                evidence=evidence,
                error=execution.summary,
            )
        except Exception as exc:
            if not self._assignment_is_current(task_id, lease_id):
                return self._stale_result(task_id, lease, str(exc))
            # A bare ``worker_exception`` transition used to carry only
            # ``error=str(exc)`` -- none of the keys ``_diagnostic_output_tail``
            # scans (stdout/stderr/output/*_tail), so the hub recorded an empty
            # ``output_tail`` with "transition supplied no ... field" and every
            # retry blocked with no actionable diagnostics. Preserve the
            # traceback tail as a durable execution/evidence artifact and surface
            # it (plus the exception type and evidence id) on the transition so
            # the failure is diagnosable from the task history alone.
            exc_type = type(exc).__name__
            tb_text = traceback.format_exc()
            self._observe_log(
                "worker.execution.exception",
                level="error",
                subject_type="task",
                subject_id=task_id,
                detail={"error": str(exc), "exception_type": exc_type},
            )
            evidence: Optional[JsonDict] = None
            if task_dir is not None:
                try:
                    exc_execution = WorkerExecution(
                        1,
                        "worker raised %s: %s" % (exc_type, exc),
                        stdout="",
                        stderr=tb_text,
                        metadata={
                            "worker_exception": True,
                            "exception_type": exc_type,
                        },
                    )
                    evidence = self._record_execution(
                        task_id,
                        task_dir,
                        exc_execution,
                        lease_id=lease_id,
                    )
                except Exception:  # noqa: BLE001 - evidence capture is best-effort
                    evidence = None
            try:
                self.client.post(
                    "/tasks/%s/transition" % quote(task_id, safe=""),
                    {
                        "target_state": "blocked",
                        "actor": self.agent_id,
                        "lease_id": lease_id,
                        "detail": {
                            "reason": "worker_exception",
                            "manual_repair_required": True,
                            "error": str(exc),
                            "exception_type": exc_type,
                            "output_tail": tb_text,
                            "evidence_id": evidence.get("id") if evidence else None,
                        },
                    },
                )
            except Exception:
                pass
            raise

    def _assignment_is_current(self, task_id: str, lease_id: str) -> bool:
        try:
            current = self.client.get("/tasks/%s" % quote(task_id, safe=""))
        except MacApiError:
            # Hub unreachable / transient API error: preserve the older
            # behavior and let the concrete operation surface the
            # failure. Narrowed from bare ``except Exception`` (mac-h3d)
            # so TypeError/KeyError/AttributeError from a malformed
            # response or a programming bug bubbles up instead of being
            # silently treated as "still current."
            return True
        current_task = current.get("task", current)
        return (
            current_task.get("owner_agent_id") == self.agent_id
            and current_task.get("lease_id") == lease_id
            and current_task.get("state") in {"claimed", "running"}
        )

    def _stale_result(
        self,
        task_id: str,
        lease: JsonDict,
        reason: str,
        execution: Optional[WorkerExecution] = None,
    ) -> WorkerRunResult:
        detail: JsonDict = {
            "agent_id": self.agent_id,
            "lease_id": lease["id"],
            "reason": reason,
        }
        if execution is not None:
            detail.update(
                {
                    "returncode": execution.returncode,
                    "summary": execution.summary,
                }
            )
        self._observe_log(
            "worker.execution.stale_result",
            level="warning",
            subject_type="task",
            subject_id=task_id,
            detail=detail,
        )
        try:
            current = self.client.get("/tasks/%s" % quote(task_id, safe=""))
            current_task: Optional[JsonDict] = current.get("task", current)
        except Exception:
            current_task = None
        return WorkerRunResult(
            status="stale_result",
            task=current_task,
            lease=lease,
            error=reason,
        )

    def _execute_with_lease_renewal(
        self,
        task: JsonDict,
        lease: JsonDict,
        task_dir: Path,
    ) -> WorkerExecution:
        stop = threading.Event()
        thread: Optional[threading.Thread] = None
        interval = self.lease_renew_interval_seconds
        if interval is None:
            interval = max(1.0, min(60.0, float(self.lease_seconds) / 2.0))
        if self.lease_seconds > 0 and interval > 0:
            thread = threading.Thread(
                target=self._renew_lease_until_stopped,
                args=(lease["id"], task["id"], stop, interval),
                daemon=True,
            )
            thread.start()
        metadata = task.get("metadata") if isinstance(task, dict) else {}
        runtime = metadata.get("runtime") if isinstance(metadata, dict) else {}
        break_glass = (
            runtime.get("break_glass_authorization")
            if isinstance(runtime, dict)
            and isinstance(runtime.get("break_glass_authorization"), dict)
            else None
        )
        audit_metadata: JsonDict = {"execution_kind": "task"}
        if break_glass is not None:
            audit_metadata.update(
                {
                    "execution_boundary": "host",
                    "break_glass_authorization_id": break_glass.get("id"),
                }
            )
        try:
            return self._call_executor(
                task,
                task_dir,
                {
                    "agent_id": self.agent_id,
                    "task_id": task["id"],
                    "lease_id": lease["id"],
                    "metadata": audit_metadata,
                },
            )
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=1.0)

    def _renew_lease_until_stopped(
        self,
        lease_id: str,
        task_id: str,
        stop: threading.Event,
        interval_seconds: float,
    ) -> None:
        cancellation_poll_seconds = max(
            0.01,
            min(
                5.0,
                _env_float("MAC_WORKER_CANCELLATION_POLL_SECONDS", 5.0),
                interval_seconds,
            ),
        )
        next_renewal = time.monotonic() + interval_seconds
        while not stop.wait(cancellation_poll_seconds):
            try:
                assignment_lost = (
                    isinstance(self.executor, SubprocessExecutor)
                    and self.executor.has_active_process()
                    and not self._assignment_is_current(task_id, lease_id)
                )
            except Exception as exc:  # noqa: BLE001 - the assignment check makes
                # an HTTP call; a transient (or unexpected) error there must NOT
                # escape and kill this renewal thread — that silently loses the
                # lease and wedges the task (observed 2026-07-14 on a hub blip).
                # Fail safe ("still ours"), log loudly, and keep renewing.
                self._observe_log(
                    "worker.lease_renew.assignment_check_error",
                    level="warning",
                    subject_type="task",
                    subject_id=task_id,
                    detail={"agent_id": self.agent_id, "lease_id": lease_id, "error": str(exc)},
                )
                assignment_lost = False
            if assignment_lost:
                reason = "task assignment is no longer current"
                cancelled = self.executor.cancel_current(reason)
                self._observe_log(
                    "worker.execution.cancelled",
                    level="warning",
                    subject_type="task",
                    subject_id=task_id,
                    detail={
                        "agent_id": self.agent_id,
                        "lease_id": lease_id,
                        "reason": reason,
                        "process_tree_terminated": cancelled,
                    },
                )
                return
            if time.monotonic() < next_renewal:
                continue
            next_renewal = time.monotonic() + interval_seconds
            try:
                lease = self.client.post(
                    "/leases/%s/renew" % quote(lease_id, safe=""),
                    {"agent_id": self.agent_id, "lease_seconds": self.lease_seconds},
                )
                self._observe_log(
                    "worker.lease_renewed",
                    subject_type="task",
                    subject_id=task_id,
                    detail={"lease_id": lease_id, "expires_at": lease["expires_at"]},
                )
            except Exception as exc:  # noqa: BLE001 - renewal is best-effort telemetry
                self._observe_log(
                    "worker.lease_renew_failed",
                    level="error",
                    subject_type="task",
                    subject_id=task_id,
                    detail={"lease_id": lease_id, "error": str(exc)},
                )
            # Keep last_seen_at fresh while this single-threaded worker is busy.
            # The agent heartbeat is otherwise only sent between tasks (run_once),
            # so a task longer than the reviewer-stale window (default 300s) makes
            # this alive, lease-renewing agent look `reviewer_stale` and get
            # dropped/retracted as a reviewer under load. Send status=busy -- the
            # agent IS busy; idle would wrongly free it for new dispatch. Best
            # effort: never let a liveness ping disturb execution.
            try:
                self._heartbeat(status_override="busy")
            except Exception:  # noqa: BLE001 - liveness ping is best-effort
                pass

    def _review_heartbeat_interval_seconds(self) -> float:
        """Cadence for the review liveness ticker — well inside the stale window."""
        interval = self.lease_renew_interval_seconds
        if interval is None or interval <= 0:
            interval = max(5.0, min(60.0, float(self.lease_seconds or 120) / 4.0))
        return float(interval)

    def _heartbeat_until_stopped(self, stop: threading.Event, interval_seconds: float) -> None:
        """Heartbeat busy on a background thread while a long REVIEW runs.

        Reviews have no lease (so no lease ticker), yet a heavy review rebuilds and
        runs the full contract suite for minutes, blocking this single-threaded
        worker. Without this ping the hub flips the agent out of IDLE/BUSY and
        retracts the review claim as `reviewer_not_available` before the verdict
        lands. Best effort: a liveness ping must never disturb the review.
        """
        while not stop.wait(interval_seconds):
            try:
                self._heartbeat(status_override="busy")
            except Exception:  # noqa: BLE001 - liveness ping is best-effort
                pass

    def _claim_next_for_agent(self) -> Optional[JsonDict]:
        return self.client.post(
            "/agents/%s/claim-next" % quote(self.agent_id, safe=""),
            self._claim_payload(dry_run=False),
        )

    def _current_dispatch_hold(self) -> Optional[JsonDict]:
        """Return the durable hold record, distinguishing it from an empty queue."""

        # The hold is a safety fence, not optional status decoration.  An
        # unavailable or malformed hub response must stop this iteration; it
        # must never be interpreted as positive proof that the worker is
        # unheld.  run_forever owns retry/backoff for transient API failures.
        agent = self.client.get(
            "/agents/%s" % quote(self.agent_id, safe="")
        )
        if not isinstance(agent, dict):
            raise MacApiError("agent dispatch-hold response is not an object")
        return agent if isinstance(agent, dict) and agent.get("dispatch_hold") else None

    def dry_run_claim(self) -> Optional[JsonDict]:
        self._heartbeat()
        self._observe_policy_once()
        assignment = self.client.post(
            "/agents/%s/claim-next" % quote(self.agent_id, safe=""),
            self._claim_payload(dry_run=True),
        )
        self._observe_log(
            "worker.routing.dry_run_result",
            level="info" if assignment is not None else "debug",
            subject_type="task" if assignment else None,
            subject_id=(assignment.get("task") or {}).get("id") if assignment else None,
            detail={
                "agent_id": self.agent_id,
                "matched": assignment is not None,
                "policy": self._policy_payload(),
            },
        )
        return assignment

    def _process_review_nudges(self) -> Optional[WorkerRunResult]:
        try:
            messages = self.client.post(
                "/agents/%s/messages/deliver?%s"
                % (quote(self.agent_id, safe=""), urlencode({"limit": 20})),
                {},
            )
        except Exception as exc:  # noqa: BLE001 - message polling must not break task polling.
            self._observe_log(
                "worker.review_nudge.poll_failed",
                level="warning",
                detail={"agent_id": self.agent_id, "error": str(exc)},
            )
            return None

        if not isinstance(messages, list):
            return None
        skipped_result: Optional[WorkerRunResult] = None
        for message in messages:
            if not isinstance(message, dict):
                continue
            if str(message.get("message_type") or "") == "status_update":
                self._handle_status_update_message(message)
                continue
            if str(message.get("message_type") or "") != "nudge":
                continue
            payload = message.get("payload")
            if not isinstance(payload, dict):
                continue
            if str(payload.get("reason") or "") != "produce_review_verdict":
                continue
            result = self._handle_review_verdict_nudge(message, payload)
            if result.status in {"review_not_claimable", "review_nudge_invalid"}:
                skipped_result = result
                continue
            return result
        return skipped_result

    def _handle_status_update_message(self, message: JsonDict) -> None:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return
        if str(payload.get("schema") or "") != "mac.notifier.task_progress.v1":
            return
        try:
            result = self.status_update_sink(payload)
            self._observe_log(
                "worker.notifier.status_forwarded",
                level="info",
                detail={
                    "message_id": message.get("id"),
                    "status": result.get("status"),
                    "sent": result.get("sent", 0),
                    "skipped": result.get("skipped", 0),
                    "failed": result.get("failed", 0),
                },
            )
        except Exception as exc:  # noqa: BLE001 - notification forwarding is best effort.
            self._observe_log(
                "worker.notifier.status_forward_failed",
                level="warning",
                detail={"message_id": message.get("id"), "error": str(exc)},
            )

    def _send_status_update_to_home_channels(self, payload: JsonDict) -> JsonDict:
        if os.environ.get("MAC_CHAT_GATEWAY_IMPL", "").strip().lower() == "openclaw":
            # OpenClaw deployments use the fenced communication outbox.  Never
            # fall back to a direct provider SDK: that would bypass the stable
            # public identity, gateway lease, delivery receipt, and sandbox.
            return {
                "status": "skipped",
                "sent": 0,
                "skipped": 1,
                "failed": 0,
                "reason": "openclaw_outbox_required",
            }
        channel_type = str(payload.get("channel_type") or "").strip().lower()
        target = ensure_json_object(payload.get("target"))
        target_type = str(target.get("channel_type") or "").strip().lower()
        # OpenClaw is the only supported persona/Slack runtime; ``hermes`` is no
        # longer an accepted persona runtime value, so only blank/``slack`` route
        # here (OpenClaw itself returns above via the fenced outbox).
        if channel_type not in {"", "slack"} and target_type != "slack":
            return {"status": "skipped", "sent": 0, "skipped": 1, "failed": 0}

        hermes_home = mac_paths.gateway_home()
        accounts = _load_slack_accounts(hermes_home)
        home_channels = _load_slack_home_channels(hermes_home)
        if not accounts or not home_channels:
            return {"status": "skipped", "sent": 0, "skipped": 1, "failed": 0}

        try:
            from slack_sdk import WebClient  # type: ignore
        except Exception as exc:  # noqa: BLE001 - optional Hermes messaging dependency.
            return {
                "status": "failed",
                "sent": 0,
                "skipped": 0,
                "failed": 1,
                "error": "slack_sdk unavailable: %s" % exc,
            }

        notification = ensure_json_object(payload.get("notification"))
        text = _status_update_slack_text(notification)
        account_by_name = {
            str(account.get("name") or ""): account
            for account in accounts
            if str(account.get("name") or "")
        }
        target_team, target_channel = _target_slack_route(target)
        sent = 0
        failed = 0
        skipped = 0
        for channel in home_channels:
            channel_id = str(channel.get("channel_id") or "").strip()
            team_id = str(channel.get("team_id") or "").strip()
            if not channel_id:
                skipped += 1
                continue
            if target_channel and channel_id != target_channel:
                skipped += 1
                continue
            if target_team and team_id and team_id != target_team:
                skipped += 1
                continue
            account_name = str(channel.get("name") or channel.get("account") or "")
            account = account_by_name.get(account_name)
            if account is None:
                skipped += 1
                continue
            token = str(
                account.get("bot_token")
                or account.get("token")
                or account.get("slack_bot_token")
                or ""
            ).strip()
            if not token:
                skipped += 1
                continue
            try:
                response = WebClient(token=token).chat_postMessage(
                    channel=channel_id,
                    text=text,
                )
                if bool(response.get("ok", True)):
                    sent += 1
                else:
                    failed += 1
            except Exception as exc:  # noqa: BLE001 - continue across workspaces.
                failed += 1
                self._observe_log(
                    "worker.notifier.slack_send_failed",
                    level="warning",
                    detail={
                        "channel_id": channel_id,
                        "team_id": team_id,
                        "account": account_name,
                        "error": str(exc),
                    },
                )
        status = "sent" if sent else ("failed" if failed else "skipped")
        return {"status": status, "sent": sent, "skipped": skipped, "failed": failed}

    def _maintain_openclaw_gateway_leases(self) -> None:
        identity = os.environ.get("MAC_OPENCLAW_PUBLIC_IDENTITY", "").strip()
        message_bin = Path(
            os.environ.get("MAC_OPENCLAW_MESSAGE_BIN")
            or mac_paths.mac_home() / "bin" / "openclaw-message"
        )
        if not identity or not message_bin.is_file():
            return
        now = time.monotonic()
        if now - self._last_gateway_lease_renew_at < 30.0:
            return
        self._last_gateway_lease_renew_at = now
        try:
            accounts = self.client.get(
                "/communication/accounts?%s"
                % urlencode({"identity_id": identity, "enabled": "true"})
            )
            if not isinstance(accounts, list):
                return
            for account in accounts:
                if not isinstance(account, dict) or not account.get("id"):
                    continue
                try:
                    self.client.post(
                        "/communication/gateway-leases/acquire",
                        {
                            "account_id": account["id"],
                            "agent_id": self.agent_id,
                            "lease_seconds": 90,
                            "metadata": {
                                "runtime": "openclaw",
                                "confinement": "openshell",
                                "public_identity": identity,
                            },
                        },
                    )
                except Exception as exc:  # another healthy provider owns it
                    self._observe_log(
                        "worker.communication.gateway_lease_unavailable",
                        level="debug",
                        detail={"account_id": account["id"], "error": str(exc)},
                    )
        except Exception as exc:
            self._observe_log(
                "worker.communication.gateway_lease_failed",
                level="warning",
                detail={"identity": identity, "error": str(exc)},
            )

    def _process_human_delivery_outbox(self) -> None:
        """Lock-guarded outbox drain, callable from the task loop AND the
        background drain thread; a drain already in progress is skipped
        rather than queued (claims are leased hub-side, so skipping is safe)."""
        if not self._delivery_drain_lock.acquire(blocking=False):
            return
        try:
            self._drain_human_delivery_outbox()
        finally:
            self._delivery_drain_lock.release()

    def _start_delivery_drain_thread(self) -> None:
        """Drain the human-message outbox on a timer independent of the task loop.

        The loop-side drain in run_once only runs between task iterations, so a
        worker busy on a long task starved its gateway's outbox for the whole
        iteration even though the gateway itself was idle and connected
        (task_c049302b). Ephemeral or represented agents' proxied messages land
        on this gateway's account; they must not wait for its task cadence.
        """
        if (
            self._delivery_drain_thread is not None
            or self.delivery_drain_interval_seconds <= 0
        ):
            return
        stop = threading.Event()

        def _loop() -> None:
            while not stop.wait(self.delivery_drain_interval_seconds):
                try:
                    self._process_human_delivery_outbox()
                except Exception as exc:  # noqa: BLE001 — drain must never kill the thread
                    self._observe_log(
                        "worker.communication.outbox_drain_thread_error",
                        level="warning",
                        detail={"error": str(exc)},
                    )

        thread = threading.Thread(
            target=_loop, name="delivery-outbox-drain", daemon=True
        )
        self._delivery_drain_stop = stop
        self._delivery_drain_thread = thread
        thread.start()

    def _stop_delivery_drain_thread(self) -> None:
        if self._delivery_drain_stop is not None:
            self._delivery_drain_stop.set()
        self._delivery_drain_thread = None
        self._delivery_drain_stop = None

    def _drain_human_delivery_outbox(self) -> None:
        identity = os.environ.get("MAC_OPENCLAW_PUBLIC_IDENTITY", "").strip()
        message_bin = Path(
            os.environ.get("MAC_OPENCLAW_MESSAGE_BIN")
            or mac_paths.mac_home() / "bin" / "openclaw-message"
        )
        if not identity or not message_bin.is_file():
            return
        try:
            deliveries = self.client.post(
                "/communication/deliveries/claim",
                {"agent_id": self.agent_id, "limit": 10, "lease_seconds": 90},
            )
        except Exception as exc:
            self._observe_log(
                "worker.communication.outbox_claim_failed",
                level="warning",
                detail={"error": str(exc)},
            )
            return
        if not isinstance(deliveries, list):
            return
        account_cache: Dict[str, JsonDict] = {}
        for delivery in deliveries:
            if not isinstance(delivery, dict):
                continue
            delivery_id = str(delivery.get("id") or "")
            account_record_id = str(delivery.get("account_id") or "")
            try:
                account = account_cache.get(account_record_id)
                if account is None:
                    loaded = self.client.get(
                        "/communication/accounts/%s"
                        % quote(account_record_id, safe="")
                    )
                    if not isinstance(loaded, dict):
                        raise MacApiError("communication account response is not an object")
                    account = loaded
                    account_cache[account_record_id] = account
                command = [
                    str(message_bin),
                    "send",
                    "--channel",
                    str(delivery.get("channel") or account.get("channel") or ""),
                    "--account",
                    str(account.get("account_id") or "default"),
                    "--target",
                    str(delivery.get("target") or ""),
                    "--message",
                    str(delivery.get("body") or ""),
                    "--json",
                ]
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )
                if completed.returncode != 0:
                    raise RuntimeError(
                        (completed.stderr or completed.stdout or "OpenClaw send failed").strip()[:1000]
                    )
                receipt = _json_object_from_text(completed.stdout)
                self.client.post(
                    "/communication/deliveries/%s/ack"
                    % quote(delivery_id, safe=""),
                    {
                        "agent_id": self.agent_id,
                        "provider_message_id": _provider_message_id(receipt),
                        "detail": {
                            "channel": delivery.get("channel"),
                            "account_id": account.get("account_id"),
                            "openclaw": True,
                        },
                    },
                )
                self._observe_log(
                    "worker.communication.delivery_sent",
                    subject_type="human_message_delivery",
                    subject_id=delivery_id,
                    detail={"channel": delivery.get("channel"), "identity": identity},
                )
            except Exception as exc:
                try:
                    self.client.post(
                        "/communication/deliveries/%s/fail"
                        % quote(delivery_id, safe=""),
                        {
                            "agent_id": self.agent_id,
                            "error": str(exc)[:1000],
                            "retryable": True,
                        },
                    )
                except Exception:
                    pass
                self._observe_log(
                    "worker.communication.delivery_failed",
                    level="warning",
                    subject_type="human_message_delivery",
                    subject_id=delivery_id,
                    detail={"error": str(exc)},
                )

    def _handle_review_verdict_nudge(self, message: JsonDict, payload: JsonDict) -> WorkerRunResult:
        task_id = str(payload.get("task_id") or "").strip()
        review_id = str(payload.get("review_id") or "").strip()
        executor_evidence_id = str(payload.get("executor_evidence_id") or "").strip()
        if not task_id or not review_id or not executor_evidence_id:
            error = "review verdict nudge missing task_id, review_id, or executor_evidence_id"
            self._observe_log(
                "worker.review_nudge.invalid",
                level="warning",
                detail={"message_id": message.get("id"), "error": error, "payload": payload},
            )
            return WorkerRunResult(status="review_nudge_invalid", error=error)

        try:
            claim = self.client.post(
                "/reviews/%s/claim" % quote(review_id, safe=""),
                {
                    "reviewer_agent_id": self.agent_id,
                    "executor_evidence_id": executor_evidence_id,
                    "actor": self.agent_id,
                },
            )
            if isinstance(claim, dict) and claim.get("status") != "claimed":
                return WorkerRunResult(
                    status="review_not_claimable",
                    task=(
                        claim.get("task")
                        if isinstance(claim.get("task"), dict)
                        else None
                    ),
                    error=str(claim.get("reason") or "review is not claimable"),
                )
            task_detail = self.client.get("/tasks/%s" % quote(task_id, safe=""))
            task_dir = self._prepare_review_workspace(
                task_id,
                review_id,
                executor_evidence_id,
                task_detail if isinstance(task_detail, dict) else {},
                message,
                claim if isinstance(claim, dict) else {},
            )
            started = time.monotonic()
            # Keep this agent alive while the (minutes-long) review runs, so the
            # hub does not retract the claim as reviewer_not_available mid-review.
            review_hb_stop = threading.Event()
            review_hb = threading.Thread(
                target=self._heartbeat_until_stopped,
                args=(review_hb_stop, self._review_heartbeat_interval_seconds()),
                daemon=True,
            )
            review_hb.start()
            try:
                execution = self._call_executor(
                    self._review_task_payload(task_dir),
                    task_dir,
                    {
                        "agent_id": self.agent_id,
                        "task_id": task_id,
                        "metadata": {
                            "execution_kind": "review",
                            "review_id": review_id,
                            "executor_evidence_id": executor_evidence_id,
                            "nudge_message_id": message.get("id"),
                        },
                    },
                )
            finally:
                review_hb_stop.set()
                review_hb.join(timeout=1.0)
            duration_ms = (time.monotonic() - started) * 1000.0
            self._observe_metric(
                "worker.review.duration_ms",
                duration_ms,
                unit="ms",
                subject_type="task",
                subject_id=task_id,
                detail={
                    "returncode": execution.returncode,
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                },
            )
            evidence = self._record_review_execution(
                task_id,
                task_dir,
                execution,
                review_id=review_id,
                executor_evidence_id=executor_evidence_id,
                message_id=str(message.get("id") or ""),
            )
            if execution.succeeded:
                self._advance_review_workflow_after_verdict(task_id)
            else:
                self._heartbeat()
            status = "review_verdict_recorded" if execution.succeeded else "review_verdict_failed"
            self._observe_log(
                "worker.%s" % status,
                level="info" if execution.succeeded else "error",
                subject_type="task",
                subject_id=task_id,
                detail={
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                    "evidence_id": evidence.get("id"),
                    "returncode": execution.returncode,
                    "summary": execution.summary,
                },
            )
            return WorkerRunResult(
                status=status,
                task=(task_detail.get("task") if isinstance(task_detail, dict) else None),
                evidence=evidence,
                error=None if execution.succeeded else execution.summary,
            )
        except Exception as exc:
            if isinstance(exc, RepositoryAccessError):
                # The repository-access learning is written before the
                # exception is raised. Re-run reviewer selection immediately
                # so the control plane can prefer a known-successful peer
                # instead of re-nudging this reviewer with the same pattern.
                self._advance_review_workflow_after_verdict(task_id)
                try:
                    self._heartbeat()
                except Exception:  # noqa: BLE001 - the failure is already recorded.
                    pass
            self._observe_log(
                "worker.review_nudge.exception",
                level="error",
                subject_type="task",
                subject_id=task_id,
                detail={
                    "message_id": message.get("id"),
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                    "error": str(exc),
                    "failure_class": getattr(exc, "failure_class", ""),
                },
            )
            return WorkerRunResult(status="review_verdict_failed", error=str(exc))

    def _advance_review_workflow_after_verdict(self, task_id: str) -> None:
        try:
            self.client.post(
                "/reviews/default/tick?%s"
                % urlencode({"limit": 10, "actor": self.agent_id}),
                {},
            )
        except Exception as exc:  # noqa: BLE001 - verdict evidence is already recorded.
            self._observe_log(
                "worker.review_workflow.advance_failed",
                level="warning",
                subject_type="task",
                subject_id=task_id,
                detail={"agent_id": self.agent_id, "error": str(exc)},
            )

    def _process_agentbus_control(
        self, *, repository_update_only: bool = False
    ) -> Optional[JsonDict]:
        if not self.agentbus_control_enabled:
            return None
        try:
            processed = self._load_agentbus_control_state()
            streams = self.client.get(
                "/agentbus/streams?%s"
                % urlencode({"agent_id": self.agent_id, "status": "closed", "limit": 50})
            )
        except Exception as exc:  # noqa: BLE001 - control bus must not break task polling.
            self._observe_log(
                "worker.agentbus.control_poll_failed",
                level="warning",
                detail={"agent_id": self.agent_id, "error": str(exc)},
            )
            return None

        if not isinstance(streams, list):
            return None
        for stream in reversed(streams):
            if not isinstance(stream, dict):
                continue
            stream_id = str(stream.get("id") or "")
            if not stream_id or stream_id in processed:
                continue
            if stream.get("recipient_agent_id") != self.agent_id:
                continue
            topic = str(stream.get("topic") or "")
            content_type = str(stream.get("content_type") or "").split(";", 1)[0]
            if repository_update_only and not (
                topic == REPO_UPDATE_TOPIC
                and content_type == REPO_UPDATE_CONTENT_TYPE
            ):
                # A worker that cannot heartbeat is allowed only the signed,
                # bounded source-recovery control.  Config, terminal, reflect,
                # and directable work all wait for a positive healthy heartbeat.
                continue
            if topic == REPO_UPDATE_TOPIC and content_type == REPO_UPDATE_CONTENT_TYPE:
                result = self._handle_repo_update_stream(stream)
                processed.append(stream_id)
                self._save_agentbus_control_state(processed)
                self._publish_repo_update_result(stream, result)
                service_result = self._run_repo_update_service_restarts(result)
                if service_result:
                    self._publish_repo_update_result(
                        stream,
                        service_result,
                        attempts=_bounded_int(
                            os.environ.get("MAC_AGENTBUS_SERVICE_RESULT_PUBLISH_ATTEMPTS"),
                            1,
                            120,
                            60,
                        ),
                        delay_seconds=_bounded_float(
                            os.environ.get("MAC_AGENTBUS_SERVICE_RESULT_PUBLISH_RETRY_SECONDS"),
                            0.1,
                            10.0,
                            1.0,
                        ),
                    )
                if result.get("restart_requested"):
                    return result
                continue
            if topic == HERMES_CONFIG_APPLY_TOPIC and content_type == HERMES_CONFIG_APPLY_CONTENT_TYPE:
                result = self._handle_hermes_config_apply_stream(stream)
                processed.append(stream_id)
                self._save_agentbus_control_state(processed)
                self._publish_hermes_config_apply_result(stream, result)
                continue
            if topic == DEBUG_TERMINAL_OPEN_TOPIC and content_type == DEBUG_TERMINAL_OPEN_CONTENT_TYPE:
                result = self._handle_debug_terminal_open_stream(stream)
                processed.append(stream_id)
                self._save_agentbus_control_state(processed)
                self._observe_log(
                    "worker.agentbus.debug_terminal.%s" % result["status"],
                    level="info" if result["status"] == "opened" else "error",
                    detail=result,
                )
                continue
            if topic == REFLECT_REQUEST_TOPIC and content_type == REFLECT_REQUEST_CONTENT_TYPE:
                result = self._handle_reflect_request_stream(stream)
                processed.append(stream_id)
                self._save_agentbus_control_state(processed)
                continue
            # Directable peer/directive handling (task_c6f02f06). Default OFF:
            # when MAC_WORKER_DIRECTABLE is unset these branches are not entered
            # and behavior is byte-for-byte unchanged. GROUP peer streams
            # (participants set) are skipped in Phase 0 (1:1 only). The turn runs
            # off the poll thread and only marks the stream processed after its
            # reply publishes, so a crash re-tries.
            if _env_bool("MAC_WORKER_DIRECTABLE", False):
                if (
                    topic == PEER_MESSAGE_TOPIC
                    and content_type == PEER_MESSAGE_CONTENT_TYPE
                    and not stream.get("participants")
                ):
                    self._dispatch_directable_turn(
                        stream, self._handle_peer_message_stream
                    )
                    continue
                if topic == HUMAN_DIRECTIVE_TOPIC:
                    self._dispatch_directable_turn(
                        stream, self._handle_human_directive_stream
                    )
                    continue
            continue
        return None

    def _dispatch_directable_turn(
        self, stream: JsonDict, handler: Callable[[JsonDict], JsonDict]
    ) -> None:
        """Run a directable peer/directive turn on a background thread.

        A turn may take up to MAC_DIRECTABLE_TIMEOUT (120s default); running it
        inline in the poll loop would starve heartbeats and task claiming
        (mirrors the _start_delivery_drain_thread precedent). The handler both
        runs the turn and publishes the reply; only after it returns do we mark
        the stream processed, so a crash before the reply re-tries. An in-flight
        guard prevents a second thread for the same stream while the first runs.
        """
        stream_id = str(stream.get("id") or "")
        if not stream_id:
            return
        with self._directable_state_lock:
            if stream_id in self._directable_inflight:
                return
            self._directable_inflight.add(stream_id)

        def _run() -> None:
            try:
                handler(stream)
                self._mark_directable_processed(stream_id)
            except Exception as exc:  # noqa: BLE001 - never crash the worker.
                self._observe_log(
                    "worker.agentbus.directable.dispatch_error",
                    level="warning",
                    detail={"stream_id": stream_id, "error": str(exc)},
                )
            finally:
                with self._directable_state_lock:
                    self._directable_inflight.discard(stream_id)

        thread = threading.Thread(
            target=_run, name="directable-turn-%s" % stream_id[:16], daemon=True
        )
        thread.start()

    def _mark_directable_processed(self, stream_id: str) -> None:
        """Append *stream_id* to the persisted control state under a lock."""
        with self._directable_state_lock:
            processed = self._load_agentbus_control_state()
            if stream_id not in processed:
                processed.append(stream_id)
            self._save_agentbus_control_state(processed)

    def _handle_hermes_config_apply_stream(self, stream: JsonDict) -> JsonDict:
        stream_id = str(stream.get("id") or "")
        chunks = self.client.get(
            "/agentbus/streams/%s/chunks?%s"
            % (
                quote(stream_id, safe=""),
                urlencode({"agent_id": self.agent_id, "after_sequence": 0, "limit": 10}),
            )
        )
        payload: Any = None
        if isinstance(chunks, list) and chunks:
            payload = chunks[-1].get("payload") if isinstance(chunks[-1], dict) else None
        try:
            result = self._execute_hermes_config_apply(payload, stream_id)
        except Exception as exc:  # noqa: BLE001 - malformed control messages should report failure.
            result = self._hermes_config_apply_result(
                stream_id,
                "error",
                "Hermes config apply handler failed: %s" % exc,
                {},
            )
        self._observe_log(
            "worker.agentbus.hermes_config_apply.%s" % result["status"],
            level="info" if result["status"] == "applied" else "error",
            detail=result,
        )
        return result

    def _execute_hermes_config_apply(self, payload: Any, stream_id: str) -> JsonDict:
        request: JsonDict = payload if isinstance(payload, dict) else {}
        if request.get("schema") not in {None, "", HERMES_CONFIG_APPLY_SCHEMA}:
            return self._hermes_config_apply_result(
                stream_id,
                "error",
                "unsupported Hermes config apply schema: %s" % request.get("schema"),
                request,
            )
        hermes_payload = request.get("payload")
        if not isinstance(hermes_payload, dict):
            return self._hermes_config_apply_result(
                stream_id,
                "error",
                "Hermes config apply payload is missing",
                request,
            )
        applied = apply_hermes_surface_payload(hermes_payload)
        return self._hermes_config_apply_result(
            stream_id,
            "applied",
            "Hermes config applied",
            request,
            apply_result=applied,
            config_keys=applied.get("config_keys", []),
            env_keys=applied.get("env_keys", []),
            removed_env=applied.get("removed_env", []),
        )

    def _hermes_config_apply_result(
        self,
        stream_id: str,
        status: str,
        summary: str,
        request: JsonDict,
        **extra: Any,
    ) -> JsonDict:
        result: JsonDict = {
            "schema": HERMES_CONFIG_APPLY_RESULT_SCHEMA,
            "status": status,
            "summary": summary,
            "agent_id": self.agent_id,
            "stream_id": stream_id,
            "request_id": request.get("request_id"),
            "fleet_id": request.get("fleet_id"),
            "fleet_name": request.get("fleet_name"),
            "restart_requested": False,
        }
        for key, value in extra.items():
            if isinstance(value, str):
                result[key] = value[:4000]
            else:
                result[key] = value
        return result

    def _publish_hermes_config_apply_result(self, stream: JsonDict, result: JsonDict) -> None:
        sender = str(stream.get("sender_agent_id") or "")
        if not sender:
            return
        try:
            self.client.post(
                "/agentbus",
                {
                    "sender_agent_id": self.agent_id,
                    "recipient_agent_id": sender,
                    "content_type": HERMES_CONFIG_APPLY_RESULT_CONTENT_TYPE,
                    "topic": HERMES_CONFIG_APPLY_RESULT_TOPIC,
                    "payload": result,
                },
            )
        except Exception as exc:  # noqa: BLE001 - result publishing is best-effort.
            self._observe_log(
                "worker.agentbus.hermes_config_apply_result_failed",
                level="warning",
                detail={"stream_id": stream.get("id"), "error": str(exc)},
            )

    # ------------------------------------------------------------------
    # Reflect-request handler
    # ------------------------------------------------------------------

    def _handle_repo_update_stream(self, stream: JsonDict) -> JsonDict:
        stream_id = str(stream.get("id") or "")
        chunks = self.client.get(
            "/agentbus/streams/%s/chunks?%s"
            % (
                quote(stream_id, safe=""),
                urlencode({"agent_id": self.agent_id, "after_sequence": 0, "limit": 10}),
            )
        )
        payload: Any = None
        if isinstance(chunks, list) and chunks:
            payload = chunks[-1].get("payload") if isinstance(chunks[-1], dict) else None
        try:
            result = self._execute_repo_update(payload, stream_id)
        except Exception as exc:  # noqa: BLE001 - malformed control messages should report failure.
            result = self._repo_update_result(
                stream_id,
                "error",
                "repo update handler failed: %s" % exc,
                {},
            )
        self._observe_log(
            "worker.agentbus.repo_update.%s" % result["status"],
            level="info"
            if result["status"] in {"updated", "no_update", "skipped", "deferred"}
            else "error",
            detail=result,
        )
        return result

    def _execute_repo_update(self, payload: Any, stream_id: str) -> JsonDict:
        request: JsonDict = payload if isinstance(payload, dict) else {}
        if request.get("schema") not in {None, "", REPO_UPDATE_SCHEMA}:
            return self._repo_update_result(
                stream_id,
                "error",
                "unsupported repo update schema: %s" % request.get("schema"),
                request,
            )

        repo = self.self_update_repo.expanduser()
        requested_repo = str(request.get("repo_path") or "").strip()
        if requested_repo:
            try:
                if Path(requested_repo).expanduser().resolve() != repo.resolve():
                    return self._repo_update_result(
                        stream_id,
                        "error",
                        "repo_path does not match this listener's configured update repo",
                        request,
                        repo_path=str(repo),
                    )
            except OSError as exc:
                return self._repo_update_result(
                    stream_id,
                    "error",
                    "could not resolve repo_path: %s" % exc,
                    request,
                    repo_path=str(repo),
                )

        remote = str(request.get("remote") or "origin").strip()
        branch = str(request.get("branch") or "").strip()
        target_sha = str(request.get("target_sha") or "").strip()
        restart = bool(request.get("restart", True))
        try:
            restart_services = _normalize_restart_services(request.get("restart_services"))
        except ValueError as exc:
            return self._repo_update_result(
                stream_id,
                "error",
                str(exc),
                request,
                repo_path=str(repo),
            )
        if not _safe_git_ref(remote):
            return self._repo_update_result(
                stream_id,
                "error",
                "invalid git remote name",
                request,
                repo_path=str(repo),
            )
        if branch and not _safe_git_ref(branch):
            return self._repo_update_result(
                stream_id,
                "error",
                "invalid git branch/ref name",
                request,
                repo_path=str(repo),
            )
        if target_sha and not re.fullmatch(r"[0-9a-f]{40}", target_sha):
            return self._repo_update_result(
                stream_id,
                "error",
                "target_sha must be a lowercase 40-character commit SHA",
                request,
                repo_path=str(repo),
            )

        # Idle gate: never dirty the pinned environment under an active task.
        # The serial loop makes this implicit for loop-mode; the explicit
        # check covers service/job modes and defers the update until the
        # agent is between tasks (applied before its next claim).
        if not bool(request.get("force")):
            active_task_id = self._active_task_id()
            if active_task_id:
                self._stash_pending_repo_update(request, stream_id)
                return self._repo_update_result(
                    stream_id,
                    "deferred",
                    "agent is mid-task (%s); update pending until idle" % active_task_id,
                    request,
                    repo_path=str(repo),
                    active_task_id=active_task_id,
                )
        if not repo.exists():
            return self._repo_update_result(
                stream_id,
                "skipped",
                "self-update repo does not exist",
                request,
                repo_path=str(repo),
            )

        inside = _run_git(repo, ["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return self._repo_update_result(
                stream_id,
                "skipped",
                "self-update repo is not a git worktree",
                request,
                repo_path=str(repo),
                stderr=inside.stderr,
            )

        dirty = _run_git(repo, ["status", "--porcelain"])
        if dirty.returncode != 0:
            return self._repo_update_result(
                stream_id,
                "error",
                "could not inspect git status",
                request,
                repo_path=str(repo),
                stderr=dirty.stderr,
            )
        if dirty.stdout.strip():
            return self._repo_update_result(
                stream_id,
                "skipped",
                "self-update repo has local modifications",
                request,
                repo_path=str(repo),
            )

        before = _run_git(repo, ["rev-parse", "HEAD"])
        before_sha = before.stdout.strip() if before.returncode == 0 else ""
        if target_sha:
            if not branch:
                return self._repo_update_result(
                    stream_id, "error", "branch/ref is required with target_sha",
                    request, repo_path=str(repo), before_sha=before_sha,
                )
            fetched = _run_git(repo, ["fetch", "--no-tags", remote, branch])
            if fetched.returncode != 0:
                return self._repo_update_result(
                    stream_id, "error", "git fetch for exact source release failed",
                    request, repo_path=str(repo), before_sha=before_sha,
                    stdout=fetched.stdout, stderr=fetched.stderr,
                )
            target_exists = _run_git(repo, ["cat-file", "-e", "%s^{commit}" % target_sha])
            target_published = _run_git(repo, ["merge-base", "--is-ancestor", target_sha, "FETCH_HEAD"])
            can_fast_forward = _run_git(repo, ["merge-base", "--is-ancestor", before_sha, target_sha])
            if target_exists.returncode != 0 or target_published.returncode != 0:
                return self._repo_update_result(
                    stream_id, "error",
                    "target_sha is not present on the fetched canonical ref",
                    request, repo_path=str(repo), before_sha=before_sha,
                    target_sha=target_sha,
                )
            if can_fast_forward.returncode != 0:
                return self._repo_update_result(
                    stream_id, "error",
                    "exact source release is not a fast-forward from current HEAD",
                    request, repo_path=str(repo), before_sha=before_sha,
                    target_sha=target_sha,
                )
            managed_guard = self._managed_openshell_source_update_guard(
                current_sha=before_sha,
                target_sha=target_sha,
            )
            if managed_guard is not None:
                if managed_guard.get("fatal"):
                    self._write_repo_update_dispatch_blocker(managed_guard)
                self._observe_log(
                    "worker.openshell.source_update_blocked",
                    level="error",
                    subject_type="agent",
                    subject_id=self.agent_id,
                    detail=managed_guard,
                )
                return self._repo_update_result(
                    stream_id,
                    "error",
                    "source update rejected before checkout: %s"
                    % managed_guard["reason"],
                    request,
                    repo_path=str(repo),
                    before_sha=before_sha,
                    after_sha=before_sha,
                    target_sha=target_sha,
                    openshell_image_rebuild=managed_guard,
                )
            pulled = _run_git(repo, ["merge", "--ff-only", target_sha])
            failure_summary = "git merge --ff-only to target_sha failed"
        else:
            pull_args = ["pull", "--ff-only"]
            if branch:
                pull_args.extend([remote, branch])
            pulled = _run_git(repo, pull_args)
            failure_summary = "git pull --ff-only failed"
        if pulled.returncode != 0:
            return self._repo_update_result(
                stream_id,
                "error",
                failure_summary,
                request,
                repo_path=str(repo),
                before_sha=before_sha,
                target_sha=target_sha or None,
                stdout=pulled.stdout,
                stderr=pulled.stderr,
            )

        after = _run_git(repo, ["rev-parse", "HEAD"])
        after_sha = after.stdout.strip() if after.returncode == 0 else ""
        if target_sha and after_sha != target_sha:
            return self._repo_update_result(
                stream_id, "error", "checkout did not reach requested target_sha",
                request, repo_path=str(repo), before_sha=before_sha,
                after_sha=after_sha, target_sha=target_sha,
            )
        updated = bool(before_sha and after_sha and before_sha != after_sha)

        # Poisoned-checkout guard: a pulled tree that cannot even import
        # produces executors that die before any telemetry (traceless lease
        # expiries). Validate the new pin before restarting onto it or
        # baking it into a sandbox image; roll back to the prior SHA on
        # failure so the worker keeps running known-good code.
        if updated:
            self_test = self._repo_update_self_test(repo)
            if not self_test.get("ok"):
                rollback = _run_git(repo, ["reset", "--hard", before_sha])
                rollback_head = _run_git(repo, ["rev-parse", "HEAD"])
                rollback_dirty = _run_git(repo, ["status", "--porcelain"])
                rollback_ok = bool(
                    rollback.returncode == 0
                    and rollback_head.returncode == 0
                    and rollback_head.stdout.strip() == before_sha
                    and rollback_dirty.returncode == 0
                    and not rollback_dirty.stdout.strip()
                )
                if not rollback_ok:
                    blocker = {
                        "status": "self_test_rollback_failed",
                        "fatal": True,
                        "reason": (
                            "checkout rollback failed after post-update self-test failure"
                        ),
                        "before_sha": before_sha,
                        "attempted_after_sha": after_sha,
                        "rollback_ok": False,
                        "rollback_returncode": rollback.returncode,
                        "rollback_head": rollback_head.stdout.strip(),
                        "rollback_dirty": rollback_dirty.stdout.strip(),
                    }
                    self._write_repo_update_dispatch_blocker(blocker)
                    return self._repo_update_result(
                        stream_id,
                        "error",
                        "post-update self-test failed and checkout rollback could not "
                        "be verified; local dispatch is held",
                        request,
                        repo_path=str(repo),
                        before_sha=before_sha,
                        after_sha=rollback_head.stdout.strip(),
                        attempted_after_sha=after_sha,
                        self_test=self_test,
                        rollback_ok=False,
                        rollback_stderr=rollback.stderr,
                        rollback_head_stderr=rollback_head.stderr,
                        rollback_status_stderr=rollback_dirty.stderr,
                        restart_requested=False,
                    )
                return self._repo_update_result(
                    stream_id,
                    "rolled_back",
                    "post-update self-test failed; checkout rolled back to %s"
                    % before_sha[:12],
                    request,
                    repo_path=str(repo),
                    before_sha=before_sha,
                    after_sha=before_sha,
                    attempted_after_sha=after_sha,
                    self_test=self_test,
                    rollback_ok=True,
                    restart_requested=False,
                )

        summary = "repo already current"
        image_rebuild = self._maybe_rebuild_openshell_image_after_update(
            repo, before_sha, after_sha
        )
        image_rebuild_failed = bool(
            image_rebuild
            and image_rebuild.get("status")
            in {"drift", "failed", "managed_image_invalid", "managed_image_stale"}
        )
        managed_image_failure = bool(
            image_rebuild
            and image_rebuild.get("status")
            in {"managed_image_invalid", "managed_image_stale"}
        )
        if updated and managed_image_failure:
            attempted_after_sha = after_sha
            rollback = _run_git(repo, ["reset", "--hard", before_sha])
            rollback_head = _run_git(repo, ["rev-parse", "HEAD"])
            rollback_dirty = _run_git(repo, ["status", "--porcelain"])
            rollback_ok = bool(
                rollback.returncode == 0
                and rollback_head.returncode == 0
                and rollback_head.stdout.strip() == before_sha
                and rollback_dirty.returncode == 0
                and not rollback_dirty.stdout.strip()
            )
            fatal = bool(
                image_rebuild.get("status") == "managed_image_invalid"
                or not rollback_ok
            )
            if fatal:
                self._write_repo_update_dispatch_blocker(
                    {
                        **image_rebuild,
                        "fatal": True,
                        "reason": (
                            "managed runtime marker is invalid"
                            if image_rebuild.get("status") == "managed_image_invalid"
                            else "checkout rollback failed after managed runtime mismatch"
                        ),
                        "before_sha": before_sha,
                        "attempted_after_sha": attempted_after_sha,
                        "rollback_ok": rollback_ok,
                    }
                )
            if rollback_ok and not fatal:
                return self._repo_update_result(
                    stream_id,
                    "rolled_back",
                    "source update required a new published runtime digest; checkout rolled back",
                    request,
                    repo_path=str(repo),
                    before_sha=before_sha,
                    after_sha=before_sha,
                    attempted_after_sha=attempted_after_sha,
                    rollback_ok=True,
                    restart_requested=False,
                    openshell_image_rebuild=image_rebuild,
                )
            return self._repo_update_result(
                stream_id,
                "error",
                "source/runtime consistency could not be restored; local dispatch is held",
                request,
                repo_path=str(repo),
                before_sha=before_sha,
                after_sha=rollback_head.stdout.strip(),
                attempted_after_sha=attempted_after_sha,
                rollback_ok=rollback_ok,
                rollback_stderr=rollback.stderr,
                restart_requested=False,
                openshell_image_rebuild=image_rebuild,
            )
        if updated:
            summary = "repo updated"
            if restart and not image_rebuild_failed:
                summary += "; restart requested"
            if restart_services and not image_rebuild_failed:
                summary += "; service restart requested"
        if image_rebuild:
            summary += "; openshell image %s" % image_rebuild.get("status")
        if image_rebuild_failed:
            summary += "; deployment blocked until image rebuild succeeds"
        return self._repo_update_result(
            stream_id,
            "error" if image_rebuild_failed else "updated" if updated else "no_update",
            summary,
            request,
            repo_path=str(repo),
            before_sha=before_sha,
            after_sha=after_sha,
            stdout=pulled.stdout,
            stderr=pulled.stderr,
            restart_requested=updated and restart and not image_rebuild_failed,
            service_restart_requested=(
                updated and bool(restart_services) and not image_rebuild_failed
            ),
            restart_services=(
                restart_services
                if updated and restart_services and not image_rebuild_failed
                else []
            ),
            openshell_image_rebuild=image_rebuild,
        )

    def _active_task_id(self) -> str:
        """The task this agent is currently working, or '' when idle.

        Fail-open to '' (idle): if the hub is unreachable the caller is the
        control loop, which only runs between tasks in loop-mode anyway, and
        a wrongly-applied update is recoverable while a wedged one is not.
        """
        try:
            agent = self.client.get("/agents/%s" % quote(self.agent_id, safe=""))
        except Exception:  # noqa: BLE001 - status probing must not break control
            return ""
        if not isinstance(agent, dict):
            return ""
        return str(agent.get("current_task_id") or "")

    def _stash_pending_repo_update(self, request: JsonDict, stream_id: str) -> None:
        try:
            self.pending_repo_update_path.parent.mkdir(parents=True, exist_ok=True)
            self.pending_repo_update_path.write_text(
                json.dumps(
                    {"request": request, "stream_id": stream_id},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - a lost stash only delays the
            # update until the next fleet refresh publishes again.
            self._observe_log(
                "worker.agentbus.repo_update.stash_failed",
                level="warning",
                detail={"path": str(self.pending_repo_update_path), "error": str(exc)},
            )

    def _load_pending_repo_update(self) -> Optional[JsonDict]:
        try:
            raw = json.loads(self.pending_repo_update_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return raw if isinstance(raw, dict) and isinstance(raw.get("request"), dict) else None

    def _clear_pending_repo_update(self) -> None:
        try:
            self.pending_repo_update_path.unlink()
        except OSError:
            pass

    def apply_pending_repo_update_if_idle(self) -> Optional[JsonDict]:
        """Apply a stashed repo update now that the agent may be idle.

        Runs in run_once BEFORE claim-next, so new work never starts on a
        stale pin while an update is pending. Returns the update result when
        an application was attempted, else None.
        """
        pending = self._load_pending_repo_update()
        if pending is None:
            return None
        if self._active_task_id():
            return None  # still busy; keep the stash for the next iteration
        request = dict(pending.get("request") or {})
        request["force"] = True  # the idle check just passed; don't re-defer
        stream_id = str(pending.get("stream_id") or "")
        try:
            result = self._execute_repo_update(request, stream_id)
        except Exception as exc:  # noqa: BLE001 - a broken apply must clear
            # the stash rather than wedge every future iteration.
            result = self._repo_update_result(
                stream_id, "error", "pending repo update failed: %s" % exc, request
            )
        self._clear_pending_repo_update()
        self._observe_log(
            "worker.agentbus.repo_update.%s" % result["status"],
            level="info"
            if result["status"] in {"updated", "no_update", "skipped", "deferred"}
            else "error",
            detail={**result, "applied_from": "pending_stash"},
        )
        self._run_repo_update_service_restarts(result)
        return result

    def _repo_update_dispatch_blocker_path(self) -> Path:
        configured = str(
            os.environ.get("MAC_REPO_UPDATE_DISPATCH_BLOCKER_FILE") or ""
        ).strip()
        if configured:
            return Path(configured).expanduser()
        mac_home = mac_paths.mac_home()
        return mac_home / "repo-update-dispatch-blocked.json"

    def _write_repo_update_dispatch_blocker(
        self, detail: Mapping[str, Any]
    ) -> None:
        payload = {
            "schema": "mac.worker.source_runtime_dispatch_block.v1",
            "agent_id": self.agent_id,
            "reason": str(
                detail.get("reason")
                or "source/runtime consistency requires a fleet redeploy"
            )[:1000],
            "detail": dict(detail),
            "observed_at": utcnow(),
        }
        self._in_memory_repo_update_dispatch_blocker = payload
        path = self._repo_update_dispatch_blocker_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name("%s.tmp.%d" % (path.name, os.getpid()))
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            os.replace(temporary, path)
        except OSError as exc:
            self._observe_log(
                "worker.dispatch_block.persist_failed",
                level="error",
                subject_type="agent",
                subject_id=self.agent_id,
                detail={"path": str(path), "error": str(exc)},
            )

    def _local_repo_update_dispatch_blocker(self) -> Optional[JsonDict]:
        in_memory = getattr(
            self, "_in_memory_repo_update_dispatch_blocker", None
        )
        if isinstance(in_memory, dict):
            return dict(in_memory)
        path = self._repo_update_dispatch_blocker_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "schema": "mac.worker.source_runtime_dispatch_block.v1",
                "reason": "source/runtime dispatch blocker is unreadable",
                "path": str(path),
                "error_type": type(exc).__name__,
            }
        if not isinstance(payload, dict) or payload.get("schema") != (
            "mac.worker.source_runtime_dispatch_block.v1"
        ):
            return {
                "schema": "mac.worker.source_runtime_dispatch_block.v1",
                "reason": "source/runtime dispatch blocker is malformed",
                "path": str(path),
            }
        return payload

    def _managed_openshell_source_update_guard(
        self, *, current_sha: str, target_sha: str
    ) -> Optional[JsonDict]:
        mac_home = mac_paths.mac_home()
        runtime_ref_file = Path(
            os.environ.get("MAC_OPENSHELL_RUNTIME_IMAGE_REF_FILE")
            or mac_home / "openshell" / "runtime-image-ref"
        ).expanduser()
        if not runtime_ref_file.exists():
            return None
        source_marker = Path(
            os.environ.get("MAC_OPENSHELL_IMAGE_SOURCE_SHA_FILE")
            or mac_home / "openshell" / "image-source-sha"
        ).expanduser()
        try:
            runtime_ref = runtime_ref_file.read_text(encoding="utf-8").strip()
            marked_sha = source_marker.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return {
                "status": "managed_image_invalid",
                "fatal": True,
                "reason": "digest-managed runtime markers are unreadable",
                "error_type": type(exc).__name__,
                "runtime_ref_file": str(runtime_ref_file),
                "source_marker": str(source_marker),
            }
        if not re.fullmatch(
            r"ghcr\.io/jordanhubbard/mac-openshell-runtime@sha256:[0-9a-f]{64}",
            runtime_ref,
        ) or not re.fullmatch(r"[0-9a-f]{40}", marked_sha):
            return {
                "status": "managed_image_invalid",
                "fatal": True,
                "reason": "digest-managed runtime markers are malformed",
                "runtime_ref_file": str(runtime_ref_file),
                "source_marker": str(source_marker),
            }
        if marked_sha != current_sha:
            return {
                "status": "managed_image_invalid",
                "fatal": True,
                "reason": "current source does not match the digest-managed runtime revision",
                "runtime_image_ref": runtime_ref,
                "marked_sha": marked_sha,
                "current_sha": current_sha,
                "target_sha": target_sha,
            }
        if target_sha != marked_sha:
            return {
                "status": "managed_image_stale",
                "fatal": False,
                "reason": "target source requires a matching published runtime digest",
                "runtime_image_ref": runtime_ref,
                "marked_sha": marked_sha,
                "current_sha": current_sha,
                "target_sha": target_sha,
            }
        return None

    def _repo_update_self_test(self, repo: Path) -> JsonDict:
        """Prove the updated tree can at least import before adopting it."""
        if not _env_truthy(os.environ.get("MAC_REPO_UPDATE_SELF_TEST", "1")):
            return {"ok": True, "skipped": "disabled"}
        python = (
            os.environ.get("MAC_REPO_UPDATE_SELF_TEST_PYTHON") or sys.executable
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(repo / "src")] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
        )
        # A worker normally runs as a control-plane client.  Importing
        # ``mac.api`` constructs its module-level application, and client role
        # correctly refuses to own a database.  The source-adoption probe is
        # not a live control-plane start: give it an isolated in-memory hub so
        # it can validate the API import without touching the worker's live
        # database or failing solely because of its production role.
        env["MAC_CONTROL_PLANE_ROLE"] = "hub"
        env["MAC_DB"] = ":memory:"
        env.pop("MAC_DATABASE_URL", None)
        env["MAC_BIND_HOST"] = "127.0.0.1"
        try:
            completed = subprocess.run(
                [python, "-c", "import mac.services, mac.worker, mac.api"],
                cwd=str(repo),
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": str(exc)[:500]}
        if completed.returncode == 0:
            return {"ok": True}
        return {
            "ok": False,
            "returncode": completed.returncode,
            "stderr": (completed.stderr or "")[-1000:],
        }

    def _maybe_rebuild_openshell_image_after_update(
        self, repo: Path, before_sha: str, after_sha: str
    ) -> Optional[JsonDict]:
        """Rebuild the OpenShell sandbox image when its source SHA is stale.

        The image embeds MAC's source tree, not just its Containerfile. A durable
        SHA marker therefore tracks the complete build input and also makes an
        unchanged-source refresh retry a previously failed build. Failures are
        reported to the caller, which blocks the deployment restart.
        """
        if not _env_truthy(os.environ.get("MAC_OPENSHELL_SANDBOX")):
            return None  # node doesn't run the sandbox -> no image to keep current
        if not _env_truthy(os.environ.get("MAC_OPENSHELL_REBUILD_ON_SOURCE_UPDATE", "1")):
            return None
        marker = Path(
            os.environ.get("MAC_OPENSHELL_IMAGE_SOURCE_SHA_FILE")
            or mac_paths.mac_home()
            / "openshell"
            / "image-source-sha"
        ).expanduser()
        try:
            marked_sha = marker.read_text(encoding="utf-8").strip()
        except OSError:
            marked_sha = ""
        if after_sha and marked_sha == after_sha:
            return None
        managed_ref_file = Path(
            os.environ.get("MAC_OPENSHELL_RUNTIME_IMAGE_REF_FILE")
            or mac_paths.mac_home()
            / "openshell"
            / "runtime-image-ref"
        ).expanduser()
        try:
            managed_ref = managed_ref_file.read_text(encoding="utf-8").strip()
        except OSError:
            managed_ref = ""
        if managed_ref_file.exists():
            if not re.fullmatch(
                r"ghcr\.io/jordanhubbard/mac-openshell-runtime@sha256:[0-9a-f]{64}",
                managed_ref,
            ):
                self._observe_log(
                    "worker.openshell.managed_image_invalid",
                    level="error",
                    subject_type="agent",
                    subject_id=self.agent_id,
                    detail={
                        "reason": "digest-managed runtime marker is invalid",
                        "marker": str(managed_ref_file),
                        "before_sha": before_sha,
                        "after_sha": after_sha,
                    },
                )
                return {"status": "managed_image_invalid", "marker": str(managed_ref_file)}
            # A published runtime is bound to one exact source revision. Source
            # adoption must wait for a fleet deploy carrying the corresponding
            # new digest; rebuilding this tag locally would silently destroy the
            # single-image identity required by synchronized execution.
            self._observe_log(
                "worker.openshell.managed_image_stale",
                level="error",
                subject_type="agent",
                subject_id=self.agent_id,
                detail={
                    "reason": "source update requires a matching published runtime image",
                    "runtime_image_ref": managed_ref,
                    "marked_sha": marked_sha,
                    "before_sha": before_sha,
                    "after_sha": after_sha,
                },
            )
            return {
                "status": "managed_image_stale",
                "runtime_image_ref": managed_ref,
                "marked_sha": marked_sha,
                "after_sha": after_sha,
            }
        containerfile = repo / _OPENSHELL_CONTAINERFILE_RELPATH
        image_builder = repo / "deploy/openshell/build-runtime-image.sh"
        tag = (os.environ.get("MAC_OPENSHELL_IMAGE_TAG") or "").strip() or "localhost/mac-hermes:net"
        docker = _resolve_openshell_docker_bin()
        if not containerfile.is_file() or not image_builder.is_file() or not docker:
            self._observe_log(
                "worker.openshell.image_drift",
                level="error",
                subject_type="agent",
                subject_id=self.agent_id,
                detail={
                    "reason": "sandbox Containerfile changed but image could not be rebuilt",
                    "containerfile_present": containerfile.is_file(),
                    "image_builder_present": image_builder.is_file(),
                    "docker_present": bool(docker),
                    "before_sha": before_sha,
                    "after_sha": after_sha,
                    "tag": tag,
                },
            )
            return {"status": "drift", "tag": tag}
        self._observe_log(
            "worker.openshell.image_rebuild_started",
            subject_type="agent",
            subject_id=self.agent_id,
            detail={"tag": tag, "before_sha": before_sha, "after_sha": after_sha},
        )
        try:
            build_env = os.environ.copy()
            build_env.update(
                {
                    "MAC_SRC": str(repo),
                    "OSH_DOCKER_BIN": docker,
                    "OSH_IMAGE_TAG": tag,
                    "MAC_IMAGE_SOURCE_SHA": after_sha,
                    "MAC_IMAGE_SOURCE_SHA_FILE": str(marker),
                }
            )
            build = subprocess.run(
                ["/bin/bash", str(image_builder)],
                cwd=str(repo),
                capture_output=True,
                text=True,
                check=False,
                timeout=1800,
                env=build_env,
            )
        except Exception as exc:  # noqa: BLE001 - rebuild is best-effort.
            self._observe_log(
                "worker.openshell.image_rebuild_failed",
                level="error",
                subject_type="agent",
                subject_id=self.agent_id,
                detail={"tag": tag, "error": str(exc)},
            )
            return {"status": "failed", "tag": tag, "error": str(exc)}
        if build.returncode != 0:
            self._observe_log(
                "worker.openshell.image_rebuild_failed",
                level="error",
                subject_type="agent",
                subject_id=self.agent_id,
                detail={
                    "tag": tag,
                    "returncode": build.returncode,
                    "stderr": _truncate_process_text(build.stderr or build.stdout),
                },
            )
            return {"status": "failed", "tag": tag, "returncode": build.returncode}
        # Mirror docker -> podman when present: some OpenShell deployments read the
        # runtime image from podman's store (see bootstrap-openshell.sh).
        podman = shutil.which("podman")
        if podman:
            try:
                save = subprocess.Popen([docker, "image", "save", tag], stdout=subprocess.PIPE)
                subprocess.run([podman, "load"], stdin=save.stdout, capture_output=True, text=True, check=False, timeout=900)
                if save.stdout:
                    save.stdout.close()
                save.wait(timeout=900)
            except Exception:  # noqa: BLE001 - mirror is best-effort.
                pass
        self._observe_log(
            "worker.openshell.image_rebuilt",
            subject_type="agent",
            subject_id=self.agent_id,
            detail={"tag": tag, "after_sha": after_sha},
        )
        return {"status": "rebuilt", "tag": tag}

    def _run_repo_update_service_restarts(self, result: JsonDict) -> Optional[JsonDict]:
        if not result.get("service_restart_requested"):
            return None
        services = _normalize_restart_services(result.get("restart_services"))
        if not services:
            return None
        service_results = [_restart_systemd_service(service) for service in services]
        failures = [item for item in service_results if item.get("status") == "error"]
        status = "service_restart_error" if failures else "service_restarted"
        summary = (
            "one or more service restarts failed"
            if failures
            else "requested services restarted or skipped where absent"
        )
        return self._repo_update_result(
            str(result.get("stream_id") or ""),
            status,
            summary,
            {"request_id": result.get("request_id")},
            repo_path=result.get("repo_path"),
            after_sha=result.get("after_sha"),
            service_restarts=service_results,
        )

    def _repo_update_result(
        self,
        stream_id: str,
        status: str,
        summary: str,
        request: JsonDict,
        **extra: Any,
    ) -> JsonDict:
        result: JsonDict = {
            "schema": REPO_UPDATE_RESULT_SCHEMA,
            "status": status,
            "summary": summary,
            "agent_id": self.agent_id,
            "stream_id": stream_id,
            "request_id": request.get("request_id"),
            "restart_requested": bool(extra.pop("restart_requested", False)),
        }
        for key in ("target_sha", "desired_generation", "release_id"):
            if request.get(key) is not None:
                result[key] = request.get(key)
        for key, value in extra.items():
            if isinstance(value, str):
                result[key] = value[:4000]
            else:
                result[key] = value
        return result

    def _publish_repo_update_result(
        self,
        stream: JsonDict,
        result: JsonDict,
        *,
        attempts: int = 5,
        delay_seconds: float = 0.5,
    ) -> None:
        sender = str(stream.get("sender_agent_id") or "")
        if not sender:
            return
        last_error = ""
        attempts = _bounded_int(attempts, 1, 120, 5)
        delay_seconds = _bounded_float(delay_seconds, 0.1, 10.0, 0.5)
        for attempt in range(attempts):
            try:
                self.client.post(
                    "/agentbus",
                    {
                        "sender_agent_id": self.agent_id,
                        "recipient_agent_id": sender,
                        "content_type": REPO_UPDATE_RESULT_CONTENT_TYPE,
                        "topic": REPO_UPDATE_RESULT_TOPIC,
                        "payload": result,
                    },
                )
                return
            except Exception as exc:  # noqa: BLE001 - result publishing is best-effort.
                last_error = str(exc)
                if attempt < attempts - 1:
                    time.sleep(delay_seconds)
        self._observe_log(
            "worker.agentbus.repo_update_result_failed",
            level="warning",
            detail={"stream_id": stream.get("id"), "error": last_error},
        )

    def _load_agentbus_control_state(self) -> List[str]:
        try:
            loaded = json.loads(self.agentbus_control_state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            return []
        values = loaded.get("processed_stream_ids") if isinstance(loaded, dict) else []
        if not isinstance(values, list):
            return []
        return [str(value) for value in values if str(value)]

    def _save_agentbus_control_state(self, processed_stream_ids: List[str]) -> None:
        try:
            self.agentbus_control_state_path.parent.mkdir(parents=True, exist_ok=True)
            deduped = list(dict.fromkeys(processed_stream_ids))[-500:]
            self.agentbus_control_state_path.write_text(
                json.dumps({"processed_stream_ids": deduped}, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - state loss should not break task polling.
            self._observe_log(
                "worker.agentbus.control_state_write_failed",
                level="warning",
                detail={"path": str(self.agentbus_control_state_path), "error": str(exc)},
            )

    def _claim_payload(self, dry_run: bool) -> JsonDict:
        return {
            "lease_seconds": self.lease_seconds,
            "allowed_projects": self.allowed_projects,
            "required_metadata": self.required_metadata,
            "claim_only_canary_tasks": self.claim_only_canary_tasks,
            "dry_run": dry_run,
        }

    def _policy_payload(self) -> JsonDict:
        return {
            "allowed_projects": self.allowed_projects,
            "required_metadata": self.required_metadata,
            "claim_only_canary_tasks": self.claim_only_canary_tasks,
        }

    def _observe_policy_once(self) -> None:
        if self._declared_policy:
            return
        self._declared_policy = True
        self._observe_log(
            "worker.routing.policy",
            detail={"agent_id": self.agent_id, "policy": self._policy_payload()},
        )

    def _is_onboarding_task(self, task: JsonDict) -> bool:
        """Return True when this task is an onboarding task.

        Onboarding tasks are identified by origin.onboarding == True OR by
        the absence of a repository_contract with a schema field.
        """
        metadata = task.get("metadata") if isinstance(task, dict) else None
        if not isinstance(metadata, dict):
            return False
        origin = metadata.get("origin")
        if not isinstance(origin, dict):
            return False
        if origin.get("onboarding") is True:
            return True
        contract = origin.get("repository_contract")
        if not isinstance(contract, dict) or not contract.get("schema"):
            return True
        return False

    def _prepare_task_workspace(self, task: JsonDict, lease: JsonDict) -> Path:
        task_dir = self.workspace / _safe_path_component(task["id"])
        task_dir.mkdir(parents=True, exist_ok=True)
        repository_context = self._prepare_repository_worktree(task, lease, task_dir)
        if repository_context is not None:
            metadata = task.get("metadata") if isinstance(task, dict) else None
            if metadata_declares_read_only_report_repository(metadata):
                repository_contract = _current_repository_contract(task)
                repository_context = dict(repository_context)
                repository_context.update(
                    {
                        "repository_access_mode": REPORT_REPOSITORY_READ_ONLY_MODE,
                        "repository_access_schema": REPORT_REPOSITORY_ACCESS_SCHEMA,
                        "repository_contract": repository_contract,
                    }
                )
                # The executor needs only the disposable inspection clone. Do
                # not expose the registered source checkout as an alternate
                # writable path in task.json or MAC_TASK_REPO_SOURCE.
                if isinstance(metadata, dict):
                    for container_key in ("origin", "execution_contract"):
                        container = metadata.get(container_key)
                        if isinstance(container, dict):
                            container.pop("repository_path", None)
                            nested_contract = container.get("repository_contract")
                            if isinstance(nested_contract, dict):
                                nested_contract.pop("repository_path", None)
            metadata = task.setdefault("metadata", {})
            if isinstance(metadata, dict):
                runtime = metadata.setdefault("runtime", {})
                if isinstance(runtime, dict):
                    runtime.update(repository_context)
            (task_dir / "repository-worktree.json").write_text(
                json.dumps(repository_context, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        if repository_context is not None and self._is_onboarding_task(task):
            worktree_dir = Path(repository_context.get("repository_worktree") or "")
            if worktree_dir.is_dir():
                try:
                    env_contract = derive_environment_contract(worktree_dir)
                    env_contract = validate_environment_contract(env_contract)
                    (task_dir / "environment-contract.json").write_text(
                        json.dumps(env_contract, indent=2, sort_keys=True),
                        encoding="utf-8",
                    )
                    metadata = task.setdefault("metadata", {})
                    if isinstance(metadata, dict):
                        runtime = metadata.setdefault("runtime", {})
                        if isinstance(runtime, dict):
                            runtime["environment_contract"] = env_contract
                    self._observe_log(
                        "worker.environment_contract.derived",
                        subject_type="task",
                        subject_id=str(task.get("id") or ""),
                        detail={"status": env_contract.get("preflight", {}).get("status", "unknown")},
                    )
                except Exception as _env_exc:
                    self._observe_log(
                        "worker.environment_contract.derivation_failed",
                        level="warning",
                        subject_type="task",
                        subject_id=str(task.get("id") or ""),
                        detail={"error": str(_env_exc)},
                    )
        (task_dir / "task.json").write_text(
            json.dumps({"task": task, "lease": lease}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return task_dir

    def _review_task_payload(self, task_dir: Path) -> JsonDict:
        loaded = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        task = loaded.get("task", loaded)
        return task if isinstance(task, dict) else loaded

    def _record_execution(
        self,
        task_id: str,
        task_dir: Path,
        execution: WorkerExecution,
        *,
        lease_id: str,
        attempt_state: Optional[JsonDict] = None,
    ) -> JsonDict:
        _write_host_control_text(task_dir / "stdout.txt", execution.stdout, task_dir)
        _write_host_control_text(task_dir / "stderr.txt", execution.stderr, task_dir)
        if execution.succeeded:
            self._write_missing_repository_evidence_manifest(
                task_id,
                task_dir,
                execution,
                attempt_state=attempt_state,
            )
        metadata = self._execution_metadata(task_dir, execution)
        result_path = task_dir / "worker-result.json"
        _write_host_control_text(
            result_path,
            json.dumps(
                {
                    "returncode": execution.returncode,
                    "summary": execution.summary,
                    "metadata": metadata,
                },
                indent=2,
                sort_keys=True,
            ),
            task_dir,
        )
        artifacts = _durable_evidence_artifacts(task_dir, result_path)
        evidence_result = self.client.post(
            "/tasks/%s/evidence" % quote(task_id, safe=""),
            {
                "kind": "log",
                "uri": result_path.resolve().as_uri(),
                "summary": execution.summary,
                "created_by": self.agent_id,
                "lease_id": lease_id,
                "artifacts": artifacts,
                "metadata": {
                    "returncode": execution.returncode,
                    "stdout": (task_dir / "stdout.txt").resolve().as_uri(),
                    "stderr": (task_dir / "stderr.txt").resolve().as_uri(),
                    **metadata,
                },
            },
        )
        # Glanceable per-task narrative (additive to the evidence/logs above):
        # what the worker actually did, in the agent's own closing words plus the
        # build/test/push outcome. Best-effort -- never disturbs the evidence post.
        self._post_task_activity(
            task_id,
            "worker",
            self._execution_activity_summary(task_dir, execution),
            lease_id=lease_id,
        )
        env_summary = self._execution_env_summary(task_dir)
        if env_summary:
            self._post_task_activity(task_id, "env", env_summary, lease_id=lease_id)
        return evidence_result

    def _execution_env_summary(self, task_dir: Path) -> str:
        """Note environment changes needed to build/test the task (toolchain
        bootstrap, installed/missing deps) for the per-task narrative. Returns ""
        when nothing notable changed. Best-effort."""
        try:
            manifest = ensure_json_object(
                json.loads((task_dir / "mac-evidence.json").read_text(encoding="utf-8"))
            )
        except Exception:  # noqa: BLE001 - env note is best-effort
            return ""
        parts: List[str] = []
        bootstrap = manifest.get("bootstrap")
        if isinstance(bootstrap, dict):
            cmd = str(bootstrap.get("command") or "").strip()
            if cmd:
                rc = bootstrap.get("returncode")
                parts.append("bootstrap: %s%s" % (cmd, "" if rc is None else " (rc %s)" % rc))
        for test in manifest.get("tests") or []:
            if not isinstance(test, dict):
                continue
            delta = test.get("environment_delta")
            if not isinstance(delta, dict):
                continue
            for label in ("installed", "added"):
                vals = delta.get(label)
                if isinstance(vals, list) and vals:
                    parts.append("%s: %s" % (label, ", ".join(str(v) for v in vals[:8])))
            for label in ("missing", "missing_commands"):
                vals = delta.get(label)
                if isinstance(vals, list) and vals:
                    parts.append("missing: %s" % ", ".join(str(v) for v in vals[:8]))
        return "; ".join(parts[:4])

    def _post_task_activity(
        self,
        task_id: str,
        phase: str,
        summary: str,
        *,
        lease_id: Optional[str] = None,
    ) -> None:
        """Append a short, human-readable activity entry to the task's narrative
        (`mac task summary`). Additive to the durable evidence/logs; a failure
        here must never disturb task execution."""
        summary = (summary or "").strip()
        if not summary:
            return
        try:
            body = {"phase": phase, "actor": self.agent_id, "summary": summary}
            if lease_id:
                body["lease_id"] = lease_id
            self.client.post(
                "/tasks/%s/activity" % quote(task_id, safe=""),
                body,
            )
        except Exception:  # noqa: BLE001 - narrative is best-effort
            pass

    def _execution_activity_summary(self, task_dir: Path, execution: WorkerExecution) -> str:
        """A few-line 'what the worker did': the coding agent's closing words
        (stdout tail) plus the build/test/push outcome from the finalized
        evidence manifest."""
        recap = _extract_marked_summary(execution.stdout) or "\n".join(
            _prose_tail(execution.stdout, 4)
        )
        try:
            manifest = ensure_json_object(
                json.loads((task_dir / "mac-evidence.json").read_text(encoding="utf-8"))
            )
        except Exception:  # noqa: BLE001 - facts are best-effort enrichment
            manifest = {}
        repo = ensure_json_object(manifest.get("repo"))
        facts: List[str] = []
        files_changed = repo.get("files_changed")
        if isinstance(files_changed, list) and files_changed:
            facts.append("%d file(s) changed" % len(files_changed))
        tests = manifest.get("tests")
        if isinstance(tests, list):
            for test in tests:
                if isinstance(test, dict) and test.get("command"):
                    facts.append(
                        "%s %s"
                        % (test.get("command"), "passed" if test.get("returncode") == 0 else "FAILED")
                    )
        if repo.get("pushed") is True:
            facts.append("branch pushed")
        problems = manifest.get("problems")
        if isinstance(problems, list) and problems:
            facts.append("problems: " + "; ".join(str(p) for p in problems[:2]))
        parts: List[str] = []
        if recap:
            parts.append(recap)
        if facts:
            parts.append("— " + "; ".join(facts[:5]))
        return "\n".join(parts).strip() or (execution.summary or "").strip()

    def _write_missing_repository_evidence_manifest(
        self,
        task_id: str,
        task_dir: Path,
        execution: WorkerExecution,
        attempt_state: Optional[JsonDict] = None,
    ) -> bool:
        task = _task_payload_from_workspace(task_dir)
        serialized_context = _load_repository_context(task_dir)
        trusted_context = _trusted_read_only_repository_context(task)
        if trusted_context:
            return self._write_read_only_report_evidence_manifest(
                task_id,
                task_dir,
                execution,
                trusted_context,
                context_problems=_read_only_repository_context_drift_problems(
                    trusted_context, serialized_context
                ),
            )
        context = serialized_context
        if not context:
            return False
        raw_package_assignment = ensure_json_object(task.get("metadata")).get(
            "work_package_assignment"
        )
        package_assignment: JsonDict = {}
        if raw_package_assignment is not None:
            try:
                package_assignment = _work_package_assignment_projection(task, context)
            except ValueError:
                # Force the deterministic finalizer below; it will preserve the
                # projection error in an invalid evidence manifest rather than
                # silently trusting an agent-authored mutable branch.
                package_assignment = {}
        # If the coding agent pushed this task's branch from a throwaway
        # in-sandbox clone (it does that when the uploaded worktree's gitlink is
        # unusable inside the sandbox), the HOST worktree holds the same edits
        # UNCOMMITTED while the work already exists on the remote. Reset the host
        # worktree onto the pushed tip so independent host-side verification sees
        # a clean, already-pushed HEAD instead of false-blocking finished work.
        # No-op unless the worktree is dirty AND its tracked content matches the
        # pushed branch exactly; a genuinely-dirty/unpushed worktree is left
        # alone so the dirty-worktree contract still blocks it.
        adopted = False
        worktree_raw = str(context.get("repository_worktree") or "").strip()
        worktree = Path(worktree_raw).expanduser() if worktree_raw else None
        if worktree is not None and worktree.exists() and _repository_worktree_is_dirty(worktree):
            adopted = self._adopt_pushed_branch_if_worktree_matches(
                task,
                worktree,
                str(
                    package_assignment.get("attempt_ref")
                    or context.get("repository_branch")
                    or ""
                ).strip(),
                context,
            )
        # Rescue "agent did the work but forgot to commit": when the worktree is
        # DIRTY (untracked new files AND/OR modified tracked files left
        # uncommitted), re-finalize so ALL changes are committed, the contract test
        # is re-run on the result, and the branch is pushed ONLY if that test
        # passes. This covers an agent that edited tracked files and passed
        # `make test` but slipped on `git commit` (mac task_94aa4ed5) — previously
        # only untracked files were rescued, so a tracked-but-uncommitted edit was
        # blocked despite passing tests. Safe because the finalizer refuses to push
        # when the contract test does not pass (it records the failure), so
        # unverified/incomplete dirt is never silently accepted.
        is_dirty = (
            worktree is not None
            and worktree.exists()
            and _repository_worktree_is_dirty(worktree)
        )
        manifest_path = task_dir / "mac-evidence.json"
        # When we adopted an already-pushed branch, the agent-authored manifest
        # describes its throwaway in-sandbox clone — notably its tests may be in a
        # non-canonical shape the strict validator treats as missing. Re-finalize
        # from the adopted host worktree to re-run the contract test and emit a
        # valid manifest. Otherwise keep an existing agent manifest untouched.
        if manifest_path.exists() and not adopted and not is_dirty:
            if raw_package_assignment is None:
                return False
            if package_assignment and worktree is not None:
                try:
                    existing = ensure_json_object(
                        json.loads(manifest_path.read_text(encoding="utf-8"))
                    )
                except (OSError, json.JSONDecodeError):
                    existing = {}
                repo = ensure_json_object(existing.get("repo"))
                repo = {
                    **repo,
                    "remote_ref": package_assignment["attempt_ref"],
                    "head_sha": _git_stdout(worktree, ["rev-parse", "HEAD"]),
                    "remote_url": _repository_publication_remote(task, context),
                }
                if (
                    existing.get("evidence_type") == "repo_change"
                    and repo["head_sha"]
                    and _repository_context_head_is_pushed(worktree, repo)
                ):
                    return False

        try:
            manifest = self._finalize_missing_repository_evidence_manifest(
                task_id,
                task_dir,
                execution,
                context,
                attempt_state=attempt_state,
            )
        except Exception as exc:  # noqa: BLE001 - evidence must record finalizer failures.
            manifest = {
                "schema": VERIFICATION_SCHEMA,
                "status": "invalid",
                "evidence_type": "repo_change",
                "summary": "repository evidence finalizer failed",
                "problems": ["repository evidence finalizer failed: %s" % exc],
                "repo": _repository_context_repo_snapshot(context),
            }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._observe_log(
            "worker.repository.missing_manifest_finalized",
            level="info" if str(manifest.get("status") or "") == "complete" else "error",
            subject_type="task",
            subject_id=task_id,
            detail={
                "manifest_path": str(manifest_path),
                "status": manifest.get("status"),
                "evidence_type": manifest.get("evidence_type"),
                "problems": manifest.get("problems") or [],
            },
        )
        return True

    def _write_read_only_report_evidence_manifest(
        self,
        task_id: str,
        task_dir: Path,
        execution: WorkerExecution,
        context: JsonDict,
        *,
        context_problems: Optional[List[str]] = None,
    ) -> bool:
        """Ensure repository-inspection reports stay diff-free operator results.

        This deliberately does not call the repository finalizer.  The
        inspection checkout has no publication remote, and any local mutation
        turns the result into fail-closed operator evidence for the submission
        gate to reject.
        """

        task = _task_payload_from_workspace(task_dir)
        worktree_raw = str(context.get("repository_worktree") or "").strip()
        worktree = Path(worktree_raw).expanduser() if worktree_raw else Path()
        problems = list(context_problems or [])
        problems.extend(_read_only_repository_problems(worktree, context))
        if execution.returncode != 0:
            problems.append(
                "read-only repository report executor failed with returncode %d"
                % execution.returncode
            )
        manifest_path = task_dir / "mac-evidence.json"
        try:
            existing = ensure_json_object(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            existing = {}
        existing_type = str(existing.get("evidence_type") or "").strip().lower()
        if existing and existing_type != "operator_result":
            problems.append(
                "read-only repository report evidence_type must be operator_result"
            )
        result_text = (execution.stdout or execution.stderr or execution.summary or "").strip()
        summary = str(existing.get("summary") or execution.summary or "").strip()
        if not summary:
            summary = next(
                (line.strip() for line in result_text.splitlines() if line.strip()),
                "read-only repository analysis completed",
            )
        manifest: JsonDict = {
            **existing,
            "schema": VERIFICATION_SCHEMA,
            "status": "invalid" if problems else "complete",
            "evidence_type": "operator_result",
            "summary": summary[:1000],
            "result": str(existing.get("result") or result_text)[-20000:],
            "repository_access": _read_only_repository_access_evidence(context),
        }
        manifest, trusted_test_problems = _attach_trusted_read_only_report_test(
            manifest, task_dir, task
        )
        problems.extend(trusted_test_problems)
        manifest["status"] = "invalid" if problems else "complete"
        # A read-only report is never a publication anchor, even when an agent
        # supplied a repo-shaped lookalike in its mutable manifest.
        manifest.pop("repo", None)
        if problems:
            manifest["problems"] = list(dict.fromkeys(problems))
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._observe_log(
            "worker.repository.read_only_report_validated",
            level="error" if problems else "info",
            subject_type="task",
            subject_id=task_id,
            detail={"status": manifest["status"], "problems": problems},
        )
        return True

    def _finalize_missing_repository_evidence_manifest(
        self,
        task_id: str,
        task_dir: Path,
        execution: WorkerExecution,
        context: JsonDict,
        attempt_state: Optional[JsonDict] = None,
    ) -> JsonDict:
        task = _task_payload_from_workspace(task_dir)
        worktree = Path(str(context.get("repository_worktree") or "")).expanduser()
        if not worktree.exists():
            return {
                "schema": VERIFICATION_SCHEMA,
                "status": "invalid",
                "evidence_type": "repo_change",
                "summary": "repository worktree missing",
                "problems": ["repository worktree is missing: %s" % worktree],
                "repo": _repository_context_repo_snapshot(context),
            }

        package_assignment = _work_package_assignment_projection(task, context)
        package_mode = bool(package_assignment)
        branch = str(context.get("repository_branch") or "").strip()
        if package_mode:
            branch = str(package_assignment["attempt_ref"])
        canonical_remote = _repository_publication_remote(task, context)
        canonical_branch = str(context.get("repository_canonical_branch") or "").strip()
        prepared_base_sha = str(
            package_assignment.get("attempt_base_sha")
            or context.get("repository_base_sha")
            or ""
        ).strip()
        problems: List[str] = []
        self._commit_dirty_repository_worktree(task_id, task, worktree, problems)
        # Rebase onto the advanced canonical tip BEFORE the contract test runs,
        # so the suite validates the projected published tree (fleet agents
        # race each other to one canonical branch; without this a slow task
        # dies at the freshness gate after all its work passed). Clean rebases
        # only — conflicts abort and the freshness gate reports precisely.
        if package_mode:
            canonical_sync = {
                "ok": True,
                "status": "skipped",
                "reason": "immutable_work_package_attempt_base",
                "prepared_base_sha": prepared_base_sha,
            }
            base = _run_git(
                worktree, ["rev-parse", "--verify", "%s^{commit}" % prepared_base_sha]
            )
            if base.returncode != 0 or base.stdout.strip().lower() != prepared_base_sha:
                problems.append("work-package assignment base is not present in the worktree")
            elif _run_git(
                worktree,
                ["merge-base", "--is-ancestor", prepared_base_sha, "HEAD"],
            ).returncode != 0:
                problems.append("work-package attempt HEAD is not descended from its assigned base")
        else:
            canonical_sync = sync_worktree_with_canonical(
                worktree,
                canonical_remote,
                canonical_branch,
            )
        diff_context = dict(context)
        diff_context["repository_base_sha"] = prepared_base_sha
        files_changed = _repository_context_changed_files(worktree, diff_context)

        test_command = _repository_contract_test_command(task)
        hub_verify = _env_truthy(os.environ.get("MAC_REVIEW_HUB_VERIFY"))
        test_item = self._run_repository_contract_test(worktree, test_command, task_dir=task_dir, hub_verify=hub_verify)
        tests = [test_item]
        repo = _repository_context_repo_snapshot(context)
        repo["head_sha"] = _git_stdout(worktree, ["rev-parse", "HEAD"]) or repo.get("head_sha", "")
        repo["dirty"] = _repository_worktree_is_dirty(worktree)
        repo["files_changed"] = files_changed
        repo["pushed"] = False
        repo["canonical_sync"] = canonical_sync
        if branch:
            repo["remote_ref"] = (
                branch if package_mode or branch.startswith("refs/") else "refs/heads/%s" % branch
            )
        repo["base_sha"] = prepared_base_sha
        if package_mode:
            repo["branch"] = branch
        repo["push_remote"] = _redact_git_remote_auth(
            _inject_git_remote_auth(canonical_remote)
        )
        codegraph = run_codegraph_audit(worktree, files_changed)
        repo["dirty"] = _repository_worktree_is_dirty(worktree)

        publication_target = None
        target_error = ""
        if package_mode:
            repo["freshness"] = {
                "ok": not problems,
                "mode": "immutable_attempt_base",
                "prepared_base_sha": prepared_base_sha,
                "task_head_sha": repo["head_sha"],
                "attempt_ref": branch,
                "error": "; ".join(problems),
            }
        else:
            try:
                lease_id = str(context.get("repository_lease_id") or "").strip()
                if not lease_id:
                    raise ValueError("repository context is missing repository_lease_id")
                publication_target = resolve_canonical_publication_target(
                    worktree=worktree,
                    canonical_remote=canonical_remote,
                    canonical_branch=canonical_branch,
                    destination_branch=branch,
                    prepared_base_sha=prepared_base_sha,
                    isolation_key="%s-%s" % (task_id, lease_id),
                )
            except (OSError, ValueError) as exc:
                target_error = str(exc)
                repo["freshness"] = {
                    "ok": False,
                    "remote": repo["push_remote"],
                    "canonical_branch": canonical_branch,
                    "prepared_base_sha": prepared_base_sha,
                    "task_head_sha": repo["head_sha"],
                    "error": target_error,
                }

        pushed = False
        push_item: Optional[JsonDict] = None
        prepush_problems = _repository_finalizer_prepush_problems(
            task,
            repo,
            test_item,
            codegraph=codegraph,
            hub_verify=hub_verify,
        )
        if problems:
            problems.append("repository finalizer had local errors; refusing to push")
        elif prepush_problems:
            problems.extend(prepush_problems)
            problems.append("repository evidence failed local contract checks; refusing to push")
        elif test_item.get("returncode") == 0 or (hub_verify and _is_hub_verify_deferred_item(test_item)):
            if package_mode:
                publication_result = _publish_exact_work_package_attempt(
                    worktree,
                    canonical_remote,
                    branch,
                    str(repo["head_sha"]),
                )
                repo["push_remote"] = publication_result["remote_display"]
                repo["freshness"] = {
                    "ok": publication_result["ok"],
                    "mode": "immutable_attempt_ref",
                    "prepared_base_sha": prepared_base_sha,
                    "task_head_sha": repo["head_sha"],
                    "attempt_ref": branch,
                    "observed_head_sha": publication_result.get("observed_head_sha", ""),
                    "already_present": publication_result.get("already_present", False),
                    "error": publication_result.get("error", ""),
                }
                push_item = _process_check_item(
                    "protected work-package attempt push",
                    0 if publication_result["ok"] else 1,
                    command="git push <canonical-remote> HEAD:%s" % branch,
                    stdout=str(publication_result.get("stdout") or ""),
                    stderr=str(
                        publication_result.get("stderr")
                        or publication_result.get("error")
                        or ""
                    ),
                )
                pushed = bool(
                    publication_result["ok"]
                    and publication_result["remote_verified"]
                )
                if not pushed:
                    problems.append(
                        "protected work-package attempt publication blocked: %s"
                        % publication_result.get("error", "unknown error")
                    )
            elif publication_target is not None:
                publication = guarded_push(publication_target)
                display = (
                    publication.target.remote_display
                    if publication.target is not None
                    else repo["push_remote"]
                )
                repo["push_remote"] = display
                if publication.canonical_tip_sha:
                    repo["base_sha"] = publication.canonical_tip_sha
                repo["freshness"] = publication.evidence()
                push_item = _process_check_item(
                    "guarded git push",
                    0 if publication.ok and publication.remote_verified else 1,
                    command="guarded git push %s HEAD:refs/heads/%s" % (display, branch),
                    stdout=publication.push_stdout,
                    stderr=publication.push_stderr or publication.error,
                )
                pushed = publication.ok and publication.remote_verified
                if not pushed:
                    _push_fail_info = "repository publication blocked: %s" % publication.error
                    if attempt_state is not None:
                        def _noop_dispatch_p(_action, _ctx):
                            pass
                        _p_recovered, _p_choice, _p_msg = _hrr.try_recovery(
                            attempt_state,
                            _push_fail_info,
                            _noop_dispatch_p,
                            lambda _s, _c, _r: self._emit_recovery_observability(
                                task_id, _s, _c, _r
                            ),
                        )
                        self._append_harness_recovery_log(
                            task_dir, "retry_push", _p_choice, _p_msg
                        )
                        if _p_recovered:
                            publication = guarded_push(publication_target)
                            display = (
                                publication.target.remote_display
                                if publication.target is not None
                                else repo["push_remote"]
                            )
                            repo["push_remote"] = display
                            if publication.canonical_tip_sha:
                                repo["base_sha"] = publication.canonical_tip_sha
                            repo["freshness"] = publication.evidence()
                            push_item = _process_check_item(
                                "guarded git push",
                                0 if publication.ok and publication.remote_verified else 1,
                                command="guarded git push %s HEAD:refs/heads/%s" % (display, branch),
                                stdout=publication.push_stdout,
                                stderr=publication.push_stderr or publication.error,
                            )
                            pushed = publication.ok and publication.remote_verified
                    if not pushed:
                        problems.append("repository publication blocked: %s" % publication.error)
            else:
                problems.append("repository publication target invalid: %s" % target_error)
        else:
            problems.append("repository contract test failed; refusing to push")

        repo["pushed"] = pushed

        checks: List[JsonDict] = []
        if str(codegraph.get("status") or "") != "skipped":
            checks.append(codegraph_audit_check(codegraph))
        if push_item is not None:
            checks.append(push_item)
        manifest: JsonDict = {
            "schema": VERIFICATION_SCHEMA,
            "status": "complete",
            "evidence_type": "repo_change",
            "summary": (
                "worker finalized missing repository evidence for successful executor result"
            ),
            "executor_summary": execution.summary,
            "repo": repo,
            "codegraph": codegraph,
            "tests": tests,
            "checks": checks,
        }
        if problems:
            manifest["problems"] = problems
        return manifest

    def _adopt_pushed_branch_if_worktree_matches(
        self,
        task: JsonDict,
        worktree: Path,
        branch: str,
        context: JsonDict,
    ) -> bool:
        """Adopt an already-pushed task branch when the host worktree matches it.

        The sandboxed coding agent pushes the task branch from a throwaway clone
        when the uploaded worktree's gitlink is unusable inside the sandbox; the
        host worktree then holds the same edits uncommitted. Committing locally
        would fork the branch and the finalizer's push would be rejected
        non-fast-forward. When the working tree's tracked content is identical to
        the already-pushed tip, reset onto it so the finalizer sees a clean,
        already-pushed HEAD instead. Returns True iff the branch was adopted.
        """
        if not branch:
            return False
        push_remote, _ = _repository_push_remote(task, context)
        remote_ref = branch if branch.startswith("refs/") else "refs/heads/%s" % branch
        fetch = _run_git(worktree, ["fetch", push_remote, remote_ref])
        if fetch.returncode != 0:
            return False
        # `git diff --quiet FETCH_HEAD --` exits 0 iff the working tree (tracked
        # files) is identical to the pushed tip — i.e. the agent already pushed
        # exactly this work. Untracked build artifacts are not counted.
        if _run_git(worktree, ["diff", "--quiet", "FETCH_HEAD", "--"]).returncode != 0:
            return False
        reset = _run_git(worktree, ["reset", "--hard", "FETCH_HEAD"])
        if reset.returncode != 0:
            return False
        self._observe_log(
            "worker.repository.adopted_pushed_branch",
            subject_type="task",
            subject_id=str(task.get("id") or ""),
            detail={
                "repository_branch": branch,
                "head_sha": _git_stdout(worktree, ["rev-parse", "HEAD"]),
            },
        )
        return True

    def _commit_dirty_repository_worktree(
        self,
        task_id: str,
        task: JsonDict,
        worktree: Path,
        problems: List[str],
    ) -> None:
        status = _run_git(worktree, ["status", "--porcelain"])
        if status.returncode != 0:
            problems.append(
                "could not inspect repository worktree status: %s"
                % ((status.stderr or status.stdout or "").strip() or worktree)
            )
            return
        tracked_lines, untracked_paths, staged_new_paths = _split_repository_porcelain_status(status.stdout)
        if not (tracked_lines or untracked_paths or staged_new_paths):
            return
        # OpenShell returns repository content, not an authoritative Git index.
        # Commit the complete synchronized change at the host boundary so newly
        # created modules follow the same test/CodeGraph/push contract as edits.
        add = _run_git(worktree, ["add", "-A"])
        if add.returncode != 0:
            problems.append(
                "repository finalizer add failed: %s"
                % ((add.stderr or add.stdout or "").strip() or worktree)
            )
            return
        staged = _run_git(worktree, ["diff", "--cached", "--quiet"])
        if staged.returncode == 0:
            return
        if staged.returncode != 1:
            problems.append(
                "repository finalizer staged diff failed: %s"
                % ((staged.stderr or staged.stdout or "").strip() or worktree)
            )
            return
        title = str(task.get("title") or task_id).strip() or task_id
        commit = _run_git(
            worktree,
            [
                "-c",
                "user.email=mac-fleet@nvidia.com",
                "-c",
                "user.name=MAC fleet",
                "commit",
                "-m",
                "MAC task %s: %s" % (task_id, title[:120]),
            ],
        )
        if commit.returncode != 0:
            problems.append(
                "repository finalizer commit failed: %s"
                % ((commit.stderr or commit.stdout or "").strip() or worktree)
            )

    def _run_repository_contract_test(
        self,
        worktree: Path,
        command: str,
        *,
        task_dir: Optional[Path] = None,
        hub_verify: bool = False,
    ) -> JsonDict:
        sandbox_item = _sandbox_repository_verification_item(task_dir, command, hub_verify=hub_verify)
        if sandbox_item is not None:
            return sandbox_item
        if not command:
            return {
                "name": "repository contract test",
                "command": "",
                "returncode": 1,
                "status": "fail",
                "stderr": "repository contract test.command is missing",
            }
        try:
            # Progress-based watchdog: kills only when the command stops
            # emitting output (MAC_TEST_STALL_TIMEOUT, default 300s), with
            # MAC_WORKER_REPOSITORY_TEST_TIMEOUT (1800s) as a hard backstop.
            # Total-runtime constants kept going stale as legitimate work grew
            # (venv bootstrap + suite) and killed healthy runs mid-flight.
            from mac.task_executor import run_with_stall_watchdog

            proc = run_with_stall_watchdog(["bash", "-lc", command], worktree)
        except Exception as exc:  # noqa: BLE001 - report as verification failure.
            return {
                "name": "repository contract test",
                "command": command,
                "returncode": 1,
                "status": "fail",
                "stderr": str(exc),
            }
        return _process_check_item(
            "repository contract test",
            proc.returncode,
            command=command,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    def _execution_submission_problems(self, task_dir: Path, evidence: JsonDict) -> List[str]:
        problems: List[str] = []
        metadata = evidence.get("metadata") if isinstance(evidence, dict) else None
        manifest = metadata.get("verification") if isinstance(metadata, dict) else None
        task_payload = _task_payload_from_workspace(task_dir)
        if not isinstance(manifest, dict):
            return ["evidence metadata lacks verification manifest"]
        if str(manifest.get("schema") or "").strip() != VERIFICATION_SCHEMA:
            problems.append("verification.schema must be %s" % VERIFICATION_SCHEMA)
        if str(manifest.get("status") or "").strip().lower() != "complete":
            problems.append('verification.status must be "complete"')
        evidence_type = str(manifest.get("evidence_type") or "").strip().lower()
        if not evidence_type:
            problems.append("verification.evidence_type is required")
        if not str(manifest.get("signed_by") or "").strip() or not str(manifest.get("signature") or "").strip():
            problems.append("verification.signed_by and verification.signature are required")
        if evidence_type:
            if (
                evidence_type == "investigation"
                and declared_non_repository_outcome_evidence_type(
                    ensure_json_object(task_payload.get("metadata"))
                )
                != "investigation"
            ):
                problems.append(
                    "investigation evidence requires an operator-authored "
                    "investigation execution contract"
                )
            problems.extend(
                _worker_verification_contract_problems(
                    manifest,
                    evidence_type,
                    allow_empty_repo_change=_worker_allows_empty_repo_change_evidence(
                        task_payload,
                        evidence_type,
                    ),
                )
            )
            if evidence_type == "review_verdict":
                problems.extend(_worker_review_verdict_executor_repo_problems(task_dir, manifest))
            problems.extend(_worker_required_changed_file_problems(task_payload, manifest))

        serialized_context = _load_repository_context(task_dir)
        trusted_read_only_context = _trusted_read_only_repository_context(task_payload)
        repository_context = trusted_read_only_context or serialized_context
        is_review_task = isinstance(
            ensure_json_object(task_payload.get("metadata")).get("review_context"),
            dict,
        )
        if repository_context:
            worktree_raw = str(repository_context.get("repository_worktree") or "").strip()
            worktree = Path(worktree_raw).expanduser() if worktree_raw else Path()
            if not worktree.exists():
                problems.append("repository worktree is missing: %s" % worktree)
            elif trusted_read_only_context:
                problems.extend(
                    _read_only_repository_context_drift_problems(
                        trusted_read_only_context, serialized_context
                    )
                )
                expected_evidence_type = (
                    "review_verdict" if is_review_task else "operator_result"
                )
                if evidence_type != expected_evidence_type:
                    problems.append(
                        "read-only repository %s evidence_type must be %s"
                        % (
                            "review" if is_review_task else "report",
                            expected_evidence_type,
                        )
                    )
                problems.extend(
                    _read_only_repository_problems(worktree, repository_context)
                )
                expected_access = _read_only_repository_access_evidence(
                    trusted_read_only_context
                )
                if is_review_task:
                    expected_access["independent_review_verified"] = True
                if manifest.get("repository_access") != expected_access:
                    problems.append(
                        "verification.repository_access does not match the prepared "
                        "read-only repository contract"
                    )
                trusted_test_item, trusted_test_problems = (
                    _trusted_read_only_report_test_item(task_dir, task_payload)
                )
                problems.extend(trusted_test_problems)
                if _repository_contract_test_command(task_payload):
                    expected_tests = (
                        [trusted_test_item] if trusted_test_item is not None else []
                    )
                    if manifest.get("tests") != expected_tests:
                        problems.append(
                            "verification.tests does not match the trusted OpenShell "
                            "repository contract result"
                        )
            else:
                # New-file handoff: the sandboxed coding agent commonly leaves
                # intended new source/test files untracked (OpenShell returns
                # repository content, not an authoritative Git index). Stage and
                # commit the complete synchronized change here -- BEFORE the
                # dirty gate -- so those new files follow the same contract as
                # edits instead of tripping "uncommitted changes" and wasting an
                # attempt on an otherwise-successful task.
                task_id = str(task_payload.get("id") or "").strip()
                self._commit_dirty_repository_worktree(
                    task_id, task_payload, worktree, problems
                )
                dirty = _run_git(worktree, ["status", "--porcelain"])
                if dirty.returncode != 0:
                    problems.append(
                        "could not inspect repository worktree status: %s"
                        % ((dirty.stderr or dirty.stdout or "").strip() or worktree)
                    )
                elif dirty.stdout.strip():
                    problems.append("repository worktree has uncommitted changes")
                head = _run_git(worktree, ["rev-parse", "HEAD"])
                repo = manifest.get("repo") if isinstance(manifest.get("repo"), dict) else {}
                worktree_head = head.stdout.strip() if head.returncode == 0 else ""
                manifest_head = str(repo.get("head_sha") or "").strip() if isinstance(repo, dict) else ""
                # Auto-committing new files advances HEAD past the SHA the agent
                # recorded. Reconcile the manifest with the freshly committed
                # HEAD (on disk and in memory) so the head_sha equality check
                # stays coherent rather than failing on our own commit.
                if (
                    worktree_head
                    and manifest_head
                    and worktree_head != manifest_head
                    and isinstance(repo, dict)
                ):
                    self._reconcile_manifest_head(
                        task_dir, evidence, repo, worktree_head
                    )
                    manifest_head = worktree_head
                if worktree_head and manifest_head and worktree_head != manifest_head:
                    problems.append("verification.repo.head_sha does not match worktree HEAD")
        return problems

    def _reconcile_manifest_head(
        self,
        task_dir: Path,
        evidence: JsonDict,
        repo: JsonDict,
        head_sha: str,
    ) -> None:
        """Point the evidence manifest at ``head_sha`` after the finalizer
        auto-committed new files.

        The worker stages/commits intended new files before the dirty gate, so
        HEAD advances past the SHA the agent recorded. Update the in-memory
        manifest and the on-disk ``mac-evidence.json`` so downstream checks and
        the host finalizer read a coherent, freshly committed head_sha. The disk
        write is best-effort: the in-memory manifest is authoritative for this
        gate.
        """
        repo["head_sha"] = head_sha
        repo["dirty"] = False
        manifest_path = task_dir / "mac-evidence.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(manifest, dict):
            return
        manifest_repo = manifest.get("repo")
        if isinstance(manifest_repo, dict):
            manifest_repo["head_sha"] = head_sha
            manifest_repo["dirty"] = False
        try:
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            pass

    def _record_review_execution(
        self,
        task_id: str,
        task_dir: Path,
        execution: WorkerExecution,
        *,
        review_id: str,
        executor_evidence_id: str,
        message_id: str,
    ) -> JsonDict:
        _write_host_control_text(task_dir / "stdout.txt", execution.stdout, task_dir)
        _write_host_control_text(task_dir / "stderr.txt", execution.stderr, task_dir)
        result_path = task_dir / "review-result.json"
        metadata = self._execution_metadata(task_dir, execution)
        _write_host_control_text(
            result_path,
            json.dumps(
                {
                    "returncode": execution.returncode,
                    "summary": execution.summary,
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                    "metadata": metadata,
                },
                indent=2,
                sort_keys=True,
            ),
            task_dir,
        )
        artifacts = _durable_evidence_artifacts(task_dir, result_path)
        evidence_result = self.client.post(
            "/tasks/%s/evidence" % quote(task_id, safe=""),
            {
                "kind": "review",
                "uri": result_path.resolve().as_uri(),
                "summary": execution.summary,
                "created_by": self.agent_id,
                "artifacts": artifacts,
                "metadata": {
                    "returncode": execution.returncode,
                    "stdout": (task_dir / "stdout.txt").resolve().as_uri(),
                    "stderr": (task_dir / "stderr.txt").resolve().as_uri(),
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                    "nudge_message_id": message_id,
                    **metadata,
                },
            },
        )
        # The reviewer's findings in its own words. The approved/rejected verdict
        # line is recorded separately when the workflow finalizes (submit_review);
        # this captures what the reviewer actually looked at / found. Best-effort.
        self._post_task_activity(task_id, "review", self._review_activity_summary(execution))
        return evidence_result

    def _review_activity_summary(self, execution: WorkerExecution) -> str:
        """The reviewer's recap: its delimited summary block, else a prose tail,
        else a harness-failure note."""
        recap = _extract_marked_summary(execution.stdout)
        if recap:
            return recap
        body = "\n".join(_prose_tail(execution.stdout, 4)).strip()
        if body:
            return body
        if not execution.succeeded:
            return "review harness did not produce a verdict (rc %s)" % execution.returncode
        return (execution.summary or "").strip()

    def _execution_metadata(self, task_dir: Path, execution: WorkerExecution) -> JsonDict:
        metadata = dict(execution.metadata)
        # The external activation probe is optional diagnostic evidence only.
        # It consumes activations supplied by an instrumented runtime; it cannot
        # inspect hosted-model internals. Its adapter catches model/checkpoint/
        # input failures, and this outer boundary guarantees a future adapter
        # regression still cannot change task success, review, or publication.
        try:
            from mac.activation_probe.advisory import (
                activation_probe_audit_from_environment,
            )

            activation_probe_audit = activation_probe_audit_from_environment(
                task_dir, execution.metadata
            )
            if activation_probe_audit is not None:
                metadata["activation_probe_audit"] = activation_probe_audit
        except Exception as exc:  # noqa: BLE001 - advisory means non-authoritative.
            logger.warning("external activation-probe evidence unavailable: %s", exc)
        # Raw residual tensors can be very large and are an executor-to-auditor
        # handoff, not durable task evidence.  Persist only the bounded result.
        metadata.pop("activation_probe_activations", None)
        task_payload = _task_payload_from_workspace(task_dir)
        serialized_context = _load_repository_context(task_dir)
        trusted_read_only_context = _trusted_read_only_repository_context(task_payload)
        is_review_task = isinstance(
            ensure_json_object(task_payload.get("metadata")).get("review_context"),
            dict,
        )
        manifest = metadata.get("verification") or self._load_verification_manifest(task_dir)
        manifest = ensure_json_object(manifest)
        if trusted_read_only_context:
            # Read-only report provenance is not a publishable repo anchor.
            # Keeping it outside ``repo`` prevents the hub/reviewer from
            # imposing pushed-commit semantics on operator_result evidence.
            manifest = dict(manifest)
            manifest.pop("repo", None)
            manifest["evidence_type"] = (
                "review_verdict" if is_review_task else "operator_result"
            )
            authoritative_access = _read_only_repository_access_evidence(
                trusted_read_only_context
            )
            if is_review_task and ensure_json_object(
                manifest.get("repository_access")
            ).get("independent_review_verified") is True:
                authoritative_access["independent_review_verified"] = True
            manifest["repository_access"] = authoritative_access
            manifest, trusted_test_problems = _attach_trusted_read_only_report_test(
                manifest, task_dir, task_payload
            )
            if trusted_test_problems:
                manifest["status"] = "invalid"
                manifest["problems"] = list(
                    dict.fromkeys(
                        [
                            *_manifest_list(manifest.get("problems")),
                            *trusted_test_problems,
                        ]
                    )
                )
        else:
            manifest = _enrich_verification_manifest_from_repository_context(
                manifest,
                serialized_context,
                task=task_payload,
            )
            manifest = _attach_repository_codegraph_audit(manifest, serialized_context)
        metadata["verification"] = self._sign_verification_manifest(manifest)
        metadata.setdefault(
            "workspace_outputs",
            {
                "stdout_sha256": _sha256_file(task_dir / "stdout.txt"),
                "stderr_sha256": _sha256_file(task_dir / "stderr.txt"),
            },
        )
        return metadata

    def _sign_verification_manifest(self, manifest: JsonDict) -> JsonDict:
        """Stamp ``signed_by`` + ``signature`` onto the manifest if an
        attestation key is configured (mac-ng2). Without a key the
        manifest is returned unmodified — the default-review workflow
        will then refuse the evidence as ``manifest_not_signed``,
        which is the correct outcome for an unkeyed worker."""
        if not self.attestation_key or not isinstance(manifest, dict):
            return manifest
        from mac.services import sign_verification_manifest

        signed = dict(manifest)
        signed["signed_by"] = self.agent_id
        signed["signature"] = sign_verification_manifest(self.attestation_key, signed)
        return signed

    def _heal_attestation_key(self) -> bool:
        """Diagnose key drift without granting a worker rotation authority.

        Key replacement is an administrator-owned deployment transaction: it
        proves the target under the deployer lock, conditionally rotates,
        installs through owner-only files, restarts, and proves the new key a
        second time. A bound worker may perform this secret-free health check,
        but must never retrieve or install fresh signing authority itself.

        The historical return contract is retained for the submission caller;
        it always returns ``False`` because no in-process re-sign is permitted.
        """
        if not self.attestation_key:
            return False
        now = time.monotonic()
        min_interval = _env_float("MAC_ATTESTATION_HEAL_MIN_SECONDS", 60.0)
        if self._last_attestation_heal_at and now - self._last_attestation_heal_at < min_interval:
            return False
        self._last_attestation_heal_at = now
        try:
            if _attestation_key_matches_hub(
                self.client, self.agent_id, self.attestation_key
            ):
                return False
        except Exception as exc:  # noqa: BLE001 - healing is best-effort.
            self._observe_log(
                "worker.attestation.heal_failed",
                level="error",
                detail={"agent_id": self.agent_id, "error": str(exc)},
            )
            return False
        self._observe_log(
            "worker.attestation.controller_recovery_required",
            level="error",
            detail={
                "agent_id": self.agent_id,
                "reason": (
                    "installed key does not match the hub; a fenced admin "
                    "deployment recovery is required"
                ),
            },
        )
        return False

    def _load_verification_manifest(self, task_dir: Path) -> JsonDict:
        manifest_path = task_dir / "mac-evidence.json"
        if not manifest_path.exists():
            return {
                "schema": "mac.worker_evidence.v1",
                "status": "missing",
                "problems": ["mac-evidence.json was not produced by the executor"],
            }
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - malformed evidence should be captured, not crash reporting
            return {
                "schema": "mac.worker_evidence.v1",
                "status": "invalid",
                "problems": ["could not parse mac-evidence.json: %s" % exc],
                "uri": manifest_path.resolve().as_uri(),
            }
        if not isinstance(loaded, dict):
            return {
                "schema": "mac.worker_evidence.v1",
                "status": "invalid",
                "problems": ["mac-evidence.json must contain a JSON object"],
                "uri": manifest_path.resolve().as_uri(),
            }
        loaded.setdefault("schema", "mac.worker_evidence.v1")
        loaded.setdefault("uri", manifest_path.resolve().as_uri())
        loaded.setdefault("sha256", _sha256_file(manifest_path))
        return loaded

    def _call_executor(
        self,
        task: JsonDict,
        task_dir: Path,
        audit_context: JsonDict,
    ) -> WorkerExecution:
        if isinstance(self.executor, SubprocessExecutor):
            prior_context = self.executor.audit_context
            self.executor.audit_context = audit_context
            try:
                return self.executor(task, task_dir)
            finally:
                self.executor.audit_context = prior_context
        return self.executor(task, task_dir)

    def _record_command_audit(self, record: JsonDict) -> None:
        payload = {
            "command_id": record.get("command_id"),
            "phase": record.get("phase"),
            "argv": record.get("argv") or [],
            "cwd": record.get("cwd") or "",
            "task_id": record.get("task_id"),
            "lease_id": record.get("lease_id"),
            "started_at": record.get("started_at"),
            "completed_at": record.get("completed_at"),
            "duration_ms": record.get("duration_ms"),
            "returncode": record.get("returncode"),
            "stdout_sha256": record.get("stdout_sha256"),
            "stderr_sha256": record.get("stderr_sha256"),
            "stdout_bytes": record.get("stdout_bytes"),
            "stderr_bytes": record.get("stderr_bytes"),
            "metadata": record.get("metadata") or {},
        }
        try:
            self.client.post(
                "/agents/%s/command-audit" % quote(self.agent_id, safe=""),
                payload,
            )
        except Exception:
            pass

    # --- autonomous self-install (pip/npm into the agent's OWN environment) ----
    # Fully unrestricted by decision: install only into the agent venv / local
    # npm prefix (never the system), every install is audited via command_audit,
    # and the resulting footprint is reported to the hub so redeploys re-hydrate
    # it. This is what lets an agent provision its own tools for autonomous work.

    def _maybe_sync_service_claims(self) -> None:
        # media-01: claim/renew the media service-roles this host is willing to
        # run, and advertise only the ops it currently HOLDS. Throttled + fully
        # best-effort (never breaks the run loop).
        import time as _t

        now = _t.monotonic()
        last = getattr(self, "_last_service_sync", None)
        if last is not None and (now - last) < 30.0:
            return
        self._last_service_sync = now
        try:
            self._sync_service_claims()
        except Exception:  # noqa: BLE001 - service sync must never break the loop
            pass

    def _sync_service_claims(self) -> None:
        host = (os.environ.get("MAC_AGENT_GEN_HOST") or socket.gethostname()).strip()
        try:
            agent = self.client.get("/agents/%s" % quote(self.agent_id, safe=""))
        except Exception:  # noqa: BLE001
            return
        base = dict((agent or {}).get("resources") or {})
        all_routes = _build_willing_media_routes(host, base.get("hardware"))
        willing_ops = sorted({str(r.get("op")) for r in all_routes if r.get("op")})
        if not willing_ops:
            return
        try:
            res = self.client.post(
                "/agents/%s/service-claims/sync" % quote(self.agent_id, safe=""),
                {"willing_ops": willing_ops},
            )
        except Exception:  # noqa: BLE001
            return
        held = set((res or {}).get("held") or [])
        managed = set((res or {}).get("managed") or [])
        # advertise-on-hold: advertise an op if we HOLD its claim, OR if it isn't
        # managed by a service_role at all (back-compat: no roles seeded -> advertise
        # everything we're willing+able to serve, as before).
        base["media_routes"] = [
            r for r in all_routes if r.get("op") in held or r.get("op") not in managed
        ]
        base = self._resources_with_live_report_executor_attestation(base)
        try:
            refreshed = self.client.post(
                "/agents/%s/heartbeat" % quote(self.agent_id, safe=""),
                _deployment_heartbeat_payload("idle", resources=base),
            )
            _apply_read_only_report_executor_approval(
                refreshed.get("resources") if isinstance(refreshed, Mapping) else None,
                os.environ,
            )
        except Exception:  # noqa: BLE001
            pass

    def _resources_with_live_report_executor_attestation(
        self, resources: Optional[Mapping[str, Any]]
    ) -> JsonDict:
        """Replace the report claim with a fresh local artifact probe."""

        refreshed = dict(resources or {})
        refreshed.pop(REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY, None)
        refreshed.pop(REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY, None)
        executor_argv = (
            list(self.executor.argv)
            if isinstance(self.executor, SubprocessExecutor)
            else []
        )
        attestation = _read_only_report_executor_attestation(executor_argv)
        if attestation is not None:
            refreshed[REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY] = attestation
        return refreshed

    def _heartbeat(self, status_override: Optional[str] = None) -> None:
        # Push dispatch may have claimed a task since this process last polled.
        # Read our durable agent row before declaring local idleness so the hub
        # sees a truthful BUSY heartbeat and can return that existing lease from
        # claim-next.  Preserve the normal idle fallback when the read itself is
        # transiently unavailable; the heartbeat will surface any real conflict.
        heartbeat_status = status_override or "idle"
        current_agent: Optional[JsonDict] = None
        try:
            current_agent = self.client.get(
                "/agents/%s" % quote(self.agent_id, safe="")
            )
            if status_override is None and (current_agent or {}).get("current_task_id"):
                heartbeat_status = "busy"
        except MacApiError:
            pass
        self._maybe_start_coding_route_probe()
        command_resources = self._maybe_command_inventory_resources()
        deployment_generation, _ = _deployment_barrier_state()
        if deployment_generation:
            if command_resources is not None:
                command_resources = dict(command_resources)
            elif isinstance(current_agent, Mapping) and isinstance(
                current_agent.get("resources"), Mapping
            ):
                command_resources = dict(current_agent["resources"])
        if command_resources is None and isinstance(current_agent, Mapping):
            current_resources = current_agent.get("resources")
            if isinstance(current_resources, Mapping):
                command_resources = dict(current_resources)
        # A heartbeat that supplies ``resources`` is a full-document replacement:
        # the hub swaps the stored resource map for exactly what we send.  When
        # no prerequisite GET produced a base document (e.g. a first heartbeat
        # that raced hub availability after a restart), ``command_resources`` is
        # ``None`` and we MUST omit resources entirely -- otherwise the live
        # report-executor attestation refresh would synthesise a partial,
        # attestation-only map and erase hardware, media_routes, openclaw_runtime,
        # chat_gateway, gateway_ownership, and representation.  Only refresh the
        # attestation when we have a real base to refresh.
        if command_resources is not None:
            command_resources = self._resources_with_live_report_executor_attestation(
                command_resources
            )
        # Health is a worker observation, not a controller override. The hub
        # projects this request through sticky startup-self-test resources, so
        # asking for healthy can still correctly remain degraded.
        payload = _deployment_heartbeat_payload(
            heartbeat_status,
            resources=command_resources,
            report_health=True,
        )
        # Declare the build the agent is running. Send the digest at most once
        # per process; subsequent heartbeats are pure liveness pings.
        if self.running_digest and not self._declared_digest:
            payload["running_digest"] = self.running_digest
        refreshed = self.client.post(
            "/agents/%s/heartbeat" % quote(self.agent_id, safe=""),
            payload,
        )
        _apply_read_only_report_executor_approval(
            refreshed.get("resources") if isinstance(refreshed, Mapping) else None,
            os.environ,
        )
        if self.running_digest and not self._declared_digest:
            self._declared_digest = True

    def _maybe_command_inventory_resources(self) -> Optional[JsonDict]:
        interval = _env_float(
            "MAC_WORKER_COMMAND_INVENTORY_INTERVAL_SECONDS",
            DEFAULT_COMMAND_INVENTORY_INTERVAL_SECONDS,
        )
        if interval < 0:
            return None
        now = time.monotonic()
        with self._coding_route_probe_lock:
            route_dirty = self._coding_route_report_dirty
        if (
            not route_dirty
            and self._last_command_inventory_at
            and (now - self._last_command_inventory_at) < interval
        ):
            return None
        try:
            agent = self.client.get("/agents/%s" % quote(self.agent_id, safe=""))
            resources = ensure_json_object((agent or {}).get("resources"))
        except Exception:
            return None
        self._last_command_inventory_at = now
        with self._coding_route_probe_lock:
            route_report = dict(self._coding_route_report)
            self._coding_route_report_dirty = False
        return _resources_with_command_inventory(
            resources,
            route_report,
            source_repo=self.self_update_repo,
            agent_id=self.agent_id,
        )

    def _maybe_start_coding_route_probe(self) -> None:
        """Asynchronously verify the preferred route before the hub dispatches work."""
        with self._coding_route_probe_lock:
            if self._coding_route_probe_thread is not None and self._coding_route_probe_thread.is_alive():
                return
            verified = self._coding_route_report.get("verified") is True
            # Successful executor-side proofs cache for five minutes. Probe on
            # a ten-minute cadence so every scheduled success refresh is live,
            # never a cached proof followed by another full sleep interval.
            default_interval = 600.0 if verified else 60.0
            interval = _env_float(
                "MAC_WORKER_CODING_ROUTE_PROBE_INTERVAL_SECONDS",
                default_interval,
            )
            now = time.monotonic()
            if self._last_coding_route_probe_at and now - self._last_coding_route_probe_at < max(1.0, interval):
                return
            self._last_coding_route_probe_at = now
            self._coding_route_report = {
                "schema": "mac.coding_agent.verification.v1",
                "agent": "",
                "verified": False,
                "checked_at": _utcnow(),
                "failure_class": "pending",
            }
            self._coding_route_report_dirty = True
            thread = threading.Thread(
                target=self._probe_coding_route,
                name="mac-coding-route-probe-%s" % self.agent_id,
                daemon=True,
            )
            self._coding_route_probe_thread = thread
            thread.start()

    def _probe_coding_route(self) -> None:
        reports: JsonDict = {}
        try:
            from mac.coding_agent import resolve_coding_agent
            from mac.task_executor import coding_agent_sandbox_verification

            def _verify(choice: Any) -> bool:
                try:
                    checked = dict(coding_agent_sandbox_verification(choice))
                except Exception as exc:  # noqa: BLE001
                    # Continue to the next configured route after a probe crash.
                    checked = {
                        **choice.observable(),
                        "schema": "mac.coding_agent.verification.v1",
                        "agent": choice.agent,
                        "route_fingerprint": choice.route_fingerprint(),
                        "verified": False,
                        "checked_at": _utcnow(),
                        "failure_class": "probe_exception",
                        "detail": exc.__class__.__name__,
                    }
                reports[choice.agent] = checked
                return checked.get("verified") is True

            choice = resolve_coding_agent(accept=_verify)
            verified = bool(choice.available)
            if verified:
                failure_class = ""
            elif reports:
                failure_class = "all_routes_failed"
            else:
                failure_class = "not_configured"
            report = {
                "schema": "mac.coding_agent.verifications.v1",
                "agent": choice.agent if verified else "",
                "verified": verified,
                "checked_at": _utcnow(),
                "failure_class": failure_class,
                "reports": reports,
            }
        except Exception as exc:  # noqa: BLE001 - verification failure is a route hold, not worker death.
            report = {
                "schema": "mac.coding_agent.verifications.v1",
                "agent": "",
                "verified": False,
                "checked_at": _utcnow(),
                "failure_class": "probe_exception",
                "detail": exc.__class__.__name__,
                "reports": reports,
            }
        with self._coding_route_probe_lock:
            self._coding_route_report = dict(report)
            self._coding_route_report_dirty = True

    def _observe_metric(
        self,
        name: str,
        value: float,
        unit: str = "",
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        detail: Optional[JsonDict] = None,
    ) -> None:
        self._post_observation(
            "/observability/metrics",
            {
                "name": name,
                "value": value,
                "unit": unit,
                "layer": "worker",
                "source": self.agent_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "detail": detail or {},
            },
        )

    def _observe_log(
        self,
        name: str,
        level: str = "info",
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        detail: Optional[JsonDict] = None,
    ) -> None:
        self._post_observation(
            "/observability/logs",
            {
                "name": name,
                "level": level,
                "layer": "worker",
                "source": self.agent_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "detail": detail or {},
            },
        )

    def _post_observation(self, path: str, payload: JsonDict) -> None:
        try:
            self.client.post(path, payload)
        except Exception as exc:  # noqa: BLE001 - telemetry cannot block work.
            self._observation_post_failures += 1
            now = time.monotonic()
            # The old blanket `pass` made a broken telemetry path invisible.
            # Log the first drop immediately and then at most once per minute;
            # this remains best-effort without turning an outage into another
            # unbounded event stream.
            if (
                self._observation_post_failures == 1
                or now - self._last_observation_failure_log_at >= 60.0
            ):
                self._last_observation_failure_log_at = now
                logger.warning(
                    "observation delivery failed path=%s dropped=%d error=%s",
                    path,
                    self._observation_post_failures,
                    type(exc).__name__,
                )

    def _emit_recovery_observability(
        self,
        task_id: str,
        step: str,
        choice: str,
        result_detail: str,
    ) -> None:
        """Emit a structured observability event for a harness recovery action.

        Called before dispatching each remediation so the hub and fleet
        operators can observe recovery attempts in the task event log.
        """
        self._observe_log(
            "worker.harness.recovery",
            level="info",
            subject_type="task",
            subject_id=task_id,
            detail={
                "step": step,
                "choice": choice,
                "result": result_detail,
            },
        )

    def _append_harness_recovery_log(
        self,
        task_dir: Path,
        step: str,
        choice: str,
        result_detail: str,
    ) -> None:
        """Append one recovery entry to harness-recovery-log.json in task_dir.

        The file is written/extended on each invocation so cross-process
        evidence survives partial failures.
        """
        log_path = task_dir / "harness-recovery-log.json"
        try:
            existing: List[JsonDict] = []
            if log_path.exists():
                raw = log_path.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    existing = [e for e in parsed if isinstance(e, dict)]
            existing.append({"step": step, "choice": choice, "result": result_detail, "ts": _utcnow()})
            log_path.write_text(
                json.dumps(existing, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 - harness log is best-effort evidence
            pass


def _summary_from_output(returncode: int, stdout: str, stderr: str) -> str:
    stream = stdout if stdout.strip() else stderr
    first_line = next((line.strip() for line in stream.splitlines() if line.strip()), "")
    if first_line:
        return first_line[:500]
    return "executor completed" if returncode == 0 else "executor failed with returncode %d" % returncode


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _command_audit_id() -> str:
    seed = "%s:%s:%s" % (time.time_ns(), os.getpid(), threading.get_ident())
    return "cmd_%s" % hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _sha256_text(value: str) -> str:
    return "sha256:%s" % hashlib.sha256(value.encode("utf-8")).hexdigest()


def ensure_json_object(value: Any) -> JsonDict:
    return dict(value) if isinstance(value, dict) else {}


def _json_object_from_text(value: str) -> JsonDict:
    """Parse an OpenClaw JSON receipt without retaining human message text."""

    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Some CLI builds prefix one informational line before the JSON body.
        start = text.find("{")
        if start < 0:
            return {}
        try:
            parsed = json.loads(text[start:])
        except json.JSONDecodeError:
            return {}
    return ensure_json_object(parsed)


def _provider_message_id(receipt: Any) -> Optional[str]:
    """Find a provider receipt id across OpenClaw channel result shapes."""

    if isinstance(receipt, dict):
        for key in ("messageId", "message_id", "ts", "id"):
            value = receipt.get(key)
            if value not in (None, "") and not isinstance(value, (dict, list)):
                return str(value)
        for key in ("result", "data", "message", "response"):
            found = _provider_message_id(receipt.get(key))
            if found:
                return found
    elif isinstance(receipt, list):
        for item in receipt:
            found = _provider_message_id(item)
            if found:
                return found
    return None


def _load_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_slack_accounts(hermes_home: Path) -> List[JsonDict]:
    data = _load_json_file(hermes_home / "slack_accounts.json")
    if isinstance(data, dict):
        raw_accounts = data.get("accounts") or data.get("workspaces") or data
        if isinstance(raw_accounts, dict):
            raw_accounts = [
                {**ensure_json_object(value), "name": key}
                for key, value in raw_accounts.items()
            ]
    else:
        raw_accounts = data
    if not isinstance(raw_accounts, list):
        return []
    return [ensure_json_object(item) for item in raw_accounts if isinstance(item, dict)]


def _load_slack_home_channels(hermes_home: Path) -> List[JsonDict]:
    data = _load_json_file(hermes_home / "slack_home_channels.json")
    if isinstance(data, dict):
        raw_channels = data.get("channels") or data.get("home_channels") or data
        if isinstance(raw_channels, dict):
            raw_channels = [
                {**ensure_json_object(value), "name": key}
                for key, value in raw_channels.items()
            ]
    else:
        raw_channels = data
    if not isinstance(raw_channels, list):
        return []
    return [ensure_json_object(item) for item in raw_channels if isinstance(item, dict)]


def _target_slack_route(target: JsonDict) -> tuple[str, str]:
    team_id = str(target.get("team_id") or "").strip()
    channel_id = str(target.get("channel_id") or "").strip()
    external_id = str(target.get("external_id") or "").strip()
    if external_id and "/" in external_id:
        raw_team, raw_channel = external_id.split("/", 1)
        team_id = team_id or raw_team.strip()
        channel_id = channel_id or raw_channel.strip()
    return team_id, channel_id


def _status_update_slack_text(notification: JsonDict) -> str:
    """Return a compact one-liner for Slack task-progress notifications.

    Format: ``[event_type] body`` — the bracketed event type gives quick context
    and the body carries the human-readable summary.  When body is absent or
    duplicates the title, fall back to just the title so the line never looks
    empty.  Subject ID is omitted from the visible text to keep it short
    (it is already in the notification payload itself for any tool that needs it).
    """
    body = str(notification.get("body") or "").strip()
    title = str(notification.get("title") or "Task update").strip()
    event_type = str(notification.get("event_type") or "").strip()
    text = body if body and body != title else title
    if event_type:
        return ("[%s] %s" % (event_type, text))[:3000]
    return text[:3000]


def _coerce_process_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _audit_safe_argv(argv: List[str]) -> List[str]:
    safe: List[str] = []
    redact_next = False
    for raw in argv:
        arg = str(raw)
        lowered = arg.lower()
        if redact_next:
            safe.append(_redacted_arg(arg))
            redact_next = False
            continue
        if lowered in {"--token", "--api-key", "--key", "--secret", "--password"}:
            safe.append(arg)
            redact_next = True
            continue
        if any(marker in lowered for marker in ("bearer ", "token=", "api_key=", "apikey=", "password=", "secret=")):
            safe.append(_redacted_arg(arg))
            continue
        if len(arg) > 512:
            safe.append("<truncated:%s:chars=%d>" % (_sha256_text(arg), len(arg)))
            continue
        safe.append(arg)
    return safe


def _redacted_arg(value: str) -> str:
    return "<redacted:%s:chars=%d>" % (_sha256_text(value), len(value))


def _safe_path_component(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)[:180]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError:
        return ""
    return "sha256:%s" % digest.hexdigest()


def _evidence_artifact_max_bytes() -> int:
    raw = os.environ.get("MAC_EVIDENCE_ARTIFACT_MAX_BYTES", "").strip()
    try:
        value = int(raw) if raw else 5 * 1024 * 1024
    except ValueError:
        value = 5 * 1024 * 1024
    return min(50 * 1024 * 1024, max(0, value))


def _evidence_artifact_total_max_bytes() -> int:
    raw = os.environ.get("MAC_EVIDENCE_ARTIFACT_TOTAL_MAX_BYTES", "").strip()
    try:
        value = int(raw) if raw else 50 * 1024 * 1024
    except ValueError:
        value = 50 * 1024 * 1024
    return min(100 * 1024 * 1024, max(0, value))


# Media produced by direct-LLM generation (image/audio/video). Used both to
# recognize generated media for durable capture and to stamp a correct
# content-type so a downstream consumer can render/replay it.
_MEDIA_CONTENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".bmp": "image/bmp", ".tiff": "image/tiff",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
    ".flac": "audio/flac", ".m4a": "audio/mp4", ".aac": "audio/aac",
}


def _artifact_content_type(path: Path) -> str:
    if path.suffix == ".json":
        return "application/json"
    if path.suffix in {".txt", ".log", ".md"}:
        return "text/plain; charset=utf-8"
    media = _MEDIA_CONTENT_TYPES.get(path.suffix.lower())
    if media is not None:
        return media
    return "application/octet-stream"


def _evidence_media_total_max_bytes() -> int:
    raw = os.environ.get("MAC_EVIDENCE_MEDIA_TOTAL_MAX_BYTES", "").strip()
    try:
        value = int(raw) if raw else 25 * 1024 * 1024
    except ValueError:
        value = 25 * 1024 * 1024
    return min(100 * 1024 * 1024, max(0, value))


def _evidence_media_max_files() -> int:
    raw = os.environ.get("MAC_EVIDENCE_MEDIA_MAX_FILES", "").strip()
    try:
        value = int(raw) if raw else 20
    except ValueError:
        value = 20
    return min(200, max(0, value))


def _hermes_media_cache_dirs() -> List[Path]:
    """Where direct-LLM generation writes: the Hermes per-agent media cache.

    The mac-hub image plugin (and the audio/video paths) save generated bytes
    under $HERMES_HOME/cache/{images,audio,video}. Scanning these — rather than
    the repo worktree — captures produced media without picking up the repo's
    own checked-in assets.
    """
    home = (
        os.environ.get("HERMES_HOME")
        or os.environ.get("MAC_HERMES_HOME")
        or ""
    ).strip()
    base = Path(home).expanduser() if home else mac_paths.gateway_home()
    cache = base / "cache"
    return [cache / "images", cache / "audio", cache / "video"]


def _durable_media_artifacts(task_dir: Path) -> List[JsonDict]:
    """Capture image/audio/video generated during this task as durable,
    fetchable evidence artifacts.

    Without this, media produced by direct-LLM generation survives only as a
    path string in the agent's tool output and is lost when the workspace/cache
    is torn down — so an autonomous "generate an image/video" task had no
    deliverable. Scoped to files modified during this task run (mtime floor at
    the task workspace's creation time) so stale cache entries from prior tasks
    are not attached. Bounded by per-artifact + media-total byte caps and a
    file-count cap.
    """
    try:
        since = task_dir.stat().st_mtime - 5.0  # small clock-skew tolerance
    except OSError:
        since = 0.0
    per_limit = _evidence_artifact_max_bytes()
    total_limit = _evidence_media_total_max_bytes()
    max_files = _evidence_media_max_files()
    if per_limit <= 0 or total_limit <= 0 or max_files <= 0:
        return []

    found: List[Path] = []
    for directory in _hermes_media_cache_dirs():
        if not directory.is_dir():
            continue
        try:
            entries = sorted(directory.rglob("*"))
        except OSError:
            continue
        for path in entries:
            if path.suffix.lower() not in _MEDIA_CONTENT_TYPES:
                continue
            try:
                if not path.is_file() or path.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            found.append(path)

    artifacts: List[JsonDict] = []
    seen: set[str] = set()
    captured_total = 0
    for path in found:
        if len(artifacts) >= max_files:
            break
        remaining = total_limit - captured_total
        if remaining <= 0:
            break
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        captured = _capture_evidence_artifact(
            path,
            name=path.name,
            artifact_type="media",
            max_bytes=min(per_limit, remaining),
        )
        if captured is not None:
            artifacts.append(captured)
            captured_total += int(captured.get("size_bytes") or 0)
    return artifacts


def _write_host_control_text(path: Path, content: str, workspace: Path) -> None:
    """Write a host-owned workspace output without following agent symlinks."""

    workspace_resolved = workspace.resolve()
    try:
        path.parent.resolve().relative_to(workspace_resolved)
    except (OSError, ValueError):
        raise OSError("host control output is outside task workspace") from None
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and not stat.S_ISREG(info.st_mode):
        path.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("host control output is not a regular file")
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(content)
            handle.flush()
    finally:
        os.close(fd)


def _capture_evidence_artifact(
    path: Path,
    *,
    name: str,
    artifact_type: str,
    max_bytes: int,
    allowed_root: Optional[Path] = None,
) -> Optional[JsonDict]:
    try:
        source_info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(source_info.st_mode):
        return None
    try:
        resolved = path.resolve(strict=True)
        if allowed_root is not None:
            resolved.relative_to(allowed_root.resolve())
    except (OSError, ValueError):
        return None
    source_size = source_info.st_size
    source_digest = hashlib.sha256()
    captured = bytearray()
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != source_info.st_dev
            or opened.st_ino != source_info.st_ino
        ):
            os.close(fd)
            return None
        with os.fdopen(fd, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                source_digest.update(chunk)
                if len(captured) < max_bytes:
                    remaining = max_bytes - len(captured)
                    captured.extend(chunk[:remaining])
    except OSError:
        return None
    content = bytes(captured)
    content_digest = "sha256:%s" % hashlib.sha256(content).hexdigest()
    source_sha256 = "sha256:%s" % source_digest.hexdigest()
    truncated = source_size > len(content)
    return {
        "name": name,
        "artifact_type": artifact_type,
        "source_uri": resolved.as_uri(),
        "content_type": _artifact_content_type(path),
        "encoding": "base64",
        "size_bytes": len(content),
        "sha256": content_digest,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "truncated": truncated,
        "metadata": {
            "schema": "mac.evidence_artifact_capture.v1",
            "source_size_bytes": source_size,
            "source_sha256": source_sha256,
            "captured_size_bytes": len(content),
            "capture_limit_bytes": max_bytes,
        },
    }


def _durable_evidence_artifacts(task_dir: Path, primary_result_path: Path) -> List[JsonDict]:
    candidates = [
        (primary_result_path, primary_result_path.name, "result"),
        (task_dir / "repository-wip.json", "repository-wip.json", "repository_wip"),
        (task_dir / "stdout.txt", "stdout.txt", "stdout"),
        (task_dir / "stderr.txt", "stderr.txt", "stderr"),
        (task_dir / "mac-evidence.json", "mac-evidence.json", "verification_manifest"),
        (task_dir / "finalizer-progress.json", "finalizer-progress.json", "finalizer_progress"),
        (task_dir / "mac-sandbox-verification.json", "mac-sandbox-verification.json", "sandbox_verification"),
        (task_dir / "openshell-salvage.json", "openshell-salvage.json", "sandbox_salvage"),
        (task_dir / "repository-worktree.json", "repository-worktree.json", "repository_context"),
        (task_dir / "executor-evidence.json", "executor-evidence.json", "review_context"),
        (task_dir / "executor-task.json", "executor-task.json", "review_context"),
        (task_dir / "review-independent-findings.json", "review-independent-findings.json", "review_experiment"),
        (task_dir / "review-protocol.json", "review-protocol.json", "review_experiment"),
        (task_dir / "review-independent-draft-evidence.json", "review-independent-draft-evidence.json", "review_experiment"),
    ]
    try:
        wip_manifest = json.loads(
            (task_dir / "repository-wip.json").read_text(encoding="utf-8")
        )
    except Exception:
        wip_manifest = {}
    if isinstance(wip_manifest, dict):
        bundle_name = str(wip_manifest.get("bundle_name") or "").strip()
        if (
            re.fullmatch(r"repository-wip-[A-Za-z0-9_.-]{1,180}\.bundle", bundle_name)
            and "/" not in bundle_name
        ):
            candidates.insert(
                2,
                (
                    task_dir / bundle_name,
                    bundle_name,
                    "repository_wip_bundle",
                ),
            )
    artifacts: List[JsonDict] = []
    seen: set[str] = set()
    per_artifact_limit = _evidence_artifact_max_bytes()
    total_limit = _evidence_artifact_total_max_bytes()
    captured_total = 0
    for path, name, artifact_type in candidates:
        remaining = total_limit - captured_total
        if remaining <= 0:
            break
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        captured = _capture_evidence_artifact(
            path,
            name=name,
            artifact_type=artifact_type,
            max_bytes=min(per_artifact_limit, remaining),
            allowed_root=task_dir,
        )
        if captured is not None:
            artifacts.append(captured)
            captured_total += int(captured.get("size_bytes") or 0)
    # Media generated during the task (image/audio/video) becomes a durable
    # deliverable, on its own budget so it never crowds out the diagnostic
    # evidence above.
    for media in _durable_media_artifacts(task_dir):
        key = str(media.get("source_uri") or media.get("name") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        artifacts.append(media)
    return artifacts


def _default_self_update_repo() -> Path:
    configured = os.environ.get("MAC_SELF_UPDATE_REPO")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2]

def _startup_import_self_check(repo: Path) -> str:
    """Run 'import mac.services, mac.worker, mac.api' in a subprocess; non-fatal."""
    try:
        pythonpath = str(repo / "src")
        env = {**os.environ, "PYTHONPATH": pythonpath}
        result = subprocess.run(
            [sys.executable, "-c", "import mac.services, mac.worker, mac.api"],
            env=env,
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            return "ok"
        return "failed: " + result.stderr.decode("utf-8", errors="replace").strip()[:200]
    except Exception as exc:
        return "error: " + str(exc)


def _repository_task_origin(task: JsonDict) -> Optional[JsonDict]:
    metadata = task.get("metadata") if isinstance(task, dict) else None
    if not isinstance(metadata, dict):
        return None
    # Reports get no repository by default.  The one explicit exception is a
    # schema-versioned operator opt-in for a task-owned read-only inspection
    # checkout; it remains an operator_result task and never enters publication.
    if metadata_declares_report_deliverable(
        metadata
    ) and not metadata_declares_read_only_report_repository(metadata):
        return None
    origin = metadata.get("origin")
    if not isinstance(origin, dict):
        return None
    repository_path = str(origin.get("repository_path") or "").strip()
    repository_url = str(origin.get("repository_url") or "").strip()
    # mac-k8s clone path: allow tasks that ship only a remote URL (the
    # Job pod has no local source). Either a local path or a remote URL
    # is now sufficient to identify a repository-mode task.
    if not repository_path and not repository_url:
        return None

    # Dirty-source remediation tasks are the one explicit exception: their
    # purpose is to repair the registered checkout itself.
    remediation = metadata.get("remediation")
    if isinstance(remediation, dict) and remediation.get("type") == "beads_source_refresh":
        return None
    if origin.get("type") == "beads_source_remediation":
        return None

    contract = origin.get("repository_contract")
    execution_contract = metadata.get("execution_contract")
    if isinstance(execution_contract, dict) and execution_contract.get("type") == "repository":
        return dict(origin)
    if isinstance(contract, dict) and contract.get("schema"):
        return dict(origin)
    if str(origin.get("type") or "") in {"beads", "direct_task"}:
        return dict(origin)
    return None


def _current_repository_contract(task: JsonDict) -> JsonDict:
    """Return the normalized current repository contract attached to a task."""

    metadata = task.get("metadata") if isinstance(task, dict) else None
    if not isinstance(metadata, dict):
        return {}
    execution = metadata.get("execution_contract")
    origin = metadata.get("origin")
    candidates = (
        execution.get("repository_contract") if isinstance(execution, dict) else None,
        origin.get("repository_contract") if isinstance(origin, dict) else None,
        metadata.get("repository_contract"),
    )
    for candidate in candidates:
        if isinstance(candidate, dict):
            return copy.deepcopy(candidate)
    return {}


def _repository_context_is_read_only_report(context: JsonDict) -> bool:
    return (
        isinstance(context, dict)
        and context.get("repository_access_schema") == REPORT_REPOSITORY_ACCESS_SCHEMA
        and context.get("repository_access_mode") == REPORT_REPOSITORY_READ_ONLY_MODE
    )


_READ_ONLY_REPOSITORY_CONTEXT_KEYS = (
    "repository_access_schema",
    "repository_access_mode",
    "repository_worktree",
    "repository_base_sha",
    "repository_base_tree",
    "repository_refs_digest",
    "repository_content_digest",
    "repository_canonical_remote_url",
    "repository_canonical_branch",
)


def _trusted_read_only_repository_context(task: JsonDict) -> JsonDict:
    """Project the host-stamped inspection context from trusted task metadata."""

    metadata = task.get("metadata") if isinstance(task, dict) else None
    if not metadata_declares_read_only_report_repository(metadata):
        return {}
    runtime = metadata.get("runtime") if isinstance(metadata, dict) else None
    context = copy.deepcopy(runtime) if isinstance(runtime, dict) else {}
    context["repository_access_schema"] = REPORT_REPOSITORY_ACCESS_SCHEMA
    context["repository_access_mode"] = REPORT_REPOSITORY_READ_ONLY_MODE
    contract = _current_repository_contract(task)
    canonical_remote = str(contract.get("canonical_remote_url") or "").strip()
    canonical_branch = str(
        contract.get("default_branch") or contract.get("canonical_branch") or ""
    ).strip()
    if canonical_remote:
        context["repository_canonical_remote_url"] = canonical_remote
    if canonical_branch:
        context["repository_canonical_branch"] = canonical_branch
    return context


def _read_only_repository_context_drift_problems(
    trusted: JsonDict, serialized: JsonDict
) -> List[str]:
    """Reject agent-controlled context drift from the host-stamped projection."""

    if not serialized:
        return ["read-only repository context file is missing"]
    problems: List[str] = []
    for key in _READ_ONLY_REPOSITORY_CONTEXT_KEYS:
        if serialized.get(key) != trusted.get(key):
            problems.append("read-only repository context drifted at %s" % key)
    return problems


def _read_only_repository_access_evidence(context: JsonDict) -> JsonDict:
    return {
        "schema": REPORT_REPOSITORY_ACCESS_SCHEMA,
        "mode": REPORT_REPOSITORY_READ_ONLY_MODE,
        "canonical_remote_url": context.get("repository_canonical_remote_url"),
        "canonical_branch": context.get("repository_canonical_branch"),
        "base_sha": context.get("repository_base_sha"),
        "base_tree": context.get("repository_base_tree"),
        "refs_digest": context.get("repository_refs_digest"),
        "content_digest": context.get("repository_content_digest"),
    }


def _repository_source_candidates(origin: JsonDict, self_update_repo: Path) -> List[Path]:
    candidates: List[Path] = []
    raw = str(origin.get("repository_path") or "").strip()
    if raw:
        declared = Path(raw).expanduser()
        candidates.append(declared)
        parts = declared.parts
        if ".mac" in parts:
            idx = parts.index(".mac")
            suffix = Path(*parts[idx + 1 :]) if idx + 1 < len(parts) else Path()
            candidates.append(mac_paths.mac_home() / suffix)

    repository_name = str(origin.get("repository_name") or "").strip()
    if repository_name:
        candidates.append(mac_paths.mac_home() / "src" / _safe_path_component(repository_name))

    source = str(origin.get("source") or "").strip()
    contract = origin.get("repository_contract")
    project = str(contract.get("project") or "").strip() if isinstance(contract, dict) else ""
    if repository_name == "mac" or source == "repo-beads-mac":
        candidates.insert(0, self_update_repo.expanduser())
    elif project == "repo-beads-mac":
        candidates.append(self_update_repo.expanduser())

    seen = set()
    unique: List[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key and key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _task_worktree_branch(agent_id: str, task_id: str, lease_id: str) -> str:
    agent = _safe_path_component(agent_id).strip("._-/") or "agent"
    task = _safe_path_component(task_id).strip("._-/") or "task"
    lease = _safe_path_component(lease_id).strip("._-/") or "lease"
    branch = "mac/%s/%s-%s" % (agent[:32], task[:48], lease[:24])
    return branch[:127].rstrip("./-") or "mac/agent/task"


def _load_repository_context(task_dir: Path) -> JsonDict:
    path = task_dir / "repository-worktree.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


_BLIND_REVIEW_HIDDEN_METADATA_KEYS = frozenset(
    {
        "activity",
        "latest_review_claim",
        "model",
        "model_strength",
        "repository_ref_lifecycle",
        "review_claims",
        "review_context",
        "review_model",
        "review_model_strength",
        "runtime",
        "target_agent_id",
    }
)

_REVIEW_CLAIM_IDENTITY_KEYS = frozenset(
    {
        "actor",
        "claimed_at",
        "executor_evidence_id",
        "review_id",
        "reviewer_agent_id",
        "schema",
        "task_id",
    }
)


def _review_input_task(task: JsonDict) -> JsonDict:
    """Return the pre-execution task contract visible to semantic reviewers.

    Review claims, activity summaries, runtime publication anchors, and the
    executor model are post-execution treatment data. They must not be copied
    into ``executor-task.json`` or the review task metadata because the blind
    discovery pass can read both files while ``executor-evidence.json`` is
    withheld. Unknown task-authored metadata remains available so custom
    acceptance criteria are not lost.
    """
    safe = copy.deepcopy(task) if isinstance(task, dict) else {}
    metadata = safe.get("metadata")
    if isinstance(metadata, dict):
        for key in _BLIND_REVIEW_HIDDEN_METADATA_KEYS:
            metadata.pop(key, None)
        # Registered host paths are preparation inputs, not semantic-review
        # inputs. A reviewer receives only its task-owned exact-base checkout;
        # never reveal an alternate host/source path it could try to access.
        for container_key in ("origin", "execution_contract"):
            container = metadata.get(container_key)
            if isinstance(container, dict):
                container.pop("repository_path", None)
                contract = container.get("repository_contract")
                if isinstance(contract, dict):
                    contract.pop("repository_path", None)
    for key in (
        "attempt_count",
        "completed_at",
        "last_updated_at",
        "lease_id",
        "leased_until",
        "owner_agent_id",
        "started_at",
        "state",
        "updated_at",
    ):
        safe.pop(key, None)
    return safe


def _review_claim_identity(claim: JsonDict) -> JsonDict:
    """Keep claim identity needed by finalization without leaking evidence."""
    return {
        key: copy.deepcopy(value)
        for key, value in claim.items()
        if key in _REVIEW_CLAIM_IDENTITY_KEYS
    }


def _task_model_override(task: JsonDict, hub_client: Any = None) -> str:
    """Per-task LLM model override from task metadata.

    Executor tasks use ``metadata.model`` (flat, what ``mac task create
    --model`` writes). Review payloads deliberately do not inherit that model:
    they use ``metadata.review_model`` (or the corresponding runtime key) and
    otherwise fall back to the reviewer's fleet default. This preserves model
    independence instead of silently asking the reviewer to use the author's
    pinned model.
    ``metadata.model_strength`` (int 1..10) is the name-decoupled alternative:
    1 = cheapest/weakest, 10 = strongest, resolved to a concrete available model
    via the active strength ladder (so the task stays decoupled from model names
    as they churn). ``metadata.runtime.model`` is honored last. Empty string when
    the task pins nothing — the agent's fleet default applies.

    The ladder is resolved from the LOCAL selection store first (co-located hub
    process), then, if that is empty, from the hub's ``/model-selection/status``
    via ``hub_client`` — without that fallback a spoke worker (which has no local
    selection file) would silently ignore ``--model-strength`` and always drop to
    the fleet default."""
    metadata = task.get("metadata") if isinstance(task, dict) else None
    if not isinstance(metadata, dict):
        return ""
    is_review = isinstance(metadata.get("review_context"), dict)
    model_key = "review_model" if is_review else "model"
    strength_key = "review_model_strength" if is_review else "model_strength"
    value = str(metadata.get(model_key) or "").strip()
    if value:
        return value[:256]
    strength = metadata.get(strength_key)
    if strength is None and isinstance(metadata.get("runtime"), dict):
        strength = metadata["runtime"].get(strength_key)
    if strength is not None and str(strength).strip():
        try:
            scale = int(strength)
        except (TypeError, ValueError):
            scale = None
        if scale is not None:
            resolved = _resolve_strength_local_or_hub(scale, hub_client)
            if resolved:
                return resolved[:256]
    runtime = metadata.get("runtime")
    if isinstance(runtime, dict):
        return str(runtime.get(model_key) or "").strip()[:256]
    return ""


def _task_iteration_override(task: JsonDict) -> Optional[int]:
    """Resolve a bounded Hermes iteration budget from immutable task metadata.

    Review payloads use ``review_max_iterations`` so an experiment can bound
    each discovery/adjudication pass independently of the executor budget.
    Values outside 1..500 are ignored instead of producing an unsafe or
    effectively unbounded child process.
    """
    metadata = task.get("metadata") if isinstance(task, dict) else None
    if not isinstance(metadata, dict):
        return None
    is_review = isinstance(metadata.get("review_context"), dict)
    key = "review_max_iterations" if is_review else "max_iterations"
    value = metadata.get(key)
    if value is None and isinstance(metadata.get("runtime"), dict):
        value = metadata["runtime"].get(key)
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return None
    return resolved if 1 <= resolved <= 500 else None


def _resolve_strength_local_or_hub(scale: int, hub_client: Any = None) -> str:
    """Resolve a 1..10 strength to a concrete model via the LOCAL active ladder,
    falling back to the hub's ``/model-selection/status`` ladder for spoke
    workers that have no local selection file. Best-effort — "" on any failure."""
    try:
        from mac.model_selection import resolve_strength, resolve_strength_from_selection
    except Exception:  # noqa: BLE001
        return ""
    try:
        resolved = resolve_strength_from_selection(scale)
    except Exception:  # noqa: BLE001
        resolved = ""
    if resolved:
        return resolved
    if hub_client is not None:
        try:
            status = hub_client.get("/model-selection/status")
            ladder = (((status or {}).get("active") or {}).get("ladder")) or []
            return resolve_strength(scale, [str(m) for m in ladder if str(m).strip()])
        except Exception:  # noqa: BLE001 - hub fallback is best-effort.
            return ""
    return ""


def _task_payload_from_workspace(task_dir: Path) -> JsonDict:
    path = task_dir / "task.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    task = loaded.get("task")
    return task if isinstance(task, dict) else loaded


def _work_package_assignment_projection(
    task: JsonDict,
    context: Optional[JsonDict] = None,
) -> JsonDict:
    """Return and validate the controller-authored exact-attempt projection.

    The projection is worker routing input, never integration authority.  Its
    exact lease/ref/base identity is rejoined to the immutable assignment audit
    when evidence reaches the hub, and the controller independently fetches the
    protected ref before accepting any candidate.
    """

    metadata = ensure_json_object(task.get("metadata"))
    raw = metadata.get("work_package_assignment")
    if raw is None:
        return {}
    assignment = ensure_json_object(raw)
    if assignment.get("schema") != "mac.work_package.assignment_projection.v1":
        raise ValueError("work-package assignment projection has an invalid schema")
    required = (
        "package_id",
        "node_key",
        "task_id",
        "agent_id",
        "lease_id",
        "attempt_ref",
        "attempt_base_ref",
        "attempt_base_sha",
        "declared_effects_digest",
    )
    missing = [name for name in required if not str(assignment.get(name) or "").strip()]
    if missing:
        raise ValueError(
            "work-package assignment projection is incomplete: %s"
            % ", ".join(sorted(missing))
        )
    if str(assignment["task_id"]) != str(task.get("id") or ""):
        raise ValueError("work-package assignment task identity does not match")
    owner = str(task.get("owner_agent_id") or "").strip()
    if owner and str(assignment["agent_id"]) != owner:
        raise ValueError("work-package assignment agent identity does not match")
    if context is not None:
        lease_id = str(context.get("repository_lease_id") or "").strip()
        if not lease_id or str(assignment["lease_id"]) != lease_id:
            raise ValueError("work-package assignment lease identity does not match")
    try:
        attempt_number = int(assignment.get("attempt_number"))
        plan_version = int(assignment.get("plan_version"))
        epoch = int(assignment.get("epoch"))
    except (TypeError, ValueError) as exc:
        raise ValueError("work-package assignment counters are invalid") from exc
    if min(attempt_number, plan_version, epoch) < 1:
        raise ValueError("work-package assignment counters must be positive")
    attempt_ref = validate_git_ref(str(assignment["attempt_ref"]))
    if not attempt_ref.startswith("refs/mac/attempts/"):
        raise ValueError("work-package assignment ref is outside refs/mac/attempts")
    base_sha = str(assignment["attempt_base_sha"])
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base_sha):
        raise ValueError("work-package assignment base is not a full lowercase object id")
    return dict(assignment)


def _task_detail_evidence(task_detail: JsonDict, evidence_id: str) -> JsonDict:
    evidence_items = task_detail.get("evidence")
    if not isinstance(evidence_items, list):
        return {}
    for item in evidence_items:
        if isinstance(item, dict) and str(item.get("id") or "") == evidence_id:
            return item
    return {}


def _task_detail_canonical_remote_url(task_detail: JsonDict) -> str:
    task = ensure_json_object(task_detail.get("task"))
    metadata = ensure_json_object(task.get("metadata"))
    candidates = (
        ensure_json_object(
            ensure_json_object(metadata.get("execution_contract")).get(
                "repository_contract"
            )
        ),
        ensure_json_object(
            ensure_json_object(metadata.get("origin")).get("repository_contract")
        ),
        ensure_json_object(metadata.get("repository_contract")),
        ensure_json_object(metadata.get("origin")),
    )
    for candidate in candidates:
        remote_url = str(
            candidate.get("canonical_remote_url")
            or candidate.get("repository_url")
            or ""
        ).strip()
        if remote_url:
            return remote_url
    return ""


def _repository_context_env(context: JsonDict) -> Dict[str, str]:
    mapping = {
        "MAC_TASK_REPO_WORKTREE": context.get("repository_worktree"),
        "MAC_TASK_REPO_SOURCE": context.get("repository_source_path"),
        "MAC_TASK_REPO_BRANCH": context.get("repository_branch"),
        "MAC_TASK_REPO_LEASE_ID": context.get("repository_lease_id"),
        "MAC_TASK_REPO_BASE_SHA": context.get("repository_base_sha"),
        "MAC_TASK_REPO_BASE_TREE": context.get("repository_base_tree"),
        "MAC_TASK_REPO_REFS_DIGEST": context.get("repository_refs_digest"),
        "MAC_TASK_REPO_CONTENT_DIGEST": context.get("repository_content_digest"),
        "MAC_TASK_REPO_REMOTE": context.get("repository_origin_remote"),
        "MAC_TASK_CANONICAL_REMOTE": context.get("repository_canonical_remote_url"),
        "MAC_TASK_REPO_DEFAULT_BRANCH": context.get("repository_canonical_branch"),
        "MAC_TASK_REPO_ACCESS_MODE": context.get("repository_access_mode"),
        "MAC_TASK_REPO_ACCESS_SCHEMA": context.get("repository_access_schema"),
    }
    return {key: str(value) for key, value in mapping.items() if value not in {None, ""}}


def _enrich_verification_manifest_from_repository_context(
    manifest: JsonDict,
    context: JsonDict,
    *,
    task: Optional[JsonDict] = None,
) -> JsonDict:
    if not manifest or not context:
        return manifest
    enriched = dict(manifest)
    repo_value = manifest.get("repo")
    repo = dict(repo_value) if isinstance(repo_value, dict) else {}
    package_assignment = _work_package_assignment_projection(task or {}, context)
    if context.get("checkout_policy") == "review_git_worktree" and repo:
        reviewed_ref = str(context.get("repository_reviewed_remote_ref") or "").strip()
        branch = str(context.get("repository_branch") or "").strip()
        remote_ref = reviewed_ref or branch
        if remote_ref and not remote_ref.startswith("refs/"):
            remote_ref = "refs/heads/%s" % remote_ref
        defaults = {
            "path": context.get("repository_worktree"),
            "remote_url": (
                context.get("repository_canonical_remote_url")
                or context.get("repository_origin_remote")
            ),
            "branch": reviewed_ref or branch,
            "base_sha": context.get("repository_base_sha"),
            "remote_ref": remote_ref,
            "head_sha": context.get("repository_reviewed_head_sha")
            or context.get("repository_base_sha"),
        }
        for key, value in defaults.items():
            if value not in {None, ""} and repo.get(key) in {None, ""}:
                repo[key] = value
        worktree_raw = str(context.get("repository_worktree") or "").strip()
        worktree = Path(worktree_raw).expanduser() if worktree_raw else None
        if "pushed" not in repo and worktree is not None and worktree.exists():
            repo["pushed"] = _repository_context_head_is_pushed(worktree, repo)
        enriched["repo"] = repo
        return enriched
    worktree_raw = str(context.get("repository_worktree") or "").strip()
    worktree = Path(worktree_raw).expanduser() if worktree_raw else None
    if worktree is not None and worktree.exists():
        head = _run_git(worktree, ["rev-parse", "HEAD"])
        if head.returncode == 0 and head.stdout.strip():
            repo["head_sha"] = head.stdout.strip()
        status = _run_git(worktree, ["status", "--porcelain"])
        if status.returncode == 0:
            repo["dirty"] = bool(status.stdout.strip())
        repo["files_changed"] = _repository_context_changed_files(worktree, context)

    defaults = {
        "path": context.get("repository_worktree"),
        "remote_url": (
            context.get("repository_canonical_remote_url")
            or context.get("repository_origin_remote")
        ),
        "branch": context.get("repository_branch"),
        "base_sha": context.get("repository_base_sha"),
    }
    if context.get("repository_branch"):
        _branch = str(context.get("repository_branch"))
        # repository_branch may already be a full ref (refs/heads/...); don't
        # re-prefix it into refs/heads/refs/heads/... (mac review-worktree fix)
        defaults["remote_ref"] = (
            _branch if _branch.startswith("refs/") else "refs/heads/%s" % _branch
        )
    if package_assignment:
        # Package workers never choose a mutable review branch.  Overwrite any
        # agent-supplied routing fields with the controller projection and then
        # recompute `pushed` from the exact remote ref below.
        defaults.update(
            {
                "branch": package_assignment["attempt_ref"],
                "base_sha": package_assignment["attempt_base_sha"],
                "remote_ref": package_assignment["attempt_ref"],
            }
        )
    for key, value in defaults.items():
        if value not in {None, ""}:
            repo[key] = value
    if worktree is not None and worktree.exists():
        repo["pushed"] = _repository_context_head_is_pushed(worktree, repo)
    enriched["repo"] = repo
    return enriched


def _repository_context_changed_files(worktree: Path, context: JsonDict) -> List[str]:
    base_sha = str(context.get("repository_base_sha") or "").strip()
    candidates: List[str] = []
    if base_sha:
        diff = _run_git(worktree, ["diff", "--name-only", "%s...HEAD" % base_sha])
        if diff.returncode != 0:
            diff = _run_git(worktree, ["diff", "--name-only", base_sha, "HEAD"])
        if diff.returncode == 0:
            candidates.extend(line.strip() for line in diff.stdout.splitlines())
    for args in (["diff", "--name-only"], ["diff", "--cached", "--name-only"]):
        diff = _run_git(worktree, args)
        if diff.returncode == 0:
            candidates.extend(line.strip() for line in diff.stdout.splitlines())
    return sorted({item for item in candidates if item})


def _git_stdout(worktree: Path, args: List[str]) -> str:
    result = _run_git(worktree, args)
    return result.stdout.strip() if result.returncode == 0 else ""


def _repository_worktree_is_dirty(worktree: Path) -> bool:
    status = _run_git(worktree, ["status", "--porcelain"])
    return status.returncode != 0 or bool(status.stdout.strip())


def _read_only_repository_problems(worktree: Path, context: JsonDict) -> List[str]:
    """Return mutation/isolation failures for a report inspection checkout."""

    if not str(context.get("repository_worktree") or "").strip():
        return ["read-only repository worktree is not declared"]
    if not worktree.exists():
        return ["read-only repository worktree is missing: %s" % worktree]
    problems: List[str] = []
    status = _run_git(worktree, ["status", "--porcelain"])
    if status.returncode != 0:
        problems.append(
            "could not inspect read-only repository worktree status: %s"
            % ((status.stderr or status.stdout or "").strip() or worktree)
        )
    elif status.stdout.strip():
        problems.append("read-only repository worktree was mutated")
    head = _run_git(worktree, ["rev-parse", "HEAD"])
    base_sha = str(context.get("repository_base_sha") or "").strip()
    if head.returncode != 0 or not head.stdout.strip():
        problems.append("could not resolve read-only repository worktree HEAD")
    elif not base_sha or head.stdout.strip() != base_sha:
        problems.append("read-only repository worktree HEAD changed from its prepared base")
    tree = _run_git(worktree, ["rev-parse", "HEAD^{tree}"])
    base_tree = str(context.get("repository_base_tree") or "").strip()
    if tree.returncode != 0 or not base_tree or tree.stdout.strip() != base_tree:
        problems.append("read-only repository worktree tree changed from its prepared base")
    refs = _run_git(
        worktree,
        ["for-each-ref", "--format=%(refname) %(objectname)"],
    )
    refs_digest = str(context.get("repository_refs_digest") or "").strip()
    observed_refs_digest = (
        hashlib.sha256(refs.stdout.encode("utf-8")).hexdigest()
        if refs.returncode == 0
        else ""
    )
    if (
        refs.returncode != 0
        or not refs_digest
        or observed_refs_digest != refs_digest
    ):
        problems.append("read-only repository refs changed from their prepared state")
    remotes = _run_git(worktree, ["remote"])
    if remotes.returncode != 0:
        problems.append("could not verify read-only repository remote isolation")
    elif remotes.stdout.strip():
        problems.append("read-only repository worktree retained a publication remote")
    # Repository-owned build/test commands may leave ignored disposable output.
    # Only clean after the ordinary status gate proves there are no tracked or
    # untracked edits; never reset a source mutation. CodeGraph is a permitted
    # generated analysis cache and is intentionally retained/excluded.
    if status.returncode == 0 and not status.stdout.strip():
        cleaned = _run_git(worktree, ["clean", "-fdx", "-e", ".codegraph/"])
        if cleaned.returncode != 0:
            problems.append("could not clean read-only repository disposable outputs")
    expected_content_digest = str(
        context.get("repository_content_digest") or ""
    ).strip()
    try:
        observed_content_digest = read_only_repository_content_digest(worktree)
    except OSError as exc:
        observed_content_digest = ""
        problems.append(
            "could not hash read-only repository content: %s" % str(exc)
        )
    if (
        not expected_content_digest
        or observed_content_digest != expected_content_digest
    ):
        problems.append(
            "read-only repository content changed from its prepared state"
        )
    return problems


def _repository_context_repo_snapshot(context: JsonDict) -> JsonDict:
    worktree_raw = str(context.get("repository_worktree") or "").strip()
    worktree = Path(worktree_raw).expanduser() if worktree_raw else None
    branch = str(context.get("repository_branch") or "").strip()
    repo: JsonDict = {
        "path": context.get("repository_worktree"),
        # Store the CANONICAL remote (no injected auth, no redaction) so every
        # consumer — the reviewer's clone, the hub publish — validates it and
        # injects credentials itself. repository_origin_remote is the redacted
        # DISPLAY string ("https://x-access-token:<redacted>@…"); once
        # inject_git_remote_auth began tokenizing SSH remotes, that redacted
        # form reached the reviewer's _validate_git_remote_url and failed
        # ("<redacted>" is not a valid URL char), so no review verdict could
        # ever be produced. The canonical form (git@host:… or clean https)
        # validates and re-auths cleanly.
        "remote_url": (
            context.get("repository_canonical_remote_url")
            or context.get("repository_origin_remote")
        ),
        "branch": branch,
        "base_sha": context.get("repository_base_sha"),
        "head_sha": context.get("repository_base_sha"),
        # branch may already be a full ref; avoid doubling the prefix.
        "remote_ref": (
            branch if branch.startswith("refs/") else "refs/heads/%s" % branch
        )
        if branch
        else "",
        "dirty": True,
        "pushed": False,
        "files_changed": [],
    }
    if worktree is not None and worktree.exists():
        head = _git_stdout(worktree, ["rev-parse", "HEAD"])
        if head:
            repo["head_sha"] = head
        repo["dirty"] = _repository_worktree_is_dirty(worktree)
        repo["files_changed"] = _repository_context_changed_files(worktree, context)
    return repo


def _append_codegraph_audit_check(manifest: JsonDict, audit: JsonDict) -> None:
    if str(audit.get("status") or "") == "skipped":
        return
    checks = manifest.get("checks")
    if not isinstance(checks, list):
        checks = []
        manifest["checks"] = checks
    checks[:] = [
        item
        for item in checks
        if not (isinstance(item, dict) and item.get("name") == "codegraph_audit")
    ]
    checks.append(codegraph_audit_check(audit))


def _attach_repository_codegraph_audit(manifest: JsonDict, context: JsonDict) -> JsonDict:
    if not context:
        return manifest
    worktree_raw = str(context.get("repository_worktree") or "").strip()
    worktree = Path(worktree_raw).expanduser() if worktree_raw else None
    if worktree is None or not worktree.exists():
        return manifest
    repo = manifest.get("repo") if isinstance(manifest.get("repo"), dict) else {}
    files_changed = _metadata_path_list(repo.get("files_changed")) if isinstance(repo, dict) else []
    if not files_changed:
        files_changed = _repository_context_changed_files(worktree, context)
    candidate = dict(manifest)
    candidate["repo"] = {**repo, "files_changed": files_changed}
    if not codegraph_audit_manifest_problems(candidate):
        return manifest
    audit = run_codegraph_audit(worktree, files_changed)
    if not isinstance(manifest.get("repo"), dict):
        manifest["repo"] = {}
    manifest["repo"]["files_changed"] = files_changed
    manifest["codegraph"] = audit
    _append_codegraph_audit_check(manifest, audit)
    return manifest


def _repository_contract_test_command(task: JsonDict) -> str:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    if not isinstance(metadata, dict):
        return ""
    if metadata_declares_read_only_report_repository(metadata):
        # Read-only reports may execute only the test command in their current
        # execution contract.  Historical origin/top-level contracts are not
        # authority for executable verification code.
        current = _nested_dict(
            metadata, "execution_contract", "repository_contract", "test"
        )
        return str(current.get("command") or "").strip()
    candidates = [
        _nested_dict(metadata, "execution_contract", "test"),
        _nested_dict(metadata, "execution_contract", "repository_contract", "test"),
        _nested_dict(metadata, "origin", "repository_contract", "test"),
        _nested_dict(metadata, "repository_contract", "test"),
    ]
    for candidate in candidates:
        command = str(candidate.get("command") or "").strip()
        if command:
            return command
    return ""


def _repository_contract_canonical_remote(task: JsonDict) -> str:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    if not isinstance(metadata, dict):
        return ""
    candidates = [
        _nested_dict(metadata, "execution_contract", "repository_contract"),
        _nested_dict(metadata, "origin", "repository_contract"),
        _nested_dict(metadata, "repository_contract"),
    ]
    for candidate in candidates:
        remote = str(candidate.get("canonical_remote_url") or "").strip()
        if remote:
            return remote
    return ""


def _repository_publication_remote(task: JsonDict, context: Optional[JsonDict] = None) -> str:
    """Return the explicit task/preparation canonical URL, without fallback.

    The prepared context intentionally stores only a display-safe URL. Reading
    the raw target from the task contract keeps credentials out of workspace
    metadata while allowing the publication guard to resolve the exact remote.
    """
    canonical = _repository_contract_canonical_remote(task)
    if canonical:
        return canonical
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    origin = metadata.get("origin") if isinstance(metadata, dict) else {}
    if isinstance(origin, dict):
        remote = str(origin.get("repository_url") or "").strip()
        if remote:
            return remote
    if isinstance(context, dict):
        return str(context.get("repository_canonical_remote_url") or "").strip()
    return ""


def _repository_push_remote(task: JsonDict, context: JsonDict) -> tuple[str, str]:
    remote = _repository_publication_remote(task, context)
    authed = _inject_git_remote_auth(remote)
    return authed, _redact_git_remote_auth(authed)


def _publish_exact_work_package_attempt(
    worktree: Path,
    canonical_remote: str,
    attempt_ref: str,
    head_sha: str,
) -> JsonDict:
    """Create one immutable attempt ref and prove its exact remote object.

    A retry may observe the same already-created ref after a worker crash.  It
    may reuse that ref only when it names the exact local HEAD; a different
    object is a hard collision.  First creation uses a create-only lease so two
    publishers cannot race into a silent overwrite.
    """

    clean_remote = validate_git_remote_url(canonical_remote)
    ref = validate_git_ref(attempt_ref)
    if not ref.startswith("refs/mac/attempts/"):
        raise ValueError("work-package attempt ref is outside refs/mac/attempts")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head_sha):
        raise ValueError("work-package attempt head is not a full lowercase object id")
    remote = _inject_git_remote_auth(clean_remote)
    display = _redact_git_remote_auth(remote)

    def observe() -> tuple[Optional[str], subprocess.CompletedProcess[str]]:
        result = _run_git(worktree, ["ls-remote", remote, ref])
        observed: Optional[str] = None
        if result.returncode == 0:
            rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
            exact = [parts[0] for parts in rows if len(parts) == 2 and parts[1] == ref]
            if len(exact) == 1:
                observed = exact[0].lower()
            elif exact:
                observed = "ambiguous"
        return observed, result

    observed, initial = observe()
    if initial.returncode != 0:
        return {
            "ok": False,
            "remote_verified": False,
            "remote_display": display,
            "attempt_ref": ref,
            "head_sha": head_sha,
            "error": "could not inspect protected attempt ref",
            "stdout": _redact_git_remote_auth_in_text(initial.stdout),
            "stderr": _redact_git_remote_auth_in_text(initial.stderr),
        }
    if observed and observed != head_sha:
        return {
            "ok": False,
            "remote_verified": False,
            "remote_display": display,
            "attempt_ref": ref,
            "head_sha": head_sha,
            "observed_head_sha": observed,
            "error": "protected attempt ref already names a different object",
            "stdout": "",
            "stderr": "",
        }

    push: Optional[subprocess.CompletedProcess[str]] = None
    if observed is None:
        push = _run_git(
            worktree,
            [
                "push",
                "--porcelain",
                "--force-with-lease=%s:" % ref,
                remote,
                "HEAD:%s" % ref,
            ],
        )
        if push.returncode != 0:
            return {
                "ok": False,
                "remote_verified": False,
                "remote_display": display,
                "attempt_ref": ref,
                "head_sha": head_sha,
                "error": "create-only protected attempt push failed",
                "stdout": _redact_git_remote_auth_in_text(push.stdout),
                "stderr": _redact_git_remote_auth_in_text(push.stderr),
            }

    readback, final = observe()
    verified = final.returncode == 0 and readback == head_sha
    return {
        "ok": verified,
        "remote_verified": verified,
        "remote_display": display,
        "attempt_ref": ref,
        "head_sha": head_sha,
        "observed_head_sha": readback or "",
        "already_present": observed == head_sha,
        "error": "" if verified else "protected attempt ref readback did not match",
        "stdout": _redact_git_remote_auth_in_text(push.stdout if push else ""),
        "stderr": _redact_git_remote_auth_in_text(
            (push.stderr if push else "") or final.stderr
        ),
    }


# --- Option A vs Option C decision ---
#
# Option A (original): the worker runs the repository contract test
# (scripts/run-contract-tests.sh) inside the agent's own OpenShell sandbox
# before pushing the branch.  The sandbox is per-node and requires a working
# coding-agent CLI (Claude Code, Codex, Cursor) provisioned on every fleet
# member.  This turned out to be fragile: per-host sandbox setup variability
# and CLI auth state caused test runs to fail non-deterministically, stalling
# the autonomous dispatch→review→merge loop.
#
# Option C (current): the worker defers the contract test and pushes the
# branch immediately.  The hub then runs the test once in its own controlled
# OpenShell sandbox (the auto-registered hub-reviewer agent) and records the
# signed verdict.  This concentrates the test-execution environment on a
# single, operator-managed node (the hub) instead of requiring a clean CLI
# auth on every spoke, eliminating the per-node variability that caused
# Option A to stall.  The four env vars that activate this path are:
#   MAC_REVIEW_HUB_VERIFY=1          — enables deferred mode in the worker
#   MAC_HUB_REVIEWER_AUTO_REGISTER=1 — hub auto-registers the reviewer agent
#   MAC_HUB_REVIEWER_AGENT_NAME      — stable name for the hub reviewer
#   MAC_HUB_REVIEWER_AGENT_ID        — stable id for the hub reviewer
# All four are set by deploy_env.py for hub nodes only (is_hub=True).
# The deferred path is detected in _sandbox_repository_verification_item via
# _hub_verify_deferred_test_item / _is_hub_verify_deferred_item below.
# ---
def _repository_finalizer_prepush_problems(
    task: JsonDict,
    repo: JsonDict,
    test_item: JsonDict,
    *,
    codegraph: Optional[JsonDict] = None,
    hub_verify: bool = False,
) -> List[str]:
    problems: List[str] = []
    head_sha = str(repo.get("head_sha") or "").strip()
    if not GIT_SHA_RE.match(head_sha):
        problems.append("repo.head_sha must be a git SHA")
    if repo.get("dirty") is not False:
        problems.append("repo evidence must declare dirty=false")
    files_changed = _manifest_list(repo.get("files_changed"))
    if not files_changed and not _worker_allows_empty_repo_change_evidence(task, "repo_change"):
        problems.append("repo evidence requires changed files")
    # When hub-verify mode is active and the test item is the deferred sentinel,
    # skip the passing-test gate — the hub finalizer will run the contract test
    # after the branch is pushed.  All other prepush checks (head_sha, dirty,
    # files_changed, codegraph) are still enforced.
    if hub_verify and _is_hub_verify_deferred_item(test_item):
        pass  # test gate intentionally skipped in hub-verify deferred mode
    elif _worker_verification_item_passed(test_item) is not True:
        problems.append("repo code evidence requires at least one passing test/check")
    if codegraph is not None:
        problems.extend(codegraph_audit_manifest_problems({"repo": repo, "codegraph": codegraph}))
    problems.extend(_worker_required_changed_file_problems(task, {"repo": repo}))
    return problems


def _hub_verify_deferred_test_item(command: str) -> JsonDict:
    """Return the deferred sentinel emitted when MAC_REVIEW_HUB_VERIFY=1 and no
    sandbox result is available.  The hub finalizer will run the contract test
    after the branch is pushed; the worker must not block on it."""
    return {
        "name": "repository contract test",
        "command": command,
        "returncode": None,
        "status": "deferred",
        "execution_environment": "hub_verify_pending",
        "stdout": "",
        "stderr": "",
    }


def _is_hub_verify_deferred_item(item: Any) -> bool:
    """True iff *item* is the deferred sentinel produced by hub-verify mode."""
    if not isinstance(item, dict):
        return False
    return (
        str(item.get("status") or "").strip().lower() == "deferred"
        and str(item.get("execution_environment") or "").strip().lower()
        == "hub_verify_pending"
    )


def _sandbox_repository_verification_item(
    task_dir: Optional[Path],
    command: str,
    *,
    hub_verify: bool = False,
    require_command_match: bool = False,
) -> Optional[JsonDict]:
    if task_dir is None:
        if hub_verify:
            return _hub_verify_deferred_test_item(command)
        return None
    path = task_dir / "mac-sandbox-verification.json"
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise OSError("sandbox verification evidence is not a regular file")
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        if hub_verify:
            return _hub_verify_deferred_test_item(command)
        return None
    if not isinstance(loaded, dict):
        if hub_verify:
            return _hub_verify_deferred_test_item(command)
        return None
    observed_command = str(loaded.get("command") or "").strip()
    record_problem = ""
    if str(loaded.get("schema") or "").strip() != "mac.sandbox_verification.v1":
        record_problem = "sandbox verification record has an invalid schema"
    elif require_command_match and observed_command != command:
        record_problem = (
            "sandbox verification command does not match the repository contract"
        )
    try:
        returncode = int(loaded.get("returncode"))
    except (TypeError, ValueError):
        returncode = 1
    if record_problem:
        returncode = 1
    item = _process_check_item(
        "repository contract test",
        returncode,
        command=command if require_command_match else (observed_command or command),
        stdout=str(loaded.get("stdout") or ""),
        stderr="\n".join(
            part
            for part in (str(loaded.get("stderr") or "").strip(), record_problem)
            if part
        ),
    )
    item["execution_environment"] = "openshell_sandbox"
    if isinstance(loaded.get("environment_delta"), dict):
        item["environment_delta"] = loaded["environment_delta"]
    if loaded.get("worktree"):
        item["worktree"] = loaded.get("worktree")
    return item


def _trusted_read_only_report_test_item(
    task_dir: Path, task: JsonDict
) -> tuple[Optional[JsonDict], List[str]]:
    """Return the host-harvested OpenShell contract result and hard failures."""

    command = _repository_contract_test_command(task)
    if not command:
        return None, [
            "read-only repository report current contract lacks test.command"
        ]
    item = _sandbox_repository_verification_item(
        task_dir,
        command,
        require_command_match=True,
    )
    if item is None:
        return None, [
            "read-only repository report lacks trusted OpenShell contract test evidence"
        ]
    if _worker_verification_item_passed(item) is not True:
        return item, ["read-only repository report contract test did not pass"]
    return item, []


def _attach_trusted_read_only_report_test(
    manifest: JsonDict, task_dir: Path, task: JsonDict
) -> tuple[JsonDict, List[str]]:
    item, problems = _trusted_read_only_report_test_item(task_dir, task)
    candidate = dict(manifest)
    # These fields are host-owned for the read-only lane. A model can describe
    # its analysis in result/summary, but cannot claim that the repository's
    # executable contract passed.
    candidate["tests"] = [dict(item)] if item is not None else []
    candidate["checks"] = [dict(item)] if item is not None else []
    return candidate, problems


def _process_check_item(
    name: str,
    returncode: int,
    *,
    command: str,
    stdout: str,
    stderr: str,
) -> JsonDict:
    return {
        "name": name,
        "command": command,
        "returncode": int(returncode),
        "status": "pass" if int(returncode) == 0 else "fail",
        "stdout": _truncate_process_text(stdout),
        "stderr": _truncate_process_text(stderr),
    }


def _repository_context_head_is_pushed(worktree: Path, repo: JsonDict) -> bool:
    head_sha = str(repo.get("head_sha") or "").strip()
    remote_url = str(repo.get("remote_url") or "").strip()
    remote_ref = str(repo.get("remote_ref") or "").strip()
    if not head_sha or not remote_ref:
        return False
    if remote_url:
        remote = _run_git(worktree, ["ls-remote", remote_url, remote_ref])
        if remote.returncode == 0:
            for line in remote.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == head_sha and parts[1] == remote_ref:
                    return True
    remote = _run_git(worktree, ["ls-remote", "origin", remote_ref])
    if remote.returncode == 0:
        for line in remote.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == head_sha and parts[1] == remote_ref:
                return True
    branch = _remote_branch_from_ref(remote_ref)
    if branch:
        remote_head = _run_git(worktree, ["rev-parse", "--verify", "origin/%s" % branch])
        if remote_head.returncode == 0 and remote_head.stdout.strip() == head_sha:
            return True
    return False


def _repository_context_audit_metadata(context: JsonDict) -> JsonDict:
    if not context:
        return {}
    return {
        "repository_checkout_policy": context.get("checkout_policy"),
        "repository_worktree": context.get("repository_worktree"),
        "repository_source_path": context.get("repository_source_path"),
        "repository_branch": context.get("repository_branch"),
        "repository_base_sha": context.get("repository_base_sha"),
        "repository_access_mode": context.get("repository_access_mode"),
        "repository_access_schema": context.get("repository_access_schema"),
    }


def _safe_git_ref(value: str) -> bool:
    return bool(value and not value.startswith("-") and re.match(SAFE_GIT_REF_RE, value))


def _normalize_restart_services(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    raw_items = value if isinstance(value, list) else [value]
    services: List[str] = []
    for raw in raw_items:
        service = str(raw or "").strip()
        if not service:
            continue
        if service.startswith("-") or not re.match(SAFE_SYSTEMD_SERVICE_RE, service):
            raise ValueError("invalid systemd service name: %s" % service)
        if service not in services:
            services.append(service)
    return services


def _bounded_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _bounded_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _manifest_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _metadata_path_list(value: Any) -> List[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        return []
    paths: List[str] = []
    seen = set()
    for item in values:
        path = _normalize_repo_relative_path(item)
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _nested_dict(root: JsonDict, *keys: str) -> JsonDict:
    node: Any = root
    for key in keys:
        if not isinstance(node, dict):
            return {}
        node = node.get(key)
    return dict(node) if isinstance(node, dict) else {}


def _required_changed_files_from_task(task: JsonDict) -> List[str]:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    if not isinstance(metadata, dict):
        return []
    containers = [
        metadata,
        _nested_dict(metadata, "acceptance"),
        _nested_dict(metadata, "execution_contract"),
        _nested_dict(metadata, "execution_contract", "evidence"),
        _nested_dict(metadata, "execution_contract", "repository_contract"),
        _nested_dict(metadata, "execution_contract", "repository_contract", "evidence"),
        _nested_dict(metadata, "origin", "repository_contract"),
        _nested_dict(metadata, "origin", "repository_contract", "evidence"),
        _nested_dict(metadata, "repository_contract"),
        _nested_dict(metadata, "repository_contract", "evidence"),
    ]
    required: List[str] = []
    seen = set()
    for container in containers:
        if not container:
            continue
        for key in REQUIRED_CHANGED_FILE_KEYS:
            for path in _metadata_path_list(container.get(key)):
                if path not in seen:
                    seen.add(path)
                    required.append(path)
    return required


def _worker_required_changed_file_problems(task: JsonDict, manifest: JsonDict) -> List[str]:
    required = _required_changed_files_from_task(task)
    if not required:
        return []
    repo = manifest.get("repo") if isinstance(manifest.get("repo"), dict) else {}
    changed = _metadata_path_list(repo.get("files_changed")) if isinstance(repo, dict) else []
    missing = [
        path
        for path in required
        if not any(_repo_path_satisfies_requirement(item, path) for item in changed)
    ]
    if not missing:
        return []
    return ["repo evidence missing required changed files: %s" % ", ".join(missing)]


def _worker_verification_contract_problems(
    manifest: JsonDict,
    evidence_type: str,
    *,
    allow_empty_repo_change: bool = False,
) -> List[str]:
    if evidence_type == "repo_change":
        return _worker_repo_verification_problems(
            manifest,
            require_tests=True,
            allow_empty_repo_change=allow_empty_repo_change,
        )
    if evidence_type == "documentation":
        return _worker_repo_verification_problems(manifest, require_tests=False)
    if evidence_type == "deployment":
        problems = _worker_require_pushed_repo_anchor(manifest)
        if _worker_passed_verification_check_count(manifest) < 1:
            problems.append("deployment evidence requires at least one passing check")
        if not (
            _manifest_list(manifest.get("targets"))
            or _manifest_list(manifest.get("services"))
            or _manifest_list(manifest.get("artifacts"))
        ):
            problems.append("deployment evidence requires targets, services, or artifacts")
        problems.extend(codegraph_audit_manifest_problems(manifest))
        return problems
    if evidence_type in {"test", "artifact"}:
        problems = _worker_require_pushed_repo_anchor(manifest)
        if _worker_passed_verification_check_count(manifest) < 1:
            problems.append("%s evidence requires at least one passing check or test" % evidence_type)
        if evidence_type == "artifact" and not _manifest_list(manifest.get("artifacts")):
            problems.append("artifact evidence requires artifacts")
        problems.extend(codegraph_audit_manifest_problems(manifest))
        return problems
    if evidence_type == "no_change":
        problems = _worker_require_clean_repo_anchor(manifest)
        if not str(manifest.get("reason") or manifest.get("no_change_reason") or "").strip():
            problems.append("no_change evidence requires a reason")
        if _worker_passed_verification_check_count(manifest) < 1:
            problems.append("no_change evidence requires at least one passing check")
        problems.extend(codegraph_audit_manifest_problems(manifest))
        return problems
    if evidence_type == "review_verdict":
        return codegraph_audit_manifest_problems(manifest)
    if evidence_type == "operator_result":
        # autonomy-loop fix: mirror the server's substance gate so the worker
        # pre-check fails chatter ("hello hello hello") / placeholder evidence
        # locally and fails the task cleanly, instead of submitting it and
        # crashing on the server's 400. One definition of "substantive" —
        # reused from evidence_validators so worker and server can't drift.
        from mac.evidence_validators import operator_result_validation_problems

        return operator_result_validation_problems(manifest)
    if evidence_type in {"investigation", "plan_decomposed"}:
        from mac.evidence_validators import validate_evidence_type

        return validate_evidence_type(
            evidence_type,
            manifest,
            passed_check_count=_worker_passed_verification_check_count,
        )
    return ["unsupported verification.evidence_type: %s" % evidence_type]


def _executor_verification_manifest_from_review_workspace(task_dir: Path) -> JsonDict:
    try:
        loaded = json.loads((task_dir / "executor-evidence.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    metadata = loaded.get("metadata") if isinstance(loaded.get("metadata"), dict) else {}
    manifest = metadata.get("verification") if isinstance(metadata.get("verification"), dict) else None
    if manifest is None and isinstance(loaded.get("verification"), dict):
        manifest = loaded.get("verification")
    return dict(manifest) if isinstance(manifest, dict) else {}


def _worker_review_verdict_executor_repo_problems(task_dir: Path, manifest: JsonDict) -> List[str]:
    executor_manifest = _executor_verification_manifest_from_review_workspace(task_dir)
    executor_repo = executor_manifest.get("repo") if isinstance(executor_manifest.get("repo"), dict) else {}
    if not executor_repo:
        return []
    review_repo = manifest.get("repo") if isinstance(manifest.get("repo"), dict) else {}
    problems: List[str] = []
    executor_changed = _metadata_path_list(executor_repo.get("files_changed"))
    review_changed = _metadata_path_list(review_repo.get("files_changed"))
    if executor_changed and set(review_changed) != set(executor_changed):
        problems.append(
            "review_verdict repo.files_changed must match executor evidence: %s != %s"
            % (review_changed, executor_changed)
        )
    codegraph_manifest = {
        **manifest,
        "repo": {**review_repo, "files_changed": executor_changed},
    }
    problems.extend(codegraph_audit_manifest_problems(codegraph_manifest))
    return problems


def _worker_repo_verification_problems(
    manifest: JsonDict,
    require_tests: bool,
    *,
    allow_empty_repo_change: bool = False,
) -> List[str]:
    problems = _worker_require_pushed_repo_anchor(manifest)
    repo = manifest.get("repo") if isinstance(manifest.get("repo"), dict) else {}
    files_changed = _manifest_list(repo.get("files_changed")) if isinstance(repo, dict) else []
    if not files_changed and not allow_empty_repo_change:
        problems.append("repo evidence requires changed files")
    if require_tests and _worker_passed_verification_check_count(manifest) < 1:
        problems.append("repo code evidence requires at least one passing test/check")
    problems.extend(codegraph_audit_manifest_problems(manifest))
    return problems


def _worker_allows_empty_repo_change_evidence(task: JsonDict, evidence_type: str) -> bool:
    if str(evidence_type or "").strip().lower() != "repo_change":
        return False
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    if not isinstance(metadata, dict):
        return False
    origin = metadata.get("origin")
    remediation = metadata.get("remediation")
    origin_type = origin.get("type") if isinstance(origin, dict) else None
    remediation_type = remediation.get("type") if isinstance(remediation, dict) else None
    return origin_type == "beads_source_remediation" or remediation_type == "beads_source_refresh"


def _worker_require_clean_repo_anchor(manifest: JsonDict) -> List[str]:
    repo = manifest.get("repo")
    if not isinstance(repo, dict):
        return ["repo evidence requires verification.repo object"]
    problems: List[str] = []
    head_sha = str(repo.get("head_sha") or "").strip()
    if not GIT_SHA_RE.match(head_sha):
        problems.append("repo.head_sha must be a git SHA")
    dirty = repo.get("dirty")
    if dirty not in {False, "false", "False", 0, "0"}:
        problems.append("repo evidence must declare dirty=false")
    return problems


def _worker_require_pushed_repo_anchor(manifest: JsonDict) -> List[str]:
    problems = _worker_require_clean_repo_anchor(manifest)
    repo = manifest.get("repo")
    if not isinstance(repo, dict):
        return problems
    pushed = repo.get("pushed") is True or str(repo.get("pushed") or "").lower() == "true"
    remote_ref = str(repo.get("remote_ref") or "").strip()
    pr_url = str(repo.get("pr_url") or "").strip()
    if not (pushed and remote_ref) and not pr_url:
        problems.append("repo evidence requires pushed=true with remote_ref, or pr_url")
    return problems


def _worker_passed_verification_check_count(manifest: JsonDict) -> int:
    count = 0
    for item in _manifest_list(manifest.get("tests")):
        if _worker_verification_item_passed(item):
            count += 1
    for item in _manifest_list(manifest.get("checks")):
        if _worker_verification_item_passed(item):
            count += 1
    return count


PASSING_VERIFICATION_WORDS = {"pass", "passed", "success", "successful", "succeeded", "ok"}


def _worker_int_value(value: Any) -> Optional[int]:
    try:
        if isinstance(value, bool):
            return int(value)
        return int(value)
    except (TypeError, ValueError):
        return None


def _worker_verification_item_passed(item: Any) -> bool:
    if isinstance(item, list):
        return any(_worker_verification_item_passed(nested) for nested in item)
    if not isinstance(item, dict):
        return False
    if "returncode" in item:
        return _worker_int_value(item["returncode"]) == 0
    failed = _worker_int_value(item.get("failed"))
    if failed is not None and failed > 0:
        return False
    for key in ("status", "result", "outcome"):
        if str(item.get(key) or "").strip().lower() in PASSING_VERIFICATION_WORDS:
            return True
    for key in ("passed", "success", "succeeded", "ok", "satisfied"):
        value = item.get(key)
        if value is True:
            return True
        number = _worker_int_value(value)
        if number is not None and number > 0 and failed == 0:
            return True
    bool_values = [value for value in item.values() if isinstance(value, bool)]
    if bool_values and len(bool_values) == len(item) and all(bool_values):
        return True
    return any(
        _worker_verification_item_passed(nested)
        for nested in item.values()
        if isinstance(nested, (dict, list))
    )


def _split_repository_porcelain_status(status_text: str) -> tuple[List[str], List[str], List[str]]:
    tracked_lines: List[str] = []
    untracked_paths: List[str] = []
    staged_new_paths: List[str] = []
    for line in str(status_text or "").splitlines():
        if not line:
            continue
        if line.startswith("?? "):
            untracked_paths.append(line[3:])
            continue
        tracked_lines.append(line)
        xy = line[:2]
        if ("A" in xy or "C" in xy) and "R" not in xy:
            staged_new_paths.append(line[3:])
    return tracked_lines, untracked_paths, staged_new_paths


def _repository_untracked_finalize_message(untracked_paths: List[str]) -> str:
    return (
        "untracked files present at finalize time — agent must commit ALL new files "
        "before declaring done: %s" % ", ".join(untracked_paths)
    )


def _repository_new_file_finalize_message(paths: List[str]) -> str:
    return (
        "new files staged at finalize time — agent must commit ALL new files "
        "before declaring done: %s" % ", ".join(paths)
    )


def _run_git(repo: Path, args: List[str]) -> subprocess.CompletedProcess[str]:
    try:
        timeout = float(os.environ.get("MAC_SELF_UPDATE_GIT_TIMEOUT", "120"))
    except ValueError:
        timeout = 120.0
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _truncate_process_text(value: str, limit: int = 4000) -> str:
    """Bound process output for evidence, keeping head AND tail.

    The tail is where the diagnosis lives — pytest prints its failure summary
    last — and the previous head-only cut made every long verification failure
    undiagnosable from the ledger (observed live: a suite that died after the
    4000-char mark left only passing '[ 12%]' progress lines in evidence,
    everywhere, including the workspace copy)."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(0, limit // 4)
    tail = limit - head
    marker = "\n… [%d chars omitted] …\n" % (len(text) - head - tail)
    return text[:head] + marker + text[-tail:]


def _env_truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _prose_tail(text: str, n: int = 4) -> List[str]:
    """The last ``n`` natural-language lines of agent output — the agent's closing
    summary, skipping diff/patch/code noise so the per-task narrative reads like a
    person's recap rather than a raw diff. Falls back to the last non-empty lines
    if nothing looks like prose."""
    prose: List[str] = []
    nonempty: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        nonempty.append(line)
        if line[0] in "+-@" or line.startswith(("diff ", "index ", "---", "+++", "@@", "```")):
            continue  # diff / patch / fence noise
        if " " not in line:
            continue  # a bare token / path / code symbol, not prose
        letters = sum(ch.isalpha() for ch in line)
        if letters < max(3, len(line) // 3):
            continue  # mostly punctuation / code
        prose.append(line)
    return (prose or nonempty)[-n:]


def _extract_marked_summary(text: str) -> str:
    """The agent/reviewer's delimited recap — the plain-prose block it was told to
    print (see task_executor.MAC_TASK_SUMMARY_BEGIN/END) — or "" if absent. Strips
    ANSI and matches the markers tolerantly so it survives CLI formatting. This is
    the clean human summary; _prose_tail is the fallback when the agent omits it."""
    if not text:
        return ""
    import re

    clean = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)  # strip ANSI/CSI escapes
    match = re.search(
        r"=== *MAC TASK SUMMARY *===\s*(.*?)\s*=== *END MAC TASK SUMMARY *===",
        clean,
        re.DOTALL,
    )
    if not match:
        return ""
    lines = [ln.strip() for ln in match.group(1).splitlines() if ln.strip()]
    return "\n".join(lines[:6]).strip()


#: Path (relative to the self-update repo root) of the OpenShell sandbox image
#: build recipe.
_OPENSHELL_CONTAINERFILE_RELPATH = "deploy/openshell/mac-hermes.Containerfile"


def _resolve_openshell_docker_bin() -> Optional[str]:
    """Resolve Docker from service-safe paths, including Docker Desktop.

    macOS launchd jobs do not inherit the interactive shell PATH, so a plain
    ``shutil.which('docker')`` incorrectly reports drift on an otherwise ready
    host.  Keep the configured override first, then conventional service paths.
    """
    configured = (os.environ.get("MAC_OPENSHELL_DOCKER_BIN") or "").strip()
    if configured:
        resolved = shutil.which(configured)
        if resolved:
            return resolved
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    resolved = shutil.which("docker")
    if resolved:
        return resolved
    for raw in (
        "/Applications/Docker.app/Contents/Resources/bin/docker",
        "/opt/homebrew/bin/docker",
        "/usr/local/bin/docker",
    ):
        candidate = Path(raw)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _openshell_containerfile_changed(repo: Path, before_sha: str, after_sha: str) -> bool:
    """Return whether a pull changed the historical image recipe path.

    Kept as a public test/diagnostic primitive even though deployment freshness
    now uses the stronger complete-source SHA marker.
    """
    if not before_sha or not after_sha or before_sha == after_sha:
        return False
    diff = _run_git(repo, ["diff", "--name-only", "%s..%s" % (before_sha, after_sha)])
    if diff.returncode != 0:
        return False
    return any(
        line.strip() == _OPENSHELL_CONTAINERFILE_RELPATH for line in diff.stdout.splitlines()
    )


def _self_worker_service_names() -> set[str]:
    names = {"mac-agent.service"}
    configured = str(os.environ.get("MAC_AGENT_SERVICE_NAME") or "").strip()
    if configured:
        names.add(configured)
    fleet = str(os.environ.get("FLEET_NAME") or os.environ.get("MAC_FLEET_NAME") or "").strip()
    if fleet:
        names.add("%s-agent.service" % fleet)
    return names


def _restart_systemd_service(service: str) -> JsonDict:
    try:
        _normalize_restart_services([service])
    except ValueError as exc:
        return {"service": service, "status": "error", "error": str(exc)}
    if service in _self_worker_service_names():
        return {
            "service": service,
            "status": "skipped",
            "reason": "worker service restart is handled by the repo-update restart flag",
        }
    if not shutil.which("systemctl"):
        return {"service": service, "status": "skipped", "reason": "systemctl not found"}
    try:
        timeout = float(os.environ.get("MAC_SELF_UPDATE_SERVICE_TIMEOUT", "30"))
    except ValueError:
        timeout = 30.0

    try:
        show = subprocess.run(
            ["systemctl", "show", service, "--property=LoadState", "--value"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - service restarts are reported, not raised.
        return {
            "service": service,
            "status": "skipped",
            "reason": "could not inspect service",
            "error": str(exc),
        }
    load_state = show.stdout.strip()
    if show.returncode != 0:
        return {
            "service": service,
            "status": "skipped",
            "reason": "could not inspect service",
            "returncode": show.returncode,
            "stdout": _truncate_process_text(show.stdout),
            "stderr": _truncate_process_text(show.stderr),
        }
    if load_state in {"", "not-found"}:
        return {
            "service": service,
            "status": "skipped",
            "reason": "service not installed",
            "load_state": load_state or "unknown",
        }

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        argv = ["systemctl", "restart", service]
    else:
        argv = ["sudo", "-n", "systemctl", "restart", service]
    try:
        restarted = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - report failure in the result payload.
        return {"service": service, "status": "error", "command": argv, "error": str(exc)}
    return {
        "service": service,
        "status": "restarted" if restarted.returncode == 0 else "error",
        "command": argv,
        "returncode": restarted.returncode,
        "stdout": _truncate_process_text(restarted.stdout),
        "stderr": _truncate_process_text(restarted.stderr),
    }


def _run_git_in(cwd: Path, args: List[str]) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` with ``cwd`` as the working directory.

    Used for clone where the target directory does not yet exist (so
    ``git -C <target>`` is invalid). Mirrors ``_run_git`` for timeout
    + capture behaviour so the K8s clone path is testable via the
    same monkeypatch surface."""
    try:
        timeout = float(os.environ.get("MAC_SELF_UPDATE_GIT_TIMEOUT", "120"))
    except ValueError:
        timeout = 120.0
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _inject_git_remote_auth(url: str) -> str:
    from mac.gitops import inject_git_remote_auth as _impl
    return _impl(url)


def _redact_git_remote_auth(url: str) -> str:
    from mac.gitops import redact_git_remote_auth as _impl
    return _impl(url)


def _redact_git_remote_auth_in_text(value: str) -> str:
    from mac.gitops import redact_git_remote_auth_in_text as _impl
    return _impl(value)


def _stable_id(prefix: str, value: str) -> str:
    return "%s_%s" % (prefix, _safe_path_component(value.lower()).strip("_") or "default")


def _register_runtime_identity_for_worker(
    client: MacApiClient,
    agent_name: str,
    hermes_instance_id: Optional[str],
) -> None:
    if not hermes_instance_id:
        return
    tenant_id = (os.environ.get("MAC_FLEET_TENANT_ID") or "").strip()
    if not tenant_id:
        return
    persona_id = (
        os.environ.get("MAC_HERMES_PERSONA_ID")
        or os.environ.get("MAC_WORKER_PERSONA_ID")
        or _stable_id("persona", agent_name)
    )
    hermes_home = mac_paths.gateway_home()
    fleet_name = (
        os.environ.get("MAC_FLEET_NAME")
        or os.environ.get("FLEET_NAME")
        or tenant_id.removeprefix("tenant_")
        or "mac"
    )
    agent_id = os.environ.get("MAC_AGENT_ID") or _stable_id("agent", agent_name)
    client.post(
        "/tenants",
        {
            "name": fleet_name,
            "tenant_id": tenant_id,
            "metadata": {"source": "mac-agent", "fleet": fleet_name},
        },
    )
    client.post(
        "/personas",
        {
            "tenant_id": tenant_id,
            "name": agent_name,
            "soul_ref": str(hermes_home / "SOUL.md"),
            "memory_scope": str(hermes_home),
            "persona_id": persona_id,
            "metadata": {"source": "mac-agent", "agent_id": agent_id},
        },
    )
    client.post(
        "/persona-instances",
        {
            "tenant_id": tenant_id,
            "name": agent_name,
            "persona_id": persona_id,
            "home_ref": str(hermes_home),
            "instance_id": hermes_instance_id,
            "metadata": {"source": "mac-agent", "agent_id": agent_id, "fleet": fleet_name},
        },
    )


def _fleet_name_from_env(tenant_id: str) -> str:
    explicit = (os.environ.get("MAC_FLEET_NAME") or os.environ.get("FLEET_NAME") or "").strip()
    if explicit:
        return explicit
    if tenant_id.startswith("tenant_"):
        return tenant_id.removeprefix("tenant_") or "mac"
    return "mac"


def _fleet_auto_registration_enabled() -> bool:
    raw = os.environ.get("MAC_AUTO_REGISTER_FLEET")
    if raw is None or raw == "":
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _local_fleet_registry_path() -> Path:
    return Path(
        os.environ.get("MAC_FLEETS_CONFIG")
        or os.environ.get("MAC_DEPLOY_FLEETS_CONFIG")
        or mac_paths.fleets_config()
    ).expanduser()


def _agent_configured_in_local_registry(
    *,
    fleet_name: str,
    agent_name: str,
    registry_path: Optional[Path] = None,
) -> bool:
    path = (registry_path or _local_fleet_registry_path()).expanduser()
    if not path.exists():
        return False
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    fleets = data.get("fleets") if isinstance(data, dict) else None
    if isinstance(fleets, dict):
        fleet_items = fleets.items()
    elif isinstance(fleets, list):
        fleet_items = (
            (str(item.get("hub_agent") or item.get("fleet_name") or ""), item)
            for item in fleets
            if isinstance(item, dict)
        )
    else:
        return False
    for key, fleet in fleet_items:
        if not isinstance(fleet, dict):
            continue
        names = {
            str(key or "").strip(),
            str(fleet.get("fleet_name") or "").strip(),
            str(fleet.get("name") or "").strip(),
            str(fleet.get("hub_agent") or "").strip(),
        }
        if fleet_name not in names:
            continue
        for agent in fleet.get("agents") or []:
            if not isinstance(agent, dict):
                continue
            if str(agent.get("name") or "").strip() != agent_name:
                continue
            return bool(agent.get("enabled", True))
    return False


def _get_fleet_or_none(client: MacApiClient, fleet_name: str) -> Optional[JsonDict]:
    try:
        result = client.get("/fleets/%s" % quote(fleet_name, safe=""))
    except MacApiError as exc:
        if "not found" in str(exc).lower() or "404" in str(exc):
            return None
        raise
    return result if isinstance(result, dict) else None


def _ensure_worker_fleet_membership(
    client: MacApiClient,
    *,
    agent_name: str,
    agent_id: str,
) -> None:
    """Record runtime fleet presence without mutating configured topology."""
    if not _fleet_auto_registration_enabled():
        return
    tenant_id = (os.environ.get("MAC_FLEET_TENANT_ID") or "").strip()
    if not tenant_id:
        return
    fleet_name = _fleet_name_from_env(tenant_id)
    shared_services_manager = resolve_hub_agent("MAC_SHARED_SERVICES_MANAGER_AGENT")
    metadata = {
        "source": "mac-agent",
        "fleet": fleet_name,
        "hub_agent": shared_services_manager or agent_name,
    }
    configured_by_registry = _agent_configured_in_local_registry(
        fleet_name=fleet_name,
        agent_name=agent_name,
    )
    if configured_by_registry:
        metadata["topology_source"] = str(_local_fleet_registry_path())
    client.post(
        "/tenants",
        {
            "name": fleet_name,
            "tenant_id": tenant_id,
            "metadata": metadata,
        },
    )
    existing = _get_fleet_or_none(client, fleet_name)
    if existing is None:
        try:
            client.post(
                "/fleets",
                {
                    "name": fleet_name,
                    "description": "Auto-registered deployment fleet",
                    "status": "active",
                    "metadata": metadata,
                    "tenant_id": tenant_id,
                    "agent_ids": [agent_id] if configured_by_registry else [],
                    "fleet_id": _stable_id("fleet", fleet_name),
                    "actor": "mac-agent",
                },
            )
        except MacApiError as exc:
            if "already exists" not in str(exc).lower() and "unique" not in str(exc).lower():
                raise
            existing = _get_fleet_or_none(client, fleet_name)
    if not existing:
        existing = _get_fleet_or_none(client, fleet_name)
    fleet_key = str((existing or {}).get("id") or fleet_name)
    if existing and configured_by_registry:
        current_members = [
            str(item) for item in existing.get("agent_ids") or [] if str(item).strip()
        ]
        next_members = sorted(set(current_members + [agent_id]))
        if next_members != current_members:
            client.request(
                "PUT",
                "/fleets/%s" % quote(fleet_key, safe=""),
                {
                    "status": "active",
                    "metadata": {
                        **(
                            existing.get("metadata")
                            if isinstance(existing.get("metadata"), dict)
                            else {}
                        ),
                        **metadata,
                    },
                    "tenant_id": tenant_id,
                    "agent_ids": next_members,
                    "actor": "mac-agent",
                },
            )
    client.post(
        "/fleets/%s/observed-agents" % quote(fleet_key, safe=""),
        {
            "agent_id": agent_id,
            "source": "mac-agent",
            "metadata": metadata,
            "actor": "mac-agent",
        },
    )


def _csv_arg(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _json_arg(value: Optional[str]) -> JsonDict:
    if not value:
        return {}
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise MacApiError("resources must be a JSON object")
    return loaded


def _read_env_value(path: Path, key: str) -> Optional[str]:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == key and value.strip():
                return value.strip()
    except FileNotFoundError:
        return None
    return None


def _write_env_value(path: Path, key: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    replaced = False
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    updated: List[str] = []
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            name, _old = line.split("=", 1)
            if name.strip() == key:
                updated.append("%s=%s" % (key, value))
                replaced = True
                continue
        updated.append(line)
    if not replaced:
        updated.append("%s=%s" % (key, value))
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _attestation_key_matches_hub(
    client: MacApiClient,
    agent_id: str,
    attestation_key: str,
) -> bool:
    from mac.services import sign_verification_manifest

    challenge = {
        "schema": "mac.agent_attestation_challenge.v1",
        "purpose": "attestation-key-healthcheck",
        "agent_id": agent_id,
        "nonce": secrets.token_urlsafe(32),
    }
    response = client.post(
        "/agents/%s/attestation-key/verify" % quote(agent_id, safe=""),
        {
            "challenge": challenge,
            "signature": sign_verification_manifest(attestation_key, challenge),
        },
    )
    return bool(isinstance(response, dict) and response.get("valid") is True)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="mac worker harness")
    parser.add_argument(
        "--url",
        default=os.environ.get("MAC_URL") or os.environ.get("MAC_HUB_URL") or "http://127.0.0.1:8000",
    )
    # Token resolution honors --fleet (or MAC_FLEET) so machines in
    # multiple fleets don't collide on a single MAC_API_TOKEN. Resolution
    # is deferred to main() once --fleet is known: baking a fleet-blind
    # flat token into the parser default would let a legacy flat
    # MAC_WORKER_TOKEN win over the correct scoped MAC_WORKER_TOKEN__<FLEET>
    # (and fire the mac-g55y deprecation warning at parse time). See
    # mac.fleet_env (mac-g55y).
    parser.add_argument(
        "--fleet",
        default=os.environ.get("MAC_FLEET"),
        help="fleet name used to scope env var lookup (MAC_API_TOKEN__<FLEET>)",
    )
    parser.add_argument(
        "--token",
        default=None,
    )
    parser.add_argument("--agent-id", default=os.environ.get("MAC_AGENT_ID"))
    parser.add_argument(
        "--hermes-instance-id",
        default=os.environ.get("MAC_WORKER_HERMES_INSTANCE_ID")
        or os.environ.get("MAC_HERMES_INSTANCE_ID"),
        help="MAC Hermes instance id to bind this worker agent to",
    )
    parser.add_argument(
        "--register",
        action="store_true",
        help="register or refresh this host's machine and agent rows before running",
    )
    parser.add_argument("--machine-id", default=os.environ.get("MAC_MACHINE_ID"))
    parser.add_argument("--hostname", default=os.environ.get("MAC_HOSTNAME"))
    parser.add_argument("--agent-name", default=os.environ.get("MAC_AGENT_NAME"))
    parser.add_argument(
        "--capabilities",
        default=os.environ.get("MAC_WORKER_CAPABILITIES", ""),
        help="comma-separated capabilities to advertise when --register is used",
    )
    parser.add_argument(
        "--resources",
        default=os.environ.get("MAC_WORKER_RESOURCES"),
        help="JSON resource/capacity object to advertise when --register is used",
    )
    parser.add_argument(
        "--install-pip",
        action="append",
        default=[],
        metavar="SPEC",
        help="pip spec to self-install into the agent venv (repeatable); audited + "
        "reported to the hub footprint, then exits.",
    )
    parser.add_argument(
        "--install-npm",
        action="append",
        default=[],
        metavar="PKG",
        help="npm package to self-install under the agent's local prefix (repeatable).",
    )
    parser.add_argument(
        "--install-index-url",
        default=None,
        help="optional pip --index-url for --install-pip (e.g. a CUDA wheel index).",
    )
    parser.add_argument(
        "--install-reason",
        default="agent self-install",
        help="reason recorded in the command audit for --install-pip/--install-npm.",
    )
    parser.add_argument("--workspace", default=".mac-agent-workspaces")
    parser.add_argument("--lease-seconds", type=int, default=900)
    # mac-ehch: hard-cap executor runtime so a wedged subprocess can't
    # keep renewing its lease forever. One hour is well above the median
    # claim duration in production and below the point where a stuck
    # task should be visible to operators. Override with --timeout if a
    # longer-running task is genuinely needed.
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument(
        "--allowed-projects",
        default=os.environ.get("MAC_WORKER_ALLOWED_PROJECTS", ""),
        help="comma-separated projects this worker may claim",
    )
    parser.add_argument(
        "--required-metadata",
        default=os.environ.get("MAC_WORKER_REQUIRED_METADATA"),
        help="JSON object of top-level task metadata key/value pairs required before claiming",
    )
    parser.add_argument(
        "--claim-only-canary-tasks",
        action="store_true",
        default=_env_bool("MAC_WORKER_CLAIM_ONLY_CANARY_TASKS", False),
        help="claim only tasks with metadata.canary, metadata.mac_canary, or metadata.worker_canary true",
    )
    parser.add_argument(
        "--running-digest",
        default=os.environ.get("MAC_WORKER_RUNNING_DIGEST"),
        help="runtime_environments.digest the worker is running (declared at first heartbeat)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="run forever (poll for tasks). Default is run_once and exit.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        help="cap iterations in --loop mode (mostly for tests)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="seconds to sleep between polls when no task is available",
    )
    parser.add_argument(
        "--self-update-repo",
        default=os.environ.get("MAC_SELF_UPDATE_REPO"),
        help="git worktree this worker may pull for AgentBus repo-update control messages",
    )
    parser.add_argument(
        "--disable-agentbus-control",
        action="store_true",
        help="disable AgentBus control-message polling before task claims",
    )
    parser.add_argument(
        "--attestation-key-env",
        default=os.environ.get("MAC_ATTESTATION_KEY_ENV"),
        help="env file where a first-registration attestation key should be persisted",
    )
    parser.add_argument(
        "--heartbeat-only",
        action="store_true",
        help="register/heartbeat once and exit without claiming tasks",
    )
    parser.add_argument(
        "--dry-run-claim",
        action="store_true",
        help="register/heartbeat and ask the hub what this worker would claim without creating a lease",
    )
    parser.add_argument(
        "--executor",
        nargs=argparse.REMAINDER,
        default=None,
        help="executor argv; pass this flag last, followed by the command and arguments",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # Resolve the token fleet-aware now that --fleet (or MAC_FLEET) is
    # known. An explicit --token still wins; otherwise the fleet-scoped
    # MAC_*__<FLEET> form takes precedence over the legacy flat form, and
    # the mac-g55y deprecation warning only fires when we actually fall
    # back to the flat form.
    if args.token is None:
        from mac.fleet_env import resolve_first as _rt

        args.token = _rt(["MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN"], fleet=args.fleet)
    client = MacApiClient(args.url, token=args.token)
    agent_id = args.agent_id
    try:
        registered: Optional[JsonDict] = None
        attestation_key = os.environ.get("MAC_ATTESTATION_KEY")
        attestation_env_path = Path(args.attestation_key_env).expanduser() if args.attestation_key_env else None
        if not attestation_key and attestation_env_path is not None:
            attestation_key = _read_env_value(attestation_env_path, "MAC_ATTESTATION_KEY")
        if args.register:
            registered = register_worker(
                client,
                hostname=args.hostname,
                agent_name=args.agent_name,
                capabilities=_csv_arg(args.capabilities),
                resources=_json_arg(args.resources),
                machine_id=args.machine_id,
                agent_id=args.agent_id,
                hermes_instance_id=args.hermes_instance_id,
                executor_argv=list(args.executor or []),
            )
            agent_id = registered["id"]
            if registered.get("attestation_key"):
                attestation_key = str(registered["attestation_key"])
                os.environ["MAC_ATTESTATION_KEY"] = attestation_key
                if attestation_env_path is not None:
                    _write_env_value(attestation_env_path, "MAC_ATTESTATION_KEY", attestation_key)
            # Data-driven OpenShell sandbox requirement: project the agent's
            # runtime resources["openshell_required"] (owned by the hub) into the
            # env the executor inherits, so a redeploy isn't needed to honor an
            # operator's DB change. A pre-set MAC_OPENSHELL_REQUIRED wins; an
            # unset resource is a no-op. Replaces the old hardcoded agent list.
            from mac.openshell_runtime import apply_openshell_requirement

            apply_openshell_requirement(registered.get("resources"), os.environ)
            _apply_read_only_report_executor_approval(
                registered.get("resources"), os.environ
            )
        # --- startup behaviors (all default-on, env-gated) ---
        startup_info: JsonDict = {"agent_id": agent_id}
        # Dispatch holds are hub/operator authority. An ordinary worker restart
        # cannot know whether a hold is stale or was just replaced by an
        # operator, so clearing is legacy opt-in rather than the default.
        if args.register and _env_bool("MAC_STARTUP_CLEAR_HOLD", False):
            try:
                client.request("DELETE", "/agents/%s/dispatch-hold" % quote(agent_id, safe=""), None)
                startup_info["hold_cleared"] = True
            except Exception:
                startup_info["hold_cleared"] = False
        else:
            startup_info["hold_cleared"] = False
        if args.register and _env_bool("MAC_STARTUP_EMIT_CHECKOUT_SHA", True):
            try:
                self_update_repo = (
                    Path(args.self_update_repo).expanduser()
                    if args.self_update_repo
                    else _default_self_update_repo()
                )
                sha = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(self_update_repo),
                    stderr=subprocess.DEVNULL,
                ).decode("utf-8").strip()
                startup_info["checkout_sha"] = sha
            except Exception:
                startup_info["checkout_sha"] = None
        else:
            startup_info["checkout_sha"] = None
        if args.register and _env_bool("MAC_STARTUP_IMPORT_SELF_CHECK", True):
            try:
                self_update_repo_for_check = (
                    Path(args.self_update_repo).expanduser()
                    if args.self_update_repo
                    else _default_self_update_repo()
                )
                startup_info["import_self_check"] = _startup_import_self_check(self_update_repo_for_check)
            except Exception as exc:
                startup_info["import_self_check"] = "error: " + str(exc)
        else:
            startup_info["import_self_check"] = None
        if not agent_id:
            raise MacApiError("--agent-id or --register is required")
        if args.install_pip or args.install_npm:
            # Autonomous self-install: provision tools into the agent's own
            # environment, audit + report the footprint, then exit.
            installer = MacWorker(
                client, agent_id, Path(args.workspace), SubprocessExecutor(["true"])
            )
            results: JsonDict = {}
            if args.install_pip:
                results["pip"] = installer.ensure_pip(
                    args.install_pip,
                    reason=args.install_reason,
                    index_url=args.install_index_url,
                )
            if args.install_npm:
                results["npm"] = installer.ensure_npm(
                    args.install_npm, reason=args.install_reason
                )
            print(json.dumps({"status": "self-install", "results": results}, indent=2, sort_keys=True))
            return 0 if all(r.get("ok", True) for r in results.values()) else 1
        if args.heartbeat_only:
            deployment_generation, _ = _deployment_barrier_state()
            heartbeat_resources: Optional[Mapping[str, Any]] = None
            if deployment_generation:
                registered_resources = (
                    registered.get("resources")
                    if isinstance(registered, Mapping)
                    else None
                )
                heartbeat_resources = (
                    dict(registered_resources)
                    if isinstance(registered_resources, Mapping)
                    else {}
                )
            heartbeat_payload = _deployment_heartbeat_payload(
                "idle",
                resources=heartbeat_resources,
                report_health=True,
            )
            heartbeat_payload["running_digest"] = args.running_digest
            heartbeat = client.post(
                "/agents/%s/heartbeat" % quote(agent_id, safe=""),
                heartbeat_payload,
            )
            directive_snapshot = _synchronize_directive_policy(client, agent_id)
            print(
                json.dumps(
                    {
                        **startup_info,
                        "status": "heartbeat",
                        "agent": heartbeat,
                        "registered": registered,
                        "directive_snapshot": directive_snapshot,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        required_metadata = _json_arg(args.required_metadata)
        allowed_projects = _csv_arg(args.allowed_projects)
        executor_argv = list(args.executor or [])
        if executor_argv and executor_argv[0] == "--":
            executor_argv = executor_argv[1:]
        if args.dry_run_claim:
            worker = MacWorker(
                client,
                agent_id,
                Path(args.workspace),
                SubprocessExecutor(["true"]),
                lease_seconds=args.lease_seconds,
                running_digest=args.running_digest,
                poll_interval_seconds=args.poll_interval,
                allowed_projects=allowed_projects,
                required_metadata=required_metadata,
                claim_only_canary_tasks=args.claim_only_canary_tasks,
                agentbus_control_enabled=not args.disable_agentbus_control,
                self_update_repo=Path(args.self_update_repo).expanduser()
                if args.self_update_repo
                else None,
                attestation_key=attestation_key,
            )
            print(json.dumps({**startup_info, "status": "dry_run", "assignment": worker.dry_run_claim()}, indent=2, sort_keys=True))
            return 0
        if not executor_argv:
            raise MacApiError("--executor is required unless --heartbeat-only is set")
        worker = MacWorker(
            client,
            agent_id,
            Path(args.workspace),
            SubprocessExecutor(executor_argv, timeout=args.timeout),
            lease_seconds=args.lease_seconds,
            running_digest=args.running_digest,
            poll_interval_seconds=args.poll_interval,
            allowed_projects=allowed_projects,
            required_metadata=required_metadata,
            claim_only_canary_tasks=args.claim_only_canary_tasks,
            agentbus_control_enabled=not args.disable_agentbus_control,
            self_update_repo=Path(args.self_update_repo).expanduser()
            if args.self_update_repo
            else None,
            attestation_key=attestation_key,
            attestation_key_env_path=attestation_env_path,
        )
        if args.loop:
            results = worker.run_forever(max_iterations=args.max_iterations)
            print(json.dumps([{**startup_info, **r.to_dict()} for r in results], indent=2, sort_keys=True))
            if any(result.status == "self_update_restart" for result in results):
                return SELF_UPDATE_RESTART_EXIT_CODE
        else:
            result = worker.run_once()
            print(json.dumps({**startup_info, **result.to_dict()}, indent=2, sort_keys=True))
            if result.status == "self_update_restart":
                return SELF_UPDATE_RESTART_EXIT_CODE
    except MacApiError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2, sort_keys=True))
        return 1
    except (TimeoutError, ConnectionError) as exc:
        # A bare socket timeout / connection error can still surface from paths
        # that bypass MacApiClient's wrapper (e.g. the --heartbeat-only startup
        # path or an ad-hoc transport). Left unwrapped it escapes the MacApiError
        # guard and, under the service wrapper's ``set -e``, crash-loops
        # mac-agent-service on a transient hub blip. Treat it as a recoverable
        # error exit instead of an unhandled traceback.
        print(
            json.dumps(
                {"status": "error", "error": "transient transport error: %s" % exc},
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
