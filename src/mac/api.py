from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union

from fastapi import BackgroundTasks, Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from mac.agentbus_control import (
    REPO_UPDATE_CONTENT_TYPE,
    REPO_UPDATE_TOPIC,
    repo_update_payload,
)
from mac.hermes_startup import build_hermes_startup_report
from mac.models import AuthorizationError, MACError, NotFoundError, ValidationError
from mac.services import ControlPlane
from mac.store import SQLiteStore, default_db_path

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenPrincipal:
    """Authenticated bearer principal.

    ``scopes`` is the set of scope strings the token may use; ``"admin"``
    implicitly grants every scope. ``tenant_id`` is the tenant binding; ``None``
    means cross-tenant (admin-like) and any other value means the token may
    only write resources scoped to that tenant. Reads currently ignore the
    tenant binding — that surface returns full fleet state by design today.

    mac-rreh / mac-kgi5 / mac-wcfy: ``agent_id`` binds the token to a
    specific agent. When set, the token may only impersonate that agent
    for actor-bearing actions (heartbeat, claim-next, evidence,
    AgentBus publish, messages, command-audit). When unset, the
    principal is unbound (admin/operator) and may assert any actor.
    """

    scopes: frozenset = field(default_factory=frozenset)
    tenant_id: Optional[str] = None
    agent_id: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        return "admin" in self.scopes

    def has_scope(self, scope: str) -> bool:
        # ``admin`` is the catch-all; ``write`` is the broad authoring
        # bucket that historically covered everything not specifically
        # carved out. New domain scopes (``roles``, ``workflow``) are
        # *additive* — operators with pre-existing ``write`` tokens keep
        # working without having to mint narrower tokens immediately.
        if self.is_admin:
            return True
        if scope in self.scopes:
            return True
        if scope in {"roles", "workflow"} and "write" in self.scopes:
            return True
        return False

    def assert_tenant(self, target_tenant_id: Optional[str]) -> None:
        if self.is_admin or self.tenant_id is None:
            return
        if target_tenant_id is None or target_tenant_id != self.tenant_id:
            raise AuthorizationError(
                "token is bound to a tenant and cannot write to a different tenant"
            )

    def require_global_fleet(self) -> None:
        """Refuse the call for tenant-bound, non-admin tokens.

        Machines, agents, runtimes, environments, and rollouts are part of the
        shared fleet today. A tenant-bound token has no business reaching them
        until we extend the schema to be tenant-aware.
        """
        if self.is_admin or self.tenant_id is None:
            return
        raise AuthorizationError(
            "token is bound to a tenant and cannot operate on global fleet resources"
        )

    def assert_actor(self, claimed_agent_id: str) -> None:
        """Bind a request's actor field to the bearer principal.

        mac-rreh / mac-kgi5 / mac-wcfy: callers pass actor identifiers
        (``agent_id``, ``sender_agent_id``, ``accessor_agent_id``,
        ``created_by``) in request bodies / URL paths. When the token
        is bound to a specific agent, the claimed actor MUST match the
        binding. Admin tokens may impersonate freely; unbound
        operator-style tokens (no agent_id) are treated like admin
        here. This is the principal/payload binding from the audit.
        """
        if self.is_admin or self.agent_id is None:
            return
        if not claimed_agent_id or claimed_agent_id != self.agent_id:
            raise AuthorizationError(
                "token is bound to agent %s and cannot act as %r"
                % (self.agent_id, claimed_agent_id)
            )


AuthTokenMapping = Mapping[str, Union[List[str], Dict[str, Any], TokenPrincipal]]


def _coerce_principal(value: Union[List[str], Dict[str, Any], TokenPrincipal]) -> TokenPrincipal:
    if isinstance(value, TokenPrincipal):
        return value
    if isinstance(value, dict):
        scopes = frozenset(str(s) for s in value.get("scopes", []))
        tenant = value.get("tenant_id")
        agent = value.get("agent_id")
        return TokenPrincipal(scopes=scopes, tenant_id=tenant, agent_id=agent)
    return TokenPrincipal(scopes=frozenset(str(s) for s in value))


def _normalize_auth_tokens(
    raw: Optional[AuthTokenMapping],
) -> Dict[str, TokenPrincipal]:
    if not raw:
        return {}
    return {str(token): _coerce_principal(value) for token, value in raw.items()}


def _resolve_principal(
    token: str, tokens: Mapping[str, TokenPrincipal]
) -> Optional[TokenPrincipal]:
    """Constant-time lookup over the registered tokens.

    Iterates every registered token so timing does not leak which prefix
    matched; ``hmac.compare_digest`` short-circuits in constant time within
    each pair.

    mac-glh0: also supports hashed registrations of the form
    ``sha256:<hex>``. Hashes the candidate token with sha256 and
    compares against the registered hash so a leaked env file with
    only hashed values does not expose the live token.
    """
    import hashlib as _hashlib

    candidate_bytes = token.encode("utf-8")
    candidate_hash = "sha256:" + _hashlib.sha256(candidate_bytes).hexdigest()
    candidate_hash_bytes = candidate_hash.encode("ascii")
    matched: Optional[TokenPrincipal] = None
    for registered, principal in tokens.items():
        registered_bytes = registered.encode("utf-8")
        if registered.startswith("sha256:") and len(registered) == 71:  # 7 + 64
            if hmac.compare_digest(candidate_hash_bytes, registered_bytes):
                matched = principal
        else:
            if hmac.compare_digest(candidate_bytes, registered_bytes):
                matched = principal
    return matched


def _get_principal(request: Request) -> TokenPrincipal:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        # No auth tokens configured — treat as admin to keep dev mode working.
        return TokenPrincipal(scopes=frozenset({"admin"}))
    return principal


def _data(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


AGENTBUS_MAX_EVENT_TIMEOUT_SECONDS = 60.0
AGENTBUS_MIN_EVENT_POLL_SECONDS = 0.25
AGENTBUS_MAX_EVENT_POLL_SECONDS = 5.0
DASHBOARD_TASK_HISTORY_LIMIT = 50
DASHBOARD_TASK_EVIDENCE_LIMIT = 25
DASHBOARD_TASK_REVIEW_LIMIT = 25
DASHBOARD_TASK_PUBLICATION_LIMIT = 10
DASHBOARD_MESSAGE_LIMIT = 200


def _agentbus_clamp_timeout(value: float) -> float:
    return min(AGENTBUS_MAX_EVENT_TIMEOUT_SECONDS, max(0.0, float(value)))


def _agentbus_clamp_poll_interval(value: float) -> float:
    return min(
        AGENTBUS_MAX_EVENT_POLL_SECONDS,
        max(AGENTBUS_MIN_EVENT_POLL_SECONDS, float(value)),
    )


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    project: Optional[str] = None
    priority: int = 0
    required_capabilities: List[str] = Field(default_factory=list)
    dependencies: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = 3
    actor: str = "human"


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    project: Optional[str] = None
    priority: Optional[int] = None
    required_capabilities: Optional[List[str]] = None
    dependencies: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    max_attempts: Optional[int] = None
    actor: str = "human"


class TaskChildCreate(BaseModel):
    title: str
    description: str = ""
    project: Optional[str] = None
    priority: Optional[int] = None
    required_capabilities: Optional[List[str]] = None
    dependencies: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    max_attempts: Optional[int] = None


class TaskChildrenCreate(BaseModel):
    children: List[TaskChildCreate]
    actor: str = "human"


class TaskDelete(BaseModel):
    actor: str = "human"


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)
    status: str = "active"
    actor: str = "human"
    project_id: Optional[str] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    actor: str = "human"


class ProjectDelete(BaseModel):
    actor: str = "human"


class FleetCreate(BaseModel):
    name: str
    description: str = ""
    status: str = "active"
    metadata: Dict[str, Any] = Field(default_factory=dict)
    tenant_id: Optional[str] = None
    agent_ids: List[str] = Field(default_factory=list)
    fleet_id: Optional[str] = None
    actor: str = "human"


class FleetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    tenant_id: Optional[str] = None
    agent_ids: Optional[List[str]] = None
    actor: str = "human"


class FleetDelete(BaseModel):
    actor: str = "human"


class TenantRegister(BaseModel):
    name: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    tenant_id: Optional[str] = None


class UserRegister(BaseModel):
    tenant_id: str
    handle: str
    display_name: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)
    user_id: Optional[str] = None


class PersonaRegister(BaseModel):
    tenant_id: str
    name: str
    soul_ref: str
    memory_scope: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    persona_id: Optional[str] = None


class HermesInstanceRegister(BaseModel):
    tenant_id: str
    name: str
    persona_id: Optional[str] = None
    home_ref: str = ""
    status: str = "active"
    metadata: Dict[str, Any] = Field(default_factory=dict)
    instance_id: Optional[str] = None


class PlatformBindingRegister(BaseModel):
    tenant_id: str
    hermes_instance_id: str
    platform: str
    external_id: str
    display_name: str = ""
    scopes: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    binding_id: Optional[str] = None


class InteractionTaskCreate(TaskCreate):
    user_id: Optional[str] = None
    platform_binding_id: Optional[str] = None
    conversation_ref: Optional[str] = None
    actor: str = "hermes"


class HermesRuntimeProofCreate(BaseModel):
    hermes_startup: Dict[str, Any] = Field(default_factory=dict)


class TransitionRequest(BaseModel):
    target_state: str
    actor: str
    detail: Dict[str, Any] = Field(default_factory=dict)


class EvidenceCreate(BaseModel):
    kind: str
    uri: str
    summary: str
    created_by: str
    checksum: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MachineRegister(BaseModel):
    hostname: str
    labels: Dict[str, Any] = Field(default_factory=dict)
    resources: Dict[str, Any] = Field(default_factory=dict)
    trusted: bool = True
    machine_id: Optional[str] = None
    hardware: Dict[str, Any] = Field(default_factory=dict)


class AgentRegister(BaseModel):
    machine_id: str
    name: str
    capabilities: List[str] = Field(default_factory=list)
    resources: Dict[str, Any] = Field(default_factory=dict)
    agent_id: Optional[str] = None
    hermes_instance_id: Optional[str] = None
    fleet_id: Optional[str] = None
    actor: str = "human"


class AgentAttestationKeyVerify(BaseModel):
    challenge: Dict[str, Any] = Field(default_factory=dict)
    signature: str


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    capabilities: Optional[List[str]] = None
    resources: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    health_status: Optional[str] = None
    hermes_instance_id: Optional[str] = None
    actor: str = "human"


class AgentBulkUpdate(BaseModel):
    agent_ids: List[str] = Field(default_factory=list)
    status: Optional[str] = None
    health_status: Optional[str] = None
    capabilities: Optional[List[str]] = None
    hermes_instance_id: Optional[str] = None
    actor: str = "human"


class RoleCreate(BaseModel):
    slug: str
    name: str
    description: str
    system_prompt: str
    level: str
    display_name: Optional[str] = None
    reports_to: Optional[str] = None
    specialties: List[str] = Field(default_factory=list)
    default_capabilities: List[str] = Field(default_factory=list)
    required_capabilities: List[str] = Field(default_factory=list)
    hardware_requirements: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    tenant_id: Optional[str] = None
    role_id: Optional[str] = None


class RoleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    level: Optional[str] = None
    display_name: Optional[str] = None
    reports_to: Optional[str] = None
    specialties: Optional[List[str]] = None
    default_capabilities: Optional[List[str]] = None
    required_capabilities: Optional[List[str]] = None
    hardware_requirements: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None


class RoleAssign(BaseModel):
    role_id_or_slug: str


class RoleSeed(BaseModel):
    replace: bool = False


class ProvisioningRequestCreate(BaseModel):
    reason: str
    role_slug: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)
    hardware: Dict[str, Any] = Field(default_factory=dict)
    task_id: Optional[str] = None
    tenant_id: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class ProvisioningRequestFulfill(BaseModel):
    agent_id: str


class ProvisioningRequestCancel(BaseModel):
    reason: str = "operator-cancelled"


class WorkflowCreate(BaseModel):
    slug: str
    name: str
    description: str = ""
    workflow_type: str
    definition: Dict[str, Any]
    created_by: str = "human"
    tenant_id: Optional[str] = None
    is_default: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    workflow_type: Optional[str] = None
    definition: Optional[Dict[str, Any]] = None
    is_default: Optional[bool] = None
    enabled: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None


class WorkflowImportYaml(BaseModel):
    yaml: str
    tenant_id: Optional[str] = None
    is_default: bool = False
    created_by: str = "human"


class WorkflowSeed(BaseModel):
    pass


class WorkflowStart(BaseModel):
    started_by: str = "human"
    input: Dict[str, Any] = Field(default_factory=dict)
    tenant_id: Optional[str] = None
    # wf-03: front-loaded approval decisions. Each key must reference an
    # approval-typed node in the workflow definition; each value must be
    # "approved" or "rejected". Validated server-side.
    pre_decisions: Dict[str, str] = Field(default_factory=dict)


class WorkflowPreview(BaseModel):
    definition: Optional[Dict[str, Any]] = None
    input: Dict[str, Any] = Field(default_factory=dict)
    tenant_id: Optional[str] = None


class WorkflowDraftCreate(BaseModel):
    goal: str
    created_by: str = "human"
    tenant_id: Optional[str] = None
    proposed_steps: List[Dict[str, Any]] = Field(default_factory=list)
    questions: List[Dict[str, Any]] = Field(default_factory=list)
    answers: Dict[str, Any] = Field(default_factory=dict)


class WorkflowDraftUpdate(BaseModel):
    goal: Optional[str] = None
    proposed_steps: Optional[List[Dict[str, Any]]] = None
    questions: Optional[List[Dict[str, Any]]] = None
    answers: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    actor: str = "human"


class WorkflowDraftApprove(BaseModel):
    slug: str
    name: str
    workflow_type: str = "custom"
    approved_by: str = "human"
    is_default: bool = False


class WorkflowCancel(BaseModel):
    reason: str
    actor: str = "human"


class HeartbeatRequest(BaseModel):
    status: Optional[str] = None
    health_status: Optional[str] = None
    resources: Optional[Dict[str, Any]] = None
    running_digest: Optional[str] = None
    actor: Optional[str] = None


class LeaseRenewRequest(BaseModel):
    agent_id: str
    lease_seconds: int = 900


class DispatchRequest(BaseModel):
    lease_seconds: int = 900
    limit: int = 100
    stale_after_seconds: Optional[int] = None


class AgentClaimNextRequest(BaseModel):
    lease_seconds: int = 900
    allowed_projects: List[str] = Field(default_factory=list)
    required_metadata: Dict[str, Any] = Field(default_factory=dict)
    require_canary: bool = False
    dry_run: bool = False


class CommandAuditCreate(BaseModel):
    command_id: Optional[str] = None
    phase: str
    argv: List[str]
    cwd: str
    task_id: Optional[str] = None
    lease_id: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: Optional[float] = None
    returncode: Optional[int] = None
    stdout_sha256: Optional[str] = None
    stderr_sha256: Optional[str] = None
    stdout_bytes: Optional[int] = None
    stderr_bytes: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MessageCreate(BaseModel):
    sender_agent_id: str
    recipient_agent_id: Optional[str] = None
    task_id: Optional[str] = None
    message_type: str
    payload: Dict[str, Any]


