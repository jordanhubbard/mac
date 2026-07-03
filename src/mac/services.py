from __future__ import annotations

import base64
import binascii
import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import yaml
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mac.agentbus_control import (
    REPO_UPDATE_CONTENT_TYPE,
    REPO_UPDATE_TOPIC,
    repo_update_payload,
)
from mac.models import (
    Agent,
    AgentProvisioningRequest,
    AgentRole,
    Workflow,
    WorkflowRun,
    AgentBusChunk,
    AgentBusStream,
    AgentMessage,
    AgentStatus,
    Artifact,
    AuthorizationError,
    ProjectRepository,
    COMMAND_AUDIT_PHASES,
    CommandAuditRecord,
    MoodOverlay,
    NapRun,
    NapSchedule,
    ConversationThread,
    Deployment,
    Environment,
    EVIDENCE_KINDS,
    EvalRun,
    EvalSet,
    Evidence,
    EvidenceArtifact,
    Fleet,
    HealthStatus,
    HistoryEvent,
    HermesInstance,
    IntegrationFinding,
    IntegrationObservation,
    JsonDict,
    Lease,
    LeaseStatus,
    MACError,
    Machine,
    MemoryRecord,
    MessageStatus,
    MessageType,
    NotFoundError,
    NotifierChannel,
    ObservabilityEvent,
    OperatorNotification,
    Persona,
    PlatformBinding,
    ProjectRecord,
    ProjectItem,
    Publication,
    PublicationStatus,
    Review,
    ReviewStatus,
    Rollout,
    RuntimeEnvironment,
    RuntimeEnvironmentDelta,
    RuntimeRun,
    SecretAccess,
    SecretHandle,
    SecretRecord,
    ServiceRole,
    metadata_declares_report_deliverable,
    Task,
    TASK_TRANSITIONS,
    TaskState,
    TaskTransitionOutbox,
    Tenant,
    TERMINAL_TASK_STATES,
    TransitionError,
    User,
    ValidationError,
    VectorRef,
    coerce_list,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    parse_time,
    utcnow,
    validate_transition,
    WorkflowDraft,
)
from mac.repository_hygiene import (
    normalize_cancellation_detail,
    repository_ref_lifecycle_for_transition,
)
from mac.reconciliation import ReconciliationCoordinator
from mac.agent_state_service import AgentStateService
from mac.agentbus_control import (
    ARTIFACT_PUBLISH_CONTENT_TYPE,
    ARTIFACT_PUBLISH_TOPIC,
    artifact_publish_payload,
)
from mac.action_event_service import ActionEventService
from mac.agentbus_service import AgentBusService
from mac.deploy_service import DeployService
from mac.codegraph_audit import codegraph_audit_manifest_problems
from mac import evidence_blobs
from mac.evidence_validators import rejected_verdict_feedback_problems, validate_evidence_type
from mac.eval_service import EvalService
from mac.fleet_learning import (
    REPOSITORY_ACCESS_RECORD_TYPE,
    repository_access_state,
    repository_host,
    task_repository_remote,
)
from mac.identity_service import IdentityService
from mac.memory_service import MemoryService
from mac.messaging_service import MessagingService
from mac.notifier_service import NotifierService
from mac.observability_service import ObservabilityService
from mac.openshell_runtime import openshell_required_for_identity
from mac.openshell_service import OpenShellService
from mac.provisioning_service import ProvisioningService
from mac.retention_service import RetentionService
from mac.service_role_service import ServiceRoleService
from mac.review_service import ReviewService, cross_llm_review_problems
from mac.roles_service import RolesService
from mac.rollout_service import RolloutService
from mac.secrets_service import SecretsService
from mac.store import SQLiteStore, Store, make_store_from_env
from mac.task_lifecycle import DispatchService, TaskLedgerService
from mac.workflow_runtime import WorkflowRuntime
from mac.workflow_service import WorkflowService


def _state_value(state: Any) -> str:
    return state.value if hasattr(state, "value") else str(state)


def _configured_qdrant_url(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit
    for name in ("MAC_QDRANT_URL", "QDRANT_URL", "QDRANT_ADDRESS", "QDRANT_FLEET_URL"):
        value = os.environ.get(name)
        if value:
            return value
    return None




def _manifest_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _unique_ordered(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _metadata_string_list(value: Any) -> List[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if not isinstance(value, Iterable):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


_REQUIRED_CHANGED_FILE_KEYS = (
    "required_changed_files",
    "required_files",
    "required_repo_files",
)

MAX_EVIDENCE_ARTIFACTS = 16
DEFAULT_EVIDENCE_ARTIFACT_BYTES = 5 * 1024 * 1024
MAX_EVIDENCE_ARTIFACT_BYTES = 50 * 1024 * 1024
DEFAULT_EVIDENCE_ARTIFACT_TOTAL_BYTES = 50 * 1024 * 1024
MAX_EVIDENCE_ARTIFACT_TOTAL_BYTES = 100 * 1024 * 1024


def _evidence_artifact_max_bytes() -> int:
    raw = os.environ.get("MAC_EVIDENCE_ARTIFACT_MAX_BYTES", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_EVIDENCE_ARTIFACT_BYTES
    except ValueError:
        value = DEFAULT_EVIDENCE_ARTIFACT_BYTES
    return min(MAX_EVIDENCE_ARTIFACT_BYTES, max(0, value))


def _evidence_artifact_total_max_bytes() -> int:
    raw = os.environ.get("MAC_EVIDENCE_ARTIFACT_TOTAL_MAX_BYTES", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_EVIDENCE_ARTIFACT_TOTAL_BYTES
    except ValueError:
        value = DEFAULT_EVIDENCE_ARTIFACT_TOTAL_BYTES
    return min(MAX_EVIDENCE_ARTIFACT_TOTAL_BYTES, max(0, value))


def _evidence_artifact_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_repo_relative_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    path = re.sub(r"/+", "/", path)
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/")


def _metadata_path_list(value: Any) -> List[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Iterable) and not isinstance(value, dict):
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


def _nested_json_object(root: JsonDict, *keys: str) -> JsonDict:
    node: Any = root
    for key in keys:
        if not isinstance(node, dict):
            return {}
        node = node.get(key)
    return ensure_json_object(node) if isinstance(node, dict) else {}


def _repository_contract_test_command_for_task(task: "Task") -> str:
    """The repository contract's test command for a task, or "" (the hub
    verifier then defaults to scripts/run-contract-tests.sh)."""
    metadata = ensure_json_object(task.metadata)
    for path in (
        ("execution_contract", "test"),
        ("execution_contract", "repository_contract", "test"),
        ("origin", "repository_contract", "test"),
        ("repository_contract", "test"),
    ):
        node = _nested_json_object(metadata, *path)
        command = str(node.get("command") or "").strip()
        if command:
            return command
    return ""


def _repository_contracts_from_metadata(metadata: JsonDict) -> List[JsonDict]:
    contracts: List[JsonDict] = []
    seen: set[str] = set()
    for path in (
        ("execution_contract", "repository_contract"),
        ("origin", "repository_contract"),
        ("repository_contract",),
    ):
        contract = _nested_json_object(metadata, *path)
        if not contract:
            continue
        key = json_dumps(contract)
        if key not in seen:
            seen.add(key)
            contracts.append(contract)
    return contracts


def _repository_required_commands_from_metadata(metadata: JsonDict) -> List[str]:
    required: List[str] = []
    seen: set[str] = set()
    for contract in _repository_contracts_from_metadata(metadata):
        toolchain = ensure_json_object(contract.get("toolchain"))
        for command in _metadata_string_list(toolchain.get("required_commands")):
            if command not in seen:
                seen.add(command)
                required.append(command)
    return required


_REPOSITORY_HOST_REQUIRED_COMMANDS = {"git"}


def _repository_host_required_commands_from_metadata(metadata: JsonDict) -> List[str]:
    """Commands a sandboxed host worker must have before it can start repo work.

    Repository contracts describe the project toolchain, which belongs inside the
    task sandbox when the agent is OpenShell-required. Dispatch should not
    require sandboxed worker hosts to advertise project-local commands like
    pnpm, java, or lein. Keep only the primitive the worker itself needs to
    prepare/push task-owned git worktrees.
    """
    return [
        command
        for command in _repository_required_commands_from_metadata(metadata)
        if command in _REPOSITORY_HOST_REQUIRED_COMMANDS
    ]


def _agent_resource_command_names(resources: JsonDict) -> set[str]:
    names: set[str] = set()
    for key in ("commands", "command_inventory"):
        inventory = resources.get(key)
        if isinstance(inventory, dict):
            for value in _metadata_string_list(inventory.get("available")):
                names.add(value)
            commands = inventory.get("commands")
            if isinstance(commands, list):
                for item in commands:
                    if isinstance(item, str) and item.strip():
                        names.add(item.strip())
                    elif isinstance(item, dict):
                        name = str(item.get("name") or "").strip()
                        if name:
                            names.add(name)
            paths = inventory.get("paths")
            if isinstance(paths, dict):
                names.update(str(name).strip() for name in paths if str(name).strip())
        elif isinstance(inventory, list):
            for item in inventory:
                if isinstance(item, str) and item.strip():
                    names.add(item.strip())
                elif isinstance(item, dict):
                    name = str(item.get("name") or "").strip()
                    if name:
                        names.add(name)
    return names


def _agent_requires_openshell(agent: Agent) -> bool:
    return openshell_required_for_identity(
        agent_id=agent.id,
        agent_name=agent.name,
        resources=ensure_json_object(agent.resources),
    )


def _required_changed_files_from_metadata(metadata: JsonDict) -> List[str]:
    containers = [
        ensure_json_object(metadata),
        _nested_json_object(metadata, "acceptance"),
        _nested_json_object(metadata, "execution_contract"),
        _nested_json_object(metadata, "execution_contract", "evidence"),
        _nested_json_object(metadata, "execution_contract", "repository_contract"),
        _nested_json_object(metadata, "execution_contract", "repository_contract", "evidence"),
        _nested_json_object(metadata, "origin", "repository_contract"),
        _nested_json_object(metadata, "origin", "repository_contract", "evidence"),
        _nested_json_object(metadata, "repository_contract"),
        _nested_json_object(metadata, "repository_contract", "evidence"),
    ]
    required: List[str] = []
    seen = set()
    for container in containers:
        if not container:
            continue
        for key in _REQUIRED_CHANGED_FILE_KEYS:
            for path in _metadata_path_list(container.get(key)):
                if path not in seen:
                    seen.add(path)
                    required.append(path)
    return required


def _repo_path_satisfies_requirement(changed_path: str, required_path: str) -> bool:
    changed = _normalize_repo_relative_path(changed_path)
    required = _normalize_repo_relative_path(required_path)
    if not changed or not required:
        return False
    if any(char in required for char in "*?["):
        return fnmatch.fnmatchcase(changed, required)
    return changed == required


def _truthy_env(name: str, default: str = "") -> bool:
    value = os.environ.get(name, default).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive integer config knob, falling back to *default*.

    Empty / unparseable / below-*minimum* values fall back to the default so a
    misconfigured env var can never DISABLE a safety cap (only widen/narrow it
    within sane bounds).
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


# Decomposition guardrails (T1 — bound runaway auto-decompose). Enforced
# server-side in ControlPlane.add_child_tasks so NEITHER decomposition path
# (the executor prompt that tells the agent to POST /children, nor the
# declarative maybe_auto_decompose manifest path) can exceed them regardless of
# agent behavior. Both are overridable via env for operators who need wider
# trees, but never below the floor of 1.
DEFAULT_MAX_CHILD_TASKS_PER_PARENT = 10
DEFAULT_MAX_DECOMPOSE_DEPTH = 2






def _failure_diagnosis(target_state: str, detail: Optional[Dict[str, Any]]) -> Optional[str]:
    """Map a block/fail transition to a glanceable 'Problem / Remediation' note.

    Failures should explain themselves ON the task (surfaced by `mac task show`/
    `summary`) instead of forcing an operator to SSH and dig through logs. Returns
    a short two-line string for the per-task activity log, or None when the
    transition is not a diagnosable failure. Signature-matched against the failure
    modes seen on the live fleets; falls back to a generic note.
    """
    if target_state not in (TaskState.BLOCKED.value, TaskState.FAILED.value):
        return None
    detail = detail or {}
    reason = str(detail.get("reason") or "").strip()
    error = str(detail.get("error") or "").strip()
    problems = detail.get("problems") or []
    problems_text = "; ".join(str(p) for p in problems) if isinstance(problems, list) else str(problems)
    blob = " ".join([reason, error, problems_text]).lower()

    def note(problem: str, remediation: str) -> str:
        return "Problem: %s\nRemediation: %s" % (problem.strip(), remediation.strip())

    if "could not clone" in blob or "authentication failed" in blob or "saml" in blob or (
        "clone" in blob and ("denied" in blob or "invalid username" in blob or "403" in blob or "sso" in blob)
    ):
        return note(
            "Repository clone/auth failed — the agent could not fetch the repo (%s)." % (error or reason or "git clone error"),
            "The fleet's git token isn't authorized for this repo/org (often SAML SSO). Provision an SSO-authorized token as GH_TOKEN (deploy with MAC_DEPLOY_GH_TOKEN) or SSO-authorize the deploy key; onboard with the https URL so the token is injected.",
        )
    if "heartbeat_offline" in blob or "lease_expired" in blob or "lease expired" in blob:
        return note(
            "Agent went offline mid-task (lease expired / heartbeat lost).",
            "Check agent<->hub connectivity (reverse tunnel), agent process health (crash/restart/OOM), or a long synchronous op (e.g. a large clone during repo-prep) blocking the heartbeat. Often transient during a deploy/restart — retry once agents are idle+healthy.",
        )
    if "timed out" in blob or "timeout" in blob or "rc=124" in blob or "returncode 124" in blob or "code: 124" in blob:
        return note(
            "Agent run timed out — the task is likely too large for one run.",
            "Raise MAC_EXECUTOR_AGENT_TIMEOUT for heavier work and/or split into child tasks (add_child_tasks / decompose-on-failure). Pre-bake slow toolchains into the sandbox image so setup doesn't consume the budget.",
        )
    if reason == "verification_contract_failed" or "refusing to push" in blob or "pushed=true" in blob or "contract" in blob:
        return note(
            "Contract verification failed — work was not pushed/accepted (%s)." % (problems_text or error or "see evidence")[:280],
            "Run the repository contract test in the worktree and make it pass cleanly (incl. lint/guard/docs tests), commit ALL changes (no untracked files), and push the branch before declaring done.",
        )
    if "review_retraction_cap_hit" in blob or "review_verdict_wait_cap_hit" in blob or "reviewer" in blob:
        return note(
            "Review never completed (reviewer unavailable or timed out).",
            "Ensure a free, fresh reviewer (don't run every agent executing at once); raise MAC_REVIEW_RETRACTION_CAP / MAC_REVIEW_VERDICT_WAIT_CAP / MAC_DEFAULT_REVIEWER_STALE_AFTER_SECONDS. Heavy reviews need the review heartbeat to stay alive.",
        )
    if "max attempt" in blob:
        return note(
            "Task failed after exhausting its retry budget (max_attempts).",
            "Inspect the per-attempt failures in history for the recurring cause; fix it, then `mac task reopen` (resets attempts) to retry, or decompose if the task is too large.",
        )
    if reason or problems_text or error:
        return note(
            "Task %s: %s" % (target_state, reason or problems_text or error),
            "Inspect the task evidence + history (`mac task show`) and the agent workspace logs for the root cause.",
        )
    return None


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-._").lower()
    return slug or "repo"


def _safe_git_ref(value: str) -> bool:
    return bool(
        value
        and not value.startswith("-")
        and re.match(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,127}$", value)
    )


def _remote_branch_from_ref(remote_ref: str) -> str:
    ref = str(remote_ref or "").strip()
    if not ref:
        return ""
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
            break
    if ref.startswith("origin/"):
        ref = ref[len("origin/"):]
    if _safe_git_ref(ref) and not ref.startswith("refs/"):
        return ref
    return ""


_SCP_GIT_URL_RE = re.compile(r"^(?P<user>[^@]+@)?(?P<host>[^:/]+):(?P<path>.+)$")


def _canonicalize_git_url(url: str) -> Optional[Tuple[str, str]]:
    """Canonicalize a git remote URL to ``(host, path)`` for equivalence
    comparisons across ``git@host:path``, ``ssh://host/path``,
    ``https://host/path``, and ``git://host/path`` forms.

    Trailing ``.git`` and surrounding slashes are stripped from the path;
    the host is lowercased. Returns ``None`` if the URL is unparseable so
    callers can choose to fail-closed without crashing.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    if "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        host = parsed.hostname or ""
        path = parsed.path or ""
    else:
        m = _SCP_GIT_URL_RE.match(raw)
        if not m:
            return None
        host = m.group("host") or ""
        path = m.group("path") or ""
    host = host.lower().strip()
    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not host or not path:
        return None
    return (host, path)


REPOSITORY_CONTRACT_SCHEMA = "mac.repository_contract.v1"
REPOSITORY_CONTRACT_FILES = (
    Path(".mac") / "project.yaml",
    Path(".mac") / "project.yml",
)
VERIFICATION_SCHEMA = "mac.worker_evidence.v1"
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
# mac-5xwh: bd issue ids look like ``<prefix>-<slug>`` where the slug
# may itself contain dashes (e.g. ``mac-defer-claim``). Reject anything
# that could be misread as a CLI flag (leading ``-``) or that contains
# whitespace, redirects, or quoting metacharacters.
_BEAD_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-[A-Za-z0-9_][A-Za-z0-9_\-]*$")

# Bytes of cleartext attestation key per agent. 256 bits of HMAC key is
# overkill for the threat model but fits in one stretch of base64 and
# keeps the door closed if HMAC-SHA256 ever becomes the bottleneck.
ATTESTATION_KEY_BYTES = 32


def _generate_attestation_key() -> str:
    """Mint a fresh per-agent HMAC key. Returned base64url so it fits
    in a single env var or JSON string without escaping."""
    import secrets as _secrets

    return base64.urlsafe_b64encode(_secrets.token_bytes(ATTESTATION_KEY_BYTES)).decode("ascii").rstrip("=")


def _contract_mapping(value: Any, field: str) -> JsonDict:
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % field)
    return value


def _contract_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s must be a non-empty string" % field)
    return value.strip()


def _contract_string_list(value: Any, field: str, *, required: bool = True) -> List[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list):
        raise ValidationError("%s must be a list of strings" % field)
    strings = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError("%s must contain only non-empty strings" % field)
        strings.append(item.strip())
    if required and not strings:
        raise ValidationError("%s must not be empty" % field)
    return strings


def _contract_relative_paths(value: Any, field: str) -> List[str]:
    paths = _contract_string_list(value, field, required=False)
    for raw_path in paths:
        candidate = Path(raw_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValidationError("%s entries must be relative paths inside the repository" % field)
    return paths


def _repository_contract_root(repo_path: Path) -> Path:
    expanded = repo_path.expanduser()
    if not expanded.exists():
        raise ValidationError("project repository path does not exist: %s" % repo_path)
    return expanded if expanded.is_dir() else expanded.parent


_ONBOARDING_REMOTE_URL_RE = re.compile(r"^(https://|git@|ssh://|git://)\S+$")


def _normalize_onboarding_remote_url(value: str) -> str:
    """Light validation of a git remote URL for onboarding (the worker
    re-validates strictly with its own regex before cloning)."""
    url = (value or "").strip()
    if not url or url.startswith("-"):
        raise ValidationError("repository_url is empty or looks like a flag: %r" % value)
    if len(url) > 2048:
        raise ValidationError("repository_url exceeds 2048 byte limit")
    if not _ONBOARDING_REMOTE_URL_RE.match(url):
        raise ValidationError(
            "repository_url must be an https://, git@, ssh:// or git:// git remote: %r" % value
        )
    return url


def _repository_name_from_url(url: str) -> str:
    """Derive a short project/repo name from a git remote URL.

    ``https://github.com/NVIDIA-dev/taskbrain.git`` -> ``taskbrain``;
    ``git@github.com:NVIDIA-dev/taskbrain.git``     -> ``taskbrain``.
    """
    tail = url.rstrip("/").split("/")[-1]
    if "/" not in tail and ":" in tail:  # scp-style git@host:org/repo with no extra slash
        tail = tail.split(":")[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    name = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in tail).strip("-._")
    return name or "project"


def _build_onboarding_description(url: str, repo_name: str) -> str:
    return "\n".join(
        [
            "Repository onboarding for %s (%s)." % (repo_name, url),
            "",
            "MAC has cloned a clean, writable checkout for you at $MAC_TASK_REPO_WORKTREE (a task branch). Work entirely there.",
            "This is READ-ONLY with respect to the remote: do NOT push or open a pull request. You MAY write local analysis files in the checkout (notably .mac/project.yaml).",
            "If CodeGraph is available, initialize it for local API/code behavior analysis with `codegraph init`; `.codegraph/` is generated local state and must not be committed or included as a deliverable.",
            "",
            "Start from the repo's own self-description. Read these files first if they exist and treat them as authoritative for intent, not just for code: README.md (what it is / how to build), AGENTS.md (instructions for AI agents working here), and PLAN.md (roadmap / planned work). Fold what they say into the deliverables below; do not contradict them without explaining why.",
            "",
            "ENVIRONMENT CONTRACT (derive before authoring the repository contract):",
            "  Run `from mac.environment_contract import derive_environment_contract, validate_environment_contract`",
            "  or call the CLI equivalent to statically analyse the checkout and emit the environment contract",
            "  (schema mac.environment_contract.v1).  The contract captures:",
            "    - runtime_versions: node_min / python_min / pnpm_min derived from engines, packageManager,",
            "      .nvmrc, .node-version, pyproject.toml, setup.cfg, lockfile headers.",
            "    - native_build.required: True when binding.gyp, Cargo.toml, go.mod, CMakeLists.txt,",
            "      or a known-native npm package (e.g. @vscode/sqlite3, better-sqlite3, sharp) is present.",
            "    - egress.hosts: registry URLs from .npmrc / lockfiles, plus nodejs.org when native_build.",
            "  After derivation, call validate_environment_contract() to run preflight checks against the",
            "  current sandbox.  If preflight.status == 'fail', report the precise error and STOP — do not",
            "  attempt install/build steps that will fail for the same root cause.",
            "  Include the full environment contract JSON in your evidence.",
            "",
            "Deliverables — report all of these in your evidence (evidence_type=investigation):",
            "  1. A concise summary of what the project does and its architecture (languages, frameworks, key modules, entry points), grounded in README.md/AGENTS.md/PLAN.md where present.",
            "  2. How to build it and run its tests, inferred from the repo's own manifests/CI and README — not guessed.",
            "  3. The environment contract (mac.environment_contract.v1) derived from static analysis of the checkout.",
            "  4. An authored repository contract written to .mac/project.yaml using schema mac.repository_contract.v1 (keys: schema, project, platforms, toolchain.required_commands, bootstrap.command, test.command, evidence.required). Include its full content in the evidence.",
            "  5. A prioritized backlog of 5-10 concrete next steps, improvements, or risks you observe. If PLAN.md exists, reconcile your backlog against it (what is already planned vs. newly surfaced).",
        ]
    )


def _load_repository_contract(repo_path: Path) -> JsonDict:
    root = _repository_contract_root(repo_path)
    checked = []
    for relative in REPOSITORY_CONTRACT_FILES:
        candidate = root / relative
        checked.append(str(relative))
        if not candidate.exists():
            continue
        try:
            raw = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValidationError("repository runtime contract is invalid YAML: %s: %s" % (candidate, exc)) from exc
        try:
            contract_path = str(candidate.relative_to(root))
        except ValueError:
            contract_path = str(candidate)
        return _normalize_repository_contract(raw, contract_path)
    raise ValidationError(
        "repository runtime contract not found under %s; expected one of: %s"
        % (root, ", ".join(checked))
    )


def _tail_text(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def _ensure_codegraph_git_exclude(repo_path: Path) -> Optional[str]:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            [git, "rev-parse", "--git-path", "info/exclude"],
            cwd=str(repo_path),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    exclude_path = Path(result.stdout.strip())
    if not exclude_path.is_absolute():
        exclude_path = repo_path / exclude_path
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        lines = {line.strip() for line in existing.splitlines()}
        if ".codegraph/" not in lines:
            suffix = "" if existing.endswith("\n") or not existing else "\n"
            exclude_path.write_text(existing + suffix + ".codegraph/\n", encoding="utf-8")
    except OSError:
        return None
    return str(exclude_path)


def _resolve_codegraph_binary() -> Optional[str]:
    found = shutil.which("codegraph")
    if found:
        return found
    home = Path.home()
    candidates: List[Path] = []
    mac_home = os.environ.get("MAC_HOME")
    if mac_home:
        candidates.append(Path(mac_home).expanduser() / "bin" / "codegraph")
    candidates.extend(
        [
            home / ".mac" / "bin" / "codegraph",
            home / ".codegraph" / "bin" / "codegraph",
            home / ".local" / "bin" / "codegraph",
            home / ".cargo" / "bin" / "codegraph",
            home / "bin" / "codegraph",
            Path("/opt/homebrew/bin/codegraph"),
            Path("/usr/local/bin/codegraph"),
        ]
    )
    for candidate in candidates:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


def _initialize_codegraph_repository(repo_path: Path) -> JsonDict:
    status: JsonDict = {
        "schema": "mac.codegraph_init.v1",
        "command": "codegraph init",
        "attempted": False,
        "initialized": False,
    }
    if not repo_path.exists() or not repo_path.is_dir():
        status["reason"] = "repository_path_not_directory"
        return status
    git = shutil.which("git")
    if git is None:
        status["reason"] = "git_unavailable"
        return status
    try:
        inside = subprocess.run(
            [git, "rev-parse", "--is-inside-work-tree"],
            cwd=str(repo_path),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        status["reason"] = "git_probe_failed"
        status["error"] = str(exc)
        return status
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        status["reason"] = "not_git_worktree"
        if inside.stderr.strip():
            status["stderr"] = _tail_text(inside.stderr)
        return status
    codegraph = _resolve_codegraph_binary()
    if codegraph is None:
        status["reason"] = "codegraph_unavailable"
        return status
    status["binary"] = codegraph
    status["git_exclude"] = _ensure_codegraph_git_exclude(repo_path)
    status["attempted"] = True
    try:
        result = subprocess.run(
            [codegraph, "init"],
            cwd=str(repo_path),
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        status["reason"] = "codegraph_init_failed"
        status["error"] = str(exc)
        return status
    status["returncode"] = result.returncode
    if result.stdout:
        status["stdout"] = _tail_text(result.stdout)
    if result.stderr:
        status["stderr"] = _tail_text(result.stderr)
    if result.returncode == 0:
        status["initialized"] = True
    else:
        status["reason"] = "codegraph_init_nonzero"
    return status


def _raise_for_codegraph_init_failure(status: JsonDict) -> None:
    if not status.get("attempted") or status.get("initialized"):
        return
    reason = str(status.get("reason") or "codegraph_init_failed")
    detail = str(status.get("stderr") or status.get("stdout") or status.get("error") or "").strip()
    if detail:
        detail = ": " + _tail_text(detail, limit=500)
    raise ValidationError("codegraph init failed (%s)%s" % (reason, detail))


def _normalize_repository_contract(raw: Any, contract_path: str) -> JsonDict:
    data = _contract_mapping(raw, "repository runtime contract")
    schema = _contract_string(data.get("schema"), "repository runtime contract.schema")
    if schema != REPOSITORY_CONTRACT_SCHEMA:
        raise ValidationError(
            "repository runtime contract.schema must be %s" % REPOSITORY_CONTRACT_SCHEMA
        )
    project = _contract_string(data.get("project"), "repository runtime contract.project")
    platforms = _contract_string_list(data.get("platforms"), "repository runtime contract.platforms")
    toolchain = _contract_mapping(data.get("toolchain"), "repository runtime contract.toolchain")
    bootstrap = _contract_mapping(data.get("bootstrap"), "repository runtime contract.bootstrap")
    test = _contract_mapping(data.get("test"), "repository runtime contract.test")
    evidence = _contract_mapping(data.get("evidence"), "repository runtime contract.evidence")
    canonical_remote_url_raw = data.get("canonical_remote_url")
    canonical_remote_url: Optional[str] = None
    if canonical_remote_url_raw is not None:
        canonical_remote_url = _contract_string(
            canonical_remote_url_raw,
            "repository runtime contract.canonical_remote_url",
        )
        if _canonicalize_git_url(canonical_remote_url) is None:
            raise ValidationError(
                "repository runtime contract.canonical_remote_url is not a parseable git URL: %r"
                % canonical_remote_url
            )
    return {
        "schema": schema,
        "project": project,
        "contract_path": contract_path,
        "canonical_remote_url": canonical_remote_url,
        "platforms": platforms,
        "toolchain": {
            "required_commands": _contract_string_list(
                toolchain.get("required_commands"),
                "repository runtime contract.toolchain.required_commands",
            ),
        },
        "bootstrap": {
            "command": _contract_string(
                bootstrap.get("command"),
                "repository runtime contract.bootstrap.command",
            ),
            "creates": _contract_relative_paths(
                bootstrap.get("creates"),
                "repository runtime contract.bootstrap.creates",
            ),
        },
        "test": {
            "command": _contract_string(test.get("command"), "repository runtime contract.test.command"),
        },
        "evidence": {
            "required": _contract_string_list(
                evidence.get("required"),
                "repository runtime contract.evidence.required",
            ),
        },
    }


def _canonicalize_for_signature(manifest: Dict[str, Any]) -> bytes:
    """Deterministic JSON encoding of the verification manifest for
    HMAC signing.

    mac-wu3f: include ``signed_by`` in the canonical form so the
    signer identity is cryptographically bound into the MAC. Previously
    ``signed_by`` was excluded — a captured signature could be replayed
    in a manifest with a different ``signed_by``, and verification
    (which keys off the new ``signed_by``) might still pass if both
    agents shared a key. The ``signature`` field is still excluded
    because it's the output, not the input.
    """
    excluded = {"signature"}
    filtered = {k: v for k, v in manifest.items() if k not in excluded}
    return json_dumps(filtered).encode("utf-8")


def _hub_review_verify_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Option C: hub runs the review contract test itself (one controlled
    sandbox) instead of dispatching to a reviewer agent. Off by default; the
    hub deploy sets MAC_REVIEW_HUB_VERIFY=1."""
    env = os.environ if environ is None else environ
    return str(env.get("MAC_REVIEW_HUB_VERIFY") or "").strip().lower() in {"1", "true", "yes", "on"}


def sign_verification_manifest(key: str, manifest: Dict[str, Any]) -> str:
    """Sign ``manifest`` with the agent's attestation key. Returns the
    base64url HMAC tag. Exposed for the worker (writes signatures) and
    for tests (constructs signed evidence fixtures)."""
    import hmac as _hmac
    import hashlib as _hashlib

    digest = _hmac.new(
        key.encode("ascii"), _canonicalize_for_signature(manifest), _hashlib.sha256
    ).digest()
    return "v1:" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_verification_manifest_signature(
    key: str, manifest: Dict[str, Any], signature: str
) -> bool:
    """Constant-time HMAC verification. Returns True iff ``signature``
    matches the expected tag for ``manifest`` under ``key``."""
    import hmac as _hmac

    if not signature or not signature.startswith("v1:"):
        return False
    expected = sign_verification_manifest(key, manifest)
    return _hmac.compare_digest(expected, signature)


class ControlPlane:
    """Application service layer for the multi-agent control plane."""

    def __init__(
        self,
        store: Optional[Store] = None,
        secret_key: Optional[str] = None,
    ) -> None:
        # When no store is injected, pick an explicitly configured backend:
        # MAC_DATABASE_URL -> PostgresStore, or MAC_DB -> SQLiteStore. Missing
        # configuration is an error; a client-home database is never inferred.
        # This is what makes multi-replica mac-api stateless — every
        # replica hits the shared CNPG cluster without any code change.
        self.store: Store = store or make_store_from_env()
        raw_key = secret_key if secret_key is not None else os.environ.get("MAC_SECRET_KEY")
        if not raw_key:
            raise ValidationError(
                "MAC_SECRET_KEY is required (32+ chars). Set it in the environment or pass secret_key explicitly."
            )
        if len(raw_key) < 32:
            raise ValidationError("MAC_SECRET_KEY must be at least 32 characters")
        # Refuse common placeholder substrings so the example env file in
        # deploy/systemd/mac.env.example cannot be deployed verbatim. The
        # placeholder is long enough to satisfy the length check, but lands
        # every secret under a globally-known Fernet key. Better to fail loud
        # at startup than encrypt with a known constant.
        placeholder_substrings = (
            "REPLACE-ME",
            "REPLACE_ME",
            "CHANGE-ME",
            "CHANGE_ME",
            "your-key-here",
            "xxxxxxxx",
        )
        for marker in placeholder_substrings:
            if marker.lower() in raw_key.lower():
                raise ValidationError(
                    "MAC_SECRET_KEY appears to be a placeholder (%r). "
                    "Generate one with: openssl rand -base64 48" % marker
                )
        fernet_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"mac.control_plane.secrets.v1",
            info=b"fernet-key",
        ).derive(raw_key.encode("utf-8"))
        self._fernet = Fernet(base64.urlsafe_b64encode(fernet_key))
        # Domain sub-services. New domains should land here as their own
        # service classes rather than as more methods on ControlPlane.
        self.task_ledger = TaskLedgerService(self.store)
        self.dispatch = DispatchService(self)
        self._task_outbox_drain_lock = threading.Lock()
        self.reconciliation = ReconciliationCoordinator(self.store)
        self.identity = IdentityService(self.store)
        self.action_events = ActionEventService(self.store)
        self.observability = ObservabilityService(
            self.store,
            action_event_recorder=self.action_events.project_observability,
        )
        self.retention = RetentionService(
            self.store,
            observability_recorder=self._retention_obs_recorder,
        )
        self.openshell = OpenShellService(self.store, get_agent=self.get_agent)
        self.agentbus = AgentBusService(self.store, self.observability)
        self.provisioning = ProvisioningService(self.store, self.observability)
        self.service_roles = ServiceRoleService(self.store, self.observability)
        self.roles = RolesService(
            self.store,
            self.observability,
            get_tenant=self.get_tenant,
            get_agent=self.get_agent,
            get_machine=self.get_machine,
            get_hermes_instance=self.identity.get_hermes_instance,
            get_persona=self.identity.get_persona,
        )
        self.workflows = WorkflowService(
            self.store,
            self.observability,
            get_role=self.roles.get_role,
            get_tenant=self.get_tenant,
        )
        self.workflow_runtime = WorkflowRuntime(
            self.store,
            self.observability,
            self.workflows,
            self.roles,
            create_task=self.create_task,
            transition_task=self.transition_task,
            transition_task_in_transaction=self._transition_task_in_transaction,
            get_task=self.get_task,
            record_history=self._record_history,
            drain_task_transition_outbox=self.drain_task_transition_outbox,
            reconciliation=self.reconciliation,
        )
        self.secrets = SecretsService(
            self.store,
            self.observability,
            self._fernet,
            get_agent=self.get_agent,
            get_machine=self.get_machine,
            machine_allows_tenant=self._machine_allows_tenant,
        )
        self.memory = MemoryService(
            self.store,
            get_task=self.get_task,
            get_evidence=self.get_evidence,
            get_platform_binding=self.get_platform_binding,
            record_history=self._record_history,
        )
        self.messaging = MessagingService(
            self.store,
            get_agent=self.get_agent,
            get_task=self.get_task,
        )
        self.notifiers = NotifierService(
            self.store,
            list_agents=self.list_agents,
            get_agent=self.get_agent,
            list_platform_bindings=self.identity.list_platform_bindings,
            get_platform_binding=self.identity.get_platform_binding,
            send_message=self.send_message,
            record_log=self.record_log,
        )
        self.evaluations = EvalService(
            self.store,
            self.observability,
            get_evidence=self.get_evidence,
        )
        self.reviews = ReviewService(
            self.store,
            self.observability,
            self.messaging,
            get_task=self.get_task,
            get_agent=self.get_agent,
            get_evidence=self.get_evidence,
            transition_task=self.transition_task,
            transition_task_in_transaction=self._transition_task_in_transaction,
            record_history=self._record_history,
            find_verdict_evidence=self._find_review_verdict_evidence,
            reviewer_eligibility_check=self._reviewer_assignment_problem,
            drain_task_transition_outbox=self.drain_task_transition_outbox,
        )
        self.agent_state = AgentStateService(
            self.store,
            self.observability,
            get_agent=self.get_agent,
            get_evidence=self.get_evidence,
            agent_has_active_lease=self._agent_has_active_lease,
        )
        self.deploy = DeployService(
            self.store,
            self.observability,
            get_tenant=self.get_tenant,
            get_task=self.get_task,
            get_agent=self.get_agent,
            get_evidence=self.get_evidence,
        )
        self.rollouts = RolloutService(
            self.store,
            self.observability,
            get_tenant=self.get_tenant,
            get_runtime=self.get_runtime,
            get_eval_set=self.get_eval_set,
            create_task=self.create_task,
            add_memory=self.add_memory,
            task_from_row=self._task_from_row,
            deploy_artifact=self.deploy.deploy_artifact,
            get_artifact_by_digest=self.deploy.get_artifact,
            get_environment=self.deploy.get_environment,
            current_deployment=self.deploy.current_deployment,
        )

    @classmethod
    def in_memory(cls) -> "ControlPlane":
        return cls(SQLiteStore(":memory:"), secret_key="test-key-with-enough-entropy-32+chars")

    def _resolved_json_column(
        self,
        table: str,
        column: str,
        row_id: str,
        value: Optional[Dict[str, Any]],
    ) -> str:
        """Resolve a JSON column for register-style upserts.

        If the caller explicitly passed a value, use it. Otherwise preserve the
        existing row's value (so re-registering with no metadata does not wipe
        previously-stored metadata). Defaults to {} for new rows.
        """
        if value is not None:
            return json_dumps(ensure_json_object(value))
        row = self.store.query_one(
            "SELECT %s AS value FROM %s WHERE id = ?" % (column, table),
            (row_id,),
        )
        if row is None or row["value"] is None:
            return json_dumps({})
        return row["value"]

    def _agent_resources_with_preserved_control_plane_fields(
        self,
        agent_id: str,
        resources: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Merge self-reported resources without erasing hub-owned policy.

        ``startup_self_test`` is agent-produced but sticky when omitted.
        ``openshell_required`` is control-plane policy and is therefore always
        retained once the agent exists; operators change it through
        ``update_agent``/OpenShell reconciliation, not self-registration or a
        heartbeat inventory refresh.
        """
        if resources is None:
            raw = self._resolved_json_column("agents", "resources", agent_id, None)
            return ensure_json_object(json_loads(raw, {}))
        resource_value = ensure_json_object(resources)
        row = self.store.query_one("SELECT resources FROM agents WHERE id = ?", (agent_id,))
        if row is None:
            return resource_value
        existing = ensure_json_object(json_loads(row["resources"], {}))
        merged = dict(resource_value)
        if "startup_self_test" not in merged and "startup_self_test" in existing:
            merged["startup_self_test"] = existing["startup_self_test"]
        if "openshell_required" in existing:
            merged["openshell_required"] = existing["openshell_required"]
        return merged

    @staticmethod
    def _startup_self_test_degrades_health(resources: Dict[str, Any]) -> bool:
        startup = resources.get("startup_self_test")
        if not isinstance(startup, dict):
            return False
        status = str(startup.get("status") or "").strip().lower()
        if status in {"degraded", "failed"}:
            return True
        return bool(str(startup.get("hermes_failure_class") or "").strip())

    def _project_agent_health_for_resources(
        self,
        current_health: str,
        requested_health: Optional[str],
        resources: Dict[str, Any],
    ) -> Optional[str]:
        if not self._startup_self_test_degrades_health(resources):
            return requested_health
        if requested_health is None:
            return HealthStatus.DEGRADED.value if current_health == HealthStatus.HEALTHY.value else None
        if requested_health == HealthStatus.HEALTHY.value:
            return HealthStatus.DEGRADED.value
        return requested_health

    # Human-facing identity + Hermes boundary: thin facade over
    # ``self.identity``. New code should call ``cp.identity.<method>``.

    def register_tenant(self, *args: Any, **kwargs: Any) -> Tenant:
        return self.identity.register_tenant(*args, **kwargs)

    def get_tenant(self, tenant_id_or_name: str) -> Tenant:
        return self.identity.get_tenant(tenant_id_or_name)

    def list_tenants(self) -> List[Tenant]:
        return self.identity.list_tenants()

    def register_user(self, *args: Any, **kwargs: Any) -> User:
        return self.identity.register_user(*args, **kwargs)

    def get_user(self, user_id: str) -> User:
        return self.identity.get_user(user_id)

    def list_users(self, *args: Any, **kwargs: Any) -> List[User]:
        return self.identity.list_users(*args, **kwargs)

    def register_persona(self, *args: Any, **kwargs: Any) -> Persona:
        return self.identity.register_persona(*args, **kwargs)

    def get_persona(self, persona_id: str) -> Persona:
        return self.identity.get_persona(persona_id)

    def list_personas(self, *args: Any, **kwargs: Any) -> List[Persona]:
        return self.identity.list_personas(*args, **kwargs)

    def register_hermes_instance(self, *args: Any, **kwargs: Any) -> HermesInstance:
        return self.identity.register_hermes_instance(*args, **kwargs)

    def get_hermes_instance(self, instance_id: str) -> HermesInstance:
        return self.identity.get_hermes_instance(instance_id)

    def list_hermes_instances(self, *args: Any, **kwargs: Any) -> List[HermesInstance]:
        return self.identity.list_hermes_instances(*args, **kwargs)

    def register_platform_binding(self, *args: Any, **kwargs: Any) -> PlatformBinding:
        return self.identity.register_platform_binding(*args, **kwargs)

    def get_platform_binding(self, binding_id: str) -> PlatformBinding:
        return self.identity.get_platform_binding(binding_id)

    def list_platform_bindings(self, *args: Any, **kwargs: Any) -> List[PlatformBinding]:
        return self.identity.list_platform_bindings(*args, **kwargs)

    def hermes_context(self, hermes_instance_id: str) -> JsonDict:
        return self.identity.hermes_context(hermes_instance_id)

    def hermes_work_context(
        self,
        hermes_instance_id: str,
        *,
        include_completed: bool = True,
        task_limit: int = 100,
    ) -> JsonDict:
        """MAC-authoritative operational view for a Hermes runtime.

        Hermes owns personality and user memory, but MAC owns task/project/agent
        state. This projection is the bridge contract Hermes can load when it
        needs to reason about work with the same durable objects operators see.
        """

        identity_context = self.hermes_context(hermes_instance_id)
        instance = self.get_hermes_instance(hermes_instance_id)
        tenant_id = instance.tenant_id
        all_tenant_tasks = self.list_tasks(tenant_id=tenant_id)
        visible_tasks = [
            task
            for task in all_tenant_tasks
            if include_completed or task.state not in TERMINAL_TASK_STATES
        ]
        limit = min(max(1, int(task_limit)), 500)
        limited_tasks = visible_tasks[:limit]
        agents = self.list_agents()
        fleets = [
            fleet.to_dict()
            for fleet in self.list_fleets()
            if fleet.tenant_id in (None, tenant_id)
        ]
        project_items = [item.to_dict() for item in self.list_project_items()]
        repositories = [repo.to_dict() for repo in self.list_project_repositories()]
        return {
            "schema": "mac.hermes_work_context.v1",
            "authority": {
                "fleets": "mac",
                "tasks": "mac",
                "projects": "mac",
                "agents": "mac",
                "personality": "hermes",
                "user_memory": "hermes",
            },
            "tenant": identity_context["tenant"],
            "hermes_instance": identity_context["hermes_instance"],
            "persona": identity_context["persona"],
            "platform_bindings": identity_context["platform_bindings"],
            "memory_contract": identity_context["memory_contract"],
            "fleets": fleets,
            "projects": self._hermes_project_contexts(
                all_tenant_tasks,
                agents,
                project_items,
                repositories,
                [project.to_dict() for project in self.list_project_records()],
            ),
            "tasks": [self._hermes_task_context(task) for task in limited_tasks],
            "task_count": len(visible_tasks),
            "task_limit": limit,
            "task_truncated": len(visible_tasks) > limit,
            "agents": [
                self._hermes_agent_context(agent, all_tenant_tasks)
                for agent in agents
            ],
            "relationships": self._hermes_work_relationships(all_tenant_tasks, agents),
            "operations": self._hermes_operation_contract(hermes_instance_id),
        }

    def hermes_runtime_proof(
        self,
        hermes_instance_id: str,
        *,
        hermes_startup: Optional[JsonDict] = None,
    ) -> JsonDict:
        """Return an auditable proof that MAC/Hermes work semantics align."""

        work_context = self.hermes_work_context(
            hermes_instance_id,
            include_completed=False,
            task_limit=100,
        )
        instance = work_context["hermes_instance"]
        operations = work_context["operations"]
        api_operation_names = {
            str(operation.get("name"))
            for operation in operations.get("api", [])
            if isinstance(operation, dict)
        }
        expected_task_api_operations = {
            "create_task_from_conversation",
            "list_tasks",
            "get_task",
            "update_task",
            "delete_task",
            "add_child_tasks",
            "get_task_summary",
            "claim_next_task",
            "claim_task",
            "start_task",
            "transition_task",
            "add_evidence",
            "submit_for_review",
            "request_review",
            "claim_review",
            "submit_review",
            "publish_task",
            "record_command_audit",
            "list_command_audit",
            "write_completed_task_to_memory",
        }
        expected_project_api_operations = {
            "create_project",
            "list_projects",
            "get_project",
            "update_project",
            "delete_project",
            "import_project_item",
            "list_project_items",
            "register_project_repository",
            "list_project_repositories",
        }
        expected_agent_api_operations = {
            "create_agent",
            "list_agents",
            "get_agent",
            "update_agent",
            "disable_agent",
            "delete_agent",
            "get_agent_identity",
            "claim_next_task",
            "record_command_audit",
            "list_command_audit",
        }
        expected_fleet_api_operations = {
            "create_fleet",
            "list_fleets",
            "get_fleet",
            "update_fleet",
            "delete_fleet",
        }
        mac_cli_commands = [str(command) for command in operations.get("mac_cli", [])]
        mac_hermes_commands = [
            str(command) for command in operations.get("mac_hermes_cli", [])
        ]
        expected_api_operations = {
            "get_work_context",
            "get_runtime_proof",
        } | expected_task_api_operations | expected_project_api_operations | expected_agent_api_operations | expected_fleet_api_operations
        expected_cli_fragments = (
            "mac-hermes work-context",
            "mac-hermes runtime-proof",
            "mac-hermes projects",
            "mac-hermes project-detail",
            "mac-hermes import-project-item",
            "mac-hermes project-items",
            "mac-hermes project-repositories",
            "mac-hermes register-project-repository",
            "mac-hermes claim-next",
            "mac-hermes tasks",
            "mac-hermes task ",
            "mac-hermes task-detail",
            "mac-hermes claim",
            "mac-hermes start",
            "mac-hermes transition",
            "mac-hermes evidence",
            "mac-hermes submit-review",
            "mac-hermes request-review",
            "mac-hermes claim-review",
            "mac-hermes review-decision",
            "mac-hermes publish",
            "mac-hermes command-audit",
            "mac-hermes web-search",
            "mac-hermes web-scrape",
            "mac-hermes web-crawl",
            "mac-hermes writeback",
        )
        expected_agent_cli_fragments = (
            "mac-hermes agents",
            "mac-hermes agent-detail",
            "mac-hermes agent-identity",
            "mac-hermes claim-next",
            "mac-hermes command-audit",
        )
        authority = work_context.get("authority", {})
        project_contexts = [
            project
            for project in work_context.get("projects", [])
            if isinstance(project, dict)
        ]
        tenant_id = instance.get("tenant_id") if isinstance(instance, dict) else None
        live_tenant_tasks = self.list_tasks(tenant_id=tenant_id)
        live_visible_tasks = [
            task for task in live_tenant_tasks if task.state not in TERMINAL_TASK_STATES
        ]
        live_task_contexts = [
            self._hermes_task_context(task)
            for task in live_visible_tasks[: int(work_context.get("task_limit") or 0)]
        ]
        context_tasks = [
            task for task in work_context.get("tasks", []) if isinstance(task, dict)
        ]
        live_task_by_id = {str(task.get("id")): task for task in live_task_contexts}
        context_task_by_id = {str(task.get("id")): task for task in context_tasks}
        task_ids_ready = (
            set(context_task_by_id) <= set(live_task_by_id)
            if bool(work_context.get("task_truncated"))
            else set(context_task_by_id) == set(live_task_by_id)
        )
        task_fields_ready = all(
            context_task_by_id[task_id] == live_task_by_id.get(task_id)
            for task_id in context_task_by_id
        )
        live_agents = self.list_agents()
        live_agent_contexts = [
            self._hermes_agent_context(agent, live_tenant_tasks)
            for agent in live_agents
        ]
        context_agents = [
            agent for agent in work_context.get("agents", []) if isinstance(agent, dict)
        ]
        live_agent_by_id = {str(agent.get("id")): agent for agent in live_agent_contexts}
        context_agent_by_id = {str(agent.get("id")): agent for agent in context_agents}
        agent_fields = (
            "id",
            "name",
            "status",
            "health_status",
            "current_task_id",
            "active_task_ids",
            "active_projects",
            "hermes_instance_id",
        )
        agent_fields_ready = all(
            {
                field: context_agent_by_id[agent_id].get(field)
                for field in agent_fields
            }
            == {
                field: live_agent_by_id.get(agent_id, {}).get(field)
                for field in agent_fields
            }
            for agent_id in context_agent_by_id
        )
        live_project_contexts = self._hermes_project_contexts(
            live_tenant_tasks,
            live_agents,
            [item.to_dict() for item in self.list_project_items()],
            [repository.to_dict() for repository in self.list_project_repositories()],
            [project.to_dict() for project in self.list_project_records()],
        )
        live_alignment = {
            "schema": "mac.hermes.live_object_alignment.v1",
            "ready": (
                int(work_context.get("task_count") or 0) == len(live_visible_tasks)
                and task_ids_ready
                and task_fields_ready
                and live_project_contexts == project_contexts
                and set(context_agent_by_id) == set(live_agent_by_id)
                and agent_fields_ready
            ),
            "tasks": {
                "live_count": len(live_visible_tasks),
                "work_context_count": int(work_context.get("task_count") or 0),
                "work_context_visible_count": len(context_tasks),
                "truncated": bool(work_context.get("task_truncated")),
                "ids_ready": task_ids_ready,
                "fields_ready": task_fields_ready,
                "live_ids": [task.id for task in live_visible_tasks[:20]],
                "work_context_ids": list(context_task_by_id)[:20],
            },
            "projects": {
                "ready": live_project_contexts == project_contexts,
                "live_names": [str(project.get("project")) for project in live_project_contexts],
                "work_context_names": [str(project.get("project")) for project in project_contexts],
            },
            "fleets": {
                "ready": isinstance(work_context.get("fleets"), list),
                "work_context_names": [
                    str(fleet.get("name"))
                    for fleet in work_context.get("fleets", [])
                    if isinstance(fleet, dict)
                ],
            },
            "agents": {
                "ready": set(context_agent_by_id) == set(live_agent_by_id) and agent_fields_ready,
                "ids_ready": set(context_agent_by_id) == set(live_agent_by_id),
                "fields_ready": agent_fields_ready,
                "live_ids": list(live_agent_by_id)[:20],
                "work_context_ids": list(context_agent_by_id)[:20],
            },
        }
        bound_agents = [
            agent
            for agent in work_context.get("agents", [])
            if agent.get("hermes_instance_id") == hermes_instance_id
        ]
        dashboard_url_contract = self._hermes_dashboard_url_contract(
            hermes_instance_id,
            tasks=context_tasks,
            projects=project_contexts,
            agents=bound_agents or context_agents,
            fleets=[
                fleet
                for fleet in work_context.get("fleets", [])
                if isinstance(fleet, dict)
            ],
        )
        dashboard_operation_contract = (
            operations.get("dashboard")
            if isinstance(operations.get("dashboard"), dict)
            else {}
        )
        dashboard_operation_ready = (
            dashboard_operation_contract.get("entrypoint") == "/ui"
            and {
                "work",
                "projects",
                "map",
                "fleets",
                "agents",
                "tasks",
                "workflows",
                "hermes",
                "ops",
                "integrations",
                "runtime",
                "observability",
                "secrets",
            }
            <= set(dashboard_operation_contract.get("views") or [])
            and {
                "view",
                "project",
                "task_state",
                "selected",
                "agent_q",
                "agent_filter",
                "agent_sort",
                "agent_page",
            }
            <= set(dashboard_operation_contract.get("url_state_parameters") or [])
        )
        relationships = (
            work_context.get("relationships")
            if isinstance(work_context.get("relationships"), dict)
            else {}
        )
        runtime = (
            hermes_startup.get("task_project_runtime")
            if isinstance(hermes_startup, dict)
            else None
        )
        runtime = runtime if isinstance(runtime, dict) else {}
        prompt_bridge = runtime.get("prompt_bridge") if isinstance(runtime.get("prompt_bridge"), dict) else {}
        markdown_contract = (
            runtime.get("markdown_contract")
            if isinstance(runtime.get("markdown_contract"), dict)
            else {}
        )
        runtime_required = bool(runtime.get("required"))
        runtime_instance_id = runtime.get("hermes_instance_id")
        session_capabilities = {
            str(name)
            for name in (runtime.get("session_capability_names") or [])
            if str(name).strip()
        }
        runtime_first_class_objects = {
            str(name)
            for name in (runtime.get("first_class_object_names") or [])
            if str(name).strip()
        }
        expected_first_class_objects = {"fleets", "tasks", "projects", "agents"}
        expected_session_capabilities = {
            "mac_api",
            "mac_cli",
            "mac_hermes_cli",
            "shell_execution",
            "workspace_file_access",
            "beads_issue_tracker",
            "git_source_control",
            "quality_gate",
            "hermes_oneshot_executor",
            "command_audit",
            "web_search",
        }
        session_contract_required = runtime_required or bool(session_capabilities)
        session_availability = (
            runtime.get("session_capability_availability")
            if isinstance(runtime.get("session_capability_availability"), dict)
            else {}
        )

        def matching(commands: Iterable[str], fragments: Iterable[str]) -> List[str]:
            return [
                command
                for command in commands
                if any(fragment in command for fragment in fragments)
            ]

        def has_all(commands: Iterable[str], fragments: Iterable[str]) -> bool:
            command_list = list(commands)
            return all(any(fragment in command for command in command_list) for fragment in fragments)

        runtime_capabilities_ready = (
            expected_session_capabilities <= session_capabilities
            if session_contract_required
            else True
        )
        first_class_objects: JsonDict = {
            "fleets": {
                "authority": authority.get("fleets"),
                "api_operations": sorted(api_operation_names & expected_fleet_api_operations),
                "api_ready": expected_fleet_api_operations <= api_operation_names,
                "dashboard_projection": {
                    "state_key": "fleets",
                    "fields": ["id", "name", "status", "agent_ids"],
                    "urls": dashboard_url_contract["object_deep_links"]["fleets"]["templates"],
                },
                "dashboard_ready": (
                    isinstance(work_context.get("fleets"), list)
                    and dashboard_operation_ready
                    and bool(dashboard_url_contract["object_deep_links"]["fleets"]["ready"])
                ),
                "runtime_capabilities": sorted(
                    session_capabilities
                    & {
                        "mac_api",
                        "shell_execution",
                        "workspace_file_access",
                    }
                ),
                "runtime_ready": runtime_capabilities_ready,
            },
            "tasks": {
                "authority": authority.get("tasks"),
                "api_operations": sorted(api_operation_names & expected_task_api_operations),
                "api_ready": expected_task_api_operations <= api_operation_names,
                "mac_cli_commands": matching(mac_cli_commands, ("mac task ",)),
                "mac_cli_ready": has_all(
                    mac_cli_commands,
                    ("mac task list", "mac task show", "mac task create"),
                ),
                "mac_hermes_cli_commands": matching(
                    mac_hermes_commands,
                    (
                        "mac-hermes tasks",
                        "mac-hermes task ",
                        "mac-hermes task-detail",
                        "mac-hermes claim-next",
                        "mac-hermes claim",
                        "mac-hermes start",
                        "mac-hermes add-child-task",
                        "mac-hermes transition",
                        "mac-hermes command-audit",
                    ),
                ),
                "mac_hermes_cli_ready": has_all(
                    mac_hermes_commands,
                    (
                        "mac-hermes tasks",
                        "mac-hermes task ",
                        "mac-hermes task-detail",
                        "mac-hermes claim-next",
                        "mac-hermes claim",
                        "mac-hermes start",
                        "mac-hermes add-child-task",
                        "mac-hermes transition",
                        "mac-hermes command-audit",
                    ),
                ),
                "dashboard_projection": {
                    "state_key": "hermes_work_contexts",
                    "fields": ["tasks", "relationships.task_dependencies", "operations.task_state_transitions"],
                    "urls": dashboard_url_contract["object_deep_links"]["tasks"]["templates"],
                },
                "dashboard_ready": (
                    isinstance(work_context.get("tasks"), list)
                    and isinstance(relationships.get("task_dependencies"), list)
                    and isinstance(operations.get("task_state_transitions"), dict)
                    and dashboard_operation_ready
                    and bool(dashboard_url_contract["object_deep_links"]["tasks"]["ready"])
                ),
                "runtime_capabilities": sorted(
                    session_capabilities
                    & {
                        "mac_api",
                        "mac_cli",
                        "mac_hermes_cli",
                        "shell_execution",
                        "workspace_file_access",
                        "quality_gate",
                        "hermes_oneshot_executor",
                        "command_audit",
                    }
                ),
                "runtime_ready": runtime_capabilities_ready,
            },
            "projects": {
                "authority": authority.get("projects"),
                "api_operations": sorted(api_operation_names & expected_project_api_operations),
                "api_ready": expected_project_api_operations <= api_operation_names,
                "mac_cli_commands": matching(mac_cli_commands, ("mac project ", "mac bridge ")),
                "mac_cli_ready": has_all(
                    mac_cli_commands,
                    (
                        "mac project list",
                        "mac project show",
                        "mac bridge import",
                        "mac bridge list",
                        "mac bridge repository register",
                    ),
                ),
                "mac_hermes_cli_commands": matching(
                    mac_hermes_commands,
                    (
                        "mac-hermes projects",
                        "mac-hermes project-detail",
                        "mac-hermes import-project-item",
                        "mac-hermes project-items",
                        "mac-hermes project-repositories",
                        "mac-hermes register-project-repository",
                    ),
                ),
                "mac_hermes_cli_ready": has_all(
                    mac_hermes_commands,
                    (
                        "mac-hermes projects",
                        "mac-hermes project-detail",
                        "mac-hermes import-project-item",
                        "mac-hermes project-items",
                        "mac-hermes project-repositories",
                        "mac-hermes register-project-repository",
                    ),
                ),
                "dashboard_projection": {
                    "state_key": "hermes_work_contexts",
                    "fields": ["projects", "projects.bridge_item_count", "projects.repository_count"],
                    "urls": dashboard_url_contract["object_deep_links"]["projects"]["templates"],
                },
                "dashboard_ready": (
                    isinstance(work_context.get("projects"), list)
                    and dashboard_operation_ready
                    and bool(dashboard_url_contract["object_deep_links"]["projects"]["ready"])
                ),
                "runtime_capabilities": sorted(
                    session_capabilities
                    & {
                        "mac_api",
                        "mac_cli",
                        "mac_hermes_cli",
                        "shell_execution",
                        "workspace_file_access",
                        "git_source_control",
                        "beads_issue_tracker",
                        "hermes_oneshot_executor",
                    }
                ),
                "runtime_ready": runtime_capabilities_ready,
            },
            "agents": {
                "authority": authority.get("agents"),
                "api_operations": sorted(api_operation_names & expected_agent_api_operations),
                "api_ready": expected_agent_api_operations <= api_operation_names,
                "mac_cli_commands": matching(mac_cli_commands, ("mac agent ",)),
                "mac_cli_ready": has_all(mac_cli_commands, ("mac agent register", "mac agent list", "mac agent heartbeat")),
                "mac_hermes_cli_commands": matching(mac_hermes_commands, expected_agent_cli_fragments),
                "mac_hermes_cli_ready": has_all(mac_hermes_commands, expected_agent_cli_fragments),
                "dashboard_projection": {
                    "state_key": "hermes_work_contexts",
                    "fields": ["agents", "relationships.agent_assignments", "agents.active_task_ids"],
                    "urls": dashboard_url_contract["object_deep_links"]["agents"]["templates"],
                },
                "dashboard_ready": (
                    isinstance(work_context.get("agents"), list)
                    and isinstance(relationships.get("agent_assignments"), list)
                    and dashboard_operation_ready
                    and bool(dashboard_url_contract["object_deep_links"]["agents"]["ready"])
                ),
                "runtime_capabilities": sorted(
                    session_capabilities
                    & {
                        "mac_api",
                        "mac_cli",
                        "mac_hermes_cli",
                        "shell_execution",
                        "workspace_file_access",
                        "hermes_oneshot_executor",
                        "command_audit",
                    }
                ),
                "runtime_ready": runtime_capabilities_ready,
            },
        }
        for object_proof in first_class_objects.values():
            checks = ["api_ready", "dashboard_ready", "runtime_ready"]
            for optional_check in ("mac_cli_ready", "mac_hermes_cli_ready"):
                if optional_check in object_proof:
                    checks.append(optional_check)
            object_proof["ready"] = all(bool(object_proof.get(check)) for check in checks)

        checks: JsonDict = {
            "api_work_context_schema": work_context.get("schema") == "mac.hermes_work_context.v1",
            "mac_authority_declared": (
                authority.get("tasks") == "mac"
                and authority.get("projects") == "mac"
                and authority.get("agents") == "mac"
                and authority.get("fleets") == "mac"
                and authority.get("personality") == "hermes"
                and authority.get("user_memory") == "hermes"
            ),
            "api_lifecycle_operations_present": expected_api_operations <= api_operation_names,
            "live_object_alignment_consistent": bool(live_alignment.get("ready")),
            "cli_lifecycle_commands_present": all(
                any(fragment in command for command in mac_hermes_commands)
                for fragment in expected_cli_fragments
            )
            and has_all(mac_hermes_commands, expected_agent_cli_fragments),
            "agent_bound_to_hermes_instance": bool(bound_agents),
            "runtime_context_ready": (
                bool(runtime.get("ready"))
                if runtime_required or runtime
                else True
            ),
            "runtime_context_instance_matches": (
                runtime_instance_id in (None, "", hermes_instance_id)
            ),
            "runtime_prompt_bridge_active": (
                bool(prompt_bridge.get("present"))
                if bool(prompt_bridge.get("required")) or runtime_required
                else True
            ),
            "runtime_markdown_contract_present": (
                bool(markdown_contract.get("ready"))
                if runtime_required
                else True
            ),
            "runtime_session_capabilities_declared": runtime_capabilities_ready,
            "runtime_first_class_object_model_declared": (
                expected_first_class_objects <= runtime_first_class_objects
                if session_contract_required
                else True
            ),
            "runtime_session_capabilities_available": (
                bool(session_availability.get("ready"))
                if session_contract_required
                else True
            ),
            "first_class_object_matrix_ready": all(
                bool(item.get("ready")) for item in first_class_objects.values()
            ),
            "dashboard_projection_available": all(
                bool(item.get("dashboard_ready")) for item in first_class_objects.values()
            ),
            "dashboard_url_state_contract_present": bool(dashboard_url_contract.get("ready")),
            "work_context_dashboard_contract_present": bool(dashboard_operation_ready),
        }
        missing = [name for name, ok in checks.items() if not ok]
        return {
            "schema": "mac.hermes_runtime_proof.v1",
            "ready": not missing,
            "hermes_instance": instance,
            "authority": authority,
            "checks": checks,
            "missing": missing,
            "evidence": {
                "api": {
                    "work_context_schema": work_context.get("schema"),
                    "work_context_path": "/hermes-instances/%s/work-context" % hermes_instance_id,
                    "operation_names": sorted(api_operation_names),
                    "task_operation_names": sorted(
                        api_operation_names & expected_task_api_operations
                    ),
                    "project_operation_names": sorted(
                        api_operation_names & expected_project_api_operations
                    ),
                    "agent_operation_names": sorted(
                        api_operation_names & expected_agent_api_operations
                    ),
                    "fleet_operation_names": sorted(
                        api_operation_names & expected_fleet_api_operations
                    ),
                },
                "cli": {
                    "mac_hermes_commands": mac_hermes_commands,
                    "mac_cli_commands": mac_cli_commands,
                },
                "ui": {
                    "dashboard_state_keys": ["hermes_work_contexts", "hermes_runtime_proofs"],
                    "dashboard_state_key": "hermes_runtime_proofs",
                    "dashboard_record_key": hermes_instance_id,
                    "dashboard_operation_contract": dashboard_operation_contract,
                    "dashboard_url_contract": dashboard_url_contract,
                    "first_class_object_projection": {
                        name: proof.get("dashboard_projection")
                        for name, proof in first_class_objects.items()
                    },
                },
                "hermes_runtime": {
                    "status": runtime.get("status"),
                    "required": runtime_required,
                    "ready": runtime.get("ready"),
                    "hermes_instance_id": runtime_instance_id,
                    "context_file": runtime.get("context_file"),
                    "markdown_file": runtime.get("markdown_file"),
                    "markdown_contract": markdown_contract,
                    "prompt_bridge": prompt_bridge,
                    "workspace": runtime.get("workspace"),
                    "first_class_object_names": sorted(runtime_first_class_objects),
                    "first_class_objects": runtime.get("first_class_objects", {}),
                    "session_capability_names": sorted(session_capabilities),
                    "session_capabilities": runtime.get("session_capabilities", []),
                    "session_capability_availability": session_availability,
                },
                "work_context": {
                    "task_count": work_context.get("task_count"),
                    "project_count": len(project_contexts),
                    "project_bridge_item_count": sum(
                        int(project.get("bridge_item_count") or 0)
                        for project in project_contexts
                    ),
                    "project_repository_count": sum(
                        int(project.get("repository_count") or 0)
                        for project in project_contexts
                    ),
                    "agent_count": len(work_context.get("agents", [])),
                    "bound_agent_ids": [agent.get("id") for agent in bound_agents],
                    "relationship_counts": {
                        key: len(value) if isinstance(value, list) else 0
                        for key, value in work_context.get("relationships", {}).items()
                    },
                },
                "live_alignment": live_alignment,
                "first_class_objects": first_class_objects,
            },
        }

    def _hermes_dashboard_url_contract(
        self,
        hermes_instance_id: str,
        *,
        tasks: List[JsonDict],
        projects: List[JsonDict],
        agents: List[JsonDict],
        fleets: List[JsonDict],
    ) -> JsonDict:
        """Bookmarkable dashboard URL contract for Hermes-visible objects."""

        task_id = str(tasks[0].get("id")) if tasks else "{task_id}"
        project = str(projects[0].get("project")) if projects else "{project}"
        agent_id = str(agents[0].get("id")) if agents else "{agent_id}"
        fleet_id = str(fleets[0].get("id")) if fleets else "{fleet_id}"
        contract = {
            "schema": "mac.hermes.dashboard_url_contract.v1",
            "entrypoint": "/ui",
            "required_views": [
                "work",
                "projects",
                "map",
                "fleets",
                "agents",
                "tasks",
                "workflows",
                "hermes",
                "ops",
                "integrations",
                "runtime",
                "observability",
                "secrets",
            ],
            "url_state_parameters": [
                {"name": "view", "purpose": "selected dashboard pane"},
                {"name": "project", "purpose": "project or epic scope"},
                {"name": "task_state", "purpose": "task lane/status filter"},
                {"name": "selected", "purpose": "selected task, agent, or Hermes instance id"},
                {"name": "agent_q", "purpose": "agent search query"},
                {"name": "agent_filter", "purpose": "agent status/health/eligibility filter"},
                {"name": "agent_sort", "purpose": "agent table ordering"},
                {"name": "agent_page", "purpose": "agent table page"},
                {"name": "obs_subject_type", "purpose": "observability subject type filter"},
                {"name": "obs_subject_id", "purpose": "observability subject id filter"},
                {"name": "obs_event_prefix", "purpose": "observability event/name prefix filter"},
                {"name": "obs_actor", "purpose": "audit actor filter"},
                {"name": "obs_layer", "purpose": "observability layer filter"},
                {"name": "obs_level", "purpose": "observability level filter"},
                {"name": "obs_agent", "purpose": "agent scoped audit filter"},
                {"name": "obs_task", "purpose": "task scoped audit filter"},
                {"name": "obs_project", "purpose": "project scoped audit filter"},
                {"name": "obs_fleet", "purpose": "fleet scoped audit filter"},
                {"name": "obs_since", "purpose": "observability lower time bound"},
                {"name": "obs_until", "purpose": "observability upper time bound"},
            ],
            "object_deep_links": {
                "fleets": {
                    "required_params": ["view", "selected"],
                    "required_views": ["fleets", "map"],
                    "templates": [
                        "/ui?view=fleets&selected={fleet_id}",
                        "/ui?view=map&selected={fleet_id}",
                    ],
                    "samples": [
                        self._dashboard_url(view="fleets", selected=fleet_id),
                        self._dashboard_url(view="map", selected=fleet_id),
                    ],
                },
                "tasks": {
                    "required_params": ["view", "selected"],
                    "required_views": ["work", "tasks", "map"],
                    "templates": [
                        "/ui?view=work&selected={task_id}",
                        "/ui?view=tasks&task_state=open&selected={task_id}",
                        "/ui?view=map&selected={task_id}",
                    ],
                    "samples": [
                        self._dashboard_url(view="work", selected=task_id),
                        self._dashboard_url(view="tasks", task_state="open", selected=task_id),
                        self._dashboard_url(view="map", selected=task_id),
                    ],
                },
                "projects": {
                    "required_params": ["view", "project"],
                    "required_views": ["projects", "work", "agents", "map"],
                    "templates": [
                        "/ui?view=projects&project={project}",
                        "/ui?view=work&project={project}",
                        "/ui?view=agents&project={project}",
                        "/ui?view=map&project={project}",
                    ],
                    "samples": [
                        self._dashboard_url(view="projects", project=project),
                        self._dashboard_url(view="work", project=project),
                        self._dashboard_url(view="agents", project=project),
                        self._dashboard_url(view="map", project=project),
                    ],
                },
                "agents": {
                    "required_params": ["view", "selected"],
                    "required_views": ["agents", "work", "map"],
                    "templates": [
                        "/ui?view=agents&selected={agent_id}",
                        "/ui?view=work&selected={agent_id}",
                        "/ui?view=map&selected={agent_id}",
                    ],
                    "samples": [
                        self._dashboard_url(view="agents", selected=agent_id),
                        self._dashboard_url(view="work", selected=agent_id),
                        self._dashboard_url(view="map", selected=agent_id),
                    ],
                },
                "hermes_instances": {
                    "required_params": ["view", "selected"],
                    "required_views": ["hermes", "runtime"],
                    "templates": [
                        "/ui?view=hermes&selected={hermes_instance_id}",
                        "/ui?view=runtime&selected={hermes_instance_id}",
                    ],
                    "samples": [
                        self._dashboard_url(view="hermes", selected=hermes_instance_id),
                        self._dashboard_url(view="runtime", selected=hermes_instance_id),
                    ],
                },
            },
        }
        required_params = {
            str(item.get("name"))
            for item in contract["url_state_parameters"]
            if isinstance(item, dict)
        }
        missing: List[str] = []
        for object_name, links in contract["object_deep_links"].items():
            templates = [str(item) for item in links.get("templates", [])]
            samples = [str(item) for item in links.get("samples", [])]
            params = set(links.get("required_params", []))
            views = set(links.get("required_views", []))
            if not templates:
                missing.append("%s.templates" % object_name)
            if not samples:
                missing.append("%s.samples" % object_name)
            if not params <= required_params:
                missing.append("%s.required_params" % object_name)
            for view in sorted(views):
                if any("view=%s" % view in url for url in templates + samples):
                    continue
                missing.append("%s.view:%s" % (object_name, view))
            links["ready"] = not any(item.startswith("%s." % object_name) for item in missing)
        contract["missing"] = sorted(set(missing))
        contract["ready"] = not contract["missing"]
        return contract

    @staticmethod
    def _dashboard_url(**params: str) -> str:
        filtered = {
            key: value
            for key, value in params.items()
            if value is not None and str(value).strip()
        }
        return "/ui?%s" % urllib.parse.urlencode(filtered)

    def record_hermes_runtime_proof(
        self,
        hermes_instance_id: str,
        proof: JsonDict,
        *,
        actor: str = "hermes",
    ) -> HermesInstance:
        instance = self.get_hermes_instance(hermes_instance_id)
        metadata = ensure_json_object(instance.metadata)
        now = utcnow()
        stored_proof = json_loads(json_dumps(ensure_json_object(proof)), {})
        evidence = ensure_json_object(stored_proof.get("evidence"))
        ui = ensure_json_object(evidence.get("ui"))
        ui["dashboard_source"] = "agent_submitted_runtime_proof"
        ui["submitted_at"] = now
        evidence["ui"] = ui
        stored_proof["evidence"] = evidence
        metadata["latest_runtime_proof"] = {
            "schema": "mac.hermes.submitted_runtime_proof.v1",
            "actor": actor,
            "recorded_at": now,
            "proof": stored_proof,
        }
        self.store.execute(
            """
            UPDATE hermes_instances
            SET metadata = ?, updated_at = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (json_dumps(metadata), now, now, hermes_instance_id),
        )
        return self.get_hermes_instance(hermes_instance_id)

    def _hermes_task_project_key(self, task: Task) -> str:
        project = str(task.project or "").strip()
        if project:
            return project
        for key in ("project", "repository", "repo"):
            value = str(task.metadata.get(key) or "").strip()
            if value:
                return value
        origin = task.metadata.get("origin")
        if isinstance(origin, dict):
            for key in ("project", "repository", "repo", "source"):
                value = str(origin.get(key) or "").strip()
                if value:
                    return value
        return "unassigned"

    def _hermes_task_context(self, task: Task) -> JsonDict:
        origin = task.metadata.get("origin")
        memory_boundary = task.metadata.get("memory_boundary")
        return {
            "id": task.id,
            "title": task.title,
            "project": self._hermes_task_project_key(task),
            "declared_project": task.project,
            "state": task.state,
            "priority": task.priority,
            "owner_agent_id": task.owner_agent_id,
            "required_capabilities": list(task.required_capabilities),
            "dependencies": list(task.dependencies),
            "origin": origin if isinstance(origin, dict) else {},
            "memory_boundary": memory_boundary if isinstance(memory_boundary, dict) else {},
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }

    def _hermes_project_contexts(
        self,
        tasks: List[Task],
        agents: List[Agent],
        project_items: List[JsonDict],
        repositories: List[JsonDict],
        project_records: Optional[List[JsonDict]] = None,
    ) -> List[JsonDict]:
        task_by_id = {task.id: task for task in tasks}
        agent_by_id = {agent.id: agent for agent in agents}
        buckets: Dict[str, JsonDict] = {}

        def bucket(project: str) -> JsonDict:
            if project not in buckets:
                buckets[project] = {
                    "project": project,
                    "task_count": 0,
                    "active_count": 0,
                    "ready_count": 0,
                    "held_count": 0,
                    "blocked_count": 0,
                    "review_count": 0,
                    "completed_count": 0,
                    "state_counts": {},
                    "dependency_edge_count": 0,
                    "cross_project_dependency_count": 0,
                    "active_agent_ids": set(),
                    "active_agent_names": set(),
                    "required_capabilities": set(),
                    "frontier_tasks": [],
                    "waiting_tasks": [],
                    "active_tasks": [],
                    "cross_project_edges": [],
                    "bridge_item_count": 0,
                    "repository_count": 0,
                    "repository_url": "",
                    "description": "",
                    "status": "derived",
                    "metadata": {},
                    "project_id": None,
                }
            return buckets[project]

        for record in project_records or []:
            name = str(record.get("name") or record.get("project") or "").strip()
            if not name:
                continue
            item = bucket(name)
            item["description"] = str(record.get("description") or "")
            item["status"] = str(record.get("status") or "active")
            metadata = record.get("metadata")
            item["metadata"] = metadata if isinstance(metadata, dict) else {}
            item["project_id"] = record.get("id")
            if isinstance(metadata, dict) and metadata.get("repository_url"):
                item["repository_url"] = str(metadata.get("repository_url"))

        for task in tasks:
            project = self._hermes_task_project_key(task)
            item = bucket(project)
            item["task_count"] += 1
            state_counts = item["state_counts"]
            state_counts[task.state] = state_counts.get(task.state, 0) + 1
            item["dependency_edge_count"] += len(task.dependencies)
            for capability in task.required_capabilities:
                item["required_capabilities"].add(str(capability))
            if task.owner_agent_id:
                item["active_agent_ids"].add(task.owner_agent_id)
                agent = agent_by_id.get(task.owner_agent_id)
                if agent is not None:
                    item["active_agent_names"].add(agent.name)
            if task.state not in TERMINAL_TASK_STATES:
                item["active_count"] += 1
            if task.state in {TaskState.NEEDS_REVIEW.value, TaskState.REVIEWING.value}:
                item["review_count"] += 1
            if task.state == TaskState.COMPLETED.value:
                item["completed_count"] += 1
            waiting_on = []
            for dependency_id in task.dependencies:
                dependency = task_by_id.get(dependency_id)
                if dependency is None or dependency.state != TaskState.COMPLETED.value:
                    waiting_on.append(dependency_id)
                if dependency is not None and self._hermes_task_project_key(dependency) != project:
                    item["cross_project_dependency_count"] += 1
                    if len(item["cross_project_edges"]) < 8:
                        item["cross_project_edges"].append(
                            {
                                "from_project": self._hermes_task_project_key(dependency),
                                "from_task_id": dependency.id,
                                "from_task_title": dependency.title,
                                "to_task_id": task.id,
                                "to_task_title": task.title,
                            }
                        )
            compact = self._hermes_task_context(task)
            dispatch_held = self._task_dispatch_held(task)
            project_paused = self._project_dispatch_paused(task.project)
            if task.state == TaskState.OPEN.value and dispatch_held:
                item["held_count"] += 1
            if (
                task.state == TaskState.OPEN.value
                and not waiting_on
                and not dispatch_held
                and not project_paused
            ):
                item["ready_count"] += 1
                if len(item["frontier_tasks"]) < 10:
                    item["frontier_tasks"].append(compact)
            elif task.state == TaskState.BLOCKED.value:
                item["blocked_count"] += 1
                if len(item["waiting_tasks"]) < 10:
                    blocked = dict(compact)
                    if waiting_on:
                        blocked["waiting_on"] = waiting_on[:8]
                    item["waiting_tasks"].append(blocked)
            elif task.state == TaskState.OPEN.value and waiting_on:
                item["blocked_count"] += 1
                if len(item["waiting_tasks"]) < 10:
                    item["waiting_tasks"].append({**compact, "waiting_on": waiting_on[:8]})
            elif task.state in {
                TaskState.CLAIMED.value,
                TaskState.RUNNING.value,
                TaskState.NEEDS_REVIEW.value,
                TaskState.REVIEWING.value,
            }:
                if len(item["active_tasks"]) < 10:
                    item["active_tasks"].append(compact)

        for bridge_item in project_items:
            bucket(str(bridge_item.get("project") or bridge_item.get("source") or "unassigned"))[
                "bridge_item_count"
            ] += 1
        for repository in repositories:
            bucket(str(repository.get("project") or repository.get("name") or repository.get("source") or "unassigned"))[
                "repository_count"
            ] += 1

        normalized = []
        for item in buckets.values():
            normalized.append(
                {
                    **item,
                    "active_agent_ids": sorted(item["active_agent_ids"]),
                    "active_agent_names": sorted(item["active_agent_names"]),
                    "required_capabilities": sorted(item["required_capabilities"]),
                }
            )
        return sorted(
            normalized,
            key=lambda item: (
                -int(item["ready_count"]),
                -int(item["active_count"]),
                str(item["project"]),
            ),
        )

    def _hermes_agent_context(self, agent: Agent, tasks: List[Task]) -> JsonDict:
        active_tasks = [
            task
            for task in tasks
            if task.owner_agent_id == agent.id and task.state not in TERMINAL_TASK_STATES
        ]
        return {
            "id": agent.id,
            "name": agent.name,
            "status": agent.status,
            "health_status": agent.health_status,
            "capabilities": list(agent.capabilities),
            "resources": dict(agent.resources),
            "role_id": agent.role_id,
            "hermes_instance_id": agent.hermes_instance_id,
            "current_task_id": agent.current_task_id,
            "capacity": self._agent_capacity(agent),
            "active_lease_count": self._agent_active_lease_count(agent.id),
            "active_task_ids": [task.id for task in active_tasks],
            "active_projects": sorted(
                {self._hermes_task_project_key(task) for task in active_tasks}
            ),
        }

    def _hermes_work_relationships(self, tasks: List[Task], agents: List[Agent]) -> JsonDict:
        task_by_id = {task.id: task for task in tasks}
        agent_ids = {agent.id for agent in agents}
        dependency_edges = []
        assignment_edges = []
        hermes_origins = []
        for task in tasks:
            task_project = self._hermes_task_project_key(task)
            for dependency_id in task.dependencies:
                dependency = task_by_id.get(dependency_id)
                dependency_edges.append(
                    {
                        "task_id": task.id,
                        "task_project": task_project,
                        "depends_on_task_id": dependency_id,
                        "depends_on_project": (
                            self._hermes_task_project_key(dependency)
                            if dependency is not None
                            else None
                        ),
                        "depends_on_state": dependency.state if dependency is not None else None,
                        "cross_project": (
                            dependency is not None
                            and self._hermes_task_project_key(dependency) != task_project
                        ),
                    }
                )
            if task.owner_agent_id:
                assignment_edges.append(
                    {
                        "agent_id": task.owner_agent_id,
                        "task_id": task.id,
                        "project": task_project,
                        "state": task.state,
                        "agent_registered": task.owner_agent_id in agent_ids,
                    }
                )
            origin = task.metadata.get("origin")
            if isinstance(origin, dict) and origin.get("hermes_instance_id"):
                hermes_origins.append(
                    {
                        "hermes_instance_id": origin.get("hermes_instance_id"),
                        "task_id": task.id,
                        "project": task_project,
                        "origin_type": origin.get("type"),
                        "conversation_ref": origin.get("conversation_ref"),
                    }
                )
        return {
            "task_dependencies": dependency_edges,
            "agent_assignments": assignment_edges,
            "hermes_task_origins": hermes_origins,
        }

    def _hermes_operation_contract(self, hermes_instance_id: str) -> JsonDict:
        return {
            "api": [
                {
                    "name": "get_work_context",
                    "method": "GET",
                    "path": "/hermes-instances/%s/work-context" % hermes_instance_id,
                },
                {
                    "name": "get_runtime_proof",
                    "method": "GET",
                    "path": "/hermes-instances/%s/runtime-proof" % hermes_instance_id,
                },
                {
                    "name": "create_task_from_conversation",
                    "method": "POST",
                    "path": "/hermes-instances/%s/tasks" % hermes_instance_id,
                },
                {"name": "list_tasks", "method": "GET", "path": "/tasks"},
                {"name": "get_task", "method": "GET", "path": "/tasks/{task_id}"},
                {"name": "update_task", "method": "PUT", "path": "/tasks/{task_id}"},
                {"name": "delete_task", "method": "DELETE", "path": "/tasks/{task_id}"},
                {
                    "name": "add_child_tasks",
                    "method": "POST",
                    "path": "/tasks/{task_id}/children",
                },
                {
                    "name": "get_task_summary",
                    "method": "GET",
                    "path": "/tasks/{task_id}/summary",
                },
                {
                    "name": "claim_next_task",
                    "method": "POST",
                    "path": "/agents/{agent_id}/claim-next",
                },
                {
                    "name": "claim_task",
                    "method": "POST",
                    "path": "/tasks/{task_id}/claim?agent_id={agent_id}",
                },
                {
                    "name": "start_task",
                    "method": "POST",
                    "path": "/tasks/{task_id}/start?agent_id={agent_id}",
                },
                {
                    "name": "transition_task",
                    "method": "POST",
                    "path": "/tasks/{task_id}/transition",
                },
                {
                    "name": "add_evidence",
                    "method": "POST",
                    "path": "/tasks/{task_id}/evidence",
                },
                {
                    "name": "submit_for_review",
                    "method": "POST",
                    "path": "/tasks/{task_id}/submit-for-review?agent_id={agent_id}",
                },
                {
                    "name": "request_review",
                    "method": "POST",
                    "path": "/tasks/{task_id}/reviews",
                },
                {
                    "name": "claim_review",
                    "method": "POST",
                    "path": "/reviews/{review_id}/claim",
                },
                {
                    "name": "submit_review",
                    "method": "POST",
                    "path": "/reviews/{review_id}/decision",
                },
                {
                    "name": "publish_task",
                    "method": "POST",
                    "path": "/publications",
                },
                {
                    "name": "record_command_audit",
                    "method": "POST",
                    "path": "/agents/{agent_id}/command-audit",
                },
                {
                    "name": "list_command_audit",
                    "method": "GET",
                    "path": "/command-audit",
                },
                {
                    "name": "write_completed_task_to_memory",
                    "method": "POST",
                    "path": "/memory",
                },
                {
                    "name": "create_project",
                    "method": "POST",
                    "path": "/projects",
                },
                {
                    "name": "list_projects",
                    "method": "GET",
                    "path": "/projects",
                },
                {
                    "name": "get_project",
                    "method": "GET",
                    "path": "/projects/{project}",
                },
                {
                    "name": "update_project",
                    "method": "PUT",
                    "path": "/projects/{project}",
                },
                {
                    "name": "delete_project",
                    "method": "DELETE",
                    "path": "/projects/{project}",
                },
                {
                    "name": "import_project_item",
                    "method": "POST",
                    "path": "/bridge/items",
                },
                {
                    "name": "list_project_items",
                    "method": "GET",
                    "path": "/bridge/items",
                },
                {
                    "name": "register_project_repository",
                    "method": "POST",
                    "path": "/bridge/repositories",
                },
                {
                    "name": "list_project_repositories",
                    "method": "GET",
                    "path": "/bridge/repositories",
                },
                {
                    "name": "create_fleet",
                    "method": "POST",
                    "path": "/fleets",
                },
                {
                    "name": "list_fleets",
                    "method": "GET",
                    "path": "/fleets",
                },
                {
                    "name": "get_fleet",
                    "method": "GET",
                    "path": "/fleets/{fleet_id_or_name}",
                },
                {
                    "name": "update_fleet",
                    "method": "PUT",
                    "path": "/fleets/{fleet_id_or_name}",
                },
                {
                    "name": "delete_fleet",
                    "method": "DELETE",
                    "path": "/fleets/{fleet_id_or_name}",
                },
                {
                    "name": "create_agent",
                    "method": "POST",
                    "path": "/agents",
                },
                {
                    "name": "list_agents",
                    "method": "GET",
                    "path": "/agents",
                },
                {
                    "name": "get_agent",
                    "method": "GET",
                    "path": "/agents/{agent_id}",
                },
                {
                    "name": "update_agent",
                    "method": "PUT",
                    "path": "/agents/{agent_id}",
                },
                {
                    "name": "disable_agent",
                    "method": "POST",
                    "path": "/agents/{agent_id}/disable",
                },
                {
                    "name": "delete_agent",
                    "method": "DELETE",
                    "path": "/agents/{agent_id}",
                },
                {
                    "name": "get_agent_identity",
                    "method": "GET",
                    "path": "/agents/{agent_id}/identity",
                },
                {
                    "name": "track_conversation_thread",
                    "method": "POST",
                    "path": "/conversation-threads",
                },
            ],
            "mac_cli": [
                "mac hermes work-context %s" % hermes_instance_id,
                "mac hermes runtime-proof %s" % hermes_instance_id,
                "mac project create <name> --description <description>",
                "mac project list",
                "mac project show <project>",
                "mac bridge import <source> <external_id> <title> --project <project>",
                "mac bridge list",
                "mac bridge repository register <name> <path> --project <project>",
                "mac bridge repository repos",
                "mac task list",
                "mac task show {task_id}",
                "mac task create --title ...",
                "mac agent register <machine_id> <name>",
                "mac agent list",
                "mac agent heartbeat {agent_id}",
            ],
            "mac_hermes_cli": [
                "mac-hermes work-context %s" % hermes_instance_id,
                "mac-hermes runtime-proof %s" % hermes_instance_id,
                "mac-hermes create-project <name> --description <description>",
                "mac-hermes projects",
                "mac-hermes project-detail <project>",
                "mac-hermes import-project-item <source> <external_id> <title> --project <project>",
                "mac-hermes project-items",
                "mac-hermes project-repositories",
                "mac-hermes register-project-repository <name> <path> --project <project>",
                "mac-hermes agents",
                "mac-hermes agent-detail {agent_id}",
                "mac-hermes agent-identity {agent_id}",
                "mac-hermes claim-next {agent_id} --dry-run",
                "mac-hermes tasks --state open",
                "mac-hermes task %s <title> --summary ..." % hermes_instance_id,
                "mac-hermes task-detail {task_id}",
                "mac-hermes summary {task_id}",
                "mac-hermes claim {task_id} {agent_id}",
                "mac-hermes start {task_id} {agent_id}",
                "mac-hermes add-child-task {task_id} <title>",
                "mac-hermes transition {task_id} {target_state} --actor {actor}",
                "mac-hermes evidence {task_id} --kind test --uri artifact://... --summary ... --created-by {agent_id}",
                "mac-hermes submit-review {task_id} {agent_id}",
                "mac-hermes request-review {task_id} {reviewer_agent_id}",
                "mac-hermes claim-review {review_id} {reviewer_agent_id}",
                "mac-hermes review-decision {review_id} approved {reviewer_agent_id} --evidence-id {evidence_id}",
                "mac-hermes publish {task_id} {target} {created_by}",
                "mac-hermes command-audit record {agent_id} --phase started --argv-json '[\"git\",\"status\"]' --cwd /workspace",
                "mac-hermes command-audit list --agent-id {agent_id}",
                "mac-hermes web-search \"current release notes\" --limit 5",
                "mac-hermes web-scrape https://example.com --format markdown",
                "mac-hermes web-crawl https://example.com --limit 1",
                "mac-hermes web-crawl-status {crawl_id}",
                "mac-hermes writeback %s {task_id}" % hermes_instance_id,
            ],
            "dashboard": {
                "schema": "mac.hermes.dashboard_operation_contract.v1",
                "entrypoint": "/ui",
                "views": [
                    "work",
                    "projects",
                    "map",
                    "fleets",
                    "agents",
                    "tasks",
                    "workflows",
                    "hermes",
                    "ops",
                    "integrations",
                    "runtime",
                    "observability",
                    "secrets",
                ],
                "url_state_parameters": [
                    "view",
                    "project",
                    "task_state",
                    "selected",
                    "agent_q",
                    "agent_filter",
                    "agent_sort",
                    "agent_page",
                    "obs_subject_type",
                    "obs_subject_id",
                    "obs_event_prefix",
                    "obs_actor",
                    "obs_layer",
                    "obs_level",
                    "obs_agent",
                    "obs_task",
                    "obs_project",
                    "obs_fleet",
                    "obs_since",
                    "obs_until",
                ],
                "deep_link_templates": {
                    "fleets": [
                        "/ui?view=fleets&selected={fleet_id}",
                        "/ui?view=map&selected={fleet_id}",
                    ],
                    "tasks": [
                        "/ui?view=work&selected={task_id}",
                        "/ui?view=tasks&task_state=open&selected={task_id}",
                        "/ui?view=map&selected={task_id}",
                    ],
                    "projects": [
                        "/ui?view=projects&project={project}",
                        "/ui?view=work&project={project}",
                        "/ui?view=agents&project={project}",
                        "/ui?view=map&project={project}",
                    ],
                    "agents": [
                        "/ui?view=agents&selected={agent_id}",
                        "/ui?view=work&selected={agent_id}",
                        "/ui?view=map&selected={agent_id}",
                    ],
                    "hermes_instances": [
                        "/ui?view=hermes&selected=%s" % hermes_instance_id,
                        "/ui?view=runtime&selected=%s" % hermes_instance_id,
                    ],
                },
            },
            "task_state_transitions": {
                state: sorted(targets)
                for state, targets in TASK_TRANSITIONS.items()
            },
        }

    # Agent roles: thin facade over ``self.roles``.

    def create_role(self, *args: Any, **kwargs: Any) -> AgentRole:
        return self.roles.create_role(*args, **kwargs)

    def get_role(self, *args: Any, **kwargs: Any) -> AgentRole:
        return self.roles.get_role(*args, **kwargs)

    def list_roles(self, *args: Any, **kwargs: Any) -> List[AgentRole]:
        return self.roles.list_roles(*args, **kwargs)

    def update_role(self, *args: Any, **kwargs: Any) -> AgentRole:
        return self.roles.update_role(*args, **kwargs)

    def delete_role(self, *args: Any, **kwargs: Any) -> None:
        return self.roles.delete_role(*args, **kwargs)

    def assign_role(self, agent_id: str, role_id_or_slug: str) -> Agent:
        return self.roles.assign_role(agent_id, role_id_or_slug)

    def unassign_role(self, agent_id: str) -> Agent:
        return self.roles.unassign_role(agent_id)

    def list_provisioning_requests(
        self, *args: Any, **kwargs: Any
    ) -> List[AgentProvisioningRequest]:
        return self.provisioning.list_requests(*args, **kwargs)

    def get_provisioning_request(self, request_id: str) -> AgentProvisioningRequest:
        return self.provisioning.get_request(request_id)

    def fulfill_provisioning_request(
        self, request_id: str, agent_id: str
    ) -> AgentProvisioningRequest:
        return self.provisioning.fulfill_request(request_id, agent_id)

    def cancel_provisioning_request(
        self, request_id: str, *, reason: str = "operator-cancelled"
    ) -> AgentProvisioningRequest:
        return self.provisioning.cancel_request(request_id, reason=reason)

    def agent_identity(self, agent_id: str) -> JsonDict:
        """Layered identity for an agent: soul → role → mood → hardware.

        The layers are returned separately rather than fused into a
        single prompt string — callers (worker, Hermes) own the
        composition. Soul is authoritative for personality; role is the
        operational hat; mood is the agent's transient self-report;
        hardware is the machine the agent runs on.
        """
        agent = self.get_agent(agent_id)
        machine = self.get_machine(agent.machine_id)
        soul: Optional[JsonDict] = None
        role_slugs: Optional[List[str]] = self.roles._allowed_role_slugs_for(agent)
        if agent.hermes_instance_id:
            try:
                instance = self.identity.get_hermes_instance(agent.hermes_instance_id)
                persona = (
                    self.identity.get_persona(instance.persona_id)
                    if instance.persona_id
                    else None
                )
                soul = {
                    "hermes_instance": instance.to_dict(),
                    "persona": persona.to_dict() if persona else None,
                }
            except NotFoundError:
                soul = None
        role: Optional[JsonDict] = None
        if agent.role_id:
            try:
                role = self.roles.get_role(agent.role_id).to_dict()
            except NotFoundError:
                role = None
        mood_overlay = self.agent_state.get_current_mood(agent.id)
        return {
            "agent": agent.to_dict(),
            "soul": soul,
            "allowed_role_slugs": role_slugs,
            "role": role,
            "mood": mood_overlay.to_dict() if mood_overlay is not None else None,
            "machine_hardware": machine.hardware,
        }

    # Workflows: thin facade over ``self.workflows``.

    def create_workflow(self, *args: Any, **kwargs: Any) -> Workflow:
        return self.workflows.create_workflow(*args, **kwargs)

    def get_workflow(self, *args: Any, **kwargs: Any) -> Workflow:
        return self.workflows.get_workflow(*args, **kwargs)

    def list_workflows(self, *args: Any, **kwargs: Any) -> List[Workflow]:
        return self.workflows.list_workflows(*args, **kwargs)

    def update_workflow(self, *args: Any, **kwargs: Any) -> Workflow:
        return self.workflows.update_workflow(*args, **kwargs)

    def delete_workflow(self, workflow_id: str) -> None:
        return self.workflows.delete_workflow(workflow_id)

    def import_workflow_yaml(self, *args: Any, **kwargs: Any) -> Workflow:
        return self.workflows.import_yaml(*args, **kwargs)

    def create_workflow_draft(self, *args: Any, **kwargs: Any) -> WorkflowDraft:
        return self.workflows.create_draft(*args, **kwargs)

    def update_workflow_draft(self, *args: Any, **kwargs: Any) -> WorkflowDraft:
        return self.workflows.update_draft(*args, **kwargs)

    def get_workflow_draft(self, draft_id: str) -> WorkflowDraft:
        return self.workflows.get_draft(draft_id)

    def list_workflow_drafts(self, *args: Any, **kwargs: Any) -> List[WorkflowDraft]:
        return self.workflows.list_drafts(*args, **kwargs)

    def preview_workflow(self, *args: Any, **kwargs: Any) -> JsonDict:
        return self.workflows.preview_workflow(*args, **kwargs)

    def preview_workflow_definition(self, *args: Any, **kwargs: Any) -> JsonDict:
        return self.workflows.preview_definition(*args, **kwargs)

    def preview_workflow_draft(self, *args: Any, **kwargs: Any) -> JsonDict:
        return self.workflows.preview_draft(*args, **kwargs)

    def approve_workflow_draft(self, *args: Any, **kwargs: Any) -> Workflow:
        return self.workflows.approve_draft(*args, **kwargs)

    def start_workflow(self, *args: Any, **kwargs: Any) -> WorkflowRun:
        return self.workflow_runtime.start_run(*args, **kwargs)

    def get_workflow_run(self, run_id: str) -> WorkflowRun:
        return self.workflow_runtime.get_run(run_id)

    def list_workflow_runs(self, *args: Any, **kwargs: Any) -> List[WorkflowRun]:
        return self.workflow_runtime.list_runs(*args, **kwargs)

    def workflow_decisions(
        self,
        workflow_id_or_slug: str,
        *,
        tenant_id: Optional[str] = None,
    ) -> JsonDict:
        """Enumerate every human-decision gate in a workflow definition."""
        return self.workflows.decisions_for_workflow(
            workflow_id_or_slug, tenant_id=tenant_id
        )

    def workflow_run_decisions(self, run_id: str) -> JsonDict:
        """Enumerate human-decision gates for a live workflow run."""
        run = self.workflow_runtime.get_run(run_id)
        return self.workflows.decisions_for_run(run)

    def cancel_workflow_run(self, *args: Any, **kwargs: Any) -> WorkflowRun:
        return self.workflow_runtime.cancel_run(*args, **kwargs)

    def tick_workflow_runs(self, *args: Any, **kwargs: Any) -> List[WorkflowRun]:
        return self.workflow_runtime.tick(*args, **kwargs)

    def workflow_runs_summary(self) -> JsonDict:
        """Counts grouped by state plus the 20 most recent runs for the
        dashboard. Designed to be inlined into /dashboard/state without
        adding a separate read query path."""
        rows = self.store.query_all(
            "SELECT state, COUNT(*) AS count FROM workflow_runs GROUP BY state"
        )
        by_state = {row["state"]: int(row["count"]) for row in rows}
        latest = [
            run.to_dict()
            for run in self.workflow_runtime.list_runs(limit=20)
        ]
        return {
            "counts": by_state,
            "total": sum(by_state.values()),
            "latest": latest,
        }

    def create_interaction_task(
        self,
        hermes_instance_id: str,
        title: str,
        user_id: Optional[str] = None,
        platform_binding_id: Optional[str] = None,
        conversation_ref: Optional[str] = None,
        description: str = "",
        project: Optional[str] = None,
        priority: int = 0,
        required_capabilities: Optional[Iterable[str]] = None,
        dependencies: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        max_attempts: int = 3,
        actor: str = "hermes",
    ) -> Task:
        instance = self.get_hermes_instance(hermes_instance_id)
        if user_id:
            user = self.get_user(user_id)
            if user.tenant_id != instance.tenant_id:
                raise ValidationError("interaction user must belong to hermes instance tenant")
        if platform_binding_id:
            binding = self.get_platform_binding(platform_binding_id)
            if binding.tenant_id != instance.tenant_id or binding.hermes_instance_id != instance.id:
                raise ValidationError("platform binding must belong to hermes instance")
        task_metadata = ensure_json_object(metadata)
        task_metadata.setdefault(
            "origin",
            {
                "type": "hermes_interaction",
                "tenant_id": instance.tenant_id,
                "user_id": user_id,
                "hermes_instance_id": instance.id,
                "persona_id": instance.persona_id,
                "platform_binding_id": platform_binding_id,
                "conversation_ref": conversation_ref,
            },
        )
        task_metadata.setdefault(
            "memory_boundary",
            {
                "hermes_is_authoritative_for_personality": True,
                "hermes_is_authoritative_for_user_memory": True,
                "mac_records_operational_provenance_only": True,
            },
        )
        return self.create_task(
            title,
            description=description,
            project=project,
            priority=priority,
            required_capabilities=required_capabilities,
            dependencies=dependencies,
            metadata=task_metadata,
            max_attempts=max_attempts,
            actor=actor,
        )

    # Task ledger

    def _apply_project_task_defaults(
        self,
        project: Optional[str],
        required_capabilities: List[str],
        metadata: Dict[str, Any],
    ) -> Tuple[List[str], JsonDict]:
        normalized = ensure_json_object(metadata)
        caps = list(required_capabilities)
        if not project:
            return caps, normalized
        try:
            record = self.get_project_record(project)
        except NotFoundError:
            return caps, normalized
        project_meta = ensure_json_object(record.metadata)
        defaults = project_meta.get("task_defaults")
        if not isinstance(defaults, dict):
            return caps, normalized

        role = str(defaults.get("role") or "").strip()
        if role and not str(normalized.get("required_role") or "").strip():
            try:
                self.roles.get_role(role)
            except NotFoundError as exc:
                raise ValidationError(
                    "unknown project default role for %s: %s" % (project, role)
                ) from exc
            normalized["required_role"] = role

        default_caps = defaults.get("required_capabilities")
        if not caps and isinstance(default_caps, list):
            caps = [str(item).strip() for item in default_caps if str(item).strip()]

        # Capability policy (untrusted LLM input guard): when a project pins
        # an allow-list of hard runtime capabilities, any requested capability
        # outside it (e.g. domain/language labels like "typescript",
        # "frontend", "design" hallucinated by Hermes) is stripped from the
        # scheduler's hard requirements and preserved as domain context so the
        # task stays claimable while keeping the LLM's classification intent.
        allowed_caps = defaults.get("allowed_capabilities")
        if caps and isinstance(allowed_caps, list):
            allowed = [str(item).strip() for item in allowed_caps if str(item).strip()]
            allowed_set = set(allowed)
            accepted: List[str] = []
            filtered: List[str] = []
            for cap in caps:
                (accepted if cap in allowed_set else filtered).append(cap)
            if filtered:
                existing_domain = normalized.get("domain_capabilities")
                domain = list(existing_domain) if isinstance(existing_domain, list) else []
                for cap in filtered:
                    if cap not in domain:
                        domain.append(cap)
                normalized["domain_capabilities"] = domain
                normalized["capability_policy"] = {
                    "source": "project.task_defaults.allowed_capabilities",
                    "allowed": allowed,
                    "accepted": accepted,
                    "filtered": filtered,
                }
            caps = accepted
        return caps, normalized

    def _decouple_repository_commands_from_capabilities(
        self,
        required_capabilities: List[str],
        metadata: Dict[str, Any],
    ) -> Tuple[List[str], JsonDict]:
        normalized = ensure_json_object(metadata)
        required_commands = _repository_required_commands_from_metadata(normalized)
        if not required_commands:
            return list(required_capabilities), normalized

        command_set = set(required_commands)
        kept: List[str] = []
        filtered: List[str] = []
        for capability in required_capabilities:
            cap = str(capability).strip()
            if not cap:
                continue
            if cap in command_set:
                filtered.append(cap)
            else:
                kept.append(cap)

        toolchain = ensure_json_object(normalized.get("toolchain_requirements"))
        toolchain.update(
            {
                "schema": "mac.task_toolchain_requirements.v1",
                "source": "repository_contract.toolchain.required_commands",
                "required_commands": required_commands,
            }
        )
        if filtered:
            toolchain["filtered_from_required_capabilities"] = filtered
        normalized["toolchain_requirements"] = toolchain
        return kept, normalized

    def create_task(
        self,
        title: str,
        description: str = "",
        project: Optional[str] = None,
        priority: int = 0,
        required_capabilities: Optional[Iterable[str]] = None,
        dependencies: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        max_attempts: int = 3,
        actor: str = "human",
        _task_id: Optional[str] = None,
        _workflow_run_id: Optional[str] = None,
        _workflow_node_key: Optional[str] = None,
    ) -> Task:
        title = title.strip()
        if not title:
            raise ValidationError("task title is required")
        dep_ids = coerce_list(dependencies)
        for dep_id in dep_ids:
            self.get_task(dep_id)
        now = utcnow()
        task_id = str(_task_id or new_id("task")).strip()
        if not task_id:
            raise ValidationError("task id is required")
        if bool(_workflow_run_id) != bool(_workflow_node_key):
            raise ValidationError(
                "workflow-linked task creation requires both run id and node key"
            )
        state = TaskState.BLOCKED.value if dep_ids else TaskState.OPEN.value
        task_capabilities, task_metadata = self._apply_project_task_defaults(
            project,
            coerce_list(required_capabilities),
            ensure_json_object(metadata),
        )
        normalized_metadata = self._normalize_task_execution_contract(
            task_metadata,
            project,
            task_capabilities,
        )
        task_capabilities, normalized_metadata = self._decouple_repository_commands_from_capabilities(
            task_capabilities,
            normalized_metadata,
        )
        created = False
        with self.store.transaction() as conn:
            existing = conn.execute(
                "SELECT workflow_run_id, workflow_node_key FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if existing is not None:
                if not _task_id:
                    raise ValidationError("task already exists: %s" % task_id)
                if (
                    str(existing["workflow_run_id"] or "")
                    != str(_workflow_run_id or "")
                    or str(existing["workflow_node_key"] or "")
                    != str(_workflow_node_key or "")
                ):
                    raise ValidationError(
                        "idempotent workflow task id %s belongs to a different run or node"
                        % task_id
                    )
            else:
                inserted = conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, description, project, priority, state,
                        required_capabilities, dependencies, metadata,
                        owner_agent_id, lease_id, leased_until, attempt_count,
                        max_attempts, started_at, completed_at, created_at, updated_at,
                        workflow_run_id, workflow_node_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?, NULL, NULL, ?, ?, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (
                        task_id,
                        title,
                        description,
                        project,
                        int(priority),
                        state,
                        json_dumps(task_capabilities),
                        json_dumps(dep_ids),
                        json_dumps(normalized_metadata),
                        int(max_attempts),
                        now,
                        now,
                        _workflow_run_id,
                        _workflow_node_key,
                    ),
                )
                if inserted.rowcount == 0:
                    existing = conn.execute(
                        "SELECT workflow_run_id, workflow_node_key FROM tasks WHERE id = ?",
                        (task_id,),
                    ).fetchone()
                    if not _task_id or existing is None:
                        raise ValidationError("task already exists: %s" % task_id)
                    if (
                        str(existing["workflow_run_id"] or "")
                        != str(_workflow_run_id or "")
                        or str(existing["workflow_node_key"] or "")
                        != str(_workflow_node_key or "")
                    ):
                        raise ValidationError(
                            "idempotent workflow task id %s belongs to a different run or node"
                            % task_id
                        )
                else:
                    self._record_history(
                        task_id,
                        "task.created",
                        actor,
                        None,
                        state,
                        {
                            "title": title,
                            "required_capabilities": task_capabilities,
                            "dependencies": dep_ids,
                            "execution_contract_type": (
                                normalized_metadata.get("execution_contract", {}).get("type")
                                if isinstance(normalized_metadata.get("execution_contract"), dict)
                                else None
                            ),
                        },
                        conn=conn,
                    )
                    created = True
        if (
            created
            and isinstance(normalized_metadata.get("execution_contract"), dict)
            and normalized_metadata["execution_contract"].get("quality") == "weak"
        ):
            self.record_log(
                "task.execution_contract.weak",
                layer="control_plane",
                source=actor,
                level="warning",
                subject_type="task",
                subject_id=task_id,
                detail={
                    "project": project,
                    "required_capabilities": task_capabilities,
                    "reason": normalized_metadata["execution_contract"].get("reason"),
                },
            )
        return self.get_task(task_id)

    def onboard_repository(
        self,
        repository_url: str,
        *,
        project: Optional[str] = None,
        default_branch: Optional[str] = None,
        title: Optional[str] = None,
        priority: int = 0,
        required_capabilities: Optional[Iterable[str]] = None,
        actor: str = "human",
    ) -> Task:
        """Onboard a git repository as a contract-backed project.

        Creates one read-only *onboarding* task whose ``metadata.origin`` carries
        the remote URL + ``type=direct_task`` — enough for a worker to clone a
        task-owned worktree (see ``worker._repository_task_origin``) *without* a
        pre-existing ``repository_contract``. The agent then analyses the
        checkout and authors ``.mac/project.yaml`` (the contract), after which
        every later task on the project is fully contract-backed
        (``_normalize_task_execution_contract`` attaches it automatically).

        This is the missing "take a git URL and onboard it" entry point: the
        contract is the onboarding task's *output*, not a precondition.
        """
        url = _normalize_onboarding_remote_url(repository_url)
        repo_name = _repository_name_from_url(url)
        project = (project or repo_name).strip() or repo_name
        origin: JsonDict = {
            "type": "direct_task",
            "repository_url": url,
            "repository_name": repo_name,
            "onboarding": True,
        }
        if default_branch:
            origin["default_branch"] = str(default_branch).strip()
        metadata: JsonDict = {
            "origin": origin,
            # Drives the weak execution-contract's evidence_type so the
            # verification gate expects an investigation write-up, not a push.
            "evidence_type": "investigation",
        }
        # Register a first-class project record so onboard and create converge:
        # the repo URL becomes durable project state (surfaced by `project
        # list`/`show`), not just task metadata. Idempotent by project name.
        self._ensure_onboarded_project_record(
            project, url, default_branch=default_branch, actor=actor
        )
        resolved_title = (
            title or "Onboard %s: analyze, summarize, and author the repository contract" % repo_name
        ).strip()
        return self.create_task(
            resolved_title,
            description=_build_onboarding_description(url, repo_name),
            project=project,
            priority=priority,
            required_capabilities=required_capabilities,
            metadata=metadata,
            actor=actor,
        )

    def _ensure_onboarded_project_record(
        self,
        project: str,
        repository_url: str,
        *,
        default_branch: Optional[str] = None,
        actor: str = "human",
    ) -> ProjectRecord:
        """Create or augment the ``projects`` record for an onboarded repo.

        New records carry ``repository_url`` (+ optional ``default_branch``) in
        metadata and are left ACTIVE on purpose: onboarding creates a single
        read-only analysis task that must dispatch so the repo gets analyzed.
        (``create``'s default-paused staging is the different case of seeding a
        backlog of work tickets.) For a pre-existing record we only *fill in* a
        missing repository_url/default_branch and never touch its dispatch
        state, so re-onboarding is idempotent and operator intent is preserved.
        """
        branch = str(default_branch).strip() if default_branch else None
        try:
            existing = self.get_project_record(project)
        except NotFoundError:
            existing = None
        if existing is None:
            metadata: JsonDict = {"repository_url": repository_url, "onboarding": True}
            if branch:
                metadata["default_branch"] = branch
            return self.create_project(
                project,
                metadata=metadata,
                actor=actor,
                dispatch_paused=False,
            )
        md = ensure_json_object(existing.metadata)
        changed = False
        if not md.get("repository_url"):
            md["repository_url"] = repository_url
            changed = True
        if branch and not md.get("default_branch"):
            md["default_branch"] = branch
            changed = True
        if not changed:
            return existing
        return self.update_project(existing.id, metadata=md, actor=actor)

    def _normalize_task_execution_contract(
        self,
        metadata: Dict[str, Any],
        project: Optional[str],
        required_capabilities: List[str],
    ) -> JsonDict:
        normalized = ensure_json_object(metadata)
        origin = normalized.get("origin")
        origin_dict = dict(origin) if isinstance(origin, dict) else {}
        existing_contract = normalized.get("execution_contract")
        if isinstance(existing_contract, dict) and existing_contract.get("type"):
            contract_type = str(existing_contract.get("type") or "").strip().lower()
            repo_like = (
                contract_type == "repository"
                or existing_contract.get("repository_required") is True
                or isinstance(existing_contract.get("repository_contract"), dict)
            )
            if repo_like:
                merged_contract = dict(existing_contract)
                merged_contract.setdefault("schema", "mac.task_execution_contract.v1")
                merged_contract.setdefault("type", "repository")
                merged_contract.setdefault("evidence_type", "repo_change")
                repository_contract = merged_contract.get("repository_contract")
                if not isinstance(repository_contract, dict) or not repository_contract.get("schema"):
                    origin_contract = origin_dict.get("repository_contract")
                    if isinstance(origin_contract, dict) and origin_contract.get("schema"):
                        merged_contract["repository_contract"] = origin_contract
                    else:
                        repo = self._repository_for_project(project)
                        if repo is not None:
                            contract = repo.metadata.get("repository_contract")
                            if not isinstance(contract, dict) or not contract.get("schema"):
                                contract = self._repository_contract_for_repo(repo)
                            origin_dict.setdefault("type", "direct_task")
                            origin_dict.setdefault("repository_id", repo.id)
                            origin_dict.setdefault("repository_name", repo.name)
                            origin_dict.setdefault("repository_path", repo.path)
                            origin_dict.setdefault("source", repo.source)
                            origin_dict["repository_contract"] = contract
                            normalized["origin"] = origin_dict
                            acc_metadata = (
                                dict(normalized.get("acc_metadata"))
                                if isinstance(normalized.get("acc_metadata"), dict)
                                else {}
                            )
                            acc_metadata.setdefault("workflow_role", "work")
                            acc_metadata.setdefault("repository_contract_schema", contract["schema"])
                            acc_metadata.setdefault("repository_contract_project", contract["project"])
                            normalized["acc_metadata"] = acc_metadata
                            merged_contract.setdefault("quality", "strong")
                            merged_contract.setdefault("source", "registered_project")
                            merged_contract["repository_id"] = repo.id
                            merged_contract["repository_path"] = repo.path
                            merged_contract["repository_contract"] = contract
                        else:
                            project_repository_url = self._project_repository_url(project)
                            if project_repository_url and not origin_dict.get("onboarding"):
                                raise ValidationError(
                                    "project %s advertises repository_url %s but has no registered "
                                    "repository contract; complete onboarding, ensure .mac/project.yaml "
                                    "exists in the hub-visible checkout, then run `mac bridge repository "
                                    "register <name> <path> --project %s` before creating normal tasks"
                                    % (project, project_repository_url, project)
                                )
                normalized["execution_contract"] = merged_contract
            return normalized
        repository_contract = origin_dict.get("repository_contract")
        if isinstance(repository_contract, dict) and repository_contract.get("schema"):
            normalized["execution_contract"] = {
                "schema": "mac.task_execution_contract.v1",
                "type": "repository",
                "quality": "strong",
                "source": "task_origin",
                "evidence_type": "repo_change",
                "repository_contract": repository_contract,
            }
            return normalized
        repo = self._repository_for_project(project)
        if repo is not None:
            contract = repo.metadata.get("repository_contract")
            if not isinstance(contract, dict) or not contract.get("schema"):
                contract = self._repository_contract_for_repo(repo)
            origin_dict.setdefault("type", "direct_task")
            origin_dict.setdefault("repository_id", repo.id)
            origin_dict.setdefault("repository_name", repo.name)
            origin_dict.setdefault("repository_path", repo.path)
            origin_dict.setdefault("source", repo.source)
            origin_dict["repository_contract"] = contract
            normalized["origin"] = origin_dict
            acc_metadata = (
                dict(normalized.get("acc_metadata"))
                if isinstance(normalized.get("acc_metadata"), dict)
                else {}
            )
            acc_metadata.setdefault("workflow_role", "work")
            acc_metadata.setdefault("repository_contract_schema", contract["schema"])
            acc_metadata.setdefault("repository_contract_project", contract["project"])
            normalized["acc_metadata"] = acc_metadata
            normalized["execution_contract"] = {
                "schema": "mac.task_execution_contract.v1",
                "type": "repository",
                "quality": "strong",
                "source": "registered_project",
                "evidence_type": "repo_change",
                "repository_id": repo.id,
                "repository_path": repo.path,
                "repository_contract": contract,
            }
            return normalized
        project_repository_url = self._project_repository_url(project)
        if project_repository_url and not origin_dict.get("onboarding"):
            raise ValidationError(
                "project %s advertises repository_url %s but has no registered "
                "repository contract; complete onboarding, ensure .mac/project.yaml "
                "exists in the hub-visible checkout, then run `mac bridge repository "
                "register <name> <path> --project %s` before creating normal tasks"
                % (project, project_repository_url, project)
            )
        policy = normalized.get("policy") if isinstance(normalized.get("policy"), dict) else {}
        evidence_type = str(
            normalized.get("evidence_type")
            or policy.get("evidence_type")
            or policy.get("expected_evidence_type")
            or "operator_result"
        ).strip()
        normalized["execution_contract"] = {
            "schema": "mac.task_execution_contract.v1",
            "type": "operator_directive",
            "quality": "weak",
            "source": "task_crud",
            "repository_required": False,
            "evidence_type": evidence_type,
            "required_capabilities": required_capabilities,
            "reason": "no_registered_repository_or_task_repository_contract",
        }
        return normalized

    def _project_repository_url(self, project: Optional[str]) -> Optional[str]:
        if not project:
            return None
        row = self.store.query_one(
            "SELECT metadata FROM projects WHERE name = ? OR id = ?",
            (project, project),
        )
        if row is None:
            return None
        metadata = ensure_json_object(json_loads(row["metadata"], {}))
        value = metadata.get("repository_url")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _repository_for_project(self, project: Optional[str]) -> Optional[ProjectRepository]:
        if not project:
            return None
        row = self.store.query_one(
            """
            SELECT * FROM project_repositories
            WHERE project = ? AND enabled = ?
            ORDER BY name, id
            LIMIT 1
            """,
            (project, 1),
        )
        return self._repository_from_row(row) if row is not None else None

    def get_task(self, task_id: str) -> Task:
        row = self.store.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise NotFoundError("task not found: %s" % task_id)
        return self._task_from_row(row)

    def list_tasks(
        self,
        state: Optional[str] = None,
        tenant_id: Optional[str] = None,
        *,
        limit: Optional[int] = None,
    ) -> List[Task]:
        # mac-5ayd: dispatch_once / claim_next used to pull EVERY open
        # task into Python and sort in memory on every tick. Pass an
        # explicit ``limit`` from those hot paths so the working set
        # stays bounded; default None keeps full-list semantics for
        # admin / CLI listings.
        limit_clause = ""
        params: list = []
        if state:
            sql = "SELECT * FROM tasks WHERE state = ? ORDER BY priority DESC, created_at"
            params.append(_state_value(state))
        else:
            sql = "SELECT * FROM tasks ORDER BY priority DESC, created_at"
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(int(max(1, limit)))
        rows = self.store.query_all(sql + limit_clause, tuple(params))
        tasks = [self._task_from_row(row) for row in rows]
        if tenant_id is not None:
            tasks = [task for task in tasks if self._task_tenant_id(task) == tenant_id]
        return tasks

    def ready_tasks(
        self,
        *,
        project: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Task]:
        """Open tasks with all dependencies completed and no owner/lease.

        The dispatcher's readiness semantics (parity with ``bd ready``), served
        so the CLI works in hub mode (parity-ready-http-01).
        """
        where = ["state = ?", "owner_agent_id IS NULL", "lease_id IS NULL"]
        params: list = [TaskState.OPEN.value]
        if project is not None:
            where.append("project = ?")
            params.append(project)
        rows = self.store.query_all(
            "SELECT * FROM tasks WHERE %s ORDER BY priority DESC, created_at"
            % " AND ".join(where),
            tuple(params),
        )
        out: List[Task] = []
        for row in rows:
            task = self._task_from_row(row)
            if tenant_id is not None and self._task_tenant_id(task) != tenant_id:
                continue
            if self._task_dispatch_held(task):
                continue  # staged / do-not-dispatch — not claimable until released
            if self._project_dispatch_paused(task.project):
                continue  # project not yet activated for autonomous dispatch
            try:
                ready = self._dependencies_satisfied(task)
            except Exception:  # noqa: BLE001 - a missing dependency blocks readiness
                ready = False
            if ready:
                out.append(task)
            if limit and len(out) >= limit:
                break
        return out

    def search_tasks(
        self,
        query: str,
        *,
        project: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Task]:
        """Keyword search across title/description (parity with bd search)."""
        like = "%" + (query or "") + "%"
        where = ["(title LIKE ? OR description LIKE ?)"]
        params: list = [like, like]
        if project is not None:
            where.append("project = ?")
            params.append(project)
        rows = self.store.query_all(
            "SELECT * FROM tasks WHERE %s ORDER BY priority DESC, created_at DESC LIMIT ?"
            % " AND ".join(where),
            tuple(params + [int(limit)]),
        )
        tasks = [self._task_from_row(row) for row in rows]
        if tenant_id is not None:
            tasks = [t for t in tasks if self._task_tenant_id(t) == tenant_id]
        return tasks

    def task_stats(
        self,
        *,
        project: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """Task counts by state (parity with bd stats)."""
        if tenant_id is not None:
            tasks = self.list_tasks(tenant_id=tenant_id)
            if project is not None:
                tasks = [t for t in tasks if t.project == project]
            counts: Dict[str, int] = {}
            for t in tasks:
                counts[t.state] = counts.get(t.state, 0) + 1
            return dict(sorted(counts.items()))
        where = ""
        params: list = []
        if project is not None:
            where = " WHERE project = ?"
            params.append(project)
        rows = self.store.query_all(
            "SELECT state, COUNT(*) AS n FROM tasks%s GROUP BY state ORDER BY state" % where,
            tuple(params),
        )
        return {row["state"]: int(row["n"]) for row in rows}

    def update_task(
        self,
        task_id: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        project: Optional[str] = None,
        priority: Optional[int] = None,
        required_capabilities: Optional[Iterable[str]] = None,
        dependencies: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        max_attempts: Optional[int] = None,
        actor: str = "human",
    ) -> Task:
        task = self.get_task(task_id)
        updates: List[str] = []
        params: List[Any] = []
        detail: JsonDict = {}
        new_project = task.project
        new_capabilities = list(task.required_capabilities)
        new_metadata = ensure_json_object(task.metadata)
        if title is not None:
            title_value = str(title or "").strip()
            if not title_value:
                raise ValidationError("task title is required")
            updates.append("title = ?")
            params.append(title_value)
            detail["title"] = title_value
        if description is not None:
            updates.append("description = ?")
            params.append(str(description or ""))
            detail["description_changed"] = True
        if project is not None:
            new_project = str(project).strip() or None
            updates.append("project = ?")
            params.append(new_project)
            detail["project"] = new_project
        if priority is not None:
            updates.append("priority = ?")
            params.append(int(priority))
            detail["priority"] = int(priority)
        if dependencies is not None:
            dep_ids = coerce_list(dependencies)
            if task_id in dep_ids:
                raise ValidationError("task cannot depend on itself")
            for dep_id in dep_ids:
                self.get_task(dep_id)
            updates.append("dependencies = ?")
            params.append(json_dumps(dep_ids))
            detail["dependencies"] = dep_ids
            if task.state in {TaskState.OPEN.value, TaskState.BLOCKED.value}:
                next_state = TaskState.BLOCKED.value if dep_ids else TaskState.OPEN.value
                updates.append("state = ?")
                params.append(next_state)
                detail["state"] = next_state
        should_reconcile_metadata = metadata is not None or project is not None or required_capabilities is not None
        explicit_required_capabilities_update = required_capabilities is not None
        if required_capabilities is not None:
            new_capabilities = coerce_list(required_capabilities)
        if metadata is not None:
            new_metadata = ensure_json_object(metadata)
        if should_reconcile_metadata:
            new_capabilities, new_metadata = self._apply_project_task_defaults(
                new_project,
                new_capabilities,
                ensure_json_object(new_metadata),
            )
            new_metadata = self._normalize_task_execution_contract(
                new_metadata,
                new_project,
                new_capabilities,
            )
            new_capabilities, new_metadata = self._decouple_repository_commands_from_capabilities(
                new_capabilities,
                new_metadata,
            )
            if explicit_required_capabilities_update or new_capabilities != list(task.required_capabilities):
                updates.append("required_capabilities = ?")
                params.append(json_dumps(new_capabilities))
                detail["required_capabilities"] = new_capabilities
            updates.append("metadata = ?")
            params.append(json_dumps(new_metadata))
            if metadata is not None:
                detail["metadata_changed"] = True
            else:
                detail["metadata_reconciled"] = True
        if max_attempts is not None:
            if int(max_attempts) < 1:
                raise ValidationError("max_attempts must be >= 1")
            updates.append("max_attempts = ?")
            params.append(int(max_attempts))
            detail["max_attempts"] = int(max_attempts)
        if not updates:
            return task
        updates.append("updated_at = ?")
        params.append(utcnow())
        params.append(task_id)
        self.store.execute(
            "UPDATE tasks SET %s WHERE id = ?" % ", ".join(updates),
            tuple(params),
        )
        updated = self.get_task(task_id)
        self._record_history(
            task_id,
            "task.updated",
            actor,
            task.state,
            updated.state,
            detail,
        )
        return updated

    def append_task_activity(
        self,
        task_id: str,
        phase: str,
        actor: str,
        summary: str,
        *,
        max_entries: int = 24,
    ) -> Task:
        """Append a short, human-readable entry to the task's per-task activity
        narrative (``task.metadata['activity']``).

        This is a glanceable, additive record of what happened on a task -- what
        the worker did, what the reviewer found/fixed, environment changes made to
        build/test -- a few lines per phase, the way a person watching the
        claude/codex/cursor CLI would summarize it. It deliberately does NOT touch
        evidence, history, or the verification pipeline (those remain the durable
        forensic logs); it is surfaced by ``mac task summary`` / ``task show``.
        """
        summary = str(summary or "").strip()
        if not summary:
            return self.get_task(task_id)
        phase = (str(phase or "").strip().lower() or "note")[:24]
        # Keep it glanceable: a few lines, bounded length.
        lines = [ln.rstrip() for ln in summary.splitlines() if ln.strip()][:6]
        summary = "\n".join(lines)[:1200]
        task = self.get_task(task_id)
        metadata = ensure_json_object(task.metadata)
        activity = metadata.get("activity")
        if not isinstance(activity, list):
            activity = []
        activity.append(
            {
                "phase": phase,
                "actor": str(actor or "")[:120],
                "summary": summary,
                "at": utcnow(),
            }
        )
        metadata["activity"] = activity[-max_entries:]
        self.store.execute(
            "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
            (json_dumps(metadata), utcnow(), task_id),
        )
        return self.get_task(task_id)

    def release_task(self, task_id: str, *, actor: str = "human") -> Task:
        """Clear a per-task dispatch hold (``no_dispatch``), un-staging it.

        The inverse of ``mac task create --no-dispatch``: once released the
        task becomes eligible for autonomous claim and re-appears in the ready
        queue (subject to dependencies, capability match, and any project-level
        pause). No-op if the task is not held.
        """
        task = self.get_task(task_id)
        md = ensure_json_object(task.metadata)
        if not md.pop("no_dispatch", None):
            return task
        return self.update_task(task_id, metadata=md, actor=actor)

    def _task_decompose_depth(self, task: Task, *, _max_walk: int = 64) -> int:
        """Count ancestors above *task* via the parent_task_id chain.

        0 = a root task (no parent). Cycle- and runaway-safe: the walk is
        bounded and de-dups visited ids. Used to enforce the decomposition
        depth cap so a deep chain of child-of-child tasks can't recurse forever.
        """
        depth = 0
        current = task
        seen: set = set()
        for _ in range(_max_walk):
            meta = ensure_json_object(current.metadata)
            relationships = ensure_json_object(meta.get("relationships"))
            parent_id = relationships.get("parent_task_id") or meta.get("parent_task_id")
            if not parent_id or parent_id in seen:
                break
            seen.add(parent_id)
            try:
                current = self.get_task(str(parent_id))
            except NotFoundError:
                break
            depth += 1
        return depth

    def add_child_tasks(
        self,
        task_id: str,
        children: Iterable[Dict[str, Any]],
        *,
        actor: str = "human",
    ) -> JsonDict:
        parent = self.get_task(task_id)
        if parent.state not in {
            TaskState.OPEN.value,
            TaskState.BLOCKED.value,
            TaskState.CLAIMED.value,
            TaskState.RUNNING.value,
        }:
            raise ValidationError(
                "child tasks can only be added to open, blocked, claimed, or running tasks"
            )

        # --- Decomposition guardrails (T1) -----------------------------------
        # Server-side backstop against the runaway auto-decompose that hit the
        # live fleet. Refuse to decompose handoff/plan-note tasks, refuse beyond
        # the depth cap, and bound the cumulative child count per parent.
        parent_metadata = ensure_json_object(parent.metadata)
        if parent_metadata.get("no_decompose"):
            raise ValidationError(
                "task %s is marked no_decompose (handoff/plan note) — refusing to "
                "create child tasks; execute or stage it directly" % parent.id
            )
        max_depth = _int_env("MAC_MAX_DECOMPOSE_DEPTH", DEFAULT_MAX_DECOMPOSE_DEPTH)
        depth = self._task_decompose_depth(parent)
        if depth >= max_depth:
            raise ValidationError(
                "decomposition depth limit reached: task %s is at depth %d (max %d) "
                "— execute it directly instead of decomposing further"
                % (parent.id, depth, max_depth)
            )

        specs = [ensure_json_object(spec) for spec in children]
        if not specs:
            raise ValidationError("at least one child task is required")

        max_children = _int_env(
            "MAC_MAX_CHILD_TASKS_PER_PARENT", DEFAULT_MAX_CHILD_TASKS_PER_PARENT
        )
        existing_children = _metadata_string_list(
            ensure_json_object(parent_metadata.get("relationships")).get("child_task_ids")
        )
        projected = len(existing_children) + len(specs)
        if projected > max_children:
            raise ValidationError(
                "child task limit exceeded for %s: %d existing + %d requested = %d "
                "(max %d) — split into a smaller plan or raise "
                "MAC_MAX_CHILD_TASKS_PER_PARENT"
                % (parent.id, len(existing_children), len(specs), projected, max_children)
            )

        prepared: List[JsonDict] = []
        for index, spec in enumerate(specs, start=1):
            title = str(spec.get("title") or "").strip()
            if not title:
                raise ValidationError("child task %d title is required" % index)
            child_dependencies = coerce_list(spec.get("dependencies"))
            if parent.id in child_dependencies:
                raise ValidationError("child task cannot depend on its parent")
            for dep_id in child_dependencies:
                self.get_task(dep_id)
            child_project = (
                str(spec.get("project")).strip()
                if spec.get("project") is not None
                else parent.project
            )
            child_project = child_project or None
            child_capabilities = (
                coerce_list(spec.get("required_capabilities"))
                if spec.get("required_capabilities") is not None
                else list(parent.required_capabilities)
            )
            child_metadata = ensure_json_object(spec.get("metadata"))
            relationships = ensure_json_object(child_metadata.get("relationships"))
            relationships["parent_task_id"] = parent.id
            relationships["relationship"] = "child"
            relationships["blocks"] = _unique_ordered(
                [*_metadata_string_list(relationships.get("blocks")), parent.id]
            )
            child_metadata["relationships"] = relationships
            child_metadata.setdefault("parent_task_id", parent.id)
            child_metadata.setdefault("parent_task_title", parent.title)
            coordination = ensure_json_object(child_metadata.get("coordination"))
            coordination.update(
                {
                    "mode": "cooperative_child",
                    "integration_task_id": parent.id,
                    "require_distinct_agent": True,
                }
            )
            child_metadata["coordination"] = coordination
            normalized_metadata = self._normalize_task_execution_contract(
                child_metadata,
                child_project,
                child_capabilities,
            )
            prepared.append(
                {
                    "id": new_id("task"),
                    "title": title,
                    "description": str(spec.get("description") or ""),
                    "project": child_project,
                    "priority": int(
                        spec["priority"]
                        if spec.get("priority") is not None
                        else parent.priority
                    ),
                    "state": (
                        TaskState.BLOCKED.value
                        if child_dependencies
                        else TaskState.OPEN.value
                    ),
                    "required_capabilities": child_capabilities,
                    "dependencies": child_dependencies,
                    "metadata": normalized_metadata,
                    "max_attempts": int(
                        spec["max_attempts"]
                        if spec.get("max_attempts") is not None
                        else parent.max_attempts
                    ),
                }
            )
            if prepared[-1]["max_attempts"] < 1:
                raise ValidationError("child task max_attempts must be >= 1")

        now = utcnow()
        child_ids = [item["id"] for item in prepared]
        parent_dependencies = _unique_ordered([*parent.dependencies, *child_ids])
        parent_metadata = ensure_json_object(parent.metadata)
        parent_relationships = ensure_json_object(parent_metadata.get("relationships"))
        parent_relationships["child_task_ids"] = _unique_ordered(
            [*_metadata_string_list(parent_relationships.get("child_task_ids")), *child_ids]
        )
        parent_relationships["blocked_by_task_ids"] = parent_dependencies
        parent_metadata["relationships"] = parent_relationships
        parent_coordination = ensure_json_object(parent_metadata.get("coordination"))
        parent_coordination.update(
            {
                "mode": "cooperative_integration",
                "phase": "awaiting_children",
                "child_task_ids": _unique_ordered(
                    [
                        *_metadata_string_list(parent_coordination.get("child_task_ids")),
                        *child_ids,
                    ]
                ),
                "require_distinct_agent": True,
            }
        )
        parent_metadata["coordination"] = parent_coordination

        release_lease_id = parent.lease_id
        with self.store.transaction() as conn:
            for child in prepared:
                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, description, project, priority, state,
                        required_capabilities, dependencies, metadata,
                        owner_agent_id, lease_id, leased_until, attempt_count,
                        max_attempts, started_at, completed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        child["id"],
                        child["title"],
                        child["description"],
                        child["project"],
                        child["priority"],
                        child["state"],
                        json_dumps(child["required_capabilities"]),
                        json_dumps(child["dependencies"]),
                        json_dumps(child["metadata"]),
                        child["max_attempts"],
                        now,
                        now,
                    ),
                )
                self._record_history(
                    child["id"],
                    "task.created",
                    actor,
                    None,
                    child["state"],
                    {
                        "title": child["title"],
                        "parent_task_id": parent.id,
                        "relationship": "child",
                        "dependencies": child["dependencies"],
                    },
                    conn=conn,
                )
            if release_lease_id:
                conn.execute(
                    "UPDATE leases SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                    (LeaseStatus.RELEASED.value, now, release_lease_id, LeaseStatus.ACTIVE.value),
                )
            conn.execute(
                """
                UPDATE tasks
                SET dependencies = ?, metadata = ?, state = ?, owner_agent_id = NULL,
                    lease_id = NULL, leased_until = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    json_dumps(parent_dependencies),
                    json_dumps(parent_metadata),
                    TaskState.BLOCKED.value,
                    now,
                    parent.id,
                ),
            )
            if parent.owner_agent_id:
                self._set_agent_idle(parent.owner_agent_id, conn=conn)
            detail = {
                "child_task_ids": child_ids,
                "blocked_by_task_ids": parent_dependencies,
                "released_lease_id": release_lease_id,
            }
            self._record_history(
                parent.id,
                "task.children_added",
                actor,
                parent.state,
                TaskState.BLOCKED.value,
                detail,
                conn=conn,
            )

        self.drain_task_transition_outbox(task_id=parent.id, limit=20)
        return {
            "parent": self.get_task(parent.id).to_dict(),
            "children": [self.get_task(child_id).to_dict() for child_id in child_ids],
            "relationships": {
                "blocked_by": parent_dependencies,
                "blocks": [],
                "children": child_ids,
            },
        }

    def delete_task(self, task_id: str, *, force: bool = False, actor: str = "human") -> None:
        task = self.get_task(task_id)
        active = self.store.query_one(
            "SELECT 1 FROM leases WHERE task_id = ? AND status = ? LIMIT 1",
            (task_id, LeaseStatus.ACTIVE.value),
        )
        if active is not None:
            raise ValidationError("task cannot be deleted while it has an active lease")
        dependents = [item for item in self.list_tasks() if task_id in item.dependencies]
        if dependents and not force:
            raise ValidationError(
                "task has dependent tasks: %s" % ", ".join(item.id for item in dependents)
            )
        with self.store.transaction() as conn:
            for dependent in dependents:
                remaining = [dep_id for dep_id in dependent.dependencies if dep_id != task_id]
                next_state = (
                    TaskState.OPEN.value
                    if dependent.state == TaskState.BLOCKED.value and not remaining
                    else dependent.state
                )
                conn.execute(
                    """
                    UPDATE tasks SET dependencies = ?, state = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dumps(remaining), next_state, utcnow(), dependent.id),
                )
            conn.execute("DELETE FROM tasks WHERE id = ?", (task.id,))
        self.record_notification(
            "task.deleted",
            "Task deleted: %s" % task.title,
            "Task was deleted by %s." % actor,
            subject_type="task",
            subject_id=task.id,
            channels=["dashboard"],
            metadata={"actor": actor, "force": force},
        )

    @staticmethod
    def _project_item_project_key(item: JsonDict) -> str:
        return str(item.get("project") or item.get("source") or "unassigned")

    @staticmethod
    def _repository_project_key(repository: JsonDict) -> str:
        return str(
            repository.get("project")
            or repository.get("name")
            or repository.get("source")
            or "unassigned"
        )

    def list_projects(self) -> List[JsonDict]:
        return self._hermes_project_contexts(
            self.list_tasks(),
            self.list_agents(),
            [item.to_dict() for item in self.list_project_items()],
            [repository.to_dict() for repository in self.list_project_repositories()],
            [project.to_dict() for project in self.list_project_records()],
        )

    def create_project(
        self,
        name: str,
        description: str = "",
        *,
        metadata: Optional[Dict[str, Any]] = None,
        status: str = "active",
        actor: str = "human",
        project_id: Optional[str] = None,
        dispatch_paused: Optional[bool] = None,
    ) -> ProjectRecord:
        project_name = str(name or "").strip()
        if not project_name:
            raise ValidationError("project name is required")
        if _ONBOARDING_REMOTE_URL_RE.match(project_name):
            # A git URL is not a project name. Storing it as one produces a
            # junk project (name == URL, no repo linkage, nothing cloned).
            # Point the caller at the URL-only onboarding path instead.
            raise ValidationError(
                "project name looks like a git URL (%r); to register a project "
                "from a repository URL use `mac project onboard %s` "
                "(POST /repositories/onboard), which clones the repo and reads "
                "its README/AGENTS/PLAN to build the project."
                % (project_name, project_name)
            )
        existing = self.store.query_one(
            "SELECT * FROM projects WHERE name = ?",
            (project_name,),
        )
        project_metadata = ensure_json_object(metadata)
        if actor:
            project_metadata.setdefault("created_by", actor)
        if dispatch_paused is not None:
            project_metadata["dispatch_paused"] = bool(dispatch_paused)
        if existing is not None:
            return self._project_record_from_row(existing)
        now = utcnow()
        pid = project_id or new_id("project")
        status_value = str(status or "active")
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO projects (id, name, description, metadata, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    project_name,
                    str(description or ""),
                    json_dumps(project_metadata),
                    status_value,
                    now,
                    now,
                ),
            )
            self._record_project_event(
                conn,
                pid,
                "project.created",
                actor,
                {
                    "project_id": pid,
                    "project_name": project_name,
                    "status": status_value,
                    "metadata_keys": sorted(project_metadata.keys()),
                },
                now,
            )
        notification_body = str(description or "Project %s was created." % project_name)
        self.record_notification(
            "project.created",
            "Project created: %s" % project_name,
            notification_body,
            subject_type="project",
            subject_id=project_name,
            channels=["dashboard", "hermes"],
            metadata={"project": project_name, "actor": actor},
        )
        return self.get_project_record(project_name)

    def get_project_record(self, name_or_id: str) -> ProjectRecord:
        row = self.store.query_one(
            "SELECT * FROM projects WHERE name = ? OR id = ?",
            (name_or_id, name_or_id),
        )
        if row is None:
            raise NotFoundError("project record not found: %s" % name_or_id)
        return self._project_record_from_row(row)

    def list_project_records(self) -> List[ProjectRecord]:
        rows = self.store.query_all("SELECT * FROM projects ORDER BY name, id")
        return [self._project_record_from_row(row) for row in rows]

    def get_project(self, project: str) -> JsonDict:
        project_key = str(project or "unassigned").strip() or "unassigned"
        summaries = {
            str(summary.get("project")): summary
            for summary in self.list_projects()
            if isinstance(summary, dict)
        }
        summary = summaries.get(project_key)
        if summary is None:
            raise NotFoundError("project not found: %s" % project_key)

        tasks = [
            task.to_dict()
            for task in self.list_tasks()
            if self._hermes_task_project_key(task) == project_key
        ]
        bridge_items = [
            item.to_dict()
            for item in self.list_project_items()
            if self._project_item_project_key(item.to_dict()) == project_key
        ]
        project_repositories = [
            repository.to_dict()
            for repository in self.list_project_repositories()
            if self._repository_project_key(repository.to_dict()) == project_key
        ]
        return {
            "project": project_key,
            "summary": summary,
            "record": (
                self.get_project_record(project_key).to_dict()
                if summary.get("project_id")
                else None
            ),
            "tasks": tasks,
            "bridge_items": bridge_items,
            "project_repositories": project_repositories,
        }

    def update_project(
        self,
        name_or_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
        actor: str = "human",
    ) -> ProjectRecord:
        project = self.get_project_record(name_or_id)
        updates: List[str] = []
        params: List[Any] = []
        new_name = project.name
        if name is not None:
            name_value = str(name or "").strip()
            if not name_value:
                raise ValidationError("project name is required")
            updates.append("name = ?")
            params.append(name_value)
            new_name = name_value
        changed_fields: List[str] = []
        if name is not None and new_name != project.name:
            changed_fields.append("name")
        if description is not None:
            updates.append("description = ?")
            params.append(str(description or ""))
            if str(description or "") != project.description:
                changed_fields.append("description")
        if metadata is not None:
            updates.append("metadata = ?")
            params.append(json_dumps(ensure_json_object(metadata)))
            changed_fields.append("metadata")
        if status is not None:
            status_value = str(status or "").strip().lower()
            if status_value not in {"active", "inactive", "archived"}:
                raise ValidationError("unsupported project status: %s" % status_value)
            updates.append("status = ?")
            params.append(status_value)
            if status_value != project.status:
                changed_fields.append("status")
        if not updates:
            return project
        now = utcnow()
        with self.store.transaction() as conn:
            updates.append("updated_at = ?")
            params.append(now)
            params.append(project.id)
            try:
                conn.execute(
                    "UPDATE projects SET %s WHERE id = ?" % ", ".join(updates),
                    tuple(params),
                )
            except Exception as exc:  # noqa: BLE001 - normalize sqlite uniqueness errors.
                if "UNIQUE" in str(exc).upper():
                    raise ValidationError("project already exists: %s" % new_name) from exc
                raise
            if new_name != project.name:
                conn.execute("UPDATE tasks SET project = ?, updated_at = ? WHERE project = ?", (new_name, now, project.name))
                conn.execute("UPDATE project_repositories SET project = ?, updated_at = ? WHERE project = ?", (new_name, now, project.name))
            self._record_project_event(
                conn,
                project.id,
                "project.updated",
                actor,
                {
                    "project_id": project.id,
                    "project_name": new_name,
                    "previous_name": project.name,
                    "changed_fields": sorted(set(changed_fields)),
                    "previous_status": project.status,
                    "status": status_value if status is not None else project.status,
                },
                now,
            )
        self.record_notification(
            "project.updated",
            "Project updated: %s" % new_name,
            "Project was updated by %s." % actor,
            subject_type="project",
            subject_id=new_name,
            channels=["dashboard", "hermes"],
            metadata={"previous_name": project.name, "actor": actor},
        )
        return self.get_project_record(new_name)

    def set_project_dispatch(
        self,
        name_or_id: str,
        *,
        paused: bool,
        actor: str = "human",
    ) -> ProjectRecord:
        """Pause or resume autonomous dispatch for an entire project.

        Persists ``metadata.dispatch_paused`` on the project record. When
        paused, the project's open tickets are hidden from the ready queue and
        rejected by autonomous claim (reason ``project_dispatch_paused``);
        operators can still start them explicitly. This is the project-level
        onboarding gate that complements the per-task ``no_dispatch`` hold.
        """
        project = self.get_project_record(name_or_id)
        md = ensure_json_object(project.metadata)
        md["dispatch_paused"] = bool(paused)
        return self.update_project(project.id, metadata=md, actor=actor)

    def delete_project(self, name_or_id: str, *, force: bool = False, actor: str = "human") -> None:
        project = self.get_project_record(name_or_id)
        tasks = [task for task in self.list_tasks() if task.project == project.name]
        repo_rows = self.store.query_all(
            "SELECT id FROM project_repositories WHERE project = ?",
            (project.name,),
        )
        if (tasks or repo_rows) and not force:
            blockers = []
            if tasks:
                blockers.append("%d task(s)" % len(tasks))
            if repo_rows:
                blockers.append("%d Beads repositorie(s)" % len(repo_rows))
            raise ValidationError("project has linked records: %s" % ", ".join(blockers))
        now = utcnow()
        with self.store.transaction() as conn:
            if force:
                conn.execute("UPDATE tasks SET project = NULL, updated_at = ? WHERE project = ?", (now, project.name))
                conn.execute("UPDATE project_repositories SET enabled = 0, updated_at = ? WHERE project = ?", (now, project.name))
            self._record_project_event(
                conn,
                project.id,
                "project.deleted",
                actor,
                {
                    "project_id": project.id,
                    "project_name": project.name,
                    "force": bool(force),
                    "task_count": len(tasks),
                    "project_repository_count": len(repo_rows),
                },
                now,
            )
            conn.execute("DELETE FROM projects WHERE id = ?", (project.id,))
        self.record_notification(
            "project.deleted",
            "Project deleted: %s" % project.name,
            "Project was deleted by %s." % actor,
            subject_type="project",
            subject_id=project.name,
            channels=["dashboard"],
            metadata={"actor": actor, "force": force},
        )

    def task_detail(
        self,
        task_id: str,
        *,
        history_limit: Optional[int] = None,
        evidence_limit: Optional[int] = None,
        review_limit: Optional[int] = None,
        publication_limit: Optional[int] = None,
    ) -> JsonDict:
        task = self.get_task(task_id)
        return {
            "task": task.to_dict(),
            "history": [
                event.to_dict()
                for event in self.task_history(task_id, limit=history_limit)
            ],
            "evidence": [
                item.to_dict()
                for item in self.list_evidence(task_id, limit=evidence_limit)
            ],
            "reviews": [
                item.to_dict()
                for item in self.list_reviews(task_id, limit=review_limit)
            ],
            "publications": [
                item.to_dict()
                for item in self.list_publications(
                    task_id, limit=publication_limit
                )
            ],
        }

    def task_summary(self, task_id: str) -> JsonDict:
        task = self.get_task(task_id).to_dict()
        evidence = [item.to_dict() for item in self.list_evidence(task_id)]
        reviews = [item.to_dict() for item in self.list_reviews(task_id)]
        approved_reviews = [review for review in reviews if review["status"] == ReviewStatus.APPROVED.value]
        publications = [
            pub.to_dict()
            for pub in self.reviews.list_publications(task_id)
            if pub.status == PublicationStatus.PUBLISHED.value
        ]
        parts = ["%s is %s" % (task["title"], task["state"])]
        if task["owner_agent_id"]:
            parts.append("owner=%s" % task["owner_agent_id"])
        if evidence:
            parts.append("%d evidence item(s)" % len(evidence))
        if approved_reviews:
            parts.append("%d approved review(s)" % len(approved_reviews))
        if publications:
            parts.append("published to %s" % publications[-1]["target"])
        return {
            "task_id": task_id,
            "title": task["title"],
            "state": task["state"],
            "owner_agent_id": task["owner_agent_id"],
            "evidence_count": len(evidence),
            "review_count": len(reviews),
            "approved_review_count": len(approved_reviews),
            "publications": publications,
            "origin": task["metadata"].get("origin"),
            "memory_boundary": task["metadata"].get("memory_boundary"),
            "summary": "; ".join(parts),
        }

    def task_history(
        self,
        task_id: str,
        limit: Optional[int] = None,
    ) -> List[HistoryEvent]:
        self.get_task(task_id)
        limit_value = None if limit is None else max(0, int(limit))
        if limit_value == 0:
            return []
        if limit_value is None:
            rows = self.store.query_all(
                "SELECT * FROM task_history WHERE task_id = ? ORDER BY created_at, id",
                (task_id,),
            )
        else:
            rows = list(
                reversed(
                    self.store.query_all(
                        """
                        SELECT * FROM task_history
                        WHERE task_id = ?
                        ORDER BY created_at DESC, id DESC
                        LIMIT ?
                        """,
                        (task_id, limit_value),
                    )
                )
            )
        return [self._history_from_row(row) for row in rows]

    # Unified audit / event stream

    EVENT_SUBJECT_TYPES = (
        "task",
        "rollout",
        "eval_set",
        "secret",
        "environment",
        "conversation_thread",
        "vector_ref",
        "action_event",
        "agent",
        "project",
        "fleet",
    )

    def list_events(
        self,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        actor: Optional[str] = None,
        event_type: Optional[str] = None,
        event_type_prefix: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
    ) -> List[JsonDict]:
        """Query the unified audit stream across task/rollout/eval_set/secret events.

        Filters compose with AND. Results are newest-first; cap is 1000 to keep
        a single page bounded. Operators asking "what happened" should reach for
        this method instead of joining the four per-resource audit tables.
        """
        if subject_type is not None and subject_type not in self.EVENT_SUBJECT_TYPES:
            raise ValidationError(
                "unsupported event subject_type: %s (allowed: %s)"
                % (subject_type, ", ".join(self.EVENT_SUBJECT_TYPES))
            )
        clauses: List[str] = []
        params: List[Any] = []
        if subject_type is not None:
            clauses.append("subject_type = ?")
            params.append(subject_type)
        if subject_id is not None:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        if actor is not None:
            clauses.append("actor = ?")
            params.append(actor)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if event_type_prefix is not None:
            escaped = event_type_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("event_type LIKE ? ESCAPE '\\'")
            params.append(escaped + "%")
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("created_at <= ?")
            params.append(until)
        sql = "SELECT id, subject_type, subject_id, event_type, actor, detail, created_at FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(min(max(1, int(limit)), 1000))
        rows = self.store.query_all(sql, tuple(params))
        return [
            {
                "id": row["id"],
                "subject_type": row["subject_type"],
                "subject_id": row["subject_id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "detail": json_loads(row["detail"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # Observability: thin facade over ``self.observability`` so existing
    # callers keep working. New code should call ``cp.observability.<method>``
    # directly.

    def record_observation(self, *args: Any, **kwargs: Any) -> ObservabilityEvent:
        return self.observability.record_observation(*args, **kwargs)

    def record_metric(self, *args: Any, **kwargs: Any) -> ObservabilityEvent:
        return self.observability.record_metric(*args, **kwargs)

    def record_log(self, *args: Any, **kwargs: Any) -> ObservabilityEvent:
        return self.observability.record_log(*args, **kwargs)

    def list_observability(self, *args: Any, **kwargs: Any) -> List[ObservabilityEvent]:
        return self.observability.list_observability(*args, **kwargs)

    def prune_observability(self, *args: Any, **kwargs: Any) -> int:
        return self.observability.prune(*args, **kwargs)

    def observability_summary(self, *args: Any, **kwargs: Any) -> JsonDict:
        return self.observability.summary(*args, **kwargs)

    # Retention service façade -------------------------------------------

    def _retention_obs_recorder(self, name: str, *, detail: Any = None) -> None:
        """Bridge from RetentionService audit callbacks to observability.

        RetentionService calls this as ``obs_recorder(name, detail=...)``.
        We forward it as an info-level log so the audit trail is visible
        in the same observability stream as every other MAC operation.
        """
        try:
            self.observability.record_log(
                name,
                level="info",
                layer="control_plane",
                source="retention",
                detail=detail or {},
            )
        except Exception:
            pass  # audit must never raise

    def retention_stats(self, record_class: Optional[str] = None) -> List[JsonDict]:
        return self.retention.stats(record_class=record_class)

    def retention_dry_run(self, record_class: str, *, actor: str = "operator") -> JsonDict:
        return self.retention.dry_run(record_class, actor=actor).to_dict()

    def retention_prune(self, record_class: str, *, actor: str = "operator") -> JsonDict:
        return self.retention.prune(record_class, actor=actor).to_dict()

    def retention_prune_all(self, *, actor: str = "operator") -> List[JsonDict]:
        return self.retention.prune_all(actor=actor)

    def retention_list_policies(self) -> List[JsonDict]:
        return self.retention.list_policies()

    # OpenShell policies / action events --------------------------------

    def create_openshell_policy(self, *args: Any, **kwargs: Any) -> Any:
        return self.openshell.create_policy(*args, **kwargs)

    def list_openshell_policies(self, *args: Any, **kwargs: Any) -> Any:
        return self.openshell.list_policies(*args, **kwargs)

    def get_openshell_policy(self, *args: Any, **kwargs: Any) -> Any:
        return self.openshell.get_policy(*args, **kwargs)

    def update_openshell_policy(self, *args: Any, **kwargs: Any) -> Any:
        return self.openshell.update_policy(*args, **kwargs)

    def delete_openshell_policy(self, *args: Any, **kwargs: Any) -> Any:
        return self.openshell.delete_policy(*args, **kwargs)

    def render_openshell_policy(self, *args: Any, **kwargs: Any) -> JsonDict:
        return self.openshell.render_policy(*args, **kwargs)

    def list_openshell_policy_versions(self, *args: Any, **kwargs: Any) -> Any:
        return self.openshell.versions(*args, **kwargs)

    def assign_openshell_policy(self, *args: Any, **kwargs: Any) -> Any:
        return self.openshell.assign_policy(*args, **kwargs)

    def list_openshell_policy_assignments(self, *args: Any, **kwargs: Any) -> Any:
        return self.openshell.list_assignments(*args, **kwargs)

    def report_openshell_status(self, *args: Any, **kwargs: Any) -> Any:
        return self.openshell.report_agent_status(*args, **kwargs)

    def get_openshell_status(self, *args: Any, **kwargs: Any) -> JsonDict:
        return self.openshell.agent_status(*args, **kwargs)

    def record_action_event(self, *args: Any, **kwargs: Any) -> Any:
        return self.action_events.record_action_event(*args, **kwargs)

    def list_action_events(self, *args: Any, **kwargs: Any) -> Any:
        return self.action_events.list_action_events(*args, **kwargs)

    def export_action_events_otlp(self, *args: Any, **kwargs: Any) -> JsonDict:
        return self.action_events.export_otlp(*args, **kwargs)

    def summarize_actions_to_memory(
        self,
        *,
        agent_id: Optional[str] = None,
        since: Optional[str] = None,
        created_by: str = "mac",
        write: bool = True,
    ) -> JsonDict:
        summary = self.action_events.summarize(agent_id=agent_id, since=since)
        content = json_dumps(summary)
        memory = None
        if write and summary["event_count"]:
            subject_type = "agent" if agent_id else "project"
            subject_id = agent_id or "mac"
            memory = self.memory.add_memory(
                None,
                subject_type,
                subject_id,
                "action_summary",
                content,
                None,
                created_by,
            )
            self.action_events.record_action_event(
                actor=created_by,
                action_type="memory",
                action_name="summarize_actions",
                subject_type=subject_type,
                subject_id=subject_id,
                agent_id=agent_id,
                outcome="success",
                severity="info",
                attributes={
                    "schema": "mac.action_summary_memory.v1",
                    "memory_id": memory.id,
                    "event_count": summary["event_count"],
                },
                redaction_state="summary",
            )
        return {
            "schema": "mac.memory.summarize_actions.v1",
            "summary": summary,
            "memory": memory.to_dict() if memory else None,
        }

    # Integration authority ledger -------------------------------------

    def _integration_fingerprint(self, value: Any) -> str:
        return hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()

    def record_integration_observation(
        self,
        source_kind: str,
        source_id: str,
        authority: str,
        status: str,
        *,
        fingerprint: Optional[str] = None,
        cursor: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        observed_at: Optional[str] = None,
        observation_id: Optional[str] = None,
    ) -> IntegrationObservation:
        source_kind_value = str(source_kind or "").strip()
        source_id_value = str(source_id or "").strip()
        authority_value = str(authority or "").strip()
        status_value = str(status or "").strip().lower()
        if not source_kind_value:
            raise ValidationError("integration observation source_kind is required")
        if not source_id_value:
            raise ValidationError("integration observation source_id is required")
        if not authority_value:
            raise ValidationError("integration observation authority is required")
        if not status_value:
            raise ValidationError("integration observation status is required")
        row_id = observation_id or new_id("iobs")
        now = observed_at or utcnow()
        self.store.execute(
            """
            INSERT INTO integration_observations (
                id, source_id, source_kind, authority, status, fingerprint,
                cursor, detail, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                source_id_value,
                source_kind_value,
                authority_value,
                status_value,
                str(fingerprint).strip() if fingerprint else None,
                str(cursor).strip() if cursor else None,
                json_dumps(ensure_json_object(detail)),
                now,
            ),
        )
        row = self.store.query_one("SELECT * FROM integration_observations WHERE id = ?", (row_id,))
        return self._integration_observation_from_row(row)

    def record_integration_finding(
        self,
        source_kind: str,
        source_id: str,
        finding_type: str,
        title: str,
        detail: Optional[Dict[str, Any]] = None,
        *,
        severity: str = "warning",
        fingerprint: Optional[str] = None,
        notify: bool = False,
        channels: Optional[Iterable[str]] = None,
        notification_body: Optional[str] = None,
    ) -> IntegrationFinding:
        source_kind_value = str(source_kind or "").strip()
        source_id_value = str(source_id or "").strip()
        finding_type_value = str(finding_type or "").strip()
        title_value = str(title or "").strip()
        severity_value = str(severity or "warning").strip().lower()
        if not source_kind_value:
            raise ValidationError("integration finding source_kind is required")
        if not source_id_value:
            raise ValidationError("integration finding source_id is required")
        if not finding_type_value:
            raise ValidationError("integration finding finding_type is required")
        if not title_value:
            raise ValidationError("integration finding title is required")
        if severity_value not in {"info", "warning", "error", "critical"}:
            raise ValidationError("unsupported integration finding severity: %s" % severity)
        detail_value = ensure_json_object(detail)
        fingerprint_value = str(fingerprint or "").strip()
        if not fingerprint_value:
            fingerprint_value = self._integration_fingerprint(
                {
                    "source_kind": source_kind_value,
                    "source_id": source_id_value,
                    "finding_type": finding_type_value,
                    "detail": detail_value,
                }
            )
        now = utcnow()
        existing = self.store.query_one(
            """
            SELECT * FROM integration_findings
            WHERE source_kind = ? AND source_id = ? AND finding_type = ? AND fingerprint = ?
            """,
            (source_kind_value, source_id_value, finding_type_value, fingerprint_value),
        )
        was_open = existing is not None and existing["status"] == "open"
        if existing is None:
            finding_id = new_id("ifnd")
            self.store.execute(
                """
                INSERT INTO integration_findings (
                    id, source_id, source_kind, finding_type, severity, status,
                    title, detail, fingerprint, first_seen_at, last_seen_at,
                    resolved_at, resolution
                ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    finding_id,
                    source_id_value,
                    source_kind_value,
                    finding_type_value,
                    severity_value,
                    title_value,
                    json_dumps(detail_value),
                    fingerprint_value,
                    now,
                    now,
                ),
            )
            changed = True
            transition = "opened"
        else:
            finding_id = existing["id"]
            self.store.execute(
                """
                UPDATE integration_findings
                SET severity = ?, status = 'open', title = ?, detail = ?,
                    last_seen_at = ?, resolved_at = NULL, resolution = NULL
                WHERE id = ?
                """,
                (
                    severity_value,
                    title_value,
                    json_dumps(detail_value),
                    now,
                    finding_id,
                ),
            )
            changed = not was_open
            transition = "reopened" if changed else "refreshed"
        finding = self.get_integration_finding(finding_id)
        if changed:
            level = "error" if severity_value in {"error", "critical"} else (
                "warning" if severity_value == "warning" else "info"
            )
            self.record_log(
                "integration.finding.%s" % transition,
                layer="control_plane",
                source="integration-ledger",
                level=level,
                subject_type=source_kind_value,
                subject_id=source_id_value,
                detail=finding.to_dict(),
            )
            if notify:
                self.record_notification(
                    "integration.%s" % finding_type_value,
                    title_value,
                    notification_body or title_value,
                    subject_type=source_kind_value,
                    subject_id=source_id_value,
                    channels=channels or ["dashboard"],
                    metadata={"finding": finding.to_dict()},
                )
        return finding

    def get_integration_finding(self, finding_id: str) -> IntegrationFinding:
        row = self.store.query_one(
            "SELECT * FROM integration_findings WHERE id = ?", (finding_id,)
        )
        if row is None:
            raise NotFoundError("integration finding not found: %s" % finding_id)
        return self._integration_finding_from_row(row)

    def list_integration_observations(
        self,
        source_kind: Optional[str] = None,
        source_id: Optional[str] = None,
        authority: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[IntegrationObservation]:
        clauses: List[str] = []
        params: List[Any] = []
        if source_kind is not None:
            clauses.append("source_kind = ?")
            params.append(str(source_kind).strip())
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(str(source_id).strip())
        if authority is not None:
            clauses.append("authority = ?")
            params.append(str(authority).strip())
        if status is not None:
            clauses.append("status = ?")
            params.append(str(status).strip().lower())
        sql = "SELECT * FROM integration_observations"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY observed_at DESC, id DESC LIMIT ?"
        params.append(min(max(1, int(limit)), 1000))
        return [
            self._integration_observation_from_row(row)
            for row in self.store.query_all(sql, tuple(params))
        ]

    def list_integration_findings(
        self,
        source_kind: Optional[str] = None,
        source_id: Optional[str] = None,
        finding_type: Optional[str] = None,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 100,
    ) -> List[IntegrationFinding]:
        clauses: List[str] = []
        params: List[Any] = []
        if source_kind is not None:
            clauses.append("source_kind = ?")
            params.append(str(source_kind).strip())
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(str(source_id).strip())
        if finding_type is not None:
            clauses.append("finding_type = ?")
            params.append(str(finding_type).strip())
        if status is not None:
            clauses.append("status = ?")
            params.append(str(status).strip().lower())
        if severity is not None:
            clauses.append("severity = ?")
            params.append(str(severity).strip().lower())
        sql = "SELECT * FROM integration_findings"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += """
            ORDER BY
                CASE status WHEN 'open' THEN 0 WHEN 'suppressed' THEN 1 ELSE 2 END,
                last_seen_at DESC,
                id DESC
            LIMIT ?
        """
        params.append(min(max(1, int(limit)), 1000))
        return [
            self._integration_finding_from_row(row)
            for row in self.store.query_all(sql, tuple(params))
        ]

    def resolve_integration_finding(
        self,
        finding_id: str,
        *,
        resolution: str = "resolved",
    ) -> IntegrationFinding:
        finding = self.get_integration_finding(finding_id)
        if finding.status == "resolved":
            return finding
        now = utcnow()
        self.store.execute(
            """
            UPDATE integration_findings
            SET status = 'resolved', resolved_at = ?, resolution = ?
            WHERE id = ?
            """,
            (now, str(resolution or "resolved").strip(), finding_id),
        )
        resolved = self.get_integration_finding(finding_id)
        self.record_log(
            "integration.finding.resolved",
            layer="control_plane",
            source="integration-ledger",
            level="info",
            subject_type=resolved.source_kind,
            subject_id=resolved.source_id,
            detail=resolved.to_dict(),
        )
        return resolved

    def record_notification(
        self,
        event_type: str,
        title: str,
        body: str,
        *,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        channels: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        status: str = "pending",
        conn: Any = None,
        created_at: Optional[str] = None,
    ) -> OperatorNotification:
        event_value = str(event_type or "").strip()
        title_value = str(title or "").strip()
        body_value = str(body or "").strip()
        status_value = str(status or "pending").strip().lower()
        if not event_value:
            raise ValidationError("notification event_type is required")
        if not title_value:
            raise ValidationError("notification title is required")
        if not body_value:
            raise ValidationError("notification body is required")
        if status_value not in {"pending", "delivered", "failed", "skipped"}:
            raise ValidationError("unsupported notification status: %s" % status)
        channel_list = [
            str(item).strip()
            for item in (channels or ["dashboard"])
            if str(item).strip()
        ]
        if not channel_list:
            channel_list = ["dashboard"]
        notification_id = new_id("note")
        now = created_at or utcnow()
        writer = conn if conn is not None else self.store
        writer.execute(
            """
            INSERT INTO operator_notifications (
                id, event_type, subject_type, subject_id, title, body,
                channels, metadata, status, created_at, delivered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                notification_id,
                event_value,
                subject_type,
                subject_id,
                title_value,
                body_value,
                json_dumps(channel_list),
                json_dumps(ensure_json_object(metadata)),
                status_value,
                now,
            ),
        )
        if conn is not None:
            row = conn.execute(
                "SELECT * FROM operator_notifications WHERE id = ?", (notification_id,)
            ).fetchone()
            return self._notification_from_row(row)
        return self.get_notification(notification_id)

    def get_notification(self, notification_id: str) -> OperatorNotification:
        row = self.store.query_one(
            "SELECT * FROM operator_notifications WHERE id = ?", (notification_id,)
        )
        if row is None:
            raise NotFoundError("notification not found: %s" % notification_id)
        return self._notification_from_row(row)

    def list_notifications(
        self,
        status: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[OperatorNotification]:
        clauses: List[str] = []
        params: List[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(str(status).strip().lower())
        if subject_type is not None:
            clauses.append("subject_type = ?")
            params.append(subject_type)
        if subject_id is not None:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        sql = "SELECT * FROM operator_notifications"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(min(max(1, int(limit)), 1000))
        return [
            self._notification_from_row(row)
            for row in self.store.query_all(sql, tuple(params))
        ]

    def mark_notification_delivered(
        self,
        notification_id: str,
        *,
        status: str = "delivered",
    ) -> OperatorNotification:
        status_value = str(status or "delivered").strip().lower()
        if status_value not in {"delivered", "failed", "skipped"}:
            raise ValidationError("unsupported delivered notification status: %s" % status)
        current = self.get_notification(notification_id)
        # Refuse overwriting a terminal status so a late/duplicate ack cannot mask a real non-delivery; same-status is an idempotent no-op.
        if current.status in {"delivered", "failed", "skipped"}:
            if current.status == status_value:
                return current
            raise TransitionError(
                "notification %s already %s; refusing to set %s"
                % (notification_id, current.status, status_value)
            )
        now = utcnow()
        self.store.execute(
            """
            UPDATE operator_notifications
            SET status = ?, delivered_at = ?
            WHERE id = ?
            """,
            (status_value, now, notification_id),
        )
        return self.get_notification(notification_id)

    def configure_notifier_channel(self, *args: Any, **kwargs: Any) -> NotifierChannel:
        return self.notifiers.configure_channel(*args, **kwargs)

    def get_notifier_channel(self, channel_id_or_name: str) -> NotifierChannel:
        return self.notifiers.get_channel(channel_id_or_name)

    def list_notifier_channels(self, *args: Any, **kwargs: Any) -> List[NotifierChannel]:
        return self.notifiers.list_channels(*args, **kwargs)

    def delete_notifier_channel(self, channel_id_or_name: str) -> None:
        return self.notifiers.delete_channel(channel_id_or_name)

    def deliver_pending_notifications(self, *args: Any, **kwargs: Any) -> JsonDict:
        return self.notifiers.deliver_pending(*args, **kwargs)

    # Short-retention command audit -------------------------------------

    # mac-6m14: scrub common secret-bearing argv shapes before writing.
    _SECRET_KEY_HINTS = (
        "password",
        "passwd",
        "secret",
        "token",
        "apikey",
        "api_key",
        "api-key",
        "credential",
        "auth",
    )
    # High-entropy bare values: anything resembling a base64/hex blob of
    # ≥ 32 chars and not a path, URL, or numeric.
    _ARGV_BARE_HIGH_ENTROPY_RE = re.compile(r"^[A-Za-z0-9+/=_-]{32,}$")

    @classmethod
    def _scrub_argv(cls, argv: Iterable[str]) -> List[str]:
        """Replace secret-bearing argv elements with redaction markers.

        Common patterns:
          ``--token=abc123`` -> ``--token=<redacted>``
          ``--password sekret`` -> ``--password <redacted>`` (best-effort
            via key-then-value matching)
          ``ghp_abcdef...`` (bare 32+ char alphanum) -> ``<redacted>``
        """
        out: List[str] = []
        skip_next = False
        for raw in argv:
            item = str(raw)
            if skip_next:
                out.append("<redacted>")
                skip_next = False
                continue
            # --foo=bar where the key contains a secret hint
            if item.startswith("-") and "=" in item:
                key_part, _sep, value = item.partition("=")
                if any(hint in key_part.lower() for hint in cls._SECRET_KEY_HINTS) and value:
                    out.append("%s=<redacted>" % key_part)
                    continue
            # --foo  <value>: key flag followed by a separate value
            if item.startswith("-") and any(
                hint in item.lower() for hint in cls._SECRET_KEY_HINTS
            ):
                out.append(item)
                skip_next = True
                continue
            # Bare high-entropy values; skip URLs and absolute paths.
            if (
                cls._ARGV_BARE_HIGH_ENTROPY_RE.match(item)
                and "/" not in item
                and ":" not in item
                and not item.isdigit()
            ):
                out.append("<redacted>")
                continue
            out.append(item)
        return out

    def record_command_audit(
        self,
        agent_id: str,
        phase: str,
        argv: Iterable[str],
        cwd: str,
        command_id: Optional[str] = None,
        task_id: Optional[str] = None,
        lease_id: Optional[str] = None,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        duration_ms: Optional[float] = None,
        returncode: Optional[int] = None,
        stdout_sha256: Optional[str] = None,
        stderr_sha256: Optional[str] = None,
        stdout_bytes: Optional[int] = None,
        stderr_bytes: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        retention_seconds: Optional[int] = None,
    ) -> CommandAuditRecord:
        self.get_agent(agent_id)
        phase_value = str(phase or "").strip().lower()
        if phase_value not in COMMAND_AUDIT_PHASES:
            raise ValidationError("unsupported command audit phase: %s" % phase)
        argv_raw = [str(item) for item in argv]
        if not argv_raw:
            raise ValidationError("command audit requires argv")
        # mac-6m14: scrub before persisting so secrets on argv never
        # land in command_audit or observability detail.
        argv_list = self._scrub_argv(argv_raw)
        cwd_value = str(cwd or "").strip()
        if not cwd_value:
            raise ValidationError("command audit requires cwd")
        if task_id:
            self.get_task(task_id)
        audit_id = new_id("cmda")
        cid = command_id or new_id("cmd")
        now = utcnow()
        detail = ensure_json_object(metadata or {})
        retention = self._command_audit_retention_seconds(retention_seconds)
        cutoff = (
            parse_time(now) - timedelta(seconds=retention)
        ).isoformat(timespec="microseconds")
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO command_audit (
                    id, command_id, agent_id, phase, argv, cwd, task_id, lease_id,
                    started_at, completed_at, duration_ms, returncode,
                    stdout_sha256, stderr_sha256, stdout_bytes, stderr_bytes,
                    metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    cid,
                    agent_id,
                    phase_value,
                    json_dumps(argv_list),
                    cwd_value,
                    task_id,
                    lease_id,
                    started_at,
                    completed_at,
                    duration_ms,
                    returncode,
                    stdout_sha256,
                    stderr_sha256,
                    stdout_bytes,
                    stderr_bytes,
                    json_dumps(detail),
                    now,
                ),
            )
            self.action_events.project_command_audit(
                conn,
                audit_id=audit_id,
                command_id=cid,
                agent_id=agent_id,
                phase=phase_value,
                argv=argv_list,
                cwd=cwd_value,
                task_id=task_id,
                lease_id=lease_id,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                returncode=returncode,
                stdout_sha256=stdout_sha256,
                stderr_sha256=stderr_sha256,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                metadata=detail,
                timestamp=now,
            )
            conn.execute("DELETE FROM command_audit WHERE created_at < ?", (cutoff,))
            self.observability.insert_observation(
                conn,
                "log",
                "command.%s" % phase_value,
                "worker",
                agent_id,
                "error" if phase_value in {"failed", "timeout", "error"} else "info",
                None,
                "",
                "task" if task_id else "agent",
                task_id or agent_id,
                {
                    "command_id": cid,
                    "argv": argv_list,
                    "cwd": cwd_value,
                    "task_id": task_id,
                    "lease_id": lease_id,
                    "duration_ms": duration_ms,
                    "returncode": returncode,
                    **detail,
                },
                now,
            )
        return self.get_command_audit(audit_id)

    def get_command_audit(self, audit_id: str) -> CommandAuditRecord:
        row = self.store.query_one("SELECT * FROM command_audit WHERE id = ?", (audit_id,))
        if row is None:
            raise NotFoundError("command audit record not found: %s" % audit_id)
        return self._command_audit_from_row(row)

    def list_command_audit(
        self,
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
        command_id: Optional[str] = None,
        phase: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 200,
    ) -> List[CommandAuditRecord]:
        self.prune_command_audit()
        clauses: List[str] = []
        params: List[Any] = []
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if command_id is not None:
            clauses.append("command_id = ?")
            params.append(command_id)
        if phase is not None:
            phase_value = str(phase).strip().lower()
            if phase_value not in COMMAND_AUDIT_PHASES:
                raise ValidationError("unsupported command audit phase: %s" % phase)
            clauses.append("phase = ?")
            params.append(phase_value)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("created_at <= ?")
            params.append(until)
        sql = "SELECT * FROM command_audit"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(min(max(1, int(limit)), 1000))
        return [
            self._command_audit_from_row(row)
            for row in self.store.query_all(sql, tuple(params))
        ]

    def prune_command_audit(self, older_than: Optional[str] = None) -> int:
        cutoff = older_than
        if cutoff is None:
            now = utcnow()
            retention = self._command_audit_retention_seconds(None)
            cutoff = (
                parse_time(now) - timedelta(seconds=retention)
            ).isoformat(timespec="microseconds")
        cursor = self.store.execute(
            "DELETE FROM command_audit WHERE created_at < ?", (cutoff,)
        )
        return int(cursor.rowcount or 0)

    def _command_audit_retention_seconds(self, override: Optional[int]) -> int:
        if override is not None:
            return max(60, int(override))
        raw = os.environ.get("MAC_COMMAND_AUDIT_RETENTION_SECONDS")
        if raw:
            return max(60, int(raw))
        return 24 * 60 * 60

    def list_dead_letters(
        self,
        tenant_id: Optional[str] = None,
        *,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> List[Task]:
        return self.list_dead_letters_page(
            tenant_id=tenant_id,
            limit=limit,
            cursor=cursor,
        )["tasks"]

    def list_dead_letters_page(
        self,
        tenant_id: Optional[str] = None,
        *,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> JsonDict:
        limit_value = max(1, min(int(limit), 1000))
        clauses = ["state = ?", "attempt_count >= max_attempts"]
        params: List[Any] = [TaskState.FAILED.value]
        if tenant_id is not None:
            clauses.append(
                "COALESCE("
                "NULLIF(json_extract(metadata, '$.origin.tenant_id'), ''), "
                "NULLIF(json_extract(metadata, '$.tenant_id'), '')"
                ") = ?"
            )
            params.append(tenant_id)
        decoded = self._decode_scan_cursor(cursor, "dead-letters")
        if decoded is not None:
            updated_at, task_id = decoded
            clauses.append("(updated_at > ? OR (updated_at = ? AND id > ?))")
            params.extend([updated_at, updated_at, task_id])
        params.append(limit_value + 1)
        rows = self.store.query_all(
            "SELECT * FROM tasks WHERE %s "
            "ORDER BY updated_at, id LIMIT ?" % " AND ".join(clauses),
            tuple(params),
        )
        has_more = len(rows) > limit_value
        tasks = [self._task_from_row(row) for row in rows[:limit_value]]
        next_cursor = (
            self._encode_scan_cursor(
                "dead-letters",
                tasks[-1].updated_at,
                tasks[-1].id,
            )
            if has_more and tasks
            else None
        )
        return {
            "tasks": tasks,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    def _encode_scan_cursor(self, kind: str, position: str, item_id: str) -> str:
        payload = json_dumps(
            {"kind": kind, "position": position, "item_id": item_id}
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return "v1:%s" % encoded

    def _decode_scan_cursor(
        self,
        cursor: Optional[str],
        kind: str,
    ) -> Optional[Tuple[str, str]]:
        if cursor is None:
            return None
        raw = str(cursor).strip()
        if not raw.startswith("v1:"):
            raise ValidationError("invalid %s cursor" % kind)
        encoded = raw[3:]
        try:
            padding = "=" * (-len(encoded) % 4)
            payload = json_loads(
                base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode(
                    "utf-8"
                ),
                {},
            )
            payload_kind = str(payload["kind"])
            position = str(payload["position"])
            item_id = str(payload["item_id"])
        except (binascii.Error, KeyError, TypeError, ValueError, UnicodeError):
            raise ValidationError("invalid %s cursor" % kind) from None
        if payload_kind != kind or not position or not item_id:
            raise ValidationError("invalid %s cursor" % kind)
        return position, item_id

    def transition_task(
        self,
        task_id: str,
        target_state: str,
        actor: str,
        detail: Optional[Dict[str, Any]] = None,
        *,
        drain_outbox: bool = True,
    ) -> Task:
        return self._transition_task_impl(
            task_id,
            target_state,
            actor,
            detail,
            drain_outbox=drain_outbox,
            conn=None,
        )

    def _transition_task_in_transaction(
        self,
        conn: Any,
        task_id: str,
        target_state: str,
        actor: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Task:
        return self._transition_task_impl(
            task_id,
            target_state,
            actor,
            detail,
            drain_outbox=False,
            conn=conn,
        )

    def _transition_task_impl(
        self,
        task_id: str,
        target_state: str,
        actor: str,
        detail: Optional[Dict[str, Any]] = None,
        *,
        drain_outbox: bool,
        conn: Optional[Any],
    ) -> Task:
        target = _state_value(target_state)
        if conn is None:
            task = self.get_task(task_id)
        else:
            task_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task_row is None:
                raise NotFoundError("task not found: %s" % task_id)
            task = self._task_from_row(task_row)
        transition_detail = dict(detail or {})
        if target == TaskState.CANCELLED.value:
            transition_detail = normalize_cancellation_detail(transition_detail)
        detail = transition_detail
        if task.state == target:
            # A terminal cancellation may be re-submitted solely to backfill or
            # correct its repository-ref disposition. Keep the original
            # terminal timestamp so this cannot reset the grace period.
            if target != TaskState.CANCELLED.value:
                return task
            lifecycle = repository_ref_lifecycle_for_transition(
                target,
                detail,
                now=task.completed_at or utcnow(),
            )
            metadata = ensure_json_object(task.metadata)
            if metadata.get("repository_ref_lifecycle") == lifecycle:
                if drain_outbox and conn is None:
                    self.drain_task_transition_outbox(task_id=task_id, limit=20)
                return task
            metadata["repository_ref_lifecycle"] = lifecycle
            now = utcnow()

            def apply_lifecycle(transaction: Any) -> None:
                transaction.execute(
                    "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json_dumps(metadata), now, task_id),
                )
                self._record_history(
                    task_id,
                    "repository_ref.lifecycle_updated",
                    actor,
                    target,
                    target,
                    detail,
                    conn=transaction,
                )
                self.task_ledger.enqueue_outbox(
                    transaction,
                    task_id=task_id,
                    event_type="task.lifecycle",
                    actor=actor,
                    from_state=target,
                    to_state=target,
                    detail=detail,
                    created_at=now,
                )
            if conn is None:
                with self.store.transaction() as transaction:
                    apply_lifecycle(transaction)
                if drain_outbox:
                    self.drain_task_transition_outbox(task_id=task_id, limit=20)
                return self.get_task(task_id)
            apply_lifecycle(conn)
            transitioned_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if transitioned_row is None:
                raise NotFoundError("task not found: %s" % task_id)
            return self._task_from_row(transitioned_row)
        validate_transition(task.state, target)
        review_ready_evidence: Optional[Evidence] = None
        if target == TaskState.NEEDS_REVIEW.value:
            review_ready_evidence = self._require_review_ready(task)
        if target == TaskState.COMPLETED.value and not self.reviews.completion_authorized(task_id):
            raise ValidationError("task completion requires approved review and evidence")
        now = utcnow()
        updated_metadata: Optional[JsonDict] = None
        candidate_metadata = ensure_json_object(task.metadata)
        metadata_changed = False
        if review_ready_evidence is not None:
            candidate_metadata["review_target"] = {
                "executor_evidence_id": review_ready_evidence.id,
                "attempt_count": task.attempt_count,
                "recorded_at": now,
            }
            metadata_changed = True
        elif target in {
            TaskState.OPEN.value,
            TaskState.BLOCKED.value,
            TaskState.RUNNING.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        }:
            if candidate_metadata.pop("review_target", None) is not None:
                metadata_changed = True
        repository_ref_lifecycle = repository_ref_lifecycle_for_transition(
            target,
            detail,
            now=now,
        )
        if repository_ref_lifecycle is not None:
            if candidate_metadata.get("repository_ref_lifecycle") != repository_ref_lifecycle:
                candidate_metadata["repository_ref_lifecycle"] = repository_ref_lifecycle
                metadata_changed = True
        if metadata_changed:
            updated_metadata = candidate_metadata
        owner_agent_id = task.owner_agent_id
        lease_id = task.lease_id
        leased_until = task.leased_until
        release_lease_id = None
        if target in {
            TaskState.BLOCKED.value,
            TaskState.OPEN.value,
            TaskState.NEEDS_REVIEW.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        }:
            release_lease_id = lease_id
            owner_agent_id = None
            lease_id = None
            leased_until = None
        # mac-d2xh: a dead-letter requeue (FAILED→OPEN or CANCELLED→OPEN)
        # must reset attempt_count and clear completed_at; otherwise the
        # next claim immediately fails the cap check (attempt_count >=
        # max_attempts) and the requeue is a no-op.
        is_requeue_from_terminal = (
            task.state in {TaskState.FAILED.value, TaskState.CANCELLED.value}
            and target == TaskState.OPEN.value
        )

        def apply_transition(conn: Any) -> None:
            if release_lease_id:
                conn.execute(
                    "UPDATE leases SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                    (LeaseStatus.RELEASED.value, now, release_lease_id, LeaseStatus.ACTIVE.value),
                )
            if is_requeue_from_terminal:
                changed = conn.execute(
                    """
                    UPDATE tasks
                    SET state = ?, owner_agent_id = ?, lease_id = ?, leased_until = ?,
                        started_at = NULL, completed_at = NULL,
                        attempt_count = 0, updated_at = ?
                    WHERE id = ? AND state = ?
                    """,
                    (
                        target,
                        owner_agent_id,
                        lease_id,
                        leased_until,
                        now,
                        task_id,
                        task.state,
                    ),
                )
            else:
                changed = conn.execute(
                    """
                    UPDATE tasks
                    SET state = ?, owner_agent_id = ?, lease_id = ?, leased_until = ?,
                        started_at = ?, completed_at = ?, updated_at = ?
                    WHERE id = ? AND state = ?
                    """,
                    (
                        target,
                        owner_agent_id,
                        lease_id,
                        leased_until,
                        now if target == TaskState.RUNNING.value and not task.started_at else task.started_at,
                        now if target in TERMINAL_TASK_STATES and not task.completed_at else task.completed_at,
                        now,
                        task_id,
                        task.state,
                    ),
                )
            if changed.rowcount != 1:
                raise TransitionError("task state changed during transition; retry")
            if updated_metadata is not None:
                conn.execute(
                    "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json_dumps(updated_metadata), now, task_id),
                )
            if task.owner_agent_id and target in TERMINAL_TASK_STATES.union(
                {TaskState.BLOCKED.value, TaskState.OPEN.value, TaskState.NEEDS_REVIEW.value}
            ):
                self._set_agent_idle(task.owner_agent_id, conn=conn)
            self._record_history(
                task_id, "task.transitioned", actor, task.state, target, detail or {}, conn=conn
            )
            self.task_ledger.enqueue_outbox(
                conn,
                task_id=task_id,
                event_type="task.lifecycle",
                actor=actor,
                from_state=task.state,
                to_state=target,
                detail=detail or {},
                created_at=now,
            )
            if target in TERMINAL_TASK_STATES.union({TaskState.BLOCKED.value}):
                row = conn.execute(
                    "SELECT workflow_run_id FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is not None and row["workflow_run_id"]:
                    self.task_ledger.enqueue_outbox(
                        conn,
                        task_id=task_id,
                        event_type="workflow.advance",
                        actor=actor,
                        from_state=task.state,
                        to_state=target,
                        detail=detail or {},
                        created_at=now,
                    )
        if conn is None:
            with self.store.transaction() as transaction:
                apply_transition(transaction)
        else:
            apply_transition(conn)
            transitioned_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if transitioned_row is None:
                raise NotFoundError("task not found: %s" % task_id)
            return self._task_from_row(transitioned_row)
        if drain_outbox:
            self.drain_task_transition_outbox(task_id=task_id, limit=20)
        # Self-documenting failures: on a block/fail, append a glanceable
        # 'Problem / Remediation' note to the task's activity log so the cause +
        # fix are visible in `mac task show`/`summary` without digging through
        # logs. Best-effort: diagnostics must never break the transition.
        try:
            diagnosis = _failure_diagnosis(target, detail)
            if diagnosis:
                self.append_task_activity(task_id, "diagnosis", actor, diagnosis)
        except Exception:  # noqa: BLE001 - diagnostics are advisory only
            pass
        transitioned = self.get_task(task_id)
        return transitioned

    def reopen_task(
        self,
        task_id: str,
        actor: str,
        reason: Optional[str] = None,
    ) -> Task:
        """Recovery action: return a stuck/terminal task to OPEN so it can be
        retried or reconciled.

        Valid from ``failed``/``cancelled`` (resets ``attempt_count`` and clears
        ``completed_at`` so the requeue isn't immediately re-exhausted) and from
        ``blocked``; the state machine rejects reopening an already-``completed``
        task. Records who reopened it and why. Counterpart to
        :meth:`force_complete_task`.
        """
        detail: Dict[str, Any] = {"via": "operator_reopen"}
        if reason:
            detail["reason"] = reason
        return self.transition_task(task_id, TaskState.OPEN.value, actor, detail)

    def force_complete_task(
        self,
        task_id: str,
        actor: str,
        reason: Optional[str] = None,
    ) -> Task:
        """Operator override: mark a task COMPLETED regardless of its current
        state or review status.

        For reconciling work done out-of-band (e.g. a task whose change merged
        via a PR) or recovering a task stranded in a terminal state where the
        normal review→publish path can no longer run. This deliberately bypasses
        the review/evidence completion gate, so it records who forced it, the
        prior state, and why. Recovery counterpart to :meth:`reopen_task`.
        """
        task = self.get_task(task_id)
        if task.state == TaskState.COMPLETED.value:
            return task
        now = utcnow()
        detail: Dict[str, Any] = {"via": "operator_force_complete", "from_state": task.state}
        if reason:
            detail["reason"] = reason
        metadata = ensure_json_object(task.metadata)
        metadata["repository_ref_lifecycle"] = repository_ref_lifecycle_for_transition(
            TaskState.COMPLETED.value,
            detail,
            now=now,
        )
        with self.store.transaction() as conn:
            if task.lease_id:
                conn.execute(
                    "UPDATE leases SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                    (LeaseStatus.RELEASED.value, now, task.lease_id, LeaseStatus.ACTIVE.value),
                )
            if task.owner_agent_id:
                self._set_agent_idle(task.owner_agent_id, conn=conn)
            conn.execute(
                """
                UPDATE tasks
                SET state = ?, owner_agent_id = NULL, lease_id = NULL, leased_until = NULL,
                    completed_at = COALESCE(completed_at, ?), metadata = ?, updated_at = ?
                WHERE id = ?
                """,
                (TaskState.COMPLETED.value, now, json_dumps(metadata), now, task_id),
            )
            self._record_history(
                task_id,
                "task.force_completed",
                actor,
                task.state,
                TaskState.COMPLETED.value,
                detail,
                conn=conn,
            )
            self.task_ledger.enqueue_outbox(
                conn,
                task_id=task_id,
                event_type="task.lifecycle",
                actor=actor,
                from_state=task.state,
                to_state=TaskState.COMPLETED.value,
                detail=detail,
                created_at=now,
            )
        self.drain_task_transition_outbox(task_id=task_id, limit=20)
        return self.get_task(task_id)

    def claim_task(
        self,
        task_id: str,
        agent_id: str,
        lease_seconds: int = 900,
        *,
        sync_beads: bool = True,
    ) -> Tuple[Task, Lease]:
        task = self.get_task(task_id)
        agent = self.get_agent(agent_id)
        if (
            task.state == TaskState.BLOCKED.value
            and task.dependencies
            and self._dependencies_satisfied(task)
            and not self._blocked_task_requires_manual_repair(task)
        ):
            task = self._prepare_cooperative_integration_task(task)
            task = self.transition_task(task_id, TaskState.OPEN.value, "dispatcher", {"reason": "dependencies satisfied"})
        if task.state != TaskState.OPEN.value:
            raise TransitionError("only open tasks can be claimed")
        # mac-1g3u: the tenant gate also runs as an explicit chokepoint
        # in claim_task itself, not only through _agent_available_for.
        # A future dispatch path that forgets the broader eligibility
        # check still hits this assertion because every claim must go
        # through claim_task. The richer Python policy lives in
        # _machine_allows_tenant; this is the redundant safety belt.
        machine = self.get_machine(agent.machine_id)
        task_tenant = self._task_tenant_id(task)
        if not self._machine_allows_tenant(machine, task_tenant):
            raise AuthorizationError(
                "machine %s tenant policy refuses tenant %s for task %s"
                % (machine.id, task_tenant, task_id)
            )
        if not self._agent_available_for(agent, task):
            raise ValidationError("agent %s cannot claim task %s" % (agent_id, task_id))
        if task.attempt_count >= task.max_attempts:
            self.transition_task(task_id, TaskState.FAILED.value, "dispatcher", {"reason": "max attempts"})
            raise TransitionError("task %s exhausted max_attempts" % task_id)
        now = utcnow()
        expires_at = (parse_time(now) + timedelta(seconds=int(lease_seconds))).isoformat(timespec="microseconds")
        lease_id = new_id("lease")
        coordination_related_ids = self._coordination_related_task_ids(task)
        coordination_lock_task_id: Optional[str] = None
        if coordination_related_ids:
            relationships = ensure_json_object(
                ensure_json_object(task.metadata).get("relationships")
            )
            coordination_lock_task_id = str(
                relationships.get("parent_task_id")
                or ensure_json_object(task.metadata).get("parent_task_id")
                or task.id
            ).strip()
        with self.store.transaction() as conn:
            if coordination_lock_task_id:
                # Serialize family participation across dispatchers.  The
                # eligibility check above is intentionally repeated while a
                # shared parent-row lock is held, closing the race where two
                # child claims could otherwise assign the same agent before
                # either lease became visible.
                family_lock = conn.execute(
                    "UPDATE tasks SET updated_at = updated_at WHERE id = ?",
                    (coordination_lock_task_id,),
                )
                if family_lock.rowcount != 1:
                    raise ValidationError(
                        "cooperative task family lock %s is unavailable"
                        % coordination_lock_task_id
                    )
                placeholders = ",".join("?" for _ in coordination_related_ids)
                prior_participation = conn.execute(
                    "SELECT 1 FROM leases WHERE agent_id = ? AND task_id IN (%s) LIMIT 1"
                    % placeholders,
                    (agent_id, *sorted(coordination_related_ids)),
                ).fetchone()
                if prior_participation is not None:
                    raise ValidationError(
                        "agent %s already participated in cooperative task family for %s"
                        % (agent_id, task_id)
                    )
            # Atomic claim: the UPDATE only succeeds if the task is still OPEN and
            # unleased. rowcount==0 means another dispatcher already took it.
            cursor = conn.execute(
                """
                UPDATE tasks
                SET state = ?, owner_agent_id = ?, lease_id = ?, leased_until = ?,
                    attempt_count = attempt_count + 1, updated_at = ?
                WHERE id = ? AND state = ? AND lease_id IS NULL
                """,
                (
                    TaskState.CLAIMED.value,
                    agent_id,
                    lease_id,
                    expires_at,
                    now,
                    task_id,
                    TaskState.OPEN.value,
                ),
            )
            if cursor.rowcount != 1:
                raise TransitionError("task %s was claimed by another agent" % task_id)
            conn.execute(
                """
                INSERT INTO leases (id, task_id, agent_id, expires_at, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (lease_id, task_id, agent_id, expires_at, LeaseStatus.ACTIVE.value, now, now),
            )
            conn.execute(
                """
                UPDATE agents
                SET status = ?, current_task_id = ?, updated_at = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (AgentStatus.BUSY.value, task_id, now, now, agent_id),
            )
            detail = {"lease_id": lease_id, "expires_at": expires_at}
            self._record_history(
                task_id,
                "task.claimed",
                agent_id,
                task.state,
                TaskState.CLAIMED.value,
                detail,
                conn=conn,
            )
        claimed_task = self.get_task(task_id)
        if sync_beads:
            self.drain_task_transition_outbox(task_id=task_id, limit=20)
        return claimed_task, self.get_lease(lease_id)


    def list_task_transition_outbox(
        self,
        *,
        status: str = "pending",
        task_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[TaskTransitionOutbox]:
        return self.task_ledger.list_outbox(status=status, task_id=task_id, limit=limit)

    def drain_task_transition_outbox(
        self,
        *,
        task_id: Optional[str] = None,
        limit: int = 100,
    ) -> JsonDict:
        processed = []
        for item in self.task_ledger.list_outbox(task_id=task_id, limit=limit):
            try:
                self._process_task_transition_outbox_item(item)
            except Exception as exc:  # noqa: BLE001 - one failed side effect must not block later rows.
                self.task_ledger.mark_outbox_failed(item.id, str(exc))
                self.record_log(
                    "task.transition_outbox.failed",
                    layer="control_plane",
                    source="task-ledger",
                    level="warning",
                    subject_type="task",
                    subject_id=item.task_id,
                    detail={"outbox_id": item.id, "event_type": item.event_type, "error": str(exc)},
                )
                processed.append({"id": item.id, "event_type": item.event_type, "status": "failed"})
                continue
            self.task_ledger.mark_outbox_processed(item.id)
            processed.append({"id": item.id, "event_type": item.event_type, "status": "delivered"})
        return {"processed": processed, "count": len(processed)}

    def drain_task_transition_outbox_best_effort(
        self,
        *,
        task_id: Optional[str] = None,
        limit: int = 100,
    ) -> JsonDict:
        if not self._task_outbox_drain_lock.acquire(blocking=False):
            return {"processed": [], "count": 0, "status": "busy"}
        try:
            result = self.drain_task_transition_outbox(task_id=task_id, limit=limit)
            # Success resets the failure streak so the health signal reflects
            # only *ongoing* trouble.
            self._task_outbox_drain_failures = 0
            return result
        except Exception as exc:  # noqa: BLE001 - side effects must not break API responses.
            # Track failures in an in-memory counter that CANNOT itself fail:
            # the previous code logged-and-swallowed, then wrapped the log in a
            # bare `except: pass`, so a persistently failing outbox (stranded
            # task transitions) could be entirely invisible if logging also
            # failed and the caller ignored the return. The counter guarantees
            # the failure is observable via status(), and severity escalates
            # once failures persist.
            self._task_outbox_drain_failures = (
                getattr(self, "_task_outbox_drain_failures", 0) + 1
            )
            failures = self._task_outbox_drain_failures
            try:
                self.record_log(
                    "task.transition_outbox.drain_failed",
                    layer="control_plane",
                    source="task-ledger",
                    # A one-off drain miss is a warning; a sustained failure
                    # streak means transitions are stranding — escalate.
                    level="error" if failures >= 3 else "warning",
                    subject_type="task" if task_id else None,
                    subject_id=task_id,
                    detail={
                        "error": str(exc),
                        "limit": limit,
                        "consecutive_failures": failures,
                    },
                )
            except Exception:  # noqa: BLE001 - telemetry may be down; counter still holds it.
                pass
            return {
                "processed": [],
                "count": 0,
                "status": "failed",
                "error": str(exc),
                "consecutive_failures": failures,
            }
        finally:
            self._task_outbox_drain_lock.release()

    def _process_task_transition_outbox_item(self, item: TaskTransitionOutbox) -> None:
        if item.event_type == "task.lifecycle":
            task = self.get_task(item.task_id)
            metadata = ensure_json_object(task.metadata)
            lifecycle = ensure_json_object(metadata.get("repository_ref_lifecycle"))
            if lifecycle:
                self.record_log(
                    "repository.ref.lifecycle",
                    layer="control_plane",
                    source="task-ledger",
                    level="info",
                    subject_type="task",
                    subject_id=item.task_id,
                    detail={
                        "task_state": task.state,
                        "disposition": lifecycle.get("disposition"),
                        "status": lifecycle.get("status"),
                        "eligible_after": lifecycle.get("eligible_after"),
                        "replacement_task_id": lifecycle.get("replacement_task_id"),
                    },
                )
            return
        task = self.get_task(item.task_id)
        if item.event_type == "workflow.advance":
            # Workflow-runtime hook. The link is the `tasks.workflow_run_id`
            # column (never caller metadata), so forged task metadata cannot
            # push a free-floating task into the workflow state machine.
            if item.to_state in TERMINAL_TASK_STATES.union({TaskState.BLOCKED.value}):
                self.workflow_runtime.on_task_completed(item.task_id, item.to_state or "")
            return
        raise ValidationError("unsupported task transition outbox event: %s" % item.event_type)

    def start_task(self, task_id: str, agent_id: str, *, drain_outbox: bool = True) -> Task:
        task = self.get_task(task_id)
        # PR2c (spec §6.3): accept either the lease owner OR a delegated
        # actor (recorded via delegate_lease). Renewal / release stay
        # strictly owner-only and are unchanged.
        if not self._lease_actor_allowed(task, agent_id):
            raise AuthorizationError("agent does not own task lease")
        # A loop worker may restart after it moved an assignment to RUNNING but
        # before it recorded evidence.  Starting the same, still-active lease
        # is idempotent so the worker can resume execution instead of stranding
        # the dispatcher-owned assignment on an invalid RUNNING -> RUNNING
        # transition.
        if task.state == TaskState.RUNNING.value:
            return task
        return self.transition_task(
            task_id,
            TaskState.RUNNING.value,
            agent_id,
            {},
            drain_outbox=drain_outbox,
        )

    def submit_for_review(
        self,
        task_id: str,
        agent_id: str,
        *,
        drain_outbox: bool = True,
    ) -> Task:
        task = self.get_task(task_id)
        # PR2c (spec §6.3): accept either the lease owner OR a delegated
        # actor (recorded via delegate_lease).
        if not self._lease_actor_allowed(task, agent_id):
            raise AuthorizationError("agent does not own task lease")
        reviewed = self.transition_task(
            task_id,
            TaskState.NEEDS_REVIEW.value,
            agent_id,
            {},
            drain_outbox=drain_outbox,
        )
        return reviewed

    def _require_review_ready(self, task: Task) -> Evidence:
        evidence, assessment = self._default_review_evidence(task)
        if evidence is None:
            problems: List[str] = []
            for rejected in assessment.get("rejected_evidence", []) or []:
                if isinstance(rejected, dict):
                    problems.extend(str(item) for item in rejected.get("problems", []) or [])
            if not problems:
                problems = [str(assessment.get("reason") or "no verifiable evidence")]
            raise ValidationError(
                "task needs verifiable evidence before review: %s"
                % "; ".join(problems[:8])
            )
        return evidence

    def add_evidence(
        self,
        task_id: str,
        kind: str,
        uri: str,
        summary: str,
        created_by: str,
        checksum: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        artifacts: Optional[List[Dict[str, Any]]] = None,
        sync_beads: bool = True,
    ) -> Evidence:
        task = self.get_task(task_id)
        if task.state in {TaskState.CLAIMED.value, TaskState.RUNNING.value}:
            if not self._lease_actor_allowed(task, created_by):
                raise AuthorizationError("agent does not own task lease")
        if not kind or not uri or not summary:
            raise ValidationError("evidence requires kind, uri, and summary")
        if kind not in EVIDENCE_KINDS:
            raise ValidationError("unsupported evidence kind: %s" % kind)
        if kind == "publication" and not checksum:
            raise ValidationError("publication evidence requires a checksum")
        # mem-11: reject `operator_result` evidence_type when the task's
        # execution contract declared a repository contract (or set
        # repository_required=true). The original `task_d7c51a0b`
        # incident hinged on bullwinkle emitting operator_result evidence
        # for a code task; the validator accepted it because
        # OperatorResultValidator only requires *any* summary string,
        # which then sent the review loop hunting a remote_ref that was
        # never pushed.
        self._enforce_repo_coupled_evidence_type(task, metadata)
        now = utcnow()
        evidence_id = new_id("ev")
        stored_artifacts = self._prepare_evidence_artifacts(
            evidence_id,
            task_id,
            artifacts or [],
            now,
        )
        metadata_obj = dict(ensure_json_object(metadata))
        metadata_obj.pop("durable_artifacts", None)
        if stored_artifacts:
            metadata_obj["durable_artifacts"] = {
                "schema": "mac.evidence_artifacts.v1",
                "count": len(stored_artifacts),
                "artifacts": [
                    self._evidence_artifact_public_dict(item, include_content=False)
                    for item in stored_artifacts
                ],
            }
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO evidence (id, task_id, kind, uri, summary, checksum, metadata, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    task_id,
                    kind,
                    uri,
                    summary,
                    checksum,
                    json_dumps(metadata_obj),
                    created_by,
                    now,
                ),
            )
            for artifact in stored_artifacts:
                conn.execute(
                    """
                    INSERT INTO evidence_artifacts (
                        id, evidence_id, task_id, name, artifact_type, source_uri,
                        content_type, encoding, size_bytes, sha256, content_base64,
                        content_uri, truncated, metadata, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.id,
                        artifact.evidence_id,
                        artifact.task_id,
                        artifact.name,
                        artifact.artifact_type,
                        artifact.source_uri,
                        artifact.content_type,
                        artifact.encoding,
                        artifact.size_bytes,
                        artifact.sha256,
                        artifact.content_base64 or "",
                        artifact.content_uri or "",
                        1 if artifact.truncated else 0,
                        json_dumps(artifact.metadata),
                        artifact.created_at,
                    ),
                )
            self._record_history(
                task_id,
                "task.evidence_added",
                created_by,
                None,
                None,
                {
                    "evidence_id": evidence_id,
                    "kind": kind,
                    "uri": uri,
                    "artifact_count": len(stored_artifacts),
                },
                conn=conn,
            )
        evidence = self.get_evidence(evidence_id)
        self._capture_runtime_delta_from_evidence(evidence, task)
        return evidence

    def _prepare_evidence_artifacts(
        self,
        evidence_id: str,
        task_id: str,
        artifacts: List[Dict[str, Any]],
        created_at: str,
    ) -> List[EvidenceArtifact]:
        if not artifacts:
            return []
        if len(artifacts) > MAX_EVIDENCE_ARTIFACTS:
            raise ValidationError(
                "evidence accepts at most %d durable artifacts" % MAX_EVIDENCE_ARTIFACTS
            )
        max_bytes = _evidence_artifact_max_bytes()
        total_max_bytes = _evidence_artifact_total_max_bytes()
        total_bytes = 0
        prepared: List[EvidenceArtifact] = []
        for index, item in enumerate(artifacts):
            if not isinstance(item, dict):
                raise ValidationError("evidence artifact %d must be an object" % index)
            encoding = str(item.get("encoding") or "base64").strip().lower()
            if encoding != "base64":
                raise ValidationError("evidence artifact %d uses unsupported encoding" % index)
            if "content_base64" not in item:
                raise ValidationError("evidence artifact %d requires content_base64" % index)
            raw_b64 = str(item.get("content_base64") or "").strip()
            try:
                content = base64.b64decode(raw_b64.encode("ascii"), validate=True)
            except Exception as exc:  # noqa: BLE001
                raise ValidationError("evidence artifact %d has invalid base64: %s" % (index, exc))
            if len(content) > max_bytes:
                raise ValidationError(
                    "evidence artifact %d exceeds %d bytes"
                    % (index, max_bytes)
                )
            if total_bytes + len(content) > total_max_bytes:
                raise ValidationError(
                    "evidence artifacts exceed aggregate limit of %d bytes"
                    % total_max_bytes
                )
            total_bytes += len(content)
            declared_size = item.get("size_bytes")
            if declared_size not in {None, ""}:
                try:
                    declared_size_int = int(declared_size)
                except (TypeError, ValueError):
                    raise ValidationError("evidence artifact %d size_bytes is invalid" % index)
                if declared_size_int != len(content):
                    raise ValidationError(
                        "evidence artifact %d size_bytes does not match content" % index
                    )
            digest = "sha256:%s" % hashlib.sha256(content).hexdigest()
            declared_sha = str(item.get("sha256") or "").strip()
            if declared_sha and declared_sha != digest:
                raise ValidationError("evidence artifact %d sha256 does not match content" % index)
            # Externalize bytes to the hub blob store when configured: the
            # ledger row keeps digest/size/metadata + a content_uri, so ledger
            # DB growth decouples from artifact volume. Small payloads stay
            # inline (cheaper next to their metadata); failures fall back to
            # inline so evidence capture never depends on the blob volume.
            content_b64 = base64.b64encode(content).decode("ascii")
            content_uri = ""
            blob_root = evidence_blobs.blob_root()
            if blob_root is not None and len(content) > evidence_blobs.inline_max_bytes():
                try:
                    content_uri = evidence_blobs.store_blob(blob_root, content)
                    content_b64 = ""
                except OSError:
                    content_uri = ""
            prepared.append(
                EvidenceArtifact(
                    id=new_id("eva"),
                    evidence_id=evidence_id,
                    task_id=task_id,
                    name=self._normalize_evidence_artifact_name(item.get("name"), index),
                    artifact_type=(
                        str(item.get("artifact_type") or item.get("kind") or "artifact").strip()[:64]
                        or "artifact"
                    ),
                    source_uri=str(item.get("source_uri") or item.get("uri") or "").strip()[:2048],
                    content_type=(
                        str(item.get("content_type") or "application/octet-stream").strip()[:128]
                        or "application/octet-stream"
                    ),
                    encoding="base64",
                    size_bytes=len(content),
                    sha256=digest,
                    content_base64=content_b64,
                    truncated=_evidence_artifact_bool(item.get("truncated")),
                    metadata=ensure_json_object(item.get("metadata")),
                    created_at=created_at,
                    content_uri=content_uri,
                )
            )
        return prepared

    def _normalize_evidence_artifact_name(self, value: Any, index: int) -> str:
        name = str(value or "").strip().replace("\\", "/")
        name = name.rsplit("/", 1)[-1].strip()
        if not name or name in {".", ".."}:
            name = "artifact-%02d" % (index + 1)
        return name[:160]

    def _evidence_artifact_public_dict(
        self,
        artifact: EvidenceArtifact,
        *,
        include_content: bool,
    ) -> JsonDict:
        data = artifact.to_dict()
        if not include_content:
            data.pop("content_base64", None)
            data.pop("metadata", None)
        return data

    def list_evidence_artifacts(self, evidence_id: str) -> List[JsonDict]:
        self.get_evidence(evidence_id)
        rows = self.store.query_all(
            """
            SELECT * FROM evidence_artifacts
            WHERE evidence_id = ?
            ORDER BY created_at, id
            """,
            (evidence_id,),
        )
        return [
            self._evidence_artifact_public_dict(
                self._evidence_artifact_from_row(row),
                include_content=False,
            )
            for row in rows
        ]

    def get_evidence_artifact(self, evidence_id: str, artifact_id: str) -> JsonDict:
        row = self.store.query_one(
            """
            SELECT * FROM evidence_artifacts
            WHERE evidence_id = ? AND id = ?
            """,
            (evidence_id, artifact_id),
        )
        if row is None:
            raise NotFoundError("evidence artifact not found: %s" % artifact_id)
        artifact = self._evidence_artifact_from_row(row)
        # Externalized bytes: materialize content from the blob store so the
        # response shape is identical to an inline row. Verified read — a
        # missing or corrupted blob fails closed rather than returning wrong
        # bytes under a valid-looking digest.
        if not artifact.content_base64 and artifact.content_uri:
            root = evidence_blobs.blob_root()
            if root is None:
                raise NotFoundError(
                    "evidence artifact %s content is externalized but no blob store "
                    "is configured (set %s)" % (artifact_id, evidence_blobs.BLOB_DIR_ENV)
                )
            try:
                content = evidence_blobs.read_blob(
                    root, artifact.content_uri, expected_sha256=artifact.sha256
                )
            except FileNotFoundError:
                raise NotFoundError(
                    "evidence artifact %s blob is missing from the blob store" % artifact_id
                )
            except evidence_blobs.BlobIntegrityError as exc:
                raise ValidationError(str(exc))
            artifact.content_base64 = base64.b64encode(content).decode("ascii")
        return self._evidence_artifact_public_dict(
            artifact,
            include_content=True,
        )

    def _capture_runtime_delta_from_evidence(
        self,
        evidence: Evidence,
        task: Task,
    ) -> None:
        metadata = evidence.metadata if isinstance(evidence.metadata, dict) else {}
        verification = metadata.get("verification")
        delta = None
        if isinstance(verification, dict) and isinstance(verification.get("environment_delta"), dict):
            delta = verification.get("environment_delta")
        elif isinstance(metadata.get("environment_delta"), dict):
            delta = metadata.get("environment_delta")
        if not isinstance(delta, dict):
            return
        runtime_meta = task.metadata.get("runtime") if isinstance(task.metadata, dict) else {}
        if not isinstance(runtime_meta, dict):
            runtime_meta = {}
        try:
            self.propose_runtime_delta(
                task.id,
                evidence.created_by,
                str(delta.get("package_manager") or ""),
                delta.get("commands") if isinstance(delta.get("commands"), list) else [],
                (
                    delta.get("added_dependencies")
                    if isinstance(delta.get("added_dependencies"), list)
                    else delta.get("dependencies")
                    if isinstance(delta.get("dependencies"), list)
                    else []
                ),
                str(delta.get("reason") or "worker proposed task-local dependency delta"),
                project=str(delta.get("project") or task.project or "").strip() or None,
                base_runtime_id=(
                    str(delta.get("base_runtime_id") or runtime_meta.get("runtime_environment_id") or "").strip()
                    or None
                ),
                base_runtime_digest=(
                    str(delta.get("base_runtime_digest") or runtime_meta.get("runtime_digest") or "").strip()
                    or None
                ),
                lockfile_path=str(delta.get("lockfile_path") or "").strip() or None,
                lockfile_digest=str(delta.get("lockfile_digest") or "").strip() or None,
                evidence_id=evidence.id,
            )
        except Exception as exc:  # noqa: BLE001 - evidence is already durable.
            try:
                self.record_log(
                    "runtime_delta.capture_failed",
                    layer="control_plane",
                    source=evidence.created_by,
                    level="warning",
                    subject_type="task",
                    subject_id=task.id,
                    detail={
                        "evidence_id": evidence.id,
                        "error": str(exc),
                    },
                )
            except Exception:
                pass

    def _enforce_repo_coupled_evidence_type(
        self,
        task: Task,
        metadata: Optional[Dict[str, Any]],
    ) -> None:
        """mem-11: refuse to accept operator_result evidence for a task
        that the dispatcher staged with a repository contract.

        The verification block inside metadata carries the proposed
        evidence_type. Tasks whose execution_contract.type='repository'
        or whose execution_contract.repository_required is True are
        "repo-coupled" — they must use one of the strict evidence types
        (repo_change / documentation / test / artifact / no_change /
        review_verdict) that exercises `require_pushed_repo_anchor()`
        downstream. The soft `operator_result` type bypasses that
        anchor check and was the bug that produced the runaway review
        loop on `task_d7c51a0b04bd...`.
        """
        if not isinstance(metadata, dict):
            return
        # An operator-declared report/answer task is expected to produce
        # operator_result — that is the whole point of the declaration, so do
        # not apply the repo-coupled bar to it. (The bar still protects tasks
        # that carry a repository contract without such a declaration.)
        if metadata_declares_report_deliverable(task.metadata):
            return
        verification = metadata.get("verification")
        if not isinstance(verification, dict):
            return
        evidence_type = str(verification.get("evidence_type") or "").strip().lower()
        if evidence_type != "operator_result":
            return
        contract = (
            task.metadata.get("execution_contract")
            if isinstance(task.metadata, dict)
            else None
        )
        if not isinstance(contract, dict):
            return
        is_repo_coupled = (
            str(contract.get("type") or "").strip().lower() == "repository"
            or contract.get("repository_required") is True
        )
        if not is_repo_coupled:
            return
        raise ValidationError(
            "operator_result evidence cannot be recorded for a repo-coupled "
            "task (execution_contract.type=repository or "
            "repository_required=true); use repo_change / test / documentation "
            "/ artifact / no_change / review_verdict instead. Task: %s"
            % task.id
        )


    def get_evidence(self, evidence_id: str) -> Evidence:
        row = self.store.query_one("SELECT * FROM evidence WHERE id = ?", (evidence_id,))
        if row is None:
            raise NotFoundError("evidence not found: %s" % evidence_id)
        return self._evidence_from_row(row)

    def list_evidence(
        self,
        task_id: str,
        limit: Optional[int] = None,
    ) -> List[Evidence]:
        limit_value = None if limit is None else max(0, int(limit))
        if limit_value == 0:
            return []
        if limit_value is None:
            rows = self.store.query_all(
                "SELECT * FROM evidence WHERE task_id = ? ORDER BY created_at, id",
                (task_id,),
            )
        else:
            rows = list(
                reversed(
                    self.store.query_all(
                        """
                        SELECT * FROM evidence
                        WHERE task_id = ?
                        ORDER BY created_at DESC, id DESC
                        LIMIT ?
                        """,
                        (task_id, limit_value),
                    )
                )
            )
        return [self._evidence_from_row(row) for row in rows]

    def renew_lease(self, lease_id: str, agent_id: str, lease_seconds: int = 900) -> Lease:
        lease = self.get_lease(lease_id)
        if lease.agent_id != agent_id:
            raise AuthorizationError("agent does not own lease")
        if lease.status != LeaseStatus.ACTIVE.value:
            raise ValidationError("only active leases can be renewed")
        now = utcnow()
        expires_at = (parse_time(now) + timedelta(seconds=int(lease_seconds))).isoformat(timespec="microseconds")
        with self.store.transaction() as conn:
            lease_cursor = conn.execute(
                """
                UPDATE leases
                SET expires_at = ?, updated_at = ?
                WHERE id = ? AND agent_id = ? AND status = ?
                """,
                (expires_at, now, lease_id, agent_id, LeaseStatus.ACTIVE.value),
            )
            if lease_cursor.rowcount != 1:
                raise ValidationError("only active leases can be renewed")
            task_cursor = conn.execute(
                """
                UPDATE tasks
                SET leased_until = ?, updated_at = ?
                WHERE id = ?
                  AND lease_id = ?
                  AND owner_agent_id = ?
                  AND state IN (?, ?)
                """,
                (
                    expires_at,
                    now,
                    lease.task_id,
                    lease_id,
                    agent_id,
                    TaskState.CLAIMED.value,
                    TaskState.RUNNING.value,
                ),
            )
            if task_cursor.rowcount != 1:
                raise ValidationError("lease is no longer attached to an active task")
            conn.execute(
                """
                UPDATE agents
                SET status = ?, current_task_id = ?, updated_at = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (AgentStatus.BUSY.value, lease.task_id, now, now, agent_id),
            )
        self._record_history(lease.task_id, "task.lease_renewed", agent_id, None, None, {"lease_id": lease_id})
        heartbeat_agent = self.get_agent(agent_id)
        self._maybe_advance_reviews_on_heartbeat(heartbeat_agent)
        self._maybe_drain_notifications_on_heartbeat(heartbeat_agent)
        return self.get_lease(lease_id)

    def get_lease(self, lease_id: str) -> Lease:
        row = self.store.query_one("SELECT * FROM leases WHERE id = ?", (lease_id,))
        if row is None:
            raise NotFoundError("lease not found: %s" % lease_id)
        return self._lease_from_row(row)

    def delegate_lease(
        self,
        lease_id: str,
        owner_agent_id: str,
        to_agent_id: str,
    ) -> Lease:
        """Delegate lifecycle authorship on a lease's task to another agent.

        Per spec §6.3 (Option B), the dispatcher (``mac-runner``) holds
        the lease but the role-specialised Job pod authors lifecycle
        transitions and evidence. This call records the delegation so
        ``start_task`` / ``submit_for_review`` / ``add_evidence`` accept
        the delegate as a legitimate actor.

        Renewal and release stay strictly owner-only — see the spec.
        """
        lease = self.get_lease(lease_id)
        if lease.agent_id != owner_agent_id:
            raise AuthorizationError("only the lease owner can delegate")
        # Verify the target agent exists; this also implicitly enforces
        # the agents-table FK that the schema declares so SQLite (no FK
        # enforcement by default) matches Postgres semantics.
        self.get_agent(to_agent_id)
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE leases
                SET delegated_agent_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (to_agent_id, now, lease_id),
            )
            self._record_history(
                lease.task_id,
                "task.lease_delegated",
                owner_agent_id,
                None,
                None,
                {"lease_id": lease_id, "delegated_agent_id": to_agent_id},
                conn=conn,
            )
        return self.get_lease(lease_id)

    def _lease_actor_allowed(self, task: Task, agent_id: str) -> bool:
        """Return True if ``agent_id`` may author lifecycle transitions
        on ``task`` per spec §6.3 (Option B).

        Allowed actors:
          * the task's current owner (``task.owner_agent_id``); or
          * the agent recorded in the task's active lease's
            ``delegated_agent_id`` (set via :meth:`delegate_lease`).
        """
        if task.owner_agent_id and task.owner_agent_id == agent_id:
            return True
        if not task.lease_id:
            return False
        try:
            lease = self.get_lease(task.lease_id)
        except NotFoundError:
            return False
        if lease.status != LeaseStatus.ACTIVE.value:
            return False
        return bool(lease.delegated_agent_id) and lease.delegated_agent_id == agent_id

    def expire_leases(
        self,
        now: Optional[str] = None,
        *,
        grace_seconds: Optional[int] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> List[Task]:
        return self._expire_leases_page(
            now=now,
            grace_seconds=grace_seconds,
            limit=limit,
            cursor=cursor,
        )["tasks"]

    def _expire_leases_page(
        self,
        now: Optional[str] = None,
        *,
        grace_seconds: Optional[int] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> JsonDict:
        # mac-vgw9: when `now` is auto-derived, subtract a small tolerance
        # so an NTP step forward doesn't mass-expire every lease. When
        # the caller passes `now` explicitly, honor it exactly (callers
        # use this for deterministic tests or for manually advancing the
        # clock). Operators can also pass an explicit ``grace_seconds``.
        if now is not None:
            grace = grace_seconds or 0
            cutoff_dt = parse_time(now) - timedelta(seconds=int(grace))
        else:
            grace = 30 if grace_seconds is None else int(grace_seconds)
            cutoff_dt = parse_time(utcnow()) - timedelta(seconds=grace)
        cutoff = cutoff_dt.isoformat(timespec="microseconds")
        limit_value = max(1, min(int(limit), 1000))
        clauses = ["status = ?", "expires_at <= ?"]
        params: List[Any] = [LeaseStatus.ACTIVE.value, cutoff]
        decoded = self._decode_scan_cursor(cursor, "expired-leases")
        if decoded is not None:
            expires_at, lease_id = decoded
            clauses.append("(expires_at > ? OR (expires_at = ? AND id > ?))")
            params.extend([expires_at, expires_at, lease_id])
        params.append(limit_value + 1)
        rows = self.store.query_all(
            "SELECT * FROM leases WHERE %s "
            "ORDER BY expires_at, id LIMIT ?" % " AND ".join(clauses),
            tuple(params),
        )
        has_more = len(rows) > limit_value
        rows = rows[:limit_value]
        recovered: List[Task] = []
        for row in rows:
            try:
                task = self._expire_lease_row(row)
            except Exception as exc:  # noqa: BLE001 - isolate corrupt lease rows.
                try:
                    self.record_log(
                        "lease.recovery.failed",
                        layer="control_plane",
                        source="dispatcher",
                        level="error",
                        subject_type="lease",
                        subject_id=str(row["id"]),
                        detail={"lease_id": str(row["id"]), "error": str(exc)},
                    )
                except Exception:
                    pass
                continue
            if task is not None:
                recovered.append(task)
        next_cursor = (
            self._encode_scan_cursor(
                "expired-leases",
                str(rows[-1]["expires_at"]),
                str(rows[-1]["id"]),
            )
            if has_more and rows
            else None
        )
        return {
            "tasks": recovered,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    def _expire_lease_row(self, row: Any) -> Optional[Task]:
        lease = self._lease_from_row(row)
        task = self.get_task(lease.task_id)
        next_state = (
            TaskState.FAILED.value
            if task.attempt_count >= task.max_attempts
            else TaskState.OPEN.value
        )
        timestamp = utcnow()
        with self.store.transaction() as conn:
            # Guard both updates so another replica renewing/reclaiming the
            # lease wins cleanly instead of having its task state overwritten.
            cur = conn.execute(
                "UPDATE leases SET status = ?, updated_at = ? "
                "WHERE id = ? AND status = ?",
                (
                    LeaseStatus.EXPIRED.value,
                    timestamp,
                    lease.id,
                    LeaseStatus.ACTIVE.value,
                ),
            )
            if cur.rowcount == 0:
                return None
            cur = conn.execute(
                """
                UPDATE tasks
                SET state = ?, owner_agent_id = NULL, lease_id = NULL,
                    leased_until = NULL,
                    completed_at = CASE
                        WHEN ? = ? AND completed_at IS NULL THEN ?
                        ELSE completed_at
                    END,
                    updated_at = ?
                WHERE id = ? AND lease_id = ?
                """,
                (
                    next_state,
                    next_state,
                    TaskState.FAILED.value,
                    timestamp,
                    timestamp,
                    task.id,
                    lease.id,
                ),
            )
            if cur.rowcount == 0:
                return None
            conn.execute(
                """
                UPDATE agents
                SET status = ?, current_task_id = NULL, updated_at = ?
                WHERE id = ? AND current_task_id = ?
                """,
                (AgentStatus.IDLE.value, timestamp, lease.agent_id, task.id),
            )
            self._record_history(
                task.id,
                "task.lease_expired",
                "dispatcher",
                task.state,
                next_state,
                {"lease_id": lease.id, "agent_id": lease.agent_id},
                conn=conn,
            )
        recovered = self.get_task(task.id)
        self.drain_task_transition_outbox(task_id=task.id, limit=20)
        return recovered

    def _expire_leases_sweep_page(self, *, limit: int) -> JsonDict:
        claim = self.reconciliation.claim("expired-lease-sweep")
        if claim is None:
            return {
                "tasks": [],
                "next_cursor": None,
                "has_more": False,
                "skipped": "lease_held",
            }
        try:
            result = self._expire_leases_page(limit=limit, cursor=claim.cursor)
        except Exception:
            self.reconciliation.abandon(claim)
            raise
        self.reconciliation.complete(claim, cursor=result.get("next_cursor"))
        return result

    def release_lease(self, lease_id: str, agent_id: str) -> Task:
        lease = self.get_lease(lease_id)
        if lease.agent_id != agent_id:
            raise AuthorizationError("agent does not own lease")
        task = self.get_task(lease.task_id)
        now = utcnow()
        with self.store.transaction() as conn:
            # mac-79s1: guard the lease UPDATE on status='active'. If
            # the lease was already expired/released by the hub or another
            # path, this UPDATE affects 0 rows and we must NOT proceed to
            # clobber the task row — by then it may have a new owner.
            cur = conn.execute(
                "UPDATE leases SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (LeaseStatus.RELEASED.value, now, lease_id, LeaseStatus.ACTIVE.value),
            )
            if cur.rowcount == 0:
                raise TransitionError(
                    "lease %s is no longer active (already released or expired)" % lease_id
                )
            # mac-79s1: guard the task UPDATE on the lease still pointing at
            # this lease and being owned by this agent. If a new owner has
            # taken over, the row count is 0 and we refuse.
            cur = conn.execute(
                """
                UPDATE tasks
                SET state = ?, owner_agent_id = NULL, lease_id = NULL, leased_until = NULL, updated_at = ?
                WHERE id = ? AND lease_id = ? AND owner_agent_id = ?
                """,
                (TaskState.OPEN.value, now, task.id, lease_id, agent_id),
            )
            if cur.rowcount == 0:
                raise TransitionError(
                    "task %s has been reclaimed by another owner; refusing to release" % task.id
                )
            conn.execute(
                "UPDATE agents SET status = ?, current_task_id = NULL, updated_at = ? WHERE id = ?",
                (AgentStatus.IDLE.value, now, agent_id),
            )
            detail = {"lease_id": lease_id}
            self._record_history(
                task.id,
                "task.lease_released",
                agent_id,
                task.state,
                TaskState.OPEN.value,
                detail,
                conn=conn,
            )
        self.drain_task_transition_outbox(task_id=task.id, limit=20)
        return self.get_task(task.id)

    # Fleet registry

    def register_machine(
        self,
        hostname: str,
        labels: Optional[Dict[str, Any]] = None,
        resources: Optional[Dict[str, Any]] = None,
        trusted: bool = True,
        machine_id: Optional[str] = None,
        hardware: Optional[Dict[str, Any]] = None,
    ) -> Machine:
        if not hostname:
            raise ValidationError("hostname is required")
        now = utcnow()
        mid = machine_id or new_id("machine")
        labels_json = self._resolved_json_column("machines", "labels", mid, labels)
        resources_json = self._resolved_json_column("machines", "resources", mid, resources)
        hardware_json = self._resolved_json_column("machines", "hardware", mid, hardware)
        self.store.execute(
            """
            INSERT INTO machines (id, hostname, labels, resources, trusted, created_at, updated_at, last_seen_at, hardware)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                hostname = excluded.hostname,
                labels = excluded.labels,
                resources = excluded.resources,
                trusted = excluded.trusted,
                updated_at = excluded.updated_at,
                last_seen_at = excluded.last_seen_at,
                hardware = excluded.hardware
            """,
            (
                mid,
                hostname,
                labels_json,
                resources_json,
                1 if trusted else 0,
                now,
                now,
                now,
                hardware_json,
            ),
        )
        return self.get_machine(mid)

    def get_machine(self, machine_id: str) -> Machine:
        row = self.store.query_one("SELECT * FROM machines WHERE id = ?", (machine_id,))
        if row is None:
            raise NotFoundError("machine not found: %s" % machine_id)
        return self._machine_from_row(row)

    def list_machines(self) -> List[Machine]:
        return [self._machine_from_row(row) for row in self.store.query_all("SELECT * FROM machines ORDER BY hostname")]

    # Fleets are user-facing collections of agents. They intentionally do not
    # own machines or tasks; those remain independent first-class objects.

    def create_fleet(
        self,
        name: str,
        description: str = "",
        *,
        status: str = "active",
        metadata: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
        agent_ids: Optional[Iterable[str]] = None,
        fleet_id: Optional[str] = None,
        actor: str = "human",
    ) -> Fleet:
        fleet_name = str(name or "").strip()
        if not fleet_name:
            raise ValidationError("fleet name is required")
        if tenant_id:
            self.get_tenant(tenant_id)
        normalized_status = str(status or "active").strip().lower()
        if normalized_status not in {"active", "inactive", "retired"}:
            raise ValidationError("unsupported fleet status: %s" % normalized_status)
        members = self._validated_fleet_agent_ids(agent_ids or [])
        now = utcnow()
        metadata_value = ensure_json_object(metadata)
        if actor:
            metadata_value.setdefault("created_by", actor)
        fid = fleet_id or new_id("fleet")
        with self.store.transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO fleets (
                        id, name, description, status, metadata, tenant_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fid,
                        fleet_name,
                        str(description or ""),
                        normalized_status,
                        json_dumps(metadata_value),
                        tenant_id,
                        now,
                        now,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - normalize sqlite uniqueness errors.
                if "UNIQUE" in str(exc).upper():
                    raise ValidationError("fleet already exists: %s" % fleet_name) from exc
                raise
            self._replace_fleet_members(conn, fid, members, now)
            self._record_fleet_event(
                conn,
                fid,
                "fleet.created",
                actor,
                {
                    "fleet_id": fid,
                    "fleet_name": fleet_name,
                    "status": normalized_status,
                    "tenant_id": tenant_id,
                    "agent_ids": members,
                    "agent_count": len(members),
                    "metadata_keys": sorted(metadata_value.keys()),
                },
                now,
            )
        self.record_notification(
            "fleet.created",
            "Fleet created: %s" % fleet_name,
            str(description or "Fleet %s was created." % fleet_name),
            subject_type="fleet",
            subject_id=fid,
            channels=["dashboard", "hermes"],
            metadata={"fleet": fleet_name, "actor": actor},
        )
        return self.get_fleet(fid)

    def get_fleet(self, fleet_id_or_name: str) -> Fleet:
        row = self.store.query_one(
            "SELECT * FROM fleets WHERE id = ? OR name = ?",
            (fleet_id_or_name, fleet_id_or_name),
        )
        if row is None:
            raise NotFoundError("fleet not found: %s" % fleet_id_or_name)
        return self._fleet_from_row(row)

    def list_fleets(
        self,
        *,
        status: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Fleet]:
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(str(status).strip().lower())
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.store.query_all(
            "SELECT * FROM fleets%s ORDER BY name, id" % where,
            tuple(params),
        )
        return [self._fleet_from_row(row) for row in rows]

    def update_fleet(
        self,
        fleet_id_or_name: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
        agent_ids: Optional[Iterable[str]] = None,
        actor: str = "human",
    ) -> Fleet:
        fleet = self.get_fleet(fleet_id_or_name)
        updates: List[str] = []
        params: List[Any] = []
        changed_fields: List[str] = []
        new_name = fleet.name
        new_status = fleet.status
        new_tenant_id = fleet.tenant_id
        if name is not None:
            name_value = str(name or "").strip()
            if not name_value:
                raise ValidationError("fleet name is required")
            updates.append("name = ?")
            params.append(name_value)
            new_name = name_value
            if name_value != fleet.name:
                changed_fields.append("name")
        if description is not None:
            updates.append("description = ?")
            params.append(str(description or ""))
            if str(description or "") != fleet.description:
                changed_fields.append("description")
        if status is not None:
            status_value = str(status or "").strip().lower()
            if status_value not in {"active", "inactive", "retired"}:
                raise ValidationError("unsupported fleet status: %s" % status_value)
            updates.append("status = ?")
            params.append(status_value)
            new_status = status_value
            if status_value != fleet.status:
                changed_fields.append("status")
        if metadata is not None:
            updates.append("metadata = ?")
            params.append(json_dumps(ensure_json_object(metadata)))
            changed_fields.append("metadata")
        if tenant_id is not None:
            tenant_value = tenant_id.strip()
            if tenant_value:
                self.get_tenant(tenant_value)
                updates.append("tenant_id = ?")
                params.append(tenant_value)
                new_tenant_id = tenant_value
            else:
                updates.append("tenant_id = NULL")
                new_tenant_id = None
            if new_tenant_id != fleet.tenant_id:
                changed_fields.append("tenant_id")
        members = None
        if agent_ids is not None:
            members = self._validated_fleet_agent_ids(agent_ids)
            if members != fleet.agent_ids:
                changed_fields.append("agent_ids")
        if not updates and members is None:
            return fleet
        now = utcnow()
        with self.store.transaction() as conn:
            if updates:
                updates.append("updated_at = ?")
                params.append(now)
                params.append(fleet.id)
                try:
                    conn.execute(
                        "UPDATE fleets SET %s WHERE id = ?" % ", ".join(updates),
                        tuple(params),
                    )
                except Exception as exc:  # noqa: BLE001 - normalize sqlite uniqueness errors.
                    if "UNIQUE" in str(exc).upper():
                        raise ValidationError("fleet already exists: %s" % name) from exc
                    raise
            if members is not None:
                self._replace_fleet_members(conn, fleet.id, members, now)
                conn.execute("UPDATE fleets SET updated_at = ? WHERE id = ?", (now, fleet.id))
            next_members = members if members is not None else fleet.agent_ids
            previous_members = set(fleet.agent_ids)
            next_member_set = set(next_members)
            self._record_fleet_event(
                conn,
                fleet.id,
                "fleet.updated",
                actor,
                {
                    "fleet_id": fleet.id,
                    "fleet_name": new_name,
                    "previous_name": fleet.name,
                    "changed_fields": sorted(set(changed_fields)),
                    "previous_status": fleet.status,
                    "status": new_status,
                    "previous_tenant_id": fleet.tenant_id,
                    "tenant_id": new_tenant_id,
                    "agent_ids": next_members,
                    "added_agent_ids": sorted(next_member_set - previous_members),
                    "removed_agent_ids": sorted(previous_members - next_member_set),
                },
                now,
            )
        self.record_notification(
            "fleet.updated",
            "Fleet updated: %s" % new_name,
            "Fleet membership or metadata changed.",
            subject_type="fleet",
            subject_id=fleet.id,
            channels=["dashboard"],
            metadata={"actor": actor},
        )
        return self.get_fleet(fleet.id)

    def delete_fleet(self, fleet_id_or_name: str, *, actor: str = "human") -> None:
        fleet = self.get_fleet(fleet_id_or_name)
        now = utcnow()
        with self.store.transaction() as conn:
            self._record_fleet_event(
                conn,
                fleet.id,
                "fleet.deleted",
                actor,
                {
                    "fleet_id": fleet.id,
                    "fleet_name": fleet.name,
                    "status": fleet.status,
                    "tenant_id": fleet.tenant_id,
                    "agent_ids": fleet.agent_ids,
                    "agent_count": len(fleet.agent_ids),
                },
                now,
            )
            conn.execute("DELETE FROM fleets WHERE id = ?", (fleet.id,))

    def observe_fleet_agent(
        self,
        fleet_id_or_name: str,
        agent_id: str,
        *,
        source: str = "mac-agent",
        metadata: Optional[Dict[str, Any]] = None,
        actor: str = "mac-agent",
    ) -> Fleet:
        """Record live fleet presence without changing configured membership.

        ``fleet_agents`` is the desired/configured membership reconciled from
        deployment topology. Agent startup and heartbeat code should use this
        observation path so unmanaged runtime drift is visible but does not
        silently become canonical fleet topology.
        """
        fleet = self.get_fleet(fleet_id_or_name)
        self.get_agent(agent_id)
        now = utcnow()
        metadata_value = ensure_json_object(metadata)
        source_value = str(source or "runtime").strip() or "runtime"
        was_configured = agent_id in set(fleet.agent_ids)
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO fleet_agent_observations (
                    fleet_id, agent_id, source, metadata, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(fleet_id, agent_id) DO UPDATE SET
                    source = excluded.source,
                    metadata = excluded.metadata,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    fleet.id,
                    agent_id,
                    source_value,
                    json_dumps(metadata_value),
                    now,
                    now,
                ),
            )
            self._record_fleet_event(
                conn,
                fleet.id,
                "fleet.agent_observed",
                actor,
                {
                    "fleet_id": fleet.id,
                    "fleet_name": fleet.name,
                    "agent_id": agent_id,
                    "source": source_value,
                    "configured": was_configured,
                    "unmanaged": not was_configured,
                    "metadata_keys": sorted(metadata_value.keys()),
                },
                now,
            )
        return self.get_fleet(fleet.id)

    def _validated_fleet_agent_ids(self, agent_ids: Iterable[str]) -> List[str]:
        normalized = coerce_list(str(agent_id).strip() for agent_id in (agent_ids or []))
        for agent_id in normalized:
            self.get_agent(agent_id)
        return normalized

    def _replace_fleet_members(self, conn: Any, fleet_id: str, agent_ids: List[str], now: str) -> None:
        conn.execute("DELETE FROM fleet_agents WHERE fleet_id = ?", (fleet_id,))
        # Use execute() per row, not executemany(): executemany is not part of
        # the StoreConnection protocol — the Postgres _Transaction has no such
        # method (only SQLite's connection happens to). Member lists are small.
        for agent_id in agent_ids:
            conn.execute(
                "INSERT INTO fleet_agents (fleet_id, agent_id, created_at) VALUES (?, ?, ?)",
                (fleet_id, agent_id, now),
            )

    def register_agent(
        self,
        machine_id: str,
        name: str,
        capabilities: Optional[Iterable[str]] = None,
        resources: Optional[Dict[str, Any]] = None,
        agent_id: Optional[str] = None,
        hermes_instance_id: Optional[str] = None,
        actor: str = "human",
    ) -> Agent:
        self.get_machine(machine_id)
        if not name:
            raise ValidationError("agent name is required")
        if hermes_instance_id is not None:
            # Confirms the soul exists before binding. The identity layer
            # is what gates role assignment downstream.
            self.identity.get_hermes_instance(hermes_instance_id)
        now = utcnow()
        aid = agent_id or new_id("agent")
        existing_agent_row = self.store.query_one("SELECT id FROM agents WHERE id = ?", (aid,))
        if capabilities is None:
            existing_caps = self.store.query_one(
                "SELECT capabilities FROM agents WHERE id = ?", (aid,)
            )
            capabilities_json = (
                existing_caps["capabilities"] if existing_caps is not None else json_dumps([])
            )
        else:
            capabilities_json = json_dumps(coerce_list(capabilities))
        resource_value = self._agent_resources_with_preserved_control_plane_fields(aid, resources)
        resources_json = json_dumps(resource_value)
        health_value = self._project_agent_health_for_resources(
            HealthStatus.HEALTHY.value,
            HealthStatus.HEALTHY.value,
            resource_value,
        ) or HealthStatus.HEALTHY.value
        # Preserve hermes_instance_id across re-registrations when the caller
        # didn't pass one, so an ops re-register doesn't accidentally orphan
        # the agent from its soul.
        if hermes_instance_id is None:
            existing_soul = self.store.query_one(
                "SELECT hermes_instance_id FROM agents WHERE id = ?", (aid,)
            )
            hermes_instance_id = (
                existing_soul["hermes_instance_id"] if existing_soul is not None else None
            )
        # Attestation key. mac-ng2: every agent gets an HMAC-SHA256 key
        # at first registration. The cleartext key is returned ONCE in
        # the registration response so the operator can deploy it to
        # the worker; the ciphertext is stored under the same Fernet
        # used for secrets. Re-registrations preserve the existing key
        # — rotating it would invalidate all in-flight signed evidence.
        attestation_key_plaintext: Optional[str] = None
        existing_key_row = self.store.query_one(
            "SELECT attestation_key_ciphertext FROM agents WHERE id = ?", (aid,)
        )
        if existing_key_row is not None and existing_key_row["attestation_key_ciphertext"]:
            attestation_ciphertext = existing_key_row["attestation_key_ciphertext"]
        else:
            attestation_key_plaintext = _generate_attestation_key()
            attestation_ciphertext = self.secrets._encrypt(attestation_key_plaintext)
        event_type = "agent.reregistered" if existing_agent_row is not None else "agent.registered"
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO agents (
                    id, machine_id, name, capabilities, resources, status, health_status,
                    current_task_id, created_at, updated_at, last_seen_at,
                    hermes_instance_id, attestation_key_ciphertext
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    machine_id = excluded.machine_id,
                    name = excluded.name,
                    capabilities = excluded.capabilities,
                    resources = excluded.resources,
                    status = excluded.status,
                    health_status = excluded.health_status,
                    updated_at = excluded.updated_at,
                    last_seen_at = excluded.last_seen_at,
                    hermes_instance_id = excluded.hermes_instance_id,
                    attestation_key_ciphertext = excluded.attestation_key_ciphertext
                """,
                (
                    aid,
                    machine_id,
                    name,
                    capabilities_json,
                    resources_json,
                    AgentStatus.IDLE.value,
                    health_value,
                    now,
                    now,
                    now,
                    hermes_instance_id,
                    attestation_ciphertext,
                ),
            )
            self._record_agent_lifecycle_event(
                conn,
                aid,
                event_type,
                actor,
                {
                    "agent_id": aid,
                    "agent_name": name,
                    "machine_id": machine_id,
                    "capabilities": json_loads(capabilities_json, []),
                    "resource_keys": sorted(ensure_json_object(json_loads(resources_json, {})).keys()),
                    "status": AgentStatus.IDLE.value,
                    "health_status": health_value,
                    "hermes_instance_id": hermes_instance_id,
                },
                now,
            )
        agent = self.get_agent(aid)
        self._ensure_agent_nap_schedule(agent.id, actor=actor)
        agent = self.get_agent(aid)
        # Stash the cleartext key on the returned agent so the API layer
        # can surface it to the caller on first registration. The Agent
        # dataclass itself never persists this — it's an attribute set
        # only on the in-memory object returned from this call.
        if attestation_key_plaintext is not None:
            agent.attestation_key = attestation_key_plaintext  # type: ignore[attr-defined]
        return agent

    def _agent_attestation_key(self, agent_id: str) -> Optional[str]:
        """Decrypted HMAC key for an agent, or None if the row predates
        the attestation-key column."""
        row = self.store.query_one(
            "SELECT attestation_key_ciphertext FROM agents WHERE id = ?", (agent_id,)
        )
        if row is None or not row["attestation_key_ciphertext"]:
            return None
        try:
            return self.secrets._decrypt(row["attestation_key_ciphertext"])
        except Exception:  # noqa: BLE001 - corrupt or rotated key shouldn't crash review
            return None

    def rotate_agent_attestation_key(self, agent_id: str) -> str:
        """Rotate and return the cleartext HMAC key for one agent.

        Registration returns the first key exactly once. This explicit
        recovery path is for deploy/bootstrap cases where the host-local
        environment lost that one-time value before the worker could sign
        evidence. It intentionally rotates instead of exporting the old
        secret; in-flight signatures from the previous key will no longer
        verify — and the rotation timestamp is recorded so the verifier
        can produce a clearer "key was rotated" error for pending
        verdicts signed under the previous key (mac-s2vz).
        """
        self.get_agent(agent_id)
        key = _generate_attestation_key()
        now = utcnow()
        self.store.execute(
            """
            UPDATE agents
            SET attestation_key_ciphertext = ?, attestation_key_rotated_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (self.secrets._encrypt(key), now, now, agent_id),
        )
        return key

    def verify_agent_attestation_challenge(
        self,
        agent_id: str,
        challenge: JsonDict,
        signature: str,
    ) -> bool:
        self.get_agent(agent_id)
        if not isinstance(challenge, dict):
            return False
        key = self._agent_attestation_key(agent_id)
        if key is None:
            return False
        return verify_verification_manifest_signature(key, challenge, signature)

    def get_agent(self, agent_id: str) -> Agent:
        row = self.store.query_one("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if row is None:
            raise NotFoundError("agent not found: %s" % agent_id)
        return self._agent_from_row(row)

    def list_agents(self) -> List[Agent]:
        rows = self.store.query_all("SELECT * FROM agents ORDER BY name, id")
        return [self._agent_from_row(row) for row in rows]

    def update_agent(
        self,
        agent_id: str,
        *,
        name: Optional[str] = None,
        capabilities: Optional[Iterable[str]] = None,
        resources: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
        health_status: Optional[str] = None,
        hermes_instance_id: Optional[str] = None,
        actor: str = "human",
    ) -> Agent:
        agent_before = self.get_agent(agent_id)
        updates: List[str] = []
        params: List[Any] = []
        changed_fields: List[str] = []
        next_name = agent_before.name
        next_status = agent_before.status
        next_health_status = agent_before.health_status
        next_hermes_instance_id = agent_before.hermes_instance_id
        if name is not None:
            name_value = name.strip()
            if not name_value:
                raise ValidationError("agent name is required")
            updates.append("name = ?")
            params.append(name_value)
            next_name = name_value
            if name_value != agent_before.name:
                changed_fields.append("name")
        if capabilities is not None:
            capability_list = coerce_list(capabilities)
            updates.append("capabilities = ?")
            params.append(json_dumps(capability_list))
            if capability_list != agent_before.capabilities:
                changed_fields.append("capabilities")
        if resources is not None:
            resource_value = ensure_json_object(resources)
            updates.append("resources = ?")
            params.append(json_dumps(resource_value))
            if resource_value != agent_before.resources:
                changed_fields.append("resources")
        if status is not None:
            status_value = _state_value(status)
            try:
                AgentStatus(status_value)
            except ValueError:
                raise ValidationError("unsupported agent status: %s" % status_value)
            if status_value == AgentStatus.IDLE.value and self._agent_has_active_lease(agent_id):
                raise ValidationError("agent cannot be set idle while holding an active lease")
            if status_value == AgentStatus.OFFLINE.value:
                self._expire_agent_active_leases(agent_id, utcnow(), "agent_update_offline")
            updates.append("status = ?")
            params.append(status_value)
            next_status = status_value
            if status_value != agent_before.status:
                changed_fields.append("status")
            if status_value in {AgentStatus.IDLE.value, AgentStatus.OFFLINE.value}:
                updates.append("current_task_id = NULL")
        if health_status is not None:
            health_value = _state_value(health_status)
            try:
                HealthStatus(health_value)
            except ValueError:
                raise ValidationError("unsupported agent health_status: %s" % health_value)
            updates.append("health_status = ?")
            params.append(health_value)
            next_health_status = health_value
            if health_value != agent_before.health_status:
                changed_fields.append("health_status")
        if hermes_instance_id is not None:
            hermes_value = hermes_instance_id.strip()
            if hermes_value:
                self.identity.get_hermes_instance(hermes_value)
                updates.append("hermes_instance_id = ?")
                params.append(hermes_value)
                next_hermes_instance_id = hermes_value
            else:
                updates.append("hermes_instance_id = NULL")
                next_hermes_instance_id = None
            if next_hermes_instance_id != agent_before.hermes_instance_id:
                changed_fields.append("hermes_instance_id")
        if not updates:
            return self.get_agent(agent_id)
        updates.append("updated_at = ?")
        now = utcnow()
        params.append(now)
        params.append(agent_id)
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE agents SET %s WHERE id = ?" % ", ".join(updates),
                tuple(params),
            )
            self._record_agent_lifecycle_event(
                conn,
                agent_id,
                "agent.updated",
                actor,
                {
                    "agent_id": agent_id,
                    "agent_name": next_name,
                    "previous_name": agent_before.name,
                    "changed_fields": sorted(set(changed_fields)),
                    "previous_status": agent_before.status,
                    "status": next_status,
                    "previous_health_status": agent_before.health_status,
                    "health_status": next_health_status,
                    "previous_hermes_instance_id": agent_before.hermes_instance_id,
                    "hermes_instance_id": next_hermes_instance_id,
                },
                now,
            )
        return self.get_agent(agent_id)

    def update_agent_installed_packages(
        self,
        agent_id: str,
        installed_packages: Dict[str, Any],
        *,
        actor: str = "agent",
    ) -> Agent:
        """Record the agent's self-installed pip/npm footprint (its persistent
        "default footprint"). Kept separate from update_agent so a footprint
        report can't clobber capabilities/status. Re-registration preserves it
        (the register UPSERT does not touch this column)."""
        agent_before = self.get_agent(agent_id)
        payload = ensure_json_object(installed_packages)
        now = utcnow()
        pip = payload.get("pip")
        npm = payload.get("npm")
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE agents SET installed_packages = ?, updated_at = ? WHERE id = ?",
                (json_dumps(payload), now, agent_id),
            )
            self._record_agent_lifecycle_event(
                conn,
                agent_id,
                "agent.installed_packages_updated",
                actor,
                {
                    "agent_id": agent_id,
                    "agent_name": agent_before.name,
                    "pip_count": len(pip) if isinstance(pip, list) else 0,
                    "npm_count": len(npm) if isinstance(npm, list) else 0,
                },
                now,
            )
        return self.get_agent(agent_id)

    def disable_agent(self, agent_id: str, *, actor: str = "human") -> Agent:
        return self.update_agent(
            agent_id,
            status=AgentStatus.OFFLINE.value,
            health_status=HealthStatus.DEGRADED.value,
            actor=actor,
        )

    def delete_agent(self, agent_id: str, *, actor: str = "human") -> None:
        agent = self.get_agent(agent_id)
        if self._agent_has_active_lease(agent_id):
            raise ValidationError("agent cannot be deleted while holding an active lease")
        now = utcnow()
        with self.store.transaction() as conn:
            self._record_agent_lifecycle_event(
                conn,
                agent_id,
                "agent.deleted",
                actor,
                {
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "machine_id": agent.machine_id,
                    "status": agent.status,
                    "health_status": agent.health_status,
                    "hermes_instance_id": agent.hermes_instance_id,
                },
                now,
            )
            conn.execute("DELETE FROM mood_overlays WHERE agent_id = ?", (agent_id,))
            conn.execute("DELETE FROM nap_schedules WHERE agent_id = ?", (agent_id,))
            conn.execute("DELETE FROM nap_runs WHERE agent_id = ?", (agent_id,))
            conn.execute("DELETE FROM agent_events WHERE agent_id = ?", (agent_id,))
            conn.execute("DELETE FROM messages WHERE sender_agent_id = ? OR recipient_agent_id = ?", (agent_id, agent_id))
            conn.execute("DELETE FROM agents WHERE id = ?", (agent.id,))

    def heartbeat_agent(
        self,
        agent_id: str,
        status: Optional[str] = None,
        health_status: Optional[str] = None,
        resources: Optional[Dict[str, Any]] = None,
        running_digest: Optional[str] = None,
        running_digest_signature: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> Agent:
        agent_before = self.get_agent(agent_id)
        now = utcnow()
        updates = ["last_seen_at = ?", "updated_at = ?"]
        params: List[Any] = [now, now]
        status_value: Optional[str] = None
        health_value: Optional[str] = None
        resource_value = agent_before.resources
        next_running_digest = agent_before.running_digest
        changed_fields: List[str] = []
        if status is not None:
            status_value = _state_value(status)
            try:
                AgentStatus(status_value)
            except ValueError:
                raise ValidationError("unsupported agent status: %s" % status_value)
            updates.append("status = ?")
            params.append(status_value)
            if status_value != agent_before.status:
                changed_fields.append("status")
        if health_status is not None:
            health_value = _state_value(health_status)
            try:
                HealthStatus(health_value)
            except ValueError:
                raise ValidationError("unsupported agent health_status: %s" % health_value)
        if resources is not None:
            resource_value = self._agent_resources_with_preserved_control_plane_fields(agent_id, resources)
            updates.append("resources = ?")
            params.append(json_dumps(resource_value))
            if resource_value != agent_before.resources:
                changed_fields.append("resources")
        projected_health_value = self._project_agent_health_for_resources(
            agent_before.health_status,
            health_value,
            resource_value,
        )
        if projected_health_value is not None:
            updates.append("health_status = ?")
            params.append(projected_health_value)
            if projected_health_value != agent_before.health_status:
                changed_fields.append("health_status")
            health_value = projected_health_value
        if running_digest is not None:
            digest = running_digest.strip()
            if digest:
                # Anchor fleet rollout state to a known runtime build. If you
                # roll out a new agent build, register the runtime first; the
                # heartbeat that declares the new digest then becomes the truth
                # source for "how many agents are on which build."
                exists = self.store.query_one(
                    "SELECT 1 FROM runtime_environments WHERE digest = ? LIMIT 1",
                    (digest,),
                )
                if exists is None:
                    raise ValidationError(
                        "running_digest %s is not registered as a runtime_environments.digest"
                        % digest
                    )
                # mac-oud5: require the agent to sign its digest claim
                # with its attestation key when it differs from the
                # previously-declared digest. This doesn't prove the
                # process IS that digest — that would need OS / runtime
                # attestation — but it does cryptographically bind the
                # claim to the agent's identity so a peer cannot inject
                # a false digest on another agent's behalf, and the
                # signature is durable enough to audit later.
                if (
                    digest != agent_before.running_digest
                    and running_digest_signature is not None
                ):
                    key = self._agent_attestation_key(agent_id)
                    if key is None:
                        raise ValidationError(
                            "agent %s has no attestation key — cannot verify "
                            "running_digest signature" % agent_id
                        )
                    claim = {"agent_id": agent_id, "running_digest": digest}
                    if not verify_verification_manifest_signature(
                        key, claim, running_digest_signature
                    ):
                        raise ValidationError(
                            "running_digest signature does not verify under "
                            "agent's attestation key"
                        )
                updates.append("running_digest = ?")
                params.append(digest)
                next_running_digest = digest
                if digest != agent_before.running_digest:
                    changed_fields.append("running_digest")
                    if running_digest_signature is None:
                        self.observability.record_log(
                            "agent.running_digest_unsigned",
                            level="warning",
                            layer="control_plane",
                            source="control_plane",
                            subject_type="agent",
                            subject_id=agent_id,
                            detail={
                                "running_digest": digest,
                                "note": "mac-oud5: digest accepted without signature; "
                                "future enforcement will require running_digest_signature",
                            },
                        )
            else:
                updates.append("running_digest = NULL")
                next_running_digest = None
                if agent_before.running_digest is not None:
                    changed_fields.append("running_digest")
        if status_value == AgentStatus.IDLE.value and self._agent_has_active_lease(agent_id):
            raise ValidationError("agent cannot report idle while holding an active lease")
        if status_value == AgentStatus.DRAINING.value and self._agent_has_active_lease(agent_id):
            updates.append("current_task_id = NULL")
        if status_value == AgentStatus.OFFLINE.value:
            self._expire_agent_active_leases(agent_id, now, "heartbeat_offline")
            try:
                self.service_roles.expire_agent_claims(agent_id, reason="heartbeat_offline")
            except Exception:  # noqa: BLE001 - best-effort service-claim cleanup
                pass
        if status_value in {AgentStatus.IDLE.value, AgentStatus.OFFLINE.value}:
            updates.append("current_task_id = NULL")
        params.append(agent_id)
        # Only log a lifecycle/observability event when something MEANINGFUL
        # changed (status / health / running_digest) — not on resource jitter,
        # which differs on essentially every heartbeat. Writing a durable
        # agent_lifecycle_events row (+ mirrored observability_event) on every
        # beat was the dominant source of hub-db bloat: ~527K lifecycle + ~228K
        # obs rows in ~4 days on rocky, almost all just CPU/mem jitter.
        meaningful_changes = [f for f in changed_fields if f != "resources"]
        with self.store.transaction() as conn:
            conn.execute("UPDATE agents SET %s WHERE id = ?" % ", ".join(updates), tuple(params))
            if meaningful_changes:
                self._record_agent_lifecycle_event(
                    conn,
                    agent_id,
                    "agent.heartbeat_updated",
                    actor or agent_id,
                    {
                        "agent_id": agent_id,
                        "agent_name": agent_before.name,
                        "changed_fields": sorted(set(meaningful_changes)),
                        "previous_status": agent_before.status,
                        "status": status_value or agent_before.status,
                        "previous_health_status": agent_before.health_status,
                        "health_status": health_value or agent_before.health_status,
                        "previous_running_digest": agent_before.running_digest,
                        "running_digest": next_running_digest,
                    },
                    now,
                )
        agent = self.get_agent(agent_id)
        self._ensure_agent_nap_schedule(agent.id, actor=actor or agent_id)
        self._maybe_advance_reviews_on_heartbeat(agent_before)
        self._maybe_drain_notifications_on_heartbeat(agent_before)
        return agent

    def _ensure_agent_nap_schedule(self, agent_id: str, *, actor: str) -> None:
        agent = self.get_agent(agent_id)
        if agent.status == AgentStatus.OFFLINE.value:
            return
        if self.get_nap_schedule(agent.id) is None:
            self.configure_nap(agent.id, actor=actor or agent.id)



    def _maybe_drain_notifications_on_heartbeat(self, agent: Agent) -> None:
        """Drain ``pending`` operator notifications on hub-agent heartbeat.

        Mirrors :meth:`_maybe_advance_reviews_on_heartbeat`: only the
        designated hub agent triggers the drain so a fleet of N agents
        doesn't make N concurrent delivery attempts. Without this, the
        ``deliver_pending`` method would sit idle — no other code path
        runs it periodically.
        """
        if not _truthy_env("MAC_NOTIFIER_DRAIN_ON_HEARTBEAT", "1"):
            return
        hub_agent = os.environ.get(
            "MAC_NOTIFIER_DRAIN_HUB_AGENT",
            os.environ.get(
                "MAC_REVIEW_TICK_HUB_AGENT",
                os.environ.get("MAC_BEADS_BRIDGE_HUB_AGENT", ""),
            ),
        ).strip()
        if not hub_agent:
            return
        if agent.name != hub_agent and agent.id != hub_agent:
            return
        try:
            limit = int(os.environ.get("MAC_NOTIFIER_DRAIN_LIMIT", "100"))
        except ValueError:
            limit = 100
        if limit <= 0:
            return
        try:
            result = self.deliver_pending_notifications(limit=limit)
            delivered = (
                int(result.get("delivered", 0)) if isinstance(result, dict) else 0
            )
            if delivered:
                self.record_log(
                    "notifier.heartbeat_drain",
                    layer="control_plane",
                    source=agent.id,
                    level="info",
                    detail={"delivered": delivered, "limit": limit},
                )
        except Exception as exc:  # noqa: BLE001 - heartbeat liveness must survive delivery failures.
            try:
                self.record_log(
                    "notifier.heartbeat_drain_failed",
                    layer="control_plane",
                    source=agent.id,
                    level="warning",
                    detail={"error": str(exc)},
                )
            except Exception:
                pass

    def _maybe_advance_reviews_on_heartbeat(self, agent: Agent) -> None:
        if not _truthy_env("MAC_REVIEW_TICK_ON_HEARTBEAT", "1"):
            return
        hub_agent = os.environ.get(
            "MAC_REVIEW_TICK_HUB_AGENT",
            os.environ.get("MAC_BEADS_BRIDGE_HUB_AGENT", ""),
        ).strip()
        if not hub_agent:
            return
        if agent.name != hub_agent and agent.id != hub_agent:
            return
        try:
            limit = int(os.environ.get("MAC_REVIEW_TICK_LIMIT", "25"))
        except ValueError:
            limit = 25
        try:
            result = self._advance_default_review_sweep_page(
                limit=max(1, limit),
                actor=agent.id,
                tenant_id=None,
            )
            stuck = [
                item
                for item in result.get("results", [])
                if item.get("status")
                in {
                    "waiting_for_verifiable_evidence",
                    "waiting_for_reviewer",
                    "waiting_for_reviewer_verdict",
                    "waiting_for_publication_evidence",
                    "waiting_for_publication_target",
                    "ambiguous_pending_reviews",
                }
            ]
            if result.get("processed") or stuck:
                self.record_log(
                    "workflow.default_review.heartbeat_tick",
                    layer="control_plane",
                    source=agent.id,
                    level="warning" if stuck else "info",
                    detail={"processed": result.get("processed", 0), "stuck": stuck},
                )
        except Exception as exc:  # noqa: BLE001 - heartbeat liveness must survive review sweeps.
            try:
                self.record_log(
                    "workflow.default_review.heartbeat_tick_failed",
                    layer="control_plane",
                    source=agent.id,
                    level="warning",
                    detail={"error": str(exc)},
                )
            except Exception:
                pass

    def fleet_build_distribution(self) -> JsonDict:
        """Aggregate agents by their declared running_digest.

        Useful for "what percent of the fleet is on v0.8 vs v0.9" without joining
        rollouts. Agents with no declared digest are bucketed as 'unknown'.
        """
        rows = self.store.query_all(
            """
            SELECT COALESCE(running_digest, '') AS digest, COUNT(*) AS count
            FROM agents
            WHERE status != ?
            GROUP BY running_digest
            ORDER BY count DESC
            """,
            (AgentStatus.OFFLINE.value,),
        )
        buckets = [
            {"digest": row["digest"] or None, "count": int(row["count"])}
            for row in rows
        ]
        total = sum(bucket["count"] for bucket in buckets) or 1
        for bucket in buckets:
            bucket["percent"] = round(bucket["count"] * 100.0 / total, 2)
        return {"total_live_agents": total if total > 0 else 0, "buckets": buckets}

    # Mood overlays (agent-self-reported emotional state)
    #
    # The contract: agents pick their own mood based on local signals (recent
    # outcomes, retry counts, review rejections — already in the events
    # stream). mac records and audits transitions; it does NOT derive mood on
    # the agent's behalf. Operators can read, but the authoritative caller is
    # the agent itself.

    # Moods: thin facade over ``self.agent_state``.

    def set_mood(self, *args: Any, **kwargs: Any) -> MoodOverlay:
        return self.agent_state.set_mood(*args, **kwargs)

    def get_current_mood(self, agent_id: str) -> Optional[MoodOverlay]:
        return self.agent_state.get_current_mood(agent_id)

    def clear_mood(self, *args: Any, **kwargs: Any) -> Optional[MoodOverlay]:
        return self.agent_state.clear_mood(*args, **kwargs)

    def get_mood_overlay(self, overlay_id: str) -> MoodOverlay:
        return self.agent_state.get_mood_overlay(overlay_id)

    def list_mood_history(self, *args: Any, **kwargs: Any) -> List[MoodOverlay]:
        return self.agent_state.list_mood_history(*args, **kwargs)

    # Nap schedule + lifecycle
    #
    # Each agent has a single nap_schedule row (offset_minutes, window_minutes).
    # The offset defaults to a stable hash of the agent's name to spread the
    # fleet across the early-UTC window (matches ACC's spec, MD5 % 360). Nap
    # *execution* is off-process — the agent (or a sidecar) decides what to
    # summarize and where to store it. mac records begin/complete events and
    # links to the produced summary evidence + vector refs.

    # Nap schedule + lifecycle: thin facade over ``self.agent_state``.

    def configure_nap(self, *args: Any, **kwargs: Any) -> NapSchedule:
        return self.agent_state.configure_nap(*args, **kwargs)

    def get_nap_schedule(self, agent_id: str) -> Optional[NapSchedule]:
        return self.agent_state.get_nap_schedule(agent_id)

    def list_nap_schedules(self) -> List[NapSchedule]:
        return self.agent_state.list_nap_schedules()

    def next_nap_window(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, str]]:
        return self.agent_state.next_nap_window(*args, **kwargs)

    def begin_nap(self, *args: Any, **kwargs: Any) -> NapRun:
        return self.agent_state.begin_nap(*args, **kwargs)

    def memory_health(
        self,
        *,
        qdrant_url: Optional[str] = None,
        nap_interval_hours: float = 24.0,
    ) -> JsonDict:
        """mem-10: memory-tier health snapshot.

        Returns a dict the operator (and a future scheduled alerter)
        can read to spot the failure modes the audit found:

          * Inert vector tier — memory_records growing while
            vector_refs stays at 0. The audit's smoking gun.
          * Stalled consolidator — last_nap_run_at older than
            ``2 * nap_interval_hours`` means the nightly nap stopped
            running.
          * Disk bloat — mac.db growing faster than the vector tier.

        ``qdrant_url`` defaults to the configured Qdrant URL
        (MAC_QDRANT_URL, QDRANT_URL, QDRANT_ADDRESS, or
        QDRANT_FLEET_URL). When unreachable,
        the qdrant_collections block reports its error instead of
        raising; the operator still gets the SQLite-side numbers.
        """
        from datetime import datetime, timezone
        from pathlib import Path

        now = utcnow()
        now_dt = datetime.now(tz=timezone.utc)

        # SQLite-side counts.
        def _count(sql: str) -> int:
            row = self.store.query_one(sql)
            return int(row["n"]) if row is not None else 0

        mr_count = _count("SELECT COUNT(*) AS n FROM memory_records")
        vr_count = _count("SELECT COUNT(*) AS n FROM vector_refs")
        oe_count = _count("SELECT COUNT(*) AS n FROM observability_events")

        nap_row = self.store.query_one(
            "SELECT MAX(completed_at) AS last FROM nap_runs WHERE status = 'completed'"
        )
        last_nap_at = nap_row["last"] if nap_row is not None and nap_row["last"] else None

        # mac.db file size (when we can find the file).
        db_path = getattr(self.store, "path", None) or getattr(self.store, "_path", None)
        db_size: Optional[int] = None
        if db_path:
            try:
                db_size = Path(str(db_path)).stat().st_size
            except OSError:
                db_size = None

        # Qdrant points per collection — best-effort.
        url = _configured_qdrant_url(qdrant_url)
        qdrant_block: JsonDict = {"url": url, "collections": {}, "error": None}
        if url:
            try:
                from mac.models import MAC_MEMORY_COLLECTIONS
                import json as _json
                import urllib.request as _req

                for tier_name, coll in MAC_MEMORY_COLLECTIONS.items():
                    try:
                        with _req.urlopen(
                            "%s/collections/%s"
                            % (url.rstrip("/"), coll),
                            timeout=5,
                        ) as resp:
                            data = _json.loads(resp.read().decode("utf-8"))
                            points = (
                                (data.get("result") or {}).get("points_count")
                            )
                            qdrant_block["collections"][coll] = {
                                "tier": tier_name,
                                "points_count": int(points) if points is not None else None,
                            }
                    except Exception as exc:  # noqa: BLE001
                        qdrant_block["collections"][coll] = {
                            "tier": tier_name,
                            "error": str(exc),
                        }
            except Exception as exc:  # noqa: BLE001
                qdrant_block["error"] = str(exc)

        # Alert rules (the audit's failure modes encoded).
        alerts: List[JsonDict] = []
        if mr_count > 100 and vr_count == 0:
            alerts.append(
                {
                    "severity": "critical",
                    "code": "inert_vector_tier",
                    "message": (
                        "memory_records=%d but vector_refs=0; the writer never "
                        "ran. This is the failure mode the original 2026-05-28 "
                        "audit surfaced." % mr_count
                    ),
                }
            )
        if last_nap_at is not None:
            try:
                last_dt = datetime.fromisoformat(last_nap_at)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_hours = (now_dt - last_dt).total_seconds() / 3600.0
                if age_hours > 2.0 * nap_interval_hours:
                    alerts.append(
                        {
                            "severity": "critical",
                            "code": "stalled_consolidator",
                            "message": (
                                "last successful nap completed_at=%s "
                                "(%.1fh ago, threshold %.1fh = 2× nap_interval)"
                                % (last_nap_at, age_hours, 2.0 * nap_interval_hours)
                            ),
                        }
                    )
            except (TypeError, ValueError):
                pass
        elif mr_count > 0:
            # We have memories but no completed nap_runs at all.
            alerts.append(
                {
                    "severity": "warning",
                    "code": "no_nap_history",
                    "message": (
                        "no completed nap_runs on record despite %d "
                        "memory_records — the consolidator has never run "
                        "successfully." % mr_count
                    ),
                }
            )

        return {
            "schema": "mac.memory_health.v1",
            "captured_at": now,
            "mac_db_size_bytes": db_size,
            "memory_records_count": mr_count,
            "vector_refs_count": vr_count,
            "observability_events_count": oe_count,
            "last_nap_run_at": last_nap_at,
            "qdrant": qdrant_block,
            "alerts": alerts,
        }

    def recall_memory(
        self,
        query: str,
        *,
        tier: str = "medium",
        limit: int = 5,
        min_score: Optional[float] = None,
        project: Optional[str] = None,
        tenant_id: Optional[str] = None,
        qdrant_url: Optional[str] = None,
        vector_writer: Optional[Any] = None,
    ) -> List[JsonDict]:
        """mem-09: vector-tier recall.

        Embeds ``query`` and returns the top hits in the chosen tier
        as the mem-09 standard shape (memory_id, task_id, score,
        summary, ...). Server-side filters cover project/tenant; pass
        a pre-built VectorWriterService when the caller already has
        one to skip repeated initialization.
        """
        if not query or not str(query).strip():
            raise ValidationError("recall_memory requires a non-empty query")
        if vector_writer is None:
            url = _configured_qdrant_url(qdrant_url)
            if not url:
                raise ValidationError(
                    "recall_memory needs a Qdrant URL — pass qdrant_url or set "
                    "MAC_QDRANT_URL/QDRANT_URL/QDRANT_ADDRESS/QDRANT_FLEET_URL"
                )
            from mac.vector_writer_service import VectorWriterService

            vector_writer = VectorWriterService(memory=self.memory, qdrant_url=url)
        return vector_writer.recall(
            query,
            tier=tier,
            limit=limit,
            score_threshold=min_score,
            project=project,
            tenant_id=tenant_id,
        )

    def recall_dream_artifacts(
        self,
        query: str,
        *,
        tier: str = "medium",
        limit: int = 5,
        min_score: Optional[float] = None,
        project: Optional[str] = None,
        agent_id: Optional[str] = None,
        scope: Optional[str] = None,
        kind: Optional[str] = None,
        min_confidence: Optional[str] = None,
        tenant_id: Optional[str] = None,
        qdrant_url: Optional[str] = None,
        vector_writer: Optional[Any] = None,
    ) -> List[JsonDict]:
        """Recall typed ``mac.dream.v1`` artifacts using their retrieval rules.

        Dream artifacts are stored as ordinary memory_records, but this method
        keeps their read path explicit: only ``subject_type='dream'`` hits are
        eligible, and callers may narrow by scope, kind, project, agent, tenant,
        and minimum confidence.
        """
        if not query or not str(query).strip():
            raise ValidationError("recall_dream_artifacts requires a non-empty query")
        if vector_writer is None:
            url = _configured_qdrant_url(qdrant_url)
            if not url:
                raise ValidationError(
                    "recall_dream_artifacts needs a Qdrant URL — pass qdrant_url "
                    "or set MAC_QDRANT_URL/QDRANT_URL/QDRANT_ADDRESS/QDRANT_FLEET_URL"
                )
            from mac.vector_writer_service import VectorWriterService

            vector_writer = VectorWriterService(memory=self.memory, qdrant_url=url)

        must: List[JsonDict] = [{"key": "subject_type", "match": {"value": "dream"}}]
        if project:
            must.append({"key": "project", "match": {"value": project}})
        if agent_id:
            must.append({"key": "agent_id", "match": {"value": agent_id}})
        if scope:
            must.append({"key": "dream_scope", "match": {"value": scope}})
        if kind:
            must.append({"key": "dream_kind", "match": {"value": kind}})

        hits = vector_writer.recall(
            query,
            tier=tier,
            limit=max(1, int(limit)),
            score_threshold=min_score,
            filter_payload={"must": must},
            tenant_id=tenant_id,
        )
        if min_confidence:
            floor_by_name = {"low": 0.0, "medium": 0.65, "high": 0.9}
            floor = floor_by_name.get(str(min_confidence).strip().lower())
            if floor is None:
                raise ValidationError("min_confidence must be one of low / medium / high")

            def _confidence_score(hit: JsonDict) -> float:
                payload = hit.get("payload") if isinstance(hit.get("payload"), dict) else {}
                raw = payload.get("dream_confidence_score")
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    name = str(payload.get("dream_confidence") or "").strip().lower()
                    return floor_by_name.get(name, 0.0)

            hits = [hit for hit in hits if _confidence_score(hit) >= floor]
        return hits[: max(1, int(limit))]

    def run_nap_cycle(
        self,
        agent_id: str,
        *,
        actor: Optional[str] = None,
        vector_writer: Optional[Any] = None,
        embed_into_medium: bool = True,
        emit_dream_artifacts: bool = True,
    ) -> JsonDict:
        """mem-08 autonomy: drive an agent through one full nap.

        Sequence:
          1. begin_nap (agent → DRAINING, nap_run created)
          2. consolidate (summarize since last nap, embed into medium)
          3. complete_nap (agent → IDLE, nap_run completed)

        Neither consolidation nor completion is allowed to escape once
        begin_nap has moved the agent to DRAINING: a step-2 failure is
        captured in ``consolidation_error`` and a step-3 failure in
        ``complete_error``, and the agent's resolved state is always
        refetched and reported. A leaking exception here would strand
        the agent in DRAINING, which is much worse than a missing
        summary, so this method never re-raises after the nap begins.

        If begin_nap itself refuses (e.g. the agent holds an active
        lease — it's mid-task, not nappable right now), the cycle is
        reported as ``skipped`` rather than raising. This keeps the
        autonomous nap-tick from failing its whole batch over one busy
        agent; the next tick retries once the agent is free.
        """
        try:
            run = self.begin_nap(agent_id, actor=actor)
        except ValidationError as exc:
            return {
                "nap_run": None,
                "skipped": True,
                "skip_reason": str(exc),
                "consolidation": {},
                "consolidation_error": None,
                "complete_error": None,
            }
        consolidation_report: JsonDict = {}
        consolidation_error: Optional[str] = None
        complete_error: Optional[str] = None
        completed = run
        try:
            try:
                consolidation_report = self.consolidate_nap(
                    agent_id,
                    nap_run_id=run.id,
                    embed_into_medium=embed_into_medium,
                    emit_dream_artifacts=emit_dream_artifacts,
                    vector_writer=vector_writer,
                    created_by=actor or "nap-cycle:%s" % agent_id,
                )
            except Exception as exc:  # noqa: BLE001
                consolidation_error = str(exc)
        finally:
            # Always attempt to complete the nap so the agent returns to
            # IDLE, even when consolidation threw. A completion failure
            # (anything other than the benign off-path TransitionError)
            # is recorded rather than re-raised so the agent isn't left
            # stranded in DRAINING by a propagating exception.
            try:
                completed = self.complete_nap(
                    run.id,
                    summary_evidence_id=None,
                    detail={
                        "consolidation": consolidation_report,
                        "consolidation_error": consolidation_error,
                    },
                    actor=actor,
                )
            except TransitionError:
                # An off-path actor already completed/failed the run
                # (e.g., an admin cancelled mid-cycle). Refetch and
                # report; not an error from this cycle's perspective.
                completed = self._safe_get_nap_run(run.id, run)
            except Exception as exc:  # noqa: BLE001
                complete_error = str(exc)
                completed = self._safe_get_nap_run(run.id, run)
        return {
            "nap_run": completed.to_dict(),
            "skipped": False,
            "consolidation": consolidation_report,
            "consolidation_error": consolidation_error,
            "complete_error": complete_error,
        }

    def _safe_get_nap_run(self, run_id: str, fallback: NapRun) -> NapRun:
        """Refetch a nap_run for reporting in an error path, falling
        back to a known run object if even the read fails — so
        run_nap_cycle never re-raises after the nap has begun."""
        try:
            return self.get_nap_run(run_id)
        except Exception:  # noqa: BLE001
            return fallback

    def list_due_nap_agents(self, *, as_of: Optional[str] = None) -> List[JsonDict]:
        """Return enabled nap_schedules whose current window has opened
        and hasn't been completed yet.

        An agent's "current window" is today's `midnight UTC +
        offset_minutes` (or yesterday's if today's hasn't opened yet).
        We consider it open when `as_of` >= window_start, and unclaimed
        when last_completed_at is either NULL or before window_start.

        Selection is deliberately catch-up, not strict: an agent stays
        due from window_start until it actually completes a nap, even
        once `as_of` has passed window_end. ``window_minutes`` therefore
        does NOT gate the autonomous path — it only sets how long the
        informational ``in_window`` flag stays true. This keeps the
        once-per-day nap robust against a tick that lands just after a
        narrow window closes (a strict in-window check would silently
        skip the agent for the whole day). Callers that want strict
        windowing should filter on ``in_window`` themselves.
        """
        from datetime import datetime, timedelta, timezone

        as_of_dt = (
            datetime.fromisoformat(as_of)
            if as_of
            else datetime.now(timezone.utc)
        )
        if as_of_dt.tzinfo is None:
            as_of_dt = as_of_dt.replace(tzinfo=timezone.utc)
        rows = self.store.query_all(
            """
            SELECT agent_id, offset_minutes, window_minutes, last_completed_at
            FROM nap_schedules WHERE enabled = 1
            """
        )
        due: List[JsonDict] = []
        for row in rows:
            offset = int(row["offset_minutes"] or 0)
            window = int(row["window_minutes"] or 15)
            midnight = as_of_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            window_start = midnight + timedelta(minutes=offset)
            if window_start > as_of_dt:
                window_start = window_start - timedelta(days=1)
            window_end = window_start + timedelta(minutes=window)
            already_done = False
            if row["last_completed_at"]:
                try:
                    last_dt = datetime.fromisoformat(row["last_completed_at"])
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    if last_dt >= window_start:
                        already_done = True
                except (TypeError, ValueError):
                    pass
            if already_done:
                continue
            due.append(
                {
                    "agent_id": row["agent_id"],
                    "window_start": window_start.isoformat(timespec="microseconds"),
                    "window_end": window_end.isoformat(timespec="microseconds"),
                    "last_completed_at": row["last_completed_at"],
                    "in_window": window_start <= as_of_dt <= window_end,
                }
            )
        return due

    def consolidate_nap(
        self,
        agent_id: str,
        *,
        since: Optional[str] = None,
        nap_run_id: Optional[str] = None,
        embed_into_medium: bool = True,
        emit_dream_artifacts: bool = True,
        vector_writer: Optional[Any] = None,
        created_by: Optional[str] = None,
    ) -> JsonDict:
        """mem-08: build per-(task/project) summaries from the agent's
        recent memory_records and embed them into the medium tier.

        ``vector_writer`` is an optional pre-built VectorWriterService.
        When None, no embedding happens (consolidator runs in
        summary-only mode — useful when Qdrant is unreachable)."""
        from mac.nap_consolidator import NapConsolidatorService

        consolidator = NapConsolidatorService(
            store=self.store,
            memory=self.memory,
            vector_writer=vector_writer,
        )
        return consolidator.consolidate_agent(
            agent_id,
            since=since,
            nap_run_id=nap_run_id,
            embed_into_medium=embed_into_medium,
            emit_dream_artifacts=emit_dream_artifacts,
            created_by=created_by,
        )

    def complete_nap(self, *args: Any, **kwargs: Any) -> NapRun:
        return self.agent_state.complete_nap(*args, **kwargs)

    def fail_nap(self, *args: Any, **kwargs: Any) -> NapRun:
        return self.agent_state.fail_nap(*args, **kwargs)

    def get_nap_run(self, run_id: str) -> NapRun:
        return self.agent_state.get_nap_run(run_id)

    def list_nap_runs(self, *args: Any, **kwargs: Any) -> List[NapRun]:
        return self.agent_state.list_nap_runs(*args, **kwargs)

    # -- Ticketing connectors (meta-tickets) --------------------------------
    # beads is no longer a read/write source; it's an import-only connector.
    # The native source is the MAC task ledger; .tickets/ is an optional local
    # compatibility mirror. detect/convert route through mac.ticketing so any
    # future ticketing system plugs in the same way.

    def detect_ticketing(self, repo_path: str) -> JsonDict:
        """Report which ticketing sources a repo has + whether a one-way
        ledger import should be offered (foreign source present, no local
        .tickets/ compatibility mirror). Read-only. Emits a
        ``ticketing.conversion_available`` observation the hub's hermes agent
        can surface to the user."""
        from pathlib import Path as _Path
        from mac.ticketing import detect_ticketing as _detect

        detection = _detect(_Path(repo_path))
        if detection.needs_conversion:
            self.record_log(
                "ticketing.conversion_available",
                layer="control_plane",
                source="ticketing",
                level="info",
                subject_type="environment",
                subject_id=str(repo_path),
                detail={
                    "schema": "mac.ticketing_conversion.v1",
                    "conversion_from": detection.conversion_from,
                    "message": detection.message,
                    "prompt": (
                        "Repo %s has a '%s' ticket source but no local .tickets "
                        "compatibility mirror. Import it one-way into the MAC "
                        "task ledger?"
                        % (repo_path, detection.conversion_from)
                    ),
                },
            )
        return detection.to_dict()

    def convert_ticketing_source(
        self,
        repo_path: str,
        *,
        project: str,
        actor: str = "hermes",
        dry_run: bool = False,
    ) -> JsonDict:
        """Run the one-way conversion of a detected foreign source (e.g. beads)
        into MAC ledger tasks plus optional local compatibility files. Hermes
        calls this only after the user agrees. Never writes back to the foreign
        source."""
        from pathlib import Path as _Path
        from mac.ticketing import detect_ticketing as _detect, connector_for

        detection = _detect(_Path(repo_path))
        if not detection.needs_conversion or not detection.conversion_from:
            return {"status": "no_conversion_needed", "detection": detection.to_dict()}
        connector = connector_for(detection.conversion_from)
        if connector is None:
            return {"status": "unknown_connector", "detection": detection.to_dict()}
        report = connector.convert(
            _Path(repo_path), project=project, cp=None if dry_run else self, actor=actor, dry_run=dry_run
        )
        self.record_log(
            "ticketing.converted",
            layer="control_plane",
            source="ticketing",
            level="info",
            subject_type="environment",
            subject_id=str(repo_path),
            detail={"schema": "mac.ticketing_conversion.v1", "from": detection.conversion_from, "report": report},
        )
        return {"status": "converted", "from": detection.conversion_from, "report": report}

    # -- Fleet awareness (fleet-01/02) --------------------------------------

    def fleet_snapshot(self, *, exclude_agent_id: Optional[str] = None, limit: int = 30) -> JsonDict:
        """A compact, current view of the fleet — who's online, their status, and
        what each agent is working on. Powers passive group awareness (injected
        into each agent's runtime context) and the on-demand `fleet` tool, so the
        three agents always know what the others are doing."""
        active = {
            TaskState.CLAIMED.value,
            TaskState.RUNNING.value,
            TaskState.NEEDS_REVIEW.value,
            TaskState.REVIEWING.value,
        }
        by_owner: Dict[str, Task] = {}
        for state in active:
            for task in self.list_tasks(state, limit=200):
                if task.owner_agent_id:
                    by_owner.setdefault(task.owner_agent_id, task)
        members: List[JsonDict] = []
        for agent in self.list_agents():
            if exclude_agent_id and agent.id == exclude_agent_id:
                continue
            cur = by_owner.get(agent.id)
            members.append(
                {
                    "name": agent.name,
                    "agent_id": agent.id,
                    "status": agent.status,
                    "health": agent.health_status,
                    "current_task_id": cur.id if cur else agent.current_task_id,
                    "current_task_title": (cur.title if cur else None),
                    "last_seen_at": agent.last_seen_at,
                }
            )
            if len(members) >= limit:
                break
        return {
            "schema": "mac.fleet_snapshot.v1",
            "generated_at": utcnow(),
            "members": members,
        }

    def mark_stale_agents_offline(self, stale_after_seconds: int) -> List[Agent]:
        cutoff = (
            parse_time(utcnow()) - timedelta(seconds=max(1, int(stale_after_seconds)))
        ).isoformat(timespec="microseconds")
        rows = self.store.query_all(
            """
            SELECT * FROM agents
            WHERE status != ? AND last_seen_at <= ?
            ORDER BY last_seen_at, id
            """,
            (AgentStatus.OFFLINE.value, cutoff),
        )
        marked = []
        for row in rows:
            agent = self._agent_from_row(row)
            marked.append(self.heartbeat_agent(agent.id, status=AgentStatus.OFFLINE.value))
        return marked

    # Dispatcher

    def dispatch_once(
        self,
        lease_seconds: int = 900,
        skip_tenants: Optional[Iterable[str]] = None,
    ) -> Optional[JsonDict]:
        return self.dispatch.dispatch_once(
            lease_seconds=lease_seconds,
            skip_tenants=skip_tenants,
        )

    def _dispatch_once_impl(
        self,
        lease_seconds: int = 900,
        skip_tenants: Optional[Iterable[str]] = None,
        *,
        run_maintenance: bool = True,
    ) -> Optional[JsonDict]:
        assignments = self._dispatch_batch_impl(
            lease_seconds=lease_seconds,
            limit=1,
            skip_tenants=skip_tenants,
            run_maintenance=run_maintenance,
        )
        return assignments[0] if assignments else None

    def _dispatch_batch_impl(
        self,
        *,
        lease_seconds: int = 900,
        limit: int = 100,
        skip_tenants: Optional[Iterable[str]] = None,
        run_maintenance: bool = True,
    ) -> List[JsonDict]:
        limit_value = max(1, min(int(limit), 1000))
        if run_maintenance:
            self._expire_leases_sweep_page(limit=limit_value)
            self._unblock_ready_sweep_page(limit=limit_value)
        skipped = set(skip_tenants or [])
        tasks = [
            task
            for task in self._dispatch_ordered_tasks()
            if (self._task_tenant_id(task) or "") not in skipped
        ]
        agents = self._available_agents()
        unmatched: List[Task] = []
        assignments: List[JsonDict] = []
        for task in tasks:
            # Autonomous-dispatch gates: a per-task no_dispatch hold or a
            # project-level pause must keep the push dispatcher from auto-
            # claiming, exactly as they keep tasks out of ready_tasks() and the
            # worker-pull claim policy. claim_task() deliberately does NOT
            # enforce these (operators may still claim/start a staged task
            # explicitly), so the gate has to live on every autonomous path.
            if self._task_dispatch_held(task) or self._project_dispatch_paused(task.project):
                continue
            matched = False
            for agent in agents:
                if not self._agent_available_for(agent, task):
                    continue
                try:
                    claimed, lease = self.claim_task(task.id, agent.id, lease_seconds=lease_seconds)
                except (TransitionError, ValidationError):
                    # task was already claimed, exhausted attempts, or otherwise
                    # ineligible — try the next (task, agent) pair.
                    continue
                self.send_message(
                    "dispatcher",
                    agent.id,
                    MessageType.NUDGE.value,
                    {"task_id": claimed.id, "lease_id": lease.id, "reason": "assigned"},
                    task_id=claimed.id,
                )
                assignments.append(
                    {
                        "task": claimed.to_dict(),
                        "agent": agent.to_dict(),
                        "lease": lease.to_dict(),
                    }
                )
                matched = True
                break
            if not matched:
                unmatched.append(task)
            if len(assignments) >= limit_value:
                break
        # No agent could claim any pending task. Emit a provisioning
        # signal so a future provisioner (k8s operator, nomad job, local
        # spawner) can spin up the kind of agent that's missing. Today
        # the row + observability log are the signal; no auto-spawn.
        for task in unmatched:
            self._emit_dispatch_provisioning_signal(task)
        return assignments

    def _emit_dispatch_provisioning_signal(self, task: Task) -> None:
        required_role = None
        hardware: JsonDict = {}
        metadata = ensure_json_object(task.metadata)
        required_commands = _repository_required_commands_from_metadata(metadata)
        host_required_commands = _repository_host_required_commands_from_metadata(metadata)
        if isinstance(task.metadata, dict):
            md_role = task.metadata.get("required_role")
            if isinstance(md_role, str) and md_role.strip():
                required_role = md_role.strip()
            md_hw = task.metadata.get("hardware")
            if isinstance(md_hw, dict):
                hardware = md_hw
        self.provisioning.request_agent(
            reason="dispatch.no_eligible_agent",
            role_slug=required_role,
            capabilities=list(task.required_capabilities or []),
            hardware=hardware,
            task_id=task.id,
            tenant_id=self._task_tenant_id(task),
            detail={
                "task_state": task.state,
                "task_title": task.title,
                "required_commands": required_commands,
                "sandbox_host_required_commands": host_required_commands,
                "sandbox_required_commands": required_commands,
            },
        )

    def claim_next_for_agent(
        self,
        agent_id: str,
        lease_seconds: int = 900,
        allowed_projects: Optional[Iterable[str]] = None,
        required_metadata: Optional[Dict[str, Any]] = None,
        require_canary: bool = False,
        dry_run: bool = False,
        capabilities: Optional[Iterable[str]] = None,
        sync_beads: bool = True,
    ) -> Optional[JsonDict]:
        return self.dispatch.claim_next_for_agent(
            agent_id,
            lease_seconds=lease_seconds,
            allowed_projects=allowed_projects,
            required_metadata=required_metadata,
            require_canary=require_canary,
            dry_run=dry_run,
            capabilities=capabilities,
            sync_beads=sync_beads,
        )

    def _claim_next_for_agent_impl(
        self,
        agent_id: str,
        lease_seconds: int = 900,
        allowed_projects: Optional[Iterable[str]] = None,
        required_metadata: Optional[Dict[str, Any]] = None,
        require_canary: bool = False,
        dry_run: bool = False,
        capabilities: Optional[Iterable[str]] = None,
        sync_beads: bool = True,
    ) -> Optional[JsonDict]:
        """Claim the next dispatch-eligible task for one worker.

        This is the worker-side counterpart to dispatch_once(). It preserves
        the same capability, capacity, tenant, trust, and health checks while
        allowing a worker daemon to pull only work assigned to its own durable
        identity. Worker policy filters provide a quarantine lane for canaries:
        dry runs can inspect the next eligible task without leasing it, and
        loop-mode workers can refuse non-canary or out-of-project work before
        touching production tasks.
        """
        self._expire_leases_sweep_page(limit=100)
        self._unblock_ready_sweep_page(limit=100)
        agent = self.get_agent(agent_id)
        if not dry_run:
            assignment = self._active_assignment_for_agent(agent)
            if assignment is not None:
                task = assignment["task"]
                lease = assignment["lease"]
                self.record_log(
                    "worker.routing.resumed",
                    layer="control_plane",
                    source=agent.id,
                    subject_type="task",
                    subject_id=task["id"],
                    detail={
                        "agent_id": agent.id,
                        "task_id": task["id"],
                        "lease_id": lease["id"],
                        "task_state": task["state"],
                    },
                )
                return assignment
        policy = self._worker_claim_policy(
            allowed_projects=allowed_projects,
            required_metadata=required_metadata,
            require_canary=require_canary,
            dry_run=dry_run,
            capabilities=capabilities,
        )
        rejected_policy: Dict[str, int] = {}
        rejected_dispatch = 0
        considered = 0
        for task in self._dispatch_ordered_tasks():
            considered += 1
            allowed, reason = self._task_matches_worker_claim_policy(task, policy)
            if not allowed:
                rejected_policy[reason] = rejected_policy.get(reason, 0) + 1
                continue
            if not self._agent_available_for(agent, task):
                rejected_dispatch += 1
                continue
            detail = {
                "agent_id": agent.id,
                "task_id": task.id,
                "dry_run": dry_run,
                "policy": policy,
                "considered": considered,
                "rejected_policy": rejected_policy,
                "rejected_dispatch": rejected_dispatch,
            }
            if dry_run:
                self.record_log(
                    "worker.routing.dry_run_candidate",
                    layer="control_plane",
                    source=agent.id,
                    subject_type="task",
                    subject_id=task.id,
                    detail=detail,
                )
                return {
                    "task": task.to_dict(),
                    "agent": agent.to_dict(),
                    "lease": None,
                    "dry_run": True,
                    "policy": policy,
                }
            try:
                claimed, lease = self.claim_task(
                    task.id,
                    agent.id,
                    lease_seconds=lease_seconds,
                    sync_beads=sync_beads,
                )
            except (TransitionError, ValidationError):
                continue
            self.record_log(
                "worker.routing.claimed",
                layer="control_plane",
                source=agent.id,
                subject_type="task",
                subject_id=claimed.id,
                detail={**detail, "lease_id": lease.id},
            )
            self.send_message(
                "dispatcher",
                agent.id,
                MessageType.NUDGE.value,
                {"task_id": claimed.id, "lease_id": lease.id, "reason": "worker_claimed"},
                task_id=claimed.id,
            )
            return {"task": claimed.to_dict(), "agent": agent.to_dict(), "lease": lease.to_dict()}
        self.record_log(
            "worker.routing.no_candidate",
            level="debug",
            layer="control_plane",
            source=agent.id,
            detail={
                "agent_id": agent.id,
                "dry_run": dry_run,
                "policy": policy,
                "considered": considered,
                "rejected_policy": rejected_policy,
                "rejected_dispatch": rejected_dispatch,
            },
        )
        return None

    def _active_assignment_for_agent(self, agent: Agent) -> Optional[JsonDict]:
        """Return the authoritative assignment a loop worker must resume.

        Push dispatch claims the task before the worker's next polling turn.
        Looking only for OPEN work at claim-next time therefore loses the one
        assignment the worker already owns.  Resolve it from the active lease
        and task rows, preferring ``current_task_id`` when capacity permits an
        agent to hold more than one lease.
        """
        if agent.status not in {AgentStatus.IDLE.value, AgentStatus.BUSY.value}:
            return None
        row = self.store.query_one(
            """
            SELECT l.id AS lease_id, t.id AS task_id
            FROM leases l
            JOIN tasks t ON t.lease_id = l.id
            WHERE l.agent_id = ?
              AND l.status = ?
              AND t.owner_agent_id = ?
              AND t.state IN (?, ?)
            ORDER BY CASE WHEN t.id = ? THEN 0 ELSE 1 END,
                     l.created_at, l.id
            LIMIT 1
            """,
            (
                agent.id,
                LeaseStatus.ACTIVE.value,
                agent.id,
                TaskState.CLAIMED.value,
                TaskState.RUNNING.value,
                agent.current_task_id or "",
            ),
        )
        if row is None:
            return None
        task = self.get_task(str(row["task_id"]))
        lease = self.get_lease(str(row["lease_id"]))
        return {
            "task": task.to_dict(),
            "agent": self.get_agent(agent.id).to_dict(),
            "lease": lease.to_dict(),
            "resumed": True,
        }

    def tick(
        self,
        lease_seconds: int = 900,
        limit: int = 100,
        stale_after_seconds: Optional[int] = None,
    ) -> JsonDict:
        assignment_limit = max(0, min(int(limit), 1000))
        limit_value = max(1, assignment_limit)
        stale_agents = []
        if stale_after_seconds is not None:
            stale_agents = [
                agent.to_dict()
                for agent in self.mark_stale_agents_offline(stale_after_seconds)
            ]
        expired_page = self._expire_leases_sweep_page(limit=limit_value)
        expired = [task.to_dict() for task in expired_page["tasks"]]
        try:
            workflow_runs = [
                run.to_dict()
                for run in self.workflow_runtime.tick(
                    actor="dispatcher.tick",
                    limit=limit_value,
                )
            ]
        except Exception as exc:  # noqa: BLE001 - one workflow must not stop fleet dispatch.
            workflow_runs = []
            self.record_log(
                "workflow.recovery.failed",
                layer="control_plane",
                source="dispatcher.tick",
                level="error",
                detail={"error": str(exc)},
            )
        try:
            self.reconcile_service_roles()  # media-01: reap stale claims + signal zero-holder ops
        except Exception:  # noqa: BLE001 - reconcile must never break the tick
            pass
        unblocked_page = self._unblock_ready_sweep_page(limit=limit_value)
        review_workflows = self._advance_default_review_sweep_page(
            limit=limit_value,
            actor="default-review-workflow",
            tenant_id=None,
        )
        assignments = (
            self._dispatch_batch_impl(
                lease_seconds=lease_seconds,
                limit=assignment_limit,
                run_maintenance=False,
            )
            if assignment_limit
            else []
        )
        dead_letters_page = self.list_dead_letters_page(limit=limit_value)
        return {
            "stale_agents": stale_agents,
            "expired": expired,
            "workflow_runs": workflow_runs,
            "review_workflows": review_workflows,
            "assignments": assignments,
            "dead_letters": [
                task.to_dict() for task in dead_letters_page["tasks"]
            ],
            "maintenance": {
                "expired_leases_has_more": expired_page["has_more"],
                "blocked_tasks_has_more": unblocked_page["has_more"],
                "dead_letters_has_more": dead_letters_page["has_more"],
                "dead_letters_next_cursor": dead_letters_page["next_cursor"],
            },
        }

    # Communication bus

    # Agent control messages: thin facade over ``self.messaging``.

    def send_message(self, *args: Any, **kwargs: Any) -> AgentMessage:
        return self.messaging.send_message(*args, **kwargs)

    def get_message(self, message_id: str) -> AgentMessage:
        return self.messaging.get_message(message_id)

    def deliver_messages(self, *args: Any, **kwargs: Any) -> List[AgentMessage]:
        return self.messaging.deliver_messages(*args, **kwargs)

    def list_messages(self, *args: Any, **kwargs: Any) -> List[AgentMessage]:
        return self.messaging.list_messages(*args, **kwargs)

    # AgentBus typed content streams: thin facade over ``self.agentbus``.
    # New code should call ``cp.agentbus.<method>`` directly.

    def open_agentbus_stream(self, *args: Any, **kwargs: Any) -> AgentBusStream:
        return self.agentbus.open_stream(*args, **kwargs)

    def append_agentbus_chunk(self, *args: Any, **kwargs: Any) -> AgentBusChunk:
        return self.agentbus.append_chunk(*args, **kwargs)

    def close_agentbus_stream(self, *args: Any, **kwargs: Any) -> AgentBusStream:
        return self.agentbus.close_stream(*args, **kwargs)

    def get_agentbus_stream(self, stream_id: str) -> AgentBusStream:
        return self.agentbus.get_stream(stream_id)

    def list_agentbus_streams(self, *args: Any, **kwargs: Any) -> List[AgentBusStream]:
        return self.agentbus.list_streams(*args, **kwargs)

    def assert_agentbus_authorized(self, agent_id: str, stream_id: str) -> AgentBusStream:
        return self.agentbus.assert_authorized(agent_id, stream_id)

    def read_agentbus_chunks(self, *args: Any, **kwargs: Any) -> List[AgentBusChunk]:
        return self.agentbus.read_chunks(*args, **kwargs)

    def publish_agentbus_content(self, *args: Any, **kwargs: Any) -> JsonDict:
        return self.agentbus.publish(*args, **kwargs)

    def publish_agentbus_repo_update(
        self,
        sender_agent_id: str,
        recipient_agent_ids: Optional[List[str]] = None,
        *,
        all_agents: bool = False,
        repo_path: Optional[str] = None,
        remote: str = "origin",
        branch: str = "main",
        restart: bool = True,
        restart_services: Optional[List[str]] = None,
        request_id: Optional[str] = None,
    ) -> JsonDict:
        recipients = list(recipient_agent_ids or [])
        if all_agents:
            recipients.extend(agent.id for agent in self.list_agents())
        recipients = list(dict.fromkeys(item for item in recipients if item))
        if not recipients:
            raise ValidationError("repo-update requires recipient_agent_ids or all_agents=true")
        payload = repo_update_payload(
            repo_path=repo_path,
            remote=remote,
            branch=branch,
            restart=restart,
            restart_services=list(restart_services or []),
            request_id=request_id,
        )
        published = [
            self.publish_agentbus_content(
                sender_agent_id=sender_agent_id,
                recipient_agent_id=recipient_id,
                content_type=REPO_UPDATE_CONTENT_TYPE,
                topic=REPO_UPDATE_TOPIC,
                payload=payload,
            )
            for recipient_id in recipients
        ]
        return {
            "schema": "mac.agentbus.repo_update_publish.v1",
            "count": len(published),
            "streams": [item["stream"] for item in published],
        }

    def publish_agentbus_artifact(
        self,
        sender_agent_id: str,
        operation: str = "upsert",
        recipient_agent_ids: Optional[List[str]] = None,
        all_agents: bool = False,
        artifact_id: Optional[str] = None,
        digest: Optional[str] = None,
        kind: str = "public-artifact",
        uri: Optional[str] = None,
        public_url: Optional[str] = None,
        path: Optional[str] = None,
        publish_dir: Optional[str] = None,
        sbom_uri: Optional[str] = None,
        signers: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> JsonDict:
        self.get_agent(sender_agent_id)
        op = (operation or "upsert").strip().lower()
        if op == "create":
            op = "upsert"
        if op == "read":
            op = "get"
        if op not in {"upsert", "update", "get", "list", "delete"}:
            raise ValidationError("unsupported artifact publish operation: %s" % operation)

        configured_publish_dir = (
            publish_dir
            or os.environ.get("MAC_PUBLISH_DIR")
            or os.environ.get("MAC_WEBDAV_ROOT")
            or ""
        ).strip()
        configured_public_url = (
            public_url
            or os.environ.get("MAC_PUBLISH_PUBLIC_URL")
            or os.environ.get("MAC_PUBLISH_WEBDAV_URL")
            or os.environ.get("MAC_WEBDAV_PUBLIC_URL")
            or ""
        ).strip()
        if not public_url and configured_public_url and path:
            configured_public_url = "%s/%s" % (configured_public_url.rstrip("/"), path.strip("/"))
        else:
            configured_public_url = configured_public_url.rstrip("/")

        artifact: Optional[JsonDict] = None
        artifacts: Optional[List[JsonDict]] = None
        deleted: Optional[JsonDict] = None

        if op in {"upsert", "update"}:
            if not digest:
                raise ValidationError("artifact publish upsert requires digest")
            artifact_uri = (uri or configured_public_url or "").strip()
            if not artifact_uri:
                raise ValidationError("artifact publish upsert requires uri or public_url")
            merged_metadata = dict(metadata or {})
            if path:
                merged_metadata.setdefault("publish_path", path.strip("/"))
            if configured_publish_dir:
                merged_metadata.setdefault("publish_dir", configured_publish_dir)
            if configured_public_url:
                merged_metadata.setdefault("public_url", configured_public_url)
            artifact = self.register_artifact(
                kind,
                digest,
                artifact_uri,
                sender_agent_id,
                sbom_uri=sbom_uri,
                signers=signers or [],
                metadata=merged_metadata,
            ).to_dict()
        elif op == "get":
            key = artifact_id or digest
            if not key:
                raise ValidationError("artifact publish get requires artifact_id or digest")
            artifact = self.get_artifact(key).to_dict()
        elif op == "list":
            artifacts = [item.to_dict() for item in self.list_artifacts(kind or None)]
        elif op == "delete":
            key = artifact_id or digest
            if not key:
                raise ValidationError("artifact publish delete requires artifact_id or digest")
            deleted = self.delete_artifact(key, actor=sender_agent_id)
            artifact_value = deleted.get("artifact") if isinstance(deleted, dict) else None
            artifact = artifact_value if isinstance(artifact_value, dict) else None

        recipients = list(recipient_agent_ids or [])
        if all_agents:
            recipients.extend(agent.id for agent in self.list_agents())
        recipients = list(dict.fromkeys(item for item in recipients if item))
        streams: List[JsonDict] = []
        if recipients and op in {"upsert", "update", "delete"}:
            payload = artifact_publish_payload(
                operation=op,
                artifact=artifact,
                publish_dir=configured_publish_dir,
                public_url=configured_public_url,
                path=path,
                request_id=request_id,
            )
            streams = [
                self.publish_agentbus_content(
                    sender_agent_id=sender_agent_id,
                    recipient_agent_id=recipient_id,
                    content_type=ARTIFACT_PUBLISH_CONTENT_TYPE,
                    topic=ARTIFACT_PUBLISH_TOPIC,
                    payload=payload,
                    task_id=task_id,
                )["stream"]
                for recipient_id in recipients
            ]

        response: JsonDict = {
            "schema": "mac.agentbus.artifact_publish_crud.v1",
            "operation": op,
            "count": len(streams),
            "streams": streams,
        }
        if artifact is not None:
            response["artifact"] = artifact
        if artifacts is not None:
            response["artifacts"] = artifacts
        if deleted is not None:
            response["deleted"] = bool(deleted.get("deleted"))
        if configured_publish_dir:
            response["publish_dir"] = configured_publish_dir
        if configured_public_url:
            response["public_url"] = configured_public_url
        return response

    # Reviews + publication: thin facade over ``self.reviews``.

    def request_review(self, *args: Any, **kwargs: Any) -> Review:
        review = self.reviews.request_review(*args, **kwargs)
        actor = kwargs.get("actor")
        if actor is None and len(args) >= 3:
            actor = args[2]
        if actor is None:
            actor = "dispatcher"
        return review

    def claim_review(
        self,
        review_id: str,
        reviewer_agent_id: str,
        *,
        executor_evidence_id: Optional[str] = None,
        actor: str = "reviewer",
        sync_beads: bool = True,
    ) -> JsonDict:
        review = self.get_review(review_id)
        if review.reviewer_agent_id != reviewer_agent_id:
            raise AuthorizationError("reviewer does not own review")
        task = self.get_task(review.task_id)
        existing_claim = ensure_json_object(
            ensure_json_object(task.metadata).get("review_claims")
        ).get(review.id)
        if review.status != ReviewStatus.PENDING.value:
            return {
                "schema": "mac.review_claim.v1",
                "status": "not_claimable",
                "reason": "review_%s" % review.status,
                "review": review.to_dict(),
                "task": task.to_dict(),
                "claim": existing_claim if isinstance(existing_claim, dict) else None,
            }
        if isinstance(existing_claim, dict) and existing_claim.get(
            "reviewer_agent_id"
        ) not in {
            None,
            "",
            reviewer_agent_id,
        }:
            raise ValidationError(
                "review is already claimed by %s"
                % existing_claim.get("reviewer_agent_id")
            )
        evidence = None
        if executor_evidence_id:
            evidence = self.get_evidence(executor_evidence_id)
            if evidence.task_id != task.id:
                raise ValidationError("review claim evidence must belong to reviewed task")
        claim = self._review_claim_detail(task, review, evidence, actor=actor)
        now = utcnow()
        claim["claimed_at"] = now
        metadata = ensure_json_object(task.metadata)
        claims = ensure_json_object(metadata.get("review_claims"))
        # mem-05: idempotent re-claim. The verified 30,806-row storm was one
        # review re-claiming identical evidence over and over. A repeat claim
        # (same review + executor_evidence_id + head_sha) is now a no-op: it
        # returns the prior claim and writes no new task.review_claimed row.
        prior = ensure_json_object(claims.get(review.id))
        if (
            prior
            and str(prior.get("executor_evidence_id") or "") == str(claim.get("executor_evidence_id") or "")
            and str(prior.get("repository_head_sha") or "") == str(claim.get("repository_head_sha") or "")
        ):
            refreshed = self.get_task(task.id)
            return {
                "schema": "mac.review_claim.v1",
                "status": "claimed",
                "review": review.to_dict(),
                "task": refreshed.to_dict(),
                "claim": prior,
                "idempotent": True,
            }
        claims[review.id] = claim
        metadata["review_claims"] = claims
        metadata["latest_review_claim"] = claim
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                (json_dumps(metadata), now, task.id),
            )
            conn.execute(
                """
                UPDATE agents
                SET status = ?, current_task_id = ?, updated_at = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (AgentStatus.BUSY.value, task.id, now, now, reviewer_agent_id),
            )
            self._record_history(
                task.id,
                "task.review_claimed",
                reviewer_agent_id,
                None,
                None,
                claim,
                conn=conn,
            )
        refreshed = self.get_task(task.id)
        return {
            "schema": "mac.review_claim.v1",
            "status": "claimed",
            "review": review.to_dict(),
            "task": refreshed.to_dict(),
            "claim": claim,
        }


    def _review_claim_detail(
        self,
        task: Task,
        review: Review,
        evidence: Optional[Evidence],
        *,
        actor: str,
    ) -> JsonDict:
        verification = ensure_json_object(
            evidence.metadata.get("verification") if evidence is not None else {}
        )
        repo = ensure_json_object(verification.get("repo"))
        tests = (
            verification.get("tests")
            if isinstance(verification.get("tests"), list)
            else []
        )
        checks = (
            verification.get("checks")
            if isinstance(verification.get("checks"), list)
            else []
        )
        runtime = ensure_json_object(ensure_json_object(task.metadata).get("runtime"))
        return {
            "schema": "mac.review_claim.detail.v1",
            "actor": actor,
            "task_id": task.id,
            "task_title": task.title,
            "project": task.project,
            "review_id": review.id,
            "reviewer_agent_id": review.reviewer_agent_id,
            "executor_evidence_id": evidence.id if evidence is not None else None,
            "work_summary": evidence.summary if evidence is not None else "",
            "evidence_type": verification.get("evidence_type"),
            "repository_worktree": (
                repo.get("path")
                or runtime.get("repository_worktree")
                or repo.get("worktree")
                or ""
            ),
            "repository_branch": repo.get("branch")
            or runtime.get("repository_branch")
            or "",
            "repository_head_sha": repo.get("head_sha") or "",
            "repository_remote_ref": repo.get("remote_ref") or "",
            "repository_files_changed": (
                repo.get("files_changed")
                if isinstance(repo.get("files_changed"), list)
                else []
            ),
            "checks": checks,
            "tests": tests,
        }

    def submit_review(self, *args: Any, **kwargs: Any) -> Review:
        review = self.reviews.submit_review(*args, **kwargs)
        reviewer_agent_id = kwargs.get("reviewer_agent_id")
        if reviewer_agent_id is None and len(args) >= 3:
            reviewer_agent_id = args[2]
        if reviewer_agent_id is None:
            reviewer_agent_id = review.reviewer_agent_id
        current = self.get_agent(str(reviewer_agent_id))
        if current.current_task_id == review.task_id:
            self._set_agent_idle(str(reviewer_agent_id))
        # Glanceable per-task narrative: record what the reviewer concluded.
        # Only real verdicts (not pending/retracted churn); best-effort.
        try:
            verdict = str(review.status or "").strip()
            if verdict.lower() in {"approved", "rejected", "changes_requested", "changes"}:
                reason = str(getattr(review, "reason", "") or "").strip()
                self.append_task_activity(
                    review.task_id,
                    "review",
                    str(reviewer_agent_id),
                    "%s — %s" % (verdict, reason) if reason else verdict,
                )
        except Exception:  # noqa: BLE001 - narrative is best-effort
            pass
        return review

    def get_review(self, review_id: str) -> Review:
        return self.reviews.get_review(review_id)

    def list_reviews(
        self,
        task_id: str,
        limit: Optional[int] = None,
    ) -> List[Review]:
        return self.reviews.list_reviews(task_id, limit=limit)

    def publish_task(self, *args: Any, **kwargs: Any) -> Publication:
        task_id = kwargs.get("task_id") if "task_id" in kwargs else (args[0] if args else None)
        target = kwargs.get("target") if "target" in kwargs else (args[1] if len(args) >= 2 else None)
        evidence_id = kwargs.get("evidence_id")
        if evidence_id is None and len(args) >= 4:
            evidence_id = args[3]
        if task_id is not None:
            self._validate_publication_evidence(str(task_id), evidence_id)
        git_publication = None
        if task_id is not None and target is not None and evidence_id is not None:
            git_publication = self._publish_git_target_if_needed(
                str(task_id),
                str(target),
                str(evidence_id),
            )
        publication = self.reviews.publish_task(*args, **kwargs)
        if git_publication is not None:
            self.record_log(
                "task.git_published",
                layer="control_plane",
                source=publication.created_by,
                subject_type="task",
                subject_id=publication.task_id,
                detail={**git_publication, "publication_id": publication.id},
            )
        # publish_task transitions the underlying task to COMPLETED inside
        # its own transaction (bypassing transition_task), so we run the
        # workflow runtime hook here so workflow runs advance on publish.
        row = self.store.query_one(
            "SELECT workflow_run_id FROM tasks WHERE id = ?", (publication.task_id,)
        )
        if row is not None and row["workflow_run_id"]:
            try:
                self.workflow_runtime.on_task_completed(
                    publication.task_id, TaskState.COMPLETED.value
                )
            except Exception:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).exception(
                    "workflow runtime failed to advance after publish_task"
                )
        return publication

    def _publish_git_target_if_needed(
        self,
        task_id: str,
        target: str,
        evidence_id: str,
    ) -> Optional[JsonDict]:
        if target not in {"git://main", "git://origin/main"}:
            return None
        task = self.get_task(task_id)
        metadata = ensure_json_object(task.metadata)
        origin = ensure_json_object(metadata.get("origin"))
        repo_path_raw = str(origin.get("repository_path") or "").strip()
        repository_url = str(origin.get("repository_url") or "").strip()
        # mac-k8s: remote-clone tasks (jordanh-gke and any K8s fleet) have no
        # local repository_path on the hub. Rather than refuse to publish, merge
        # via a transient authed clone of the remote so K8s-mode work can reach
        # main. The clone is cleaned up before returning (best-effort on errors).
        # Read the executor evidence first: its repo block records the remote the
        # worker actually pushed the task branch to, which lets us publish a
        # local-repo task (origin has only an agent-side repository_path that does
        # not exist on the hub) by cloning that remote.
        evidence = self.get_evidence(evidence_id)
        manifest = ensure_json_object(evidence.metadata.get("verification"))
        repo = ensure_json_object(manifest.get("repo"))
        head_sha = str(repo.get("head_sha") or "").strip()
        if not _GIT_SHA_RE.match(head_sha):
            raise ValidationError("git publication requires evidence repo.head_sha")
        remote_ref = str(repo.get("remote_ref") or "").strip()
        source_branch = _remote_branch_from_ref(remote_ref)
        if not source_branch:
            raise ValidationError("git publication requires branch-like repo.remote_ref")

        # Resolve a hub-usable repo: prefer a local path that exists on the hub,
        # else clone a remote URL — origin's, or (for local-repo tasks) the remote
        # the worker pushed to, recorded in the evidence.
        tmp_clone: Optional[Path] = None
        local_path: Optional[Path] = None
        if repo_path_raw:
            candidate = Path(repo_path_raw).expanduser()
            if candidate.exists():
                local_path = candidate
        clone_url = repository_url or str(
            repo.get("remote_url") or repo.get("push_remote") or ""
        ).strip()
        if local_path is not None:
            repo_path = local_path
        elif clone_url:
            from . import gitops as _gitops

            # inject_git_remote_auth now normalizes an SSH-form remote to
            # token-https when a token exists (single source of truth), so the
            # hub publish/merge, the worker fetch, and the finalizer push all
            # behave identically — no per-call-site SSH handling.
            auth_url = _gitops.inject_git_remote_auth(clone_url)
            tmp_clone = Path(tempfile.mkdtemp(prefix="mac-publish-"))
            clone = self._git_output(
                tmp_clone, ["clone", "--branch", "main", "--", auth_url, "."], timeout=240
            )
            if clone["returncode"] != 0:
                shutil.rmtree(tmp_clone, ignore_errors=True)
                raise ValidationError(
                    "git publication could not clone remote for merge: %s"
                    % (clone.get("stderr") or clone.get("stdout") or clone_url)
                )
            repo_path = tmp_clone
        else:
            raise ValidationError(
                "git publication requires a hub-reachable repo: origin.repository_url, "
                "a hub-local origin.repository_path, or evidence repo.remote_url"
            )

        top = self._git_output(repo_path, ["rev-parse", "--show-toplevel"])
        if top["returncode"] != 0 or not top.get("stdout"):
            raise ValidationError(
                "git publication repository path is not a git worktree: %s" % repo_path
            )
        root = Path(str(top["stdout"])).expanduser()
        dirty = self._git_output(root, ["status", "--porcelain"])
        if dirty["returncode"] != 0:
            raise ValidationError(
                "git publication could not inspect worktree: %s"
                % (dirty.get("stderr") or dirty.get("stdout") or root)
            )
        if dirty.get("stdout"):
            raise ValidationError("git publication requires clean worktree: %s" % root)

        # mac-y7ha: when the task's registered project pins a canonical
        # remote URL, refuse to publish unless the worktree's origin
        # actually points there. Without this check, a worktree cloned
        # from a private mirror happily accepts ``git push origin main``
        # and the publication record claims a merge into main that
        # nothing downstream of the mirror will ever see. URL forms are
        # canonicalised to ``(host, path)`` so equivalent ssh/https
        # variants match.
        contract = ensure_json_object(origin.get("repository_contract"))
        canonical_remote_url = str(contract.get("canonical_remote_url") or "").strip()
        if canonical_remote_url:
            origin_url_probe = self._git_output(root, ["remote", "get-url", "origin"])
            if origin_url_probe["returncode"] != 0:
                raise ValidationError(
                    "git publication could not read worktree origin: %s"
                    % (origin_url_probe.get("stderr") or origin_url_probe.get("stdout") or root)
                )
            worktree_origin = str(origin_url_probe.get("stdout") or "").strip()
            expected = _canonicalize_git_url(canonical_remote_url)
            actual = _canonicalize_git_url(worktree_origin)
            if expected is None or actual is None or expected != actual:
                raise ValidationError(
                    "git publication worktree origin %r does not match the project's "
                    "registered remote %r"
                    % (worktree_origin, canonical_remote_url)
                )

        commands: List[JsonDict] = []

        def git_step(
            name: str,
            args: List[str],
            timeout: int = 120,
            *,
            check: bool = True,
        ) -> JsonDict:
            result = self._git_output(root, args, timeout=timeout)
            commands.append({"name": name, "args": args, **result})
            if check and result["returncode"] != 0:
                raise ValidationError(
                    "git publication %s failed: %s"
                    % (name, result.get("stderr") or result.get("stdout") or args)
                )
            return result

        run_step = git_step

        run_step("fetch_main", ["fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"])
        run_step(
            "fetch_source",
            [
                "fetch",
                "origin",
                "+refs/heads/%s:refs/remotes/origin/%s" % (source_branch, source_branch),
            ],
        )
        checkout = self._git_output(root, ["checkout", "main"])
        commands.append({"name": "checkout_main", "args": ["checkout", "main"], **checkout})
        if checkout["returncode"] != 0:
            run_step("create_main", ["checkout", "-B", "main", "origin/main"])
        run_step("pull_main", ["pull", "--ff-only", "origin", "main"])
        run_step("verify_commit", ["cat-file", "-e", "%s^{commit}" % head_sha])
        publication_mode = "fast_forward"
        ff_merge = git_step("merge_source_ff", ["merge", "--ff-only", head_sha], check=False)
        if ff_merge["returncode"] != 0:
            already_merged = git_step(
                "source_already_merged",
                ["merge-base", "--is-ancestor", head_sha, "HEAD"],
                check=False,
            )
            if already_merged["returncode"] == 0:
                publication_mode = "already_integrated"
            else:
                publication_mode = "merge_commit"
                merge = git_step(
                    "merge_source",
                    ["merge", "--no-ff", "--no-edit", head_sha],
                    timeout=180,
                    check=False,
                )
                if merge["returncode"] != 0:
                    git_step("merge_abort", ["merge", "--abort"], check=False)
                    raise ValidationError(
                        "git publication merge_source failed: %s"
                        % (merge.get("stderr") or merge.get("stdout") or head_sha)
                    )
        run_step("push_main", ["push", "origin", "main"], timeout=180)
        final_head = run_step("final_head", ["rev-parse", "HEAD"])
        final_sha = str(final_head.get("stdout") or "").strip()
        contains_source = git_step(
            "verify_source_ancestor",
            ["merge-base", "--is-ancestor", head_sha, final_sha],
            check=False,
        )
        if contains_source["returncode"] != 0:
            raise ValidationError(
                "git publication final main %s does not contain reviewed commit %s"
                % (final_sha, head_sha)
            )
        if tmp_clone is not None:
            shutil.rmtree(tmp_clone, ignore_errors=True)
        return {
            "status": "published",
            "target": target,
            "repository_path": str(root),
            "source_branch": source_branch,
            "remote_ref": remote_ref,
            "head_sha": head_sha,
            "final_sha": final_sha,
            "publication_mode": publication_mode,
            "commands": commands,
        }

    def _validate_publication_evidence(self, task_id: str, evidence_id: Optional[str]) -> None:
        if evidence_id is None:
            raise ValidationError("publication requires evidence")
        task = self.get_task(task_id)
        evidence = self.get_evidence(str(evidence_id))
        if evidence.task_id != task_id:
            raise ValidationError("publication evidence must belong to task")
        if self.reviews.task_requires_publication_evidence(task):
            if evidence.kind != "publication":
                raise ValidationError("publication policy requires publication evidence")
            if not evidence.checksum:
                raise ValidationError("publication evidence requires a checksum")
            review_problems = self._publication_review_executor_problems(task_id)
            if review_problems:
                raise ValidationError(
                    "publication review evidence is not verifiable: %s"
                    % ", ".join(review_problems)
                )
            return
        assessment = self._assess_default_review_evidence(task, evidence)
        if not assessment.get("valid"):
            raise ValidationError(
                "publication evidence is not verifiable: %s"
                % ", ".join(str(item) for item in assessment.get("problems", []))
            )
        review_problems = self._publication_review_problems(task_id, evidence.id)
        if review_problems:
            raise ValidationError(
                "publication review evidence is not verifiable: %s"
                % ", ".join(review_problems)
            )

    def _publication_review_executor_problems(self, task_id: str) -> List[str]:
        task = self.get_task(task_id)
        approved = [
            review
            for review in self.list_reviews(task_id)
            if review.status == ReviewStatus.APPROVED.value
        ]
        if not approved:
            return ["publication requires an approved review"]
        problems: List[str] = []
        for review in approved:
            if not review.evidence_id:
                problems.append("review %s lacks review evidence" % review.id)
                continue
            try:
                verdict = self.get_evidence(review.evidence_id)
            except NotFoundError:
                problems.append("review %s references missing evidence" % review.id)
                continue
            manifest = verdict.metadata.get("verification")
            if not isinstance(manifest, dict):
                problems.append("review %s evidence lacks verification manifest" % review.id)
                continue
            executor_evidence_id = str(manifest.get("reviewed_evidence_id") or "").strip()
            if not executor_evidence_id:
                problems.append("review %s verdict lacks reviewed_evidence_id" % review.id)
                continue
            try:
                executor_evidence = self.get_evidence(executor_evidence_id)
            except NotFoundError:
                problems.append("review %s references missing executor evidence" % review.id)
                continue
            assessment = self._assess_default_review_evidence(task, executor_evidence)
            if not assessment.get("valid"):
                problems.append(
                    "review %s executor evidence is not verifiable: %s"
                    % (
                        review.id,
                        ", ".join(str(item) for item in assessment.get("problems", [])),
                    )
                )
                continue
            verdict_evidence, verdict_problems = self._find_review_verdict_evidence(
                task_id,
                review.reviewer_agent_id,
                executor_evidence_id=executor_evidence.id,
                verdict_evidence_id=review.evidence_id,
                not_before=review.created_at,
            )
            if verdict_evidence is not None and verdict_evidence.id == review.evidence_id:
                if self._verdict_value(verdict_evidence) == "approved":
                    return []
                problems.append("review %s verdict is not approved" % review.id)
                continue
            problems.append(
                "review %s lacks verifiable signed review_verdict evidence" % review.id
            )
            problems.extend(verdict_problems[:5])
        return problems

    def _publication_review_problems(self, task_id: str, executor_evidence_id: str) -> List[str]:
        approved = [
            review
            for review in self.list_reviews(task_id)
            if review.status == ReviewStatus.APPROVED.value
        ]
        if not approved:
            return ["publication requires an approved review"]
        problems: List[str] = []
        for review in approved:
            verdict_evidence, verdict_problems = self._find_review_verdict_evidence(
                task_id,
                review.reviewer_agent_id,
                executor_evidence_id=executor_evidence_id,
                verdict_evidence_id=review.evidence_id,
                not_before=review.created_at,
            )
            if verdict_evidence is not None and verdict_evidence.id == review.evidence_id:
                if self._verdict_value(verdict_evidence) == "approved":
                    return []
                problems.append("review %s verdict is not approved" % review.id)
                continue
            problems.append(
                "review %s lacks verifiable signed review_verdict evidence" % review.id
            )
            problems.extend(verdict_problems[:5])
        return problems

    def get_publication(self, publication_id: str) -> Publication:
        return self.reviews.get_publication(publication_id)

    def list_publications(self, *args: Any, **kwargs: Any) -> List[Publication]:
        return self.reviews.list_publications(*args, **kwargs)

    def _advance_default_review_sweep_page(
        self,
        *,
        limit: int,
        actor: str,
        tenant_id: Optional[str],
    ) -> JsonDict:
        """Advance one database-coordinated cursor page for autonomous callers."""
        claim = self.reconciliation.claim("default-review-sweep")
        if claim is None:
            return {
                "processed": 0,
                "results": [],
                "next_cursor": None,
                "has_more": False,
                "skipped": "lease_held",
            }
        try:
            result = self.advance_default_review_workflows(
                limit=limit,
                actor=actor,
                tenant_id=tenant_id,
                cursor=claim.cursor,
            )
        except Exception:
            self.reconciliation.abandon(claim)
            raise
        self.reconciliation.complete(
            claim,
            cursor=result.get("next_cursor"),
        )
        return result

    def advance_default_review_workflows(
        self,
        limit: int = 100,
        actor: str = "default-review-workflow",
        tenant_id: Optional[str] = None,
        cursor: Optional[str] = None,
    ) -> JsonDict:
        """Sweep one bounded, state-filtered page of reviewable tasks.

        ``cursor`` is the opaque ``next_cursor`` from a prior response.
        Autonomous callers persist it in ``reconciliation_state`` so tasks
        waiting on a verdict cannot starve later reviewable rows indefinitely.
        """
        limit_value = max(1, min(int(limit), 1000))
        clauses = ["state IN ('needs_review', 'reviewing')"]
        params: List[Any] = []
        if tenant_id is not None:
            clauses.append(
                "COALESCE("
                "NULLIF(json_extract(metadata, '$.origin.tenant_id'), ''), "
                "NULLIF(json_extract(metadata, '$.tenant_id'), '')"
                ") = ?"
            )
            params.append(tenant_id)
        if cursor is not None:
            priority, created_at, task_id = self._decode_review_sweep_cursor(cursor)
            clauses.append(
                "(priority < ? OR "
                "(priority = ? AND created_at > ?) OR "
                "(priority = ? AND created_at = ? AND id > ?))"
            )
            params.extend(
                [priority, priority, created_at, priority, created_at, task_id]
            )
        params.append(limit_value + 1)
        rows = self.store.query_all(
            "SELECT * FROM tasks INDEXED BY idx_tasks_review_queue WHERE %s "
            "ORDER BY priority DESC, created_at, id LIMIT ?"
            % " AND ".join(clauses),
            tuple(params),
        )
        has_more = len(rows) > limit_value
        tasks = [self._task_from_row(row) for row in rows[:limit_value]]
        results = [
            self.advance_default_review_workflow(task.id, actor=actor)
            for task in tasks
        ]
        next_cursor = (
            self._encode_review_sweep_cursor(tasks[-1])
            if has_more and tasks
            else None
        )
        return {
            "processed": len(results),
            "results": results,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    def _encode_review_sweep_cursor(self, task: Task) -> str:
        payload = json_dumps(
            {
                "priority": int(task.priority),
                "created_at": task.created_at,
                "task_id": task.id,
            }
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return "v1:%s" % encoded

    def _decode_review_sweep_cursor(self, cursor: str) -> Tuple[int, str, str]:
        raw = str(cursor or "").strip()
        if not raw.startswith("v1:"):
            raise ValidationError("invalid review sweep cursor")
        encoded = raw[3:]
        try:
            padding = "=" * (-len(encoded) % 4)
            payload = json_loads(
                base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode(
                    "utf-8"
                ),
                {},
            )
            priority = int(payload["priority"])
            created_at = str(payload["created_at"])
            task_id = str(payload["task_id"])
        except (binascii.Error, KeyError, TypeError, ValueError, UnicodeError):
            raise ValidationError("invalid review sweep cursor") from None
        if not created_at or not task_id:
            raise ValidationError("invalid review sweep cursor")
        return priority, created_at, task_id

    def advance_default_review_workflow(
        self,
        task_id: str,
        actor: str = "default-review-workflow",
    ) -> JsonDict:
        task = self.get_task(task_id)
        if task.state == TaskState.COMPLETED.value:
            return {"task_id": task_id, "status": "already_completed"}
        if task.state not in {TaskState.NEEDS_REVIEW.value, TaskState.REVIEWING.value}:
            return {"task_id": task_id, "status": "not_reviewable", "state": task.state}
        existing_publications = [
            publication
            for publication in self.list_publications(task_id)
            if publication.status == PublicationStatus.PUBLISHED.value
        ]
        if existing_publications:
            return {
                "task_id": task_id,
                "status": "already_published",
                "publication_id": existing_publications[-1].id,
            }
        if self._default_review_disabled(task):
            self._record_default_review_observation(
                task_id,
                "workflow.default_review.skipped",
                "info",
                {"reason": "disabled_by_task_policy"},
                actor,
            )
            return {"task_id": task_id, "status": "disabled_by_task_policy"}

        evidence, evidence_assessment = self._bound_review_evidence(task)
        if evidence is None:
            self._record_default_review_observation(
                task_id,
                "workflow.default_review.waiting",
                "warning",
                evidence_assessment,
                actor,
            )
            return {
                "task_id": task_id,
                "status": "waiting_for_verifiable_evidence",
                **evidence_assessment,
            }

        # If the task has more than one pending review, refuse to act —
        # the ambiguous state has no clear winner and the autonomous
        # swarm shouldn't silently pick one (mac-d9c).
        pending_reviews = [
            r for r in self.list_reviews(task_id)
            if r.status == ReviewStatus.PENDING.value
        ]
        pending_reviews = self._dedupe_same_reviewer_pending_reviews(
            pending_reviews,
            actor,
        )
        if len(pending_reviews) > 1:
            self._record_default_review_observation(
                task_id,
                "workflow.default_review.ambiguous",
                "warning",
                {
                    "reason": "multiple_pending_reviews",
                    "pending_review_ids": [r.id for r in pending_reviews],
                },
                actor,
            )
            return {
                "task_id": task_id,
                "status": "ambiguous_pending_reviews",
                "pending_review_ids": [r.id for r in pending_reviews],
            }

        review = self._default_review_for_task(task_id)
        if review is not None and review.status == ReviewStatus.PENDING.value:
            reviewer_issue = self._default_reviewer_unavailable_reason_for_id(
                task,
                review.reviewer_agent_id,
                executor_agent_id=evidence.created_by,
            )
            if reviewer_issue is not None:
                self._retract_default_review(
                    review,
                    actor,
                    "reviewer_unavailable:%s" % reviewer_issue,
                )
                self._record_default_review_observation(
                    task_id,
                    "workflow.default_review.retracted",
                    "warning",
                    {
                        "review_id": review.id,
                        "reviewer_agent_id": review.reviewer_agent_id,
                        "reason": reviewer_issue,
                    },
                    actor,
                )
                review = None
        if review is None:
            # mem-12: bound review retraction. Before creating a fresh
            # review for the same executor evidence, count how many
            # reviews for this task have already retracted *since the
            # latest evidence was recorded*. If we've hit the cap, block
            # the task for repair — looping forever is what bit task_d7c51a0b
            # with 503 retracted reviews in the original incident.
            try:
                retraction_cap = int(os.environ.get("MAC_REVIEW_RETRACTION_CAP", "3"))
            except ValueError:
                retraction_cap = 3
            # mem-12 window fix: scope the retraction count to "retractions
            # since the work under review was submitted" — i.e. the executor
            # evidence we are actually reviewing (already resolved above as
            # ``evidence``). The original query took the latest evidence of
            # ANY kind, so the reviewer's own ``review``-kind attempt evidence
            # advanced the window every cycle and the cap never tripped — the
            # bug behind the 2026-06 review runaway. Genuine rework still
            # resets the window because new executor evidence becomes the
            # reviewed ``evidence`` on the next advance.
            threshold_at = evidence.created_at or ""
            retracted_count_row = self.store.query_one(
                """
                SELECT COUNT(*) AS n FROM reviews
                WHERE task_id = ? AND status = ?
                  AND created_at >= ?
                """,
                (task_id, ReviewStatus.RETRACTED.value, threshold_at),
            )
            retracted_count = (
                int(retracted_count_row["n"]) if retracted_count_row else 0
            )
            if retracted_count >= retraction_cap:
                self._record_default_review_observation(
                    task_id,
                    "workflow.default_review.exhausted",
                    "error",
                    {
                        "reason": "review_retraction_cap_hit",
                        "cap": retraction_cap,
                        "retracted_count": retracted_count,
                        "executor_evidence_id": evidence.id,
                    },
                    actor,
                )
                self._record_history(
                    task_id,
                    "task.review_exhausted",
                    actor,
                    None,
                    None,
                    {
                        "cap": retraction_cap,
                        "retracted_count": retracted_count,
                        "executor_evidence_id": evidence.id,
                    },
                )
                try:
                    self.transition_task(
                        task_id,
                        TaskState.BLOCKED.value,
                        actor,
                        {
                            "reason": "review_retraction_cap_hit",
                            "manual_repair_required": True,
                            "cap": retraction_cap,
                            "retracted_count": retracted_count,
                            "executor_evidence_id": evidence.id,
                        },
                    )
                except TransitionError:
                    # Already terminal or otherwise moved: nothing more to do.
                    pass
                return {
                    "task_id": task_id,
                    "status": "review_retraction_exhausted",
                    "cap": retraction_cap,
                    "retracted_count": retracted_count,
                }
            reviewer = self._select_default_reviewer(
                task,
                executor_agent_id=evidence.created_by,
            )
            if reviewer is None:
                self._record_default_review_observation(
                    task_id,
                    "workflow.default_review.waiting",
                    "warning",
                    {"reason": "no_eligible_reviewer"},
                    actor,
                )
                # Signal that the swarm needs a reviewer-capable agent it
                # doesn't have. The default-review workflow will pick the
                # request up on a future tick once the provisioner has
                # registered a matching agent.
                self.provisioning.request_agent(
                    reason="review.no_eligible_reviewer",
                    capabilities=["review"],
                    task_id=task_id,
                    tenant_id=self._task_tenant_id(task),
                    detail={
                        "evidence_type": evidence_assessment.get("evidence_type"),
                    },
                )
                return {"task_id": task_id, "status": "waiting_for_reviewer"}
            review = self.request_review(task_id, reviewer.id, actor=actor)
            self._record_default_review_observation(
                task_id,
                "workflow.default_review.assigned",
                "info",
                {"review_id": review.id, "reviewer_agent_id": reviewer.id},
                actor,
            )

        if review.status == ReviewStatus.PENDING.value:
            # mac-jqb: the workflow no longer self-approves. It requires
            # the reviewer agent to have produced a *review verdict*
            # evidence row — a separate, signed manifest authored by
            # the reviewer (not the executor) declaring approve/reject.
            # Until that exists, the review stays pending. This makes
            # the second-eyes role actually do work; today the workflow
            # waits for the verdict, and a follow-up review-executor
            # worker will produce it automatically.
            verdict_evidence, verdict_problems = self._find_review_verdict_evidence(
                task_id,
                review.reviewer_agent_id,
                executor_evidence_id=evidence.id,
                not_before=review.created_at,
            )
            # Option C — hub-side verification. Instead of dispatching a nudge
            # and waiting for a reviewer agent to independently clone + run the
            # contract test (fragile: every reviewer node needs a working dev
            # environment, in-sandbox and host, and the sandbox->host handoff
            # to be perfect), the hub runs the contract test ONCE in a
            # controlled OpenShell sandbox on the pushed branch and records the
            # signed verdict on the selected reviewer's behalf. Second-eyes
            # holds (the verdict is signed by a non-author agent); the fragile
            # N-node verification collapses to one controlled environment.
            if verdict_evidence is None and _hub_review_verify_enabled():
                self._run_hub_review_verification(task, review, evidence, actor)
                verdict_evidence, verdict_problems = self._find_review_verdict_evidence(
                    task_id,
                    review.reviewer_agent_id,
                    executor_evidence_id=evidence.id,
                    not_before=review.created_at,
                )
            if verdict_evidence is None:
                # Bound the verdict-wait loop. mem-12 only caps RETRACTION;
                # a reviewer that keeps producing review-attempt evidence but
                # never a valid signed verdict would otherwise spin here
                # forever, re-nudging every tick (task_5de06b: 59 review-kind
                # evidence rows, 0 verdict — the live half of the 2026-06
                # runaway). Past a cap, block the task instead of re-nudging.
                try:
                    verdict_wait_cap = int(
                        os.environ.get("MAC_REVIEW_VERDICT_WAIT_CAP", "6")
                    )
                except ValueError:
                    verdict_wait_cap = 6
                wait_count_row = self.store.query_one(
                    """
                    SELECT COUNT(*) AS n FROM evidence
                    WHERE task_id = ? AND kind = 'review'
                      AND created_at >= ?
                    """,
                    (task_id, review.created_at),
                )
                wait_count = int(wait_count_row["n"]) if wait_count_row else 0
                if verdict_wait_cap > 0 and wait_count >= verdict_wait_cap:
                    self._record_default_review_observation(
                        task_id,
                        "workflow.default_review.exhausted",
                        "error",
                        {
                            "reason": "review_verdict_wait_cap_hit",
                            "cap": verdict_wait_cap,
                            "wait_count": wait_count,
                            "review_id": review.id,
                            "reviewer_agent_id": review.reviewer_agent_id,
                        },
                        actor,
                    )
                    self._record_history(
                        task_id,
                        "task.review_exhausted",
                        actor,
                        None,
                        None,
                        {
                            "reason": "review_verdict_wait_cap_hit",
                            "cap": verdict_wait_cap,
                            "wait_count": wait_count,
                            "review_id": review.id,
                        },
                    )
                    try:
                        self.transition_task(
                            task_id,
                            TaskState.BLOCKED.value,
                            actor,
                            {
                                "reason": "review_verdict_wait_cap_hit",
                                "manual_repair_required": True,
                                "cap": verdict_wait_cap,
                                "wait_count": wait_count,
                                "review_id": review.id,
                            },
                        )
                    except TransitionError:
                        pass
                    return {
                        "task_id": task_id,
                        "status": "review_verdict_wait_exhausted",
                        "cap": verdict_wait_cap,
                        "wait_count": wait_count,
                        "review_id": review.id,
                    }
                self._record_default_review_observation(
                    task_id,
                    "workflow.default_review.waiting_for_verdict",
                    "warning",
                    {
                        "review_id": review.id,
                        "reviewer_agent_id": review.reviewer_agent_id,
                        "evidence_id": evidence.id,
                        "problems": verdict_problems,
                    },
                    actor,
                )
                nudge = self._ensure_review_verdict_nudge(task_id, review, evidence)
                return {
                    "task_id": task_id,
                    "status": "waiting_for_reviewer_verdict",
                    "review_id": review.id,
                    "reviewer_agent_id": review.reviewer_agent_id,
                    "executor_evidence_id": evidence.id,
                    "problems": verdict_problems,
                    "nudge_id": nudge.id if nudge is not None else None,
                    "nudge_status": "queued" if nudge is not None else "already_queued",
                }
            verdict_value = self._verdict_value(verdict_evidence)
            if verdict_value == "rejected":
                review = self.submit_review(
                    review.id,
                    ReviewStatus.REJECTED.value,
                    review.reviewer_agent_id,
                    reason="reviewer rejected via signed verdict evidence",
                    evidence_id=verdict_evidence.id,
                )
                self._record_default_review_observation(
                    task_id,
                    "workflow.default_review.rejected",
                    "warning",
                    {
                        "review_id": review.id,
                        "reviewer_agent_id": review.reviewer_agent_id,
                        "verdict_evidence_id": verdict_evidence.id,
                    },
                    actor,
                )
                # Distill the rejection into a durable, project-scoped lesson so
                # the next execution run on this project recalls it (the review
                # branch never wrote a deployment_learning record, so rejected
                # work taught the fleet nothing — a real learn-from-bad gap).
                self._record_project_failure_lesson(
                    task_id,
                    evidence_type="review_verdict",
                    error_signature="review_rejected",
                    signals={"review_rejected": True, "problems": list(verdict_problems or [])[:5]},
                    evidence_id=verdict_evidence.id,
                )
            else:
                review = self.submit_review(
                    review.id,
                    ReviewStatus.APPROVED.value,
                    review.reviewer_agent_id,
                    reason="reviewer approved via signed verdict evidence",
                    evidence_id=verdict_evidence.id,
                )
                self._record_default_review_observation(
                    task_id,
                    "workflow.default_review.approved",
                    "info",
                    {
                        "review_id": review.id,
                        "reviewer_agent_id": review.reviewer_agent_id,
                        "verdict_evidence_id": verdict_evidence.id,
                        "executor_evidence_id": evidence.id,
                        "evidence_type": evidence_assessment.get("evidence_type"),
                    },
                    actor,
                )
            # The publication evidence below stays as the executor's
            # signed work — that's the artifact being published. The
            # reviewer's verdict was just consumed onto the review row
            # via submit_review(evidence_id=verdict_evidence.id) above.

        if review.status != ReviewStatus.APPROVED.value:
            return {
                "task_id": task_id,
                "status": "review_not_approved",
                "review_id": review.id,
                "review_status": review.status,
            }

        task = self.get_task(task_id)
        if task.state != TaskState.REVIEWING.value:
            return {
                "task_id": task_id,
                "status": "approved_not_publishable",
                "state": task.state,
                "review_id": review.id,
            }
        if self.reviews.task_requires_publication_evidence(task):
            self._record_default_review_observation(
                task_id,
                "workflow.default_review.waiting",
                "warning",
                {
                    "reason": "publication_evidence_required",
                    "review_id": review.id,
                    "evidence_id": evidence.id,
                },
                actor,
            )
            return {
                "task_id": task_id,
                "status": "waiting_for_publication_evidence",
                "review_id": review.id,
            }

        target = self._default_publication_target(task)
        if target is None:
            # No operator-configured publication destination; refuse to
            # invent one. The review is approved, but the task stays in
            # REVIEWING until an operator sets metadata.publication_target
            # (mac-w29).
            self._record_default_review_observation(
                task_id,
                "workflow.default_review.no_publication_target",
                "warning",
                {"review_id": review.id, "evidence_id": evidence.id},
                actor,
            )
            return {
                "task_id": task_id,
                "status": "waiting_for_publication_target",
                "review_id": review.id,
            }
        try:
            publication = self.publish_task(
                task_id,
                target,
                review.reviewer_agent_id,
                evidence_id=evidence.id,
            )
        except (ValidationError, MACError) as exc:
            # Auto-publish failed AFTER a genuine approval — most often the
            # reviewed branch no longer merges cleanly into main (a stale branch
            # base / merge conflict). Previously this exception propagated and was
            # swallowed, leaving the task silently parked in REVIEWING with no
            # explanation (approved but never published). Surface it instead: an
            # observation for telemetry AND a glanceable Problem/Remediation
            # diagnosis on the task (via `mac task show`/`summary`), so the
            # operator knows exactly why it didn't merge and how to recover.
            # (mac task_51a777c2)
            detail = str(exc)
            self._record_default_review_observation(
                task_id,
                "workflow.default_review.publish_failed",
                "warning",
                {
                    "review_id": review.id,
                    "evidence_id": evidence.id,
                    "target": target,
                    "error": detail[:500],
                },
                actor,
            )
            try:
                self.append_task_activity(
                    task_id,
                    "diagnosis",
                    actor,
                    "Problem: Auto-publish to %s failed after approval — the "
                    "reviewed branch could not be merged into main (usually a "
                    "merge conflict from a stale branch base). The task is "
                    "approved but stays in REVIEWING, unpublished.\n"
                    "Remediation: Re-drive the task from current main so its "
                    "branch merges cleanly and let review->publish re-run, or an "
                    "operator resolves the conflict and re-publishes. Error: %s"
                    % (target, detail[:300]),
                )
            except Exception:
                pass
            return {
                "task_id": task_id,
                "status": "publish_failed",
                "review_id": review.id,
                "target": target,
                "error": detail,
            }
        self._record_default_review_observation(
            task_id,
            "workflow.default_review.published",
            "info",
            {
                "review_id": review.id,
                "publication_id": publication.id,
                "target": publication.target,
            },
            actor,
        )
        return {
            "task_id": task_id,
            "status": "published",
            "review_id": review.id,
            "publication_id": publication.id,
        }

    # Secrets boundary: thin facade over ``self.secrets``. New code should
    # call ``cp.secrets.<method>`` directly.

    def create_secret(self, *args: Any, **kwargs: Any) -> SecretRecord:
        return self.secrets.create_secret(*args, **kwargs)

    def get_secret(self, secret_id_or_name: str) -> SecretRecord:
        return self.secrets.get_secret(secret_id_or_name)

    def list_secrets(self) -> List[SecretRecord]:
        return self.secrets.list_secrets()

    def request_secret(self, *args: Any, **kwargs: Any) -> SecretHandle:
        return self.secrets.request_secret(*args, **kwargs)

    def rotate_secret(self, *args: Any, **kwargs: Any) -> SecretRecord:
        return self.secrets.rotate_secret(*args, **kwargs)

    def delete_secret(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.secrets.delete_secret(*args, **kwargs)

    def list_secret_audits(self, *args: Any, **kwargs: Any) -> List[SecretAccess]:
        return self.secrets.list_audits(*args, **kwargs)

    def reveal_secret(self, *args: Any, **kwargs: Any) -> str:
        return self.secrets.reveal_secret(*args, **kwargs)

    # Artifact registry

    # Artifacts + environments + deployments + runtimes: thin facade over
    # ``self.deploy``. New code should call ``cp.deploy.<method>`` directly.

    def register_artifact(self, *args: Any, **kwargs: Any) -> Artifact:
        return self.deploy.register_artifact(*args, **kwargs)

    def get_artifact(self, artifact_id_or_digest: str) -> Artifact:
        return self.deploy.get_artifact(artifact_id_or_digest)

    def list_artifacts(self, *args: Any, **kwargs: Any) -> List[Artifact]:
        return self.deploy.list_artifacts(*args, **kwargs)

    def delete_artifact(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.deploy.delete_artifact(*args, **kwargs)

    def register_environment(self, *args: Any, **kwargs: Any) -> Environment:
        return self.deploy.register_environment(*args, **kwargs)

    def get_environment(self, env_id_or_name: str) -> Environment:
        return self.deploy.get_environment(env_id_or_name)

    def list_environments(self, *args: Any, **kwargs: Any) -> List[Environment]:
        return self.deploy.list_environments(*args, **kwargs)

    def deploy_artifact(self, *args: Any, **kwargs: Any) -> Deployment:
        return self.deploy.deploy_artifact(*args, **kwargs)

    def get_deployment(self, deployment_id: str) -> Deployment:
        return self.deploy.get_deployment(deployment_id)

    def current_deployment(self, environment_id: str) -> Optional[Deployment]:
        return self.deploy.current_deployment(environment_id)

    def list_deployments(self, environment_id: str) -> List[Deployment]:
        return self.deploy.list_deployments(environment_id)

    def create_runtime(self, *args: Any, **kwargs: Any) -> RuntimeEnvironment:
        return self.deploy.create_runtime(*args, **kwargs)

    def get_runtime(self, runtime_id_or_name: str) -> RuntimeEnvironment:
        return self.deploy.get_runtime(runtime_id_or_name)

    def list_runtimes(self) -> List[RuntimeEnvironment]:
        return self.deploy.list_runtimes()

    def propose_runtime_delta(self, *args: Any, **kwargs: Any) -> RuntimeEnvironmentDelta:
        return self.deploy.propose_runtime_delta(*args, **kwargs)

    def get_runtime_delta(self, delta_id: str) -> RuntimeEnvironmentDelta:
        return self.deploy.get_runtime_delta(delta_id)

    def list_runtime_deltas(self, *args: Any, **kwargs: Any) -> List[RuntimeEnvironmentDelta]:
        return self.deploy.list_runtime_deltas(*args, **kwargs)

    def validate_runtime_delta(self, *args: Any, **kwargs: Any) -> RuntimeEnvironmentDelta:
        return self.deploy.validate_runtime_delta(*args, **kwargs)

    def reject_runtime_delta(self, *args: Any, **kwargs: Any) -> RuntimeEnvironmentDelta:
        return self.deploy.reject_runtime_delta(*args, **kwargs)

    def promote_runtime_delta(self, *args: Any, **kwargs: Any) -> RuntimeEnvironmentDelta:
        return self.deploy.promote_runtime_delta(*args, **kwargs)

    def create_runtime_run(self, *args: Any, **kwargs: Any) -> RuntimeRun:
        return self.deploy.create_runtime_run(*args, **kwargs)

    def complete_runtime_run(self, *args: Any, **kwargs: Any) -> RuntimeRun:
        return self.deploy.complete_runtime_run(*args, **kwargs)

    def get_runtime_run(self, run_id: str) -> RuntimeRun:
        return self.deploy.get_runtime_run(run_id)

    def list_runtime_runs(self) -> List[RuntimeRun]:
        return self.deploy.list_runtime_runs()

    # Project bridge


    def import_project_item(
        self,
        source: str,
        external_id: str,
        title: str,
        payload: Dict[str, Any],
        required_capabilities: Optional[Iterable[str]] = None,
        *,
        description: Optional[str] = None,
        project: Optional[str] = None,
        priority: int = 0,
        dependencies: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        actor: str = "bridge",
    ) -> ProjectItem:
        existing = self.store.query_one(
            "SELECT * FROM project_items WHERE source = ? AND external_id = ?",
            (source, external_id),
        )
        if existing is not None:
            return self._project_item_from_row(existing)
        task_metadata = {"source": source, "external_id": external_id}
        task_metadata.update(ensure_json_object(metadata))
        task = self.create_task(
            title,
            description=description if description is not None else json_dumps(payload),
            project=project or source,
            priority=priority,
            required_capabilities=required_capabilities,
            dependencies=dependencies,
            metadata=task_metadata,
            actor=actor,
        )
        now = utcnow()
        item_id = new_id("item")
        self.store.execute(
            """
            INSERT INTO project_items (id, source, external_id, title, payload, task_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (item_id, source, external_id, title, json_dumps(payload), task.id, "imported", now, now),
        )
        self.add_memory(
            task.id,
            "project_item",
            item_id,
            "imported",
            "Imported %s:%s as durable task %s" % (source, external_id, task.id),
            None,
            actor,
        )
        return self.get_project_item(item_id)

    def register_project_repository(
        self,
        name: str,
        path: str,
        source: Optional[str] = None,
        project: Optional[str] = None,
        required_capabilities: Optional[Iterable[str]] = None,
        enabled: bool = True,
        poll_interval_seconds: int = 60,
        metadata: Optional[Dict[str, Any]] = None,
        actor: str = "project-repo",
    ) -> ProjectRepository:
        name = name.strip()
        if not name:
            raise ValidationError("project repository name is required")
        repo_path_obj = Path(path).expanduser()
        repo_path = str(repo_path_obj)
        repo_source = (source or "repo-%s" % _safe_slug(name)).strip()
        if not repo_source:
            raise ValidationError("project repository source is required")
        repo_project = (project or repo_source).strip()
        contract = _load_repository_contract(repo_path_obj)
        if contract["project"] != repo_project:
            raise ValidationError(
                "repository runtime contract project %s does not match registered project %s"
                % (contract["project"], repo_project)
            )
        codegraph_status = _initialize_codegraph_repository(repo_path_obj)
        _raise_for_codegraph_init_failure(codegraph_status)
        repo_metadata = ensure_json_object(metadata)
        repo_metadata["repository_contract"] = contract
        repo_metadata["codegraph"] = codegraph_status
        now = utcnow()
        row = self.store.query_one("SELECT id FROM project_repositories WHERE name = ?", (name,))
        repo_id = row["id"] if row is not None else new_id("projectrepo")
        self.store.execute(
            """
            INSERT INTO project_repositories (
                id, name, path, source, project, required_capabilities,
                enabled, poll_interval_seconds, metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                path = excluded.path,
                source = excluded.source,
                project = excluded.project,
                required_capabilities = excluded.required_capabilities,
                enabled = excluded.enabled,
                poll_interval_seconds = excluded.poll_interval_seconds,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                repo_id,
                name,
                repo_path,
                repo_source,
                repo_project,
                json_dumps(coerce_list(required_capabilities)),
                1 if enabled else 0,
                max(1, int(poll_interval_seconds)),
                json_dumps(repo_metadata),
                now,
                now,
            ),
        )
        self.record_log(
            "bridge.project_repository.registered",
            layer="control_plane",
            source=actor,
            subject_type="environment",
            subject_id=repo_id,
            detail={
                "name": name,
                "path": repo_path,
                "source": repo_source,
                "project": repo_project,
                "enabled": enabled,
                "repository_contract_schema": contract["schema"],
                "repository_contract_path": contract["contract_path"],
                "codegraph": codegraph_status,
            },
        )
        return self.get_project_repository(repo_id)

    def get_project_repository(self, repo_id_or_name: str) -> ProjectRepository:
        row = self.store.query_one(
            "SELECT * FROM project_repositories WHERE id = ? OR name = ?",
            (repo_id_or_name, repo_id_or_name),
        )
        if row is None:
            raise NotFoundError("project repository not found: %s" % repo_id_or_name)
        return self._repository_from_row(row)

    def list_project_repositories(self, enabled: Optional[bool] = None) -> List[ProjectRepository]:
        if enabled is None:
            rows = self.store.query_all("SELECT * FROM project_repositories ORDER BY name, id")
        else:
            rows = self.store.query_all(
                "SELECT * FROM project_repositories WHERE enabled = ? ORDER BY name, id",
                (1 if enabled else 0,),
            )
        return [self._repository_from_row(row) for row in rows]

    def _repository_contract_for_repo(self, repo: ProjectRepository) -> JsonDict:
        contract = _load_repository_contract(Path(repo.path).expanduser())
        if contract["project"] != repo.project:
            raise ValidationError(
                "repository runtime contract project %s does not match registered project %s"
                % (contract["project"], repo.project)
            )
        return contract















    def _git_output(self, repo_path: Path, args: List[str], timeout: int = 20) -> JsonDict:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "returncode": int(completed.returncode),
            "stdout": (completed.stdout or "").strip(),
            "stderr": (completed.stderr or "").strip(),
        }






    @staticmethod
    def _strip_control_chars(value: str) -> str:
        """Strip ASCII control chars except \\t/\\n; reject ANSI escape
        sequences. Used by bd import (mac-3xpl)."""
        return "".join(
            c
            for c in value
            if (c >= " " or c in ("\t", "\n"))
        )








    def get_project_item(self, item_id: str) -> ProjectItem:
        row = self.store.query_one("SELECT * FROM project_items WHERE id = ?", (item_id,))
        if row is None:
            raise NotFoundError("project item not found: %s" % item_id)
        return self._project_item_from_row(row)

    def list_project_items(self) -> List[ProjectItem]:
        rows = self.store.query_all("SELECT * FROM project_items ORDER BY created_at, id")
        return [self._project_item_from_row(row) for row in rows]

    # Memory + conversation threads + vector refs: thin facade over
    # ``self.memory``. New code should call ``cp.memory.<method>`` directly.

    def add_memory(self, *args: Any, **kwargs: Any) -> MemoryRecord:
        return self.memory.add_memory(*args, **kwargs)

    def decay_memory(self, *args: Any, **kwargs: Any) -> JsonDict:
        """dream-04: forget stale, low-salience memory (dry-run by default)."""
        return self.memory.decay_memory(*args, **kwargs)

    def get_memory(self, memory_id: str) -> MemoryRecord:
        return self.memory.get_memory(memory_id)

    def search_memory(self, *args: Any, **kwargs: Any) -> List[MemoryRecord]:
        return self.memory.search_memory(*args, **kwargs)

    def remember_memory(
        self,
        key: str,
        content: str,
        *,
        project: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> MemoryRecord:
        if not key or not str(key).strip():
            raise ValidationError("memory key is required")
        project_name = project or "default"
        record_type = "beads_memory:%s" % key
        self.store.execute(
            """
            DELETE FROM memory_records
            WHERE subject_type = 'project' AND subject_id = ? AND record_type = ?
            """,
            (project_name, record_type),
        )
        return self.add_memory(
            None,
            "project",
            project_name,
            record_type,
            content,
            None,
            actor or "operator",
        )

    def list_remembered_memory(self, *, project: Optional[str] = None) -> List[JsonDict]:
        project_name = project or "default"
        rows = self.store.query_all(
            """
            SELECT * FROM memory_records
            WHERE subject_type = 'project' AND subject_id = ?
              AND record_type LIKE 'beads_memory:%'
            ORDER BY created_at
            """,
            (project_name,),
        )
        return [
            {
                "key": row["record_type"].split(":", 1)[1]
                if ":" in row["record_type"]
                else row["record_type"],
                "content": row["content"],
                "created_at": row["created_at"],
                "id": row["id"],
            }
            for row in rows
        ]

    def forget_memory(self, key: str, *, project: Optional[str] = None) -> JsonDict:
        project_name = project or "default"
        cursor = self.store.execute(
            """
            DELETE FROM memory_records
            WHERE subject_type = 'project' AND subject_id = ? AND record_type = ?
            """,
            (project_name, "beads_memory:%s" % key),
        )
        return {"deleted": cursor.rowcount, "key": key, "project": project_name}

    def track_conversation(self, *args: Any, **kwargs: Any) -> ConversationThread:
        return self.memory.track_conversation(*args, **kwargs)

    def get_conversation_thread(self, thread_id: str) -> ConversationThread:
        return self.memory.get_conversation_thread(thread_id)

    def list_conversation_threads(self, *args: Any, **kwargs: Any) -> List[ConversationThread]:
        return self.memory.list_conversation_threads(*args, **kwargs)

    def record_vector_ref(self, *args: Any, **kwargs: Any) -> VectorRef:
        return self.memory.record_vector_ref(*args, **kwargs)

    def get_vector_ref(self, ref_id: str) -> VectorRef:
        return self.memory.get_vector_ref(ref_id)

    def list_vector_refs(self, *args: Any, **kwargs: Any) -> List[VectorRef]:
        return self.memory.list_vector_refs(*args, **kwargs)

    # Evaluation: thin facade over ``self.evaluations``.

    def create_eval_set(self, *args: Any, **kwargs: Any) -> EvalSet:
        return self.evaluations.create_eval_set(*args, **kwargs)

    def get_eval_set(self, eval_set_id_or_name: str) -> EvalSet:
        return self.evaluations.get_eval_set(eval_set_id_or_name)

    def list_eval_sets(self) -> List[EvalSet]:
        return self.evaluations.list_eval_sets()

    def update_eval_set_baseline(self, *args: Any, **kwargs: Any) -> EvalSet:
        return self.evaluations.update_eval_set_baseline(*args, **kwargs)

    def list_eval_set_events(self, eval_set_id_or_name: str) -> List[JsonDict]:
        return self.evaluations.list_eval_set_events(eval_set_id_or_name)

    def record_eval_run(self, *args: Any, **kwargs: Any) -> EvalRun:
        return self.evaluations.record_eval_run(*args, **kwargs)

    def get_eval_run(self, run_id: str) -> EvalRun:
        return self.evaluations.get_eval_run(run_id)

    def latest_eval_run(self, *args: Any, **kwargs: Any) -> Optional[EvalRun]:
        return self.evaluations.latest_eval_run(*args, **kwargs)

    def list_eval_runs(self, *args: Any, **kwargs: Any) -> List[EvalRun]:
        return self.evaluations.list_eval_runs(*args, **kwargs)

    # Rollout and rescue

    # Rollouts: thin facade over ``self.rollouts``.

    def create_rollout(self, *args: Any, **kwargs: Any) -> Rollout:
        return self.rollouts.create_rollout(*args, **kwargs)

    def get_rollout(self, rollout_id: str) -> Rollout:
        return self.rollouts.get_rollout(rollout_id)

    def list_rollouts(self, *args: Any, **kwargs: Any) -> List[Rollout]:
        return self.rollouts.list_rollouts(*args, **kwargs)

    def list_rollout_events(self, rollout_id: str) -> List[JsonDict]:
        return self.rollouts.list_rollout_events(rollout_id)

    def verify_rollout_artifact(self, *args: Any, **kwargs: Any) -> Rollout:
        return self.rollouts.verify_rollout_artifact(*args, **kwargs)

    def advance_rollout(self, *args: Any, **kwargs: Any) -> Rollout:
        return self.rollouts.advance_rollout(*args, **kwargs)

    def evaluate_rollout_health(self, *args: Any, **kwargs: Any) -> JsonDict:
        return self.rollouts.evaluate_rollout_health(*args, **kwargs)

    def rescue_rollout(self, *args: Any, **kwargs: Any) -> Tuple[Rollout, Task]:
        return self.rollouts.rescue_rollout(*args, **kwargs)


    # Row mapping

    def _task_from_row(self, row: Any) -> Task:
        return Task(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            project=row["project"],
            priority=row["priority"],
            state=row["state"],
            required_capabilities=json_loads(row["required_capabilities"], []),
            dependencies=json_loads(row["dependencies"], []),
            metadata=json_loads(row["metadata"], {}),
            owner_agent_id=row["owner_agent_id"],
            lease_id=row["lease_id"],
            leased_until=row["leased_until"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _history_from_row(self, row: Any) -> HistoryEvent:
        return HistoryEvent(
            row["id"],
            row["task_id"],
            row["event_type"],
            row["actor"],
            row["from_state"],
            row["to_state"],
            json_loads(row["detail"], {}),
            row["created_at"],
        )

    def _evidence_from_row(self, row: Any) -> Evidence:
        return Evidence(
            row["id"],
            row["task_id"],
            row["kind"],
            row["uri"],
            row["summary"],
            row["checksum"],
            json_loads(row["metadata"], {}),
            row["created_by"],
            row["created_at"],
        )

    def _evidence_artifact_from_row(self, row: Any) -> EvidenceArtifact:
        keys = row.keys() if hasattr(row, "keys") else []
        return EvidenceArtifact(
            row["id"],
            row["evidence_id"],
            row["task_id"],
            row["name"],
            row["artifact_type"],
            row["source_uri"],
            row["content_type"],
            row["encoding"],
            int(row["size_bytes"]),
            row["sha256"],
            row["content_base64"],
            str(row["truncated"]).strip().lower() in {"1", "true", "yes"},
            json_loads(row["metadata"], {}),
            row["created_at"],
            content_uri=str(row["content_uri"] or "") if "content_uri" in keys else "",
        )

    def _command_audit_from_row(self, row: Any) -> CommandAuditRecord:
        return CommandAuditRecord(
            row["id"],
            row["command_id"],
            row["agent_id"],
            row["phase"],
            json_loads(row["argv"], []),
            row["cwd"],
            row["task_id"],
            row["lease_id"],
            row["started_at"],
            row["completed_at"],
            row["duration_ms"],
            row["returncode"],
            row["stdout_sha256"],
            row["stderr_sha256"],
            row["stdout_bytes"],
            row["stderr_bytes"],
            json_loads(row["metadata"], {}),
            row["created_at"],
        )

    def _notification_from_row(self, row: Any) -> OperatorNotification:
        return OperatorNotification(
            row["id"],
            row["event_type"],
            row["subject_type"],
            row["subject_id"],
            row["title"],
            row["body"],
            json_loads(row["channels"], []),
            json_loads(row["metadata"], {}),
            row["status"],
            row["created_at"],
            row["delivered_at"],
        )

    def _integration_observation_from_row(self, row: Any) -> IntegrationObservation:
        return IntegrationObservation(
            row["id"],
            row["source_id"],
            row["source_kind"],
            row["authority"],
            row["status"],
            row["fingerprint"],
            row["cursor"],
            json_loads(row["detail"], {}),
            row["observed_at"],
        )

    def _integration_finding_from_row(self, row: Any) -> IntegrationFinding:
        return IntegrationFinding(
            row["id"],
            row["source_id"],
            row["source_kind"],
            row["finding_type"],
            row["severity"],
            row["status"],
            row["title"],
            json_loads(row["detail"], {}),
            row["fingerprint"],
            row["first_seen_at"],
            row["last_seen_at"],
            row["resolved_at"],
            row["resolution"],
        )

    def _lease_from_row(self, row: Any) -> Lease:
        # PR2c: ``delegated_agent_id`` is the additive column added by
        # the lease-delegation migration. Older DBs (pre-migration) or
        # row mappings that do not surface the column still resolve to
        # None, keeping legacy call sites bit-for-bit compatible.
        keys = row.keys() if hasattr(row, "keys") else []
        delegated = row["delegated_agent_id"] if "delegated_agent_id" in keys else None
        return Lease(
            row["id"],
            row["task_id"],
            row["agent_id"],
            row["expires_at"],
            row["status"],
            row["created_at"],
            row["updated_at"],
            delegated,
        )

    def _machine_from_row(self, row: Any) -> Machine:
        keys = row.keys() if hasattr(row, "keys") else []
        hardware = json_loads(row["hardware"], {}) if "hardware" in keys else {}
        return Machine(
            row["id"],
            row["hostname"],
            json_loads(row["labels"], {}),
            json_loads(row["resources"], {}),
            bool(row["trusted"]),
            row["created_at"],
            row["updated_at"],
            row["last_seen_at"],
            hardware,
        )

    def _fleet_from_row(self, row: Any) -> Fleet:
        member_rows = self.store.query_all(
            "SELECT agent_id FROM fleet_agents WHERE fleet_id = ? ORDER BY agent_id",
            (row["id"],),
        )
        observed_rows = self.store.query_all(
            """
            SELECT agent_id
            FROM fleet_agent_observations
            WHERE fleet_id = ?
            ORDER BY agent_id
            """,
            (row["id"],),
        )
        agent_ids = [member["agent_id"] for member in member_rows]
        observed_agent_ids = [member["agent_id"] for member in observed_rows]
        return Fleet(
            row["id"],
            row["name"],
            row["description"],
            row["status"],
            json_loads(row["metadata"], {}),
            row["tenant_id"],
            agent_ids,
            row["created_at"],
            row["updated_at"],
            observed_agent_ids,
            sorted(set(observed_agent_ids) - set(agent_ids)),
        )

    def _agent_from_row(self, row: Any) -> Agent:
        keys = row.keys() if hasattr(row, "keys") else []
        running_digest = row["running_digest"] if "running_digest" in keys else None
        role_id = row["role_id"] if "role_id" in keys else None
        hermes_instance_id = (
            row["hermes_instance_id"] if "hermes_instance_id" in keys else None
        )
        installed_packages = (
            json_loads(row["installed_packages"], {})
            if "installed_packages" in keys
            else {}
        )
        return Agent(
            row["id"],
            row["machine_id"],
            row["name"],
            json_loads(row["capabilities"], []),
            json_loads(row["resources"], {}),
            row["status"],
            row["health_status"],
            row["current_task_id"],
            running_digest,
            row["created_at"],
            row["updated_at"],
            row["last_seen_at"],
            role_id,
            hermes_instance_id,
            installed_packages,
        )

    def _project_item_from_row(self, row: Any) -> ProjectItem:
        return ProjectItem(
            row["id"],
            row["source"],
            row["external_id"],
            row["title"],
            json_loads(row["payload"], {}),
            row["task_id"],
            row["status"],
            row["created_at"],
            row["updated_at"],
        )

    def _project_record_from_row(self, row: Any) -> ProjectRecord:
        return ProjectRecord(
            row["id"],
            row["name"],
            row["description"],
            json_loads(row["metadata"], {}),
            row["status"],
            row["created_at"],
            row["updated_at"],
        )

    def _repository_from_row(self, row: Any) -> ProjectRepository:
        return ProjectRepository(
            row["id"],
            row["name"],
            row["path"],
            row["source"],
            row["project"],
            json_loads(row["required_capabilities"], []),
            bool(row["enabled"]),
            int(row["poll_interval_seconds"]),
            row["last_polled_at"],
            row["last_imported_at"],
            row["last_error"],
            json_loads(row["metadata"], {}),
            row["created_at"],
            row["updated_at"],
        )

    # Internal helpers

    def _record_history(
        self,
        task_id: str,
        event_type: str,
        actor: str,
        from_state: Optional[str],
        to_state: Optional[str],
        detail: Dict[str, Any],
        conn: Any = None,
    ) -> None:
        when = utcnow()
        writer = conn if conn is not None else self.store
        writer.execute(
            """
            INSERT INTO task_history (id, task_id, event_type, actor, from_state, to_state, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("hist"), task_id, event_type, actor, from_state, to_state, json_dumps(detail), when),
        )
        self.observability.insert_observation(
            writer,
            "log",
            event_type,
            "control_plane",
            "task",
            "info",
            None,
            "",
            "task",
            task_id,
            {"actor": actor, "from_state": from_state, "to_state": to_state, **detail},
            when,
        )
        self._record_history_notification(
            writer,
            task_id,
            event_type,
            actor,
            from_state,
            to_state,
            detail,
            when,
        )

    def _record_project_event(
        self,
        writer: Any,
        project_id: str,
        event_type: str,
        actor: str,
        detail: Dict[str, Any],
        when: str,
    ) -> None:
        payload = {"actor": actor, **ensure_json_object(detail)}
        writer.execute(
            """
            INSERT INTO project_events (id, project_id, event_type, actor, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("pevt"), project_id, event_type, actor, json_dumps(payload), when),
        )
        self.observability.insert_observation(
            writer,
            "log",
            event_type,
            "control_plane",
            "project",
            "info",
            None,
            "",
            "project",
            project_id,
            payload,
            when,
        )

    def _record_fleet_event(
        self,
        writer: Any,
        fleet_id: str,
        event_type: str,
        actor: str,
        detail: Dict[str, Any],
        when: str,
    ) -> None:
        payload = {"actor": actor, **ensure_json_object(detail)}
        writer.execute(
            """
            INSERT INTO fleet_events (id, fleet_id, event_type, actor, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("fevt"), fleet_id, event_type, actor, json_dumps(payload), when),
        )
        self.observability.insert_observation(
            writer,
            "log",
            event_type,
            "control_plane",
            "fleet",
            "info",
            None,
            "",
            "fleet",
            fleet_id,
            payload,
            when,
        )

    def _record_agent_lifecycle_event(
        self,
        writer: Any,
        agent_id: str,
        event_type: str,
        actor: str,
        detail: Dict[str, Any],
        when: str,
    ) -> None:
        payload = {"actor": actor, **ensure_json_object(detail)}
        writer.execute(
            """
            INSERT INTO agent_lifecycle_events (id, agent_id, event_type, actor, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("alce"), agent_id, event_type, actor, json_dumps(payload), when),
        )
        self.observability.insert_observation(
            writer,
            "log",
            event_type,
            "control_plane",
            "agent",
            "info",
            None,
            "",
            "agent",
            agent_id,
            payload,
            when,
        )

    def _record_history_notification(
        self,
        writer: Any,
        task_id: str,
        event_type: str,
        actor: str,
        from_state: Optional[str],
        to_state: Optional[str],
        detail: Dict[str, Any],
        when: str,
    ) -> None:
        payload = self._notification_payload_for_history(
            task_id, event_type, actor, from_state, to_state, detail
        )
        if payload is None:
            return
        self.record_notification(
            payload["event_type"],
            payload["title"],
            payload["body"],
            subject_type="task",
            subject_id=task_id,
            channels=payload.get("channels"),
            metadata=payload.get("metadata"),
            conn=writer,
            created_at=when,
        )

    def _notification_payload_for_history(
        self,
        task_id: str,
        event_type: str,
        actor: str,
        from_state: Optional[str],
        to_state: Optional[str],
        detail: Dict[str, Any],
    ) -> Optional[JsonDict]:
        task_title = task_id
        try:
            task_title = self.get_task(task_id).title
        except Exception:
            pass
        metadata = {
            "actor": actor,
            "from_state": from_state,
            "to_state": to_state,
            **ensure_json_object(detail),
        }
        if event_type == "task.claimed":
            return {
                "event_type": event_type,
                "title": "Task claimed",
                "body": "%s claimed %s" % (actor, task_title),
                "channels": ["dashboard", "hermes"],
                "metadata": metadata,
            }
        if event_type == "task.evidence_added":
            return {
                "event_type": event_type,
                "title": "Evidence recorded",
                "body": "%s added %s evidence for %s"
                % (actor, detail.get("kind", "task"), task_title),
                "channels": ["dashboard", "hermes"],
                "metadata": metadata,
            }
        if event_type == "task.review_requested":
            return {
                "event_type": event_type,
                "title": "Review requested",
                "body": "Review requested for %s" % task_title,
                "channels": ["dashboard", "hermes"],
                "metadata": metadata,
            }
        if event_type == "task.review_claimed":
            return {
                "event_type": event_type,
                "title": "Review claimed",
                "body": "%s claimed review for %s" % (actor, task_title),
                "channels": ["dashboard", "hermes"],
                "metadata": metadata,
            }
        if event_type == "task.review_completed":
            return {
                "event_type": event_type,
                "title": "Review completed",
                "body": "Review %s for %s"
                % (str(detail.get("status") or "completed"), task_title),
                "channels": ["dashboard", "hermes"],
                "metadata": metadata,
            }
        if event_type == "task.published":
            return {
                "event_type": event_type,
                "title": "Task published",
                "body": "%s published %s" % (actor, task_title),
                "channels": ["dashboard", "hermes"],
                "metadata": metadata,
            }
        if event_type == "task.lease_expired":
            return {
                "event_type": event_type,
                "title": "Task lease expired",
                "body": "%s was requeued after lease expiry" % task_title,
                "channels": ["dashboard", "hermes"],
                "metadata": metadata,
            }
        if event_type == "task.transitioned" and to_state in {
            TaskState.RUNNING.value,
            TaskState.BLOCKED.value,
            TaskState.NEEDS_REVIEW.value,
            TaskState.REVIEWING.value,
            TaskState.COMPLETED.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        }:
            return {
                "event_type": "task.%s" % to_state,
                "title": "Task %s" % to_state.replace("_", " "),
                "body": "%s moved to %s" % (task_title, to_state),
                "channels": ["dashboard", "hermes"],
                "metadata": metadata,
            }
        return None

    def _dependencies_satisfied(self, task: Task) -> bool:
        for dep_id in task.dependencies:
            dep = self.get_task(dep_id)
            if dep.state != TaskState.COMPLETED.value:
                return False
        return True

    def _blocked_task_requires_manual_repair(self, task: Task) -> bool:
        if task.state != TaskState.BLOCKED.value:
            return False
        for event in reversed(self.task_history(task.id, limit=20)):
            if event.to_state != TaskState.BLOCKED.value:
                continue
            detail = ensure_json_object(event.detail)
            reason = str(detail.get("reason") or "").strip()
            return detail.get("manual_repair_required") is True or reason in {
                "verification_contract_failed",
                "executor_failed",
                "worker_exception",
            }
        return False

    def _prepare_cooperative_integration_task(self, task: Task) -> Task:
        """Attach immutable child outputs before reopening an integration task."""
        metadata = ensure_json_object(task.metadata)
        coordination = ensure_json_object(metadata.get("coordination"))
        if coordination.get("mode") != "cooperative_integration":
            return task
        child_ids = _metadata_string_list(coordination.get("child_task_ids"))
        if not child_ids:
            return task
        outputs: List[JsonDict] = []
        for child_id in child_ids:
            try:
                child = self.get_task(child_id)
            except NotFoundError:
                outputs.append(
                    {"task_id": child_id, "status": "missing_task"}
                )
                continue
            child_target = ensure_json_object(
                ensure_json_object(child.metadata).get("review_target")
            )
            evidence_id = str(
                child_target.get("executor_evidence_id") or ""
            ).strip()
            output: JsonDict = {
                "task_id": child.id,
                "title": child.title,
                "state": child.state,
                "executor_evidence_id": evidence_id,
            }
            if evidence_id:
                try:
                    evidence = self.get_evidence(evidence_id)
                except NotFoundError:
                    output["status"] = "missing_evidence"
                else:
                    manifest = ensure_json_object(
                        ensure_json_object(evidence.metadata).get("verification")
                    )
                    repo = ensure_json_object(manifest.get("repo"))
                    output.update(
                        {
                            "status": "ready",
                            "summary": evidence.summary,
                            "created_by": evidence.created_by,
                            "evidence_type": manifest.get("evidence_type"),
                            "repo": {
                                key: repo.get(key)
                                for key in (
                                    "head_sha",
                                    "base_sha",
                                    "remote_ref",
                                    "remote_url",
                                    "pr_url",
                                    "files_changed",
                                )
                                if repo.get(key) not in (None, "", [])
                            },
                        }
                    )
            else:
                output["status"] = "missing_evidence"
            outputs.append(output)
        coordination["phase"] = "integration"
        coordination["child_outputs"] = outputs
        coordination["integration_contract"] = {
            "required": True,
            "strategy": "merge_each_exact_child_commit",
            "verify_combined_result": True,
            "verify_child_commit_ancestry": True,
        }
        metadata["coordination"] = coordination
        self.store.execute(
            "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
            (json_dumps(metadata), utcnow(), task.id),
        )
        return self.get_task(task.id)

    def _unblock_ready_tasks(
        self,
        *,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> JsonDict:
        limit_value = max(1, min(int(limit), 1000))
        clauses = ["state = ?"]
        params: List[Any] = [TaskState.BLOCKED.value]
        decoded = self._decode_scan_cursor(cursor, "blocked-tasks")
        if decoded is not None:
            updated_at, task_id = decoded
            clauses.append("(updated_at > ? OR (updated_at = ? AND id > ?))")
            params.extend([updated_at, updated_at, task_id])
        params.append(limit_value + 1)
        rows = self.store.query_all(
            "SELECT * FROM tasks WHERE %s "
            "ORDER BY updated_at, id LIMIT ?" % " AND ".join(clauses),
            tuple(params),
        )
        has_more = len(rows) > limit_value
        rows = rows[:limit_value]
        unblocked: List[Task] = []
        for row in rows:
            task = self._task_from_row(row)
            try:
                if (
                    task.dependencies
                    and self._dependencies_satisfied(task)
                    and not self._blocked_task_requires_manual_repair(task)
                ):
                    task = self._prepare_cooperative_integration_task(task)
                    unblocked.append(
                        self.transition_task(
                            task.id,
                            TaskState.OPEN.value,
                            "dispatcher",
                            {"reason": "dependencies satisfied"},
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - isolate corrupt blocked rows.
                try:
                    self.record_log(
                        "task.unblock.failed",
                        layer="control_plane",
                        source="dispatcher",
                        level="error",
                        subject_type="task",
                        subject_id=task.id,
                        detail={"task_id": task.id, "error": str(exc)},
                    )
                except Exception:
                    pass
        next_cursor = (
            self._encode_scan_cursor(
                "blocked-tasks",
                str(rows[-1]["updated_at"]),
                str(rows[-1]["id"]),
            )
            if has_more and rows
            else None
        )
        return {
            "tasks": unblocked,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    def _unblock_ready_sweep_page(self, *, limit: int) -> JsonDict:
        claim = self.reconciliation.claim("blocked-task-sweep")
        if claim is None:
            return {
                "tasks": [],
                "next_cursor": None,
                "has_more": False,
                "skipped": "lease_held",
            }
        try:
            result = self._unblock_ready_tasks(
                limit=limit,
                cursor=claim.cursor,
            )
        except Exception:
            self.reconciliation.abandon(claim)
            raise
        self.reconciliation.complete(claim, cursor=result.get("next_cursor"))
        return result

    # mac-5ayd: cap the working set per dispatch batch. Loading 100k OPEN
    # tasks into Python to sort+round-robin is wasteful and grows linearly
    # with backlog. 500 is above the largest supported tick batch while
    # remaining below the point where the Python sort cost is noticeable.
    _DISPATCH_TASK_WINDOW = 500


    def _dispatch_ordered_tasks(self) -> List[Task]:
        groups: Dict[str, List[Task]] = {}
        for task in self.list_tasks(TaskState.OPEN.value, limit=self._DISPATCH_TASK_WINDOW):
            tenant_key = self._task_tenant_id(task) or ""
            groups.setdefault(tenant_key, []).append(task)
        for tenant_tasks in groups.values():
            tenant_tasks.sort(key=lambda item: (-item.priority, item.created_at, item.id))
        tenant_order = sorted(
            groups,
            key=lambda tenant_id: (-groups[tenant_id][0].priority, tenant_id),
        )
        ordered: List[Task] = []
        while any(groups.values()):
            for tenant_id in tenant_order:
                if groups[tenant_id]:
                    ordered.append(groups[tenant_id].pop(0))
        return ordered

    def _worker_claim_policy(
        self,
        allowed_projects: Optional[Iterable[str]],
        required_metadata: Optional[Dict[str, Any]],
        require_canary: bool,
        dry_run: bool,
        capabilities: Optional[Iterable[str]] = None,
    ) -> JsonDict:
        return {
            "allowed_projects": sorted(
                {
                    str(project).strip()
                    for project in (allowed_projects or [])
                    if str(project).strip()
                }
            ),
            "required_metadata": ensure_json_object(required_metadata or {}),
            "require_canary": bool(require_canary),
            "dry_run": bool(dry_run),
            "capabilities": sorted(
                {
                    str(cap).strip()
                    for cap in (capabilities or [])
                    if str(cap).strip()
                }
            ),
        }

    def _task_dispatch_held(self, task: Task) -> bool:
        """True when a task is explicitly held from autonomous dispatch (staged).

        Set via metadata ``no_dispatch: true`` (e.g. ``mac task create
        --no-dispatch``) so a backlog — a freshly-onboarded project's tickets,
        or operator handoff notes — can be filed WITHOUT the loop-mode fleet
        auto-claiming it. The hold only blocks autonomous claim and hides the
        task from the ready queue; an operator can still start it explicitly
        (``mac task claim`` / ``mac task start``). This is the first-class
        replacement for abusing a sentinel ``required_capabilities`` value.
        """
        return bool(ensure_json_object(task.metadata).get("no_dispatch"))

    def _project_dispatch_paused(self, project: Optional[str]) -> bool:
        """True when the task's project is explicitly dispatch-PAUSED.

        Per-project onboarding gate: a newly-created project can be staged
        (``mac project create`` defaults to paused; ``mac project activate``
        releases it) so its tickets don't auto-dispatch until the operator
        turns the project on. IMPLICIT projects (no ``projects`` row — the case
        for the live fleet's default project) are never paused, so existing
        autonomous behavior is unchanged. Only projects with a record carrying
        ``metadata.dispatch_paused`` are held.
        """
        if not project:
            return False
        try:
            rec = self.get_project_record(project)
        except NotFoundError:
            return False
        except Exception:  # noqa: BLE001 — never let a lookup error block dispatch
            return False
        return bool(ensure_json_object(rec.metadata).get("dispatch_paused"))

    def _task_matches_worker_claim_policy(self, task: Task, policy: JsonDict) -> Tuple[bool, str]:
        if self._task_dispatch_held(task):
            return False, "dispatch_held"
        if self._project_dispatch_paused(task.project):
            return False, "project_dispatch_paused"
        allowed_projects = set(policy.get("allowed_projects") or [])
        if allowed_projects and (task.project or "") not in allowed_projects:
            return False, "project_not_allowed"
        capabilities = set(policy.get("capabilities") or [])
        if capabilities:
            required = set(getattr(task, "required_capabilities", None) or [])
            if required and not required.issubset(capabilities):
                return False, "capability_not_allowed"
        metadata = ensure_json_object(task.metadata)
        if policy.get("require_canary") and not (
            metadata.get("canary") is True
            or metadata.get("mac_canary") is True
            or metadata.get("worker_canary") is True
        ):
            return False, "not_canary"
        for key, expected in (policy.get("required_metadata") or {}).items():
            if metadata.get(key) != expected:
                return False, "metadata_mismatch"
        return True, "matched"

    def _available_agents(self) -> List[Agent]:
        rows = self.store.query_all(
            """
            SELECT a.* FROM agents a
            JOIN machines m ON m.id = a.machine_id
            WHERE a.status IN (?, ?) AND a.health_status = ? AND m.trusted = 1
            ORDER BY a.last_seen_at DESC, a.id
            """,
            (AgentStatus.IDLE.value, AgentStatus.BUSY.value, HealthStatus.HEALTHY.value),
        )
        return [self._agent_from_row(row) for row in rows]

    def _coordination_related_task_ids(self, task: Task) -> set[str]:
        """Return the durable task family used for cooperative separation."""
        metadata = ensure_json_object(task.metadata)
        coordination = ensure_json_object(metadata.get("coordination"))
        if coordination.get("require_distinct_agent") is not True:
            return set()
        relationships = ensure_json_object(metadata.get("relationships"))
        parent_id = str(
            relationships.get("parent_task_id")
            or metadata.get("parent_task_id")
            or task.id
        ).strip()
        related_ids = {parent_id}
        if parent_id == task.id:
            related_ids.update(
                _metadata_string_list(coordination.get("child_task_ids"))
            )
            related_ids.update(
                _metadata_string_list(relationships.get("child_task_ids"))
            )
        else:
            try:
                parent = self.get_task(parent_id)
            except NotFoundError:
                parent = None
            if parent is not None:
                parent_metadata = ensure_json_object(parent.metadata)
                parent_relationships = ensure_json_object(
                    parent_metadata.get("relationships")
                )
                parent_coordination = ensure_json_object(
                    parent_metadata.get("coordination")
                )
                related_ids.update(
                    _metadata_string_list(parent_relationships.get("child_task_ids"))
                )
                related_ids.update(
                    _metadata_string_list(parent_coordination.get("child_task_ids"))
                )
        return related_ids

    def _coordination_excluded_agent_ids(self, task: Task) -> set[str]:
        """Agents already participating in a cooperative task family.

        Decomposed children and the final integration pass require distinct
        executors.  Lease history is the durable source of participation: it
        survives task handoff and rejected attempts, unlike ``owner_agent_id``.
        """
        related_ids = self._coordination_related_task_ids(task)
        if not related_ids:
            return set()
        placeholders = ",".join("?" for _ in related_ids)
        rows = self.store.query_all(
            "SELECT DISTINCT agent_id FROM leases WHERE task_id IN (%s)"
            % placeholders,
            tuple(sorted(related_ids)),
        )
        return {str(row["agent_id"]) for row in rows if row["agent_id"]}

    def _agent_available_for(self, agent: Agent, task: Task) -> bool:
        if agent.status not in {AgentStatus.IDLE.value, AgentStatus.BUSY.value}:
            return False
        if agent.health_status != HealthStatus.HEALTHY.value:
            return False
        if agent.id in self._coordination_excluded_agent_ids(task):
            return False
        target_agent_id = (
            task.metadata.get("target_agent_id")
            if isinstance(task.metadata, dict)
            else None
        )
        if target_agent_id and agent.id != str(target_agent_id):
            return False
        target_agent_name = (
            task.metadata.get("target_agent_name")
            if isinstance(task.metadata, dict)
            else None
        )
        if target_agent_name and agent.name != str(target_agent_name):
            return False
        required_runtime_digest = self._task_required_runtime_digest(task)
        if required_runtime_digest and agent.running_digest != required_runtime_digest:
            return False
        machine = self.get_machine(agent.machine_id)
        if not machine.trusted:
            return False
        if self._agent_active_lease_count(agent.id) >= self._agent_capacity(agent):
            return False
        if not self._machine_allows_tenant(machine, self._task_tenant_id(task)):
            return False
        if not self._agent_resources_satisfy(agent, machine, task):
            return False
        if not self._agent_has_repository_commands(agent, task):
            return False
        # Role + hardware gates. Both no-op when neither the agent nor the
        # task carry role/hardware metadata, so the legacy capability path
        # below stays the dominant matcher for un-roled fleets.
        required_role = task.metadata.get("required_role") if isinstance(task.metadata, dict) else None
        if required_role:
            # Look up the target role first. An unknown role can never be
            # served, regardless of whether the agent is role-bound or a
            # multi-role dispatcher — fail closed.
            try:
                target_role = self.roles.get_role(str(required_role))
            except NotFoundError:
                return False
            if agent.role_id is not None:
                # Role-bound agent: keep the strict slug match so a tenant
                # using role-specific agents still gets the original
                # routing guarantee.
                try:
                    bound_role = self.roles.get_role(agent.role_id)
                except NotFoundError:
                    return False
                if bound_role.slug != required_role:
                    return False
            else:
                # Dispatcher case (job-per-task roles spec §6.1 Option B):
                # the runner agent has no role_id but carries the union of
                # role capabilities and re-attributes the work to a
                # role-specific identity at Job-launch time. Allow the
                # claim iff the dispatcher's capabilities cover the
                # target role's required_capabilities. The task-level
                # capabilities are still enforced by the union check at
                # the end of this method.
                if not set(target_role.required_capabilities).issubset(
                    set(agent.capabilities)
                ):
                    return False
        role_required_caps: set = set()
        if agent.role_id is not None:
            try:
                role = self.roles.get_role(agent.role_id)
            except NotFoundError:
                role = None
            if role is not None:
                ok, _reasons = self.roles.validate_hardware(role, machine)
                if not ok:
                    return False
                # Soul-role compatibility is re-checked at dispatch time
                # rather than only at assignment time, so a persona edit
                # that narrows the allowed role list immediately stops
                # affected agents from being eligible.
                if not self.roles.soul_accepts_role(agent, role):
                    return False
                role_required_caps = set(role.required_capabilities)
        capabilities = set(agent.capabilities)
        required = set(task.required_capabilities) | role_required_caps
        return required.issubset(capabilities)

    def _agent_has_repository_commands(self, agent: Agent, task: Task) -> bool:
        metadata = ensure_json_object(task.metadata)
        required_commands = (
            _repository_host_required_commands_from_metadata(metadata)
            if _agent_requires_openshell(agent)
            else _repository_required_commands_from_metadata(metadata)
        )
        if not required_commands:
            return True
        available = _agent_resource_command_names(ensure_json_object(agent.resources))
        return set(required_commands).issubset(available)

    def _task_required_runtime_digest(self, task: Task) -> Optional[str]:
        metadata = ensure_json_object(task.metadata)
        runtime = metadata.get("runtime")
        runtime_meta = ensure_json_object(runtime) if isinstance(runtime, dict) else {}
        for value in (
            runtime_meta.get("required_runtime_digest"),
            runtime_meta.get("runtime_digest"),
            runtime_meta.get("base_runtime_digest"),
            metadata.get("required_runtime_digest"),
            metadata.get("runtime_digest"),
        ):
            text = str(value or "").strip()
            if text:
                return text
        runtime_id = str(
            runtime_meta.get("runtime_environment_id")
            or runtime_meta.get("required_runtime_environment_id")
            or metadata.get("runtime_environment_id")
            or metadata.get("required_runtime_environment_id")
            or ""
        ).strip()
        if not runtime_id:
            return None
        try:
            return self.get_runtime(runtime_id).digest
        except NotFoundError:
            return "__unknown_runtime_digest__"

    def _default_review_policy(self, task: Task) -> JsonDict:
        metadata = ensure_json_object(task.metadata)
        for key in ("review", "default_review"):
            value = metadata.get(key)
            if isinstance(value, dict):
                return ensure_json_object(value)
        return {}

    def _default_review_required_capabilities(
        self,
        task: Task,
        policy: Optional[JsonDict] = None,
    ) -> List[str]:
        policy = ensure_json_object(policy or self._default_review_policy(task))
        required = set(_metadata_string_list(policy.get("required_capabilities")))
        if (
            policy.get("inherit_task_capabilities") is True
            or policy.get("inherit_required_capabilities") is True
        ):
            required.update(str(capability) for capability in task.required_capabilities)
        return sorted(required)

    def _default_review_disabled(self, task: Task) -> bool:
        policy = self._default_review_policy(task)
        mode = str(policy.get("mode") or policy.get("workflow") or "").strip().lower()
        return (
            mode == "manual"
            or policy.get("manual") is True
            or policy.get("auto") is False
            or policy.get("enabled") is False
        )

    def _default_review_evidence(self, task: Task) -> Tuple[Optional[Evidence], JsonDict]:
        evidence = self.list_evidence(task.id)
        if not evidence:
            return None, {"reason": "no_evidence"}
        successful = [
            item
            for item in evidence
            if self._evidence_returncode(item) == 0
        ]
        if not successful:
            return None, {"reason": "no_successful_evidence"}
        rejected: List[JsonDict] = []
        for item in reversed(successful):
            assessment = self._assess_default_review_evidence(task, item)
            if assessment["valid"]:
                return item, assessment
            rejected.append(
                {
                    "evidence_id": item.id,
                    "reason": assessment["reason"],
                    "problems": assessment.get("problems", []),
                }
            )
        return None, {
            "reason": "evidence_not_verifiable",
            "rejected_evidence": rejected[:5],
        }

    def _bound_review_evidence(self, task: Task) -> Tuple[Optional[Evidence], JsonDict]:
        """Resolve the immutable executor evidence target for this review attempt."""
        metadata = ensure_json_object(task.metadata)
        target = ensure_json_object(metadata.get("review_target"))
        target_id = str(target.get("executor_evidence_id") or "").strip()
        if target_id:
            try:
                evidence = self.get_evidence(target_id)
            except NotFoundError:
                return None, {
                    "reason": "review_target_missing",
                    "executor_evidence_id": target_id,
                }
            assessment = self._assess_default_review_evidence(task, evidence)
            if assessment.get("valid"):
                return evidence, assessment
            return None, {
                "reason": "bound_evidence_not_verifiable",
                "executor_evidence_id": target_id,
                "problems": assessment.get("problems", []),
            }

        # Rolling-upgrade compatibility for tasks that entered NEEDS_REVIEW
        # before review_target existed. Bind once, then never follow newer rows.
        evidence, assessment = self._default_review_evidence(task)
        if evidence is not None:
            metadata["review_target"] = {
                "executor_evidence_id": evidence.id,
                "attempt_count": task.attempt_count,
                "recorded_at": utcnow(),
            }
            self.store.execute(
                "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                (json_dumps(metadata), utcnow(), task.id),
            )
        return evidence, assessment

    def _assess_default_review_evidence(self, task: Task, evidence: Evidence) -> JsonDict:
        if self._evidence_returncode(evidence) != 0:
            return {
                "valid": False,
                "reason": "executor_not_successful",
                "problems": ["evidence returncode is not zero"],
            }
        manifest = evidence.metadata.get("verification") or evidence.metadata.get("mac_evidence")
        if not isinstance(manifest, dict):
            return {
                "valid": False,
                "reason": "missing_verification_manifest",
                "problems": ["evidence metadata lacks verification manifest"],
            }
        problems: List[str] = []
        schema = str(manifest.get("schema") or "").strip()
        if schema != VERIFICATION_SCHEMA:
            problems.append("verification.schema must be %s" % VERIFICATION_SCHEMA)
        # Canonical names only (mac-q38). Aliases were a maintainability
        # multiplier — every alias is a separate door downstream
        # validation must remember. Status must be ``complete``; the
        # alternative aliases (verified/pass/passed) are rejected at the
        # boundary. Same for evidence_type below.
        status = str(manifest.get("status") or "").strip().lower()
        if status != "complete":
            problems.append('verification.status must be "complete"')
        evidence_type = str(manifest.get("evidence_type") or "").strip().lower()
        if not evidence_type:
            problems.append("verification.evidence_type is required")
        if problems:
            return {
                "valid": False,
                "reason": "invalid_verification_manifest",
                "evidence_type": evidence_type or None,
                "problems": problems,
            }
        if evidence_type == "review_verdict":
            return {
                "valid": False,
                "reason": "review_verdict_is_not_executor_evidence",
                "evidence_type": evidence_type,
                "problems": ["review_verdict evidence only satisfies the reviewer verdict gate"],
            }
        # Root of trust (mac-ng2). The verification manifest must carry
        # ``signed_by`` (an agent_id) and ``signature`` (HMAC v1) made
        # with that agent's attestation key. Without this any executor
        # could self-approve by writing valid-looking JSON. Verification
        # is per-agent: the signer's key must be on file in the
        # ``agents.attestation_key_ciphertext`` column.
        signed_by = str(manifest.get("signed_by") or "").strip()
        signature = str(manifest.get("signature") or "").strip()
        if not signed_by or not signature:
            return {
                "valid": False,
                "reason": "manifest_not_signed",
                "evidence_type": evidence_type,
                "problems": ["verification.signed_by and verification.signature are required"],
            }
        signer_key = self._agent_attestation_key(signed_by)
        if signer_key is None:
            return {
                "valid": False,
                "reason": "signer_unknown",
                "evidence_type": evidence_type,
                "problems": ["verification.signed_by does not match a known agent with an attestation key"],
            }
        if not verify_verification_manifest_signature(signer_key, manifest, signature):
            return {
                "valid": False,
                "reason": "signature_invalid",
                "evidence_type": evidence_type,
                "problems": ["verification.signature does not verify against signed_by's attestation key"],
            }
        type_problems = self._verification_type_problems(task, manifest, evidence_type)
        if type_problems:
            return {
                "valid": False,
                "reason": "verification_contract_failed",
                "evidence_type": evidence_type,
                "problems": type_problems,
            }
        return {
            "valid": True,
            "reason": "verification_contract_satisfied",
            "evidence_type": evidence_type,
            "signed_by": signed_by,
            "verified_by": "default-review-evidence-v1",
        }

    def _verification_type_problems(
        self,
        task: Task,
        manifest: JsonDict,
        evidence_type: str,
    ) -> List[str]:
        problems = validate_evidence_type(
            evidence_type,
            manifest,
            passed_check_count=self._passed_verification_check_count,
            allow_empty_repo_change=self._allows_empty_repo_change_evidence(task, evidence_type),
            repo_coupled=self._task_is_repo_coupled(task),
            require_tests=self._task_requires_tests(task),
        )
        problems.extend(self._required_changed_file_problems(task, manifest))
        return problems

    def _required_changed_file_problems(self, task: Task, manifest: JsonDict) -> List[str]:
        required = _required_changed_files_from_metadata(ensure_json_object(task.metadata))
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
        return [
            "repo evidence missing required changed files: %s" % ", ".join(missing)
        ]

    def _allows_empty_repo_change_evidence(self, task: Task, evidence_type: str) -> bool:
        if str(evidence_type or "").strip().lower() != "repo_change":
            return False
        metadata = ensure_json_object(task.metadata)
        origin = ensure_json_object(metadata.get("origin"))
        remediation = ensure_json_object(metadata.get("remediation"))
        return origin.get("type") == "beads_source_remediation" or remediation.get(
            "type"
        ) == "beads_source_refresh"

    def _task_is_repo_coupled(self, task: Task) -> bool:
        """mem-11: True when the task carries a repository_contract — a code task
        expected to produce a pushed repo change. ``operator_result`` evidence is
        rejected for such tasks (the verified task_d7c51a0b jam was a code task
        that emitted a free-text operator_result with no commit/push)."""
        metadata = ensure_json_object(task.metadata)
        # An explicitly declared report/answer task is non-code; operator_result
        # is its correct evidence type, so it is not repo-coupled here.
        if metadata_declares_report_deliverable(metadata):
            return False
        for path in (
            ("execution_contract", "repository_contract"),
            ("origin", "repository_contract"),
            ("repository_contract",),
        ):
            contract = _nested_json_object(metadata, *path)
            if isinstance(contract, dict) and contract:
                return True
        return False

    def _task_requires_tests(self, task: Task) -> bool:
        """mac-wjy3: True when the task's repository_contract explicitly lists
        ``tests`` in its required evidence. Conservative — config/remediation
        tasks (which don't opt in) are unaffected; only contracts that demand
        tests reject a tests:null manifest."""
        metadata = ensure_json_object(task.metadata)
        for path in (
            ("execution_contract", "repository_contract"),
            ("origin", "repository_contract"),
            ("repository_contract",),
        ):
            contract = _nested_json_object(metadata, *path)
            if not isinstance(contract, dict) or not contract:
                continue
            evidence = ensure_json_object(contract.get("evidence"))
            required = evidence.get("required") if isinstance(evidence.get("required"), list) else []
            if any(str(r).strip().lower() in {"tests", "test"} for r in required):
                return True
        return False

    def _require_pushed_repo_anchor(self, manifest: JsonDict) -> List[str]:
        # Canonical field names only (mac-q38). The previous code
        # accepted ``git``/``commit``/``commit_sha``/``changed_files``/
        # ``pushed_ref``/``pull_request_url``/etc. — each alias is a
        # separate doorway. Single canonical schema:
        #   verification.repo: { head_sha, files_changed, dirty, pushed,
        #                        remote_ref, pr_url? }
        repo = manifest.get("repo")
        if not isinstance(repo, dict):
            return ["repo evidence requires verification.repo object"]
        problems: List[str] = []
        head_sha = str(repo.get("head_sha") or "").strip()
        if not _GIT_SHA_RE.match(head_sha):
            problems.append("repo.head_sha must be a git SHA")
        dirty = repo.get("dirty")
        if dirty not in {False, "false", "False", 0, "0"}:
            problems.append("repo evidence must declare dirty=false")
        pushed = repo.get("pushed") is True or str(repo.get("pushed") or "").lower() == "true"
        remote_ref = str(repo.get("remote_ref") or "").strip()
        pr_url = str(repo.get("pr_url") or "").strip()
        if not (pushed and remote_ref) and not pr_url:
            problems.append("repo evidence requires pushed=true with remote_ref, or pr_url")
        return problems

    def _passed_verification_check_count(self, manifest: JsonDict) -> int:
        # Canonical names only (mac-q38): ``tests`` and ``checks``.
        # ``test_runs`` was an alias; rejecting it here.
        count = 0
        for item in self._verification_item_candidates(manifest.get("tests")):
            if self._verification_item_passed(item):
                count += 1
        for item in self._verification_item_candidates(manifest.get("checks")):
            if self._verification_item_passed(item):
                count += 1
        return count

    def _verification_item_candidates(self, value: Any) -> List[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value]
        return []

    def _verification_item_passed(self, item: Any) -> bool:
        # Keep repo fields canonical, but accept common structured pass/fail
        # spellings for test/check result objects. Agents and tools naturally
        # emit "result=passed", booleans, and nested smoke/full-suite records;
        # rejecting those equivalent facts caused good pushed work to dead-letter.
        if isinstance(item, list):
            return any(self._verification_item_passed(nested) for nested in item)
        if not isinstance(item, dict):
            return False
        if "returncode" in item:
            return self._verification_int_value(item["returncode"]) == 0
        failed = self._verification_int_value(item.get("failed"))
        if failed is not None and failed > 0:
            return False
        if str(item.get("status") or "").strip().lower() in {
            "pass",
            "passed",
            "success",
            "successful",
            "succeeded",
            "ok",
        }:
            return True
        if str(item.get("result") or "").strip().lower() in {
            "pass",
            "passed",
            "success",
            "successful",
            "succeeded",
            "ok",
        }:
            return True
        if str(item.get("outcome") or "").strip().lower() in {
            "pass",
            "passed",
            "success",
            "successful",
            "succeeded",
            "ok",
        }:
            return True
        for key in ("passed", "success", "succeeded", "ok", "satisfied"):
            value = item.get(key)
            if value is True:
                return True
            number = self._verification_int_value(value)
            if number is not None and number > 0 and failed == 0:
                return True
        bool_values = [value for value in item.values() if isinstance(value, bool)]
        if bool_values and len(bool_values) == len(item) and all(bool_values):
            return True
        return any(
            self._verification_item_passed(nested)
            for nested in item.values()
            if isinstance(nested, (dict, list))
        )

    def _verification_int_value(self, value: Any) -> Optional[int]:
        try:
            if isinstance(value, bool):
                return int(value)
            return int(value)
        except (TypeError, ValueError):
            return None

    def _evidence_returncode(self, evidence: Evidence) -> Optional[int]:
        value = evidence.metadata.get("returncode")
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _hub_verify_repo_info(
        self, task: Task, executor_evidence: Evidence
    ) -> Optional[Dict[str, str]]:
        """Extract the pushed-branch coordinates the hub verifier needs: the
        remote (evidence repo.remote_url, else the task's canonical contract
        remote), the branch, and head_sha. Returns None when the evidence is
        not a pushed repo change (nothing to independently verify)."""
        meta = ensure_json_object(executor_evidence.metadata)
        verification = ensure_json_object(meta.get("verification"))
        repo = ensure_json_object(verification.get("repo"))
        remote_url = str(repo.get("remote_url") or "").strip()
        if not remote_url:
            md = ensure_json_object(task.metadata)
            for path in (
                ("execution_contract", "repository_contract"),
                ("origin", "repository_contract"),
                ("repository_contract",),
                ("origin",),
            ):
                node = _nested_json_object(md, *path)
                remote_url = str(
                    node.get("canonical_remote_url") or node.get("repository_url") or ""
                ).strip()
                if remote_url:
                    break
        head_sha = str(repo.get("head_sha") or "").strip()
        remote_ref = str(repo.get("remote_ref") or "").strip()
        branch = remote_ref[len("refs/heads/"):] if remote_ref.startswith("refs/heads/") else remote_ref
        if not remote_url or not _GIT_SHA_RE.match(head_sha) or not branch:
            return None
        if repo.get("pushed") is not True:
            return None
        return {"remote_url": remote_url, "head_sha": head_sha, "branch": branch}

    def _hub_verify_run_contract_test(
        self, remote_url: str, branch: str, head_sha: str, test_command: str
    ) -> Tuple[int, str]:
        """Clone the pushed branch and run the contract test in an isolated
        OpenShell sandbox on the hub. Returns (returncode, tail_of_output).

        Isolation is mandatory: this executes pushed (agent-authored) test code
        on the control-plane node, so it must not run on the hub host. Injected
        via MAC_HUB_VERIFY_RUNNER-style override in tests (see the
        ``_hub_verify_runner`` hook) so unit tests need no git/OpenShell."""
        runner = getattr(self, "_hub_verify_runner", None)
        if runner is not None:
            return runner(remote_url, branch, head_sha, test_command)
        from . import gitops as _gitops

        auth_url = _gitops.inject_git_remote_auth(remote_url)
        openshell = (os.environ.get("MAC_OPENSHELL_BIN") or "openshell").strip() or "openshell"
        image = (os.environ.get("MAC_HUB_VERIFY_IMAGE") or "localhost/mac-hermes:net").strip()
        policy = (os.environ.get("MAC_OPENSHELL_POLICY") or "").strip()
        try:
            timeout = float(os.environ.get("MAC_HUB_VERIFY_TIMEOUT", "1200"))
        except ValueError:
            timeout = 1200.0
        import uuid as _uuid

        tmp = Path(tempfile.mkdtemp(prefix="mac-hubverify-"))
        # Unique per invocation: the review sweep may re-tick while a verify is
        # still running, and a head_sha-derived name collides ("already
        # exists"). The in-flight guard in the caller also prevents overlap,
        # but a unique name is the belt-and-suspenders.
        name = "mac-hubverify-%s" % _uuid.uuid4().hex[:16]
        try:
            clone = subprocess.run(
                # Shallow single-branch clone keeps the upload into the sandbox
                # small (a deep clone's history broke the tar-over-ssh upload
                # with a broken pipe).
                ["git", "clone", "--branch", branch, "--depth", "1", "--single-branch",
                 "--", auth_url, str(tmp / "repo")],
                capture_output=True, text=True, timeout=300, check=False,
            )
            if clone.returncode != 0:
                return 1, "hub verify clone failed: %s" % _gitops.redact_git_remote_auth_in_text(
                    (clone.stderr or clone.stdout or "").strip()
                )[-800:]
            subprocess.run([openshell, "sandbox", "delete", name],
                           capture_output=True, text=True, timeout=60, check=False)
            argv = [openshell, "sandbox", "create", "--no-auto-providers"]
            if policy:
                argv += ["--policy", policy]
            argv += [
                "--name", name, "--from", image, "--env", "HOME=/tmp",
                "--upload", "%s:%s" % (str(tmp / "repo"), "/sandbox"),
                "--", "bash", "-c",
                "cd /sandbox/repo && %s" % (test_command or "scripts/run-contract-tests.sh"),
            ]
            try:
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
                out = (proc.stdout or "") + (proc.stderr or "")
                return int(proc.returncode), out[-2000:]
            finally:
                subprocess.run([openshell, "sandbox", "delete", name], capture_output=True, text=True, timeout=60, check=False)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _run_hub_review_verification(
        self, task: Task, review: Review, executor_evidence: Evidence, actor: str
    ) -> Optional[Evidence]:
        """Produce a signed review_verdict by running the contract test on the
        hub (Option C), on behalf of the selected reviewer. No-op returning None
        when the evidence isn't a pushed repo change or the reviewer has no key
        (the workflow then falls back to the agent-nudge path)."""
        info = self._hub_verify_repo_info(task, executor_evidence)
        if info is None:
            return None
        key = self._agent_attestation_key(review.reviewer_agent_id)
        if not key:
            return None
        # In-flight guard: the review sweep re-ticks (~30s) while a verify runs
        # for minutes; without this, each tick would launch another concurrent
        # sandbox for the same review. One verify per review at a time.
        inflight = getattr(self, "_hub_verify_inflight", None)
        if inflight is None:
            inflight = self._hub_verify_inflight = set()
        if review.id in inflight:
            return None
        inflight.add(review.id)
        try:
            return self._run_hub_review_verification_locked(
                task, review, executor_evidence, actor, info, key
            )
        finally:
            inflight.discard(review.id)

    def _run_hub_review_verification_locked(
        self, task: Task, review: Review, executor_evidence: Evidence, actor: str,
        info: Dict[str, str], key: str,
    ) -> Optional[Evidence]:
        test_command = _repository_contract_test_command_for_task(task)
        try:
            returncode, output = self._hub_verify_run_contract_test(
                info["remote_url"], info["branch"], info["head_sha"], test_command
            )
        except Exception as exc:  # noqa: BLE001 - a verify crash must not wedge the workflow
            self._record_default_review_observation(
                task.id, "workflow.default_review.hub_verify_error", "warning",
                {"review_id": review.id, "error": str(exc)[:300]}, actor,
            )
            return None
        verdict = "approved" if returncode == 0 else "rejected"
        manifest: Dict[str, Any] = {
            "schema": VERIFICATION_SCHEMA,
            "status": "complete",
            "evidence_type": "review_verdict",
            "verdict": verdict,
            "reviewed_evidence_id": executor_evidence.id,
            "worktree_digest": "sha256:%s" % hashlib.sha256(info["head_sha"].encode()).hexdigest(),
            "verified_by": "hub_review_verifier_v1",
            # Pushed-branch anchor for the verdict — the exact commit the hub
            # cloned and tested (mirrors the reviewed executor evidence).
            "repo": {
                "head_sha": info["head_sha"],
                "dirty": False,
                "pushed": True,
                "remote_ref": "refs/heads/%s" % info["branch"],
                # Mirror the reviewed change's file set — the verdict attests to
                # the same commit's files.
                "files_changed": _nested_json_object(
                    ensure_json_object(executor_evidence.metadata), "verification", "repo"
                ).get("files_changed") or [],
            },
            "tests": [
                {
                    "name": "hub contract verification",
                    "command": test_command or "scripts/run-contract-tests.sh",
                    "returncode": int(returncode),
                    "status": "pass" if returncode == 0 else "fail",
                }
            ],
            "signed_by": review.reviewer_agent_id,
        }
        # Carry the reviewed commit's codegraph audit (source/build changes
        # require it); the hub verified the same tree, so the executor's audit
        # result is the applicable one.
        exec_codegraph = _nested_json_object(
            ensure_json_object(executor_evidence.metadata), "verification"
        ).get("codegraph")
        if isinstance(exec_codegraph, dict) and exec_codegraph:
            manifest["codegraph"] = exec_codegraph
        if verdict == "rejected":
            manifest["feedback"] = "hub contract verification failed: %s" % (output[-500:] or "nonzero exit")
        manifest["signature"] = sign_verification_manifest(key, manifest)
        evidence = self.add_evidence(
            task.id,
            "review",
            "hub-verify://%s/%s" % (review.id, info["head_sha"][:12]),
            "hub review verification: %s (rc=%d)" % (verdict, returncode),
            review.reviewer_agent_id,
            metadata={"returncode": 0, "verification": manifest, "hub_verified": True},
        )
        self._record_default_review_observation(
            task.id, "workflow.default_review.hub_verified", "info",
            {"review_id": review.id, "verdict": verdict, "returncode": returncode,
             "reviewer_agent_id": review.reviewer_agent_id}, actor,
        )
        return evidence

    def _default_review_for_task(self, task_id: str) -> Optional[Review]:
        """Return the unambiguous review row to act on, or None.

        Refuses to pick when the task has more than one pending review
        (mac-d9c) — that's an ambiguous state and in an autonomous
        swarm there's no operator to break the tie. The caller logs
        ``workflow.default_review.ambiguous`` and leaves the task
        alone for explicit resolution.
        """
        reviews = self.list_reviews(task_id)
        if not reviews:
            return None
        pending = [review for review in reviews if review.status == ReviewStatus.PENDING.value]
        if len(pending) > 1:
            return None
        if pending:
            return pending[0]
        approved = [review for review in reviews if review.status == ReviewStatus.APPROVED.value]
        if approved:
            return approved[-1]
        return None

    def _review_verdict_nudge_payload(
        self,
        task_id: str,
        review: Review,
        evidence: Evidence,
    ) -> JsonDict:
        return {
            "task_id": task_id,
            "review_id": review.id,
            "executor_evidence_id": evidence.id,
            "reason": "produce_review_verdict",
        }

    def _ensure_review_verdict_nudge(
        self,
        task_id: str,
        review: Review,
        evidence: Evidence,
    ) -> Optional[AgentMessage]:
        # mac-ykkc: cap the number of times this review can be
        # re-nudged. Without the cap a reviewer that keeps failing to
        # produce a verdict (e.g. because the executor's lease branch
        # never made it to origin) ends up with hundreds of delivered
        # nudges as the dispatcher recreates the message on every tick.
        # Count those durable delivery attempts directly: review claims are
        # idempotent and therefore cannot serve as an attempt counter. After
        # the cap, retract the review with
        # a clear reason so the parent task transitions back to OPEN
        # or FAILED instead of spinning forever.
        try:
            attempt_count = int(os.environ.get("MAC_REVIEW_NUDGE_MAX_ATTEMPTS", "10"))
        except ValueError:
            attempt_count = 10
        attempt_row = self.store.query_one(
            """
            SELECT COUNT(*) AS n FROM messages
            WHERE task_id = ?
              AND recipient_agent_id = ?
              AND message_type = ?
              AND status = ?
              AND json_extract(payload, '$.reason') = 'produce_review_verdict'
              AND json_extract(payload, '$.review_id') = ?
            """,
            (
                task_id,
                review.reviewer_agent_id,
                MessageType.NUDGE.value,
                MessageStatus.DELIVERED.value,
                review.id,
            ),
        )
        prior_attempts = int(attempt_row["n"]) if attempt_row else 0
        if prior_attempts >= attempt_count:
            self._retract_default_review(
                review,
                "dispatcher",
                "reviewer_unable_to_produce_verdict_after_%d_attempts" % prior_attempts,
            )
            self.record_log(
                "workflow.default_review.nudge_capped",
                layer="control_plane",
                source="dispatcher",
                level="warning",
                subject_type="task",
                subject_id=task_id,
                detail={
                    "review_id": review.id,
                    "reviewer_agent_id": review.reviewer_agent_id,
                    "attempt_count": prior_attempts,
                    "cap": attempt_count,
                },
            )
            return None
        payload = self._review_verdict_nudge_payload(task_id, review, evidence)
        if self.messaging.has_queued_message(
            recipient_agent_id=review.reviewer_agent_id,
            task_id=task_id,
            message_type=MessageType.NUDGE.value,
            payload_contains=payload,
        ):
            return None
        # Nudge the reviewer so an autonomous review-executor has something to react to.
        return self.send_message(
            "dispatcher",
            review.reviewer_agent_id,
            MessageType.NUDGE.value,
            payload,
            task_id=task_id,
        )

    def _dedupe_same_reviewer_pending_reviews(
        self,
        pending_reviews: List[Review],
        actor: str,
    ) -> List[Review]:
        kept: List[Review] = []
        seen_reviewers: set[str] = set()
        retracted: List[Review] = []
        for review in sorted(pending_reviews, key=lambda item: (item.created_at, item.id)):
            if review.reviewer_agent_id in seen_reviewers:
                self._retract_default_review(
                    review,
                    actor,
                    "duplicate_pending_review_same_reviewer",
                )
                retracted.append(review)
                continue
            seen_reviewers.add(review.reviewer_agent_id)
            kept.append(review)
        if retracted:
            self._record_default_review_observation(
                kept[0].task_id if kept else retracted[0].task_id,
                "workflow.default_review.duplicate_pending_retracted",
                "warning",
                {
                    "retracted_review_ids": [review.id for review in retracted],
                    "kept_review_ids": [review.id for review in kept],
                    "reason": "duplicate_pending_review_same_reviewer",
                },
                actor,
            )
        return kept

    def _find_review_verdict_evidence(
        self,
        task_id: str,
        reviewer_agent_id: str,
        *,
        executor_evidence_id: str,
        verdict_evidence_id: Optional[str] = None,
        not_before: Optional[str] = None,
    ) -> Tuple[Optional[Evidence], List[str]]:
        """Locate the reviewer's signed verdict evidence row, or return
        ``(None, problems)`` if it doesn't exist yet (mac-jqb v1).

        The verdict is a separate Evidence row authored by the reviewer
        (not the executor) with a signed verification manifest of type
        ``review_verdict`` that names the executor's evidence_id.
        Without this row the workflow blocks — it will no longer
        auto-approve in the same process that selected the reviewer.

        Shape required for a valid verdict:
            evidence.metadata.returncode == 0
            evidence.metadata.verification:
                schema = mac.worker_evidence.v1
                status = complete
                evidence_type = review_verdict
                verdict in {approved, rejected}
                reviewed_evidence_id == executor_evidence_id
                signed_by = <reviewer_agent_id>
                signature = <HMAC of manifest under reviewer's key>
        """
        problems: List[str] = []
        reviewed_task = self.get_task(task_id)
        for evidence in reversed(self.list_evidence(task_id)):
            if verdict_evidence_id is not None and evidence.id != verdict_evidence_id:
                continue
            if evidence.created_by != reviewer_agent_id:
                continue
            if not_before is not None:
                try:
                    if parse_time(evidence.created_at) < parse_time(not_before):
                        problems.append(
                            "verdict %s predates review request" % evidence.id
                        )
                        continue
                except ValueError:
                    problems.append("verdict %s has invalid created_at" % evidence.id)
                    continue
            if self._evidence_returncode(evidence) != 0:
                problems.append("verdict evidence %s has nonzero returncode" % evidence.id)
                continue
            manifest = evidence.metadata.get("verification")
            if not isinstance(manifest, dict):
                problems.append("verdict evidence %s missing verification manifest" % evidence.id)
                continue
            if str(manifest.get("evidence_type") or "").strip().lower() != "review_verdict":
                continue  # not a verdict evidence row, skip silently
            if str(manifest.get("schema") or "").strip() != VERIFICATION_SCHEMA:
                problems.append("verdict %s schema mismatch" % evidence.id)
                continue
            if str(manifest.get("status") or "").strip().lower() != "complete":
                problems.append("verdict %s status not complete" % evidence.id)
                continue
            reviewed = str(manifest.get("reviewed_evidence_id") or "").strip()
            if reviewed != executor_evidence_id:
                problems.append(
                    "verdict %s references wrong executor evidence: %s != %s"
                    % (evidence.id, reviewed, executor_evidence_id)
                )
                continue
            signed_by = str(manifest.get("signed_by") or "").strip()
            signature = str(manifest.get("signature") or "").strip()
            if signed_by != reviewer_agent_id:
                problems.append("verdict %s signed_by != reviewer" % evidence.id)
                continue
            key = self._agent_attestation_key(signed_by)
            if key is None:
                problems.append("verdict %s signer has no attestation key" % evidence.id)
                continue
            if not verify_verification_manifest_signature(key, manifest, signature):
                # mac-s2vz: if the signer's key was rotated AFTER this
                # evidence was created, surface a clear "key rotated"
                # error with recovery guidance instead of the generic
                # "signature does not verify" message.
                rotation_row = self.store.query_one(
                    "SELECT attestation_key_rotated_at FROM agents WHERE id = ?",
                    (signed_by,),
                )
                rotated_at = rotation_row["attestation_key_rotated_at"] if rotation_row else None
                if rotated_at and evidence.created_at:
                    try:
                        if parse_time(rotated_at) > parse_time(evidence.created_at):
                            problems.append(
                                "verdict %s signed under rotated attestation key "
                                "(rotated_at=%s, evidence created_at=%s); "
                                "the reviewer must re-sign with the current key"
                                % (evidence.id, rotated_at, evidence.created_at)
                            )
                            continue
                    except ValueError:
                        pass
                problems.append("verdict %s signature does not verify" % evidence.id)
                continue
            executor_evidence = self.get_evidence(executor_evidence_id)
            executor_manifest = executor_evidence.metadata.get("verification") or {}
            if not isinstance(executor_manifest, dict):
                problems.append("verdict %s cannot resolve executor verification manifest" % evidence.id)
                continue
            verdict = str(manifest.get("verdict") or "").strip().lower()
            if verdict not in {"approved", "rejected"}:
                problems.append("verdict %s requires verdict approved or rejected" % evidence.id)
                continue
            digest = str(manifest.get("worktree_digest") or "").strip()
            if not re.match(r"^sha256:[0-9a-f]{64}$", digest):
                problems.append("verdict %s requires worktree_digest sha256" % evidence.id)
                continue
            llm_problems = cross_llm_review_problems(executor_manifest, manifest)
            if llm_problems:
                problems.extend(
                    "verdict %s %s" % (evidence.id, problem)
                    for problem in llm_problems
                )
                continue
            if verdict == "rejected":
                feedback_problems = rejected_verdict_feedback_problems(manifest)
                if feedback_problems:
                    problems.extend(
                        "verdict %s %s" % (evidence.id, problem)
                        for problem in feedback_problems
                    )
                    continue
                return evidence, []
            executor_repo = executor_manifest.get("repo")
            if isinstance(executor_repo, dict):
                repo_problems = self._require_pushed_repo_anchor(manifest)
                if repo_problems:
                    problems.extend("verdict %s %s" % (evidence.id, problem) for problem in repo_problems)
                    continue
                review_repo = manifest.get("repo") if isinstance(manifest.get("repo"), dict) else {}
                executor_changed = _metadata_path_list(executor_repo.get("files_changed"))
                review_changed = _metadata_path_list(review_repo.get("files_changed"))
                if executor_changed and set(review_changed) != set(executor_changed):
                    problems.append(
                        "verdict %s repo.files_changed does not match executor evidence: %s != %s"
                        % (evidence.id, review_changed, executor_changed)
                    )
                    continue
                reviewed_sha = str((manifest.get("repo") or {}).get("head_sha") or "").strip()
                executor_sha = str(executor_repo.get("head_sha") or "").strip()
                if reviewed_sha != executor_sha:
                    problems.append(
                        "verdict %s repo.head_sha does not match executor evidence: %s != %s"
                        % (evidence.id, reviewed_sha, executor_sha)
                    )
                    continue
                # mac-9kij: when the executor's evidence carries a local
                # repo path, recompute ``git rev-parse <head_sha>^{commit}``
                # to confirm the SHA actually exists in that repo. This
                # catches the "reviewer typed back what executor typed,
                # but neither pushed" failure mode for local-path repos.
                # Remote URLs (https/ssh) require network and are left
                # to a future ``git ls-remote`` check.
                repo_local_path = str(executor_repo.get("path") or "").strip()
                if repo_local_path:
                    from pathlib import Path as _RPath
                    from subprocess import run as _run, PIPE as _PIPE
                    candidate = _RPath(repo_local_path).expanduser()
                    if candidate.is_dir() and (candidate / ".git").exists():
                        try:
                            check = _run(
                                ["git", "rev-parse", "--verify",
                                 "%s^{commit}" % executor_sha],
                                cwd=str(candidate),
                                stdout=_PIPE,
                                stderr=_PIPE,
                                timeout=10,
                            )
                        except Exception:  # noqa: BLE001 - tooling missing → skip
                            check = None
                        if check is not None and check.returncode != 0:
                            problems.append(
                                "verdict %s executor head_sha %s not reachable in %s"
                                % (evidence.id, executor_sha, candidate)
                            )
                            continue
            if self._passed_verification_check_count(manifest) < 1:
                problems.append("verdict %s requires at least one independent passing check" % evidence.id)
                continue
            integration_problems = self._cooperative_review_integration_problems(
                reviewed_task, manifest
            )
            if integration_problems:
                problems.extend(
                    "verdict %s %s" % (evidence.id, problem)
                    for problem in integration_problems
                )
                continue
            codegraph_manifest = dict(manifest)
            if isinstance(executor_manifest.get("repo"), dict):
                review_repo = manifest.get("repo") if isinstance(manifest.get("repo"), dict) else {}
                codegraph_manifest["repo"] = {
                    **review_repo,
                    "files_changed": executor_manifest["repo"].get("files_changed") or [],
                }
            codegraph_problems = codegraph_audit_manifest_problems(codegraph_manifest)
            if codegraph_problems:
                problems.extend("verdict %s %s" % (evidence.id, problem) for problem in codegraph_problems)
                continue
            return evidence, []
        return None, problems

    def _cooperative_review_integration_problems(
        self, task: Task, verdict_manifest: JsonDict
    ) -> List[str]:
        coordination = ensure_json_object(
            ensure_json_object(task.metadata).get("coordination")
        )
        if coordination.get("phase") != "integration":
            return []
        child_outputs = coordination.get("child_outputs", [])
        expected = {
            str(item.get("executor_evidence_id") or "").strip()
            for item in child_outputs
            if isinstance(item, dict)
            and str(item.get("executor_evidence_id") or "").strip()
        }
        missing_outputs = [
            str(item.get("task_id") or "unknown").strip()
            for item in child_outputs
            if not isinstance(item, dict)
            or str(item.get("status") or "").strip() != "ready"
            or not str(item.get("executor_evidence_id") or "").strip()
            or not str(ensure_json_object(item.get("repo")).get("head_sha") or "").strip()
        ]
        integration = ensure_json_object(verdict_manifest.get("integration"))
        required = set(
            _metadata_string_list(integration.get("required_child_evidence_ids"))
        )
        verified = set(
            _metadata_string_list(integration.get("verified_child_evidence_ids"))
        )
        problems: List[str] = []
        if integration.get("status") != "pass":
            problems.append("cooperative integration verification must pass")
        if missing_outputs:
            problems.append(
                "cooperative integration has incomplete child outputs: %s"
                % ", ".join(sorted(set(missing_outputs)))
            )
        if not expected:
            problems.append("cooperative integration has no child evidence targets")
        if required != expected:
            problems.append(
                "cooperative integration required child evidence does not match task inputs"
            )
        if verified != expected:
            problems.append(
                "cooperative integration did not verify every child commit as an ancestor"
            )
        return problems

    def _verdict_value(self, evidence: Evidence) -> str:
        manifest = evidence.metadata.get("verification") or {}
        verdict = str(manifest.get("verdict") or "").strip().lower()
        # Fail closed: an unknown/malformed verdict must NOT auto-approve.
        return verdict if verdict in {"approved", "rejected"} else "rejected"

    def _retract_default_review(self, review: Review, actor: str, reason: str) -> None:
        now = utcnow()
        self.store.execute(
            """
            UPDATE reviews
            SET status = ?, reason = ?, completed_at = ?
            WHERE id = ? AND status = ?
            """,
            (
                ReviewStatus.RETRACTED.value,
                reason,
                now,
                review.id,
                ReviewStatus.PENDING.value,
            ),
        )
        self._record_history(
            review.task_id,
            "task.review_retracted",
            actor,
            None,
            None,
            {
                "review_id": review.id,
                "reviewer_agent_id": review.reviewer_agent_id,
                "reason": reason,
            },
        )

    def _select_default_reviewer(
        self,
        task: Task,
        *,
        executor_agent_id: Optional[str] = None,
    ) -> Optional[Agent]:
        """Pick a default reviewer for ``task``.

        Trust boundaries enforced here (autonomous-review context where
        there is no human in the loop):

        * Tenancy (mac-dyk): the reviewer's persona tenant_id must
          match the task's tenant. Without a human to catch a misroute,
          the tenancy boundary IS the safety boundary.
        * Capability (mac-s1a): ``review`` capability is *required*,
          not preferred. An agent without it cannot be drafted.
        * Persona separation / anti-collusion (mac-v2i): the reviewer's
          persona slug must differ from the executor's persona slug.
          Two code-reviewer-souled agents cannot approve each other's
          work — the second-eyes role only matters if it's a different
          eye.
        * Never an executor for this task: current and prior lease owners,
          plus the latest evidence author, are excluded. Small fleets wait
          for genuinely independent review rather than weakening the gate.
        """
        task_tenant = self._task_tenant_id(task)
        executor_persona_slug = self._task_executor_persona_slug(task)
        review_policy = self._default_review_policy(task)
        review_required_capabilities = self._default_review_required_capabilities(
            task,
            review_policy,
        )

        candidates: List[Agent] = []
        access_states: Dict[str, str] = {}
        for agent in self.list_agents():
            if self._default_reviewer_unavailable_reason(
                task,
                agent,
                task_tenant=task_tenant,
                executor_persona_slug=executor_persona_slug,
                executor_agent_id=executor_agent_id,
                review_policy=review_policy,
                review_required_capabilities=review_required_capabilities,
            ) is not None:
                continue
            candidates.append(agent)
            access_states[agent.id] = self._reviewer_repository_access_state(
                task,
                agent.id,
            )[0]
        if not candidates:
            return None
        candidates.sort(
            key=lambda agent: (
                0 if access_states.get(agent.id) == "success" else 1,
                0 if agent.status == AgentStatus.IDLE.value else 1,
                agent.name,
                agent.id,
            )
        )
        return candidates[0]

    def _default_reviewer_unavailable_reason_for_id(
        self,
        task: Task,
        reviewer_agent_id: str,
        *,
        executor_agent_id: Optional[str] = None,
    ) -> Optional[str]:
        try:
            agent = self.get_agent(reviewer_agent_id)
        except NotFoundError:
            return "reviewer_missing"
        return self._default_reviewer_unavailable_reason(
            task,
            agent,
            executor_agent_id=executor_agent_id,
            review_policy=self._default_review_policy(task),
        )

    def _default_reviewer_unavailable_reason(
        self,
        task: Task,
        agent: Agent,
        *,
        task_tenant: Optional[str] = None,
        executor_persona_slug: Optional[str] = None,
        executor_agent_id: Optional[str] = None,
        review_policy: Optional[JsonDict] = None,
        review_required_capabilities: Optional[Iterable[str]] = None,
    ) -> Optional[str]:
        if agent.id in self._coordination_excluded_agent_ids(task):
            return "reviewer_cooperative_family_participant"
        if agent.health_status != HealthStatus.HEALTHY.value:
            return "reviewer_unhealthy"
        if agent.status not in {AgentStatus.IDLE.value, AgentStatus.BUSY.value}:
            return "reviewer_not_available"
        if not self._agent_seen_recently(agent, self._default_reviewer_stale_after_seconds()):
            return "reviewer_stale"
        if self.reviews.agent_has_owned_task(task.id, agent.id):
            return "reviewer_previously_owned_task"
        if executor_agent_id is not None and agent.id == executor_agent_id:
            return "reviewer_created_executor_evidence"
        if "review" not in set(agent.capabilities):
            return "reviewer_missing_capability"
        policy = review_policy if review_policy is not None else self._default_review_policy(task)
        target_agent_id = str(
            policy.get("target_agent_id")
            or policy.get("reviewer_agent_id")
            or ""
        ).strip()
        if target_agent_id and agent.id != target_agent_id:
            return "reviewer_not_target_agent"
        target_agent_name = str(
            policy.get("target_agent_name")
            or policy.get("reviewer_agent_name")
            or ""
        ).strip()
        if target_agent_name and agent.name != target_agent_name:
            return "reviewer_not_target_agent"
        required = set(
            review_required_capabilities
            if review_required_capabilities is not None
            else self._default_review_required_capabilities(task, policy)
        )
        missing = sorted(required - set(agent.capabilities))
        if missing:
            return "reviewer_missing_capabilities:%s" % ",".join(missing)
        if task_tenant is None:
            task_tenant = self._task_tenant_id(task)
        if executor_persona_slug is None:
            executor_persona_slug = self._task_executor_persona_slug(task)
        agent_tenant, agent_persona_slug = self._agent_tenant_and_persona(agent)
        if task_tenant is not None:
            if agent_tenant is None:
                # Headless worker (no hermes_instance_id => no persona
                # tenant). Persona-boundary tenancy fails closed for these
                # agents, which would park every tenant-scoped Hermes task
                # forever in needs_review. Fall back to the hardware
                # boundary — the same gate the executor path uses
                # (_agent_available_for) — so a headless reviewer on a
                # machine whose tenant policy permits the task's tenant
                # stays eligible, while one on a disallowed machine is
                # still refused.
                try:
                    machine = self.get_machine(agent.machine_id)
                except NotFoundError:
                    return "reviewer_wrong_tenant"
                if not self._machine_allows_tenant(machine, task_tenant):
                    return "reviewer_wrong_tenant"
            elif agent_tenant != task_tenant:
                return "reviewer_wrong_tenant"
        if (
            executor_persona_slug is not None
            and agent_persona_slug is not None
            and agent_persona_slug == executor_persona_slug
        ):
            return "reviewer_same_persona"
        access_state, learning = self._reviewer_repository_access_state(task, agent.id)
        if access_state == "failure":
            host = str((learning or {}).get("repository_host") or "unknown")
            failure_class = str(
                (learning or {}).get("failure_class") or "authentication"
            )
            return "reviewer_repository_access_%s:%s" % (failure_class, host)
        return None

    def _reviewer_repository_access_state(
        self,
        task: Task,
        reviewer_agent_id: str,
    ) -> Tuple[str, Optional[JsonDict]]:
        remote = task_repository_remote(task)
        host = repository_host(remote)
        if not host or host == "local":
            return "unknown", None
        try:
            failure_cooldown = max(
                0,
                int(
                    os.environ.get(
                        "MAC_REPOSITORY_ACCESS_FAILURE_COOLDOWN_SECONDS",
                        "1800",
                    )
                ),
            )
        except ValueError:
            failure_cooldown = 1800
        try:
            success_ttl = max(
                0,
                int(
                    os.environ.get(
                        "MAC_REPOSITORY_ACCESS_SUCCESS_TTL_SECONDS",
                        "86400",
                    )
                ),
            )
        except ValueError:
            success_ttl = 86400
        records = self.memory.search_memory(
            subject_type="agent",
            subject_id=reviewer_agent_id,
            record_type=REPOSITORY_ACCESS_RECORD_TYPE,
            limit=50,
            order="desc",
        )
        state, learning = repository_access_state(
            records,
            project=task.project or "default",
            host=host,
            operation="review_clone",
            failure_cooldown_seconds=failure_cooldown,
            success_ttl_seconds=success_ttl,
        )
        return state, learning

    def _default_reviewer_stale_after_seconds(self) -> int:
        raw = (
            os.environ.get("MAC_DEFAULT_REVIEWER_STALE_AFTER_SECONDS", "").strip()
            or os.environ.get("MAC_AGENT_STALE_AFTER_SECONDS", "").strip()
        )
        if not raw:
            return 300
        try:
            return max(1, int(raw))
        except ValueError:
            return 300

    def _agent_seen_recently(self, agent: Agent, stale_after_seconds: int) -> bool:
        try:
            seen_at = parse_time(agent.last_seen_at)
            now = parse_time(utcnow())
        except Exception:  # noqa: BLE001 - malformed timestamps should fail closed.
            return False
        if seen_at.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        if seen_at.tzinfo is not None and now.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=None)
        return (now - seen_at).total_seconds() <= max(1, int(stale_after_seconds))

    def _agent_tenant_and_persona(self, agent: Agent) -> Tuple[Optional[str], Optional[str]]:
        """Return ``(tenant_id, persona_slug)`` for an agent, both
        optional. Used by the reviewer-selection guards (tenancy +
        anti-collusion). Agents without a hermes_instance_id have
        neither and are treated as ineligible by the reviewer picker
        when the task is tenant-scoped."""
        if not agent.hermes_instance_id:
            return None, None
        try:
            instance = self.identity.get_hermes_instance(agent.hermes_instance_id)
        except NotFoundError:
            return None, None
        if not instance.persona_id:
            return instance.tenant_id, None
        try:
            persona = self.identity.get_persona(instance.persona_id)
        except NotFoundError:
            return instance.tenant_id, None
        slug = persona.name.strip().lower().replace(" ", "-").replace("_", "-") or None
        return instance.tenant_id, slug

    def _reviewer_assignment_problem(
        self, task: Task, reviewer: Agent
    ) -> Optional[str]:
        """Apply the same complete eligibility policy to every assignment path."""
        executor_agent_id = self.reviews.latest_executor_evidence_author(task.id)
        reason = self._default_reviewer_unavailable_reason(
            task,
            reviewer,
            executor_agent_id=executor_agent_id,
            review_policy=self._default_review_policy(task),
        )
        if reason is None:
            return None
        readable = {
            "reviewer_same_persona": "reviewer and executor use the same persona",
            "reviewer_wrong_tenant": "reviewer is outside the task tenant boundary",
            "reviewer_cooperative_family_participant": (
                "reviewer executed another task in the same cooperative work family"
            ),
        }
        return readable.get(reason, reason.replace("_", " "))

    def _reviewer_independence_problem(
        self, task: Task, reviewer: Agent
    ) -> Optional[str]:
        """Compatibility form of the former independence-only policy."""
        if reviewer.id in self._coordination_excluded_agent_ids(task):
            return "reviewer executed another task in the same cooperative work family"
        task_tenant = self._task_tenant_id(task)
        reviewer_tenant, reviewer_persona = self._agent_tenant_and_persona(reviewer)
        if task_tenant is not None:
            if reviewer_tenant is None:
                try:
                    machine = self.get_machine(reviewer.machine_id)
                except NotFoundError:
                    return "reviewer machine is missing"
                if not self._machine_allows_tenant(machine, task_tenant):
                    return "reviewer is outside the task tenant boundary"
            elif reviewer_tenant != task_tenant:
                return "reviewer is outside the task tenant boundary"
        executor_persona = self._task_executor_persona_slug(task)
        if (
            executor_persona is not None
            and reviewer_persona is not None
            and executor_persona == reviewer_persona
        ):
            return "reviewer and executor use the same persona"
        return None

    def _task_executor_persona_slug(self, task: Task) -> Optional[str]:
        """Find the persona slug of whichever agent owned the task last
        (the executor). Used by anti-collusion. Returns None when the
        task has no recorded owner — in that case no executor-side
        persona constraint applies."""
        executor_agent_id: Optional[str] = task.owner_agent_id
        if executor_agent_id is None:
            # Look for the last lease against this task — it identifies
            # the executor even after submit-for-review releases owner.
            row = self.store.query_one(
                """
                SELECT agent_id FROM leases
                WHERE task_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (task.id,),
            )
            if row is not None:
                executor_agent_id = row["agent_id"]
        if executor_agent_id is None:
            return None
        try:
            executor = self.get_agent(executor_agent_id)
        except NotFoundError:
            return None
        _, slug = self._agent_tenant_and_persona(executor)
        return slug

    def _default_publication_target(self, task: Task) -> Optional[str]:
        """Resolve the publication target from task metadata or return None.

        Returns ``None`` when no operator-set target is available
        (mac-w29). Previously this synthesized ``mac://tasks/{id}`` which
        is filler — no resolver exists for that URI. The auto-review
        workflow now treats ``None`` as "no publication destination
        configured; leave the task in REVIEWING and emit a waiting
        observability event."
        """
        metadata = task.metadata
        for key in ("publication_target", "publish_target"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        publication = metadata.get("publication")
        if isinstance(publication, dict):
            target = publication.get("target")
            if isinstance(target, str) and target.strip():
                return target.strip()
        acc_metadata = metadata.get("acc_metadata")
        if isinstance(acc_metadata, dict):
            beads_id = acc_metadata.get("beads_id")
            if isinstance(beads_id, str) and beads_id.strip():
                return "beads://%s" % beads_id.strip()
        # Fall back to the task's registered project metadata so an
        # operator can configure a single publication target per project
        # (e.g. for autonomous coding tasks that all complete the same
        # way) instead of stamping every task individually.
        project_target = self._project_publication_target(task)
        if project_target:
            return project_target
        # Fleet-wide default (opt-in): when set, routine approved tasks publish
        # via this target and auto-complete instead of parking in REVIEWING for
        # want of a per-task/per-project destination. Unset => unchanged (mac-w29
        # hold). e.g. MAC_DEFAULT_PUBLICATION_TARGET=git://main
        #
        # A *git* fleet-default only applies where the HUB can actually publish
        # (origin pins a clonable url, or a hub-local repository_path exists);
        # applying it to a non-publishable task would raise in the git publish
        # (_publish_git_target_if_needed) and block the task. Non-git defaults
        # apply to all.
        fleet_default = (os.environ.get("MAC_DEFAULT_PUBLICATION_TARGET") or "").strip()
        if fleet_default and (
            not fleet_default.startswith("git://") or self._task_git_publishable(task)
        ):
            return fleet_default
        return None

    def _task_git_publishable(self, task: Task) -> bool:
        """True if a git publish/merge can be attempted for this task — i.e. it is
        a repo task (origin pins a repo url/path, or a repository_contract). The
        publish resolves a hub-usable repo from origin.repository_url, a hub-local
        origin.repository_path, or the remote the worker pushed to (from the
        evidence), so a repo task need not have a hub-local path. Non-repo
        (operator) tasks return False so a git fleet-default never blocks them."""
        origin = ensure_json_object(ensure_json_object(task.metadata).get("origin"))
        if str(origin.get("repository_url") or "").strip():
            return True
        if str(origin.get("repository_path") or "").strip():
            return True
        contract = ensure_json_object(origin.get("repository_contract"))
        return bool(
            str(contract.get("project") or "").strip()
            or str(contract.get("canonical_remote_url") or "").strip()
        )

    def _project_publication_target(self, task: Task) -> Optional[str]:
        project_name = str(getattr(task, "project", "") or "").strip()
        if not project_name:
            return None
        try:
            record = self.get_project_record(project_name)
        except NotFoundError:
            return None
        meta = ensure_json_object(record.metadata)
        for key in ("publication_target", "publish_target"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        publication = meta.get("publication")
        if isinstance(publication, dict):
            target = publication.get("target")
            if isinstance(target, str) and target.strip():
                return target.strip()
        return None

    def _record_default_review_observation(
        self,
        task_id: str,
        name: str,
        level: str,
        detail: JsonDict,
        actor: str,
    ) -> None:
        self.observability.record_log(
            name,
            level=level,
            layer="control_plane",
            source="default-review-workflow",
            subject_type="task",
            subject_id=task_id,
            detail={"actor": actor, **detail},
        )

    def _record_project_failure_lesson(
        self,
        task_id: str,
        *,
        evidence_type: str,
        error_signature: str,
        signals: Optional[JsonDict] = None,
        evidence_id: Optional[str] = None,
    ) -> None:
        """Persist a hub-side, project-scoped ``mac.deployment_learning.v1``
        failure lesson so the next execution run on this project recalls it.

        The executor records deployment lessons for failed *runs*, but review
        rejections and hub-side terminal failures produced no lesson — so the
        fleet never learned from rejected or force-failed work. The record
        shape matches the executor's ``build_learning_record`` so
        ``recall_deployment_lessons`` renders and injects it unchanged.
        Best-effort: telemetry-only, never breaks the caller.
        """
        try:
            task = self.get_task(task_id)
        except Exception:  # noqa: BLE001 - lesson recording must not break the workflow.
            return
        try:
            project = self._hermes_task_project_key(task) or (task.project or "unassigned")
            metadata = ensure_json_object(task.metadata)
            origin = metadata.get("origin") if isinstance(metadata, dict) else {}
            repo_name = str(origin.get("repository_name") or "") if isinstance(origin, dict) else ""
            content = {
                "schema": "mac.deployment_learning.v1",
                "task_id": task.id,
                "task_title": task.title,
                "project": project,
                "repository": repo_name,
                "evidence_type": evidence_type,
                "outcome": "failure",
                "signals": signals or {},
                "error_signature": error_signature or "",
                "at": utcnow(),
            }
            self.add_memory(
                task_id=task.id,
                subject_type="project",
                subject_id=project,
                # Matches the executor's DEPLOYMENT_LEARNING_PREFIX contract so
                # recall_deployment_lessons (which filters by this record_type
                # prefix + project) picks it up.
                record_type="deployment_learning:%s" % project,
                content=json_dumps(content),
                evidence_id=evidence_id,
                created_by="mac-hub-review",
            )
        except Exception:  # noqa: BLE001 - best-effort durable learning.
            import logging

            logging.getLogger(__name__).warning(
                "could not record project failure lesson for %s", task_id, exc_info=True
            )

    def _set_agent_idle(self, agent_id: str, conn: Any = None) -> None:
        now = utcnow()
        writer = conn if conn is not None else self.store
        writer.execute(
            "UPDATE agents SET status = ?, current_task_id = NULL, updated_at = ? WHERE id = ?",
            (AgentStatus.IDLE.value, now, agent_id),
        )

    def _agent_has_active_lease(self, agent_id: str) -> bool:
        row = self.store.query_one(
            """
            SELECT 1 FROM leases l
            JOIN tasks t ON t.lease_id = l.id
            WHERE l.agent_id = ?
              AND l.status = ?
              AND t.owner_agent_id = ?
            LIMIT 1
            """,
            (agent_id, LeaseStatus.ACTIVE.value, agent_id),
        )
        return row is not None

    def _agent_active_lease_count(self, agent_id: str) -> int:
        row = self.store.query_one(
            """
            SELECT COUNT(*) AS count FROM leases l
            JOIN tasks t ON t.lease_id = l.id
            WHERE l.agent_id = ?
              AND l.status = ?
              AND t.owner_agent_id = ?
            """,
            (agent_id, LeaseStatus.ACTIVE.value, agent_id),
        )
        return int(row["count"] if row is not None else 0)

    def _agent_capacity(self, agent: Agent) -> int:
        for key in ("capacity", "max_concurrent_tasks"):
            value = agent.resources.get(key)
            if value is not None:
                return max(1, int(value))
        return 1

    # --- service-role election (media-01) -------------------------------

    def _agent_eligible_for_service(self, agent: Agent, role: ServiceRole) -> bool:
        """Capability + hardware fit (uses reported GPU hw + the catalog VRAM
        floor). Capacity is checked separately in the sync loop."""
        if not set(role.required_capabilities).issubset(set(agent.capabilities or [])):
            return False
        hw = agent.resources.get("hardware") if isinstance(agent.resources, dict) else None
        if role.hardware_requirements:
            from mac.roles_service import machine_hardware_satisfies

            ok, _reasons = machine_hardware_satisfies(role.hardware_requirements, hw or {})
            if not ok:
                return False
        if role.model_id:
            try:
                from mac.local_gen_catalog import get_model, models_for_hardware

                model = get_model(role.model_id)
                if model is not None and model not in models_for_hardware(hw):
                    return False
            except Exception:  # noqa: BLE001 - catalog is optional
                pass
        return True

    def _service_holder_live(self, agent_id: str) -> bool:
        try:
            agent = self.get_agent(agent_id)
        except Exception:  # noqa: BLE001
            return False
        return agent.status != AgentStatus.OFFLINE.value

    def sync_agent_service_claims(
        self, agent_id: str, willing_ops: Iterable[str], *, lease_seconds: int = 1800
    ) -> JsonDict:
        """A capable host declares the ops it's willing+able to run; the hub
        renews its still-willing held ops, releases ones it no longer wants, and
        claims new eligible ops up to the agent's capacity (so ops spread to
        hosts with headroom). Returns the authoritative held-op set.

        Self-driving: worker syncs (every ~30s) seed desired roles + reap stale
        claims, so the subsystem doesn't depend on a periodic /dispatch/tick."""
        self._ensure_service_roles_seeded()
        self.service_roles.expire_service_claims()
        agent = self.get_agent(agent_id)
        willing = {str(op).strip() for op in (willing_ops or []) if str(op).strip()}
        capacity = self._agent_capacity(agent)
        roles_by_op = {r.op: r for r in self.service_roles.desired_services(tenant_id=None)}
        held_ops: Dict[str, Any] = {}
        for claim in self.service_roles.list_active_claims(agent_id=agent_id):
            try:
                op = self.service_roles.get_role(claim.service_role_id).op
            except Exception:  # noqa: BLE001
                continue
            held_ops[op] = claim
        # release held ops no longer willing/desired
        for op, claim in list(held_ops.items()):
            if op not in willing or op not in roles_by_op:
                self.service_roles.release_service_claim(claim.id, agent_id, reason="not_willing")
                del held_ops[op]
        # renew still-held
        for claim in held_ops.values():
            self.service_roles.renew_service_claim(claim.id, agent_id, lease_seconds)
        # claim new eligible willing ops, capacity-bounded. Prefer the LEAST-served
        # ops (fewest current live holders) so the pool spreads to cover every op
        # instead of every host piling onto the same one.
        load = len(held_ops) + self._agent_active_lease_count(agent_id)
        candidates = [
            op for op in willing
            if op not in held_ops and op in roles_by_op and roles_by_op[op].enabled
        ]

        def _holder_count(op: str) -> int:
            return len(self.service_roles.list_active_claims(role_id=roles_by_op[op].id))

        for op in sorted(candidates, key=lambda o: (_holder_count(o), o)):
            if load >= capacity:
                break  # at capacity -> leave the op for a host with headroom (spread)
            role = roles_by_op[op]
            if not self._agent_eligible_for_service(agent, role):
                continue
            try:
                self.service_roles.claim_service(role.id, agent_id, lease_seconds)
            except Exception:  # noqa: BLE001
                continue
            held_ops[op] = True
            load += 1
        # "managed" = ops that have a service_role (election active). Ops NOT managed
        # are advertised unconditionally by the worker (back-compat: a fleet that
        # seeds no service_roles keeps today's advertise-all behavior).
        return {
            "held": sorted(held_ops.keys()),
            "managed": sorted(roles_by_op.keys()),
            "capacity": capacity,
        }

    def _ensure_service_roles_seeded(self) -> None:
        """Idempotently seed the desired ops from MAC_SERVICE_ROLE_OPS (opt-in;
        unset = no election). Driven by both worker syncs and the tick."""
        ops_env = (os.environ.get("MAC_SERVICE_ROLE_OPS") or "").strip()
        if not ops_env:
            return
        wanted = [o.strip() for o in ops_env.split(",") if o.strip()]
        existing = {r.op for r in self.service_roles.desired_services(tenant_id=None)}
        if any(o not in existing for o in wanted):
            self.seed_service_roles(wanted)

    def reconcile_service_roles(self) -> JsonDict:
        """Periodic (called from tick): seed desired ops from MAC_SERVICE_ROLE_OPS
        (opt-in; unset = no election, agents advertise as before), expire silent/
        overloaded holders, drop offline holders, and emit a provisioning demand
        signal for any desired op with zero live holders ("the cluster needs a
        <op> agent")."""
        self._ensure_service_roles_seeded()
        expired = self.service_roles.expire_service_claims()
        requested: List[str] = []
        for role in self.service_roles.desired_services(tenant_id=None):
            live = [
                c for c in self.service_roles.list_active_claims(role_id=role.id)
                if self._service_holder_live(c.agent_id)
            ]
            for claim in self.service_roles.list_active_claims(role_id=role.id):
                if not self._service_holder_live(claim.agent_id):
                    self.service_roles.release_service_claim(claim.id, reason="holder_offline")
            if not live:
                try:
                    self.provisioning.request_agent(
                        reason="service_role:%s" % role.slug,
                        capabilities=role.required_capabilities,
                        hardware=role.hardware_requirements,
                        detail={"op": role.op, "model_id": role.model_id},
                        requested_by="service-role-reconciler",
                    )
                    requested.append(role.op)
                except Exception:  # noqa: BLE001 - demand signal is best-effort
                    pass
        return {"expired": len(expired), "requested": requested}

    def seed_service_roles(self, ops: Iterable[Any]) -> int:
        """Idempotently seed/enable a desired service_role per op. Each op is a
        dict {op, model_id, capabilities?} or a bare op string (model from the
        catalog)."""
        from mac.local_gen_catalog import LOCAL_GEN_MODELS

        by_op_default = {}
        for m in LOCAL_GEN_MODELS:
            if m.routable:
                by_op_default.setdefault(m.op, m.id)
        count = 0
        for spec in ops:
            if isinstance(spec, dict):
                op = str(spec.get("op") or "").strip()
                model_id = spec.get("model_id") or by_op_default.get(op)
                caps = spec.get("capabilities") or ["gpu"]
            else:
                op = str(spec).strip()
                model_id = by_op_default.get(op)
                caps = ["gpu"]
            if not op:
                continue
            self.service_roles.upsert_role(op, model_id=model_id, required_capabilities=caps)
            count += 1
        return count

    def _task_tenant_id(self, task: Task) -> Optional[str]:
        origin = task.metadata.get("origin")
        if isinstance(origin, dict) and origin.get("tenant_id"):
            return str(origin["tenant_id"])
        tenant_id = task.metadata.get("tenant_id")
        return str(tenant_id) if tenant_id else None

    def _machine_allows_tenant(self, machine: Machine, tenant_id: Optional[str]) -> bool:
        policy = machine.labels.get("tenant_policy") or {}
        if not isinstance(policy, dict):
            return True
        mode = str(policy.get("mode", "shared"))
        allowed = set(policy.get("tenant_ids") or policy.get("allow_tenants") or [])
        denied = set(policy.get("deny_tenants") or [])
        if mode == "denied":
            return False
        if tenant_id is None:
            return mode != "private"
        if tenant_id in denied:
            return False
        if mode == "private":
            return tenant_id in allowed
        if allowed:
            return tenant_id in allowed
        return True

    def _agent_resources_satisfy(self, agent: Agent, machine: Machine, task: Task) -> bool:
        required = task.metadata.get("resources") or task.metadata.get("required_resources") or {}
        if isinstance(required, dict):
            available = dict(machine.resources)
            available.update(agent.resources)
            for key, needed in required.items():
                current = available.get(key)
                if isinstance(needed, (int, float)):
                    if current is None or float(current) < float(needed):
                        return False
                elif isinstance(needed, list):
                    if not set(needed).issubset(set(current or [])):
                        return False
                elif needed is not None and current != needed:
                    return False
        # Structured hardware constraints on the task (set by the workflow
        # runtime when spawning a role-bound node task). Falls through the
        # shared matcher so the role-required-hardware vocabulary stays in
        # one place.
        hw_required = task.metadata.get("hardware") if isinstance(task.metadata, dict) else None
        if isinstance(hw_required, dict) and hw_required:
            from mac.roles_service import machine_hardware_satisfies

            ok, _reasons = machine_hardware_satisfies(hw_required, machine.hardware)
            if not ok:
                return False
        return True

    def _expire_agent_active_leases(self, agent_id: str, timestamp: str, reason: str) -> None:
        rows = self.store.query_all(
            """
            SELECT
                l.id AS lease_id,
                l.task_id AS task_id,
                t.state AS task_state,
                t.attempt_count AS attempt_count,
                t.max_attempts AS max_attempts
            FROM leases l
            JOIN tasks t ON t.lease_id = l.id
            WHERE l.agent_id = ?
              AND l.status = ?
              AND t.owner_agent_id = ?
            ORDER BY l.created_at, l.id
            """,
            (agent_id, LeaseStatus.ACTIVE.value, agent_id),
        )
        if not rows:
            return
        with self.store.transaction() as conn:
            for row in rows:
                next_state = (
                    TaskState.FAILED.value
                    if row["attempt_count"] >= row["max_attempts"]
                    else TaskState.OPEN.value
                )
                conn.execute(
                    "UPDATE leases SET status = ?, updated_at = ? WHERE id = ?",
                    (LeaseStatus.EXPIRED.value, timestamp, row["lease_id"]),
                )
                conn.execute(
                    """
                    UPDATE tasks
                    SET state = ?, owner_agent_id = NULL, lease_id = NULL, leased_until = NULL,
                        completed_at = CASE
                            WHEN ? = ? AND completed_at IS NULL THEN ?
                            ELSE completed_at
                        END,
                        updated_at = ?
                    WHERE id = ? AND lease_id = ?
                    """,
                    (
                        next_state,
                        next_state,
                        TaskState.FAILED.value,
                        timestamp,
                        timestamp,
                        row["task_id"],
                        row["lease_id"],
                    ),
                )
                detail = {
                    "lease_id": row["lease_id"],
                    "agent_id": agent_id,
                    "reason": reason,
                }
                self._record_history(
                    row["task_id"],
                    "task.lease_expired",
                    "dispatcher",
                    row["task_state"],
                    next_state,
                    detail,
                    conn,
                )
        for row in rows:
            self.drain_task_transition_outbox(task_id=row["task_id"], limit=20)
            # Self-documenting: a lease expiry means the agent stopped heartbeating
            # mid-task (offline / crash / long synchronous op like a big clone).
            # Record the cause + remediation on the task (visible in `mac task
            # show`/`summary`) whether it was requeued (OPEN) or failed out. The
            # BLOCKED target arg only selects the matching note text.
            try:
                diag = _failure_diagnosis(TaskState.BLOCKED.value, {"reason": reason})
                if diag:
                    self.append_task_activity(row["task_id"], "diagnosis", "dispatcher", diag)
            except Exception:  # noqa: BLE001 - advisory only
                pass
