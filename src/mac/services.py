from __future__ import annotations

import base64
import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import threading
import urllib.parse
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mac.models import (
    Agent,
    AgentProvisioningRequest,
    AgentRole,
    Workflow,
    WorkflowRun,
    AgentBusChunk,
    AgentBusStream,
    AgentBusStreamStatus,
    AgentMessage,
    AgentStatus,
    Artifact,
    AuthorizationError,
    BeadsRepository,
    COMMAND_AUDIT_PHASES,
    CommandAuditRecord,
    MoodMode,
    MoodOverlay,
    NapRun,
    NapSchedule,
    NapStatus,
    ConversationThread,
    Deployment,
    DeploymentStatus,
    Environment,
    EVIDENCE_KINDS,
    EvalRun,
    EvalSet,
    EvalTargetKind,
    Evidence,
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
    ROLLOUT_ACTIONS,
    RolloutStatus,
    RolloutStrategy,
    RuntimeEnvironment,
    RuntimeRun,
    RuntimeRunStatus,
    SecretAccess,
    SecretAuditResult,
    SecretHandle,
    SecretRecord,
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
from mac.agent_state_service import AgentStateService
from mac.agentbus_service import AgentBusService
from mac.beads_bridge_service import BeadsBridgeService
from mac.deploy_service import DeployService
from mac.evidence_validators import validate_evidence_type
from mac.eval_service import EvalService
from mac.identity_service import IdentityService
from mac.memory_service import MemoryService
from mac.messaging_service import MessagingService
from mac.notifier_service import NotifierService
from mac.observability_service import ObservabilityService
from mac.provisioning_service import ProvisioningService
from mac.review_service import ReviewService
from mac.roles_service import RolesService
from mac.rollout_service import RolloutService
from mac.secrets_service import SecretsService
from mac.store import SQLiteStore
from mac.task_lifecycle import DispatchService, TaskLedgerService
from mac.workflow_runtime import WorkflowRuntime
from mac.workflow_service import WorkflowService


def _state_value(state: Any) -> str:
    return state.value if hasattr(state, "value") else str(state)


def _compact_beads_ledger_text(value: Any, *, limit: int = 180) -> str:
    text = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    text = re.sub(r"Bearer\s+[-A-Za-z0-9._~+/=]+", "Bearer <redacted>", text)
    text = re.sub(
        r"(?i)(token|api[_-]?key|password|secret)=([^&\s]+)",
        r"\1=<redacted>",
        text,
    )
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


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


def _beads_cli() -> str:
    configured = os.environ.get("MAC_BEADS_CLI", "").strip()
    if configured:
        return str(Path(configured).expanduser())
    found = shutil.which("bd")
    if found:
        return found
    for candidate in (
        Path.home() / ".mac" / "bin" / "bd",
        Path.home() / ".local" / "bin" / "bd",
        Path("/usr/local/bin/bd"),
        Path("/opt/homebrew/bin/bd"),
    ):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return "bd"


def _run_beads_command(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(*args, **kwargs)


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
        raise ValidationError("beads repository path does not exist: %s" % repo_path)
    return expanded if expanded.is_dir() else expanded.parent


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
    return {
        "schema": schema,
        "project": project,
        "contract_path": contract_path,
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
        store: Optional[SQLiteStore] = None,
        secret_key: Optional[str] = None,
    ) -> None:
        self.store = store or SQLiteStore()
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
        self.beads_bridge = BeadsBridgeService(_beads_cli, runner=_run_beads_command)
        self._beads_cli_lock = threading.RLock()
        self._beads_heartbeat_poll_lock = threading.Lock()
        self._task_outbox_drain_lock = threading.Lock()
        self.identity = IdentityService(self.store)
        self.observability = ObservabilityService(self.store)
        self.agentbus = AgentBusService(self.store, self.observability)
        self.provisioning = ProvisioningService(self.store, self.observability)
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
            get_task=self.get_task,
            record_history=self._record_history,
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
            record_history=self._record_history,
            find_verdict_evidence=self._find_review_verdict_evidence,
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
        )
        self._seed_beads_repositories_from_env()

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
        repositories = [repo.to_dict() for repo in self.list_beads_repositories()]
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
            "register_beads_repository",
            "list_beads_repositories",
            "poll_beads_repositories",
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
        hgmac_commands = [str(command) for command in operations.get("hgmac_cli", [])]
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
            "mac-hermes beads-repositories",
            "mac-hermes register-beads-repository",
            "mac-hermes poll-beads-repositories",
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
        expected_hgmac_fragments = (
            "hgmac fleets list",
            "hgmac fleets show",
            "hgmac fleets create",
            "hgmac fleets update",
            "hgmac fleets delete",
            "hgmac tasks list",
            "hgmac tasks show",
            "hgmac tasks create",
            "hgmac tasks update",
            "hgmac tasks delete",
            "hgmac projects list",
            "hgmac projects show",
            "hgmac projects create",
            "hgmac projects update",
            "hgmac projects delete",
            "hgmac agents list",
            "hgmac agents show",
            "hgmac agents create",
            "hgmac agents update",
            "hgmac agents disable",
            "hgmac agents delete",
            "hgmac agents heartbeat",
            "hgmac agents claim-next",
            "hgmac agents identity",
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
            [repository.to_dict() for repository in self.list_beads_repositories()],
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
                "hermes",
                "runtime",
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
            "hgmac_agent_ops_cli",
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
                "hgmac_cli_commands": matching(hgmac_commands, ("hgmac fleets ",)),
                "hgmac_cli_ready": has_all(
                    hgmac_commands,
                    (
                        "hgmac fleets list",
                        "hgmac fleets show",
                        "hgmac fleets create",
                        "hgmac fleets update",
                        "hgmac fleets delete",
                    ),
                ),
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
                        "hgmac_agent_ops_cli",
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
                "hgmac_cli_commands": matching(hgmac_commands, ("hgmac tasks ",)),
                "hgmac_cli_ready": has_all(
                    hgmac_commands,
                    (
                        "hgmac tasks list",
                        "hgmac tasks show",
                        "hgmac tasks create",
                        "hgmac tasks add-child",
                        "hgmac tasks update",
                        "hgmac tasks delete",
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
                        "mac bridge beads register",
                    ),
                ),
                "mac_hermes_cli_commands": matching(
                    mac_hermes_commands,
                    (
                        "mac-hermes projects",
                        "mac-hermes project-detail",
                        "mac-hermes import-project-item",
                        "mac-hermes project-items",
                        "mac-hermes beads-repositories",
                        "mac-hermes register-beads-repository",
                        "mac-hermes poll-beads-repositories",
                    ),
                ),
                "mac_hermes_cli_ready": has_all(
                    mac_hermes_commands,
                    (
                        "mac-hermes projects",
                        "mac-hermes project-detail",
                        "mac-hermes import-project-item",
                        "mac-hermes project-items",
                        "mac-hermes beads-repositories",
                        "mac-hermes register-beads-repository",
                        "mac-hermes poll-beads-repositories",
                    ),
                ),
                "hgmac_cli_commands": matching(hgmac_commands, ("hgmac projects ",)),
                "hgmac_cli_ready": has_all(
                    hgmac_commands,
                    (
                        "hgmac projects list",
                        "hgmac projects show",
                        "hgmac projects create",
                        "hgmac projects update",
                        "hgmac projects delete",
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
                "hgmac_cli_commands": matching(hgmac_commands, expected_hgmac_fragments),
                "hgmac_cli_ready": has_all(hgmac_commands, expected_hgmac_fragments),
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
                        "hgmac_agent_ops_cli",
                        "hermes_oneshot_executor",
                        "command_audit",
                    }
                ),
                "runtime_ready": runtime_capabilities_ready,
            },
        }
        for object_proof in first_class_objects.values():
            checks = ["api_ready", "dashboard_ready", "runtime_ready"]
            for optional_check in ("mac_cli_ready", "mac_hermes_cli_ready", "hgmac_cli_ready"):
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
            and has_all(mac_hermes_commands, expected_agent_cli_fragments)
            and has_all(hgmac_commands, expected_hgmac_fragments),
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
                    "hgmac_cli_commands": hgmac_commands,
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
                    "beads_repository_count": sum(
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
            "required_views": ["work", "projects", "map", "fleets", "agents", "tasks", "hermes", "runtime", "observability"],
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
            if task.state == TaskState.OPEN.value and not waiting_on:
                item["ready_count"] += 1
                if len(item["frontier_tasks"]) < 10:
                    item["frontier_tasks"].append(compact)
            elif task.state in {TaskState.OPEN.value, TaskState.BLOCKED.value} and waiting_on:
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
                    "name": "register_beads_repository",
                    "method": "POST",
                    "path": "/bridge/beads/repositories",
                },
                {
                    "name": "list_beads_repositories",
                    "method": "GET",
                    "path": "/bridge/beads/repositories",
                },
                {
                    "name": "poll_beads_repositories",
                    "method": "POST",
                    "path": "/bridge/beads/poll",
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
                "mac bridge beads register <name> <path> --project <project>",
                "mac bridge beads repos",
                "mac bridge beads poll --repository <repository>",
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
                "mac-hermes beads-repositories",
                "mac-hermes register-beads-repository <name> <path> --project <project>",
                "mac-hermes poll-beads-repositories --repository <repository>",
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
            "hgmac_cli": [
                "hgmac fleets list",
                "hgmac fleets show {fleet}",
                "hgmac fleets create --name {name}",
                "hgmac fleets update {fleet}",
                "hgmac fleets delete {fleet}",
                "hgmac tasks list",
                "hgmac tasks show {task_id}",
                "hgmac tasks create --title {title}",
                "hgmac tasks add-child {task_id} --title {child}",
                "hgmac tasks update {task_id}",
                "hgmac tasks delete {task_id}",
                "hgmac projects list",
                "hgmac projects show {project}",
                "hgmac projects create --name {name}",
                "hgmac projects update {project}",
                "hgmac projects delete {project}",
                "hgmac agents list",
                "hgmac agents show {agent_id}",
                "hgmac agents create --machine-id {machine_id} --name {name}",
                "hgmac agents update {agent_id} --status {status}",
                "hgmac agents disable {agent_id}",
                "hgmac agents delete {agent_id}",
                "hgmac agents heartbeat {agent_id} --status {status}",
                "hgmac agents claim-next {agent_id} --dry-run",
                "hgmac agents identity {agent_id}",
                "hgmac agents role assign {agent_id} {role}",
                "hgmac agents role unassign {agent_id}",
                "hgmac agents mood show {agent_id}",
                "hgmac agents nap next {agent_id}",
                "hgmac agents command-audit list --agent-id {agent_id}",
            ],
            "dashboard": {
                "schema": "mac.hermes.dashboard_operation_contract.v1",
                "entrypoint": "/ui",
                "views": ["work", "projects", "map", "fleets", "agents", "tasks", "hermes", "runtime", "observability"],
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

    def seed_default_roles(self, *args: Any, **kwargs: Any) -> List[AgentRole]:
        return self.roles.seed_defaults(*args, **kwargs)

    # Provisioning hook: emitted when the swarm needs an agent it doesn't
    # have. Today the provisioner is unimplemented; rows + observability
    # are the signal an external poller acts on.

    def request_agent_provisioning(
        self, *args: Any, **kwargs: Any
    ) -> AgentProvisioningRequest:
        return self.provisioning.request_agent(*args, **kwargs)

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

    def seed_default_workflows(self, *args: Any, **kwargs: Any) -> List[Workflow]:
        return self.workflows.seed_defaults(*args, **kwargs)

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
    ) -> Task:
        title = title.strip()
        if not title:
            raise ValidationError("task title is required")
        dep_ids = coerce_list(dependencies)
        for dep_id in dep_ids:
            self.get_task(dep_id)
        now = utcnow()
        task_id = new_id("task")
        state = TaskState.BLOCKED.value if dep_ids else TaskState.OPEN.value
        normalized_metadata = self._normalize_task_execution_contract(
            ensure_json_object(metadata),
            project,
            coerce_list(required_capabilities),
        )
        self.store.execute(
            """
            INSERT INTO tasks (
                id, title, description, project, priority, state,
                required_capabilities, dependencies, metadata,
                owner_agent_id, lease_id, leased_until, attempt_count,
                max_attempts, started_at, completed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?, NULL, NULL, ?, ?)
            """,
            (
                task_id,
                title,
                description,
                project,
                int(priority),
                state,
                json_dumps(coerce_list(required_capabilities)),
                json_dumps(dep_ids),
                json_dumps(normalized_metadata),
                int(max_attempts),
                now,
                now,
            ),
        )
        self._record_history(
            task_id,
            "task.created",
            actor,
            None,
            state,
            {
                "title": title,
                "required_capabilities": coerce_list(required_capabilities),
                "dependencies": dep_ids,
                "execution_contract_type": (
                    normalized_metadata.get("execution_contract", {}).get("type")
                    if isinstance(normalized_metadata.get("execution_contract"), dict)
                    else None
                ),
            },
        )
        if (
            isinstance(normalized_metadata.get("execution_contract"), dict)
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
                    "required_capabilities": coerce_list(required_capabilities),
                    "reason": normalized_metadata["execution_contract"].get("reason"),
                },
            )
        return self.get_task(task_id)

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
            return normalized
        repository_contract = origin_dict.get("repository_contract")
        if isinstance(repository_contract, dict) and repository_contract.get("schema"):
            normalized["execution_contract"] = {
                "schema": "mac.task_execution_contract.v1",
                "type": "repository",
                "quality": "strong",
                "source": "task_origin",
                "repository_contract": repository_contract,
            }
            return normalized
        repo = self._beads_repository_for_project(project)
        if repo is not None:
            contract = repo.metadata.get("repository_contract")
            if not isinstance(contract, dict) or not contract.get("schema"):
                contract = self._repository_contract_for_beads_repo(repo)
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
            acc_metadata.setdefault("repo_beads_workflow", True)
            acc_metadata.setdefault("workflow_role", "work")
            acc_metadata.setdefault("repository_contract_schema", contract["schema"])
            acc_metadata.setdefault("repository_contract_project", contract["project"])
            normalized["acc_metadata"] = acc_metadata
            normalized["execution_contract"] = {
                "schema": "mac.task_execution_contract.v1",
                "type": "repository",
                "quality": "strong",
                "source": "registered_project",
                "repository_id": repo.id,
                "repository_path": repo.path,
                "repository_contract": contract,
            }
            return normalized
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

    def _beads_repository_for_project(self, project: Optional[str]) -> Optional[BeadsRepository]:
        if not project:
            return None
        row = self.store.query_one(
            """
            SELECT * FROM beads_repositories
            WHERE project = ? AND enabled = ?
            ORDER BY name, id
            LIMIT 1
            """,
            (project, 1),
        )
        return self._beads_repository_from_row(row) if row is not None else None

    def get_task(self, task_id: str) -> Task:
        row = self.store.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise NotFoundError("task not found: %s" % task_id)
        return self._task_from_row(row)

    def list_tasks(self, state: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Task]:
        if state:
            rows = self.store.query_all(
                "SELECT * FROM tasks WHERE state = ? ORDER BY priority DESC, created_at",
                (_state_value(state),),
            )
        else:
            rows = self.store.query_all("SELECT * FROM tasks ORDER BY priority DESC, created_at")
        tasks = [self._task_from_row(row) for row in rows]
        if tenant_id is not None:
            tasks = [task for task in tasks if self._task_tenant_id(task) == tenant_id]
        return tasks

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
        new_metadata = task.metadata
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
        if required_capabilities is not None:
            new_capabilities = coerce_list(required_capabilities)
            updates.append("required_capabilities = ?")
            params.append(json_dumps(new_capabilities))
            detail["required_capabilities"] = new_capabilities
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
        if metadata is not None:
            new_metadata = self._normalize_task_execution_contract(
                ensure_json_object(metadata),
                new_project,
                new_capabilities,
            )
            updates.append("metadata = ?")
            params.append(json_dumps(new_metadata))
            detail["metadata_changed"] = True
        elif project is not None or required_capabilities is not None:
            new_metadata = self._normalize_task_execution_contract(
                dict(task.metadata),
                new_project,
                new_capabilities,
            )
            updates.append("metadata = ?")
            params.append(json_dumps(new_metadata))
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
        specs = [ensure_json_object(spec) for spec in children]
        if not specs:
            raise ValidationError("at least one child task is required")

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
            self.task_ledger.enqueue_outbox(
                conn,
                task_id=parent.id,
                event_type="beads.ledger",
                actor=actor,
                from_state=parent.state,
                to_state=TaskState.BLOCKED.value,
                detail=detail,
                created_at=now,
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
    def _beads_repository_project_key(repository: JsonDict) -> str:
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
            [repository.to_dict() for repository in self.list_beads_repositories()],
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
    ) -> ProjectRecord:
        project_name = str(name or "").strip()
        if not project_name:
            raise ValidationError("project name is required")
        existing = self.store.query_one(
            "SELECT * FROM projects WHERE name = ?",
            (project_name,),
        )
        project_metadata = ensure_json_object(metadata)
        if actor:
            project_metadata.setdefault("created_by", actor)
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
        beads_repositories = [
            repository.to_dict()
            for repository in self.list_beads_repositories()
            if self._beads_repository_project_key(repository.to_dict()) == project_key
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
            "beads_repositories": beads_repositories,
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
                conn.execute("UPDATE beads_repositories SET project = ?, updated_at = ? WHERE project = ?", (new_name, now, project.name))
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

    def delete_project(self, name_or_id: str, *, force: bool = False, actor: str = "human") -> None:
        project = self.get_project_record(name_or_id)
        tasks = [task for task in self.list_tasks() if task.project == project.name]
        repo_rows = self.store.query_all(
            "SELECT id FROM beads_repositories WHERE project = ?",
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
                conn.execute("UPDATE beads_repositories SET enabled = 0, updated_at = ? WHERE project = ?", (now, project.name))
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
                    "beads_repository_count": len(repo_rows),
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

    def task_detail(self, task_id: str) -> JsonDict:
        task = self.get_task(task_id)
        return {
            "task": task.to_dict(),
            "history": [event.to_dict() for event in self.task_history(task_id)],
            "evidence": [item.to_dict() for item in self.list_evidence(task_id)],
            "reviews": [item.to_dict() for item in self.list_reviews(task_id)],
            "publications": [item.to_dict() for item in self.list_publications(task_id)],
        }

    def task_summary(self, task_id: str) -> JsonDict:
        detail = self.task_detail(task_id)
        task = detail["task"]
        evidence = detail["evidence"]
        reviews = detail["reviews"]
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

    def task_history(self, task_id: str) -> List[HistoryEvent]:
        self.get_task(task_id)
        rows = self.store.query_all(
            "SELECT * FROM task_history WHERE task_id = ? ORDER BY created_at, id",
            (task_id,),
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

    def _resolve_integration_findings_for_source(
        self,
        source_kind: str,
        source_id: str,
        finding_type: str,
        *,
        active_fingerprints: Optional[Iterable[str]] = None,
        resolution: str = "no longer observed",
    ) -> None:
        active = {str(item) for item in (active_fingerprints or [])}
        for finding in self.list_integration_findings(
            source_kind=source_kind,
            source_id=source_id,
            finding_type=finding_type,
            status="open",
            limit=1000,
        ):
            if finding.fingerprint not in active:
                self.resolve_integration_finding(finding.id, resolution=resolution)

    # Operator notifications ------------------------------------------

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
        self.get_notification(notification_id)
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

    def list_dead_letters(self, tenant_id: Optional[str] = None) -> List[Task]:
        rows = self.store.query_all(
            """
            SELECT * FROM tasks
            WHERE state = ? AND attempt_count >= max_attempts
            ORDER BY updated_at, id
            """,
            (TaskState.FAILED.value,),
        )
        tasks = [self._task_from_row(row) for row in rows]
        if tenant_id is not None:
            tasks = [task for task in tasks if self._task_tenant_id(task) == tenant_id]
        return tasks

    def transition_task(
        self,
        task_id: str,
        target_state: str,
        actor: str,
        detail: Optional[Dict[str, Any]] = None,
        *,
        drain_outbox: bool = True,
    ) -> Task:
        target = _state_value(target_state)
        task = self.get_task(task_id)
        if task.state == target:
            return task
        validate_transition(task.state, target)
        if target == TaskState.NEEDS_REVIEW.value:
            self._require_review_ready(task)
        if target == TaskState.COMPLETED.value and not self.reviews.completion_authorized(task_id):
            raise ValidationError("task completion requires approved review and evidence")
        now = utcnow()
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
        with self.store.transaction() as conn:
            if release_lease_id:
                conn.execute(
                    "UPDATE leases SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                    (LeaseStatus.RELEASED.value, now, release_lease_id, LeaseStatus.ACTIVE.value),
                )
            if is_requeue_from_terminal:
                conn.execute(
                    """
                    UPDATE tasks
                    SET state = ?, owner_agent_id = ?, lease_id = ?, leased_until = ?,
                        started_at = NULL, completed_at = NULL,
                        attempt_count = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (target, owner_agent_id, lease_id, leased_until, now, task_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE tasks
                    SET state = ?, owner_agent_id = ?, lease_id = ?, leased_until = ?,
                        started_at = ?, completed_at = ?, updated_at = ?
                    WHERE id = ?
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
                    ),
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
            if target in TERMINAL_TASK_STATES:
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
            if target in {
                TaskState.RUNNING.value,
                TaskState.NEEDS_REVIEW.value,
                TaskState.REVIEWING.value,
                TaskState.COMPLETED.value,
                TaskState.FAILED.value,
                TaskState.CANCELLED.value,
                TaskState.OPEN.value,
            }:
                self.task_ledger.enqueue_outbox(
                    conn,
                    task_id=task_id,
                    event_type="beads.ledger",
                    actor=actor,
                    from_state=task.state,
                    to_state=target,
                    detail=detail or {},
                    created_at=now,
                )
            if target in {TaskState.FAILED.value, TaskState.CANCELLED.value}:
                self.task_ledger.enqueue_outbox(
                    conn,
                    task_id=task_id,
                    event_type="beads.reopen",
                    actor=actor,
                    from_state=task.state,
                    to_state=target,
                    detail=detail or {},
                    created_at=now,
                )
        if drain_outbox:
            self.drain_task_transition_outbox(task_id=task_id, limit=20)
        transitioned = self.get_task(task_id)
        return transitioned

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
        if task.state == TaskState.BLOCKED.value and self._dependencies_satisfied(task):
            task = self.transition_task(task_id, TaskState.OPEN.value, "dispatcher", {"reason": "dependencies satisfied"})
        if task.state != TaskState.OPEN.value:
            raise TransitionError("only open tasks can be claimed")
        if not self._agent_available_for(agent, task):
            raise ValidationError("agent %s cannot claim task %s" % (agent_id, task_id))
        if task.attempt_count >= task.max_attempts:
            self.transition_task(task_id, TaskState.FAILED.value, "dispatcher", {"reason": "max attempts"})
            raise TransitionError("task %s exhausted max_attempts" % task_id)
        now = utcnow()
        expires_at = (parse_time(now) + timedelta(seconds=int(lease_seconds))).isoformat(timespec="microseconds")
        lease_id = new_id("lease")
        with self.store.transaction() as conn:
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
            self.task_ledger.enqueue_outbox(
                conn,
                task_id=task_id,
                event_type="beads.claim",
                actor=agent_id,
                from_state=task.state,
                to_state=TaskState.CLAIMED.value,
                detail=detail,
                created_at=now,
            )
        claimed_task = self.get_task(task_id)
        if sync_beads:
            self.drain_task_transition_outbox(task_id=task_id, limit=20)
        return claimed_task, self.get_lease(lease_id)

    def sync_claim_side_effects(
        self,
        task_id: str,
        agent_id: str,
        lease_id: str,
        expires_at: str,
    ) -> None:
        """Best-effort external writeback for a completed claim transaction.

        The durable mac lease is the authoritative coordination state. Beads
        claim/comment writeback is human-facing mirror state and must not sit
        on the hot worker claim response path; a slow Dolt push previously let
        workers time out after the lease was created, then mark themselves
        offline and cause the hub to fail the task before execution began.
        """
        try:
            claimed_task = self.get_task(task_id)
        except NotFoundError:
            return
        self._sync_beads_claim(claimed_task, agent_id)
        acc_metadata = claimed_task.metadata.get("acc_metadata") if isinstance(claimed_task.metadata, dict) else {}
        if not (
            isinstance(acc_metadata, dict)
            and acc_metadata.get("beads_sync_claim_on_claim") is False
        ):
            self._append_beads_ledger_comment(
                claimed_task,
                agent_id,
                "claimed",
                "claimed by %s" % agent_id,
                fields={
                    "lease": lease_id,
                    "attempt": claimed_task.attempt_count,
                    "leased_until": expires_at,
                },
            )

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
            return self.drain_task_transition_outbox(task_id=task_id, limit=limit)
        except Exception as exc:  # noqa: BLE001 - side effects must not break API responses.
            try:
                self.record_log(
                    "task.transition_outbox.drain_failed",
                    layer="control_plane",
                    source="task-ledger",
                    level="warning",
                    subject_type="task" if task_id else None,
                    subject_id=task_id,
                    detail={"error": str(exc), "limit": limit},
                )
            except Exception:
                pass
            return {"processed": [], "count": 0, "status": "failed", "error": str(exc)}
        finally:
            self._task_outbox_drain_lock.release()

    def _process_task_transition_outbox_item(self, item: TaskTransitionOutbox) -> None:
        if item.event_type == "task.lifecycle":
            return
        task = self.get_task(item.task_id)
        if item.event_type == "workflow.advance":
            # Workflow-runtime hook. The link is the `tasks.workflow_run_id`
            # column (never caller metadata), so forged task metadata cannot
            # push a free-floating task into the workflow state machine.
            if item.to_state in TERMINAL_TASK_STATES:
                self.workflow_runtime.on_task_completed(item.task_id, item.to_state or "")
            return
        if item.event_type == "beads.ledger":
            self._sync_beads_transition_ledger(
                task,
                item.actor,
                item.from_state or "",
                item.to_state or "",
                item.detail,
            )
            return
        if item.event_type == "beads.reopen":
            self._sync_beads_reopen(task, item.actor, item.to_state or "", item.detail)
            return
        if item.event_type == "beads.claim":
            self.sync_claim_side_effects(
                item.task_id,
                item.actor,
                str(item.detail.get("lease_id") or ""),
                str(item.detail.get("expires_at") or ""),
            )
            return
        raise ValidationError("unsupported task transition outbox event: %s" % item.event_type)

    def start_task(self, task_id: str, agent_id: str, *, drain_outbox: bool = True) -> Task:
        task = self.get_task(task_id)
        if task.owner_agent_id != agent_id:
            raise AuthorizationError("agent does not own task lease")
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
        if task.owner_agent_id != agent_id:
            raise AuthorizationError("agent does not own task lease")
        self._require_review_ready(task)
        reviewed = self.transition_task(
            task_id,
            TaskState.NEEDS_REVIEW.value,
            agent_id,
            {},
            drain_outbox=drain_outbox,
        )
        return reviewed

    def _require_review_ready(self, task: Task) -> None:
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
        sync_beads: bool = True,
    ) -> Evidence:
        self.get_task(task_id)
        if not kind or not uri or not summary:
            raise ValidationError("evidence requires kind, uri, and summary")
        if kind not in EVIDENCE_KINDS:
            raise ValidationError("unsupported evidence kind: %s" % kind)
        if kind == "publication" and not checksum:
            raise ValidationError("publication evidence requires a checksum")
        now = utcnow()
        evidence_id = new_id("ev")
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
                    json_dumps(ensure_json_object(metadata)),
                    created_by,
                    now,
                ),
            )
            self._record_history(
                task_id,
                "task.evidence_added",
                created_by,
                None,
                None,
                {"evidence_id": evidence_id, "kind": kind, "uri": uri},
                conn=conn,
            )
        evidence = self.get_evidence(evidence_id)
        if sync_beads:
            self.sync_evidence_side_effects(evidence_id)
        return evidence

    def sync_evidence_side_effects(self, evidence_id: str) -> None:
        try:
            evidence = self.get_evidence(evidence_id)
            self._append_beads_ledger_comment(
                self.get_task(evidence.task_id),
                evidence.created_by,
                "evidence_added",
                "%s evidence recorded" % evidence.kind,
                fields={
                    "evidence": evidence.id,
                    "kind": evidence.kind,
                    "summary": evidence.summary,
                },
            )
        except Exception as exc:  # noqa: BLE001 - evidence is already durable.
            try:
                evidence = self.get_evidence(evidence_id)
                self.record_log(
                    "task.evidence_side_effects_failed",
                    layer="control_plane",
                    source=evidence.created_by,
                    level="warning",
                    subject_type="task",
                    subject_id=evidence.task_id,
                    detail={"evidence_id": evidence_id, "error": str(exc)},
                )
            except Exception:
                pass

    def get_evidence(self, evidence_id: str) -> Evidence:
        row = self.store.query_one("SELECT * FROM evidence WHERE id = ?", (evidence_id,))
        if row is None:
            raise NotFoundError("evidence not found: %s" % evidence_id)
        return self._evidence_from_row(row)

    def list_evidence(self, task_id: str) -> List[Evidence]:
        rows = self.store.query_all(
            "SELECT * FROM evidence WHERE task_id = ? ORDER BY created_at, id",
            (task_id,),
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
        self._maybe_poll_beads_bridge_on_heartbeat(heartbeat_agent)
        self._maybe_advance_reviews_on_heartbeat(heartbeat_agent)
        return self.get_lease(lease_id)

    def get_lease(self, lease_id: str) -> Lease:
        row = self.store.query_one("SELECT * FROM leases WHERE id = ?", (lease_id,))
        if row is None:
            raise NotFoundError("lease not found: %s" % lease_id)
        return self._lease_from_row(row)

    def expire_leases(
        self, now: Optional[str] = None, *, grace_seconds: Optional[int] = None
    ) -> List[Task]:
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
        rows = self.store.query_all(
            "SELECT * FROM leases WHERE status = ? AND expires_at <= ? ORDER BY expires_at",
            (LeaseStatus.ACTIVE.value, cutoff),
        )
        recovered: List[Task] = []
        for row in rows:
            lease = self._lease_from_row(row)
            task = self.get_task(lease.task_id)
            next_state = TaskState.FAILED.value if task.attempt_count >= task.max_attempts else TaskState.OPEN.value
            timestamp = utcnow()
            with self.store.transaction() as conn:
                # mac-s0ta: guard the lease UPDATE on status='active' so we
                # don't expire a lease that another path (release/renew)
                # already moved. If we lost the race, skip this row entirely
                # — the task may have been reclaimed by a new owner.
                cur = conn.execute(
                    "UPDATE leases SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                    (LeaseStatus.EXPIRED.value, timestamp, lease.id, LeaseStatus.ACTIVE.value),
                )
                if cur.rowcount == 0:
                    continue
                cur = conn.execute(
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
                        task.id,
                        lease.id,
                    ),
                )
                if cur.rowcount == 0:
                    # Task no longer points at this lease — a new owner has
                    # taken over. Leave their state alone.
                    continue
                conn.execute(
                    """
                    UPDATE agents
                    SET status = ?, current_task_id = NULL, updated_at = ?
                    WHERE id = ? AND current_task_id = ?
                    """,
                    (AgentStatus.IDLE.value, timestamp, lease.agent_id, task.id),
                )
                detail = {"lease_id": lease.id, "agent_id": lease.agent_id}
                self._record_history(
                    task.id,
                    "task.lease_expired",
                    "dispatcher",
                    task.state,
                    next_state,
                    detail,
                    conn=conn,
                )
                self.task_ledger.enqueue_outbox(
                    conn,
                    task_id=task.id,
                    event_type="beads.ledger",
                    actor="dispatcher",
                    from_state=task.state,
                    to_state=next_state,
                    detail=detail,
                    created_at=timestamp,
                )
            recovered.append(self.get_task(task.id))
            self.drain_task_transition_outbox(task_id=task.id, limit=20)
        return recovered

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
            self.task_ledger.enqueue_outbox(
                conn,
                task_id=task.id,
                event_type="beads.ledger",
                actor=agent_id,
                from_state=task.state,
                to_state=TaskState.OPEN.value,
                detail=detail,
                created_at=now,
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

    def _validated_fleet_agent_ids(self, agent_ids: Iterable[str]) -> List[str]:
        normalized = coerce_list(str(agent_id).strip() for agent_id in (agent_ids or []))
        for agent_id in normalized:
            self.get_agent(agent_id)
        return normalized

    def _replace_fleet_members(self, conn: Any, fleet_id: str, agent_ids: List[str], now: str) -> None:
        conn.execute("DELETE FROM fleet_agents WHERE fleet_id = ?", (fleet_id,))
        conn.executemany(
            "INSERT INTO fleet_agents (fleet_id, agent_id, created_at) VALUES (?, ?, ?)",
            [(fleet_id, agent_id, now) for agent_id in agent_ids],
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
        resources_json = self._resolved_json_column("agents", "resources", aid, resources)
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
                    HealthStatus.HEALTHY.value,
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
                    "health_status": HealthStatus.HEALTHY.value,
                    "hermes_instance_id": hermes_instance_id,
                },
                now,
            )
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
        verify.
        """
        self.get_agent(agent_id)
        key = _generate_attestation_key()
        now = utcnow()
        self.store.execute(
            """
            UPDATE agents
            SET attestation_key_ciphertext = ?, updated_at = ?
            WHERE id = ?
            """,
            (self.secrets._encrypt(key), now, agent_id),
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
        actor: Optional[str] = None,
    ) -> Agent:
        agent_before = self.get_agent(agent_id)
        now = utcnow()
        updates = ["last_seen_at = ?", "updated_at = ?"]
        params: List[Any] = [now, now]
        status_value: Optional[str] = None
        health_value: Optional[str] = None
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
            updates.append("health_status = ?")
            params.append(health_value)
            if health_value != agent_before.health_status:
                changed_fields.append("health_status")
        if resources is not None:
            resource_value = ensure_json_object(resources)
            updates.append("resources = ?")
            params.append(json_dumps(resource_value))
            if resource_value != agent_before.resources:
                changed_fields.append("resources")
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
                updates.append("running_digest = ?")
                params.append(digest)
                next_running_digest = digest
                if digest != agent_before.running_digest:
                    changed_fields.append("running_digest")
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
        if status_value in {AgentStatus.IDLE.value, AgentStatus.OFFLINE.value}:
            updates.append("current_task_id = NULL")
        params.append(agent_id)
        with self.store.transaction() as conn:
            conn.execute("UPDATE agents SET %s WHERE id = ?" % ", ".join(updates), tuple(params))
            if status is not None or health_status is not None or resources is not None or running_digest is not None:
                self._record_agent_lifecycle_event(
                    conn,
                    agent_id,
                    "agent.heartbeat_updated",
                    actor or agent_id,
                    {
                        "agent_id": agent_id,
                        "agent_name": agent_before.name,
                        "changed_fields": sorted(set(changed_fields)),
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
        self._maybe_poll_beads_bridge_on_heartbeat(agent_before)
        self._maybe_advance_reviews_on_heartbeat(agent_before)
        return agent

    def _maybe_poll_beads_bridge_on_heartbeat(self, agent: Agent) -> None:
        if not _truthy_env("MAC_BEADS_BRIDGE_ON_HEARTBEAT"):
            return
        hub_agent = os.environ.get("MAC_BEADS_BRIDGE_HUB_AGENT", "").strip()
        if not hub_agent:
            return
        if agent.name != hub_agent and agent.id != hub_agent:
            return
        if _truthy_env("MAC_BEADS_BRIDGE_ON_HEARTBEAT_ASYNC", "1"):
            if not self._beads_heartbeat_poll_lock.acquire(blocking=False):
                return
            thread = threading.Thread(
                target=self._poll_beads_bridge_from_heartbeat,
                args=(agent.id,),
                name="mac-beads-heartbeat-poll",
                daemon=True,
            )
            thread.start()
            return
        try:
            self.poll_beads_repositories(actor=agent.id)
        except Exception as exc:  # noqa: BLE001 - heartbeat liveness must survive bridge failures.
            try:
                self.record_log(
                    "bridge.beads.heartbeat_poll_failed",
                    layer="control_plane",
                    source=agent.id,
                    level="warning",
                    detail={"error": str(exc)},
                )
            except Exception:
                pass

    def _poll_beads_bridge_from_heartbeat(self, actor: str) -> None:
        try:
            self.poll_beads_repositories(actor=actor)
        except Exception as exc:  # noqa: BLE001 - heartbeat liveness must survive bridge failures.
            try:
                self.record_log(
                    "bridge.beads.heartbeat_poll_failed",
                    layer="control_plane",
                    source=actor,
                    level="warning",
                    detail={"error": str(exc)},
                )
            except Exception:
                pass
        finally:
            self._beads_heartbeat_poll_lock.release()

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
            result = self.advance_default_review_workflows(
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

    def complete_nap(self, *args: Any, **kwargs: Any) -> NapRun:
        return self.agent_state.complete_nap(*args, **kwargs)

    def fail_nap(self, *args: Any, **kwargs: Any) -> NapRun:
        return self.agent_state.fail_nap(*args, **kwargs)

    def get_nap_run(self, run_id: str) -> NapRun:
        return self.agent_state.get_nap_run(run_id)

    def list_nap_runs(self, *args: Any, **kwargs: Any) -> List[NapRun]:
        return self.agent_state.list_nap_runs(*args, **kwargs)

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
    ) -> Optional[JsonDict]:
        self.expire_leases()
        self._unblock_ready_tasks()
        skipped = set(skip_tenants or [])
        tasks = [
            task
            for task in self._dispatch_ordered_tasks()
            if (self._task_tenant_id(task) or "") not in skipped
        ]
        agents = self._available_agents()
        unmatched: List[Task] = []
        for task in tasks:
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
                return {"task": claimed.to_dict(), "agent": agent.to_dict(), "lease": lease.to_dict()}
            if not matched:
                unmatched.append(task)
        # No agent could claim any pending task. Emit a provisioning
        # signal so a future provisioner (k8s operator, nomad job, local
        # spawner) can spin up the kind of agent that's missing. Today
        # the row + observability log are the signal; no auto-spawn.
        for task in unmatched:
            self._emit_dispatch_provisioning_signal(task)
        return None

    def _emit_dispatch_provisioning_signal(self, task: Task) -> None:
        required_role = None
        hardware: JsonDict = {}
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
        sync_beads: bool = True,
    ) -> Optional[JsonDict]:
        return self.dispatch.claim_next_for_agent(
            agent_id,
            lease_seconds=lease_seconds,
            allowed_projects=allowed_projects,
            required_metadata=required_metadata,
            require_canary=require_canary,
            dry_run=dry_run,
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
        self.expire_leases()
        self._unblock_ready_tasks()
        agent = self.get_agent(agent_id)
        policy = self._worker_claim_policy(
            allowed_projects=allowed_projects,
            required_metadata=required_metadata,
            require_canary=require_canary,
            dry_run=dry_run,
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

    def tick(
        self,
        lease_seconds: int = 900,
        limit: int = 100,
        stale_after_seconds: Optional[int] = None,
    ) -> JsonDict:
        stale_agents = []
        if stale_after_seconds is not None:
            stale_agents = [
                agent.to_dict()
                for agent in self.mark_stale_agents_offline(stale_after_seconds)
            ]
        expired = [task.to_dict() for task in self.expire_leases()]
        self._unblock_ready_tasks()
        review_workflows = self.advance_default_review_workflows(limit=limit)
        assignments = []
        served_tenants = set()
        for _ in range(limit):
            assignment = self.dispatch_once(
                lease_seconds=lease_seconds,
                skip_tenants=served_tenants,
            )
            if assignment is None and served_tenants:
                served_tenants.clear()
                assignment = self.dispatch_once(lease_seconds=lease_seconds)
            if assignment is None:
                break
            assignments.append(assignment)
            task_dict = assignment["task"]
            origin = task_dict.get("metadata", {}).get("origin", {})
            served_tenants.add(str(origin.get("tenant_id") or task_dict.get("metadata", {}).get("tenant_id") or ""))
        return {
            "stale_agents": stale_agents,
            "expired": expired,
            "review_workflows": review_workflows,
            "assignments": assignments,
            "dead_letters": [task.to_dict() for task in self.list_dead_letters()],
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

    def get_agentbus_chunk(self, chunk_id: str) -> AgentBusChunk:
        return self.agentbus.get_chunk(chunk_id)

    def list_agentbus_streams(self, *args: Any, **kwargs: Any) -> List[AgentBusStream]:
        return self.agentbus.list_streams(*args, **kwargs)

    def assert_agentbus_authorized(self, agent_id: str, stream_id: str) -> AgentBusStream:
        return self.agentbus.assert_authorized(agent_id, stream_id)

    def read_agentbus_chunks(self, *args: Any, **kwargs: Any) -> List[AgentBusChunk]:
        return self.agentbus.read_chunks(*args, **kwargs)

    def publish_agentbus_content(self, *args: Any, **kwargs: Any) -> JsonDict:
        return self.agentbus.publish(*args, **kwargs)

    # Reviews + publication: thin facade over ``self.reviews``.

    def request_review(self, *args: Any, **kwargs: Any) -> Review:
        review = self.reviews.request_review(*args, **kwargs)
        actor = kwargs.get("actor")
        if actor is None and len(args) >= 3:
            actor = args[2]
        if actor is None:
            actor = "dispatcher"
        self._append_beads_ledger_comment(
            self.get_task(review.task_id),
            str(actor),
            "review_requested",
            "review requested",
            fields={
                "review": review.id,
                "reviewer": review.reviewer_agent_id,
            },
        )
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
        if sync_beads:
            self.sync_review_claim_side_effects(refreshed.id, review.id, reviewer_agent_id)
        return {
            "schema": "mac.review_claim.v1",
            "status": "claimed",
            "review": review.to_dict(),
            "task": refreshed.to_dict(),
            "claim": claim,
        }

    def sync_review_claim_side_effects(
        self,
        task_id: str,
        review_id: str,
        reviewer_agent_id: str,
    ) -> None:
        try:
            task = self.get_task(task_id)
            claim = ensure_json_object(
                ensure_json_object(task.metadata).get("review_claims")
            ).get(review_id)
            claim_detail = claim if isinstance(claim, dict) else {}
            self._append_beads_ledger_comment(
                task,
                reviewer_agent_id,
                "review_claimed",
                "review claimed for %s" % task.title,
                fields={
                    "review": review_id,
                    "project": claim_detail.get("project"),
                    "worktree": claim_detail.get("repository_worktree"),
                    "head": claim_detail.get("repository_head_sha"),
                    "ref": claim_detail.get("repository_remote_ref"),
                    "work": claim_detail.get("work_summary"),
                },
            )
        except Exception as exc:  # noqa: BLE001 - review claim is already durable.
            try:
                self.record_log(
                    "task.review_claim_side_effects_failed",
                    layer="control_plane",
                    source=reviewer_agent_id,
                    level="warning",
                    subject_type="task",
                    subject_id=task_id,
                    detail={"review_id": review_id, "error": str(exc)},
                )
            except Exception:
                pass

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
        self._append_beads_ledger_comment(
            self.get_task(review.task_id),
            str(reviewer_agent_id),
            "review_completed",
            "review %s" % review.status,
            fields={
                "review": review.id,
                "reviewer": review.reviewer_agent_id,
                "reason": review.reason,
                "evidence": review.evidence_id,
            },
        )
        current = self.get_agent(str(reviewer_agent_id))
        if current.current_task_id == review.task_id:
            self._set_agent_idle(str(reviewer_agent_id))
        return review

    def get_review(self, review_id: str) -> Review:
        return self.reviews.get_review(review_id)

    def list_reviews(self, task_id: str) -> List[Review]:
        return self.reviews.list_reviews(task_id)

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
        self._append_beads_ledger_comment(
            self.get_task(publication.task_id),
            publication.created_by,
            "published",
            "published to %s" % publication.target,
            fields={
                "publication": publication.id,
                "target": publication.target,
                "evidence": publication.evidence_id,
                "status": publication.status,
            },
        )
        self._sync_beads_close(self.get_task(publication.task_id), publication.created_by)
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
        if not repo_path_raw:
            raise ValidationError("git publication requires task origin.repository_path")
        repo_path = Path(repo_path_raw).expanduser()
        if not repo_path.exists():
            raise ValidationError("git publication repository path does not exist: %s" % repo_path)

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

    def advance_default_review_workflows(
        self,
        limit: int = 100,
        actor: str = "default-review-workflow",
        tenant_id: Optional[str] = None,
    ) -> JsonDict:
        """Sweep reviewable tasks. When ``tenant_id`` is set, only tasks
        owned by that tenant are processed — operator tools call this
        scoped, and the autonomous-tick endpoint passes their tenant
        through. Without a filter, sweeps everything (admin / single-
        tenant deployments)."""
        results = []
        for task in self.list_tasks():
            if task.state not in {TaskState.NEEDS_REVIEW.value, TaskState.REVIEWING.value}:
                continue
            if tenant_id is not None and self._task_tenant_id(task) != tenant_id:
                continue
            results.append(self.advance_default_review_workflow(task.id, actor=actor))
            if len(results) >= max(1, int(limit)):
                break
        return {
            "processed": len(results),
            "results": results,
        }

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

        evidence, evidence_assessment = self._default_review_evidence(task)
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
            if verdict_evidence is None:
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
        publication = self.publish_task(
            task_id,
            target,
            review.reviewer_agent_id,
            evidence_id=evidence.id,
        )
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

    def create_runtime_run(self, *args: Any, **kwargs: Any) -> RuntimeRun:
        return self.deploy.create_runtime_run(*args, **kwargs)

    def complete_runtime_run(self, *args: Any, **kwargs: Any) -> RuntimeRun:
        return self.deploy.complete_runtime_run(*args, **kwargs)

    def get_runtime_run(self, run_id: str) -> RuntimeRun:
        return self.deploy.get_runtime_run(run_id)

    def list_runtime_runs(self) -> List[RuntimeRun]:
        return self.deploy.list_runtime_runs()

    # Project bridge

    def _seed_beads_repositories_from_env(self) -> None:
        """Register operator-configured Beads repos once at process startup.

        Format:
            MAC_BEADS_REPOSITORIES="mac=/path/to/repo:repo-beads-mac:mac:python,ops:60;ACC=/path"

        Only the name and path are required. The remaining colon-delimited
        fields are source, project, required capabilities, and poll interval.
        Legacy pipe-delimited values are also accepted for compatibility.
        Bad entries are logged instead of failing service startup.
        """
        raw = os.environ.get("MAC_BEADS_REPOSITORIES", "").strip()
        if not raw and _truthy_env("MAC_BEADS_AUTO_REGISTER_SELF"):
            self_repo = os.environ.get("MAC_SELF_UPDATE_REPO", "").strip()
            if self_repo:
                raw = "mac=%s|repo-beads-mac|repo-beads-mac||60" % self_repo
        if not raw:
            return
        for entry in raw.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            try:
                if "=" not in entry:
                    raise ValidationError("entry must be name=path")
                name, rest = entry.split("=", 1)
                parts = rest.split("|") if "|" in rest else rest.split(":")
                path = parts[0].strip()
                source = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
                project = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
                caps = parts[3].strip() if len(parts) > 3 else ""
                interval = int(parts[4]) if len(parts) > 4 and parts[4].strip() else 60
                self.register_beads_repository(
                    name.strip(),
                    path,
                    source=source,
                    project=project,
                    required_capabilities=[item.strip() for item in caps.split("+") if item.strip()]
                    if "+" in caps
                    else [item.strip() for item in caps.split(",") if item.strip()],
                    poll_interval_seconds=interval,
                    actor="env",
                )
            except Exception as exc:  # noqa: BLE001 - bad env should not kill the API.
                try:
                    self.record_log(
                        "bridge.beads_repository.seed_failed",
                        layer="control_plane",
                        source="env",
                        level="warning",
                        detail={"entry": entry, "error": str(exc)},
                    )
                except Exception:
                    pass

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

    def register_beads_repository(
        self,
        name: str,
        path: str,
        source: Optional[str] = None,
        project: Optional[str] = None,
        required_capabilities: Optional[Iterable[str]] = None,
        enabled: bool = True,
        poll_interval_seconds: int = 60,
        metadata: Optional[Dict[str, Any]] = None,
        actor: str = "beads-bridge",
    ) -> BeadsRepository:
        name = name.strip()
        if not name:
            raise ValidationError("beads repository name is required")
        repo_path_obj = Path(path).expanduser()
        repo_path = str(repo_path_obj)
        repo_source = (source or "repo-beads-%s" % _safe_slug(name)).strip()
        if not repo_source:
            raise ValidationError("beads repository source is required")
        repo_project = (project or repo_source).strip()
        contract = _load_repository_contract(repo_path_obj)
        if contract["project"] != repo_project:
            raise ValidationError(
                "repository runtime contract project %s does not match registered project %s"
                % (contract["project"], repo_project)
            )
        repo_metadata = ensure_json_object(metadata)
        repo_metadata["repository_contract"] = contract
        now = utcnow()
        row = self.store.query_one("SELECT id FROM beads_repositories WHERE name = ?", (name,))
        repo_id = row["id"] if row is not None else new_id("beadsrepo")
        self.store.execute(
            """
            INSERT INTO beads_repositories (
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
            "bridge.beads_repository.registered",
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
            },
        )
        return self.get_beads_repository(repo_id)

    def get_beads_repository(self, repo_id_or_name: str) -> BeadsRepository:
        row = self.store.query_one(
            "SELECT * FROM beads_repositories WHERE id = ? OR name = ?",
            (repo_id_or_name, repo_id_or_name),
        )
        if row is None:
            raise NotFoundError("beads repository not found: %s" % repo_id_or_name)
        return self._beads_repository_from_row(row)

    def list_beads_repositories(self, enabled: Optional[bool] = None) -> List[BeadsRepository]:
        if enabled is None:
            rows = self.store.query_all("SELECT * FROM beads_repositories ORDER BY name, id")
        else:
            rows = self.store.query_all(
                "SELECT * FROM beads_repositories WHERE enabled = ? ORDER BY name, id",
                (1 if enabled else 0,),
            )
        return [self._beads_repository_from_row(row) for row in rows]

    def _repository_contract_for_beads_repo(self, repo: BeadsRepository) -> JsonDict:
        contract = _load_repository_contract(Path(repo.path).expanduser())
        if contract["project"] != repo.project:
            raise ValidationError(
                "repository runtime contract project %s does not match registered project %s"
                % (contract["project"], repo.project)
            )
        return contract

    def poll_beads_repositories(
        self,
        repo_id_or_name: Optional[str] = None,
        *,
        force: bool = False,
        actor: str = "beads-bridge",
    ) -> JsonDict:
        if repo_id_or_name:
            repos = [self.get_beads_repository(repo_id_or_name)]
        else:
            repos = self.list_beads_repositories(enabled=True)
        report: JsonDict = {
            "schema": "mac.beads_bridge.poll.v1",
            "actor": actor,
            "repository_count": len(repos),
            "imported_count": 0,
            "existing_count": 0,
            "reopened_count": 0,
            "retry_exhausted_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "repositories": [],
        }
        if not self._beads_cli_lock.acquire(blocking=False):
            report["busy_count"] = len(repos)
            report["skipped_count"] = len(repos)
            report["repositories"] = [
                {
                    "repository_id": repo.id,
                    "name": repo.name,
                    "status": "busy",
                    "imported_count": 0,
                    "existing_count": 0,
                    "skipped_count": 1,
                }
                for repo in repos
            ]
            return report
        try:
            for repo in repos:
                repo_report = self._poll_beads_repository(repo, force=force, actor=actor)
                report["repositories"].append(repo_report)
                report["imported_count"] += int(repo_report.get("imported_count", 0))
                report["existing_count"] += int(repo_report.get("existing_count", 0))
                report["reopened_count"] += int(repo_report.get("reopened_count", 0))
                report["retry_exhausted_count"] += int(repo_report.get("retry_exhausted_count", 0))
                report["skipped_count"] += int(repo_report.get("skipped_count", 0))
                if repo_report.get("status") in {
                    "error",
                    "source_dirty",
                    "source_refresh_error",
                    "authority_drift",
                    "authority_export_error",
                }:
                    report["error_count"] += 1
        finally:
            self._beads_cli_lock.release()
        if report["imported_count"] or report["reopened_count"] or report["error_count"]:
            self.record_log(
                "bridge.beads.poll",
                layer="control_plane",
                source=actor,
                detail=report,
            )
        return report

    def repair_beads_repository(
        self,
        repo_id_or_name: str,
        *,
        actor: str = "beads-bridge",
        poll_after: bool = True,
    ) -> JsonDict:
        repo = self.get_beads_repository(repo_id_or_name)
        now = utcnow()
        source_state = self._refresh_beads_repository_source(repo, actor)
        poll_path = Path(str(source_state.get("poll_path") or repo.path)).expanduser()
        repair_action = self._beads_repair_action(repo, poll_path, "operator_repair")
        steps: List[JsonDict] = []

        def fail(reason: str, summary: str, status: str = "error") -> JsonDict:
            health = self._beads_repository_health(
                "unhealthy",
                reason,
                {"source_state": source_state, "steps": steps, "repair_action": repair_action},
                summary=summary,
            )
            self._update_beads_repository_poll_state(
                repo.id,
                now,
                last_imported_at=repo.last_imported_at,
                last_error=summary,
                health=health,
            )
            return {
                "schema": "mac.beads_bridge.repair.v1",
                "repository_id": repo.id,
                "name": repo.name,
                "status": status,
                "reason": reason,
                "error": summary,
                "health": health,
                "source_state": source_state,
                "repair_action": repair_action,
                "steps": steps,
            }

        if source_state.get("status") in {"dirty", "error"}:
            return fail(
                "source_refresh_error",
                str(source_state.get("error") or source_state.get("status") or "source refresh failed"),
                status=str(source_state.get("status") or "error"),
            )

        def run_step(name: str, args: List[str], timeout: int = 60) -> Any:
            result = self.beads_bridge.run(args, cwd=poll_path, actor=actor, timeout=timeout)
            steps.append(
                {
                    "name": name,
                    "argv": result.argv,
                    "returncode": result.returncode,
                    "stdout": result.stdout[:1000],
                    "stderr": result.stderr[:1000],
                }
            )
            return result

        def run_git_step(name: str, args: List[str], timeout: int = 60) -> JsonDict:
            argv = ["git", *args]
            completed = subprocess.run(
                argv,
                cwd=str(poll_path),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            step = {
                "name": name,
                "argv": argv,
                "returncode": int(completed.returncode),
                "stdout": (completed.stdout or "")[:1000],
                "stderr": (completed.stderr or "")[:1000],
            }
            steps.append(step)
            return step

        embedded_dolt = poll_path / ".beads" / "embeddeddolt"
        if embedded_dolt.exists() and any(embedded_dolt.iterdir()):
            steps.append(
                {
                    "name": "bootstrap",
                    "argv": [self.beads_bridge.cli_path(), "--actor", actor, "bootstrap", "--yes"],
                    "returncode": 0,
                    "stdout": "skipped: embedded Beads database already exists",
                    "stderr": "",
                    "skipped": True,
                }
            )
        else:
            bootstrap = run_step("bootstrap", ["bootstrap", "--yes"], timeout=120)
            if bootstrap.returncode != 0:
                return fail("bootstrap_failed", bootstrap.output or "bd bootstrap failed")
        dolt_pull = run_step("dolt_pull", ["dolt", "pull"], timeout=120)
        if dolt_pull.returncode != 0:
            return fail("dolt_pull_failed", dolt_pull.output or "bd dolt pull failed")
        ready = run_step("ready", ["ready", "--json"], timeout=30)
        if ready.returncode != 0:
            return fail("canonical_unavailable", ready.output or "bd ready --json failed")
        data = json_loads(ready.stdout.strip(), []) if ready.stdout.strip() else []
        if not isinstance(data, list):
            return fail("canonical_unavailable", "bd ready --json did not return a list")
        export_path = poll_path / ".beads" / "issues.jsonl"
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export = run_step("export", ["export", "-o", str(export_path)], timeout=60)
        if export.returncode != 0:
            return fail("authority_export_error", export.output or "bd export failed")
        try:
            exported_ready = self._ready_beads_issues_from_jsonl(export_path)
        except Exception as exc:  # noqa: BLE001 - rewrite from canonical ready output below.
            exported_ready = []
            steps.append(
                {
                    "name": "verify_export",
                    "argv": [],
                    "returncode": 1,
                    "stdout": "",
                    "stderr": str(exc)[:1000],
                }
            )
        canonical_ids = self._beads_issue_ids(data)
        exported_ids = self._beads_issue_ids(exported_ready)
        if canonical_ids != exported_ids:
            payload = "".join("%s\n" % json_dumps(issue) for issue in data)
            export_path.write_text(payload, encoding="utf-8")
            steps.append(
                {
                    "name": "canonical_ready_export",
                    "argv": [],
                    "returncode": 0,
                    "stdout": "rewrote tracked export from canonical bd ready output",
                    "stderr": "",
                    "canonical_ready_count": len(canonical_ids),
                    "exported_ready_count": len(exported_ids),
                }
            )
        if (poll_path / ".git").exists():
            status = run_git_step(
                "git_status_export",
                ["status", "--porcelain", "--", ".beads/issues.jsonl"],
                timeout=20,
            )
            if status["returncode"] != 0:
                return fail("git_status_failed", status["stderr"] or status["stdout"] or "git status failed")
            if str(status.get("stdout") or "").strip():
                add = run_git_step("git_add_export", ["add", ".beads/issues.jsonl"], timeout=20)
                if add["returncode"] != 0:
                    return fail("git_add_failed", add["stderr"] or add["stdout"] or "git add failed")
                commit = run_git_step(
                    "git_commit_export",
                    [
                        "-c",
                        "user.name=MAC Beads Bridge",
                        "-c",
                        "user.email=mac-beads-bridge@localhost",
                        "commit",
                        "-m",
                        "Repair Beads tracked export",
                    ],
                    timeout=60,
                )
                if commit["returncode"] != 0:
                    return fail("git_commit_failed", commit["stderr"] or commit["stdout"] or "git commit failed")
                branch = str(source_state.get("branch") or "").strip()
                if branch:
                    push_args = ["push", "origin", "HEAD:%s" % branch]
                else:
                    push_args = ["push"]
                push = run_git_step("git_push_export", push_args, timeout=120)
                if push["returncode"] != 0:
                    return fail("git_push_failed", push["stderr"] or push["stdout"] or "git push failed")
            else:
                steps.append(
                    {
                        "name": "git_persist_export",
                        "argv": ["git", "status", "--porcelain", "--", ".beads/issues.jsonl"],
                        "returncode": 0,
                        "stdout": "skipped: tracked export already matches git HEAD",
                        "stderr": "",
                        "skipped": True,
                    }
                )
        poll_report = (
            self.poll_beads_repositories(repo.id, force=True, actor=actor)
            if poll_after
            else None
        )
        if poll_report is not None and int(poll_report.get("error_count", 0)) > 0:
            refreshed = self.get_beads_repository(repo.id)
            health = ensure_json_object(refreshed.metadata.get("health") or {})
            if not health:
                health = self._beads_repository_health(
                    "unhealthy",
                    "post_repair_poll_failed",
                    {"source_state": source_state, "steps": steps, "poll_report": poll_report},
                    summary="post-repair poll still reports Beads repository errors",
                )
            return {
                "schema": "mac.beads_bridge.repair.v1",
                "repository_id": repo.id,
                "name": repo.name,
                "status": "error",
                "reason": "post_repair_poll_failed",
                "error": "post-repair poll still reports Beads repository errors",
                "health": health,
                "source_state": source_state,
                "repair_action": repair_action,
                "steps": steps,
                "poll_report": poll_report,
            }
        health = self._beads_repository_health(
            "healthy",
            "canonical_beads_db_reconciled",
            {"source_state": source_state, "steps": steps, "poll_report": poll_report},
        )
        self._update_beads_repository_poll_state(
            repo.id,
            utcnow(),
            last_imported_at=self.get_beads_repository(repo.id).last_imported_at,
            last_error=None,
            health=health,
        )
        return {
            "schema": "mac.beads_bridge.repair.v1",
            "repository_id": repo.id,
            "name": repo.name,
            "status": "ok",
            "health": health,
            "source_state": source_state,
            "repair_action": repair_action,
            "steps": steps,
            "poll_report": poll_report,
        }

    def _poll_beads_repository(
        self,
        repo: BeadsRepository,
        *,
        force: bool,
        actor: str,
    ) -> JsonDict:
        now = utcnow()
        source_state: Optional[JsonDict] = None
        if not force and repo.last_polled_at:
            elapsed = parse_time(now) - parse_time(repo.last_polled_at)
            if elapsed.total_seconds() < repo.poll_interval_seconds:
                return {
                    "repository_id": repo.id,
                    "name": repo.name,
                    "status": "not_due",
                    "next_poll_in_seconds": repo.poll_interval_seconds - int(elapsed.total_seconds()),
                    "imported_count": 0,
                    "existing_count": 0,
                    "skipped_count": 0,
                }
        try:
            source_state = self._refresh_beads_repository_source(repo, actor)
            if source_state["status"] in {"dirty", "error"}:
                status = (
                    "source_dirty"
                    if source_state["status"] == "dirty"
                    else "source_refresh_error"
                )
                health = self._beads_repository_health(
                    "unhealthy",
                    status,
                    source_state,
                )
                remediation_task = self._ensure_beads_source_remediation_task(
                    repo,
                    source_state,
                    actor,
                    status,
                )
                self._update_beads_repository_poll_state(
                    repo.id,
                    now,
                    last_imported_at=repo.last_imported_at,
                    last_error=source_state.get("error") or source_state["status"],
                    health=health,
                )
                self.record_notification(
                    "bridge.beads.%s" % status,
                    "Beads bridge source stale",
                    "%s was not polled because its checkout is %s"
                    % (repo.name, source_state["status"]),
                    subject_type="environment",
                    subject_id=repo.id,
                    channels=["dashboard", "hermes"],
                    metadata={
                        "repository": self._beads_repository_ref(repo),
                        "source_state": source_state,
                        "remediation_task_id": remediation_task.id if remediation_task else None,
                    },
                )
                return {
                    "repository_id": repo.id,
                    "name": repo.name,
                    "status": status,
                    "health": health,
                    "source_state": source_state,
                    "remediation_task_id": remediation_task.id if remediation_task else None,
                    "imported_count": 0,
                    "existing_count": 0,
                    "skipped_count": 0,
                }
            poll_path = Path(str(source_state.get("poll_path") or repo.path)).expanduser()
            repository_contract = self._repository_contract_for_beads_repo_at_path(repo, poll_path)
            issues = self._ready_beads_issues(
                repo,
                poll_path=poll_path,
                actor=actor,
                source_state=source_state,
            )
            imported = 0
            existing = 0
            reopened = 0
            retry_exhausted = 0
            existing_sync_results: Dict[str, int] = {}
            for issue in issues:
                prior = self.store.query_one(
                    "SELECT id, task_id FROM project_items WHERE source = ? AND external_id = ?",
                    (repo.source, str(issue["id"])),
                )
                item = self._import_bead_issue(
                    repo,
                    issue,
                    actor=actor,
                    repository_contract=repository_contract,
                )
                if prior is None:
                    imported += 1
                    self._append_beads_ledger_comment(
                        self.get_task(item.task_id),
                        actor,
                        "imported",
                        "bead imported as mac task %s" % item.task_id,
                        fields={
                            "source": repo.source,
                            "project": repo.project,
                            "priority": self.get_task(item.task_id).priority,
                        },
                    )
                else:
                    existing += 1
                    result = self._sync_existing_beads_task(
                        self.get_task(prior["task_id"]),
                        actor,
                        issue=issue,
                    )
                    existing_sync_results[result] = existing_sync_results.get(result, 0) + 1
                    if result == "reopened":
                        reopened += 1
                    elif result == "retry_exhausted":
                        retry_exhausted += 1
            health = self._beads_repository_health_from_source_state(source_state)
            report_status = "ok"
            last_error: Optional[str] = None
            if health["status"] != "healthy":
                reason = str(health.get("reason") or "")
                report_status = reason if reason in {"authority_drift", "authority_export_error"} else "error"
                last_error = str(health.get("summary") or health.get("reason") or "beads repository unhealthy")
            self._update_beads_repository_poll_state(
                repo.id,
                now,
                last_imported_at=now if imported or reopened else repo.last_imported_at,
                last_error=last_error,
                health=health,
            )
            return {
                "repository_id": repo.id,
                "name": repo.name,
                "status": report_status,
                "health": health,
                "ready_count": len(issues),
                "imported_count": imported,
                "existing_count": existing,
                "reopened_count": reopened,
                "retry_exhausted_count": retry_exhausted,
                "existing_sync_results": existing_sync_results,
                "skipped_count": 0,
                "repository_contract_schema": repository_contract["schema"],
                "source_state": source_state,
            }
        except Exception as exc:  # noqa: BLE001 - one broken repo must not break heartbeats.
            authority = ensure_json_object((source_state or {}).get("authority") if source_state else {})
            health_reason = (
                "canonical_unavailable"
                if authority.get("status") == "unavailable"
                else "poll_error"
            )
            health = self._beads_repository_health(
                "unhealthy",
                health_reason,
                source_state or {"error": str(exc)},
                summary=str(exc),
            )
            self._update_beads_repository_poll_state(
                repo.id,
                now,
                last_imported_at=repo.last_imported_at,
                last_error=str(exc),
                health=health,
            )
            return {
                "repository_id": repo.id,
                "name": repo.name,
                "status": "error",
                "error": str(exc),
                "health": health,
                "source_state": source_state,
                "imported_count": 0,
                "existing_count": 0,
                "skipped_count": 0,
            }

    def _refresh_beads_repository_source(self, repo: BeadsRepository, actor: str) -> JsonDict:
        repo_path = Path(repo.path).expanduser()
        state: JsonDict = {
            "schema": "mac.beads_bridge.source_state.v1",
            "repository_id": repo.id,
            "repository_name": repo.name,
            "registered_path": str(repo_path),
            "path": str(repo_path),
            "poll_path": str(repo_path),
            "checkout_policy": "direct",
            "auto_pull": _truthy_env("MAC_BEADS_AUTO_PULL", "1"),
            "status": "skipped",
        }
        if not state["auto_pull"]:
            state["status"] = "disabled"
            return state
        if repo_path.is_file():
            state["status"] = "file"
            return state
        if not (repo_path / ".git").exists():
            state["status"] = "not_git"
            return state

        repo_path = self._beads_repository_registered_root(repo_path, state)
        if state.get("status") == "error":
            self._record_beads_source_state(actor, repo, state, "warning")
            return state
        state["registered_path"] = str(repo_path)
        self._restore_beads_tracked_exports(
            repo_path,
            actor,
            repo.id,
            "registered_source_poll",
            subject_type="environment",
        )
        registered_dirty = self._git_output(repo_path, ["status", "--porcelain"])
        if registered_dirty["returncode"] == 0:
            dirty_paths = [
                line.strip()
                for line in registered_dirty.get("stdout", "").splitlines()
                if line.strip()
            ]
            if dirty_paths:
                state["registered_dirty_paths"] = dirty_paths[:50]
                state["registered_dirty_path_count"] = len(dirty_paths)

        bridge = self._ensure_beads_bridge_checkout(repo, repo_path, actor, state)
        if state.get("status") == "error":
            self._record_beads_source_state(actor, repo, state, "warning")
            return state
        if bridge is None:
            self._record_beads_source_state(actor, repo, state, "warning")
            return state
        repo_path = bridge
        state["path"] = str(repo_path)
        state["poll_path"] = str(repo_path)
        state["checkout_policy"] = "dedicated_git_checkout"

        before = self._git_output(repo_path, ["rev-parse", "HEAD"])
        state["head_before"] = before.get("stdout", "")
        branch_name = str(state.get("branch") or "").strip()
        upstream_ref = str(state.get("bridge_upstream_ref") or "").strip()
        dirty = self._git_output(repo_path, ["status", "--porcelain"])
        if dirty["returncode"] != 0:
            state["status"] = "error"
            state["error"] = dirty.get("stderr") or dirty.get("stdout") or "git status failed"
            self._record_beads_source_state(actor, repo, state, "warning")
            return state
        tracked_dirty_paths = [
            line.strip()
            for line in dirty.get("stdout", "").splitlines()
            if line.strip() and not line.startswith("?? ")
        ]
        if tracked_dirty_paths:
            reset = self._git_output(repo_path, ["reset", "--hard", "HEAD"])
            if reset["returncode"] != 0:
                state["status"] = "dirty"
                state["dirty_paths"] = tracked_dirty_paths
                state["error"] = reset.get("stderr") or reset.get("stdout") or "git reset failed"
                self._record_beads_source_state(actor, repo, state, "warning")
                return state
            state["tracked_dirty_reset"] = tracked_dirty_paths[:50]
        fetch = self._git_output(repo_path, ["fetch", "--quiet", "--prune"])
        if fetch["returncode"] != 0:
            state["status"] = "error"
            state["error"] = fetch.get("stderr") or fetch.get("stdout") or "git fetch failed"
            self._record_beads_source_state(actor, repo, state, "warning")
            return state
        if upstream_ref:
            update = self._git_output(
                repo_path,
                ["checkout", "-B", branch_name or "main", upstream_ref],
            )
        else:
            update = self._git_output(repo_path, ["pull", "--ff-only", "--quiet"])
        if update["returncode"] != 0:
            state["status"] = "error"
            state["error"] = update.get("stderr") or update.get("stdout") or "git update failed"
            self._record_beads_source_state(actor, repo, state, "warning")
            return state
        after = self._git_output(repo_path, ["rev-parse", "HEAD"])
        state["head_after"] = after.get("stdout", "")
        state["status"] = (
            "cloned"
            if state.get("bridge_cloned")
            else
            "updated"
            if state.get("head_before") and state.get("head_after") != state.get("head_before")
            else "current"
        )
        self._bootstrap_beads_bridge_checkout(repo, repo_path, actor, state)
        self._record_beads_source_state(actor, repo, state, "info")
        return state

    def _repository_contract_for_beads_repo_at_path(
        self,
        repo: BeadsRepository,
        repo_path: Path,
    ) -> JsonDict:
        contract = _load_repository_contract(repo_path)
        if contract["project"] != repo.project:
            raise ValidationError(
                "repository runtime contract project %s does not match registered project %s"
                % (contract["project"], repo.project)
            )
        return contract

    def _beads_repository_registered_root(self, repo_path: Path, state: JsonDict) -> Path:
        top_level = self._git_output(repo_path, ["rev-parse", "--show-toplevel"])
        if top_level["returncode"] != 0 or not top_level.get("stdout"):
            state["status"] = "error"
            state["error"] = top_level.get("stderr") or top_level.get("stdout") or "git top-level failed"
            return repo_path
        return Path(str(top_level["stdout"])).expanduser()

    def _beads_repository_poll_path(self, repo: BeadsRepository) -> Path:
        repo_path = Path(repo.path).expanduser()
        if repo_path.is_file() or not (repo_path / ".git").exists():
            return repo_path
        return self._beads_bridge_checkout_path(repo)

    def _beads_bridge_checkout_path(self, repo: BeadsRepository) -> Path:
        metadata = repo.metadata if isinstance(repo.metadata, dict) else {}
        explicit = (
            metadata.get("bridge_checkout_path")
            or metadata.get("poll_checkout_path")
            or metadata.get("beads_bridge_checkout_path")
        )
        if explicit:
            return Path(str(explicit)).expanduser()
        root_raw = (
            os.environ.get("MAC_BEADS_BRIDGE_ROOT", "").strip()
            or str(Path(repo.path).expanduser().parent / ".mac-beads-bridge")
        )
        slug = _safe_slug("%s-%s" % (repo.source or repo.name, repo.id[:8]))
        return Path(root_raw).expanduser() / slug

    def _ensure_beads_bridge_checkout(
        self,
        repo: BeadsRepository,
        registered_root: Path,
        actor: str,
        state: JsonDict,
    ) -> Optional[Path]:
        branch = self._git_output(registered_root, ["rev-parse", "--abbrev-ref", "HEAD"])
        branch_name = branch.get("stdout", "").strip() if branch["returncode"] == 0 else ""
        if branch_name == "HEAD":
            branch_name = ""
        upstream = self._git_output(
            registered_root,
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        )
        upstream_name = upstream.get("stdout", "").strip() if upstream["returncode"] == 0 else ""
        remote_name = "origin"
        upstream_branch = branch_name
        if upstream_name and "/" in upstream_name:
            remote_name, upstream_branch = upstream_name.split("/", 1)
        remote = self._git_output(registered_root, ["remote", "get-url", remote_name])
        if remote["returncode"] != 0 or not remote.get("stdout"):
            remote = self._git_output(registered_root, ["remote", "get-url", "origin"])
            remote_name = "origin"
        clone_url = remote.get("stdout", "").strip() if remote["returncode"] == 0 else str(registered_root)
        bridge_path = self._beads_bridge_checkout_path(repo)
        state["branch"] = branch_name
        state["upstream"] = upstream_name
        state["bridge_remote"] = remote_name
        state["bridge_clone_url"] = clone_url
        state["bridge_checkout_path"] = str(bridge_path)
        if upstream_branch:
            state["bridge_upstream_ref"] = "origin/%s" % upstream_branch
        bridge_path.parent.mkdir(parents=True, exist_ok=True)
        if bridge_path.exists() and not (bridge_path / ".git").exists():
            state["status"] = "error"
            state["error"] = "bridge checkout path exists but is not a git worktree: %s" % bridge_path
            return None
        if not bridge_path.exists():
            # mac-raud: even though clone_url comes from a locally-registered
            # repo's `git remote get-url`, treat it as untrusted argv — refuse
            # anything that starts with `-` or contains shell separators that
            # could be confused with a flag, and add `--` to be safe.
            if not clone_url or clone_url.startswith("-"):
                state["status"] = "error"
                state["error"] = "refusing to clone: clone_url looks like a flag (%r)" % clone_url
                return None
            clone = subprocess.run(
                ["git", "clone", "--quiet", "--", clone_url, str(bridge_path)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if clone.returncode != 0:
                state["status"] = "error"
                state["error"] = (clone.stderr or clone.stdout or "git clone failed").strip()
                return None
            state["bridge_cloned"] = True
        current_remote = self._git_output(bridge_path, ["remote", "get-url", "origin"])
        if (
            clone_url
            and current_remote["returncode"] == 0
            and current_remote.get("stdout", "").strip() != clone_url
        ):
            set_url = self._git_output(bridge_path, ["remote", "set-url", "origin", clone_url])
            if set_url["returncode"] != 0:
                state["status"] = "error"
                state["error"] = set_url.get("stderr") or set_url.get("stdout") or "git remote set-url failed"
                return None
        return bridge_path

    def _bootstrap_beads_bridge_checkout(
        self,
        repo: BeadsRepository,
        repo_path: Path,
        actor: str,
        state: JsonDict,
    ) -> None:
        beads_dir = repo_path / ".beads"
        if not beads_dir.exists():
            return
        try:
            beads_dir.chmod(0o700)
        except OSError:
            pass
        role = self._git_output(repo_path, ["config", "beads.role", "maintainer"])
        if role["returncode"] == 0:
            state["beads_role"] = "maintainer"
        else:
            state["beads_role_error"] = role.get("stderr") or role.get("stdout") or "git config failed"
        embedded = beads_dir / "embeddeddolt"
        if embedded.exists():
            try:
                if any(embedded.iterdir()):
                    state["beads_bootstrap"] = "already_exists"
                    self._sync_beads_database(repo_path, actor, repo.id, state)
                    return
            except OSError:
                pass
        try:
            completed = subprocess.run(
                [_beads_cli(), "bootstrap", "--yes"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - JSONL fallback can still work.
            state["beads_bootstrap"] = "error"
            state["beads_bootstrap_error"] = str(exc)
            return
        if completed.returncode == 0:
            state["beads_bootstrap"] = "ok"
            self._restore_beads_tracked_exports(repo_path, actor, repo.id, "bootstrap")
            self._sync_beads_database(repo_path, actor, repo.id, state)
            return
        output = (completed.stderr or completed.stdout or "").strip()
        if "database exists" in output.lower():
            state["beads_bootstrap"] = "already_exists"
            self._sync_beads_database(repo_path, actor, repo.id, state)
            return
        state["beads_bootstrap"] = "failed"
        state["beads_bootstrap_error"] = output[:1000]

    def _sync_beads_database(
        self,
        repo_path: Path,
        actor: str,
        subject_id: str,
        state: JsonDict,
    ) -> None:
        completed = self._run_beads_dolt_pull(repo_path)
        self._record_beads_dolt_pull_result(
            completed,
            repo_path,
            actor,
            subject_id,
            state,
        )
        if completed is not None and completed.returncode == 0:
            return
        if not _truthy_env("MAC_BEADS_REBUILD_ON_DOLT_PULL_FAILURE", "1"):
            return
        if self._rebuild_beads_database(repo_path, actor, subject_id, state):
            retry = self._run_beads_dolt_pull(repo_path)
            self._record_beads_dolt_pull_result(
                retry,
                repo_path,
                actor,
                subject_id,
                state,
                prefix="beads_dolt_pull_retry",
            )

    def _run_beads_dolt_pull(
        self,
        repo_path: Path,
    ) -> Optional[subprocess.CompletedProcess[str]]:
        try:
            return subprocess.run(
                [_beads_cli(), "dolt", "pull"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - JSONL/DB read can still report drift.
            return subprocess.CompletedProcess(
                [_beads_cli(), "dolt", "pull"],
                125,
                stdout="",
                stderr=str(exc),
            )

    def _record_beads_dolt_pull_result(
        self,
        completed: Optional[subprocess.CompletedProcess[str]],
        repo_path: Path,
        actor: str,
        subject_id: str,
        state: JsonDict,
        *,
        prefix: str = "beads_dolt_pull",
    ) -> None:
        if completed is None:
            state[prefix] = "error"
            state["%s_error" % prefix] = "bd dolt pull did not run"
            return
        output = (completed.stderr or completed.stdout or "").strip()
        if completed.returncode == 0:
            state[prefix] = "ok"
            if output:
                state["%s_output" % prefix] = output[:1000]
            return
        state[prefix] = "failed"
        state["%s_returncode" % prefix] = int(completed.returncode)
        state["%s_error" % prefix] = output[:2000]
        self.record_log(
            "bridge.beads.dolt_pull_failed",
            layer="control_plane",
            source=actor,
            level="warning",
            subject_type="environment",
            subject_id=subject_id,
            detail={
                "path": str(repo_path),
                "returncode": int(completed.returncode),
                "output": output[:2000],
            },
        )

    def _rebuild_beads_database(
        self,
        repo_path: Path,
        actor: str,
        subject_id: str,
        state: JsonDict,
    ) -> bool:
        beads_dir = repo_path / ".beads"
        embedded = beads_dir / "embeddeddolt"
        if not embedded.exists():
            state["beads_dolt_rebuild"] = "skipped"
            state["beads_dolt_rebuild_reason"] = "embedded_dolt_missing"
            return False
        backup = beads_dir / (
            "embeddeddolt.rebuild.%s" % _safe_slug(utcnow())
        )
        try:
            shutil.move(str(embedded), str(backup))
        except OSError as exc:
            state["beads_dolt_rebuild"] = "failed"
            state["beads_dolt_rebuild_error"] = str(exc)
            return False
        state["beads_dolt_rebuild"] = "started"
        state["beads_dolt_rebuild_backup"] = str(backup)
        try:
            completed = subprocess.run(
                [_beads_cli(), "bootstrap", "--yes"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - keep the backup for operator repair.
            state["beads_dolt_rebuild"] = "error"
            state["beads_dolt_rebuild_error"] = str(exc)
            return False
        output = (completed.stderr or completed.stdout or "").strip()
        if completed.returncode == 0:
            state["beads_dolt_rebuild"] = "ok"
            if output:
                state["beads_dolt_rebuild_output"] = output[:1000]
            self._restore_beads_tracked_exports(repo_path, actor, subject_id, "dolt_rebuild")
            self.record_log(
                "bridge.beads.dolt_rebuilt",
                layer="control_plane",
                source=actor,
                subject_type="environment",
                subject_id=subject_id,
                detail={
                    "path": str(repo_path),
                    "backup": str(backup),
                },
            )
            return True
        state["beads_dolt_rebuild"] = "failed"
        state["beads_dolt_rebuild_returncode"] = int(completed.returncode)
        state["beads_dolt_rebuild_error"] = output[:2000]
        return False

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

    def _record_beads_source_state(
        self,
        actor: str,
        repo: BeadsRepository,
        state: JsonDict,
        level: str,
    ) -> None:
        self.record_log(
            "bridge.beads.repository_source",
            layer="control_plane",
            source=actor,
            level=level,
            subject_type="environment",
            subject_id=repo.id,
            detail=state,
        )

    def _ensure_beads_source_remediation_task(
        self,
        repo: BeadsRepository,
        source_state: JsonDict,
        actor: str,
        status: str,
    ) -> Optional[Task]:
        owner = self._beads_repository_owner_agent(repo, actor)
        if owner is None:
            self.record_log(
                "bridge.beads.source_remediation.no_owner",
                layer="control_plane",
                source=actor,
                level="warning",
                subject_type="environment",
                subject_id=repo.id,
                detail={"repository": self._beads_repository_ref(repo), "source_state": source_state},
            )
            return None
        existing = self._existing_beads_source_remediation_task(repo, status)
        if existing is not None:
            return existing
        title = "Repair %s checkout before Beads polling" % repo.name
        dirty_paths = source_state.get("dirty_paths") or []
        description = (
            "The Beads bridge refused to poll %(name)s because %(path)s is in "
            "%(status)s state.\n\n"
            "This task belongs to the agent that owns that environment. Fetch "
            "upstream, pull with rebase enabled, then intentionally merge or "
            "re-apply any local changes so the checkout is clean and aligned "
            "with upstream before Beads polling resumes.\n\n"
            "Expected operator-grade shape:\n"
            "- inspect `git status --porcelain` and `git branch --show-current`\n"
            "- run `git fetch --prune` and `git pull --rebase --autostash` or an "
            "equivalent explicit rebase workflow\n"
            "- resolve conflicts by preserving intentional local work, not by "
            "discarding it blindly\n"
            "- run the repository bootstrap/test contract if the merge changes "
            "tracked source files\n"
            "- leave the registered checkout clean, with a pushed branch or PR "
            "when local changes need to become upstream changes\n\n"
            "Dirty paths observed by mac: %(dirty_paths)s\n"
            "Source state: %(source_state)s"
        ) % {
            "name": repo.name,
            "path": str(Path(repo.path).expanduser()),
            "status": source_state.get("status"),
            "dirty_paths": ", ".join(str(item) for item in dirty_paths) or "none",
            "source_state": json_dumps(source_state),
        }
        metadata = {
            "origin": {
                "type": "beads_source_remediation",
                "repository_id": repo.id,
                "repository_name": repo.name,
                "repository_path": repo.path,
                "source": repo.source,
                "repository_contract": repo.metadata.get("repository_contract"),
            },
            "target_agent_id": owner.id,
            "target_agent_name": owner.name,
            "remediation": {
                "type": "beads_source_refresh",
                "repository_id": repo.id,
                "repository_name": repo.name,
                "repository_path": repo.path,
                "source_status": status,
                "source_state": source_state,
                "required_workflow": "git_pull_rebase_then_merge_local_changes",
            },
            "publication_target": "environment://beads-repository/%s/source" % repo.id,
            "policy": {
                "expected_evidence_type": "repo_change",
            },
        }
        task = self.create_task(
            title,
            description=description,
            project=repo.project,
            priority=95,
            required_capabilities=repo.required_capabilities,
            metadata=metadata,
            actor=actor,
        )
        self.record_log(
            "bridge.beads.source_remediation.task_created",
            layer="control_plane",
            source=actor,
            subject_type="task",
            subject_id=task.id,
            detail={
                "repository_id": repo.id,
                "repository_name": repo.name,
                "owner_agent_id": owner.id,
                "source_status": status,
            },
        )
        return task

    def _existing_beads_source_remediation_task(
        self,
        repo: BeadsRepository,
        status: str,
    ) -> Optional[Task]:
        active_states = {
            TaskState.OPEN.value,
            TaskState.BLOCKED.value,
            TaskState.CLAIMED.value,
            TaskState.RUNNING.value,
            TaskState.NEEDS_REVIEW.value,
            TaskState.REVIEWING.value,
        }
        for task in self.list_tasks():
            if task.state not in active_states:
                continue
            metadata = task.metadata if isinstance(task.metadata, dict) else {}
            remediation = metadata.get("remediation")
            if not isinstance(remediation, dict):
                continue
            if remediation.get("type") != "beads_source_refresh":
                continue
            if remediation.get("repository_id") != repo.id:
                continue
            if remediation.get("source_status") != status:
                continue
            return task
        return None

    def _beads_repository_owner_agent(
        self,
        repo: BeadsRepository,
        actor: str,
    ) -> Optional[Agent]:
        metadata = repo.metadata if isinstance(repo.metadata, dict) else {}
        owner_candidates = [
            metadata.get("owner_agent_id"),
            metadata.get("owning_agent_id"),
            metadata.get("environment_owner_agent_id"),
            metadata.get("owner_agent_name"),
            metadata.get("owning_agent_name"),
            metadata.get("environment_owner_agent_name"),
            actor,
            os.environ.get("MAC_BEADS_BRIDGE_HUB_AGENT"),
        ]
        for candidate in owner_candidates:
            agent = self._agent_by_id_or_name(candidate)
            if agent is not None:
                return agent
        return None

    def _agent_by_id_or_name(self, value: Any) -> Optional[Agent]:
        candidate = str(value or "").strip()
        if not candidate:
            return None
        row = self.store.query_one(
            "SELECT * FROM agents WHERE id = ? OR name = ? ORDER BY id LIMIT 1",
            (candidate, candidate),
        )
        return self._agent_from_row(row) if row is not None else None

    def _beads_repair_action(
        self,
        repo: BeadsRepository,
        repo_path: Path,
        reason: str,
    ) -> JsonDict:
        return {
            "schema": "mac.beads_bridge.repair_action.v1",
            "type": "beads_canonical_reconcile",
            "reason": reason,
            "repository_id": repo.id,
            "repository_name": repo.name,
            "path": str(repo_path),
            "policy": "dispatch imports only canonical `bd ready --json` output; tracked JSONL is diagnostics only",
            "commands": [
                "bd doctor",
                "bd dolt pull",
                "bd ready --json",
                "bd export -o .beads/issues.jsonl",
                "git add .beads/issues.jsonl",
                "git commit -m 'Repair Beads tracked export'",
                "git push",
            ],
        }

    def _beads_repository_ref(self, repo: BeadsRepository) -> JsonDict:
        return {
            "schema": "mac.beads_repository_ref.v1",
            "id": repo.id,
            "name": repo.name,
            "path": repo.path,
            "source": repo.source,
            "project": repo.project,
            "required_capabilities": list(repo.required_capabilities),
            "enabled": repo.enabled,
            "poll_interval_seconds": repo.poll_interval_seconds,
        }

    def _beads_repository_health(
        self,
        status: str,
        reason: str,
        detail: Optional[JsonDict] = None,
        *,
        summary: Optional[str] = None,
    ) -> JsonDict:
        detail_copy = json_loads(json_dumps(ensure_json_object(detail or {})), {})
        return {
            "schema": "mac.beads_repository_health.v1",
            "status": status,
            "reason": reason,
            "summary": summary or reason,
            "checked_at": utcnow(),
            "detail": detail_copy,
        }

    def _beads_repository_health_from_source_state(self, source_state: Optional[JsonDict]) -> JsonDict:
        state = ensure_json_object(source_state or {})
        authority = ensure_json_object(state.get("authority") or {})
        authority_status = str(authority.get("status") or "ok")
        if authority_status == "drift" or state.get("authority_drift"):
            return self._beads_repository_health(
                "unhealthy",
                "authority_drift",
                state,
                summary="canonical Beads DB and tracked JSONL ready sets disagree",
            )
        if authority_status == "export_error":
            return self._beads_repository_health(
                "unhealthy",
                "authority_export_error",
                state,
                summary="tracked Beads JSONL export is unreadable",
            )
        return self._beads_repository_health("healthy", "canonical_beads_db_ok", state)

    def _ready_beads_issues(
        self,
        repo: BeadsRepository,
        *,
        poll_path: Optional[Path] = None,
        actor: str = "beads-bridge",
        source_state: Optional[JsonDict] = None,
    ) -> List[JsonDict]:
        repo_path = poll_path or Path(repo.path).expanduser()
        if not repo_path.exists():
            raise ValidationError("beads repository path does not exist: %s" % repo_path)
        if repo_path.is_file():
            raise ValidationError(
                "beads repository path must be a repository directory with canonical DB state, not a JSONL export: %s"
                % repo_path
            )
        completed: Any = None
        try:
            completed = self.beads_bridge.run(["ready", "--json"], cwd=repo_path, timeout=15)
        except (OSError, subprocess.SubprocessError):
            completed = None
        if completed is not None and completed.returncode == 0:
            output = completed.stdout.strip()
            data = json_loads(output, []) if output else []
            if not isinstance(data, list):
                raise ValidationError("bd ready --json did not return a list for %s" % repo.path)
            canonical = [issue for issue in data if self._bead_issue_is_importable(issue)]
            jsonl_ready: List[JsonDict] = []
            jsonl_error: Optional[str] = None
            jsonl_path = repo_path / ".beads" / "issues.jsonl"
            if jsonl_path.exists():
                try:
                    jsonl_ready = self._ready_beads_issues_from_jsonl(jsonl_path)
                except Exception as exc:  # noqa: BLE001 - DB authority can still poll.
                    jsonl_error = str(exc)
            canonical_ids = set(self._beads_issue_ids(canonical))
            jsonl_ids = set(self._beads_issue_ids(jsonl_ready))
            authority_status = "ok"
            if jsonl_error:
                authority_status = "export_error"
            elif canonical_ids != jsonl_ids:
                authority_status = "drift"
            observation = self._record_beads_authority_observation(
                repo,
                repo_path,
                authority="beads_db",
                status=authority_status,
                mode="canonical_bd_ready",
                canonical_ready_issues=canonical,
                jsonl_ready_issues=jsonl_ready,
                actor=actor,
                source_state=source_state,
                bd_returncode=completed.returncode,
                jsonl_error=jsonl_error,
            )
            self._record_beads_export_findings(
                repo,
                repo_path,
                canonical_ready_issues=canonical,
                jsonl_ready_issues=jsonl_ready,
                actor=actor,
                source_state=source_state,
                observation_id=observation.id,
                jsonl_error=jsonl_error,
            )
            return canonical
        bd_error = (
            (completed.stderr or completed.stdout or "").strip()
            if completed is not None
            else "bd ready unavailable"
        )
        jsonl_ready: List[JsonDict] = []
        jsonl_error: Optional[str] = None
        fallback_path = repo_path / ".beads" / "issues.jsonl"
        if fallback_path.exists():
            try:
                jsonl_ready = self._ready_beads_issues_from_jsonl(fallback_path)
            except Exception as exc:  # noqa: BLE001 - diagnostic export may also be broken.
                jsonl_error = str(exc)
        self._record_beads_authority_observation(
            repo,
            repo_path,
            authority="beads_db",
            status="unavailable",
            mode="canonical_bd_ready_failed",
            canonical_ready_issues=[],
            jsonl_ready_issues=jsonl_ready,
            actor=actor,
            source_state=source_state,
            bd_returncode=completed.returncode if completed is not None else None,
            bd_error=bd_error,
            jsonl_error=jsonl_error,
        )
        repair_action = self._beads_repair_action(repo, repo_path, "canonical_unavailable")
        finding = self.record_integration_finding(
            "beads_repository",
            repo.id,
            "beads.canonical_unavailable",
            "Canonical Beads DB is unavailable",
            {
                "schema": "mac.integration.beads_canonical_unavailable.v1",
                "repository": self._beads_repository_ref(repo),
                "poll_path": str(repo_path),
                "actor": actor,
                "bd_error": bd_error[:2000],
                "jsonl_ready_count": len(jsonl_ready),
                "jsonl_ready_ids": self._beads_issue_ids(jsonl_ready),
                "jsonl_error": jsonl_error,
                "policy": "mac fails closed and never dispatches from JSONL when canonical `bd ready --json` is unavailable",
                "repair_action": repair_action,
            },
            severity="error",
            fingerprint=self._integration_fingerprint(
                {
                    "finding_type": "beads.canonical_unavailable",
                    "repository_id": repo.id,
                    "bd_error": bool(bd_error),
                    "jsonl_error": bool(jsonl_error),
                }
            ),
            notify=True,
            channels=["dashboard", "hermes"],
            notification_body=(
                "%s cannot read canonical Beads ready state. mac will not import JSONL-only work."
            )
            % repo.name,
        )
        if source_state is not None:
            source_state["authority_findings"] = [finding.to_dict()]
            source_state["repair_action"] = repair_action
        raise ValidationError(
            "canonical Beads DB unavailable for %s: %s; JSONL exports are diagnostics only"
            % (repo.name, bd_error or "bd ready failed")
        )

    def _read_beads_jsonl_issues(self, issues_path: Path) -> List[JsonDict]:
        if not issues_path.exists():
            raise ValidationError("beads issues file not found: %s" % issues_path)
        issues: List[JsonDict] = []
        for line_number, raw in enumerate(issues_path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            try:
                issue = json_loads(raw, {})
            except Exception as exc:  # noqa: BLE001 - annotate which export row is broken.
                raise ValidationError(
                    "beads issues file %s has invalid JSON on line %d: %s"
                    % (issues_path, line_number, exc)
                ) from exc
            if isinstance(issue, dict) and issue.get("_type", "issue") == "issue":
                issues.append(issue)
        return issues

    def _ready_beads_issues_from_jsonl(self, issues_path: Path) -> List[JsonDict]:
        issues = self._read_beads_jsonl_issues(issues_path)
        by_id = {str(issue.get("id")): issue for issue in issues if issue.get("id")}
        ready: List[JsonDict] = []
        for issue in issues:
            if not self._bead_issue_is_importable(issue):
                continue
            dependencies = issue.get("dependencies") or []
            if int(issue.get("dependency_count") or 0) > 0 and not dependencies:
                continue
            blocked = False
            for dependency in dependencies:
                if not isinstance(dependency, dict):
                    blocked = True
                    break
                dep_id = str(dependency.get("depends_on_id") or "").strip()
                dep_issue = by_id.get(dep_id)
                if dep_issue is None or str(dep_issue.get("status") or "") != "closed":
                    blocked = True
                    break
            if not blocked:
                ready.append(issue)
        ready.sort(key=lambda item: (int(item.get("priority") or 2), str(item.get("created_at") or ""), str(item.get("id") or "")))
        return ready

    def _beads_issue_ids(self, issues: Iterable[JsonDict]) -> List[str]:
        return sorted({str(issue.get("id") or "").strip() for issue in issues if issue.get("id")})

    def _record_beads_authority_observation(
        self,
        repo: BeadsRepository,
        repo_path: Path,
        *,
        authority: str,
        status: str,
        mode: str,
        canonical_ready_issues: List[JsonDict],
        jsonl_ready_issues: List[JsonDict],
        actor: str,
        source_state: Optional[JsonDict],
        bd_returncode: Optional[int] = None,
        bd_error: Optional[str] = None,
        jsonl_error: Optional[str] = None,
    ) -> IntegrationObservation:
        canonical_ids = self._beads_issue_ids(canonical_ready_issues)
        jsonl_ids = self._beads_issue_ids(jsonl_ready_issues)
        detail: JsonDict = {
            "schema": "mac.integration.beads_authority_observation.v1",
            "repository_id": repo.id,
            "repository_name": repo.name,
            "source": repo.source,
            "project": repo.project,
            "poll_path": str(repo_path),
            "mode": mode,
            "actor": actor,
            "canonical_authority": "beads_db" if authority == "beads_db" else "tracked_jsonl",
            "canonical_ready_count": len(canonical_ids),
            "canonical_ready_ids": canonical_ids,
            "jsonl_ready_count": len(jsonl_ids),
            "jsonl_ready_ids": jsonl_ids,
        }
        if bd_returncode is not None:
            detail["bd_ready_returncode"] = bd_returncode
        if bd_error:
            detail["bd_error"] = bd_error[:2000]
        if jsonl_error:
            detail["jsonl_error"] = jsonl_error[:2000]
        fingerprint = self._integration_fingerprint(
            {
                "authority": authority,
                "status": status,
                "canonical_ready_ids": canonical_ids,
                "jsonl_ready_ids": jsonl_ids,
                "bd_error": bool(bd_error),
                "jsonl_error": bool(jsonl_error),
            }
        )
        observation = self.record_integration_observation(
            "beads_repository",
            repo.id,
            authority,
            status,
            fingerprint=fingerprint,
            cursor=str(repo_path),
            detail=detail,
        )
        if source_state is not None:
            source_state["authority"] = {
                "schema": "mac.integration.beads_authority_summary.v1",
                "authority": authority,
                "status": status,
                "mode": mode,
                "observation_id": observation.id,
                "canonical_ready_count": len(canonical_ids),
                "jsonl_ready_count": len(jsonl_ids),
                "canonical_ready_ids": canonical_ids[:50],
                "jsonl_ready_ids": jsonl_ids[:50],
            }
            if bd_error:
                source_state["authority"]["bd_error"] = bd_error[:1000]
            if jsonl_error:
                source_state["authority"]["jsonl_error"] = jsonl_error[:1000]
        return observation

    def _record_beads_export_findings(
        self,
        repo: BeadsRepository,
        repo_path: Path,
        *,
        canonical_ready_issues: List[JsonDict],
        jsonl_ready_issues: List[JsonDict],
        actor: str,
        source_state: Optional[JsonDict],
        observation_id: str,
        jsonl_error: Optional[str] = None,
    ) -> None:
        findings: List[JsonDict] = []
        active_ready_mismatch: List[str] = []
        active_parse_error: List[str] = []
        if jsonl_error:
            fingerprint = self._integration_fingerprint(
                {"finding_type": "beads.export_parse_error", "error": jsonl_error}
            )
            active_parse_error.append(fingerprint)
            finding = self.record_integration_finding(
                "beads_repository",
                repo.id,
                "beads.export_parse_error",
                "Beads tracked export cannot be parsed",
                {
                    "schema": "mac.integration.beads_export_parse_error.v1",
                    "repository": self._beads_repository_ref(repo),
                    "poll_path": str(repo_path),
                    "observation_id": observation_id,
                    "actor": actor,
                    "error": jsonl_error,
                },
                severity="warning",
                fingerprint=fingerprint,
                notify=True,
                channels=["dashboard", "hermes"],
                notification_body=(
                    "%s has an unreadable .beads/issues.jsonl export. "
                    "mac is using canonical `bd ready --json` output for imports."
                )
                % repo.name,
            )
            findings.append(finding.to_dict())
        self._resolve_integration_findings_for_source(
            "beads_repository",
            repo.id,
            "beads.export_parse_error",
            active_fingerprints=active_parse_error,
        )
        self._resolve_integration_findings_for_source(
            "beads_repository",
            repo.id,
            "beads.canonical_unavailable",
            active_fingerprints=[],
        )

        canonical_ids = self._beads_issue_ids(canonical_ready_issues)
        jsonl_ids = self._beads_issue_ids(jsonl_ready_issues)
        canonical_only = sorted(set(canonical_ids) - set(jsonl_ids))
        jsonl_only = sorted(set(jsonl_ids) - set(canonical_ids))
        existing_jsonl_only = self._existing_beads_project_items_by_external_id(
            repo,
            jsonl_only,
        )
        already_imported_jsonl_only = sorted(existing_jsonl_only)
        untracked_jsonl_only = sorted(
            issue_id for issue_id in jsonl_only if issue_id not in existing_jsonl_only
        )
        if canonical_only or jsonl_only:
            repair_action = self._beads_repair_action(repo, repo_path, "authority_drift")
            fingerprint = self._integration_fingerprint(
                {
                    "finding_type": "beads.export_drift.ready_mismatch",
                    "canonical_only_ready_ids": canonical_only,
                    "jsonl_only_ready_ids": jsonl_only,
                }
            )
            active_ready_mismatch.append(fingerprint)
            finding = self.record_integration_finding(
                "beads_repository",
                repo.id,
                "beads.export_drift.ready_mismatch",
                "Beads tracked export ready set differs from canonical DB",
                {
                    "schema": "mac.integration.beads_export_drift.v1",
                    "repository": self._beads_repository_ref(repo),
                    "poll_path": str(repo_path),
                    "observation_id": observation_id,
                    "actor": actor,
                    "canonical_authority": "beads_db",
                    "policy": "mac imports canonical `bd ready --json` output and never imports JSONL-only issues while the DB is readable",
                    "canonical_ready_count": len(canonical_ids),
                    "canonical_ready_ids": canonical_ids,
                    "jsonl_ready_count": len(jsonl_ids),
                    "jsonl_ready_ids": jsonl_ids,
                    "canonical_only_ready_count": len(canonical_only),
                    "canonical_only_ready_ids": canonical_only,
                    "jsonl_only_ready_count": len(jsonl_only),
                    "jsonl_only_ready_ids": jsonl_only,
                    "jsonl_only_untracked_count": len(untracked_jsonl_only),
                    "jsonl_only_untracked_ids": untracked_jsonl_only,
                    "jsonl_only_already_imported_count": len(already_imported_jsonl_only),
                    "jsonl_only_already_imported_ids": already_imported_jsonl_only,
                    "jsonl_only_existing_tasks": existing_jsonl_only,
                    "repair_action": repair_action,
                },
                severity="warning",
                fingerprint=fingerprint,
                notify=True,
                channels=["dashboard", "hermes"],
                notification_body=(
                    "%s has Beads ready-state drift: canonical-only=%d, JSONL-only=%d. "
                    "mac imports only canonical `bd ready --json` output."
                )
                % (repo.name, len(canonical_only), len(jsonl_only)),
            )
            findings.append(finding.to_dict())
        self._resolve_integration_findings_for_source(
            "beads_repository",
            repo.id,
            "beads.export_drift.ready_mismatch",
            active_fingerprints=active_ready_mismatch,
        )
        self._resolve_integration_findings_for_source(
            "beads_repository",
            repo.id,
            "beads.export_drift.jsonl_only_ready",
            active_fingerprints=[],
        )
        if source_state is not None:
            source_state["authority_findings"] = findings
            if canonical_only or jsonl_only:
                source_state["authority_drift"] = {
                    "schema": "mac.integration.beads_authority_drift_summary.v1",
                    "canonical_only_ready_count": len(canonical_only),
                    "canonical_only_ready_ids": canonical_only,
                    "jsonl_only_ready_count": len(jsonl_only),
                    "jsonl_only_ready_ids": jsonl_only,
                    "jsonl_only_untracked_count": len(untracked_jsonl_only),
                    "jsonl_only_untracked_ids": untracked_jsonl_only,
                    "jsonl_only_already_imported_count": len(already_imported_jsonl_only),
                    "jsonl_only_already_imported_ids": already_imported_jsonl_only,
                    "jsonl_only_existing_tasks": existing_jsonl_only,
                    "repair_action": self._beads_repair_action(repo, repo_path, "authority_drift"),
                }

    def _existing_beads_project_items_by_external_id(
        self,
        repo: BeadsRepository,
        external_ids: Iterable[str],
    ) -> Dict[str, JsonDict]:
        ids = sorted({str(external_id or "").strip() for external_id in external_ids if external_id})
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        rows = self.store.query_all(
            """
            SELECT project_items.external_id, project_items.task_id, tasks.state
            FROM project_items
            LEFT JOIN tasks ON tasks.id = project_items.task_id
            WHERE project_items.source = ? AND project_items.external_id IN (%s)
            """
            % placeholders,
            (repo.source, *ids),
        )
        return {
            str(row["external_id"]): {
                "task_id": row["task_id"],
                "state": row["state"],
            }
            for row in rows
        }

    # mac-3xpl: schema-validation limits for fields imported from bd
    # ready --json. The bd CLI is upstream and effectively untrusted at
    # the API surface; an oversized or control-char-laden title would
    # otherwise flow into MAC project items unsanitised.
    _BEAD_IMPORT_TITLE_MAX = 512
    _BEAD_IMPORT_DESCRIPTION_MAX = 32 * 1024
    _BEAD_IMPORT_TYPE_MAX = 64
    _BEAD_IMPORT_PRIORITY_MIN = 0
    _BEAD_IMPORT_PRIORITY_MAX = 4

    @staticmethod
    def _strip_control_chars(value: str) -> str:
        """Strip ASCII control chars except \\t/\\n; reject ANSI escape
        sequences. Used by bd import (mac-3xpl)."""
        return "".join(
            c
            for c in value
            if (c >= " " or c in ("\t", "\n"))
        )

    def _bead_issue_is_importable(self, issue: Any) -> bool:
        if not isinstance(issue, dict):
            return False
        bead_id = str(issue.get("id") or "").strip()
        if not bead_id:
            return False
        if not _BEAD_ID_RE.match(bead_id):
            return False
        if str(issue.get("status") or "").strip().lower() != "open":
            return False
        # Field-shape validation: titles/descriptions must be strings of
        # bounded length, type must be a short slug.
        title = issue.get("title")
        if title is not None and not (
            isinstance(title, str) and len(title) <= self._BEAD_IMPORT_TITLE_MAX
        ):
            return False
        description = issue.get("description")
        if description is not None and not (
            isinstance(description, str)
            and len(description) <= self._BEAD_IMPORT_DESCRIPTION_MAX
        ):
            return False
        issue_type = issue.get("type")
        if issue_type is not None and not (
            isinstance(issue_type, str) and len(issue_type) <= self._BEAD_IMPORT_TYPE_MAX
        ):
            return False
        # Priority, when present, must be in the bd-canonical 0-4 range.
        if "priority" in issue:
            try:
                pr = int(issue["priority"])
            except (TypeError, ValueError):
                return False
            if not (self._BEAD_IMPORT_PRIORITY_MIN <= pr <= self._BEAD_IMPORT_PRIORITY_MAX):
                return False
        return True

    def _import_bead_issue(
        self,
        repo: BeadsRepository,
        issue: JsonDict,
        actor: str,
        *,
        repository_contract: Optional[JsonDict] = None,
    ) -> ProjectItem:
        issue_id = str(issue["id"])
        priority = 100 - int(issue.get("priority") or 2)
        contract = repository_contract or self._repository_contract_for_beads_repo(repo)
        payload = {
            "schema": "mac.beads_bridge.issue.v1",
            "repository": self._beads_repository_ref(repo),
            "repository_contract": contract,
            "issue": issue,
        }
        metadata = {
            "origin": {
                "type": "beads",
                "repository_id": repo.id,
                "repository_name": repo.name,
                "repository_path": repo.path,
                "source": repo.source,
                "bead_id": issue_id,
                "repository_contract": contract,
            },
            "acc_metadata": {
                "source": "mac-beads-bridge",
                "beads_id": issue_id,
                "beads_path": str(Path(repo.path).expanduser() / ".beads" / "issues.jsonl"),
                "repo_beads_workflow": True,
                "workflow_role": "work",
                "beads_sync_claim_on_claim": True,
                "beads_sync_close_on_complete": True,
                "repository_contract_schema": contract["schema"],
                "repository_contract_project": contract["project"],
            },
            "publication_target": "git://main",
        }
        # mac-3xpl: scrub control chars / ANSI escapes from upstream
        # title and description before they enter the task ledger.
        clean_title = self._strip_control_chars(
            str(issue.get("title") or issue_id)
        )[: self._BEAD_IMPORT_TITLE_MAX]
        clean_description = self._strip_control_chars(
            str(issue.get("description") or "")
        )[: self._BEAD_IMPORT_DESCRIPTION_MAX]
        return self.import_project_item(
            repo.source,
            issue_id,
            clean_title,
            payload,
            required_capabilities=repo.required_capabilities,
            description=clean_description,
            project=repo.project,
            priority=priority,
            metadata=metadata,
            actor=actor,
        )

    def _update_beads_repository_poll_state(
        self,
        repo_id: str,
        last_polled_at: str,
        *,
        last_imported_at: Optional[str],
        last_error: Optional[str],
        health: Optional[JsonDict] = None,
    ) -> None:
        metadata: Optional[JsonDict] = None
        if health is not None:
            row = self.store.query_one("SELECT metadata FROM beads_repositories WHERE id = ?", (repo_id,))
            metadata = ensure_json_object(json_loads(row["metadata"], {}) if row is not None else {})
            metadata["health"] = health
        self.store.execute(
            (
                """
                UPDATE beads_repositories
                SET last_polled_at = ?, last_imported_at = ?, last_error = ?, metadata = ?, updated_at = ?
                WHERE id = ?
                """
                if metadata is not None
                else
                """
                UPDATE beads_repositories
                SET last_polled_at = ?, last_imported_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """
            ),
            (
                (last_polled_at, last_imported_at, last_error, json_dumps(metadata), utcnow(), repo_id)
                if metadata is not None
                else (last_polled_at, last_imported_at, last_error, utcnow(), repo_id)
            ),
        )

    def _beads_binding_for_task(self, task: Task) -> Optional[JsonDict]:
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        origin = metadata.get("origin")
        acc_metadata = metadata.get("acc_metadata")
        if not isinstance(origin, dict) or origin.get("type") != "beads":
            return None
        if not isinstance(acc_metadata, dict):
            return None
        bead_id = str(origin.get("bead_id") or acc_metadata.get("beads_id") or "").strip()
        repo_path = str(origin.get("repository_path") or "").strip()
        if not bead_id or not repo_path:
            return None
        # mac-5xwh: refuse any bead_id that could smuggle argv flags into
        # `bd comment <bead_id> ...`. Real bead ids match the format
        # ``<prefix>-<slug>`` with alphanumerics + underscore only.
        if not _BEAD_ID_RE.match(bead_id):
            self.record_log(
                "bridge.beads.bind.refused",
                layer="control_plane",
                source="control_plane",
                level="warning",
                subject_type="task",
                subject_id=task.id,
                detail={"bead_id": bead_id, "reason": "bead_id format rejected"},
            )
            return None
        return {
            "bead_id": bead_id,
            "repo_path": repo_path,
            "repository_id": str(origin.get("repository_id") or "").strip(),
            "source": str(origin.get("source") or task.metadata.get("source") or "").strip(),
        }

    def _run_bd_for_task(self, task: Task, args: List[str], actor: str, action: str) -> bool:
        binding = self._beads_binding_for_task(task)
        if binding is None:
            return False
        repo_path = self._beads_sync_path_for_binding(binding, actor)
        if not repo_path.exists():
            return False
        registered_path = Path(str(binding["repo_path"])).expanduser()
        if not self._beads_cli_lock.acquire(blocking=False):
            self.record_log(
                "bridge.beads.sync_busy",
                layer="control_plane",
                source=actor,
                level="warning",
                subject_type="task",
                subject_id=task.id,
                detail={
                    "action": action,
                    "bead_id": binding["bead_id"],
                    "repo_path": str(repo_path),
                },
            )
            return False
        try:
            completed = self.beads_bridge.run(
                args,
                cwd=repo_path,
                actor=actor,
                timeout=20,
            )
            self._restore_beads_tracked_exports(repo_path, actor, task.id, action)
            if completed.returncode != 0:
                output = completed.output
                if action == "claim" and "already claimed" in output.lower():
                    self.record_log(
                        "bridge.beads.sync.claim_existing",
                        layer="control_plane",
                        source=actor,
                        subject_type="task",
                        subject_id=task.id,
                        detail={
                            "bead_id": binding["bead_id"],
                            "repo_path": str(repo_path),
                            "output": output[:1000],
                        },
                    )
                    return True
                if registered_path.exists() and registered_path.resolve() != repo_path.resolve():
                    fallback = self.beads_bridge.run(
                        args,
                        cwd=registered_path,
                        actor=actor,
                        timeout=20,
                    )
                    self._restore_beads_tracked_exports(
                        registered_path,
                        actor,
                        task.id,
                        "%s_registered_fallback" % action,
                    )
                    if fallback.returncode == 0 and self._push_beads_writeback(
                        registered_path,
                        actor,
                        task,
                        binding["bead_id"],
                        "%s_registered_fallback" % action,
                    ):
                        self.record_log(
                            "bridge.beads.sync.%s.registered_fallback" % action,
                            layer="control_plane",
                            source=actor,
                            level="warning",
                            subject_type="task",
                            subject_id=task.id,
                            detail={
                                "bead_id": binding["bead_id"],
                                "repo_path": str(repo_path),
                                "fallback_repo_path": str(registered_path),
                                "primary_error": output[:1000],
                            },
                        )
                        return True
                    fallback_output = fallback.output
                    if fallback_output:
                        output = "%s; registered checkout fallback failed: %s" % (
                            output,
                            fallback_output,
                        )
                raise ValidationError(output)
            if not self._push_beads_writeback(repo_path, actor, task, binding["bead_id"], action):
                raise ValidationError("Beads writeback push failed")
            self.record_log(
                "bridge.beads.sync.%s" % action,
                layer="control_plane",
                source=actor,
                subject_type="task",
                subject_id=task.id,
                detail={"bead_id": binding["bead_id"], "repo_path": str(repo_path)},
            )
            return True
        except Exception as exc:  # noqa: BLE001 - Beads sync is secondary to task state.
            self.record_log(
                "bridge.beads.ledger_failed" if action == "ledger" else "bridge.beads.sync_failed",
                layer="control_plane",
                source=actor,
                level="warning",
                subject_type="task",
                subject_id=task.id,
                detail={
                    "action": action,
                    "bead_id": binding["bead_id"],
                    "repo_path": str(repo_path),
                    "error": str(exc),
                },
            )
            return False
        finally:
            self._beads_cli_lock.release()

    def _push_beads_writeback(
        self,
        repo_path: Path,
        actor: str,
        task: Task,
        bead_id: str,
        action: str,
    ) -> bool:
        if not _truthy_env("MAC_BEADS_PUSH_WRITEBACKS", "1"):
            return True
        completed = self.beads_bridge.run(
            ["dolt", "push"],
            cwd=repo_path,
            timeout=60,
        )
        if completed.returncode == 0:
            self.record_log(
                "bridge.beads.writeback_pushed",
                layer="control_plane",
                source=actor,
                subject_type="task",
                subject_id=task.id,
                detail={"action": action, "bead_id": bead_id, "repo_path": str(repo_path)},
            )
            return True
        output = completed.output
        self.record_log(
            "bridge.beads.writeback_push_failed",
            layer="control_plane",
            source=actor,
            level="warning",
            subject_type="task",
            subject_id=task.id,
            detail={
                "action": action,
                "bead_id": bead_id,
                "repo_path": str(repo_path),
                "error": output[:1000],
            },
        )
        return False

    def _append_beads_ledger_comment(
        self,
        task: Task,
        actor: str,
        event: str,
        message: str,
        *,
        fields: Optional[Dict[str, Any]] = None,
    ) -> bool:
        binding = self._beads_binding_for_task(task)
        if binding is None:
            return False
        acc_metadata = task.metadata.get("acc_metadata") if isinstance(task.metadata, dict) else {}
        if isinstance(acc_metadata, dict) and acc_metadata.get("beads_sync_ledger_comments") is False:
            return False
        pieces = [
            "mac-ledger v1",
            "task=%s" % task.id,
            "event=%s" % _compact_beads_ledger_text(event, limit=64),
            "actor=%s" % _compact_beads_ledger_text(actor, limit=96),
            _compact_beads_ledger_text(message, limit=220),
        ]
        for key, value in sorted(ensure_json_object(fields).items()):
            if value is None or value == "":
                continue
            pieces.append(
                "%s=%s"
                % (
                    _compact_beads_ledger_text(key, limit=40),
                    _compact_beads_ledger_text(value, limit=160),
                )
            )
        # mac-9jm2: tag the comment with a deterministic fingerprint so
        # retries don't duplicate it. We check task_history for a prior
        # ``task.beads_ledger_posted`` row with the same fingerprint
        # before re-issuing the bd call.
        fingerprint = hashlib.sha256(
            ("\n".join(pieces) + "|" + binding["bead_id"]).encode("utf-8")
        ).hexdigest()[:16]
        pieces.append("fp=%s" % fingerprint)
        existing = self.store.query_one(
            """
            SELECT id FROM task_history
            WHERE task_id = ?
              AND event_type = 'task.beads_ledger_posted'
              AND json_extract(detail, '$.fingerprint') = ?
            LIMIT 1
            """,
            (task.id, fingerprint),
        )
        if existing is not None:
            return True
        ok = self._run_bd_for_task(
            task,
            ["comment", binding["bead_id"], " | ".join(pieces)],
            actor,
            "ledger",
        )
        if ok:
            try:
                self._record_history(
                    task.id,
                    "task.beads_ledger_posted",
                    actor,
                    task.state,
                    task.state,
                    {"fingerprint": fingerprint, "event": event},
                )
            except Exception:  # noqa: BLE001 - history insert failure is non-fatal here
                pass
        return ok

    def _latest_failure_context(
        self,
        task: Task,
        detail: Optional[Dict[str, Any]] = None,
    ) -> JsonDict:
        fields: JsonDict = {}
        source_detail = detail if isinstance(detail, dict) else {}
        if source_detail.get("reason"):
            fields["failure_reason"] = source_detail.get("reason")
        problems = source_detail.get("problems")
        if isinstance(problems, list) and problems:
            fields["failure_problems"] = "; ".join(str(item) for item in problems[:4])
        if source_detail.get("evidence_id"):
            fields["evidence_id"] = source_detail.get("evidence_id")

        if not fields.get("failure_reason") or not fields.get("failure_problems"):
            row = self.store.query_one(
                """
                SELECT detail, actor, created_at FROM task_history
                WHERE task_id = ?
                  AND (
                    (event_type = 'task.transitioned' AND to_state = ?)
                    OR event_type = 'task.beads_retry_exhausted'
                  )
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (task.id, TaskState.FAILED.value),
            )
            if row is not None:
                hist_detail = ensure_json_object(json_loads(row["detail"], {}))
                fields.setdefault("failure_actor", row["actor"])
                fields.setdefault("failure_at", row["created_at"])
                if hist_detail.get("reason"):
                    fields.setdefault("failure_reason", hist_detail.get("reason"))
                hist_problems = hist_detail.get("problems")
                if isinstance(hist_problems, list) and hist_problems:
                    fields.setdefault(
                        "failure_problems",
                        "; ".join(str(item) for item in hist_problems[:4]),
                    )
                if hist_detail.get("evidence_id"):
                    fields.setdefault("evidence_id", hist_detail.get("evidence_id"))

        evidence: Optional[Evidence] = None
        evidence_id = str(fields.get("evidence_id") or "").strip()
        if evidence_id:
            try:
                evidence = self.get_evidence(evidence_id)
            except NotFoundError:
                evidence = None
        if evidence is None:
            rows = self.store.query_all(
                "SELECT * FROM evidence WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (task.id,),
            )
            if rows:
                evidence = self._evidence_from_row(rows[0])
        if evidence is not None:
            fields.setdefault("evidence_id", evidence.id)
            fields.setdefault("evidence_kind", evidence.kind)
            fields.setdefault("evidence_by", evidence.created_by)
            if evidence.summary:
                fields.setdefault("evidence_summary", evidence.summary)
            metadata = ensure_json_object(evidence.metadata)
            if metadata.get("returncode") is not None:
                fields.setdefault("returncode", metadata.get("returncode"))
            verification = ensure_json_object(metadata.get("verification"))
            verification_problems = verification.get("problems")
            if isinstance(verification_problems, list) and verification_problems:
                fields.setdefault(
                    "verification_problems",
                    "; ".join(str(item) for item in verification_problems[:4]),
                )
            if verification.get("status"):
                fields.setdefault("verification_status", verification.get("status"))
            if verification.get("summary"):
                fields.setdefault("verification_summary", verification.get("summary"))
        if not fields.get("failure_reason") and fields.get("verification_problems"):
            fields["failure_reason"] = "verification_contract_failed"
        return fields

    def _failure_summary_text(self, task: Task, fields: JsonDict) -> str:
        reason = _compact_beads_ledger_text(
            fields.get("failure_reason") or "failed task", limit=120
        )
        problems = _compact_beads_ledger_text(
            fields.get("failure_problems") or fields.get("verification_problems") or "",
            limit=220,
        )
        evidence = _compact_beads_ledger_text(fields.get("evidence_id") or "", limit=80)
        parts = ["mac task %s failed: %s" % (task.id, reason)]
        if problems:
            parts.append("problems: %s" % problems)
        if evidence:
            parts.append("evidence: %s" % evidence)
        return " | ".join(parts)

    def _failure_summary_fingerprint(self, task: Task, fields: JsonDict) -> str:
        payload = {
            "task_id": task.id,
            "state": task.state,
            "reason": fields.get("failure_reason"),
            "problems": fields.get("failure_problems") or fields.get("verification_problems"),
            "evidence_id": fields.get("evidence_id"),
            "retry_exhausted_at": ensure_json_object(
                task.metadata.get("beads_reconciliation")
                if isinstance(task.metadata, dict)
                else {}
            ).get("retry_exhausted_at"),
        }
        digest = hashlib.sha256(
            json_dumps(payload).encode("utf-8")
        ).hexdigest()
        return "sha256:%s" % digest

    def _append_beads_failure_summary_if_needed(
        self,
        task: Task,
        actor: str,
        *,
        detail: Optional[Dict[str, Any]] = None,
        event: str = "failure_summary",
    ) -> bool:
        binding = self._beads_binding_for_task(task)
        if binding is None:
            return False
        metadata = ensure_json_object(task.metadata)
        reconciliation = ensure_json_object(metadata.get("beads_reconciliation"))
        fields = self._latest_failure_context(task, detail)
        fingerprint = self._failure_summary_fingerprint(task, fields)
        if (
            reconciliation.get("failure_summary_comment_fingerprint") == fingerprint
            and reconciliation.get("failure_summary_pushed_fingerprint") == fingerprint
        ):
            return False
        note = self._failure_summary_text(task, fields)
        note_ok = self._run_bd_for_task(
            task,
            ["update", binding["bead_id"], "--append-notes", note],
            actor,
            "failure_note",
        )
        comment_ok = self._append_beads_ledger_comment(
            task,
            actor,
            event,
            note,
            fields=fields,
        )
        if not (note_ok or comment_ok):
            return False
        reconciliation.update(
            {
                "failure_summary_comment_fingerprint": fingerprint,
                "failure_summary_pushed_fingerprint": fingerprint,
                "failure_summary_comment_at": utcnow(),
                "failure_summary_comment_event": event,
            }
        )
        metadata["beads_reconciliation"] = reconciliation
        self.store.execute(
            "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
            (json_dumps(metadata), utcnow(), task.id),
        )
        return True

    def _sync_beads_transition_ledger(
        self,
        task: Task,
        actor: str,
        from_state: str,
        to_state: str,
        detail: Dict[str, Any],
    ) -> None:
        if to_state not in {
            TaskState.RUNNING.value,
            TaskState.NEEDS_REVIEW.value,
            TaskState.REVIEWING.value,
            TaskState.COMPLETED.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
            TaskState.OPEN.value,
        }:
            return
        event = "state_%s" % to_state
        reason = detail.get("reason") if isinstance(detail, dict) else None
        fields: Dict[str, Any] = {"from": from_state, "to": to_state}
        for key in (
            "reason",
            "evidence_id",
            "review_id",
            "reviewer_agent_id",
            "publication_id",
        ):
            if isinstance(detail, dict) and detail.get(key):
                fields[key] = detail.get(key)
        problems = detail.get("problems") if isinstance(detail, dict) else None
        if isinstance(problems, list) and problems:
            fields["problems"] = "; ".join(str(item) for item in problems[:4])
        if to_state == TaskState.FAILED.value:
            fields.update(self._latest_failure_context(task, detail))
        self._append_beads_ledger_comment(
            task,
            actor,
            event,
            "state %s -> %s%s"
            % (
                from_state,
                to_state,
                (": %s" % reason) if reason else "",
            ),
            fields=fields,
        )
        if to_state == TaskState.FAILED.value:
            self._append_beads_failure_summary_if_needed(
                task,
                actor,
                detail=detail,
                event="state_failed_summary",
            )

    def _beads_sync_path_for_binding(self, binding: JsonDict, actor: str) -> Path:
        repo_id = str(binding.get("repository_id") or "").strip()
        if repo_id:
            try:
                repo = self.get_beads_repository(repo_id)
            except NotFoundError:
                repo = None
            if repo is not None:
                repo_path = Path(repo.path).expanduser()
                if repo_path.is_file() or not (repo_path / ".git").exists():
                    return repo_path
                bridge_path = self._beads_bridge_checkout_path(repo)
                if not bridge_path.exists():
                    state = self._refresh_beads_repository_source(repo, actor)
                    return Path(str(state.get("poll_path") or bridge_path)).expanduser()
                return bridge_path
        return Path(str(binding["repo_path"])).expanduser()

    def _restore_beads_tracked_exports(
        self,
        repo_path: Path,
        actor: str,
        task_id: str,
        action: str,
        *,
        subject_type: str = "task",
    ) -> None:
        if not _truthy_env("MAC_BEADS_RESTORE_TRACKED_EXPORTS"):
            return
        try:
            inside = subprocess.run(
                ["git", "-C", str(repo_path), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if inside.returncode != 0 or inside.stdout.strip() != "true":
                return
            status = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "status",
                    "--porcelain",
                    "--",
                    ".beads/config.yaml",
                    ".beads/issues.jsonl",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if status.returncode != 0 or not status.stdout.strip():
                return
            dirty_exports = [
                path
                for path in (".beads/config.yaml", ".beads/issues.jsonl")
                if any(line.endswith(path) for line in status.stdout.strip().splitlines())
            ]
            if not dirty_exports:
                return
            restored = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "restore",
                    "--staged",
                    "--worktree",
                    "--",
                    *dirty_exports,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if restored.returncode != 0:
                raise ValidationError((restored.stderr or restored.stdout or "").strip())
            self.record_log(
                "bridge.beads.tracked_exports_restored",
                layer="control_plane",
                source=actor,
                subject_type=subject_type,
                subject_id=task_id,
                detail={
                    "action": action,
                    "repo_path": str(repo_path),
                    "status": status.stdout.strip().splitlines(),
                },
            )
        except Exception as exc:  # noqa: BLE001 - Beads export cleanup is secondary.
            self.record_log(
                "bridge.beads.tracked_exports_restore_failed",
                layer="control_plane",
                source=actor,
                level="warning",
                subject_type=subject_type,
                subject_id=task_id,
                detail={
                    "action": action,
                    "repo_path": str(repo_path),
                    "error": str(exc),
                },
            )

    def _sync_beads_claim(self, task: Task, actor: str) -> None:
        binding = self._beads_binding_for_task(task)
        if binding is None:
            return
        acc_metadata = task.metadata.get("acc_metadata") if isinstance(task.metadata, dict) else {}
        if isinstance(acc_metadata, dict) and acc_metadata.get("beads_sync_claim_on_claim") is False:
            return
        self._run_bd_for_task(task, ["update", binding["bead_id"], "--claim"], actor, "claim")

    def _sync_existing_beads_task(
        self,
        task: Task,
        actor: str,
        *,
        issue: Optional[JsonDict] = None,
    ) -> str:
        if task.state == TaskState.OPEN.value:
            return "open"
        if task.state in {
            TaskState.CLAIMED.value,
            TaskState.RUNNING.value,
            TaskState.NEEDS_REVIEW.value,
            TaskState.REVIEWING.value,
        }:
            self._sync_beads_claim(task, task.owner_agent_id or actor)
            return "active_claim_synced"
        if task.state == TaskState.FAILED.value:
            result = self._reopen_failed_beads_task(task, actor, issue=issue)
            if result == "retry_exhausted":
                self._append_beads_failure_summary_if_needed(
                    self.get_task(task.id),
                    actor,
                    event="retry_exhausted_summary",
                )
            return result
        if task.state in TERMINAL_TASK_STATES:
            return "terminal_ignored"
        return "inactive_ignored"

    def _reopen_failed_beads_task(
        self,
        task: Task,
        actor: str,
        *,
        issue: Optional[JsonDict] = None,
    ) -> str:
        metadata = ensure_json_object(task.metadata)
        reconciliation = ensure_json_object(metadata.get("beads_reconciliation"))
        retry_count = int(reconciliation.get("failed_task_reopen_count") or 0)
        retry_limit = self._beads_failed_task_reopen_limit()
        origin = metadata.get("origin") if isinstance(metadata.get("origin"), dict) else {}
        acc_metadata = (
            metadata.get("acc_metadata") if isinstance(metadata.get("acc_metadata"), dict) else {}
        )
        bead_id = str(
            (issue or {}).get("id")
            or origin.get("bead_id")
            or acc_metadata.get("beads_id")
            or ""
        )
        if retry_count >= retry_limit:
            if not self._mark_failed_beads_task_retry_exhausted(
                task,
                actor,
                metadata,
                reconciliation,
                retry_count,
                retry_limit,
                bead_id,
            ):
                return "race_lost"
            return "retry_exhausted"

        now = utcnow()
        next_count = retry_count + 1
        new_max_attempts = max(int(task.max_attempts), int(task.attempt_count) + 1)
        reconciliation.update(
            {
                "schema": "mac.beads_reconciliation.v1",
                "failed_task_reopen_count": next_count,
                "failed_task_reopen_limit": retry_limit,
                "last_reopened_at": now,
                "last_reopened_by": actor,
                "last_reopened_bead_id": bead_id,
                "last_reopen_reason": "bead_still_ready",
            }
        )
        reconciliation.pop("retry_exhausted_at", None)
        metadata["beads_reconciliation"] = reconciliation
        detail = {
            "reason": "bead_still_ready",
            "bead_id": bead_id,
            "failed_task_reopen_count": next_count,
            "failed_task_reopen_limit": retry_limit,
            "attempt_count": task.attempt_count,
            "previous_max_attempts": task.max_attempts,
            "max_attempts": new_max_attempts,
        }
        with self.store.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET state = ?, owner_agent_id = NULL, lease_id = NULL, leased_until = NULL,
                    max_attempts = ?, completed_at = NULL, metadata = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    TaskState.OPEN.value,
                    new_max_attempts,
                    json_dumps(metadata),
                    now,
                    task.id,
                    TaskState.FAILED.value,
                ),
            )
            if cursor.rowcount != 1:
                return "race_lost"
            self._record_history(
                task.id,
                "task.transitioned",
                actor,
                TaskState.FAILED.value,
                TaskState.OPEN.value,
                detail,
                conn=conn,
            )
        self.record_log(
            "bridge.beads.reopened_failed_task",
            layer="control_plane",
            source=actor,
            subject_type="task",
            subject_id=task.id,
            detail=detail,
        )
        self._append_beads_ledger_comment(
            self.get_task(task.id),
            actor,
            "retry_reopened",
            "failed mac task reopened because bead is still ready",
            fields=detail,
        )
        return "reopened"

    def _mark_failed_beads_task_retry_exhausted(
        self,
        task: Task,
        actor: str,
        metadata: JsonDict,
        reconciliation: JsonDict,
        retry_count: int,
        retry_limit: int,
        bead_id: str,
    ) -> bool:
        if reconciliation.get("retry_exhausted_at"):
            return True
        now = utcnow()
        reconciliation.update(
            {
                "schema": "mac.beads_reconciliation.v1",
                "failed_task_reopen_count": retry_count,
                "failed_task_reopen_limit": retry_limit,
                "retry_exhausted_at": now,
                "retry_exhausted_by": actor,
                "retry_exhausted_bead_id": bead_id,
            }
        )
        metadata["beads_reconciliation"] = reconciliation
        detail = {
            "reason": "beads_failed_task_retry_limit",
            "bead_id": bead_id,
            "failed_task_reopen_count": retry_count,
            "failed_task_reopen_limit": retry_limit,
        }
        detail.update(self._latest_failure_context(task))
        with self.store.transaction() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ? AND state = ?",
                (json_dumps(metadata), now, task.id, TaskState.FAILED.value),
            )
            if cursor.rowcount != 1:
                return False
            self._record_history(
                task.id,
                "task.beads_retry_exhausted",
                actor,
                TaskState.FAILED.value,
                TaskState.FAILED.value,
                detail,
                conn=conn,
            )
        self.record_log(
            "bridge.beads.failed_task_retry_exhausted",
            layer="control_plane",
            source=actor,
            level="warning",
            subject_type="task",
            subject_id=task.id,
            detail=detail,
        )
        self._append_beads_ledger_comment(
            self.get_task(task.id),
            actor,
            "retry_exhausted",
            "failed mac task exhausted automatic Beads retries",
            fields=detail,
        )
        self._append_beads_failure_summary_if_needed(
            self.get_task(task.id),
            actor,
            detail=detail,
            event="retry_exhausted_summary",
        )
        return True

    def _beads_failed_task_reopen_limit(self) -> int:
        raw = os.environ.get("MAC_BEADS_FAILED_TASK_REOPEN_LIMIT", "3")
        try:
            return max(0, int(raw))
        except ValueError:
            return 3

    def _sync_beads_close(self, task: Task, actor: str) -> None:
        binding = self._beads_binding_for_task(task)
        if binding is None:
            return
        acc_metadata = task.metadata.get("acc_metadata") if isinstance(task.metadata, dict) else {}
        if isinstance(acc_metadata, dict) and acc_metadata.get("beads_sync_close_on_complete") is False:
            return
        ok = self._run_bd_for_task(
            task,
            [
                "close",
                binding["bead_id"],
                "--reason",
                "Completed by mac task %s" % task.id,
            ],
            actor,
            "close",
        )
        # mac-4p2f: bd close used to be fire-and-forget — if it failed,
        # mac recorded COMPLETED while the bead stayed open, with no
        # reconciliation path. Record a task_history row that flags the
        # pending close so the periodic Beads heartbeat poll can pick
        # it up and retry. The reconciler keys on this event_type and
        # the binding bead_id; if the bd close already happened (e.g.
        # mid-retry race) the dedupe in _append_beads_ledger_comment +
        # bd close's natural idempotency keep retries safe.
        if not ok:
            try:
                self._record_history(
                    task.id,
                    "task.beads_close_pending",
                    actor,
                    None,
                    None,
                    {"bead_id": binding["bead_id"], "reason": "initial close did not succeed"},
                )
            except Exception:  # noqa: BLE001 - audit insert failure is non-fatal
                pass

    def _sync_beads_reopen(
        self,
        task: Task,
        actor: str,
        state: str,
        detail: Dict[str, Any],
    ) -> None:
        binding = self._beads_binding_for_task(task)
        if binding is None:
            return
        reason = "mac task %s moved to %s: %s" % (
            task.id,
            state,
            json_dumps(detail or {}),
        )
        self._run_bd_for_task(
            task,
            ["update", binding["bead_id"], "--status", "open", "--append-notes", reason],
            actor,
            "reopen",
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

    def get_memory(self, memory_id: str) -> MemoryRecord:
        return self.memory.get_memory(memory_id)

    def search_memory(self, *args: Any, **kwargs: Any) -> List[MemoryRecord]:
        return self.memory.search_memory(*args, **kwargs)

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
        return Lease(row["id"], row["task_id"], row["agent_id"], row["expires_at"], row["status"], row["created_at"], row["updated_at"])

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
        return Fleet(
            row["id"],
            row["name"],
            row["description"],
            row["status"],
            json_loads(row["metadata"], {}),
            row["tenant_id"],
            [member["agent_id"] for member in member_rows],
            row["created_at"],
            row["updated_at"],
        )

    def _agent_from_row(self, row: Any) -> Agent:
        keys = row.keys() if hasattr(row, "keys") else []
        running_digest = row["running_digest"] if "running_digest" in keys else None
        role_id = row["role_id"] if "role_id" in keys else None
        hermes_instance_id = (
            row["hermes_instance_id"] if "hermes_instance_id" in keys else None
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

    def _beads_repository_from_row(self, row: Any) -> BeadsRepository:
        return BeadsRepository(
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

    def _unblock_ready_tasks(self) -> None:
        for task in self.list_tasks(TaskState.BLOCKED.value):
            if self._dependencies_satisfied(task):
                self.transition_task(task.id, TaskState.OPEN.value, "dispatcher", {"reason": "dependencies satisfied"})

    def _dispatch_ordered_tasks(self) -> List[Task]:
        groups: Dict[str, List[Task]] = {}
        for task in self.list_tasks(TaskState.OPEN.value):
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
        }

    def _task_matches_worker_claim_policy(self, task: Task, policy: JsonDict) -> Tuple[bool, str]:
        allowed_projects = set(policy.get("allowed_projects") or [])
        if allowed_projects and (task.project or "") not in allowed_projects:
            return False, "project_not_allowed"
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

    def _agent_available_for(self, agent: Agent, task: Task) -> bool:
        if agent.status not in {AgentStatus.IDLE.value, AgentStatus.BUSY.value}:
            return False
        if agent.health_status != HealthStatus.HEALTHY.value:
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
        machine = self.get_machine(agent.machine_id)
        if not machine.trusted:
            return False
        if self._agent_active_lease_count(agent.id) >= self._agent_capacity(agent):
            return False
        if not self._machine_allows_tenant(machine, self._task_tenant_id(task)):
            return False
        if not self._agent_resources_satisfy(agent, machine, task):
            return False
        # Role + hardware gates. Both no-op when neither the agent nor the
        # task carry role/hardware metadata, so the legacy capability path
        # below stays the dominant matcher for un-roled fleets.
        required_role = task.metadata.get("required_role") if isinstance(task.metadata, dict) else None
        if required_role:
            if agent.role_id is None:
                return False
            try:
                role = self.roles.get_role(agent.role_id)
            except NotFoundError:
                return False
            if role.slug != required_role:
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

    def _repo_verification_problems(self, manifest: JsonDict, require_tests: bool) -> List[str]:
        problems = self._require_pushed_repo_anchor(manifest)
        repo = manifest.get("repo") if isinstance(manifest.get("repo"), dict) else {}
        files_changed = _manifest_list(repo.get("files_changed")) if isinstance(repo, dict) else []
        if not files_changed:
            problems.append("repo evidence requires changed files")
        if require_tests and self._passed_verification_check_count(manifest) < 1:
            problems.append("repo code evidence requires at least one passing test/check")
        return problems

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
            if verdict == "rejected":
                return evidence, []
            digest = str(manifest.get("worktree_digest") or "").strip()
            if not re.match(r"^sha256:[0-9a-f]{64}$", digest):
                problems.append("verdict %s requires worktree_digest sha256" % evidence.id)
                continue
            executor_repo = executor_manifest.get("repo")
            if isinstance(executor_repo, dict):
                repo_problems = self._require_pushed_repo_anchor(manifest)
                if repo_problems:
                    problems.extend("verdict %s %s" % (evidence.id, problem) for problem in repo_problems)
                    continue
                reviewed_sha = str((manifest.get("repo") or {}).get("head_sha") or "").strip()
                executor_sha = str(executor_repo.get("head_sha") or "").strip()
                if reviewed_sha != executor_sha:
                    problems.append(
                        "verdict %s repo.head_sha does not match executor evidence: %s != %s"
                        % (evidence.id, reviewed_sha, executor_sha)
                    )
                    continue
            if self._passed_verification_check_count(manifest) < 1:
                problems.append("verdict %s requires at least one independent passing check" % evidence.id)
                continue
            return evidence, []
        return None, problems

    def _verdict_value(self, evidence: Evidence) -> str:
        manifest = evidence.metadata.get("verification") or {}
        verdict = str(manifest.get("verdict") or "").strip().lower()
        return verdict if verdict in {"approved", "rejected"} else "approved"

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
        self._append_beads_ledger_comment(
            self.get_task(review.task_id),
            actor,
            "review_retracted",
            "review retracted",
            fields={
                "review": review.id,
                "reviewer": review.reviewer_agent_id,
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
        * Not the executor (existing): the latest evidence author cannot
          review their own work. Prior owners from earlier rejected
          attempts may review a newer agent's evidence so small fleets do
          not deadlock after retries.
        """
        task_tenant = self._task_tenant_id(task)
        executor_persona_slug = self._task_executor_persona_slug(task)
        review_policy = self._default_review_policy(task)
        review_required_capabilities = self._default_review_required_capabilities(
            task,
            review_policy,
        )

        candidates: List[Agent] = []
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
        if not candidates:
            return None
        candidates.sort(
            key=lambda agent: (
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
        if agent.health_status != HealthStatus.HEALTHY.value:
            return "reviewer_unhealthy"
        if agent.status not in {AgentStatus.IDLE.value, AgentStatus.BUSY.value}:
            return "reviewer_not_available"
        if not self._agent_seen_recently(agent, self._default_reviewer_stale_after_seconds()):
            return "reviewer_stale"
        if task.owner_agent_id == agent.id:
            return "reviewer_owned_task"
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
        if task_tenant is not None and (agent_tenant is None or agent_tenant != task_tenant):
            return "reviewer_wrong_tenant"
        if (
            executor_persona_slug is not None
            and agent_persona_slug is not None
            and agent_persona_slug == executor_persona_slug
        ):
            return "reviewer_same_persona"
        return None

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
                self.task_ledger.enqueue_outbox(
                    conn,
                    task_id=row["task_id"],
                    event_type="beads.ledger",
                    actor="dispatcher",
                    from_state=row["task_state"],
                    to_state=next_state,
                    detail=detail,
                    created_at=timestamp,
                )
        for row in rows:
            self.drain_task_transition_outbox(task_id=row["task_id"], limit=20)
