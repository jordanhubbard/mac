from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional


JsonDict = Dict[str, Any]


class MACError(Exception):
    """Base exception for recoverable control-plane errors."""


class NotFoundError(MACError):
    """Raised when a requested durable object does not exist."""


class AmbiguousIdError(MACError):
    """Raised when a task-id prefix matches more than one task.

    ``candidates`` is a list of full task ids that share the prefix.
    """

    def __init__(self, message: str, candidates: List[str]) -> None:
        super().__init__(message)
        self.candidates = candidates


class ValidationError(MACError):
    """Raised when user or agent input violates a contract."""


class TransitionError(MACError):
    """Raised when a state transition is not allowed."""


class AuthorizationError(MACError):
    """Raised when an agent lacks explicit authority."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex)


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def json_loads(value: Optional[str], default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def coerce_list(value: Optional[Iterable[str]]) -> List[str]:
    return sorted({item for item in (value or []) if item})


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class TaskState(StrEnum):
    OPEN = "open"
    WAITING = "waiting"
    BLOCKED = "blocked"
    CLAIMED = "claimed"
    RUNNING = "running"
    NEEDS_REVIEW = "needs_review"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATES = {
    TaskState.COMPLETED.value,
    TaskState.FAILED.value,
    TaskState.CANCELLED.value,
}


# Task deliverable kind (metadata["deliverable"]). The default, "code", expects
# a repository change and drives the strict repo-coupled evidence contract
# (repo_change / test / no_change with a pushed anchor). A "report" task is
# explicitly non-code — investigation, triage, an answer, a status summary —
# and is satisfied by an ``operator_result`` (substantive summary/findings/
# artifacts, no diff, no pushed branch). This is an operator/workflow-author
# declaration at creation, NOT something an executing agent can set for itself,
# so it cannot be used to bypass the substance gate the way the task_d7c51a0b
# incident did (an executor emitting operator_result for an implicit code task).
REPORT_DELIVERABLE = "report"
_REPORT_DELIVERABLE_ALIASES = frozenset(
    {"report", "answer", "analysis", "investigation", "question", "triage"}
)


def normalize_deliverable_kind(value: Any) -> str:
    """Canonicalize a deliverable-kind token: aliases -> "report"; blank/"code"
    -> "" (the default code path). Unknown values pass through unchanged so a
    future kind is not silently swallowed."""
    text = str(value or "").strip().lower()
    if not text or text == "code":
        return ""
    if text in _REPORT_DELIVERABLE_ALIASES:
        return REPORT_DELIVERABLE
    return text


def metadata_declares_report_deliverable(metadata: Any) -> bool:
    """True when task metadata declares a non-code (report/answer) deliverable.

    The single predicate every repo-coupling check consults so a declared
    report task is uniformly exempt from code-substance expectations."""
    if not isinstance(metadata, Mapping):
        return False
    return normalize_deliverable_kind(metadata.get("deliverable")) == REPORT_DELIVERABLE


TASK_TRANSITIONS = {
    TaskState.OPEN.value: {
        TaskState.WAITING.value,
        TaskState.BLOCKED.value,
        TaskState.CLAIMED.value,
        TaskState.CANCELLED.value,
        TaskState.FAILED.value,
    },
    TaskState.WAITING.value: {
        TaskState.OPEN.value,
        TaskState.BLOCKED.value,
        TaskState.CANCELLED.value,
        TaskState.FAILED.value,
    },
    TaskState.BLOCKED.value: {
        TaskState.OPEN.value,
        TaskState.WAITING.value,
        TaskState.CANCELLED.value,
        TaskState.FAILED.value,
    },
    TaskState.CLAIMED.value: {
        TaskState.WAITING.value,
        TaskState.BLOCKED.value,
        TaskState.OPEN.value,
        TaskState.RUNNING.value,
        TaskState.FAILED.value,
        TaskState.CANCELLED.value,
    },
    TaskState.RUNNING.value: {
        TaskState.WAITING.value,
        TaskState.BLOCKED.value,
        TaskState.NEEDS_REVIEW.value,
        TaskState.OPEN.value,
        TaskState.FAILED.value,
        TaskState.CANCELLED.value,
    },
    TaskState.NEEDS_REVIEW.value: {
        TaskState.WAITING.value,
        TaskState.BLOCKED.value,
        TaskState.REVIEWING.value,
        TaskState.RUNNING.value,
        TaskState.FAILED.value,
        TaskState.CANCELLED.value,
    },
    TaskState.REVIEWING.value: {
        TaskState.WAITING.value,
        TaskState.BLOCKED.value,
        TaskState.OPEN.value,
        TaskState.RUNNING.value,
        TaskState.COMPLETED.value,
        TaskState.FAILED.value,
        TaskState.CANCELLED.value,
    },
    TaskState.COMPLETED.value: set(),
    TaskState.FAILED.value: {TaskState.OPEN.value},
    TaskState.CANCELLED.value: {TaskState.OPEN.value},
}


class AgentStatus(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"
    DRAINING = "draining"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class LeaseStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    RELEASED = "released"
    RENEWED = "renewed"


class ServiceClaimStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    RELEASED = "released"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"
    RETRACTED = "retracted"


class PublicationStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    RETRACTED = "retracted"
    FAILED = "failed"


class RuntimeRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RuntimeDeltaStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    REJECTED = "rejected"
    PROMOTED = "promoted"


class DeploymentStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"
    FAILED = "failed"


class MoodMode(StrEnum):
    """Agent-self-reported emotional state.

    Agents pick their own mood based on local signals (recent task outcomes,
    retry counts, review rejections). The control plane records and audits
    transitions; it does not derive mood from observations on behalf of an
    agent. Operators read via GET /agents/{id}/mood; agents set via POST.
    """

    WARM = "warm"
    CHEERFUL = "cheerful"
    SAD = "sad"
    CURT = "curt"
    COLD = "cold"
    IRRITATED = "irritated"
    ANGRY = "angry"
    ENRAGED = "enraged"


MOOD_MODES: frozenset = frozenset(m.value for m in MoodMode)


class NapStatus(StrEnum):
    """Lifecycle states for one nap_run."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Nap offset is computed deterministically from the agent's name so the fleet