class AgentBusOpen(BaseModel):
    sender_agent_id: str
    recipient_agent_id: str
    task_id: Optional[str] = None
    topic: str = "content"
    content_type: str = "application/json"
    headers: Dict[str, Any] = Field(default_factory=dict)
    stream_id: Optional[str] = None


class AgentBusAppend(BaseModel):
    sender_agent_id: str
    content_type: Optional[str] = None
    payload: Any = None
    payload_encoding: str = "json"
    final: bool = False


class AgentBusPublish(BaseModel):
    sender_agent_id: str
    recipient_agent_id: str
    task_id: Optional[str] = None
    topic: str = "content"
    content_type: str = "application/json"
    headers: Dict[str, Any] = Field(default_factory=dict)
    payload: Any = None
    payload_encoding: str = "json"


class AgentBusRepoUpdate(BaseModel):
    sender_agent_id: str
    recipient_agent_ids: List[str] = Field(default_factory=list)
    all_agents: bool = False
    repo_path: Optional[str] = None
    remote: str = "origin"
    branch: str = "main"
    restart: bool = True
    request_id: Optional[str] = None


class ObservabilityMetricCreate(BaseModel):
    name: str
    value: float
    unit: str = ""
    layer: str = "external"
    source: str = "agent"
    level: str = "info"
    subject_type: Optional[str] = None
    subject_id: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class ObservabilityLogCreate(BaseModel):
    name: str
    level: str = "info"
    layer: str = "external"
    source: str = "agent"
    subject_type: Optional[str] = None
    subject_id: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class NotificationDelivery(BaseModel):
    status: str = "delivered"


class NotifierChannelConfig(BaseModel):
    name: str
    channel_type: str
    enabled: bool = True
    event_types: List[str] = Field(default_factory=list)
    target: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class NotifierDeliveryRun(BaseModel):
    limit: int = 50
    notification_id: Optional[str] = None


class ReviewRequest(BaseModel):
    reviewer_agent_id: str
    actor: str = "dispatcher"


class ReviewClaim(BaseModel):
    reviewer_agent_id: str
    executor_evidence_id: Optional[str] = None
    actor: str = "reviewer"


class ReviewDecision(BaseModel):
    status: str
    reviewer_agent_id: str
    reason: Optional[str] = None
    evidence_id: Optional[str] = None


class PublicationCreate(BaseModel):
    task_id: str
    target: str
    created_by: str
    evidence_id: Optional[str] = None


class SecretCreate(BaseModel):
    name: str
    value: str
    scopes: Dict[str, Any]
    created_by: str


class SecretAccessRequest(BaseModel):
    accessor_agent_id: str
    purpose: str
    ttl_seconds: int = 300


class SecretRevealRequest(BaseModel):
    audit_id: str
    accessor_agent_id: str


class ArtifactRegister(BaseModel):
    kind: str
    digest: str
    uri: str
    created_by: str
    sbom_uri: Optional[str] = None
    signers: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MoodSet(BaseModel):
    mode: str
    set_by: Optional[str] = None
    reason: Optional[str] = None
    ttl_seconds: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MoodClear(BaseModel):
    cleared_by: Optional[str] = None
    reason: Optional[str] = None


class NapConfigure(BaseModel):
    offset_minutes: Optional[int] = None
    window_minutes: int = 15
    enabled: bool = True
    actor: Optional[str] = None


class NapBegin(BaseModel):
    actor: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class NapComplete(BaseModel):
    summary_evidence_id: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None
    actor: Optional[str] = None


class NapFail(BaseModel):
    reason: str
    actor: Optional[str] = None


class ConversationThreadTrack(BaseModel):
    platform_binding_id: str
    external_thread_id: str
    summary: str = ""
    latest_task_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class VectorRefRecord(BaseModel):
    memory_id: str
    vector_db: str
    collection: str
    point_id: str
    embedding_model: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_by: str = "human"


class EnvironmentRegister(BaseModel):
    name: str
    tenant_id: Optional[str] = None
    channel: str = "fleet"
    promotes_from: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_by: str = "human"


class DeploymentCreate(BaseModel):
    artifact_id: str
    actor: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RuntimeCreate(BaseModel):
    name: str
    manifest: Dict[str, Any]
    created_by: str


class RuntimeRunCreate(BaseModel):
    task_id: str
    agent_id: str
    environment_id: str


class RuntimeRunComplete(BaseModel):
    evidence_id: str
    status: str = "completed"


class ProjectImport(BaseModel):
    source: str
    external_id: str
    title: str
    description: Optional[str] = None
    project: Optional[str] = None
    priority: int = 0
    payload: Dict[str, Any] = Field(default_factory=dict)
    required_capabilities: List[str] = Field(default_factory=list)
    dependencies: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    actor: str = "bridge"


class BeadsRepositoryRegister(BaseModel):
    name: str
    path: str
    source: Optional[str] = None
    project: Optional[str] = None
    required_capabilities: List[str] = Field(default_factory=list)
    enabled: bool = True
    poll_interval_seconds: int = 60
    metadata: Dict[str, Any] = Field(default_factory=dict)
    actor: str = "beads-bridge"


class BeadsRepositoryPoll(BaseModel):
    repository: Optional[str] = None
    force: bool = False
    actor: str = "beads-bridge"


class BeadsRepositoryRepair(BaseModel):
    actor: str = "beads-bridge"
    poll_after: bool = True


class MemoryCreate(BaseModel):
    task_id: Optional[str] = None
    subject_type: str
    subject_id: Optional[str] = None
    record_type: str
    content: str
    evidence_id: Optional[str] = None
    created_by: str


class RolloutCreate(BaseModel):
    version: str
    strategy: str
    target_percent: int
    created_by: str
    tenant_id: Optional[str] = None
    channel: str = "fleet"
    runtime_environment_id: Optional[str] = None
    artifact_uri: Optional[str] = None
    artifact_hash: Optional[str] = None
    health_policy: Dict[str, Any] = Field(default_factory=dict)
    required_eval_set_id: Optional[str] = None


class EvalSetCreate(BaseModel):
    name: str
    scoring: str = "higher_is_better"
    description: str = ""
    baseline_score: Optional[float] = None
    regression_threshold: float = 0.0
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_by: str = "human"


class EvalSetBaselineUpdate(BaseModel):
    baseline_score: float
    actor: str = "human"


class EvalRunRecord(BaseModel):
    eval_set_id: str
    target_kind: str
    target_id: str
    score: float
    detail: Dict[str, Any] = Field(default_factory=dict)
    evidence_id: Optional[str] = None
    created_by: str = "human"


class RolloutAdvance(BaseModel):
    action: str
    actor: str
    detail: Dict[str, Any] = Field(default_factory=dict)


class RolloutRescue(BaseModel):
    actor: str
    reason: str
    detail: Dict[str, Any] = Field(default_factory=dict)


class RolloutArtifactVerify(BaseModel):
    artifact_uri: str
    artifact_hash: str
    actor: str


class RolloutHealthReport(BaseModel):
    actor: str
    checks: Dict[str, Any]


def _load_auth_tokens_from_env() -> Dict[str, TokenPrincipal]:
    # Server-side hub is single-fleet, but honor the fleet-scoped form
    # so a hub started from a multi-fleet ~/.mac/.env (e.g., via
    # `source ~/.mac/.env`) picks the right token (mac-g55y).
    from mac.fleet_env import resolve as _resolve_env

    raw = _resolve_env("MAC_API_TOKENS")
    if raw:
        loaded = json.loads(raw)
        return _normalize_auth_tokens(loaded)
    single = _resolve_env("MAC_API_TOKEN")
    if single is None:
        return {}
    single = single.strip()
    if not single:
        # Refuse silent-fail: an empty token would disable auth without intent.
        raise ValueError(
            "MAC_API_TOKEN is set but empty; unset it to leave the API open, or provide a non-empty token"
        )
    return {single: TokenPrincipal(scopes=frozenset({"admin"}))}


def _required_scope(method: str, path: str) -> Optional[str]:
    if path == "/health":
        return None
    if path == "/ui" or path.startswith("/ui/"):
        return None
    if method == "GET":
        return "read"
    if path.startswith("/agents/") and (
        path.endswith("/heartbeat") or path.endswith("/messages/deliver")
        or path.endswith("/command-audit")
    ):
        return "agent"
    if path.startswith("/agentbus"):
        return "agent"
    if path.startswith("/observability"):
        return "agent"
    if path.startswith("/dispatch"):
        return "dispatch"
    if path.startswith("/secrets") or path.startswith("/secret-audits"):
        return "secret"
    if (
        path.startswith("/runtimes")
        or path.startswith("/environments")
        or path.startswith("/rollouts")
    ):
        return "deploy"
    if path.startswith("/roles") or path.endswith("/role"):
        return "roles"
    if path.startswith("/workflows"):
        return "workflow"
    if path.startswith("/provisioning"):
        # Provisioning rows are operational signals; treat them as
        # deploy-level (a future provisioner that polls + spawns agents
        # is doing infra work, not user-facing writes).
        return "deploy"
    if path.startswith("/reviews/default"):
        # The automated review tick is the closest thing the swarm has
        # to an auto-merge button. Restrict to admin so an ordinary
        # `write` token can't flush every reviewable task to
        # COMPLETED on demand. mac-iez.
        return "admin"
    return "write"