# spreads itself across each hourly cycle (md5_u64(name) %% 60 minutes after
# the top of the hour).
NAP_WINDOW_MINUTES = 60
NAP_DEFAULT_DURATION_MINUTES = 15


EVIDENCE_KINDS = {"test", "review", "artifact", "publication", "log", "eval"}


class EvalScoringDirection(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class EvalTargetKind(StrEnum):
    ROLLOUT_VERSION = "rollout_version"
    RUNTIME_ENVIRONMENT = "runtime_environment"
    AGENT_BUILD = "agent_build"


class MessageType(StrEnum):
    HELP_REQUEST = "help_request"
    EVIDENCE_REQUEST = "evidence_request"
    STATUS_UPDATE = "status_update"
    REVIEW_REQUEST = "review_request"
    REVIEW_RESULT = "review_result"
    NUDGE = "nudge"
    DECISION_RECORD = "decision_record"


class MessageStatus(StrEnum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    REJECTED = "rejected"


class AgentBusStreamStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    ABORTED = "aborted"


OBSERVABILITY_KINDS = {"metric", "log"}
OBSERVABILITY_LEVELS = {"debug", "info", "warning", "error", "critical"}

ACTION_EVENT_OUTCOMES = {
    "unknown",
    "started",
    "success",
    "failure",
    "denied",
    "allowed",
    "skipped",
}


class SecretAuditResult(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"
    ROTATED = "rotated"


class RolloutStrategy(StrEnum):
    CANARY = "canary"
    FULL = "full"
    RESCUE = "rescue"


class RolloutStatus(StrEnum):
    PLANNED = "planned"
    CANARYING = "canarying"
    PROMOTED = "promoted"
    PAUSED = "paused"
    RESCUING = "rescuing"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


ROLLOUT_ACTIONS = {
    "start_canary": {
        "from": {RolloutStatus.PLANNED.value},
        "to": RolloutStatus.CANARYING.value,
    },
    "promote": {
        "from": {RolloutStatus.PLANNED.value, RolloutStatus.CANARYING.value, RolloutStatus.PAUSED.value},
        "to": RolloutStatus.PROMOTED.value,
        "target_percent": 100,
    },
    "pause": {
        "from": {RolloutStatus.PLANNED.value, RolloutStatus.CANARYING.value},
        "to": RolloutStatus.PAUSED.value,
    },
    "resume": {
        "from": {RolloutStatus.PAUSED.value},
        "to": RolloutStatus.CANARYING.value,
    },
    "rollback": {
        "from": {
            RolloutStatus.CANARYING.value,
            RolloutStatus.PAUSED.value,
            RolloutStatus.PROMOTED.value,
            RolloutStatus.RESCUING.value,
        },
        "to": RolloutStatus.ROLLED_BACK.value,
        "target_percent": 0,
    },
    # mac-24f4: a successful rescue had no exit — RESCUING was a
    # one-way trap that only allowed rollback. ``complete_rescue``
    # returns the rollout to PAUSED so an operator can re-evaluate
    # health, decide whether to resume the canary or roll back, and
    # the rescue task closure can hook into a clean transition.
    "complete_rescue": {
        "from": {RolloutStatus.RESCUING.value},
        "to": RolloutStatus.PAUSED.value,
    },
}


class HermesInstanceStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


@dataclass
class Tenant:
    id: str
    name: str
    metadata: JsonDict
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class User:
    id: str
    tenant_id: str
    handle: str
    display_name: str
    metadata: JsonDict
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Persona:
    id: str
    tenant_id: str
    name: str
    soul_ref: str
    memory_scope: str
    metadata: JsonDict
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class HermesInstance:
    id: str
    tenant_id: str
    name: str
    persona_id: Optional[str]
    home_ref: str
    status: str
    metadata: JsonDict
    created_at: str
    updated_at: str
    last_seen_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class PlatformBinding:
    id: str
    tenant_id: str
    hermes_instance_id: str
    platform: str
    external_id: str
    display_name: str
    scopes: JsonDict
    metadata: JsonDict
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Task:
    id: str
    title: str
    description: str
    project: Optional[str]
    priority: int
    state: str
    required_capabilities: List[str]
    dependencies: List[str]
    metadata: JsonDict
    owner_agent_id: Optional[str]
    lease_id: Optional[str]
    leased_until: Optional[str]
    attempt_count: int
    max_attempts: int
    started_at: Optional[str]
    completed_at: Optional[str]
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        data["last_updated_at"] = self.updated_at
        return data


@dataclass
class BreakGlassAuthorization:
    """Single-task authorization to execute directly on one trusted host.

    The control plane, not task metadata, owns these records.  A task may carry
    a transient copy in its assignment payload only after an ACTIVE record has
    been atomically bound to the task's exact lease and agent.
    """

    id: str
    task_id: str
    agent_id: str
    execution_boundary: str
    reason: str
    authorized_by: str
    status: str
    metadata: JsonDict
    created_at: str
    expires_at: str
    claimed_at: Optional[str]
    lease_id: Optional[str]
    consumed_at: Optional[str]
    revoked_at: Optional[str]
    revoked_by: Optional[str]
    revoke_reason: Optional[str]

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class HistoryEvent:
    id: str
    task_id: str
    event_type: str
    actor: str
    from_state: Optional[str]
    to_state: Optional[str]
    detail: JsonDict
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class TaskTransitionOutbox:
    id: str
    task_id: str
    event_type: str
    actor: str
    from_state: Optional[str]
    to_state: Optional[str]
    detail: JsonDict
    status: str
    attempts: int
    created_at: str
    processed_at: Optional[str]

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Evidence:
    id: str
    task_id: str
    kind: str
    uri: str
    summary: str
    checksum: Optional[str]
    metadata: JsonDict
    created_by: str
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class EvidenceArtifact:
    id: str
    evidence_id: str
    task_id: str
    name: str
    artifact_type: str
    source_uri: str
    content_type: str
    encoding: str
    size_bytes: int
    sha256: str
    content_base64: Optional[str]
    truncated: bool
    metadata: JsonDict
    created_at: str
    # When artifact bytes are externalized to the hub blob store, this holds
    # the blob URI and content_base64 is empty; readers materialize content
    # through the blob store (see mac.evidence_blobs).
    content_uri: str = ""

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Lease:
    id: str
    task_id: str
    agent_id: str
    expires_at: str
    status: str
    created_at: str
    updated_at: str
    # PR2c (spec §6.3, Option B): when set, the named agent is allowed to
    # author task lifecycle transitions (start/submit_for_review) and
    # evidence on the lease's task. The lease owner (``agent_id``) still
    # owns renewal and release.
    delegated_agent_id: Optional[str] = None

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class ServiceRole:
    """A media service the cluster wants held by some capable host (media-01
    role-claims). The op is served by a catalog model; required_capabilities +
    hardware_requirements gate which agents are eligible to claim it."""
    id: str
    op: str
    slug: str
    model_id: Optional[str]
    required_capabilities: List[str]
    hardware_requirements: JsonDict
    enabled: bool
    tenant_id: Optional[str]
    metadata: JsonDict
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class ServiceClaim:
    """A capable host's leased hold on a service_role (mirrors Lease). Renewed by
    the holder's worker loop; expires on silence/overload; reopened for reclaim."""
    id: str
    service_role_id: str
    agent_id: str
    status: str
    expires_at: str
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Machine:
    id: str
    hostname: str
    labels: JsonDict
    resources: JsonDict
    trusted: bool
    created_at: str
    updated_at: str
    last_seen_at: str
    hardware: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Fleet:
    id: str
    name: str
    description: str
    status: str
    metadata: JsonDict
    tenant_id: Optional[str]
    agent_ids: List[str]
    created_at: str
    updated_at: str
    observed_agent_ids: List[str] = field(default_factory=list)
    unmanaged_agent_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Agent:
    id: str
    machine_id: str
    name: str
    capabilities: List[str]
    resources: JsonDict
    status: str
    health_status: str
    current_task_id: Optional[str]
    running_digest: Optional[str]
    created_at: str
    updated_at: str
    last_seen_at: str
    role_id: Optional[str] = None
    hermes_instance_id: Optional[str] = None
    # Packages the agent has self-installed into its own environment (pip/npm),
    # reported to the hub as its "default footprint" so redeploys re-hydrate it.
    installed_packages: JsonDict = field(default_factory=dict)
    # Dispatch hold: operator-set quarantine preventing new task dispatch.
    dispatch_hold: bool = False
    dispatch_hold_reason: Optional[str] = None
    dispatch_hold_at: Optional[str] = None
    # Zombie-detection counter: incremented when a lease expires with no
    # telemetry from the agent; reset to 0 on any successful heartbeat.
    consecutive_lease_expiries_no_telemetry: int = 0
    # Control-stream health timestamps for zombie detection.
    last_control_stream_published_at: Optional[str] = None
    last_control_stream_consumed_at: Optional[str] = None
    # Tombstone: set when the agent is decommissioned. The row is kept so
    # AgentBus streams, events, and delivery history survive with their real
    # identities (ephemeral agents' results must outlive the agent); liveness
    # operations (heartbeat, claims, publishing) refuse tombstoned agents.
    deleted_at: Optional[str] = None

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class AgentRole:
    """Persona template assignable to an agent.

    Roles bundle a system prompt, capability defaults, and optional
    hardware requirements. An agent's ``role_id`` points at one of these
    rows; capabilities the role declares as ``required`` are stacked onto
    the agent's effective requirement set at dispatch time. Hardware
    requirements gate role assignment and dispatch.
    """

    id: str
    slug: str
    name: str
    display_name: Optional[str]
    description: str
    system_prompt: str
    level: str
    reports_to: Optional[str]
    specialties: List[str]
    default_capabilities: List[str]
    required_capabilities: List[str]
    hardware_requirements: JsonDict
    metadata: JsonDict
    is_default: bool
    tenant_id: Optional[str]
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


class RoleLevel(StrEnum):
    EXEC = "exec"
    MANAGER = "manager"
    STAFF = "staff"
    IC = "ic"
    BOT = "bot"


ROLE_LEVELS = {value.value for value in RoleLevel}


@dataclass
class AgentProvisioningRequest:
    """Signal that the swarm needs an agent it doesn't have.

    Emitted by the dispatcher and the default-review workflow when no
    eligible agent can be selected for a task. A future provisioner (k8s
    operator, nomad job, local spawner) polls these rows and fulfills
    them by registering the requested agent. For now the actual
    provisioning is unimplemented — requests sit in ``pending`` until an
    operator hand-fulfills or cancels them, and the observability log
    plus this table are the signal.
    """

    id: str
    status: str
    reason: str
    role_slug: Optional[str]
    capabilities: List[str]
    hardware: JsonDict
    task_id: Optional[str]
    tenant_id: Optional[str]
    detail: JsonDict
    fulfilled_agent_id: Optional[str]
    created_at: str
    updated_at: str
    closed_at: Optional[str]
    # mac-1oi4: who requested this agent, so fulfill_request can enforce
    # a two-party check (the same actor cannot both ask and approve).
    requested_by: Optional[str] = None

    def to_dict(self) -> JsonDict:
        return asdict(self)


class ProvisioningStatus(StrEnum):
    PENDING = "pending"
    FULFILLED = "fulfilled"
    FAILED = "failed"
    CANCELLED = "cancelled"


PROVISIONING_TERMINAL_STATES = {
    ProvisioningStatus.FULFILLED.value,
    ProvisioningStatus.FAILED.value,
    ProvisioningStatus.CANCELLED.value,
}


@dataclass
class Workflow:
    """Versioned, data-driven workflow definition.

    Workflows are DAGs of typed nodes (each with a required role) and
    edges that match on terminal conditions. Definitions are immutable
    per ``version`` — updating ``definition`` bumps the version so
    in-flight runs (which snapshot the definition at start) keep their
    deterministic shape.
    """

    id: str
    slug: str
    name: str
    description: str
    workflow_type: str
    is_default: bool
    version: int
    definition: JsonDict
    tenant_id: Optional[str]
    enabled: bool
    metadata: JsonDict
    created_by: str
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class WorkflowRun:
    """One execution of a workflow.

    ``definition_snapshot`` is captured at start time so updates to the
    parent workflow don't surprise an in-flight run. ``context`` is a
    free-form bag that accumulates per-node output for later nodes to
    consume.
    """

    id: str
    workflow_id: str
    workflow_version: int
    definition_snapshot: JsonDict
    state: str
    current_node_key: Optional[str]
    current_task_id: Optional[str]
    input: JsonDict
    context: JsonDict
    tenant_id: Optional[str]
    started_by: str
    created_at: str
    updated_at: str
    next_action_at: Optional[str]
    completed_at: Optional[str]

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class WorkflowDraft:
    id: str
    tenant_id: Optional[str]
    goal: str
    status: str
    proposed_steps: List[JsonDict]
    questions: List[JsonDict]
    answers: JsonDict
    edit_history: List[JsonDict]
    compiled_workflow_id: Optional[str]
    created_by: str
    created_at: str
    updated_at: str
    approved_at: Optional[str]

    def to_dict(self) -> JsonDict:
        return asdict(self)


class WorkflowState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ESCALATED = "escalated"


class NodeType(StrEnum):
    TASK = "task"
    APPROVAL = "approval"
    COMMIT = "commit"
    VERIFY = "verify"
    # wf-04: a `plan` node runs an agent task that translates the
    # workflow's free-form input description into structured payloads
    # for downstream nodes. The task's evidence carries
    # ``metadata.plan_payloads = { <node_key>: { instructions?, metadata? } }``;
    # the runtime injects each payload into the matching downstream
    # node before its task spawns.
    PLAN = "plan"


class EdgeCondition(StrEnum):
    SUCCESS = "success"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


WORKFLOW_TERMINAL_STATES = {
    WorkflowState.COMPLETED.value,
    WorkflowState.FAILED.value,
    WorkflowState.CANCELLED.value,
}


@dataclass
class AgentMessage:
    id: str
    sender_agent_id: str
    recipient_agent_id: Optional[str]
    task_id: Optional[str]
    message_type: str
    payload: JsonDict
    status: str
    created_at: str
    delivered_at: Optional[str]

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class AgentBusStream:
    id: str
    sender_agent_id: str
    recipient_agent_id: Optional[str]
    task_id: Optional[str]
    topic: str
    content_type: str
    headers: JsonDict
    status: str
    created_at: str
    updated_at: str
    closed_at: Optional[str]
    # Group streams (task_588b67fd): when set, this is the full member list
    # (opener included) — membership governs authorization, any member may
    # append, and one conversation lives in one stream. None preserves the
    # legacy sender/recipient pair semantics byte-for-byte.
    participants: Optional[List[str]] = None

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class AgentBusChunk:
    id: str
    stream_id: str
    sequence: int
    sender_agent_id: str
    content_type: str
    payload: Any
    payload_encoding: str
    size_bytes: int
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class ObservabilityEvent:
    sequence: int
    id: str
    kind: str
    layer: str
    source: str
    level: str
    name: str
    subject_type: Optional[str]
    subject_id: Optional[str]
    value: Optional[float]
    unit: str
    detail: JsonDict
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class OperatorNotification:
    id: str
    event_type: str
    subject_type: Optional[str]
    subject_id: Optional[str]
    title: str
    body: str
    channels: List[str]
    metadata: JsonDict
    status: str
    created_at: str
    delivered_at: Optional[str]

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class NotifierChannel:
    id: str
    name: str
    channel_type: str
    enabled: bool
    event_types: List[str]
    target: JsonDict
    metadata: JsonDict
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class CommunicationIdentity:
    """Stable human-facing identity independent of any worker or host."""

    id: str
    name: str
    display_name: str
    description: str
    is_default: bool
    enabled: bool
    metadata: JsonDict
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class CommunicationAccount:
    """One provider account owned by a logical communication identity."""

    id: str
    identity_id: str
    channel: str
    account_id: str
    credential_refs: JsonDict
    config: JsonDict
    enabled: bool
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class RepresentationBinding:
    """Map an internal subject to a direct, delegated, or silent identity."""

    id: str
    subject_kind: str
    subject_id: str
    identity_id: Optional[str]
    mode: str
    priority: int
    enabled: bool
    metadata: JsonDict
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class GatewayIdentityLease:
    """Fenced singleton ownership of a channel account by one fleet agent."""

    id: str
    account_id: str
    agent_id: str
    fencing_token: str
    leased_until: str
    metadata: JsonDict
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class HumanMessageDelivery:
    """Durable, idempotent request for a public OpenClaw delivery."""

    id: str
    identity_id: str
    account_id: Optional[str]
    channel: Optional[str]
    target: str
    body: str
    origin_agent_id: Optional[str]
    task_id: Optional[str]
    idempotency_key: str
    status: str
    attempt_count: int
    max_attempts: int
    delivery_agent_id: Optional[str]
    delivery_lease_id: Optional[str]
    leased_until: Optional[str]
    provider_message_id: Optional[str]
    last_error: Optional[str]
    metadata: JsonDict
    created_at: str
    updated_at: str
    delivered_at: Optional[str]

    def to_dict(self) -> JsonDict:
        return asdict(self)


COMMAND_AUDIT_PHASES = {
    "started",
    "completed",
    "failed",
    "timeout",
    "error",
}


@dataclass
class CommandAuditRecord:
    id: str
    command_id: str
    agent_id: str
    phase: str
    argv: List[str]
    cwd: str
    task_id: Optional[str]
    lease_id: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    duration_ms: Optional[float]
    returncode: Optional[int]
    stdout_sha256: Optional[str]
    stderr_sha256: Optional[str]
    stdout_bytes: Optional[int]
    stderr_bytes: Optional[int]
    metadata: JsonDict
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class OpenShellPolicy:
    id: str
    name: str
    description: str
    policy_text: str
    parsed_metadata: JsonDict
    version: int
    checksum: str
    created_by: str
    updated_by: str
    active: bool
    created_at: str
    updated_at: str
    deleted_at: Optional[str]

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class OpenShellPolicyVersion:
    id: str
    policy_id: str
    version: int
    policy_text: str
    parsed_metadata: JsonDict
    checksum: str
    created_by: str
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class OpenShellPolicyAssignment:
    id: str
    policy_id: str
    policy_version: int
    target_type: str
    target_id: str
    active: bool
    created_by: str
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class OpenShellStatus:
    agent_id: str
    status: str
    required: bool
    active: bool
    sandbox_id: Optional[str]
    policy_id: Optional[str]
    policy_version: Optional[int]
    checksum: Optional[str]
    supervisor_pid: Optional[int]
    detail: JsonDict
    reported_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class ActionEvent:
    event_id: str
    timestamp: str
    agent_id: Optional[str]
    hermes_instance_id: Optional[str]
    task_id: Optional[str]
    session_id: Optional[str]
    sandbox_id: Optional[str]
    actor: str
    action_type: str
    action_name: str
    subject_type: Optional[str]
    subject_id: Optional[str]
    outcome: str
    severity: str
    policy_id: Optional[str]
    policy_version: Optional[int]
    command_id: Optional[str]
    parent_event_id: Optional[str]
    attributes: JsonDict
    redaction_state: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Review:
    id: str
    task_id: str
    reviewer_agent_id: str
    status: str
    reason: Optional[str]
    evidence_id: Optional[str]
    created_at: str
    completed_at: Optional[str]

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Publication:
    id: str
    task_id: str
    target: str
    status: str
    evidence_id: Optional[str]
    content_hash: Optional[str]
    created_by: str
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class SecretRecord:
    id: str
    name: str
    scopes: JsonDict
    created_by: str
    created_at: str
    updated_at: str
    rotated_at: Optional[str]
    enabled: bool

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        data["value"] = "***REDACTED***"
        return data


@dataclass
class SecretAccess:
    id: str
    secret_id: str
    accessor_agent_id: str
    purpose: str
    result: str
    expires_at: Optional[str]
    revealed_at: Optional[str]
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class SecretHandle:
    secret_id: str
    audit_id: str
    handle: str
    granted: bool

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class ConversationThread:
    id: str
    platform_binding_id: str
    external_thread_id: str
    latest_task_id: Optional[str]
    summary: str
    metadata: JsonDict
    first_seen_at: str
    last_seen_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class VectorRef:
    id: str
    memory_id: str
    vector_db: str
    collection: str
    point_id: str
    embedding_model: Optional[str]
    metadata: JsonDict
    created_by: str
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


# mem-06: typed payload + tier enum + collection registry for the
# vector memory tier. The vector writer (mem-07), nap consolidator
# (mem-08), and recall API (mem-09) all build against these. See
# docs/memory-tier-schema.md for the ADR-level rationale.


MAC_MEMORY_PAYLOAD_SCHEMA = "mac.memory.v1"


class MacMemoryTier(StrEnum):
    MEDIUM = "medium"
    LONG = "long"


# Concept → Qdrant collection. Single point of truth so the writer,
# reader, and the install script all agree.
MAC_MEMORY_COLLECTIONS: Dict[str, str] = {
    MacMemoryTier.MEDIUM.value: "mac_memory_medium",
    MacMemoryTier.LONG.value: "mac_memory_long",
}


# Default embedding model (overridable by MAC_MEMORY_EMBEDDING_MODEL +
# MAC_MEMORY_EMBEDDING_DIM at install / runtime; the model name still
# lands on every payload so cross-model recalls are filterable).
MAC_MEMORY_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
MAC_MEMORY_DEFAULT_EMBEDDING_DIM = 1536


@dataclass
class MacVectorPayload:
    """Per-point payload stored alongside the vector in Qdrant.

    Mirrors the schema in docs/memory-tier-schema.md. `to_dict()` is
    what gets sent to Qdrant's `payload` field; `from_dict()` parses
    a hit back into typed form for the recall API.
    """

    tier: str
    subject_type: str
    subject_id: str
    memory_id: str
    summary: str
    created_at: str
    embedded_at: str
    embedding_model: str
    task_id: Optional[str] = None
    project: Optional[str] = None
    agent_id: Optional[str] = None
    tenant_id: Optional[str] = None
    evidence_type: Optional[str] = None
    record_type: Optional[str] = None
    dream_kind: Optional[str] = None
    dream_scope: Optional[str] = None
    dream_confidence: Optional[str] = None
    dream_confidence_score: Optional[float] = None
    tags: List[str] = field(default_factory=list)
    schema: str = MAC_MEMORY_PAYLOAD_SCHEMA

    def to_dict(self) -> JsonDict:
        # Drop None values to keep the Qdrant payload tight. Schema +
        # required fields always pass through.
        data = asdict(self)
        return {k: v for k, v in data.items() if v is not None}

    @classmethod
    def from_dict(cls, raw: JsonDict) -> "MacVectorPayload":
        if not isinstance(raw, dict):
            raise ValidationError("vector payload must be an object")
        if str(raw.get("schema") or "") != MAC_MEMORY_PAYLOAD_SCHEMA:
            raise ValidationError(
                "vector payload schema %r does not match %r"
                % (raw.get("schema"), MAC_MEMORY_PAYLOAD_SCHEMA)
            )
        tier = str(raw.get("tier") or "").strip().lower()
        if tier not in {t.value for t in MacMemoryTier}:
            raise ValidationError("vector payload tier must be one of medium / long")
        required_fields = (
            "subject_type",
            "subject_id",
            "memory_id",
            "summary",
            "created_at",
            "embedded_at",
            "embedding_model",
        )
        for name in required_fields:
            if not raw.get(name):
                raise ValidationError("vector payload missing required field: %s" % name)
        return cls(
            schema=MAC_MEMORY_PAYLOAD_SCHEMA,
            tier=tier,
            subject_type=str(raw["subject_type"]),
            subject_id=str(raw["subject_id"]),
            memory_id=str(raw["memory_id"]),
            summary=str(raw["summary"]),
            created_at=str(raw["created_at"]),
            embedded_at=str(raw["embedded_at"]),
            embedding_model=str(raw["embedding_model"]),
            task_id=str(raw["task_id"]) if raw.get("task_id") else None,
            project=str(raw["project"]) if raw.get("project") else None,
            agent_id=str(raw["agent_id"]) if raw.get("agent_id") else None,
            tenant_id=str(raw["tenant_id"]) if raw.get("tenant_id") else None,
            evidence_type=(
                str(raw["evidence_type"]) if raw.get("evidence_type") else None
            ),
            record_type=str(raw["record_type"]) if raw.get("record_type") else None,
            dream_kind=str(raw["dream_kind"]) if raw.get("dream_kind") else None,
            dream_scope=str(raw["dream_scope"]) if raw.get("dream_scope") else None,
            dream_confidence=(
                str(raw["dream_confidence"]) if raw.get("dream_confidence") else None
            ),
            dream_confidence_score=(
                float(raw["dream_confidence_score"])
                if raw.get("dream_confidence_score") is not None
                else None
            ),
            tags=list(raw.get("tags") or []),
        )


@dataclass
class MoodOverlay:
    """One mood transition. Append-only; current mood is the most recent row
    for an agent where `cleared_at IS NULL AND (expires_at IS NULL OR expires_at > now)`."""

    id: str
    agent_id: str
    mode: str
    reason: Optional[str]
    metadata: JsonDict
    set_by: str
    set_at: str
    expires_at: Optional[str]
    cleared_at: Optional[str]
    cleared_by: Optional[str]
    cleared_reason: Optional[str]

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class NapSchedule:
    """One row per agent. `offset_minutes` is the per-hour window start;
    defaults to a stable hash of agent.name to spread the fleet."""

    agent_id: str
    offset_minutes: int
    window_minutes: int
    enabled: bool
    last_completed_at: Optional[str]
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class NapRun:
    """One execution of an agent's nap. mac records the lifecycle and the link
    to the produced summary evidence; the actual summarization and embedding
    happens off-process (Hermes / worker / Qdrant indexer)."""

    id: str
    agent_id: str
    status: str
    started_at: str
    completed_at: Optional[str]
    summary_evidence_id: Optional[str]
    detail: JsonDict
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Environment:
    id: str
    name: str
    tenant_id: Optional[str]
    channel: str
    promotes_from: Optional[str]
    metadata: JsonDict
    created_by: str
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Deployment:
    id: str
    environment_id: str
    artifact_id: str
    status: str
    deployed_by: str
    deployed_at: str
    retired_at: Optional[str]
    metadata: JsonDict

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Artifact:
    id: str
    kind: str
    digest: str
    uri: str
    sbom_uri: Optional[str]
    signers: List[str]
    metadata: JsonDict
    created_by: str
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class RuntimeEnvironment:
    id: str
    name: str
    manifest: JsonDict
    digest: str
    created_by: str
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class RuntimeEnvironmentDelta:
    id: str
    task_id: str
    agent_id: str
    project: Optional[str]
    base_runtime_id: Optional[str]
    base_runtime_digest: Optional[str]
    package_manager: str
    commands: List[str]
    added_dependencies: List[Any]
    lockfile_path: Optional[str]
    lockfile_digest: Optional[str]
    reason: str
    status: str
    validation: JsonDict
    evidence_id: Optional[str]
    promoted_runtime_environment_id: Optional[str]
    created_at: str
    updated_at: str
    validated_at: Optional[str]
    promoted_at: Optional[str]

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class RuntimeRun:
    id: str
    task_id: str
    agent_id: str
    environment_id: str
    status: str
    evidence_id: Optional[str]
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class ProjectItem:
    id: str
    source: str
    external_id: str
    title: str
    payload: JsonDict
    task_id: str
    status: str
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class ProjectRecord:
    id: str
    name: str
    description: str
    metadata: JsonDict
    status: str
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class ProjectRepository:
    id: str
    name: str
    path: str
    source: str
    project: str
    required_capabilities: List[str]
    enabled: bool
    poll_interval_seconds: int
    last_polled_at: Optional[str]
    last_imported_at: Optional[str]
    last_error: Optional[str]
    metadata: JsonDict
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class IntegrationObservation:
    id: str
    source_id: str
    source_kind: str
    authority: str
    status: str
    fingerprint: Optional[str]
    cursor: Optional[str]
    detail: JsonDict
    observed_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class IntegrationFinding:
    id: str
    source_id: str
    source_kind: str
    finding_type: str
    severity: str
    status: str
    title: str
    detail: JsonDict
    fingerprint: str
    first_seen_at: str
    last_seen_at: str
    resolved_at: Optional[str]
    resolution: Optional[str]

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class MemoryRecord:
    id: str
    task_id: Optional[str]
    subject_type: str
    subject_id: Optional[str]
    record_type: str
    content: str
    evidence_id: Optional[str]
    created_by: str
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Rollout:
    id: str
    version: str
    strategy: str
    status: str
    target_percent: int
    tenant_id: Optional[str]
    channel: str
    runtime_environment_id: Optional[str]
    artifact_uri: Optional[str]
    artifact_hash: Optional[str]
    health_policy: JsonDict
    required_eval_set_id: Optional[str]
    deploy_environment_id: Optional[str]
    created_by: str
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class EvalSet:
    id: str
    name: str
    description: str
    scoring: str
    baseline_score: Optional[float]
    regression_threshold: float
    metadata: JsonDict
    created_by: str
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class EvalRun:
    id: str
    eval_set_id: str
    target_kind: str
    target_id: str
    score: float
    baseline_score: Optional[float]
    delta: Optional[float]
    threshold: float
    passed: bool
    detail: JsonDict
    evidence_id: Optional[str]
    created_by: str
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


def validate_transition(current: str, target: str) -> None:
    allowed = TASK_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise TransitionError("cannot transition task from %s to %s" % (current, target))


# ---------------------------------------------------------------------------
# Source release and fleet desired-source models (mac.source_release.v1 and
# mac.fleet_desired_source.v1). These underpin the source-convergence system:
# SourceRelease records an immutable, reviewed, published commit; FleetDesired
# SourceState records which release a fleet/environment should run next.
# ---------------------------------------------------------------------------

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_NAME_RE = re.compile(r"^refs/heads/")


def _validate_commit_sha(sha: str) -> None:
    """Reject anything that is not a 40-hex character SHA."""
    if not _SHA_RE.match(sha):
        raise ValidationError(
            "commit_sha must be a 40-character lowercase hex string; got %r" % sha
        )


def _reject_branch_ref(canonical_ref: str) -> None:
    """Reject refs/heads/* branch names – only tags and full SHAs are allowed."""
    if _BRANCH_NAME_RE.match(canonical_ref):
        raise ValidationError(
            "canonical_ref must not be a branch name (refs/heads/*); "
            "use a tag ref (refs/tags/*) or the bare SHA. Got %r" % canonical_ref
        )


@dataclass
class SourceRelease:
    """Immutable record of a reviewed and published source commit.

    Schema: mac.source_release.v1
    """

    id: str
    # Repository identity
    repository_id: str
    repository_name: str
    # Secret-free canonical remote (no embedded credentials)
    canonical_remote_url: str
    # Immutable 40-char commit SHA – enforced at construction
    commit_sha: str
    # Canonical ref (tag or bare SHA; never a branch)
    canonical_ref: str
    # Content digest of the source tree (e.g. sha256:<hex>)
    tree_digest: str
    # Optional build artifact and OCI image digests
    artifact_digest: Optional[str]
    image_digest: Optional[str]
    # Creation provenance
    created_by: str          # actor (agent_id or human principal)
    created_by_task_id: Optional[str]  # task that produced this release
    # Review and publication evidence references
    review_evidence_id: Optional[str]
    publication_evidence_id: Optional[str]
    # Status: draft | reviewed | published | retracted
    status: str
    metadata: JsonDict
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        _validate_commit_sha(self.commit_sha)
        _reject_branch_ref(self.canonical_ref)

    def to_dict(self) -> JsonDict:
        return asdict(self)


class DesiredSourcePolicy(StrEnum):
    """Rollout policy for fleet desired-source transitions."""
    IMMEDIATE = "immediate"
    CANARY = "canary"
    MANUAL = "manual"


@dataclass
class FleetDesiredSourceState:
    """Desired-source state for a fleet or environment scope.

    Schema: mac.fleet_desired_source.v1

    Generation is monotonically increasing; each accepted update produces a
    new generation. Prior generation is recorded for optimistic-concurrency
    guards at the application layer.
    """

    id: str
    # Scope: fleet_id XOR environment_id (one must be non-None)
    fleet_id: Optional[str]
    environment_id: Optional[str]
    # Monotonic generation counter (starts at 1)
    generation: int
    # The release this scope should run
    release_id: str
    # Rollout policy applied for this transition
    rollout_policy: str
    # Actor and reason for this desired state
    actor: str
    reason: str
    # Prior generation for optimistic-concurrency validation
    prior_generation: Optional[int]
    # Pause flag: when True the rollout controller must not act on this state
    paused: bool
    # Idempotency key: caller-supplied request_id so double-submits are safe
    request_id: Optional[str]
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValidationError(
                "generation must be >= 1; got %d" % self.generation
            )
        if self.fleet_id is None and self.environment_id is None:
            raise ValidationError(
                "FleetDesiredSourceState requires fleet_id or environment_id"
            )

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class DesiredSourceTransition:
    """Append-only history record for a desired-source state change.

    Schema: mac.fleet_desired_source_transition.v1
    """

    id: str
    desired_source_state_id: str
    from_generation: Optional[int]
    to_generation: int
    release_id: str
    rollout_policy: str
    actor: str
    reason: str
    request_id: Optional[str]
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class DesiredSourceIdempotencyRecord:
    """Idempotency record for desired-source state requests.

    Prevents double-application of the same request_id within a scope.
    Schema: mac.fleet_desired_source_idempotency.v1
    """

    id: str
    scope_key: str          # e.g. "fleet:<fleet_id>" or "env:<environment_id>"
    request_id: str         # caller-supplied idempotency key
    desired_source_state_id: str
    generation: int
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class EvidenceReuseRecord:
    """Audit record of a prior-executor-evidence reuse decision.

    When a review-infrastructure failure prevents the normal reviewer path
    from completing, recovery logic may attempt to reuse an existing
    executor's evidence record instead of dispatching a fresh execution
    (see ``mac.evidence_reuse_verifier``). Each such decision is persisted
    here so the control plane keeps a durable, queryable trail of *which*
    prior evidence was considered for *which* task, whether the fail-closed
    verifier approved reuse, and the structured problems when it did not.

    Schema: mac.evidence_reuse_record.v1

    ``verification`` holds the serialised
    :class:`~mac.evidence_reuse_verifier.ReuseVerificationResult` (or an
    equivalent structured payload) that backed the ``reused`` decision.
    """

    id: str
    task_id: str
    source_evidence_id: str
    remote_url: Optional[str]
    expected_head_sha: Optional[str]
    reused: bool
    verification: JsonDict
    problems: List[str]
    decided_by: str
    created_at: str
    # Reuse provenance: which agent reused the prior evidence, the coarse
    # reuse context (e.g. "review_bypass" or "recovery_shortcut"), and an
    # open metadata bag. These are additive and optional so historical rows
    # (and positional constructors) keep working; ``prior_evidence_id`` is a
    # provenance alias for ``source_evidence_id`` kept in sync at persist time.
    reused_by_agent_id: str = ""
    reuse_context: str = ""
    metadata: JsonDict = field(default_factory=dict)

    @property
    def prior_evidence_id(self) -> str:
        return self.source_evidence_id

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        data["prior_evidence_id"] = self.source_evidence_id
        return data


def ensure_json_object(value: Optional[Mapping[str, Any]]) -> JsonDict:
    if value is None:
        return {}
    return dict(value)


# ---------------------------------------------------------------------------
# Human principals: first-class assignable human identities (username / email /
# GitHub login) and group membership rows. Human ids use the "human_" prefix so
# they are distinguishable from agent ids ("agent_*") in mixed lists.
# ---------------------------------------------------------------------------


@dataclass
class Human:
    """A first-class human principal in the MAC control plane.

    Humans are assignable to tasks (human_assignees) and can stamp task
    creation (created_by_human). They are identified by a stable synthetic id
    (``human_<uuid>``) plus three optional external identity anchors:
    username, email, and github_login.

    ``groups`` is a JSON-serialised list of group name strings; group
    membership rows are also stored in the ``human_groups`` table for
    index-friendly queries.
    """

    id: str
    username: str
    email: Optional[str]
    github_login: Optional[str]
    display_name: Optional[str]
    groups: List[str]
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class HumanGroup:
    """One group-membership row for a human principal.

    Stored in the ``human_groups`` table alongside the JSON ``groups``
    column on ``humans`` so that both per-human group lookups and
    per-group member queries are index-friendly.
    """

    id: str
    human_id: str
    group_name: str
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)