def _authorize_request(
    method: str,
    path: str,
    authorization: Optional[str],
    auth_tokens: Mapping[str, TokenPrincipal],
) -> Optional[TokenPrincipal]:
    required = _required_scope(method, path)
    if required is None or not auth_tokens:
        return None
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthorizationError("missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    principal = _resolve_principal(token, auth_tokens)
    if principal is None:
        raise AuthorizationError("unknown bearer token")
    if not principal.has_scope(required):
        raise AuthorizationError("token lacks required scope: %s" % required)
    return principal


def _should_record_http_observation(path: str) -> bool:
    return not (
        path == "/health"
        or path.startswith("/ui/assets")
        or path.startswith("/observability")
    )


def _resolve_record_http_observations(flag: Optional[bool]) -> bool:
    if flag is not None:
        return flag
    raw = os.environ.get("MAC_RECORD_HTTP_OBSERVATIONS", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


MAX_REGISTRATION_PAYLOAD_BYTES = 64 * 1024


def _ensure_payload_bounded(value: Any, field: str) -> None:
    """Cap registration-style metadata/labels/resources dicts.

    The control plane stores these as JSON blobs in SQLite forever, so an
    unbounded dict from a single client becomes permanent table bloat. 64 KB
    after JSON encoding is well above any legitimate label/metadata payload
    and well below the body-size limit that protects the HTTP layer.
    """
    if value is None:
        return
    try:
        encoded = json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("%s must be JSON serializable" % field) from exc
    if len(encoded.encode("utf-8")) > MAX_REGISTRATION_PAYLOAD_BYTES:
        raise ValidationError(
            "%s exceeds %d-byte limit" % (field, MAX_REGISTRATION_PAYLOAD_BYTES)
        )




TERMINAL_DASHBOARD_STATES = {"completed", "failed", "cancelled"}


def _task_origin(task: Dict[str, Any]) -> Dict[str, Any]:
    metadata = task.get("metadata") or {}
    origin = metadata.get("origin") if isinstance(metadata, dict) else None
    return origin if isinstance(origin, dict) else {}


def _state_counts(items: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _task_project_key(task: Any) -> str:
    project = str(getattr(task, "project", "") or "").strip()
    if project:
        return project
    metadata = getattr(task, "metadata", {}) or {}
    if isinstance(metadata, dict):
        for key in ("project", "repository", "repo"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value
        origin = metadata.get("origin")
        if isinstance(origin, dict):
            for key in ("project", "repository", "repo", "source"):
                value = str(origin.get(key) or "").strip()
                if value:
                    return value
    return "unassigned"


def _project_summary_task(task: Any) -> Dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "state": task.state,
        "priority": task.priority,
        "owner_agent_id": task.owner_agent_id,
        "required_capabilities": list(task.required_capabilities),
        "dependencies": list(task.dependencies),
        "updated_at": task.updated_at,
    }


def _dashboard_project_summaries(
    tasks: List[Any],
    agents: List[Any],
    bridge_items: List[Dict[str, Any]],
    beads_repositories: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    task_by_id = {task.id: task for task in tasks}
    agents_by_id = {agent.id: agent for agent in agents}
    projects: Dict[str, Dict[str, Any]] = {}

    def bucket(project: str) -> Dict[str, Any]:
        if project not in projects:
            projects[project] = {
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
            }
        return projects[project]

    for task in tasks:
        project = _task_project_key(task)
        item = bucket(project)
        item["task_count"] += 1
        item["state_counts"][task.state] = item["state_counts"].get(task.state, 0) + 1
        item["dependency_edge_count"] += len(task.dependencies)
        for capability in task.required_capabilities:
            item["required_capabilities"].add(str(capability))
        if task.owner_agent_id:
            item["active_agent_ids"].add(task.owner_agent_id)
            agent = agents_by_id.get(task.owner_agent_id)
            if agent is not None:
                item["active_agent_names"].add(agent.name)
        if task.state not in TERMINAL_DASHBOARD_STATES:
            item["active_count"] += 1
        if task.state in {"needs_review", "reviewing"}:
            item["review_count"] += 1
        if task.state == "completed":
            item["completed_count"] += 1
        missing_or_incomplete = []
        for dependency_id in task.dependencies:
            dependency = task_by_id.get(dependency_id)
            if dependency is None or dependency.state != "completed":
                missing_or_incomplete.append(dependency_id)
            if dependency is not None and _task_project_key(dependency) != project:
                item["cross_project_dependency_count"] += 1
                if len(item["cross_project_edges"]) < 8:
                    item["cross_project_edges"].append(
                        {
                            "from_project": _task_project_key(dependency),
                            "from_task_id": dependency.id,
                            "from_task_title": dependency.title,
                            "to_task_id": task.id,
                            "to_task_title": task.title,
                        }
                    )
        if task.state == "open" and not missing_or_incomplete:
            item["ready_count"] += 1
            if len(item["frontier_tasks"]) < 10:
                item["frontier_tasks"].append(_project_summary_task(task))
        elif task.state in {"open", "blocked"} and missing_or_incomplete:
            item["blocked_count"] += 1
            if len(item["waiting_tasks"]) < 10:
                blocked = _project_summary_task(task)
                blocked["waiting_on"] = missing_or_incomplete[:8]
                item["waiting_tasks"].append(blocked)
        elif task.state in {"claimed", "running", "needs_review", "reviewing"}:
            if len(item["active_tasks"]) < 10:
                item["active_tasks"].append(_project_summary_task(task))

    for bridge_item in bridge_items:
        project = str(bridge_item.get("project") or bridge_item.get("source") or "unassigned")
        bucket(project)["bridge_item_count"] += 1
    for repo in beads_repositories:
        project = str(repo.get("project") or repo.get("name") or repo.get("source") or "unassigned")
        bucket(project)["repository_count"] += 1

    summaries = []
    for item in projects.values():
        normalized = dict(item)
        normalized["active_agent_ids"] = sorted(item["active_agent_ids"])
        normalized["active_agent_names"] = sorted(item["active_agent_names"])
        normalized["required_capabilities"] = sorted(item["required_capabilities"])
        summaries.append(normalized)
    return sorted(
        summaries,
        key=lambda item: (
            -int(item["ready_count"]),
            -int(item["active_count"]),
            str(item["project"]),
        ),
    )


def _dashboard_swarm_summary(
    agents: List[Any],
    tasks: List[Any],
    machines_by_id: Dict[str, Any],
) -> Dict[str, Any]:
    active_project_by_agent: Dict[str, str] = {}
    for task in tasks:
        if task.owner_agent_id and task.state not in TERMINAL_DASHBOARD_STATES:
            active_project_by_agent[task.owner_agent_id] = _task_project_key(task)

    status_counts = Counter(agent.status for agent in agents)
    health_counts = Counter(agent.health_status for agent in agents)
    role_counts = Counter(str(agent.role_id or "unassigned") for agent in agents)
    project_counts = Counter(active_project_by_agent.get(agent.id, "idle") for agent in agents)
    capability_counts: Counter[str] = Counter()
    machine_counts: Counter[str] = Counter()
    for agent in agents:
        for capability in agent.capabilities:
            capability_counts[str(capability)] += 1
        machine = machines_by_id.get(agent.machine_id)
        machine_counts[machine.hostname if machine is not None else "missing-machine"] += 1

    def rows(counter: Counter[str], limit: int = 40) -> List[Dict[str, Any]]:
        return [
            {"key": key, "count": count}
            for key, count in counter.most_common(limit)
        ]

    return {
        "agent_total": len(agents),
        "status": rows(status_counts),
        "health": rows(health_counts),
        "role": rows(role_counts),
        "project": rows(project_counts),
        "capability": rows(capability_counts),
        "machine": rows(machine_counts),
    }


def _dashboard_task(
    cp: ControlPlane,
    task_id: str,
    *,
    compact: bool = False,
) -> Dict[str, Any]:
    if compact:
        detail = cp.task_detail(
            task_id,
            history_limit=DASHBOARD_TASK_HISTORY_LIMIT,
            evidence_limit=DASHBOARD_TASK_EVIDENCE_LIMIT,
            review_limit=DASHBOARD_TASK_REVIEW_LIMIT,
            publication_limit=DASHBOARD_TASK_PUBLICATION_LIMIT,
        )
    else:
        detail = cp.task_detail(task_id)
    summary = cp.task_summary(task_id)
    detail["summary"] = summary
    if compact:
        detail["history_limited_to"] = DASHBOARD_TASK_HISTORY_LIMIT
        detail["evidence_limited_to"] = DASHBOARD_TASK_EVIDENCE_LIMIT
        detail["reviews_limited_to"] = DASHBOARD_TASK_REVIEW_LIMIT
        detail["publications_limited_to"] = DASHBOARD_TASK_PUBLICATION_LIMIT
    return detail


def _dashboard_session(principal: TokenPrincipal) -> Dict[str, Any]:
    scopes = sorted(principal.scopes)
    can_write = principal.has_scope("write")
    return {
        "scopes": scopes,
        "tenant_id": principal.tenant_id,
        "agent_id": principal.agent_id,
        "is_admin": principal.is_admin,
        "can_read": principal.has_scope("read"),
        "can_write": can_write,
        "mode": (
            "admin"
            if principal.is_admin
            else "read-write"
            if can_write
            else "read-only"
        ),
    }


def _dashboard_agent_base(
    cp: ControlPlane,
    agent: Any,
    tasks: List[Any],
    machines_by_id: Dict[str, Any],
) -> Dict[str, Any]:
    machine = machines_by_id.get(agent.machine_id)
    active_tasks = [
        task.to_dict()
        for task in tasks
        if task.owner_agent_id == agent.id and task.state not in TERMINAL_DASHBOARD_STATES
    ]
    reasons: List[str] = []
    if machine is None:
        reasons.append("missing machine")
    elif not machine.trusted:
        reasons.append("untrusted machine")
    if agent.status not in {"idle", "busy"}:
        reasons.append(agent.status)
    if agent.health_status != "healthy":
        reasons.append(agent.health_status)
    capacity = cp._agent_capacity(agent)
    active_lease_count = cp._agent_active_lease_count(agent.id)
    if active_lease_count >= capacity:
        reasons.append("at capacity")
    return {
        "agent": agent.to_dict(),
        "machine": machine.to_dict() if machine is not None else None,
        "active_tasks": active_tasks,
        "active_projects": sorted({_task_project_key(task) for task in tasks if task.owner_agent_id == agent.id and task.state not in TERMINAL_DASHBOARD_STATES}),
        "capacity": capacity,
        "active_lease_count": active_lease_count,
        "availability": {
            "eligible": not reasons,
            "reasons": reasons,
        },
    }


def _dashboard_dispatch_reasons(
    cp: ControlPlane,
    agent: Any,
    task: Any,
    machine: Optional[Any],
) -> List[str]:
    reasons: List[str] = []
    if agent.status not in {"idle", "busy"}:
        reasons.append("agent status is %s" % agent.status)
    if agent.health_status != "healthy":
        reasons.append("agent health is %s" % agent.health_status)
    if machine is None:
        reasons.append("agent machine is missing")
    elif not machine.trusted:
        reasons.append("machine is not trusted")
    if cp._agent_active_lease_count(agent.id) >= cp._agent_capacity(agent):
        reasons.append("agent is at capacity")
    if machine is not None and not cp._machine_allows_tenant(machine, cp._task_tenant_id(task)):
        reasons.append("machine tenant policy blocks task")
    if machine is not None and not cp._agent_resources_satisfy(agent, machine, task):
        reasons.append("resources do not satisfy task")
    missing = sorted(set(task.required_capabilities) - set(agent.capabilities))
    if missing:
        reasons.append("missing capabilities: %s" % ", ".join(missing))
    return reasons


def _dashboard_dispatch_explain(
    cp: ControlPlane,
    tasks: Optional[List[Any]] = None,
    agents: Optional[List[Any]] = None,
    machines_by_id: Optional[Dict[str, Any]] = None,
    candidate_limit: int = 60,
) -> Dict[str, Any]:
    tasks = tasks if tasks is not None else cp.list_tasks()
    agents = agents if agents is not None else cp.list_agents()
    machines_by_id = machines_by_id if machines_by_id is not None else {
        machine.id: machine for machine in cp.list_machines()
    }
    open_tasks = [task for task in tasks if task.state == "open"]
    explanations = []
    for task in open_tasks:
        candidates = []
        eligible_count = 0
        for agent in agents:
            machine = machines_by_id.get(agent.machine_id)
            eligible = cp._agent_available_for(agent, task)
            if eligible:
                eligible_count += 1
            reasons = [] if eligible else _dashboard_dispatch_reasons(cp, agent, task, machine)
            if not eligible and not reasons:
                reasons.append("dispatch policy rejected pair")
            candidates.append(
                {
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "eligible": eligible,
                    "reasons": reasons,
                }
            )
        candidates.sort(key=lambda item: (not item["eligible"], item["agent_name"], item["agent_id"]))
        limited_candidates = candidates[: max(1, int(candidate_limit))]
        explanations.append(
            {
                "task": task.to_dict(),
                "tenant_id": cp._task_tenant_id(task),
                "candidates": limited_candidates,
                "candidate_count": len(candidates),
                "candidate_limit": max(1, int(candidate_limit)),
                "candidate_truncated": len(candidates) > len(limited_candidates),
                "eligible_agent_count": eligible_count,
            }
        )
    return {"open_task_count": len(open_tasks), "tasks": explanations}


def _dashboard_hermes_activity(
    cp: ControlPlane,
    instance_id: str,
    tasks: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    context = cp.hermes_context(instance_id)
    tasks = tasks if tasks is not None else cp.list_tasks()
    interaction_tasks = [
        task.to_dict()
        for task in tasks
        if _task_origin(task.to_dict()).get("hermes_instance_id") == instance_id
    ]
    return {"context": context, "interaction_tasks": interaction_tasks}


def _dashboard_rollout_status(cp: ControlPlane, rollout_id: str) -> Dict[str, Any]:
    rollout = cp.get_rollout(rollout_id)
    runtime = (
        cp.get_runtime(rollout.runtime_environment_id).to_dict()
        if rollout.runtime_environment_id
        else None
    )
    latest_eval = None
    if rollout.required_eval_set_id is not None:
        latest = cp.latest_eval_run(
            rollout.required_eval_set_id,
            "rollout_version",
            rollout.version,
        )
        latest_eval = latest.to_dict() if latest is not None else None
    return {
        "rollout": rollout.to_dict(),
        "runtime": runtime,
        "events": cp.list_rollout_events(rollout_id),
        "latest_eval_run": latest_eval,
    }


TOKENHUB_SESSION_TICKET_PURPOSE = "tokenhub-admin-session-v1"


def _service_env_files() -> List[Path]:
    home = Path.home()
    mac_home = Path(os.environ.get("MAC_HOME") or home / ".mac").expanduser()
    hermes_home = Path(os.environ.get("HERMES_HOME") or home / ".hermes").expanduser()
    tokenhub_state = Path(os.environ.get("TOKENHUB_STATE_DIR") or home / ".tokenhub").expanduser()
    fleet_name = os.environ.get("FLEET_NAME") or os.environ.get("MAC_FLEET_NAME") or "mac"
    return [
        mac_home / "mac.env",
        hermes_home / ".env",
        tokenhub_state / "env",
        tokenhub_state / "service.env",
        Path("/etc") / fleet_name / "qdrant.env",
        Path("/etc") / fleet_name / "firecrawl-gateway.env",
    ]


def _strip_shell_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_env_file_value(path: Path, names: Iterable[str]) -> Optional[str]:
    wanted = set(names)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        if key in wanted:
            return _strip_shell_quotes(value)
    return None


def _lookup_config_value(
    names: Iterable[str],
    env_files: Optional[Iterable[Path]] = None,
) -> Dict[str, Any]:
    name_list = [str(name) for name in names]
    for name in name_list:
        value = os.environ.get(name)
        if value:
            return {"name": name, "value": value, "source": "env:%s" % name}
    files = list(env_files or _service_env_files())
    for path in files:
        for name in name_list:
            value = _read_env_file_value(path, [name])
            if value:
                return {
                    "name": name,
                    "value": value,
                    "source": "file:%s:%s" % (path, name),
                }
    return {"name": name_list[0] if name_list else "", "value": "", "source": "not_configured"}


def _redacted_secret_ref(value: str) -> str:
    if not value:
        return ""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return "<redacted:%s:chars=%d>" % (digest, len(value))


def _credential_ref(names: Iterable[str], env_files: Optional[Iterable[Path]] = None) -> Dict[str, Any]:
    found = _lookup_config_value(names, env_files)
    value = str(found.get("value") or "")
    return {
        "name": found.get("name") or "",
        "source": found.get("source") or "not_configured",
        "present": bool(value),
        "redacted_value": _redacted_secret_ref(value),
    }


def _redact_service_url(raw: Optional[str]) -> str:
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return "<invalid-url>"
    if not parsed.netloc:
        return raw
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = "redacted@%s" % netloc.rsplit("@", 1)[1]
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _join_service_url(base_url: str, suffix: str) -> str:
    if not base_url:
        return ""
    return base_url.rstrip("/") + suffix


def _service_status(
    hermes_startup: Optional[Dict[str, Any]],
    key: str,
    fallback_url: str,
) -> str:
    if isinstance(hermes_startup, dict):
        report = hermes_startup.get(key)
        if isinstance(report, dict):
            return str(report.get("status") or ("ready" if report.get("ready") else "unknown"))
    return "configured" if fallback_url else "not_configured"


def _dashboard_service_links(
    hermes_startup: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    env_files = _service_env_files()
    tokenhub_url = str(
        _lookup_config_value(("TOKENHUB_URL", "MAC_TOKENHUB_URL"), env_files).get("value")
        or ""
    ).rstrip("/")
    qdrant_url = str(
        _lookup_config_value(("QDRANT_URL", "QDRANT_ADDRESS", "QDRANT_FLEET_URL"), env_files).get("value")
        or ""
    ).rstrip("/")
    firecrawl_url = str(
        _lookup_config_value(("FIRECRAWL_API_URL", "FIRECRAWL_GATEWAY_URL"), env_files).get("value")
        or ""
    ).rstrip("/")
    tokenhub_admin = _credential_ref(("TOKENHUB_ADMIN_TOKEN",), env_files)
    tokenhub_client = _credential_ref(
        ("TOKENHUB_API_KEY", "TOKENHUB_AGENT_KEY", "OPENAI_API_KEY"),
        env_files,
    )
    qdrant_key = _credential_ref(("QDRANT_API_KEY", "QDRANT_FLEET_KEY"), env_files)
    firecrawl_key = _credential_ref(("FIRECRAWL_API_KEY",), env_files)
    tokenhub_sso_available = bool(tokenhub_url and tokenhub_admin["present"])
    qdrant_navigate_available = bool(qdrant_url)
    firecrawl_navigate_available = bool(firecrawl_url)
    return [
        {
            "id": "tokenhub",
            "name": "TokenHub",
            "kind": "owned_ui",
            "role": "LLM token vault and wildcard model router",
            "status": _service_status(hermes_startup, "tokenhub", tokenhub_url),
            "url": _redact_service_url(tokenhub_url),
            "ui_url": _redact_service_url(tokenhub_url),
            "health_url": _redact_service_url(_join_service_url(tokenhub_url, "/healthz")),
            "auth": {
                "type": "admin_session_cookie",
                "credential_pass_through": tokenhub_sso_available,
                "pass_through_url": (
                    "/dashboard/service-links/tokenhub/sso" if tokenhub_sso_available else ""
                ),
                "notes": (
                    "MAC creates a short-lived TokenHub session ticket"
                    if tokenhub_sso_available
                    else "TOKENHUB_ADMIN_TOKEN is required for pass-through"
                ),
            },
            "credentials": [tokenhub_admin, tokenhub_client],
        },
        {
            "id": "qdrant",
            "name": "Qdrant",
            "kind": "external_ui",
            "role": "shared vector memory",
            "status": _service_status(hermes_startup, "qdrant_level2", qdrant_url),
            "url": _redact_service_url(qdrant_url),
            "ui_url": _redact_service_url(_join_service_url(qdrant_url, "/dashboard")),
            "health_url": _redact_service_url(_join_service_url(qdrant_url, "/healthz")),
            "auth": {
                "type": "api_key_or_none",
                "credential_pass_through": qdrant_navigate_available,
                "pass_through_url": (
                    "/dashboard/service-links/qdrant/navigate"
                    if qdrant_navigate_available else ""
                ),
                "notes": (
                    "MAC injects the Qdrant API key into the dashboard redirect"
                    if qdrant_key["present"]
                    else "Qdrant is open (no API key configured)"
                ),
            },
            "credentials": [qdrant_key],
        },
        {
            "id": "firecrawl",
            "name": "Firecrawl Gateway",
            "kind": "owned_api",
            "role": "Firecrawl-compatible web search API",
            "status": "configured" if firecrawl_url else "not_configured",
            "url": _redact_service_url(firecrawl_url),
            "ui_url": _redact_service_url(firecrawl_url),
            "health_url": _redact_service_url(_join_service_url(firecrawl_url, "/health")),
            "auth": {
                "type": "api_key_or_none",
                "credential_pass_through": firecrawl_navigate_available,
                "pass_through_url": (
                    "/dashboard/service-links/firecrawl/navigate"
                    if firecrawl_navigate_available else ""
                ),
                "notes": (
                    "MAC Firecrawl gateway health page"
                    if firecrawl_key["present"]
                    else "Firecrawl gateway (no API key configured)"
                ),
            },
            "credentials": [firecrawl_key],
        },
    ]


def _tokenhub_session_ticket_url() -> str:
    env_files = _service_env_files()
    tokenhub_url = str(
        _lookup_config_value(("TOKENHUB_URL", "MAC_TOKENHUB_URL"), env_files).get("value")
        or ""
    ).rstrip("/")
    admin = _lookup_config_value(("TOKENHUB_ADMIN_TOKEN",), env_files)
    admin_token = str(admin.get("value") or "")
    if not tokenhub_url:
        raise ValidationError("TokenHub URL is not configured")
    if not admin_token:
        raise ValidationError("TokenHub admin token is not configured")
    expires = int(time.time()) + 60
    body = "%s:%d" % (TOKENHUB_SESSION_TICKET_PURPOSE, expires)
    signature = hmac.new(
        admin_token.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    query = urllib.parse.urlencode(
        {"expires": str(expires), "signature": signature, "redirect": "/"}
    )
    return "%s/admin/v1/session/claim?%s" % (tokenhub_url, query)


def _qdrant_navigate_url() -> str:
    env_files = _service_env_files()
    qdrant_url = str(
        _lookup_config_value(("QDRANT_URL", "QDRANT_ADDRESS", "QDRANT_FLEET_URL"), env_files).get("value")
        or ""
    ).rstrip("/")
    if not qdrant_url:
        raise ValidationError("Qdrant URL is not configured")
    api_key = str(
        _lookup_config_value(("QDRANT_API_KEY", "QDRANT_FLEET_KEY"), env_files).get("value")
        or ""
    )
    dashboard_url = "%s/dashboard" % qdrant_url
    if api_key:
        dashboard_url += "?%s" % urllib.parse.urlencode({"api_key": api_key})
    return dashboard_url


def _firecrawl_navigate_url() -> str:
    env_files = _service_env_files()
    firecrawl_url = str(
        _lookup_config_value(("FIRECRAWL_API_URL", "FIRECRAWL_GATEWAY_URL"), env_files).get("value")
        or ""
    ).rstrip("/")
    if not firecrawl_url:
        raise ValidationError("Firecrawl URL is not configured")
    return firecrawl_url


def _dashboard_state(
    cp: ControlPlane,
    hermes_startup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    tenants = [tenant.to_dict() for tenant in cp.list_tenants()]
    users = [user.to_dict() for user in cp.list_users()]
    personas = [persona.to_dict() for persona in cp.list_personas()]
    hermes_instances = [instance.to_dict() for instance in cp.list_hermes_instances()]
    bindings = [binding.to_dict() for binding in cp.list_platform_bindings()]
    machines = cp.list_machines()
    machines_by_id = {machine.id: machine for machine in machines}
    agents = cp.list_agents()
    fleets = [fleet.to_dict() for fleet in cp.list_fleets()]
    tasks = cp.list_tasks()
    task_dicts = [task.to_dict() for task in tasks]
    dead_letters = [task.to_dict() for task in cp.list_dead_letters()]
    rollouts = cp.list_rollouts()
    roles = [role.to_dict() for role in cp.list_roles()]
    provisioning_requests = [
        request.to_dict()
        for request in cp.provisioning.list_requests(limit=120)
    ]
    secrets = [secret.to_dict() for secret in cp.list_secrets()]
    secret_audits = [audit.to_dict() for audit in cp.list_secret_audits()]
    workflows = [workflow.to_dict() for workflow in cp.list_workflows()]
    workflow_drafts = [draft.to_dict() for draft in cp.list_workflow_drafts(limit=120)]
    workflow_runs = cp.workflow_runs_summary()
    notifier_channels = [channel.to_dict() for channel in cp.list_notifier_channels()]
    agentbus_streams = [
        stream.to_dict() for stream in cp.list_agentbus_streams(limit=120)
    ]
    artifacts = [artifact.to_dict() for artifact in cp.list_artifacts()]
    bridge_items = [item.to_dict() for item in cp.list_project_items()]
    beads_repositories = [
        repo.to_dict() for repo in cp.list_beads_repositories()
    ]
    memory_records = [
        record.to_dict() for record in cp.search_memory()
    ][-120:]
    nap_schedules = [schedule.to_dict() for schedule in cp.list_nap_schedules()]
    nap_runs = [run.to_dict() for run in cp.list_nap_runs()]
    runtime_runs = [run.to_dict() for run in cp.list_runtime_runs()]
    integration_findings = [
        finding.to_dict() for finding in cp.list_integration_findings(limit=120)
    ]
    integration_observations = [
        observation.to_dict()
        for observation in cp.list_integration_observations(limit=120)
    ]
    task_details = [_dashboard_task(cp, task.id, compact=True) for task in tasks]
    rollout_statuses = [_dashboard_rollout_status(cp, rollout.id) for rollout in rollouts]
    project_summaries = cp.list_projects()
    hermes_work_contexts = {
        instance["id"]: cp.hermes_work_context(instance["id"], task_limit=40)
        for instance in hermes_instances
    }
    hermes_runtime_proofs = {}
    for instance in hermes_instances:
        submitted = _latest_submitted_runtime_proof(instance)
        hermes_runtime_proofs[instance["id"]] = submitted or cp.hermes_runtime_proof(
            instance["id"],
            hermes_startup=hermes_startup,
        )
    swarm_summary = _dashboard_swarm_summary(agents, tasks, machines_by_id)
    return {
        "overview": {
            "counts": {
                "tenants": len(tenants),
                "users": len(users),
                "personas": len(personas),
                "hermes_instances": len(hermes_instances),
                "platform_bindings": len(bindings),
                "machines": len(machines),
                "trusted_machines": sum(1 for machine in machines if machine.trusted),
                "agents": len(agents),
                "fleets": len(fleets),
                "healthy_agents": sum(1 for agent in agents if agent.health_status == "healthy"),
                "busy_agents": sum(1 for agent in agents if agent.status == "busy"),
                "active_tasks": sum(
                    1 for task in tasks if task.state not in TERMINAL_DASHBOARD_STATES
                ),
                "dead_letters": len(dead_letters),
                "rollouts": len(rollouts),
                "secrets": len(secrets),
                "secret_audits": len(secret_audits),
                "roles": len(roles),
                "pending_provisioning_requests": sum(
                    1 for request in provisioning_requests if request["status"] == "pending"
                ),
                "workflows": len(workflows),
                "workflow_drafts": len(workflow_drafts),
                "workflow_runs": workflow_runs.get("total", 0),
                "notifier_channels": len(notifier_channels),
                "agentbus_streams": len(agentbus_streams),
                "artifacts": len(artifacts),
                "beads_repositories": len(beads_repositories),
                "projects": len(project_summaries),
                "memory_records": len(memory_records),
                "integration_findings": len(integration_findings),
                "open_integration_findings": sum(
                    1 for finding in integration_findings if finding["status"] == "open"
                ),
            },
            "task_states": _state_counts(task_dicts, "state"),
            "agent_statuses": _state_counts([agent.to_dict() for agent in agents], "status"),
        },
        "project_summaries": project_summaries,
        "swarm_summary": swarm_summary,
        "tenants": tenants,
        "users": users,
        "personas": personas,
        "hermes_instances": hermes_instances,
        "hermes_work_contexts": hermes_work_contexts,
        "hermes_runtime_proofs": hermes_runtime_proofs,
        "platform_bindings": bindings,
        "roles": roles,
        "provisioning_requests": provisioning_requests,
        "machines": [machine.to_dict() for machine in machines],
        "fleets": fleets,
        "agents": [
            _dashboard_agent_base(cp, agent, tasks, machines_by_id)
            for agent in agents
        ],
        "tasks": task_details,
        "dead_letters": dead_letters,
        "dispatch": _dashboard_dispatch_explain(cp, tasks, agents, machines_by_id),
        "messages": [
            message.to_dict()
            for message in cp.list_messages(limit=DASHBOARD_MESSAGE_LIMIT)
        ],
        "notifications": [
            notification.to_dict() for notification in cp.list_notifications(limit=120)
        ],
        "notifier_channels": notifier_channels,
        "workflows": workflows,
        "workflow_drafts": workflow_drafts,
        "workflow_runs": workflow_runs,
        "agentbus_streams": agentbus_streams,
        "artifacts": artifacts,
        "bridge_items": bridge_items,
        "beads_repositories": beads_repositories,
        "memory_records": memory_records,
        "nap_schedules": nap_schedules,
        "nap_runs": nap_runs,
        "integration_findings": integration_findings,
        "integration_observations": integration_observations,
        "service_links": _dashboard_service_links(hermes_startup),
        "events": cp.list_events(limit=240),
        "command_audit": [
            record.to_dict() for record in cp.list_command_audit(limit=120)
        ],
        "secrets": secrets,
        "secret_audits": secret_audits,
        "runtimes": [runtime.to_dict() for runtime in cp.list_runtimes()],
        "runtime_runs": runtime_runs,
        "rollouts": rollout_statuses,
        "eval_sets": [eval_set.to_dict() for eval_set in cp.list_eval_sets()],
        "eval_runs": [run.to_dict() for run in cp.list_eval_runs()],
        "observability": cp.observability_summary(),
        "hermes_startup": hermes_startup,
    }


def _latest_submitted_runtime_proof(instance: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    metadata = instance.get("metadata") if isinstance(instance.get("metadata"), dict) else {}
    record = (
        metadata.get("latest_runtime_proof")
        if isinstance(metadata.get("latest_runtime_proof"), dict)
        else {}
    )
    proof = record.get("proof") if isinstance(record.get("proof"), dict) else None
    if not proof or proof.get("schema") != "mac.hermes_runtime_proof.v1":
        return None
    return json.loads(json.dumps(proof))


def create_app(
    db_path: Optional[str] = None,
    control_plane: Optional[ControlPlane] = None,
    auth_tokens: Optional[AuthTokenMapping] = None,
    record_http_observations: Optional[bool] = None,
) -> FastAPI:
    cp = control_plane or ControlPlane(
        SQLiteStore(db_path or default_db_path())
    )
    tokens: Dict[str, TokenPrincipal] = (
        _normalize_auth_tokens(auth_tokens)
        if auth_tokens is not None
        else _load_auth_tokens_from_env()
    )
    # mac-853j: refuse to fail-open when the API is bound to a non-loopback
    # interface. Deployments that explicitly want a no-auth dev mode can
    # set MAC_API_ALLOW_OPEN=1, but the default for a 0.0.0.0 hub is
    # fail-closed (the alternative was: any tenant on the network could
    # reach /secrets/{id}/reveal).
    if not tokens:
        bind_host = (os.environ.get("MAC_BIND_HOST") or "").strip()
        allow_open = (os.environ.get("MAC_API_ALLOW_OPEN") or "").strip().lower() in {"1", "true", "yes", "on"}
        is_loopback = bind_host in {"", "127.0.0.1", "::1", "localhost"}
        if not is_loopback and not allow_open:
            raise ValidationError(
                "auth fail-open refused: MAC_BIND_HOST=%r is non-loopback and "
                "no MAC_API_TOKEN/MAC_API_TOKENS is set; set MAC_API_ALLOW_OPEN=1 "
                "to override or configure tokens (mac-853j)" % bind_host
            )
    record_http_obs = _resolve_record_http_observations(record_http_observations)
    app = FastAPI(title="MAC Control Plane", version="0.1.0")
    app.state.control_plane = cp
    app.state.auth_tokens = tokens
    app.state.hermes_startup = build_hermes_startup_report()
    if (
        os.environ.get("MAC_REQUIRE_HERMES_STARTUP_READY", "").strip().lower()
        in {"1", "true", "yes", "on"}
        and not app.state.hermes_startup["ready"]
    ):
        raise ValidationError(
            "Hermes startup readiness failed: %s"
            % "; ".join(app.state.hermes_startup["warnings"])
        )
    ui_dir = Path(__file__).with_name("ui")
    if ui_dir.exists():
        app.mount("/ui/assets", StaticFiles(directory=str(ui_dir)), name="ui-assets")

    @app.exception_handler(MACError)
    async def handle_mac_error(request: Any, exc: MACError) -> JSONResponse:
        if isinstance(exc, NotFoundError):
            return JSONResponse(status_code=404, content={"detail": str(exc)})
        if isinstance(exc, AuthorizationError):
            return JSONResponse(status_code=403, content={"detail": str(exc)})
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    def _emit_http_observation(
        request: Request, status_code: int, started: float, error_name: str
    ) -> None:
        if not record_http_obs or not _should_record_http_observation(request.url.path):
            return
        duration_ms = (time.monotonic() - started) * 1000.0
        level = "error" if status_code >= 500 else "warning" if status_code >= 400 else "info"
        detail = {
            "method": request.method,
            "path": request.url.path,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 3),
        }
        if error_name:
            detail["error"] = error_name
        try:
            cp.record_metric(
                "http.request.duration_ms",
                duration_ms,
                unit="ms",
                layer="api",
                source="http",
                level=level,
                detail=detail,
            )
        except (MACError, sqlite3.Error):
            _log.warning("failed to record http observation for %s", request.url.path, exc_info=True)

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Any:
        started = time.monotonic()
        status_code = 500
        error_name = ""
        try:
            principal = _authorize_request(
                request.method,
                request.url.path,
                request.headers.get("authorization"),
                tokens,
            )
            request.state.principal = principal
        except AuthorizationError as exc:
            status_code = 403
            error_name = exc.__class__.__name__
            _emit_http_observation(request, status_code, started, error_name)
            return JSONResponse(status_code=status_code, content={"detail": str(exc)})
        try:
            response = await call_next(request)
            status_code = int(getattr(response, "status_code", 500))
            return response
        except Exception as exc:
            error_name = exc.__class__.__name__
            raise
        finally:
            _emit_http_observation(request, status_code, started, error_name)

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/startup/hermes")
    def hermes_startup() -> Dict[str, Any]:
        app.state.hermes_startup = build_hermes_startup_report()
        return app.state.hermes_startup

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(ui_dir / "index.html")

    @app.get("/dashboard/state")
    def dashboard_state(
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        app.state.hermes_startup = build_hermes_startup_report()
        model = _dashboard_state(cp, app.state.hermes_startup)
        model["session"] = _dashboard_session(principal)
        return model

    @app.get("/dashboard/service-links/tokenhub/sso", include_in_schema=False)
    def tokenhub_sso(
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> RedirectResponse:
        if not principal.is_admin:
            raise AuthorizationError("TokenHub pass-through requires an admin MAC token")
        return RedirectResponse(_tokenhub_session_ticket_url(), status_code=303)

    @app.get("/dashboard/service-links/{service_id}/navigate", include_in_schema=False)
    def service_navigate(service_id: str) -> Dict[str, str]:
        if service_id == "tokenhub":
            return {"url": _tokenhub_session_ticket_url()}
        if service_id == "qdrant":
            return {"url": _qdrant_navigate_url()}
        if service_id == "firecrawl":
            return {"url": _firecrawl_navigate_url()}
        raise NotFoundError("unknown service: %s" % service_id)

    @app.get("/dashboard/agents/{agent_id}")
    def dashboard_agent(agent_id: str) -> Dict[str, Any]:
        agent = cp.get_agent(agent_id)
        tasks = cp.list_tasks()
        machines_by_id = {machine.id: machine for machine in cp.list_machines()}
        model = _dashboard_agent_base(cp, agent, tasks, machines_by_id)
        model["messages"] = [
            message.to_dict()
            for message in cp.list_messages(agent_id, limit=DASHBOARD_MESSAGE_LIMIT)
        ]
        model["dispatch"] = [
            item
            for item in _dashboard_dispatch_explain(cp, tasks, [agent], machines_by_id)["tasks"]
            if item["eligible_agent_count"] or item["candidates"]
        ]
        return model

    @app.get("/dashboard/tasks/{task_id}/timeline")
    def dashboard_task_timeline(task_id: str) -> Dict[str, Any]:
        return _dashboard_task(cp, task_id)

    @app.get("/dashboard/dispatch/explain")
    def dashboard_dispatch_explain() -> Dict[str, Any]:
        return _dashboard_dispatch_explain(cp)

    @app.get("/dashboard/hermes/{instance_id}/activity")
    def dashboard_hermes_activity(instance_id: str) -> Dict[str, Any]:
        return _dashboard_hermes_activity(cp, instance_id)

    @app.get("/dashboard/rollouts/{rollout_id}/status")
    def dashboard_rollout_status(rollout_id: str) -> Dict[str, Any]:
        return _dashboard_rollout_status(cp, rollout_id)

    @app.post("/tenants")
    def register_tenant(
        body: TenantRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # Creating tenants is a cross-tenant operation; only admin/unbound
        # principals can perform it.
        principal.require_global_fleet()
        return cp.register_tenant(**_data(body)).to_dict()

    @app.get("/tenants")
    def list_tenants() -> List[Dict[str, Any]]:
        return [tenant.to_dict() for tenant in cp.list_tenants()]

    @app.post("/users")
    def register_user(
        body: UserRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.register_user(**_data(body)).to_dict()

    @app.get("/users")
    def list_users(tenant_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [user.to_dict() for user in cp.list_users(tenant_id)]

    @app.post("/personas")
    def register_persona(
        body: PersonaRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.register_persona(**_data(body)).to_dict()

    @app.get("/personas")
    def list_personas(tenant_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [persona.to_dict() for persona in cp.list_personas(tenant_id)]

    @app.post("/hermes-instances")
    def register_hermes_instance(
        body: HermesInstanceRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.register_hermes_instance(**_data(body)).to_dict()

    @app.get("/hermes-instances")
    def list_hermes_instances(tenant_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [instance.to_dict() for instance in cp.list_hermes_instances(tenant_id)]

    @app.get("/hermes-instances/{instance_id}/context")
    def hermes_context(instance_id: str) -> Dict[str, Any]:
        return cp.hermes_context(instance_id)

    @app.get("/hermes-instances/{instance_id}/work-context")
    def hermes_work_context(
        instance_id: str,
        include_completed: bool = Query(default=True),
        task_limit: int = Query(default=100),
    ) -> Dict[str, Any]:
        return cp.hermes_work_context(
            instance_id,
            include_completed=include_completed,
            task_limit=task_limit,
        )

    @app.get("/hermes-instances/{instance_id}/runtime-proof")
    def hermes_runtime_proof(instance_id: str) -> Dict[str, Any]:
        app.state.hermes_startup = build_hermes_startup_report()
        return cp.hermes_runtime_proof(
            instance_id,
            hermes_startup=app.state.hermes_startup,
        )

    @app.post("/hermes-instances/{instance_id}/runtime-proof")
    def hermes_runtime_proof_with_startup(
        instance_id: str,
        body: HermesRuntimeProofCreate,
    ) -> Dict[str, Any]:
        proof = cp.hermes_runtime_proof(
            instance_id,
            hermes_startup=body.hermes_startup,
        )
        cp.record_hermes_runtime_proof(instance_id, proof)
        return proof

    @app.post("/hermes-instances/{instance_id}/tasks")
    def create_interaction_task(
        instance_id: str,
        body: InteractionTaskCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        instance = cp.get_hermes_instance(instance_id)
        principal.assert_tenant(instance.tenant_id)
        data = _data(body)
        actor = data.pop("actor", "hermes")
        return cp.create_interaction_task(instance_id, actor=actor, **data).to_dict()

    @app.post("/platform-bindings")
    def register_platform_binding(
        body: PlatformBindingRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.register_platform_binding(**_data(body)).to_dict()

    @app.get("/platform-bindings")
    def list_platform_bindings(
        tenant_id: Optional[str] = Query(default=None),
        hermes_instance_id: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [
            binding.to_dict()
            for binding in cp.list_platform_bindings(
                tenant_id=tenant_id,
                hermes_instance_id=hermes_instance_id,
            )
        ]

    @app.post("/tasks")
    def create_task(
        body: TaskCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _ensure_payload_bounded(body.metadata, "task.metadata")
        data = _data(body)
        actor = data.pop("actor", "human")
        metadata = dict(data.get("metadata") or {})
        origin = dict(metadata.get("origin") or {}) if isinstance(metadata.get("origin"), dict) else {}
        existing_tenant = origin.get("tenant_id") or metadata.get("tenant_id")
        if principal.tenant_id is not None and not principal.is_admin:
            if existing_tenant is not None and existing_tenant != principal.tenant_id:
                principal.assert_tenant(existing_tenant)
            # Stamp the principal's tenant onto the task so downstream filters
            # see it even when the caller forgot to set it explicitly.
            origin["tenant_id"] = principal.tenant_id
            metadata["origin"] = origin
            data["metadata"] = metadata
        return cp.create_task(actor=actor, **data).to_dict()

    @app.get("/tasks")
    def list_tasks(
        state: Optional[str] = Query(default=None),
        tenant_id: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [task.to_dict() for task in cp.list_tasks(state, tenant_id)]

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str) -> Dict[str, Any]:
        return cp.task_detail(task_id)

    @app.put("/tasks/{task_id}")
    def update_task(task_id: str, body: TaskUpdate) -> Dict[str, Any]:
        data = _data(body)
        actor = data.pop("actor", "human")
        if data.get("metadata") is not None:
            _ensure_payload_bounded(data["metadata"], "task.metadata")
        return cp.update_task(task_id, actor=actor, **data).to_dict()

    @app.post("/tasks/{task_id}/children")
    def add_child_tasks(task_id: str, body: TaskChildrenCreate) -> Dict[str, Any]:
        data = _data(body)
        for index, child in enumerate(data.get("children") or [], start=1):
            if child.get("metadata") is not None:
                _ensure_payload_bounded(child["metadata"], "task.children.%d.metadata" % index)
        return cp.add_child_tasks(
            task_id,
            data.get("children") or [],
            actor=data.get("actor", "human"),
        )

    @app.delete("/tasks/{task_id}")
    def delete_task(
        task_id: str,
        force: bool = Query(default=False),
        actor: str = Query(default="human"),
    ) -> Dict[str, Any]:
        cp.delete_task(task_id, force=force, actor=actor)
        return {"deleted": task_id}

    @app.get("/tasks/{task_id}/summary")
    def task_summary(task_id: str) -> Dict[str, Any]:
        return cp.task_summary(task_id)

    @app.post("/fleets")
    def create_fleet(
        body: FleetCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        _ensure_payload_bounded(body.metadata, "fleet.metadata")
        return cp.create_fleet(**_data(body)).to_dict()

    @app.get("/fleets")
    def list_fleets(
        status: Optional[str] = Query(default=None),
        tenant_id: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [fleet.to_dict() for fleet in cp.list_fleets(status=status, tenant_id=tenant_id)]

    @app.get("/fleets/{fleet_id_or_name}")
    def get_fleet(fleet_id_or_name: str) -> Dict[str, Any]:
        return cp.get_fleet(fleet_id_or_name).to_dict()

    @app.put("/fleets/{fleet_id_or_name}")
    def update_fleet(
        fleet_id_or_name: str,
        body: FleetUpdate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        data = _data(body)
        if data.get("metadata") is not None:
            _ensure_payload_bounded(data["metadata"], "fleet.metadata")
        return cp.update_fleet(fleet_id_or_name, **data).to_dict()

    @app.delete("/fleets/{fleet_id_or_name}")
    def delete_fleet(
        fleet_id_or_name: str,
        actor: str = Query(default="human"),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        cp.delete_fleet(fleet_id_or_name, actor=actor)
        return {"deleted": fleet_id_or_name}

    @app.get("/projects")
    def list_projects() -> List[Dict[str, Any]]:
        return cp.list_projects()

    @app.post("/projects")
    def create_project(
        body: ProjectCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _ensure_payload_bounded(body.metadata, "project.metadata")
        data = _data(body)
        actor = data.pop("actor", "human")
        metadata = dict(data.get("metadata") or {})
        if principal.tenant_id is not None and not principal.is_admin:
            origin_value = metadata.get("origin")
            origin = dict(origin_value) if isinstance(origin_value, dict) else {}
            origin["tenant_id"] = principal.tenant_id
            metadata["origin"] = origin
            data["metadata"] = metadata
        return cp.create_project(actor=actor, **data).to_dict()

    @app.get("/projects/{project}")
    def get_project(project: str) -> Dict[str, Any]:
        return cp.get_project(project)

    @app.put("/projects/{project}")
    def update_project(project: str, body: ProjectUpdate) -> Dict[str, Any]:
        data = _data(body)
        actor = data.pop("actor", "human")
        if data.get("metadata") is not None:
            _ensure_payload_bounded(data["metadata"], "project.metadata")
        return cp.update_project(project, actor=actor, **data).to_dict()

    @app.delete("/projects/{project}")
    def delete_project(
        project: str,
        force: bool = Query(default=False),
        actor: str = Query(default="human"),
    ) -> Dict[str, Any]:
        cp.delete_project(project, force=force, actor=actor)
        return {"deleted": project}

    @app.post("/tasks/{task_id}/transition")
    def transition_task(
        task_id: str,
        body: TransitionRequest,
        background_tasks: BackgroundTasks,
    ) -> Dict[str, Any]:
        task = cp.transition_task(
            task_id,
            body.target_state,
            body.actor,
            body.detail,
            drain_outbox=False,
        )
        background_tasks.add_task(
            cp.drain_task_transition_outbox_best_effort,
            task_id=task_id,
            limit=20,
        )
        return task.to_dict()

    @app.post("/tasks/{task_id}/claim")
    def claim_task(
        task_id: str,
        agent_id: str,
        background_tasks: BackgroundTasks,
        lease_seconds: int = 900,
    ) -> Dict[str, Any]:
        task, lease = cp.claim_task(task_id, agent_id, lease_seconds, sync_beads=False)
        background_tasks.add_task(
            cp.sync_claim_side_effects,
            task.id,
            agent_id,
            lease.id,
            lease.expires_at,
        )
        return {"task": task.to_dict(), "lease": lease.to_dict()}

    @app.post("/leases/{lease_id}/renew")
    def renew_lease(lease_id: str, body: LeaseRenewRequest) -> Dict[str, Any]:
        return cp.renew_lease(lease_id, body.agent_id, body.lease_seconds).to_dict()

    @app.post("/tasks/{task_id}/start")
    def start_task(
        task_id: str,
        agent_id: str,
        background_tasks: BackgroundTasks,
    ) -> Dict[str, Any]:
        task = cp.start_task(task_id, agent_id, drain_outbox=False)
        background_tasks.add_task(
            cp.drain_task_transition_outbox_best_effort,
            task_id=task_id,
            limit=20,
        )
        return task.to_dict()

    @app.post("/tasks/{task_id}/submit-for-review")
    def submit_for_review(
        task_id: str,
        agent_id: str,
        background_tasks: BackgroundTasks,
        advance_default_workflow: bool = Query(default=False),
    ) -> Dict[str, Any]:
        task = cp.submit_for_review(task_id, agent_id, drain_outbox=False)
        background_tasks.add_task(
            cp.drain_task_transition_outbox_best_effort,
            task_id=task_id,
            limit=20,
        )
        if advance_default_workflow:
            background_tasks.add_task(
                cp.advance_default_review_workflow,
                task_id,
                actor=agent_id,
            )
        return task.to_dict()

    @app.post("/tasks/{task_id}/evidence")
    def add_evidence(
        task_id: str,
        body: EvidenceCreate,
        background_tasks: BackgroundTasks,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # mac-rreh: ``created_by`` arrived as opaque payload before; an
        # agent token could mint evidence as any other agent and defeat
        # the reviewer-independence check (which reads evidence.created_by
        # downstream). Bind it to the principal.
        principal.assert_actor(body.created_by)
        evidence = cp.add_evidence(task_id=task_id, sync_beads=False, **_data(body))
        background_tasks.add_task(cp.sync_evidence_side_effects, evidence.id)
        return evidence.to_dict()

    @app.post("/machines")
    def register_machine(
        body: MachineRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        _ensure_payload_bounded(body.labels, "machine.labels")
        _ensure_payload_bounded(body.resources, "machine.resources")
        return cp.register_machine(**_data(body)).to_dict()

    @app.get("/machines")
    def list_machines() -> List[Dict[str, Any]]:
        return [machine.to_dict() for machine in cp.list_machines()]

    @app.post("/agents")
    def register_agent(
        body: AgentRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        _ensure_payload_bounded(body.resources, "agent.resources")
        data = _data(body)
        fleet_id = data.pop("fleet_id", None)
        actor = str(data.get("actor") or "human")
        agent = cp.register_agent(**data)
        if fleet_id:
            fleet = cp.get_fleet(str(fleet_id))
            cp.update_fleet(
                fleet.id,
                agent_ids=sorted(set(fleet.agent_ids + [agent.id])),
                actor=actor,
            )
        payload = agent.to_dict()
        # First-registration only: surface the freshly-minted
        # attestation key so the operator can deploy it to the worker.
        # The key is never returned again — re-registrations get a
        # response without ``attestation_key``. mac-ng2.
        key = getattr(agent, "attestation_key", None)
        if key:
            payload["attestation_key"] = key
        return payload

    @app.post("/agents/{agent_id}/attestation-key/rotate")
    def rotate_agent_attestation_key(
        agent_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, str]:
        principal.require_global_fleet()
        return {
            "agent_id": agent_id,
            "attestation_key": cp.rotate_agent_attestation_key(agent_id),
        }

    @app.post("/agents/{agent_id}/attestation-key/verify")
    def verify_agent_attestation_key(
        agent_id: str,
        body: AgentAttestationKeyVerify,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        _ensure_payload_bounded(body.challenge, "agent.attestation.challenge")
        return {
            "agent_id": agent_id,
            "valid": cp.verify_agent_attestation_challenge(
                agent_id,
                body.challenge,
                body.signature,
            ),
        }

    @app.get("/agents")
    def list_agents() -> List[Dict[str, Any]]:
        return [agent.to_dict() for agent in cp.list_agents()]

    @app.post("/agents/bulk")
    def bulk_update_agents(
        body: AgentBulkUpdate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        if not body.agent_ids:
            raise ValidationError("agent_ids is required")
        data = _data(body)
        agent_ids = [str(agent_id).strip() for agent_id in data.pop("agent_ids", [])]
        if not data:
            raise ValidationError("bulk update requires at least one update field")
        updated = []
        failed = []
        for agent_id in agent_ids:
            if not agent_id:
                continue
            try:
                updated.append(cp.update_agent(agent_id, **data).to_dict())
            except MACError as exc:
                failed.append({"agent_id": agent_id, "error": str(exc)})
        return {
            "updated": updated,
            "updated_count": len(updated),
            "failed": failed,
            "failed_count": len(failed),
        }

    @app.get("/agents/{agent_id}")
    def get_agent(agent_id: str) -> Dict[str, Any]:
        return cp.get_agent(agent_id).to_dict()

    @app.put("/agents/{agent_id}")
    def update_agent(
        agent_id: str,
        body: AgentUpdate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        data = _data(body)
        if data.get("resources") is not None:
            _ensure_payload_bounded(data["resources"], "agent.resources")
        return cp.update_agent(agent_id, **data).to_dict()

    @app.post("/agents/{agent_id}/disable")
    def disable_agent(
        agent_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.disable_agent(agent_id).to_dict()

    @app.delete("/agents/{agent_id}")
    def delete_agent(
        agent_id: str,
        actor: str = Query(default="human"),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        cp.delete_agent(agent_id, actor=actor)
        return {"deleted": agent_id}

    # Agent roles (persona catalog) ---------------------------------

    @app.post("/roles")
    def create_role(
        body: RoleCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.roles.create_role(**_data(body)).to_dict()

    @app.get("/roles")
    def list_roles(
        tenant_id: Optional[str] = Query(default=None),
        level: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [
            role.to_dict()
            for role in cp.roles.list_roles(tenant_id=tenant_id, level=level)
        ]

    @app.get("/roles/{role_id_or_slug}")
    def get_role(role_id_or_slug: str) -> Dict[str, Any]:
        return cp.roles.get_role(role_id_or_slug).to_dict()

    @app.put("/roles/{role_id}")
    def update_role(
        role_id: str,
        body: RoleUpdate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        role = cp.roles.get_role(role_id)
        principal.assert_tenant(role.tenant_id)
        return cp.roles.update_role(role_id, **_data(body)).to_dict()

    @app.delete("/roles/{role_id}")
    def delete_role(
        role_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        role = cp.roles.get_role(role_id)
        principal.assert_tenant(role.tenant_id)
        cp.roles.delete_role(role_id)
        return {"deleted": role_id}

    @app.post("/roles/seed")
    def seed_roles(
        body: RoleSeed,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> List[Dict[str, Any]]:
        # Global catalog seeding is admin-only (the dispatch in
        # _required_scope already enforces this, but we double-check the
        # principal isn't tenant-bound to avoid stamping global rows from a
        # tenant token if scopes are ever relaxed).
        principal.require_global_fleet()
        return [role.to_dict() for role in cp.roles.seed_defaults(replace=body.replace)]

    @app.post("/agents/{agent_id}/role")
    def assign_role(
        agent_id: str,
        body: RoleAssign,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        return cp.roles.assign_role(agent_id, body.role_id_or_slug).to_dict()

    @app.delete("/agents/{agent_id}/role")
    def unassign_role(
        agent_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        return cp.roles.unassign_role(agent_id).to_dict()

    @app.get("/agents/{agent_id}/identity")
    def get_agent_identity(agent_id: str) -> Dict[str, Any]:
        return cp.agent_identity(agent_id)

    # Agent provisioning hook --------------------------------------

    @app.post("/provisioning/requests")
    def create_provisioning_request(
        body: ProvisioningRequestCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.provisioning.request_agent(**_data(body)).to_dict()

    @app.get("/provisioning/requests")
    def list_provisioning_requests(
        status: Optional[str] = Query(default=None),
        role_slug: Optional[str] = Query(default=None),
        tenant_id: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            request.to_dict()
            for request in cp.provisioning.list_requests(
                status=status,
                role_slug=role_slug,
                tenant_id=tenant_id,
                limit=limit,
            )
        ]

    @app.get("/provisioning/requests/{request_id}")
    def get_provisioning_request(request_id: str) -> Dict[str, Any]:
        return cp.provisioning.get_request(request_id).to_dict()

    @app.post("/provisioning/requests/{request_id}/fulfill")
    def fulfill_provisioning_request(
        request_id: str,
        body: ProvisioningRequestFulfill,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        return cp.provisioning.fulfill_request(request_id, body.agent_id).to_dict()

    @app.post("/provisioning/requests/{request_id}/cancel")
    def cancel_provisioning_request(
        request_id: str,
        body: ProvisioningRequestCancel,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        return cp.provisioning.cancel_request(request_id, reason=body.reason).to_dict()

    # Workflows (data-driven, definable) -----------------------------

    @app.post("/workflows")
    def create_workflow(
        body: WorkflowCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.workflows.create_workflow(**_data(body)).to_dict()

    @app.get("/workflows")
    def list_workflows(
        tenant_id: Optional[str] = Query(default=None),
        workflow_type: Optional[str] = Query(default=None),
        enabled: Optional[bool] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [
            wf.to_dict()
            for wf in cp.workflows.list_workflows(
                tenant_id=tenant_id,
                workflow_type=workflow_type,
                enabled=enabled,
            )
        ]

    @app.post("/workflows/preview")
    def preview_workflow_definition(
        body: WorkflowPreview,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        if body.tenant_id is not None:
            principal.assert_tenant(body.tenant_id)
        if body.definition is None:
            raise ValidationError("workflow preview requires definition")
        return cp.preview_workflow_definition(
            body.definition,
            tenant_id=body.tenant_id,
            input=body.input,
        )

    @app.post("/workflows/drafts")
    def create_workflow_draft(
        body: WorkflowDraftCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.create_workflow_draft(**_data(body)).to_dict()

    @app.get("/workflows/drafts")
    def list_workflow_drafts(
        tenant_id: Optional[str] = Query(default=None),
        status: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            draft.to_dict()
            for draft in cp.list_workflow_drafts(
                tenant_id=tenant_id,
                status=status,
                limit=limit,
            )
        ]

    @app.get("/workflows/drafts/{draft_id}")
    def get_workflow_draft(draft_id: str) -> Dict[str, Any]:
        return cp.get_workflow_draft(draft_id).to_dict()

    @app.put("/workflows/drafts/{draft_id}")
    def update_workflow_draft(
        draft_id: str,
        body: WorkflowDraftUpdate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        draft = cp.get_workflow_draft(draft_id)
        principal.assert_tenant(draft.tenant_id)
        return cp.update_workflow_draft(draft_id, **_data(body)).to_dict()

    @app.post("/workflows/drafts/{draft_id}/preview")
    def preview_workflow_draft(
        draft_id: str,
        body: WorkflowPreview,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        draft = cp.get_workflow_draft(draft_id)
        principal.assert_tenant(draft.tenant_id)
        return cp.preview_workflow_draft(draft_id, input=body.input)

    @app.post("/workflows/drafts/{draft_id}/approve")
    def approve_workflow_draft(
        draft_id: str,
        body: WorkflowDraftApprove,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        draft = cp.get_workflow_draft(draft_id)
        principal.assert_tenant(draft.tenant_id)
        return cp.approve_workflow_draft(draft_id, **_data(body)).to_dict()

    @app.get("/workflows/runs")
    def list_workflow_runs(
        state: Optional[str] = Query(default=None),
        workflow_id: Optional[str] = Query(default=None),
        tenant_id: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            run.to_dict()
            for run in cp.workflow_runtime.list_runs(
                state=state, workflow_id=workflow_id, tenant_id=tenant_id, limit=limit
            )
        ]

    @app.get("/workflows/{workflow_id_or_slug}")
    def get_workflow(workflow_id_or_slug: str) -> Dict[str, Any]:
        return cp.workflows.get_workflow(workflow_id_or_slug).to_dict()

    @app.post("/workflows/{workflow_id_or_slug}/preview")
    def preview_workflow(
        workflow_id_or_slug: str,
        body: WorkflowPreview,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        if body.tenant_id is not None:
            principal.assert_tenant(body.tenant_id)
        return cp.preview_workflow(
            workflow_id_or_slug,
            tenant_id=body.tenant_id,
            input=body.input,
        )

    @app.put("/workflows/{workflow_id}")
    def update_workflow(
        workflow_id: str,
        body: WorkflowUpdate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        wf = cp.workflows.get_workflow(workflow_id)
        principal.assert_tenant(wf.tenant_id)
        return cp.workflows.update_workflow(workflow_id, **_data(body)).to_dict()

    @app.delete("/workflows/{workflow_id}")
    def delete_workflow(
        workflow_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        wf = cp.workflows.get_workflow(workflow_id)
        principal.assert_tenant(wf.tenant_id)
        cp.workflows.delete_workflow(workflow_id)
        return {"deleted": workflow_id}

    @app.post("/workflows/import-yaml")
    def import_workflow_yaml(
        body: WorkflowImportYaml,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.workflows.import_yaml(
            body.yaml,
            created_by=body.created_by,
            tenant_id=body.tenant_id,
            is_default=body.is_default,
        ).to_dict()

    @app.post("/workflows/seed")
    def seed_workflows(
        body: WorkflowSeed,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> List[Dict[str, Any]]:
        principal.require_global_fleet()
        return [wf.to_dict() for wf in cp.workflows.seed_defaults()]

    @app.post("/workflows/{workflow_id_or_slug}/start")
    def start_workflow_run(
        workflow_id_or_slug: str,
        body: WorkflowStart,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.workflow_runtime.start_run(
            workflow_id_or_slug,
            started_by=body.started_by,
            input=body.input,
            tenant_id=body.tenant_id,
            pre_decisions=body.pre_decisions or None,
        ).to_dict()

    @app.get("/workflows/runs/{run_id}")
    def get_workflow_run(run_id: str) -> Dict[str, Any]:
        return cp.workflow_runtime.get_run(run_id).to_dict()

    @app.post("/workflows/runs/{run_id}/cancel")
    def cancel_workflow_run(
        run_id: str,
        body: WorkflowCancel,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        return cp.workflow_runtime.cancel_run(
            run_id, reason=body.reason, actor=body.actor
        ).to_dict()

    @app.post("/workflows/runs/tick")
    def tick_workflow_runs(
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> List[Dict[str, Any]]:
        return [run.to_dict() for run in cp.workflow_runtime.tick()]

    # wf-02: decisions inventory — enumerate every approval-node gate
    # in a workflow (or a live run) so a human can preview all the
    # input the system will need.
    @app.get("/workflows/{workflow_id_or_slug}/decisions")
    def workflow_decisions(
        workflow_id_or_slug: str,
        tenant_id: Optional[str] = Query(default=None),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        return cp.workflow_decisions(workflow_id_or_slug, tenant_id=tenant_id)

    @app.get("/workflows/runs/{run_id}/decisions")
    def workflow_run_decisions(
        run_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        return cp.workflow_run_decisions(run_id)

    @app.get("/fleet/build-distribution")
    def fleet_build_distribution() -> Dict[str, Any]:
        return cp.fleet_build_distribution()

    # Mood — agent-self-reported emotional state
    @app.put("/agents/{agent_id}/mood")
    @app.post("/agents/{agent_id}/mood")
    def set_mood(agent_id: str, body: MoodSet) -> Dict[str, Any]:
        return cp.set_mood(agent_id, **_data(body)).to_dict()

    @app.get("/agents/{agent_id}/mood")
    def get_mood(agent_id: str) -> Optional[Dict[str, Any]]:
        overlay = cp.get_current_mood(agent_id)
        return overlay.to_dict() if overlay is not None else None

    @app.delete("/agents/{agent_id}/mood")
    def clear_mood(agent_id: str, body: MoodClear) -> Optional[Dict[str, Any]]:
        cleared = cp.clear_mood(agent_id, **_data(body))
        return cleared.to_dict() if cleared is not None else None

    @app.get("/agents/{agent_id}/mood/history")
    def list_mood_history(agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return [overlay.to_dict() for overlay in cp.list_mood_history(agent_id, limit=limit)]

    # Nap — daily memory-consolidation lifecycle
    @app.put("/agents/{agent_id}/nap-schedule")
    @app.post("/agents/{agent_id}/nap-schedule")
    def configure_nap(agent_id: str, body: NapConfigure) -> Dict[str, Any]:
        return cp.configure_nap(agent_id, **_data(body)).to_dict()

    @app.get("/agents/{agent_id}/nap-schedule")
    def get_nap_schedule(agent_id: str) -> Optional[Dict[str, Any]]:
        schedule = cp.get_nap_schedule(agent_id)
        return schedule.to_dict() if schedule is not None else None

    @app.get("/agents/{agent_id}/nap-schedule/next")
    def next_nap_window(agent_id: str) -> Optional[Dict[str, Any]]:
        return cp.next_nap_window(agent_id)

    @app.get("/nap-schedules")
    def list_nap_schedules() -> List[Dict[str, Any]]:
        return [schedule.to_dict() for schedule in cp.list_nap_schedules()]

    @app.post("/agents/{agent_id}/nap-runs")
    def begin_nap(agent_id: str, body: NapBegin) -> Dict[str, Any]:
        return cp.begin_nap(agent_id, **_data(body)).to_dict()

    @app.get("/nap-runs")
    def list_nap_runs(agent_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [run.to_dict() for run in cp.list_nap_runs(agent_id)]

    @app.get("/nap-runs/{run_id}")
    def get_nap_run(run_id: str) -> Dict[str, Any]:
        return cp.get_nap_run(run_id).to_dict()

    @app.post("/nap-runs/{run_id}/complete")
    def complete_nap(run_id: str, body: NapComplete) -> Dict[str, Any]:
        return cp.complete_nap(run_id, **_data(body)).to_dict()

    @app.post("/nap-runs/{run_id}/fail")
    def fail_nap(run_id: str, body: NapFail) -> Dict[str, Any]:
        return cp.fail_nap(run_id, **_data(body)).to_dict()

    @app.post("/agents/{agent_id}/heartbeat")
    def heartbeat_agent(
        agent_id: str,
        body: HeartbeatRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # mac-wcfy: heartbeat/claim-next/command-audit took agent_id
        # from the URL with no check that the token represents that
        # agent. Any agent-scoped token could heartbeat/claim/audit-log
        # as a peer. Bind to principal.
        principal.assert_actor(agent_id)
        return cp.heartbeat_agent(agent_id, **_data(body)).to_dict()

    @app.post("/agents/{agent_id}/claim-next")
    def claim_next_for_agent(
        agent_id: str,
        body: AgentClaimNextRequest,
        background_tasks: BackgroundTasks,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Optional[Dict[str, Any]]:
        principal.assert_actor(agent_id)  # mac-wcfy
        assignment = cp.claim_next_for_agent(
            agent_id,
            lease_seconds=body.lease_seconds,
            allowed_projects=body.allowed_projects,
            required_metadata=body.required_metadata,
            require_canary=body.require_canary,
            dry_run=body.dry_run,
            sync_beads=False,
        )
        if assignment and not body.dry_run:
            task = assignment["task"]
            lease = assignment["lease"]
            background_tasks.add_task(
                cp.sync_claim_side_effects,
                task["id"],
                agent_id,
                lease["id"],
                lease["expires_at"],
            )
        return assignment

    @app.post("/agents/{agent_id}/command-audit")
    def record_agent_command_audit(
        agent_id: str,
        body: CommandAuditCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_actor(agent_id)  # mac-wcfy
        return cp.record_command_audit(agent_id=agent_id, **_data(body)).to_dict()

    @app.get("/command-audit")
    def list_command_audit(
        agent_id: Optional[str] = Query(default=None),
        task_id: Optional[str] = Query(default=None),
        command_id: Optional[str] = Query(default=None),
        phase: Optional[str] = Query(default=None),
        since: Optional[str] = Query(default=None),
        until: Optional[str] = Query(default=None),
        limit: int = Query(default=200),
    ) -> List[Dict[str, Any]]:
        return [
            record.to_dict()
            for record in cp.list_command_audit(
                agent_id=agent_id,
                task_id=task_id,
                command_id=command_id,
                phase=phase,
                since=since,
                until=until,
                limit=limit,
            )
        ]

    @app.get("/agents/{agent_id}/command-audit")
    def list_agent_command_audit(
        agent_id: str,
        task_id: Optional[str] = Query(default=None),
        phase: Optional[str] = Query(default=None),
        limit: int = Query(default=200),
    ) -> List[Dict[str, Any]]:
        return [
            record.to_dict()
            for record in cp.list_command_audit(
                agent_id=agent_id,
                task_id=task_id,
                phase=phase,
                limit=limit,
            )
        ]

    @app.post("/dispatch/assign")
    def dispatch_once(body: DispatchRequest) -> Optional[Dict[str, Any]]:
        return cp.dispatch_once(body.lease_seconds)

    @app.post("/dispatch/tick")
    def dispatch_tick(body: DispatchRequest) -> Dict[str, Any]:
        return cp.tick(body.lease_seconds, body.limit, body.stale_after_seconds)

    @app.get("/dispatch/dead-letters")
    def dead_letters(tenant_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [task.to_dict() for task in cp.list_dead_letters(tenant_id)]

    @app.get("/events")
    def list_events(
        subject_type: Optional[str] = Query(default=None),
        subject_id: Optional[str] = Query(default=None),
        actor: Optional[str] = Query(default=None),
        event_type: Optional[str] = Query(default=None),
        event_type_prefix: Optional[str] = Query(default=None),
        since: Optional[str] = Query(default=None),
        until: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return cp.list_events(
            subject_type=subject_type,
            subject_id=subject_id,
            actor=actor,
            event_type=event_type,
            event_type_prefix=event_type_prefix,
            since=since,
            until=until,
            limit=limit,
        )

    @app.post("/observability/metrics")
    def record_observability_metric(body: ObservabilityMetricCreate) -> Dict[str, Any]:
        return cp.record_metric(**_data(body)).to_dict()

    @app.post("/observability/logs")
    def record_observability_log(body: ObservabilityLogCreate) -> Dict[str, Any]:
        return cp.record_log(**_data(body)).to_dict()

    @app.get("/observability/metrics")
    def list_observability_metrics(
        layer: Optional[str] = Query(default=None),
        name: Optional[str] = Query(default=None),
        subject_type: Optional[str] = Query(default=None),
        subject_id: Optional[str] = Query(default=None),
        since: Optional[str] = Query(default=None),
        until: Optional[str] = Query(default=None),
        after_sequence: Optional[int] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            event.to_dict()
            for event in cp.list_observability(
                kind="metric",
                layer=layer,
                name=name,
                subject_type=subject_type,
                subject_id=subject_id,
                since=since,
                until=until,
                after_sequence=after_sequence,
                limit=limit,
            )
        ]

    @app.get("/observability/logs")
    def list_observability_logs(
        layer: Optional[str] = Query(default=None),
        level: Optional[str] = Query(default=None),
        name: Optional[str] = Query(default=None),
        subject_type: Optional[str] = Query(default=None),
        subject_id: Optional[str] = Query(default=None),
        since: Optional[str] = Query(default=None),
        until: Optional[str] = Query(default=None),
        after_sequence: Optional[int] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            event.to_dict()
            for event in cp.list_observability(
                kind="log",
                layer=layer,
                level=level,
                name=name,
                subject_type=subject_type,
                subject_id=subject_id,
                since=since,
                until=until,
                after_sequence=after_sequence,
                limit=limit,
            )
        ]

    @app.get("/observability")
    def list_observability(
        kind: Optional[str] = Query(default=None),
        layer: Optional[str] = Query(default=None),
        level: Optional[str] = Query(default=None),
        name: Optional[str] = Query(default=None),
        subject_type: Optional[str] = Query(default=None),
        subject_id: Optional[str] = Query(default=None),
        since: Optional[str] = Query(default=None),
        until: Optional[str] = Query(default=None),
        after_sequence: Optional[int] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            event.to_dict()
            for event in cp.list_observability(
                kind=kind,
                layer=layer,
                level=level,
                name=name,
                subject_type=subject_type,
                subject_id=subject_id,
                since=since,
                until=until,
                after_sequence=after_sequence,
                limit=limit,
            )
        ]

    @app.get("/notifications")
    def list_notifications(
        status: Optional[str] = Query(default=None),
        subject_type: Optional[str] = Query(default=None),
        subject_id: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            notification.to_dict()
            for notification in cp.list_notifications(
                status=status,
                subject_type=subject_type,
                subject_id=subject_id,
                limit=limit,
            )
        ]

    @app.get("/integrations/findings")
    def list_integration_findings(
        source_kind: Optional[str] = Query(default=None),
        source_id: Optional[str] = Query(default=None),
        finding_type: Optional[str] = Query(default=None),
        status: Optional[str] = Query(default=None),
        severity: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            finding.to_dict()
            for finding in cp.list_integration_findings(
                source_kind=source_kind,
                source_id=source_id,
                finding_type=finding_type,
                status=status,
                severity=severity,
                limit=limit,
            )
        ]

    @app.get("/integrations/observations")
    def list_integration_observations(
        source_kind: Optional[str] = Query(default=None),
        source_id: Optional[str] = Query(default=None),
        authority: Optional[str] = Query(default=None),
        status: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            observation.to_dict()
            for observation in cp.list_integration_observations(
                source_kind=source_kind,
                source_id=source_id,
                authority=authority,
                status=status,
                limit=limit,
            )
        ]

    @app.post("/notifications/{notification_id}/delivered")
    def mark_notification_delivered(
        notification_id: str,
        body: NotificationDelivery,
    ) -> Dict[str, Any]:
        return cp.mark_notification_delivered(notification_id, status=body.status).to_dict()

    @app.post("/notifier/channels")
    def configure_notifier_channel(
        body: NotifierChannelConfig,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.configure_notifier_channel(**_data(body)).to_dict()

    @app.get("/notifier/channels")
    def list_notifier_channels(
        enabled: Optional[bool] = Query(default=None),
        channel_type: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [
            channel.to_dict()
            for channel in cp.list_notifier_channels(
                enabled=enabled,
                channel_type=channel_type,
            )
        ]

    @app.get("/notifier/channels/{channel_id_or_name}")
    def get_notifier_channel(channel_id_or_name: str) -> Dict[str, Any]:
        return cp.get_notifier_channel(channel_id_or_name).to_dict()

    @app.delete("/notifier/channels/{channel_id_or_name}")
    def delete_notifier_channel(
        channel_id_or_name: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        cp.delete_notifier_channel(channel_id_or_name)
        return {"deleted": channel_id_or_name}

    @app.post("/notifier/deliver")
    def deliver_notifications(
        body: NotifierDeliveryRun,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.deliver_pending_notifications(
            limit=body.limit,
            notification_id=body.notification_id,
        )

    @app.get("/observability/summary")
    def observability_summary(limit: int = Query(default=80)) -> Dict[str, Any]:
        return cp.observability_summary(limit)

    @app.get("/observability/stream")
    async def observability_stream(
        request: Request,
        after_sequence: int = Query(default=0),
        timeout_seconds: float = Query(default=30.0),
        poll_interval_seconds: float = Query(default=0.5),
        kind: Optional[str] = Query(default=None),
        layer: Optional[str] = Query(default=None),
        level: Optional[str] = Query(default=None),
    ) -> StreamingResponse:
        cp.list_observability(
            kind=kind,
            layer=layer,
            level=level,
            after_sequence=max(0, int(after_sequence)),
            limit=1,
        )

        async def iter_observations() -> Any:
            cursor = max(0, int(after_sequence))
            deadline = time.monotonic() + _agentbus_clamp_timeout(timeout_seconds)
            poll_interval = _agentbus_clamp_poll_interval(poll_interval_seconds)
            while True:
                # mac-ob1m: abort cleanly when the client has gone away
                # so a slow-or-abandoned consumer doesn't keep issuing
                # DB queries until deadline.
                if await request.is_disconnected():
                    break
                observations = cp.list_observability(
                    kind=kind,
                    layer=layer,
                    level=level,
                    after_sequence=cursor,
                    limit=100,
                )
                for observation in observations:
                    cursor = observation.sequence
                    yield json.dumps(observation.to_dict(), sort_keys=True) + "\n"
                if observations:
                    if time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(0)
                    continue
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(poll_interval)

        return StreamingResponse(iter_observations(), media_type="application/x-ndjson")

    @app.post("/messages")
    def send_message(
        body: MessageCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # mac-kgi5: bind sender to the principal.
        principal.assert_actor(body.sender_agent_id)
        return cp.send_message(**_data(body)).to_dict()

    @app.get("/messages")
    def list_messages(
        agent_id: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None, ge=1, le=1000),
    ) -> List[Dict[str, Any]]:
        return [message.to_dict() for message in cp.list_messages(agent_id, limit=limit)]

    @app.post("/agents/{agent_id}/messages/deliver")
    def deliver_messages(agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return [message.to_dict() for message in cp.deliver_messages(agent_id, limit)]

    @app.post("/agentbus/streams")
    def open_agentbus_stream(
        body: AgentBusOpen,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # mac-kgi5
        principal.assert_actor(body.sender_agent_id)
        return cp.open_agentbus_stream(**_data(body)).to_dict()

    @app.get("/agentbus/streams")
    def list_agentbus_streams(
        agent_id: Optional[str] = Query(default=None),
        status: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            stream.to_dict()
            for stream in cp.list_agentbus_streams(agent_id=agent_id, status=status, limit=limit)
        ]

    @app.post("/agentbus/streams/{stream_id}/chunks")
    def append_agentbus_chunk(
        stream_id: str,
        body: AgentBusAppend,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # mac-kgi5
        principal.assert_actor(body.sender_agent_id)
        return cp.append_agentbus_chunk(stream_id, **_data(body)).to_dict()

    @app.get("/agentbus/streams/{stream_id}/chunks")
    def read_agentbus_chunks(
        stream_id: str,
        agent_id: str,
        after_sequence: int = Query(default=0),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            chunk.to_dict()
            for chunk in cp.read_agentbus_chunks(agent_id, stream_id, after_sequence, limit)
        ]

    @app.post("/agentbus/streams/{stream_id}/close")
    def close_agentbus_stream(
        stream_id: str,
        sender_agent_id: str,
        status: str = "closed",
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # mac-kgi5
        principal.assert_actor(sender_agent_id)
        return cp.close_agentbus_stream(stream_id, sender_agent_id, status).to_dict()

    @app.post("/agentbus")
    def publish_agentbus_content(
        body: AgentBusPublish,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # mac-kgi5
        principal.assert_actor(body.sender_agent_id)
        return cp.publish_agentbus_content(**_data(body))

    @app.post("/agentbus/repo-update")
    def publish_agentbus_repo_update(
        body: AgentBusRepoUpdate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # mac-si4l: /agentbus/repo-update is a fleet-wide restart
        # primitive. Require admin scope OR a sender that matches the
        # principal's agent_id binding. Combined, this means a single
        # compromised agent token can't broadcast restart=true to every
        # peer; only an admin or the bound agent's own future-self can.
        if not principal.is_admin:
            principal.assert_actor(body.sender_agent_id)
            if body.all_agents:
                raise AuthorizationError(
                    "fleet-wide repo-update (all_agents=true) requires admin scope"
                )
        recipients = list(body.recipient_agent_ids)
        if body.all_agents:
            recipients.extend(agent.id for agent in cp.list_agents())
        recipients = list(dict.fromkeys(item for item in recipients if item))
        if not recipients:
            raise ValidationError("repo-update requires recipient_agent_ids or all_agents=true")
        payload = repo_update_payload(
            repo_path=body.repo_path,
            remote=body.remote,
            branch=body.branch,
            restart=body.restart,
            request_id=body.request_id,
        )
        published = [
            cp.publish_agentbus_content(
                sender_agent_id=body.sender_agent_id,
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

    @app.get("/agentbus/streams/{stream_id}/events")
    async def agentbus_stream_events(
        stream_id: str,
        request: Request,
        agent_id: str,
        after_sequence: int = Query(default=0),
        timeout_seconds: float = Query(default=30.0),
        poll_interval_seconds: float = Query(default=0.25),
    ) -> StreamingResponse:
        # Authorize before we start streaming so denials surface as proper HTTP
        # errors rather than a half-open response.
        cp.assert_agentbus_authorized(agent_id, stream_id)

        async def iter_events() -> Any:
            cursor = max(0, int(after_sequence))
            deadline = time.monotonic() + _agentbus_clamp_timeout(timeout_seconds)
            poll_interval = _agentbus_clamp_poll_interval(poll_interval_seconds)
            while True:
                # mac-ob1m: stop polling when the client has disconnected.
                if await request.is_disconnected():
                    break
                chunks = cp.read_agentbus_chunks(agent_id, stream_id, cursor, limit=100)
                for chunk in chunks:
                    cursor = chunk.sequence
                    yield json.dumps(chunk.to_dict(), sort_keys=True) + "\n"
                if chunks:
                    if time.monotonic() >= deadline:
                        break
                    # Yield control between batches so we don't starve the event
                    # loop while draining a backlog.
                    await asyncio.sleep(0)
                    continue
                if cp.get_agentbus_stream(stream_id).status != "open":
                    break
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(poll_interval)

        return StreamingResponse(iter_events(), media_type="application/x-ndjson")

    @app.post("/tasks/{task_id}/reviews")
    def request_review(task_id: str, body: ReviewRequest) -> Dict[str, Any]:
        return cp.request_review(task_id, body.reviewer_agent_id, body.actor).to_dict()

    @app.post("/reviews/{review_id}/claim")
    def claim_review(
        review_id: str,
        body: ReviewClaim,
        background_tasks: BackgroundTasks,
    ) -> Dict[str, Any]:
        claim = cp.claim_review(
            review_id,
            body.reviewer_agent_id,
            executor_evidence_id=body.executor_evidence_id,
            actor=body.actor,
            sync_beads=False,
        )
        if claim.get("status") == "claimed":
            task = claim.get("task") if isinstance(claim.get("task"), dict) else {}
            background_tasks.add_task(
                cp.sync_review_claim_side_effects,
                str(task.get("id") or ""),
                review_id,
                body.reviewer_agent_id,
            )
        return claim

    @app.post("/reviews/{review_id}/decision")
    def submit_review(review_id: str, body: ReviewDecision) -> Dict[str, Any]:
        return cp.submit_review(review_id, **_data(body)).to_dict()

    @app.post("/reviews/default/tick")
    def default_review_tick(
        limit: int = Query(default=100),
        actor: str = Query(default="operator"),
        tenant_id: Optional[str] = Query(default=None),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # Admin scope is required by _required_scope. If the caller is
        # tenant-bound (rare for admin tokens but supported), force the
        # filter to their tenant so a misconfigured admin can't sweep
        # across tenant boundaries (mac-dyk).
        if principal.tenant_id is not None and tenant_id is None:
            tenant_id = principal.tenant_id
        if (
            principal.tenant_id is not None
            and tenant_id is not None
            and tenant_id != principal.tenant_id
        ):
            principal.assert_tenant(tenant_id)
        return cp.advance_default_review_workflows(
            limit=limit, actor=actor, tenant_id=tenant_id
        )

    @app.post("/publications")
    def publish(body: PublicationCreate) -> Dict[str, Any]:
        return cp.publish_task(**_data(body)).to_dict()

    def _assert_secret_tenant(principal: TokenPrincipal, secret_id: str) -> None:
        # mac-01g0: every /secrets/* operation must be tenant-isolated.
        # Derive the tenant set from the secret's scopes; refuse when
        # the principal's tenant doesn't match. Admin / unbound tokens
        # bypass the check.
        if principal.is_admin or principal.tenant_id is None:
            return
        secret = cp.get_secret(secret_id)
        scopes = secret.scopes if isinstance(secret.scopes, dict) else {}
        tenant_ids = set(scopes.get("tenant_ids") or [])
        if scopes.get("tenant_id"):
            tenant_ids.add(str(scopes["tenant_id"]))
        # An unscoped secret (no tenant) is "global" — tenant-bound
        # tokens may not touch it.
        if not tenant_ids or principal.tenant_id not in tenant_ids:
            raise AuthorizationError(
                "secret %s is not scoped to token tenant %s"
                % (secret_id, principal.tenant_id)
            )

    # mac-xc8u: simple per-principal sliding-window rate limit for
    # secret-reveal/access calls. A compromised token cannot enumerate
    # every (secret_id, audit_id) pair at line rate. The window is in
    # memory and per-process; multi-process deployments will need a
    # shared store, but for the current single-process hub this is the
    # right blast-radius reduction with no schema change.
    secret_rate_state: Dict[str, list] = {}

    def _enforce_secret_rate_limit(principal: TokenPrincipal, route: str) -> None:
        import collections
        max_calls = 30
        window_seconds = 60
        # admin tokens are operator-driven and unlikely to enumerate
        if principal.is_admin:
            return
        key = "%s::%s::%s" % (principal.tenant_id or "-", principal.agent_id or "-", route)
        bucket = secret_rate_state.setdefault(key, [])
        now_t = time.monotonic()
        cutoff = now_t - window_seconds
        # prune
        secret_rate_state[key] = [t for t in bucket if t >= cutoff]
        if len(secret_rate_state[key]) >= max_calls:
            raise AuthorizationError(
                "secret %s rate limit exceeded for token: %d calls in %ds"
                % (route, max_calls, window_seconds)
            )
        secret_rate_state[key].append(now_t)

    @app.post("/secrets")
    def create_secret(
        body: SecretCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # mac-01g0: bind create_secret to the principal's tenant if any.
        scopes = body.scopes if isinstance(body.scopes, dict) else {}
        target_tenant = scopes.get("tenant_id")
        if target_tenant is None and scopes.get("tenant_ids"):
            tenants = list(scopes.get("tenant_ids") or [])
            target_tenant = tenants[0] if tenants else None
        if target_tenant is not None:
            principal.assert_tenant(str(target_tenant))
        return cp.create_secret(**_data(body)).to_dict()

    @app.get("/secrets")
    def list_secrets() -> List[Dict[str, Any]]:
        return [secret.to_dict() for secret in cp.list_secrets()]

    @app.post("/secrets/{secret_id}/access")
    def request_secret(
        secret_id: str,
        body: SecretAccessRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _enforce_secret_rate_limit(principal, "access")  # mac-xc8u
        # mac-k30g: accessor_agent_id was self-asserted in the body. A
        # token bound to a specific agent must not be able to request
        # secrets as a different agent.
        principal.assert_actor(body.accessor_agent_id)
        _assert_secret_tenant(principal, secret_id)
        return cp.request_secret(
            secret_id,
            body.accessor_agent_id,
            body.purpose,
            body.ttl_seconds,
        ).to_dict()

    @app.post("/secrets/{secret_id}/reveal")
    def reveal_secret(
        secret_id: str,
        body: SecretRevealRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _enforce_secret_rate_limit(principal, "reveal")  # mac-xc8u
        # mac-k30g: same — reveal must be bound to the actor the token
        # represents. With no agent_id binding the call still works
        # (operator/admin), but a fleet token cannot impersonate a peer.
        principal.assert_actor(body.accessor_agent_id)
        _assert_secret_tenant(principal, secret_id)
        return {
            "secret_id": secret_id,
            "value": cp.reveal_secret(secret_id, body.audit_id, body.accessor_agent_id),
        }

    @app.get("/secret-audits")
    def list_secret_audits(secret_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [audit.to_dict() for audit in cp.list_secret_audits(secret_id)]

    @app.post("/artifacts")
    def register_artifact(body: ArtifactRegister) -> Dict[str, Any]:
        return cp.register_artifact(**_data(body)).to_dict()

    @app.get("/artifacts")
    def list_artifacts(kind: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [artifact.to_dict() for artifact in cp.list_artifacts(kind)]

    @app.get("/artifacts/{artifact_id_or_digest}")
    def get_artifact(artifact_id_or_digest: str) -> Dict[str, Any]:
        return cp.get_artifact(artifact_id_or_digest).to_dict()

    @app.post("/conversation-threads")
    def track_conversation(body: ConversationThreadTrack) -> Dict[str, Any]:
        return cp.track_conversation(**_data(body)).to_dict()

    @app.get("/conversation-threads")
    def list_conversation_threads(
        platform_binding_id: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [thread.to_dict() for thread in cp.list_conversation_threads(platform_binding_id)]

    @app.get("/conversation-threads/{thread_id}")
    def get_conversation_thread(thread_id: str) -> Dict[str, Any]:
        return cp.get_conversation_thread(thread_id).to_dict()

    @app.post("/vector-refs")
    def record_vector_ref(body: VectorRefRecord) -> Dict[str, Any]:
        return cp.record_vector_ref(**_data(body)).to_dict()

    @app.get("/vector-refs")
    def list_vector_refs(
        memory_id: Optional[str] = Query(default=None),
        vector_db: Optional[str] = Query(default=None),
        collection: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [
            ref.to_dict()
            for ref in cp.list_vector_refs(memory_id, vector_db, collection)
        ]

    @app.post("/environments")
    def register_environment(
        body: EnvironmentRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.register_environment(**_data(body)).to_dict()

    @app.get("/environments")
    def list_environments(
        tenant_id: Optional[str] = Query(default=None),
        channel: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [env.to_dict() for env in cp.list_environments(tenant_id, channel)]

    @app.get("/environments/{env_id}")
    def get_environment(env_id: str) -> Dict[str, Any]:
        return cp.get_environment(env_id).to_dict()

    @app.post("/environments/{env_id}/deploy")
    def deploy_artifact(env_id: str, body: DeploymentCreate) -> Dict[str, Any]:
        return cp.deploy_artifact(env_id, body.artifact_id, body.actor, body.metadata).to_dict()

    @app.get("/environments/{env_id}/current")
    def current_deployment(env_id: str) -> Optional[Dict[str, Any]]:
        current = cp.current_deployment(env_id)
        return current.to_dict() if current is not None else None

    @app.get("/environments/{env_id}/deployments")
    def list_deployments(env_id: str) -> List[Dict[str, Any]]:
        return [d.to_dict() for d in cp.list_deployments(env_id)]

    @app.post("/runtimes")
    def create_runtime(
        body: RuntimeCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.create_runtime(**_data(body)).to_dict()

    @app.get("/runtimes")
    def list_runtimes() -> List[Dict[str, Any]]:
        return [runtime.to_dict() for runtime in cp.list_runtimes()]

    @app.post("/runtime-runs")
    def create_runtime_run(body: RuntimeRunCreate) -> Dict[str, Any]:
        return cp.create_runtime_run(**_data(body)).to_dict()

    @app.post("/runtime-runs/{run_id}/complete")
    def complete_runtime_run(run_id: str, body: RuntimeRunComplete) -> Dict[str, Any]:
        return cp.complete_runtime_run(run_id, body.evidence_id, body.status).to_dict()

    @app.post("/bridge/items")
    def import_project_item(body: ProjectImport) -> Dict[str, Any]:
        return cp.import_project_item(**_data(body)).to_dict()

    @app.get("/bridge/items")
    def list_project_items() -> List[Dict[str, Any]]:
        return [item.to_dict() for item in cp.list_project_items()]

    @app.post("/bridge/beads/repositories")
    def register_beads_repository(body: BeadsRepositoryRegister) -> Dict[str, Any]:
        return cp.register_beads_repository(**_data(body)).to_dict()

    @app.get("/bridge/beads/repositories")
    def list_beads_repositories(enabled: Optional[bool] = Query(default=None)) -> List[Dict[str, Any]]:
        return [repo.to_dict() for repo in cp.list_beads_repositories(enabled=enabled)]

    @app.post("/bridge/beads/poll")
    def poll_beads_repositories(body: BeadsRepositoryPoll) -> Dict[str, Any]:
        return cp.poll_beads_repositories(body.repository, force=body.force, actor=body.actor)

    @app.post("/bridge/beads/repositories/{repo_id_or_name}/repair")
    def repair_beads_repository(repo_id_or_name: str, body: BeadsRepositoryRepair) -> Dict[str, Any]:
        return cp.repair_beads_repository(repo_id_or_name, actor=body.actor, poll_after=body.poll_after)

    @app.post("/memory")
    def add_memory(body: MemoryCreate) -> Dict[str, Any]:
        data = _data(body)
        data.setdefault("evidence_id", None)
        return cp.add_memory(**data).to_dict()

    @app.get("/memory")
    def search_memory(
        task_id: Optional[str] = Query(default=None),
        subject_type: Optional[str] = Query(default=None),
        subject_id: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [record.to_dict() for record in cp.search_memory(task_id, subject_type, subject_id)]

    @app.post("/eval-sets")
    def create_eval_set(body: EvalSetCreate) -> Dict[str, Any]:
        return cp.create_eval_set(**_data(body)).to_dict()

    @app.get("/eval-sets")
    def list_eval_sets() -> List[Dict[str, Any]]:
        return [eval_set.to_dict() for eval_set in cp.list_eval_sets()]

    @app.get("/eval-sets/{eval_set_id}")
    def get_eval_set(eval_set_id: str) -> Dict[str, Any]:
        return cp.get_eval_set(eval_set_id).to_dict()

    @app.post("/eval-sets/{eval_set_id}/baseline")
    def update_eval_set_baseline(eval_set_id: str, body: EvalSetBaselineUpdate) -> Dict[str, Any]:
        return cp.update_eval_set_baseline(eval_set_id, body.baseline_score, body.actor).to_dict()

    @app.get("/eval-sets/{eval_set_id}/events")
    def list_eval_set_events(eval_set_id: str) -> List[Dict[str, Any]]:
        return cp.list_eval_set_events(eval_set_id)

    @app.post("/eval-runs")
    def record_eval_run(body: EvalRunRecord) -> Dict[str, Any]:
        data = _data(body)
        eval_set_id = data.pop("eval_set_id")
        return cp.record_eval_run(eval_set_id, **data).to_dict()

    @app.get("/eval-runs")
    def list_eval_runs(
        eval_set_id: Optional[str] = Query(default=None),
        target_id: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [run.to_dict() for run in cp.list_eval_runs(eval_set_id, target_id)]

    @app.post("/rollouts")
    def create_rollout(
        body: RolloutCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.create_rollout(**_data(body)).to_dict()

    @app.get("/rollouts")
    def list_rollouts(
        tenant_id: Optional[str] = Query(default=None),
        channel: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [rollout.to_dict() for rollout in cp.list_rollouts(tenant_id, channel)]

    @app.post("/rollouts/{rollout_id}/advance")
    def advance_rollout(rollout_id: str, body: RolloutAdvance) -> Dict[str, Any]:
        return cp.advance_rollout(rollout_id, body.action, body.actor, body.detail).to_dict()

    @app.post("/rollouts/{rollout_id}/artifact")
    def verify_rollout_artifact(rollout_id: str, body: RolloutArtifactVerify) -> Dict[str, Any]:
        return cp.verify_rollout_artifact(
            rollout_id,
            body.artifact_uri,
            body.artifact_hash,
            body.actor,
        ).to_dict()

    @app.post("/rollouts/{rollout_id}/health")
    def evaluate_rollout_health(rollout_id: str, body: RolloutHealthReport) -> Dict[str, Any]:
        return cp.evaluate_rollout_health(rollout_id, body.checks, body.actor)

    @app.post("/rollouts/{rollout_id}/rescue")
    def rescue_rollout(rollout_id: str, body: RolloutRescue) -> Dict[str, Any]:
        rollout, task = cp.rescue_rollout(rollout_id, body.actor, body.reason, body.detail)
        return {"rollout": rollout.to_dict(), "task": task.to_dict()}

    return app


# Only build the default app when a secret key is present, so importing the
# module (e.g. from tests) does not require MAC_SECRET_KEY. Run uvicorn with
# `mac.api:create_app --factory` to be explicit, or set MAC_SECRET_KEY before
# `mac.api:app`.
if os.environ.get("MAC_SECRET_KEY"):
    app = create_app()
