"""MAC control-plane HTTP API.

Implements the FastAPI application and request handling that expose the MAC
control plane over HTTP, including authentication, task and agent endpoints, and
the serialization glue between the persistence layer and API clients.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Union

from fastapi import BackgroundTasks, Depends, FastAPI, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, model_validator
from starlette.middleware.gzip import GZipMiddleware

from mac.agentbus_control import (
    DEBUG_TERMINAL_INPUT_CONTENT_TYPE,
    DEBUG_TERMINAL_INPUT_SCHEMA,
    DEBUG_TERMINAL_INPUT_TOPIC,
    DEBUG_TERMINAL_OPEN_CONTENT_TYPE,
    DEBUG_TERMINAL_OPEN_TOPIC,
    DEBUG_TERMINAL_OUTPUT_CONTENT_TYPE,
    DEBUG_TERMINAL_OUTPUT_SCHEMA,
    DEBUG_TERMINAL_OUTPUT_TOPIC,
    HERMES_CONFIG_APPLY_CONTENT_TYPE,
    HERMES_CONFIG_APPLY_TOPIC,
    debug_terminal_input_payload,
    debug_terminal_open_payload,
    hermes_config_apply_payload,
)
from mac.hermes_config_surface import (
    build_hermes_config_surfaces,
    fleet_hermes_payload,
    payload_digest,
    redacted_hermes_payload,
    update_fleet_hermes_surface,
)
from mac.hermes_startup import build_hermes_startup_report
from mac.memory_config import configured_qdrant_url as _configured_qdrant_url
from mac.models import AmbiguousIdError, AuthorizationError, MACError, NotFoundError, ValidationError, new_id, utcnow
from mac.relay_observability import create_agent_scope as _relay_agent_scope
from mac.relay_observability import flush as _relay_flush
from mac.backlog_groomer import BacklogGroomer, BacklogGroomerConfig
from mac.curiosity_reviewer import CuriosityReviewer, CuriosityReviewerConfig
from mac.cicd_monitor import CICDMonitor, CICDMonitorConfig
from mac.pg_backup_scheduler import PgBackupConfig, PgBackupScheduler
from mac.nap_ticker import NapTicker, NapTickerConfig
from mac.self_healing import SelfHealingConfig, SelfHealingSentinel
from mac.model_selection import ModelSelectionConfig, ModelSelectionService
from mac.github_ingest import GitHubIngestConfig, GitHubIssueIngestor
from mac.hgx_autoscaler import HgxAutoscaler, HgxAutoscalerConfig
from mac.http_routes.system import SystemRouteServices, build_system_router
from mac.repository_ref_reconciler import (
    RepositoryRefReconciler,
    RepositoryRefReconcilerConfig,
)
from mac.services import ControlPlane
from mac.store import StoreError, make_store_from_env, open_postgres_store
from mac.work_plan_admission import (
    MANAGED_WORK_PLAN_MODE,
    managed_plan_from_dashboard_accept,
)
from mac.work_package_pipeline_runtime import build_work_package_pipeline_runtime

_log = logging.getLogger(__name__)


def _vector_writer_for_memory(
    cp: ControlPlane, *, enabled: bool, qdrant_url: Optional[str]
) -> Optional[Any]:
    if not enabled:
        return None
    resolved = _configured_qdrant_url(qdrant_url)
    if not resolved:
        return None
    from mac.vector_writer_service import VectorWriterService

    return VectorWriterService(memory=cp.memory, qdrant_url=resolved)


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
    client_id: Optional[str] = None
    principal_kind: Optional[str] = None
    credential_fingerprint: Optional[str] = None
    worker_credential_version: Optional[int] = None
    worker_credential_state: Optional[str] = None
    worker_identity_mode: str = "compatibility"

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

    def require_admin(self) -> None:
        """Require an explicitly admin-scoped principal for host escape hatches."""

        if not self.is_admin:
            raise AuthorizationError("admin scope is required for this operation")

    def assert_actor(
        self,
        claimed_agent_id: str,
        *,
        package_linked: bool = False,
        package_ready: bool = False,
    ) -> None:
        """Bind a request's actor field to the bearer principal.

        mac-rreh / mac-kgi5 / mac-wcfy: callers pass actor identifiers
        (``agent_id``, ``sender_agent_id``, ``accessor_agent_id``,
        ``created_by``) in request bodies / URL paths. Actor-bearing worker
        endpoints require a per-agent credential: neither a shared write token
        nor an admin token may turn a payload string into worker authority.
        Operators use explicit admin/recovery routes; trusted services call the
        private ControlPlane path after their own authority check.
        """
        from mac.worker_credentials import evaluate_worker_actor

        decision = evaluate_worker_actor(
            mode=self.worker_identity_mode,
            principal_agent_id=self.agent_id,
            claimed_agent_id=claimed_agent_id,
            package_linked=package_linked,
            package_ready=package_ready,
        )
        if decision.allowed:
            return
        if decision.reason == "agent_principal_mismatch":
            raise AuthorizationError(
                "token is bound to agent %s and cannot act as %r"
                % (self.agent_id, claimed_agent_id)
            )
        if decision.reason == "package_worker_readiness_required":
            raise AuthorizationError(
                "package-linked work requires a current credential readiness membership"
            )
        if decision.reason == "legacy_worker_package_link_forbidden":
            raise AuthorizationError(
                "legacy worker credentials cannot act on package-linked work"
            )
        raise AuthorizationError(
            "actor-bearing worker endpoint requires an agent-bound token"
        )


AuthTokenMapping = Mapping[str, Union[List[str], Dict[str, Any], TokenPrincipal]]


def _coerce_principal(value: Union[List[str], Dict[str, Any], TokenPrincipal]) -> TokenPrincipal:
    if isinstance(value, TokenPrincipal):
        return value
    if isinstance(value, dict):
        scopes = frozenset(str(s) for s in value.get("scopes", []))
        tenant = value.get("tenant_id")
        agent = value.get("agent_id")
        client = value.get("client_id")
        return TokenPrincipal(
            scopes=scopes,
            tenant_id=tenant,
            agent_id=agent,
            client_id=str(client) if client else None,
            principal_kind=str(value.get("principal_kind") or "") or None,
            credential_fingerprint=(
                str(value.get("credential_fingerprint") or "") or None
            ),
            worker_credential_version=(
                int(value["worker_credential_version"])
                if value.get("worker_credential_version") is not None
                else None
            ),
            worker_credential_state=(
                str(value.get("worker_credential_state") or "") or None
            ),
        )
    return TokenPrincipal(scopes=frozenset(str(s) for s in value))


def _normalize_auth_tokens(
    raw: Optional[AuthTokenMapping],
) -> Dict[str, TokenPrincipal]:
    if not raw:
        return {}
    normalized: Dict[str, TokenPrincipal] = {}
    for token, value in raw.items():
        registered = str(token)
        principal = _coerce_principal(value)
        if not principal.credential_fingerprint:
            fingerprint = (
                registered
                if registered.startswith("sha256:") and len(registered) == 71
                else "sha256:%s"
                % hashlib.sha256(registered.encode("utf-8")).hexdigest()
            )
            principal = replace(
                principal,
                credential_fingerprint=fingerprint,
            )
        normalized[registered] = principal
    return normalized


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


def _task_create_idempotency_scope(
    principal: "TokenPrincipal", *, surface: str
) -> str:
    """Build a stable, secret-free caller namespace for task-create retries."""

    identity = (
        "agent:%s" % principal.agent_id
        if principal.agent_id
        else "client:%s" % principal.client_id
        if principal.client_id
        else "credential:%s" % principal.credential_fingerprint
        if principal.credential_fingerprint
        else "tenant:%s" % principal.tenant_id
        if principal.tenant_id
        else "admin"
        if principal.is_admin
        else "scopes:%s" % ",".join(sorted(principal.scopes))
    )
    return "mac.task.create.v1|%s|%s" % (surface, identity)

def _refuse_agent_minted_directives(principal: "TokenPrincipal", topic: Any) -> None:
    """human.directive.v1 authority IS its operator provenance — an
    agent-bound token minting one would forge a human voice."""
    if principal.agent_id and str(topic or "") == "human.directive.v1":
        raise AuthorizationError(
            "agent tokens cannot publish human directives (operator provenance required)"
        )



AGENTBUS_MAX_EVENT_TIMEOUT_SECONDS = 60.0
AGENTBUS_MIN_EVENT_POLL_SECONDS = 0.25
AGENTBUS_MAX_EVENT_POLL_SECONDS = 5.0
DASHBOARD_TASK_HISTORY_LIMIT = 50
DASHBOARD_TASK_EVIDENCE_LIMIT = 25
DASHBOARD_TASK_REVIEW_LIMIT = 25
DASHBOARD_TASK_PUBLICATION_LIMIT = 10
DASHBOARD_MESSAGE_LIMIT = 200
DASHBOARD_TASK_LIMIT = 500
DASHBOARD_IDE_EVENT_LIMIT = 100
DASHBOARD_IDE_MESSAGE_LIMIT = 40
DASHBOARD_IDE_NOTIFICATION_LIMIT = 40
_TASK_LIST_SUMMARY_FIELDS = frozenset(
    {
        "id",
        "title",
        "project",
        "priority",
        "state",
        "owner_agent_id",
        "dependencies",
        "created_at",
        "updated_at",
        "last_updated_at",
        "publication_lane",
        "publication_route",
    }
)
_DASHBOARD_TASK_SUMMARY_FIELDS = _TASK_LIST_SUMMARY_FIELDS | frozenset(
    {
        "dependencies",
        "required_capabilities",
        "attempt_count",
        "max_attempts",
        "lease_id",
        "leased_until",
        "started_at",
        "completed_at",
    }
)
_DASHBOARD_IDE_TASK_FIELDS = _TASK_LIST_SUMMARY_FIELDS | frozenset(
    {
        "dependencies",
        "required_capabilities",
        "publication_lane",
        "publication_route",
    }
)
_DASHBOARD_IDE_PROJECT_FIELDS = frozenset(
    {
        "project",
        "name",
        "id",
        "project_id",
        "task_count",
        "active_count",
        "ready_count",
        "held_count",
        "blocked_count",
        "review_count",
        "completed_count",
        "state_counts",
        "status",
    }
)


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
    publication_lane_policy: Optional[Literal["auto", "managed", "legacy"]] = None
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=200)
    max_attempts: int = 3
    actor: str = "human"

    @model_validator(mode="before")
    @classmethod
    def _coerce_summary_to_description(cls, values: Any) -> Any:
        # The Hermes plugin advertises `summary` as the task body field.
        # Direct urllib callers (and LLMs blending schemas) may send `summary`
        # instead of `description`. Map it defensively so the executor PROMPT
        # is never silently empty.
        if isinstance(values, dict):
            summary = values.get("summary")
            if summary and not values.get("description"):
                values = dict(values)
                values["description"] = summary
        return values


class ProjectRegister(BaseModel):
    repository_url: str
    project: Optional[str] = None
    default_branch: Optional[str] = None
    title: Optional[str] = None
    priority: int = 0
    required_capabilities: List[str] = Field(default_factory=list)
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


class ReviewExperimentAssign(BaseModel):
    experiment_id: str
    arm: Optional[str] = None
    arms: Optional[Dict[str, float]] = None
    assignment_probability: Optional[float] = None
    blind: bool = False
    blind_arms: List[str] = Field(default_factory=list)
    policy_version: str = "v1"
    hypothesis: str = ""
    stratum: str = ""
    actor: str = "human"


class ReviewOutcomeCreate(BaseModel):
    kind: str
    status: str
    finding_id: str = ""
    severity_weight: float = 1.0
    source: str = "operator"
    detail: Dict[str, Any] = Field(default_factory=dict)
    actor: str = "human"


class ScientificPolicyCreate(BaseModel):
    name: str
    project: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    created_by: str = "human"


class ScientificPolicyAction(BaseModel):
    actor: str = "operator"
    reason: str = ""


class ScientificExperimentCreate(BaseModel):
    name: str
    project: str
    hypothesis: str
    control_policy_id: str
    treatment_policy_id: str
    primary_metric: str
    direction: Optional[str] = None
    min_effect: float = 0.0
    quality_margin: float = 0.05
    min_samples_per_arm: Optional[int] = None
    max_samples_per_arm: Optional[int] = None
    exploration_fraction: Optional[float] = None
    outcome_horizon_seconds: Optional[float] = None
    guardrails: Dict[str, Any] = Field(default_factory=dict)
    auto_promote: Optional[bool] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_by: str = "human"


class ScientificExperimentAction(BaseModel):
    actor: str = "operator"
    reason: str = ""


class TaskChildCreate(BaseModel):
    node_id: Optional[str] = None
    title: str
    description: str = ""
    project: Optional[str] = None
    priority: Optional[int] = None
    required_capabilities: Optional[List[str]] = None
    dependencies: List[str] = Field(default_factory=list)
    depends_on: Optional[List[str]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    max_attempts: Optional[int] = None


class TaskChildrenCreate(BaseModel):
    children: List[TaskChildCreate]
    actor: str = "human"
    lease_id: Optional[str] = None


class TaskDelete(BaseModel):
    actor: str = "human"


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)
    status: str = "active"
    actor: str = "human"
    project_id: Optional[str] = None
    dispatch_paused: Optional[bool] = None


class ProjectDispatch(BaseModel):
    paused: bool
    actor: str = "human"


class WorkPackageAdmit(BaseModel):
    plan: Dict[str, Any]
    actor: str = "work-package-admission-controller"
    reason: str
    tenant_id: Optional[str] = None
    root_task_id: Optional[str] = None


class WorkPackageActivate(BaseModel):
    expected_plan_version: int
    expected_epoch: int
    actor: str = "human"


class WorkPackagePause(BaseModel):
    expected_plan_version: int = Field(ge=1)
    expected_epoch: int = Field(ge=1)
    actor: str = Field(default="human", min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=4000)


class WorkPackageReplan(BaseModel):
    plan: Dict[str, Any]
    expected_plan_version: int = Field(ge=1)
    expected_epoch: int = Field(ge=1)
    actor: str = Field(
        default="work-package-replan-controller",
        min_length=1,
        max_length=256,
    )
    reason: str = Field(min_length=1, max_length=4000)


class WorkPackageOutputVerify(BaseModel):
    actor: str = "work-package-output-controller"


class WorkPackageIntegrationRequest(BaseModel):
    integration_node_key: str = Field(min_length=1, max_length=512)
    actor: str = Field(
        default="work-package-integration-controller",
        min_length=1,
        max_length=256,
    )


class WorkPackageCertificationPrepare(BaseModel):
    bundle_path: str = Field(min_length=1, max_length=4096)
    actor: str = Field(
        default="work-package-certification-controller",
        min_length=1,
        max_length=256,
    )


class WorkPackageCertificationClaim(BaseModel):
    owner: Optional[str] = Field(default=None, min_length=1, max_length=256)


class WorkPackageCertificationIngest(BaseModel):
    result: Dict[str, Any]
    owner: str = Field(min_length=1, max_length=256)
    fence: int = Field(ge=1)


class WorkPackageCertificationRun(BaseModel):
    bundle_path: str = Field(min_length=1, max_length=4096)
    owner: Optional[str] = Field(default=None, min_length=1, max_length=256)
    result_path: Optional[str] = Field(default=None, min_length=1, max_length=4096)


class WorkPackageFailedCertification(BaseModel):
    certification_id: str = Field(min_length=1, max_length=256)
    actor: str = Field(
        default="work-package-certification-controller",
        min_length=1,
        max_length=256,
    )


class WorkPackageCertificationAccept(BaseModel):
    certification_id: str = Field(min_length=1, max_length=256)


class WorkPackagePublicationFinalize(BaseModel):
    actor: str = Field(
        default="work-package-publication-finalizer",
        min_length=1,
        max_length=256,
    )
    receipt_id: Optional[str] = Field(default=None, min_length=1, max_length=256)


class WorkPackageFinalizationOutcome(BaseModel):
    outcome_type: Literal["revert", "incident"]
    external_id: str = Field(min_length=1, max_length=512)
    observed_at: str = Field(min_length=1, max_length=80)
    actor: str = Field(default="human", min_length=1, max_length=256)
    detail: Dict[str, Any] = Field(default_factory=dict)


class WorkPackageCandidateAccept(BaseModel):
    actor: str = Field(
        default="work-package-acceptance-controller",
        min_length=1,
        max_length=256,
    )


class WorkPackageCandidateReject(BaseModel):
    actor: str = Field(
        default="work-package-acceptance-controller",
        min_length=1,
        max_length=256,
    )
    reason: str = Field(min_length=1, max_length=4000)


class TaskRelease(BaseModel):
    actor: str = "human"


class TaskActivityAppend(BaseModel):
    phase: str
    actor: str
    summary: str
    lease_id: Optional[str] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    repository_registration: Optional[str] = None
    default_branch: Optional[str] = None
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


class FleetAgentObserve(BaseModel):
    agent_id: str
    source: str = "mac-agent"
    metadata: Dict[str, Any] = Field(default_factory=dict)
    actor: str = "mac-agent"


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


class HumanRegister(BaseModel):
    username: str
    email: Optional[str] = None
    github_login: Optional[str] = None
    display_name: Optional[str] = None
    groups: Optional[List[str]] = None
    human_id: Optional[str] = None


class PersonaInstanceRegister(BaseModel):
    tenant_id: str
    name: str
    persona_id: Optional[str] = None
    home_ref: str = ""
    status: str = "active"
    metadata: Dict[str, Any] = Field(default_factory=dict)
    instance_id: Optional[str] = None


# Backward-compatible alias for the pre-persona request type name.
HermesInstanceRegister = PersonaInstanceRegister


class PlatformBindingRegister(BaseModel):
    tenant_id: str
    persona_instance_id: Optional[str] = None
    # Deprecated pre-persona field name; accepted for one release.
    hermes_instance_id: Optional[str] = None
    platform: str
    external_id: str
    display_name: str = ""
    scopes: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    binding_id: Optional[str] = None

    @model_validator(mode="after")
    def _coalesce_persona_instance_id(self) -> "PlatformBindingRegister":
        if not self.persona_instance_id and self.hermes_instance_id:
            self.persona_instance_id = self.hermes_instance_id
        if not self.persona_instance_id:
            raise ValueError("persona_instance_id is required")
        # Keep the deprecated attribute in sync for legacy readers.
        self.hermes_instance_id = self.persona_instance_id
        return self


class InteractionTaskCreate(TaskCreate):
    user_id: Optional[str] = None
    platform_binding_id: Optional[str] = None
    conversation_ref: Optional[str] = None
    actor: str = "hermes"


class OpenClawDirectExecutionBegin(BaseModel):
    """Begin a direct human-driven OpenClaw Slack code execution.

    A direct, hub-authenticated human request may begin the requested code
    change immediately. The human does not have to file a task first, and the
    persona does not have to reply with a newly filed task before acting. When
    the request is deferred/delegated/follow-up, a visible MAC task is filed
    instead of executing inline.
    """

    human_id: str
    authenticated: bool = True
    directive_text: str = ""
    slack_workspace_id: str
    slack_channel_id: str
    slack_thread_ts: str
    slack_message_ts: str = ""
    repository_id: str
    repository_name: str = ""
    base_sha: str
    agent_id: Optional[str] = None
    deferred: bool = False
    delegated: bool = False
    autonomous_followup: bool = False
    requested_followup: bool = False
    requested_capabilities: Optional[List[str]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class PersonaRuntimeProofCreate(BaseModel):
    hermes_startup: Dict[str, Any] = Field(default_factory=dict)


# Backward-compatible alias for the pre-persona request type name.
HermesRuntimeProofCreate = PersonaRuntimeProofCreate


class TransitionRequest(BaseModel):
    target_state: str
    actor: str
    detail: Dict[str, Any] = Field(default_factory=dict)
    lease_id: Optional[str] = None


class TaskRecoveryRequest(BaseModel):
    actor: str
    reason: Optional[str] = None


class TaskAskRequest(BaseModel):
    questions: List[Any]
    actor: str
    why: Optional[str] = None


class TaskAnswerRequest(BaseModel):
    answer: str
    actor: str
    # Answering is a judgement, not automatically a release. "resume" keeps the
    # historical behaviour of returning the task to OPEN; "cancel" closes it,
    # which is what an answer like "no longer necessary" or "superseded by
    # task_x" actually means.
    disposition: str = "resume"
    replaced_by: Optional[str] = None


class EvidenceCreate(BaseModel):
    kind: str
    uri: str
    summary: str
    created_by: str
    checksum: Optional[str] = None
    lease_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    artifacts: List[Dict[str, Any]] = Field(default_factory=list)


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
    status: Optional[str] = None
    health_status: Optional[str] = None
    instance_kind: Optional[str] = None


class AgentAttestationKeyVerify(BaseModel):
    challenge: Dict[str, Any] = Field(default_factory=dict)
    signature: str


class AgentAttestationKeyRecover(BaseModel):
    probe: Dict[str, Any] = Field(default_factory=dict)


class AgentReportRepositoryExecutorApprove(BaseModel):
    expected_attestation: Dict[str, Any] = Field(default_factory=dict)
    expected_startup_timestamp: str
    actor: str = "fleet-deploy"


class AgentReportRepositoryExecutorRevoke(BaseModel):
    reason: str
    actor: str = "fleet-deploy"


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    capabilities: Optional[List[str]] = None
    resources: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    health_status: Optional[str] = None
    hermes_instance_id: Optional[str] = None
    instance_kind: Optional[str] = None
    actor: str = "human"


class CuriosityDecision(BaseModel):
    """Approve/reject one quarantined curiosity candidate.

    actor, reason and approval_id are all mandatory: the sidecar withholds
    approve/reject from the submitting agent precisely so that promotion
    carries external judgment with an auditable trail, and dropping any of the
    three would defeat that.
    """

    actor: str
    reason: str
    approval_id: str


class AgentBulkUpdate(BaseModel):
    agent_ids: List[str] = Field(default_factory=list)
    status: Optional[str] = None
    health_status: Optional[str] = None
    capabilities: Optional[List[str]] = None
    hermes_instance_id: Optional[str] = None
    instance_kind: Optional[str] = None
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


class DashboardWorkflowPlanRequest(BaseModel):
    goal: str
    project: Optional[str] = None
    mode: Literal["legacy", "managed"] = "legacy"
    repository_id: Optional[str] = None
    planning_base_ref: Optional[str] = None
    package_id: Optional[str] = None
    prompt: str = ""
    required_capabilities: List[str] = Field(default_factory=list)
    max_tasks: int = 8
    model: str = "*"
    context: Dict[str, Any] = Field(default_factory=dict)


class DashboardWorkflowPlanNode(BaseModel):
    node_id: str
    title: str
    description: str = ""
    kind: Optional[str] = None
    node_type: Optional[str] = None
    required_capabilities: List[str] = Field(default_factory=list)
    depends_on: List[str] = Field(default_factory=list)
    priority: int = 0
    effects: Dict[str, Any] = Field(default_factory=dict)
    inputs: List[Any] = Field(default_factory=list)
    external_dependencies: List[Any] = Field(default_factory=list)
    expected_outputs: List[Any] = Field(default_factory=list)
    verification: Dict[str, Any] = Field(default_factory=dict)
    estimates: Dict[str, Any] = Field(default_factory=dict)
    estimate: Optional[Dict[str, Any]] = None
    rework: Dict[str, Any] = Field(default_factory=dict)
    max_attempts: Optional[int] = None
    scope_confidence: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DashboardWorkflowPlanAccept(BaseModel):
    goal: str
    project: Optional[str] = None
    mode: Literal["legacy", "managed"] = "legacy"
    plan_id: Optional[str] = None
    package_id: Optional[str] = None
    repository_id: Optional[str] = None
    planning_base_ref: Optional[str] = None
    planning_base_sha: Optional[str] = None
    plan_generation: int = 1
    max_in_flight: Optional[int] = None
    max_mutation_wip: Optional[int] = None
    mutation_wip: Optional[Dict[str, Any]] = None
    integration: Optional[Dict[str, Any]] = None
    resource_namespace: Optional[Dict[str, Any]] = None
    nodes: List[DashboardWorkflowPlanNode] = Field(default_factory=list)
    plan: Optional[Dict[str, Any]] = None
    actor: str = "human"
    reason: str = "operator accepted managed work plan"
    tenant_id: Optional[str] = None
    root_task_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DashboardHermesConfigUpdate(BaseModel):
    runtime: Dict[str, Any] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)
    remove_config: List[str] = Field(default_factory=list)
    env: Dict[str, Any] = Field(default_factory=dict)
    remove_env: List[str] = Field(default_factory=list)
    plugins: Dict[str, Any] = Field(default_factory=dict)
    skills: Dict[str, Any] = Field(default_factory=dict)
    apply_local: bool = True
    actor: str = "human"


class DashboardHermesConfigApply(BaseModel):
    sender_agent_id: Optional[str] = None
    recipient_agent_ids: List[str] = Field(default_factory=list)
    request_id: Optional[str] = None
    actor: str = "human"


class HeartbeatRequest(BaseModel):
    status: Optional[str] = None
    health_status: Optional[str] = None
    resources: Optional[Dict[str, Any]] = None
    running_digest: Optional[str] = None
    actor: Optional[str] = None


class CrashReportCreate(BaseModel):
    event_id: str
    observed_at: Optional[str] = None
    supervisor: str
    process_name: str = "mac-agent"
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    signal: Optional[int] = None
    reason: str = "process exited unexpectedly"
    revision: str = "unknown"
    tree_sha: str = ""
    task_id: Optional[str] = None
    lease_id: Optional[str] = None
    stack_trace: str = ""
    stderr_tail: str = ""
    core_reference: str = ""
    core_metadata: Dict[str, Any] = Field(default_factory=dict)
    resource_snapshot: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CrashReportResolve(BaseModel):
    reason: str
    actor: str = "operator"


class AgentReflectRequest(BaseModel):
    recipient_agent_id: Optional[str] = None
    request_id: Optional[str] = None
    # Seconds to poll for the target agent's live reflect narrative. Production
    # keeps the 30s default; callers/tests that only need the published
    # inventory (not the live narrative) pass 0 to skip the blocking wait.
    reflect_timeout: float = 30.0


class LeaseRenewRequest(BaseModel):
    agent_id: str
    lease_seconds: int = 900


class LeaseDelegateRequest(BaseModel):
    # PR2c: the OWNER agent_id (caller) — must match lease.agent_id.
    agent_id: str
    # The role/worker agent to which lifecycle authorship is delegated.
    to_agent_id: str


class DispatchRequest(BaseModel):
    lease_seconds: int = 900
    limit: int = 100
    stale_after_seconds: Optional[int] = None


class DispatchHoldRequest(BaseModel):
    reason: str


class DispatchHoldAcquireRequest(BaseModel):
    reason: str
    expected_dispatch_hold: bool
    expected_reason: Optional[str] = None


class DispatchHoldBatchItem(BaseModel):
    agent_id: str
    reason: str
    generation: str
    baseline_seen: str
    principal_id: Optional[str] = None
    require_authenticated: bool = True
    require_report_executor: bool = False


class DispatchHoldBatchReleaseRequest(BaseModel):
    epoch_id: str
    holds: List[DispatchHoldBatchItem] = Field(default_factory=list)


class DispatchHoldBatchTransitionRequest(DispatchHoldBatchReleaseRequest):
    successor_reason: str


class FleetReleaseAttestationCandidateRequest(BaseModel):
    key: SecretStr


class FleetReleaseEpochParticipantRequest(BaseModel):
    agent_id: str
    expected_dispatch_hold: bool
    expected_hold_reason: Optional[str] = None
    expected_hold_at: Optional[str] = None
    generation: str
    baseline_seen: str
    principal_id: str
    attestation_candidate: Optional[
        FleetReleaseAttestationCandidateRequest
    ] = None
    report_executor_action: Literal["preserve", "approve", "revoke"] = (
        "preserve"
    )
    report_executor_attestation: Optional[Dict[str, Any]] = None


class FleetReleaseEpochOpenRequest(BaseModel):
    epoch_id: str
    participants: List[FleetReleaseEpochParticipantRequest] = Field(
        default_factory=list
    )
    successor_hold_reason: Optional[str] = None
    desired_worker_credential_mode: Optional[
        Literal["compatibility", "enforced"]
    ] = None


class FleetReleaseEpochCommitRequest(BaseModel):
    identity_sha256: str


class FleetReleaseAttestationProofRequest(BaseModel):
    challenge: Dict[str, Any] = Field(default_factory=dict)
    signature: str


class FleetReleaseEpochParticipantProofRequest(BaseModel):
    agent_id: str
    install_receipt: Dict[str, Any] = Field(default_factory=dict)
    attestation_proof: Optional[FleetReleaseAttestationProofRequest] = None
    report_executor_startup_timestamp: Optional[str] = None


class FleetReleaseEpochProveRequest(FleetReleaseEpochCommitRequest):
    proofs: List[FleetReleaseEpochParticipantProofRequest] = Field(
        default_factory=list
    )


class FleetReleaseEpochAbortRequest(FleetReleaseEpochCommitRequest):
    reason: str
    disposition: str = "auto"


class BreakGlassAuthorizeRequest(BaseModel):
    agent_id: str
    reason: str
    ttl_seconds: int = 900


class BreakGlassRevokeRequest(BaseModel):
    reason: str


class AgentClaimNextRequest(BaseModel):
    lease_seconds: int = 900
    dry_run: bool = False


class ServiceClaimsSyncRequest(BaseModel):
    willing_ops: List[str] = Field(default_factory=list)
    lease_seconds: int = 1800


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


class OpenShellPolicyCreate(BaseModel):
    name: str
    policy_text: str
    description: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_by: str = "human"
    policy_id: Optional[str] = None


class OpenShellPolicyUpdate(BaseModel):
    name: Optional[str] = None
    policy_text: Optional[str] = None
    description: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    updated_by: str = "human"


class OpenShellPolicyRender(BaseModel):
    agent_user: Optional[str] = None
    hub_host: Optional[str] = None
    hub_port: Optional[int] = None
    model_gateway_host: Optional[str] = None
    shared_services: Dict[str, int] = Field(default_factory=dict)


class OpenShellPolicyAssign(BaseModel):
    target_type: str = "agent"
    target_id: str
    created_by: str = "human"


class OpenShellStatusReport(BaseModel):
    status: str
    required: Optional[bool] = None
    active: bool = True
    sandbox_id: Optional[str] = None
    policy_id: Optional[str] = None
    policy_version: Optional[int] = None
    checksum: Optional[str] = None
    supervisor_pid: Optional[int] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class DirectivePropose(BaseModel):
    document: Dict[str, Any]
    actor: str = "human"


class DirectiveCheck(BaseModel):
    version: Optional[int] = None
    actor: str = "human"


class DirectiveApprove(BaseModel):
    version: int
    directive_digest: str
    check_id: str
    actor: str = "human"


class DirectiveActivate(BaseModel):
    version: int
    directive_digest: str
    actor: str = "human"


class DirectiveDeactivate(BaseModel):
    reason: str
    actor: str = "human"


class DirectiveBindingSet(BaseModel):
    target_type: str
    target_id: str
    key: str
    value: Any
    actor: str = "human"


class DirectiveWaiverCreate(BaseModel):
    version: int
    target_type: str
    target_id: str
    reason: str
    expires_at: Optional[str] = None
    actor: str = "human"


class DirectiveWaiverRevoke(BaseModel):
    reason: str
    actor: str = "human"


class DirectiveAck(BaseModel):
    digest: str


class ActionEventCreate(BaseModel):
    event_id: Optional[str] = None
    timestamp: Optional[str] = None
    agent_id: Optional[str] = None
    hermes_instance_id: Optional[str] = None
    task_id: Optional[str] = None
    session_id: Optional[str] = None
    sandbox_id: Optional[str] = None
    actor: str = "mac"
    action_type: str
    action_name: str
    subject_type: Optional[str] = None
    subject_id: Optional[str] = None
    outcome: str = "unknown"
    severity: str = "info"
    policy_id: Optional[str] = None
    policy_version: Optional[int] = None
    command_id: Optional[str] = None
    parent_event_id: Optional[str] = None
    attributes: Dict[str, Any] = Field(default_factory=dict)
    redaction_state: str = "redacted"


class MemorySummarizeActions(BaseModel):
    agent_id: Optional[str] = None
    since: Optional[str] = None
    created_by: str = "mac"
    write: bool = True


class AgentInstalledPackagesUpdate(BaseModel):
    installed_packages: Dict[str, Any] = Field(default_factory=dict)


class MessageCreate(BaseModel):
    sender_agent_id: str
    recipient_agent_id: Optional[str] = None
    task_id: Optional[str] = None
    message_type: str
    payload: Dict[str, Any]


class AgentBusOpen(BaseModel):
    sender_agent_id: str
    recipient_agent_id: Optional[str] = None
    task_id: Optional[str] = None
    topic: str = "content"
    content_type: str = "application/json"
    headers: Dict[str, Any] = Field(default_factory=dict)
    stream_id: Optional[str] = None
    # Group stream member list (task_588b67fd); opener is always included.
    participant_agent_ids: List[str] = Field(default_factory=list)


class AgentBusAppend(BaseModel):
    sender_agent_id: str
    content_type: Optional[str] = None
    payload: Any = None
    payload_encoding: str = "json"
    final: bool = False


class AgentBusPublish(BaseModel):
    sender_agent_id: str
    recipient_agent_id: Optional[str] = None
    task_id: Optional[str] = None
    topic: str = "content"
    content_type: str = "application/json"
    headers: Dict[str, Any] = Field(default_factory=dict)
    payload: Any = None
    payload_encoding: str = "json"
    # Group publish (task_588b67fd): opens one shared stream with these
    # members and leaves it open for their replies.
    participant_agent_ids: List[str] = Field(default_factory=list)


class AgentBusRepoUpdate(BaseModel):
    sender_agent_id: str
    recipient_agent_ids: List[str] = Field(default_factory=list)
    all_agents: bool = False
    repo_path: Optional[str] = None
    remote: str = "origin"
    branch: str = "main"
    restart: bool = True
    restart_services: List[str] = Field(default_factory=list)
    request_id: Optional[str] = None
    target_sha: Optional[str] = None
    desired_generation: Optional[int] = None
    release_id: Optional[str] = None


class AgentBusArtifactPublish(BaseModel):
    sender_agent_id: str
    operation: str = "upsert"
    recipient_agent_ids: List[str] = Field(default_factory=list)
    all_agents: bool = False
    artifact_id: Optional[str] = None
    digest: Optional[str] = None
    kind: str = "public-artifact"
    uri: Optional[str] = None
    public_url: Optional[str] = None
    path: Optional[str] = None
    publish_dir: Optional[str] = None
    sbom_uri: Optional[str] = None
    signers: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    task_id: Optional[str] = None
    request_id: Optional[str] = None


class DashboardTerminalOpen(BaseModel):
    sender_agent_id: Optional[str] = None
    shell: Optional[str] = None
    cwd: Optional[str] = None
    rows: int = 32
    cols: int = 120
    ttl_seconds: int = 900
    request_id: Optional[str] = None


class DashboardTerminalInput(BaseModel):
    input_stream_id: str
    sender_agent_id: Optional[str] = None
    data: str = ""
    data_b64: Optional[str] = None
    close: bool = False


class DashboardTerminalResize(BaseModel):
    input_stream_id: str
    sender_agent_id: Optional[str] = None
    rows: int = 32
    cols: int = 120


class DashboardTerminalClose(BaseModel):
    input_stream_id: str
    sender_agent_id: Optional[str] = None


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


class ObservabilityPruneRequest(BaseModel):
    older_than: Optional[str] = None
    keep_last: Optional[int] = None


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


class CommunicationIdentityConfig(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    is_default: bool = False
    enabled: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)
    identity_id: Optional[str] = None


class CommunicationAccountConfig(BaseModel):
    identity_id: str
    channel: str
    account_id: str = "default"
    credential_refs: Dict[str, Any] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    record_id: Optional[str] = None


class RepresentationBindingConfig(BaseModel):
    subject_kind: str
    subject_id: str
    identity_id: Optional[str] = None
    mode: str = "delegated"
    priority: int = 100
    enabled: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)
    binding_id: Optional[str] = None


class GatewayIdentityLeaseAcquire(BaseModel):
    account_id: str
    agent_id: str
    lease_seconds: int = 90
    metadata: Dict[str, Any] = Field(default_factory=dict)


class GatewayIdentityLeaseRenew(BaseModel):
    agent_id: str
    fencing_token: str
    lease_seconds: int = 90


class GatewayIdentityLeaseRelease(BaseModel):
    agent_id: str
    fencing_token: str


class HumanMessageCreate(BaseModel):
    target: str
    body: str
    origin_agent_id: Optional[str] = None
    identity_id: Optional[str] = None
    account_id: Optional[str] = None
    channel: Optional[str] = None
    task_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    max_attempts: int = 5
    metadata: Dict[str, Any] = Field(default_factory=dict)


class HumanMessageClaim(BaseModel):
    agent_id: str
    limit: int = 20
    lease_seconds: int = 60


class HumanMessageAck(BaseModel):
    agent_id: str
    provider_message_id: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class HumanMessageFail(BaseModel):
    agent_id: str
    error: str
    retryable: bool = True


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


class IntegrationFindingCreate(BaseModel):
    source_kind: str
    source_id: str
    finding_type: str
    title: str
    detail: Dict[str, Any] = Field(default_factory=dict)
    severity: str = "info"
    fingerprint: Optional[str] = None
    notify: bool = False
    channels: Optional[List[str]] = None
    notification_body: Optional[str] = None


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


class SecretRotate(BaseModel):
    value: str
    actor: str = "operator"


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


class ConfigFlagSet(BaseModel):
    value: Any
    channel: str = ""
    set_by: Optional[str] = None
    reason: Optional[str] = None


class ConfigFlagClear(BaseModel):
    channel: str = ""
    cleared_by: Optional[str] = None
    reason: Optional[str] = None


class DeployConfigReport(BaseModel):
    document: Dict[str, Any]
    reported_by: Optional[str] = None
    # Named schema_name (not "schema") to stay clear of BaseModel.schema().
    schema_name: Optional[str] = None


class AgentDeregister(BaseModel):
    actor: Optional[str] = None
    final_message: Optional[str] = None
    final_target: Optional[str] = None


class AgentBusCursorSet(BaseModel):
    topic: str
    position: Any


class AgentBusRequest(BaseModel):
    sender_agent_id: str
    recipient_agent_id: str
    payload: Dict[str, Any]
    topic: str = "peer.message.v1"
    content_type: str = "application/vnd.mac.agent-peer+json"
    reply_topic: str = "peer.reply.v1"
    deadline_seconds: float = 30.0
    correlation_id: Optional[str] = None
    task_id: Optional[str] = None


class HumanDirectivePublish(BaseModel):
    target_agent_id: str
    message: str
    issued_by: Optional[str] = None
    wait_seconds: float = 0.0
    task_id: Optional[str] = None


class AgentMemoryStore(BaseModel):
    content: str
    record_type: str = "agent_learning"
    task_id: Optional[str] = None


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


class NapCycle(BaseModel):
    actor: Optional[str] = None
    embed_into_medium: bool = True
    emit_dream_artifacts: bool = True
    qdrant_url: Optional[str] = None


class DreamImportLogs(BaseModel):
    dream_logs_dir: Optional[str] = None
    agent_id: Optional[str] = None
    created_by: str = "dream-log-import"
    embed: bool = True
    dry_run: bool = False
    qdrant_url: Optional[str] = None


class NapConsolidate(BaseModel):
    since: Optional[str] = None
    nap_run_id: Optional[str] = None
    embed_into_medium: bool = True
    emit_dream_artifacts: bool = True
    created_by: Optional[str] = None
    qdrant_url: Optional[str] = None


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


class RuntimeDeltaPropose(BaseModel):
    task_id: str
    agent_id: str
    package_manager: str
    commands: List[str] = Field(default_factory=list)
    added_dependencies: List[Any] = Field(default_factory=list)
    reason: str
    project: Optional[str] = None
    base_runtime_id: Optional[str] = None
    base_runtime_digest: Optional[str] = None
    lockfile_path: Optional[str] = None
    lockfile_digest: Optional[str] = None
    evidence_id: Optional[str] = None


class RuntimeDeltaValidate(BaseModel):
    actor: str = "operator"


class RuntimeDeltaReject(BaseModel):
    actor: str = "operator"
    reason: str


class RuntimeDeltaPromote(BaseModel):
    actor: str = "operator"
    runtime_name: Optional[str] = None


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


class ProjectRepositoryRegister(BaseModel):
    name: str
    path: str
    source: Optional[str] = None
    project: Optional[str] = None
    required_capabilities: List[str] = Field(default_factory=list)
    enabled: bool = True
    poll_interval_seconds: int = 60
    metadata: Dict[str, Any] = Field(default_factory=dict)
    actor: str = "bridge"


class MemoryCreate(BaseModel):
    task_id: Optional[str] = None
    subject_type: str
    subject_id: Optional[str] = None
    record_type: str
    content: str
    evidence_id: Optional[str] = None
    created_by: str


class MemoryRemember(BaseModel):
    key: str
    content: str
    project: Optional[str] = None
    actor: Optional[str] = None


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
    return _normalize_auth_tokens(
        {single: TokenPrincipal(scopes=frozenset({"admin"}))}
    )


def _required_scope(method: str, path: str) -> Optional[str]:
    if path == "/health":
        return None
    if path == "/.well-known/acp":
        # ACP discovery manifest (ADR 0006, Phase 3): a public well-known doc,
        # like /health. No secrets; just mac's capability advertisement.
        return None
    if path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
        # A2A AgentCard discovery (Phase 4, agent<->agent axis): an
        # unauthenticated well-known doc, like /.well-known/acp. Identity +
        # capabilities + skills only; no secrets. The canonical path is
        # agent-card.json (A2A v0.3+); agent.json is the legacy alias.
        return None
    if path == "/a2a":
        # A2A JSON-RPC endpoint (Phase 4): inbound delegation is an agent
        # action, so it requires the agent scope (admin inherits it), the same
        # bar as /v1 inference and the /acp/ws runtime seam.
        return "agent"
    if path == "/ui" or path.startswith("/ui/"):
        return None
    if path == "/v1" or path.startswith("/v1/"):
        # In-mac model router (th-merge-02): LLM inference is an agent action, so
        # the OpenAI front door requires the agent scope (admin inherits it),
        # regardless of method. This keeps the router from being an open proxy
        # when the API is bound to a network interface (e.g. the hub node).
        return "agent"
    if path.startswith("/repository-refs"):
        # A forced reconciliation in prune mode can delete remote branches.
        # Status is ordinary read data; every mutating trigger is admin-only.
        return "read" if method == "GET" else "admin"
    if path.startswith("/work-package-pipeline"):
        # The status projection is safe fleet visibility. Waking the controller
        # can ultimately certify and CAS-land code, so it is global admin work.
        return "read" if method == "GET" else "admin"
    if path.startswith("/optimizer"):
        # Learned policy changes future task execution across a project.  Reads
        # are ordinary fleet visibility; mutation and manual ticks are admin
        # control-plane operations, not general task writes.
        return "read" if method == "GET" else "admin"
    if method == "GET" and re.match(r"^/evidence/[^/]+/artifacts/[^/]+$", path):
        # Durable evidence artifact bytes can contain raw stdout/stderr and
        # result manifests. Listing metadata is a read model; fetching bytes is
        # closer to secret reveal and requires the narrower secret scope.
        return "secret"
    if method == "GET" and re.match(r"^/crash-reports/[^/]+$", path):
        # Occurrences include bounded stderr/fatal-trace tails. They are
        # redacted on ingestion, but retain the evidence-artifact privilege
        # boundary because arbitrary application output can still be private.
        return "secret"
    if path.startswith("/agents/") and (
        path.endswith("/directives/effective")
        or "/directive-activations/" in path
        or path.endswith("/openshell/policy")
    ):
        # Policy distribution is a self-only worker control path.  It must be
        # reachable by agent credentials even though the snapshot read uses
        # GET; the route then binds the path agent to the token principal.
        #
        # openshell/policy carries the guardrail text an agent must confine
        # ITSELF with, so it is deliberately narrower than the generic "read"
        # scope a GET would otherwise get: the policy names the fleet's hub and
        # gateway hosts and the binary paths permitted to reach them, which is
        # a map of the control plane for anyone holding only a read token.
        return "agent"
    if path == "/openshell/policies" or path.startswith("/openshell/policies/"):
        # One rule for the whole guardrail-policy resource, because the previous
        # split was indefensible: reading a policy's SOURCE required admin while
        # CREATING, UPDATING, DELETING and ASSIGNING one needed only `write`.
        # A token could author the guardrail it was not allowed to read back.
        #
        # Reads and writes now meet at admin. The disclosive reads
        # (`{id}`, `{id}/versions`, `{id}/render`) require it because the text is
        # the fleet's hub and gateway hosts, their ports, and the binaries
        # permitted to reach them — `render` most of all, being that template
        # with the placeholders filled IN. The mutations require it because
        # authoring the confinement every --yolo agent runs under is at least as
        # privileged as reading it. Every caller is already an operator CLI path
        # (`mac openshell policy ...`, `mac openshell reconcile`); no worker or
        # agent creates or assigns policies.
        #
        # Two identity views stay `read` deliberately — they carry name, version
        # and checksum but never the body, which is everything drift detection
        # and the dashboard need, and nothing an attacker can navigate by.
        if method == "GET" and (
            path == "/openshell/policies" or path.endswith("/assignments")
        ):
            return "read"
        return "admin"
    if method != "GET" and (
        path == "/directives"
        or path.startswith("/directives/")
        or path == "/directive-bindings"
        or path.startswith("/directive-waivers/")
    ):
        # Authoring a directive is operator speech, and this codebase already
        # says so: /agentbus/human-directive is admin precisely because human
        # directives are "never mintable via the agent scope (authority =
        # attested provenance)". A directive proposed, approved and activated
        # with a task-writing token has exactly the provenance problem that rule
        # exists to prevent -- it changes fleet-wide behaviour while claiming an
        # authority nobody granted.
        #
        # Covers the whole authoring lifecycle: propose, check, approve,
        # activate, deactivate, waivers, waiver revocation, and bindings.
        #
        # GETs stay `read` -- the directive documents, versions and impact views
        # are a read model, not a secret, and operators and dashboards depend on
        # them. The agent-side distribution paths
        # (/agents/{id}/directives/effective and .../directive-activations/...)
        # are matched earlier and keep the `agent` scope, so a worker still
        # receives and acknowledges directives normally.
        return "admin"
    if method == "GET":
        return "read"
    if path.startswith("/agents/") and (
        path.endswith("/heartbeat") or path.endswith("/messages/deliver")
        or path.endswith("/command-audit")
        or path.endswith("/openshell/status")
        or path.endswith("/crash-reports")
        or path.endswith("/directives/effective")
        or "/directive-activations/" in path
    ):
        return "agent"
    if path.startswith("/crash-reports"):
        return "read" if method == "GET" else "admin"
    if path == "/agentbus/human-directive":
        # Human directives are operator speech: never mintable via the agent
        # scope (authority = attested provenance).
        return "admin"
    if path.startswith("/agentbus"):
        return "agent"
    if path.startswith("/communication"):
        if (
            path == "/communication/deliveries"
            or path == "/communication/deliveries/claim"
            or path.endswith("/ack")
            or path.endswith("/fail")
            or path == "/communication/gateway-leases/acquire"
            or path.endswith("/renew")
            or path.endswith("/release")
        ):
            return "agent"
        return "admin"
    if path == "/observability/prune":
        # Pruning deletes global telemetry, so it requires an unbound admin
        # principal rather than the agent scope used to append observations.
        return "admin"
    if path.startswith("/observability"):
        return "agent"
    if path.startswith("/action-events"):
        return "agent"
    if path.startswith("/dispatch"):
        return "dispatch"
    if path.startswith("/secrets") or path.startswith("/secret-audits"):
        return "secret"
    if (
        path.startswith("/runtimes")
        or path.startswith("/runtime-deltas")
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
    if re.match(r"^/tasks/[^/]+/(force-complete|reopen|release|ask|answer)$", path):
        # These recovery/control-plane endpoints bypass normal worker flow or
        # make held work dispatchable. They must never be available to an
        # ordinary task writer.
        return "admin"
    if (
        method in {"PUT", "DELETE"}
        and re.match(r"^/tasks/[^/]+$", path)
    ):
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


def _authorize_acp_websocket(
    websocket: "WebSocket", auth_tokens: Mapping[str, TokenPrincipal]
) -> "tuple[Optional[TokenPrincipal], Optional[str]]":
    """Resolve the principal for an ACP WebSocket handshake.

    The HTTP auth middleware only runs for ``http`` scope, so the ``/acp/ws``
    route authenticates here instead. Returns ``(principal, accepted_subprotocol)``:

    * ``principal`` is ``None`` when no token is supplied or it does not match a
      registered token (or lacks the required ``agent`` scope). The caller
      rejects the socket *only when tokens are configured* -- when no tokens are
      set (dev mode), ``_authorize_request`` also returns ``None`` and the
      request is treated as admin, so we keep WS consistent with that.
    * ``accepted_subprotocol`` is ``"Authorization"`` when the token arrived via
      the ``Authorization`` subprotocol (the server must echo the chosen
      subprotocol back on accept), else ``None``.

    ACP runtime work requires the ``agent`` scope (same as ``/v1`` inference and
    the ``/agentbus`` / ``/action-events`` agent channels in
    :func:`_required_scope`).
    """

    required = "agent"
    token = ""
    accepted_subprotocol: Optional[str] = None

    # 1) ?token= query param.
    raw_token = websocket.query_params.get("token") if hasattr(websocket, "query_params") else None
    if raw_token:
        token = str(raw_token).strip()

    # 2) Authorization WebSocket subprotocol: clients offer
    #    ["Authorization", "<bearer>"] (browsers can't set headers on a WS
    #    handshake). Accept either a bare token as the second value or a
    #    "Bearer <token>" form.
    if not token:
        offered = []
        header = websocket.headers.get("sec-websocket-protocol") if hasattr(websocket, "headers") else None
        if header:
            offered = [p.strip() for p in header.split(",") if p.strip()]
        if offered and offered[0] == "Authorization" and len(offered) > 1:
            candidate = offered[1].strip()
            if candidate.lower().startswith("bearer "):
                candidate = candidate[len("bearer "):].strip()
            token = candidate
            accepted_subprotocol = "Authorization"

    if not token:
        return None, accepted_subprotocol

    principal = _resolve_principal(token, auth_tokens)
    if principal is None:
        return None, accepted_subprotocol
    if not principal.has_scope(required):
        return None, accepted_subprotocol
    return principal, accepted_subprotocol


def _should_record_http_observation(path: str) -> bool:
    return not (
        path == "/health"
        or path in {
            "/dashboard/state",
            "/dashboard/stream",
            "/.well-known/agent-card.json",
            "/.well-known/agent.json",
        }
        or path.startswith("/ui/assets")
        or path.startswith("/observability")
    )


def _dashboard_stream_observation_relevant(observation: Any) -> bool:
    """Return whether an observation can represent dashboard state change.

    HTTP request metrics describe reads of control-plane state. Treating those
    reads as writes creates a feedback loop: the Fleet IDE reads the dashboard,
    the read emits a metric, the stream sees the metric, and the IDE reads the
    dashboard again. Domain/task/worker observations remain refresh signals.
    """

    return str(getattr(observation, "layer", "") or "").lower() != "api"


def _safe_observation_source(value: Any, fallback: str = "router") -> str:
    text = str(value or "").strip()
    if (
        text
        and len(text) <= 128
        and text[0].isalnum()
        and all(ch.isalnum() or ch in "._-/: " for ch in text)
    ):
        return text.replace(" ", "_")
    return fallback


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


MAX_TERMINAL_INPUT_BYTES = 16 * 1024
MIN_TERMINAL_ROWS = 8
MAX_TERMINAL_ROWS = 80
MIN_TERMINAL_COLS = 40
MAX_TERMINAL_COLS = 240
MIN_TERMINAL_TTL_SECONDS = 30
MAX_TERMINAL_TTL_SECONDS = 3600


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _require_terminal_principal(principal: "TokenPrincipal") -> None:
    """Gate the debug-terminal routes: admin, or an agent acting on itself.

    A debug terminal is an interactive shell on a fleet host, so the set of
    principals allowed near one is small. ``require_global_fleet()`` was doing
    this job and cannot: it refuses only TENANT-BOUND non-admin tokens
    (``if self.is_admin or self.tenant_id is None: return``). An untenanted
    client token — the ordinary ``write`` scope, the same one that creates a
    task — has no tenant, is not admin, and carries no ``agent_id``, so it fell
    through every check and could open a session on any agent and type into it.

    The agent path is deliberately preserved rather than collapsed into
    admin-only: a worker legitimately opens its own debug terminal, and each
    handler still narrows that to the acting agent with ``assert_actor``. This
    only removes the principal class that was never meant to be here.
    """
    principal.require_global_fleet()
    if principal.is_admin or principal.agent_id:
        return
    raise AuthorizationError(
        "debug terminal sessions require an admin token, or the acting agent's "
        "own token; a general read/write client token is not sufficient"
    )


def _new_terminal_session_id() -> str:
    return "term_" + secrets.token_hex(12)


def _validate_terminal_session_id(session_id: str) -> str:
    value = str(session_id or "").strip()
    if (
        not value
        or len(value) > 64
        or not value[0].isalnum()
        or not all(ch.isalnum() or ch in "._-" for ch in value)
    ):
        raise ValidationError("invalid terminal session_id: %s" % session_id)
    return value


def _terminal_input_data_b64(body: DashboardTerminalInput) -> Optional[str]:
    if body.data_b64 is not None:
        try:
            raw = base64.b64decode(body.data_b64.encode("ascii"), validate=True)
        except Exception as exc:
            raise ValidationError("terminal input data_b64 is invalid") from exc
        if len(raw) > MAX_TERMINAL_INPUT_BYTES:
            raise ValidationError(
                "terminal input exceeds %d-byte limit" % MAX_TERMINAL_INPUT_BYTES
            )
        return base64.b64encode(raw).decode("ascii")
    raw = str(body.data or "").encode("utf-8")
    if not raw:
        return None
    if len(raw) > MAX_TERMINAL_INPUT_BYTES:
        raise ValidationError(
            "terminal input exceeds %d-byte limit" % MAX_TERMINAL_INPUT_BYTES
        )
    return base64.b64encode(raw).decode("ascii")


def _terminal_stream_for_session(
    cp: ControlPlane,
    *,
    session_id: str,
    stream_id: str,
    expected_topic: str,
    expected_content_type: str,
    expected_schema: str,
) -> Dict[str, Any]:
    session = _validate_terminal_session_id(session_id)
    stream = cp.get_agentbus_stream(stream_id).to_dict()
    headers = stream.get("headers") if isinstance(stream.get("headers"), dict) else {}
    content_type = str(stream.get("content_type") or "").split(";", 1)[0]
    if (
        stream.get("topic") != expected_topic
        or content_type != expected_content_type
        or headers.get("schema") != expected_schema
        or headers.get("terminal_session_id") != session
    ):
        raise ValidationError("agentbus stream is not part of terminal session %s" % session)
    return stream


def _terminal_session_id_from_stream(stream: Mapping[str, Any]) -> str:
    headers = stream.get("headers") if isinstance(stream.get("headers"), Mapping) else {}
    return str(headers.get("terminal_session_id") or "")


def _dashboard_terminal_sessions_from_streams(
    streams: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    sessions: Dict[str, Dict[str, Any]] = {}
    for stream in streams:
        session_id = _terminal_session_id_from_stream(stream)
        if not session_id:
            continue
        topic = str(stream.get("topic") or "")
        if topic not in {DEBUG_TERMINAL_INPUT_TOPIC, DEBUG_TERMINAL_OUTPUT_TOPIC}:
            continue
        record = sessions.setdefault(
            session_id,
            {
                "schema": "mac.dashboard.terminal_session_summary.v1",
                "session_id": session_id,
                "agent_id": "",
                "sender_agent_id": "",
                "input_stream_id": "",
                "output_stream_id": "",
                "status": "unknown",
                "created_at": "",
                "updated_at": "",
                "closed_at": None,
                "input_stream": None,
                "output_stream": None,
            },
        )
        created_at = str(stream.get("created_at") or "")
        updated_at = str(stream.get("updated_at") or created_at)
        if created_at and (not record["created_at"] or created_at < record["created_at"]):
            record["created_at"] = created_at
        if updated_at and (not record["updated_at"] or updated_at > record["updated_at"]):
            record["updated_at"] = updated_at
        if topic == DEBUG_TERMINAL_INPUT_TOPIC:
            record["input_stream"] = dict(stream)
            record["input_stream_id"] = str(stream.get("id") or "")
            record["agent_id"] = str(stream.get("recipient_agent_id") or record["agent_id"] or "")
            record["sender_agent_id"] = str(stream.get("sender_agent_id") or record["sender_agent_id"] or "")
        else:
            record["output_stream"] = dict(stream)
            record["output_stream_id"] = str(stream.get("id") or "")
            record["agent_id"] = str(stream.get("sender_agent_id") or record["agent_id"] or "")
            record["sender_agent_id"] = str(stream.get("recipient_agent_id") or record["sender_agent_id"] or "")
            record["status"] = str(stream.get("status") or record["status"] or "unknown")
            record["closed_at"] = stream.get("closed_at") or record["closed_at"]
        if record["status"] == "unknown":
            record["status"] = str(stream.get("status") or "unknown")
            record["closed_at"] = stream.get("closed_at") or record["closed_at"]
    return sorted(
        sessions.values(),
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )


def _dashboard_terminal_sessions(
    cp: ControlPlane,
    *,
    agent_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 120,
) -> List[Dict[str, Any]]:
    stream_limit = max(1, min(int(limit) * 4, 1000))
    streams = [stream.to_dict() for stream in cp.list_agentbus_streams(agent_id=agent_id, limit=stream_limit)]
    sessions = _dashboard_terminal_sessions_from_streams(streams)
    if status:
        sessions = [item for item in sessions if str(item.get("status") or "") == status]
    return sessions[: max(1, min(int(limit), 500))]


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


def _dashboard_task_summary(task: Any) -> Dict[str, Any]:
    task_dict = task.to_dict()
    return {
        "task": {
            key: task_dict[key]
            for key in _DASHBOARD_TASK_SUMMARY_FIELDS
            if key in task_dict
        },
        "detail_available": True,
        "schema": "mac.dashboard.task_summary.v1",
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
        "client_id": principal.client_id,
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


def _dashboard_dispatch_explain(
    cp: ControlPlane,
    tasks: Optional[List[Any]] = None,
    agents: Optional[List[Any]] = None,
    machines_by_id: Optional[Dict[str, Any]] = None,
    candidate_limit: int = 60,
) -> Dict[str, Any]:
    tasks = tasks if tasks is not None else cp.list_tasks()
    agents = agents if agents is not None else cp.list_agents()
    # Retained in the signature for callers that already have this snapshot;
    # dispatch truth now comes exclusively from ControlPlane.
    del machines_by_id
    open_tasks = [task for task in tasks if task.state == "open"]
    explanations = [
        cp.explain_task_dispatch(
            task,
            agents=agents,
            candidate_limit=candidate_limit,
        )
        for task in open_tasks
    ]
    return {"open_task_count": len(open_tasks), "tasks": explanations}


def _dashboard_hermes_activity(
    cp: ControlPlane,
    instance_id: str,
    tasks: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    context = cp.persona_context(instance_id)
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


def _dashboard_ide_task_dict(task_dict: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "task": {
            key: value
            for key, value in task_dict.items()
            if key in _DASHBOARD_IDE_TASK_FIELDS
        },
        "detail_loaded": False,
    }


def _dashboard_ide_project_summary(value: Dict[str, Any]) -> Dict[str, Any]:
    """Project navigation needs counts, not embedded task/metadata payloads."""
    return {
        key: value[key]
        for key in _DASHBOARD_IDE_PROJECT_FIELDS
        if key in value
    }


def _dashboard_ide_finding(value: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the problems UI useful without shipping arbitrary finding detail."""
    return {
        key: value[key]
        for key in (
            "id",
            "source_id",
            "source_kind",
            "finding_type",
            "severity",
            "status",
            "title",
            "fingerprint",
            "first_seen_at",
            "last_seen_at",
            "resolved_at",
            "resolution",
        )
        if key in value
    }


def _dashboard_ide_agent_is_physical(agent: Any, machine: Optional[Any]) -> bool:
    """Exclude hub-local service identities from the physical fleet mesh."""
    agent_resources = agent.resources if isinstance(agent.resources, dict) else {}
    if agent_resources.get("virtual") is True or "hub_review_verifier" in agent_resources:
        return False
    if machine is None:
        return True
    labels = machine.labels if isinstance(machine.labels, dict) else {}
    resources = machine.resources if isinstance(machine.resources, dict) else {}
    return not (
        labels.get("virtual") is True
        or resources.get("virtual") is True
        or machine.id == "machine_operator_review"
    )


def _dashboard_ide_resource_facts(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in (
            "hardware",
            "coding_clis",
            "openclaw_runtime",
            "chat_gateway",
            "representation",
        )
        if key in value
    }


def _dashboard_ide_agent(
    cp: ControlPlane,
    agent: Any,
    tasks: List[Any],
    machines_by_id: Dict[str, Any],
) -> Dict[str, Any]:
    base = _dashboard_agent_base(cp, agent, tasks, machines_by_id)
    agent_dict = base["agent"]
    machine_dict = base.get("machine")
    projected_agent = {
        key: agent_dict[key]
        for key in (
            "id",
            "name",
            "status",
            "health_status",
            "current_task_id",
            "capabilities",
            "role_id",
            "last_seen_at",
            "machine_id",
        )
        if key in agent_dict
    }
    projected_agent["resources"] = _dashboard_ide_resource_facts(
        agent_dict.get("resources")
    )

    projected_machine = None
    if isinstance(machine_dict, dict):
        projected_machine = {
            key: machine_dict[key]
            for key in ("id", "hostname", "trusted", "hardware")
            if key in machine_dict
        }
        projected_machine["resources"] = _dashboard_ide_resource_facts(
            machine_dict.get("resources")
        )

    active_task_dicts = list(base["active_tasks"])
    active_routes = cp.task_publication_routes(
        (task["id"] for task in active_task_dicts), compact=True
    )
    for task in active_task_dicts:
        route = active_routes[task["id"]]
        task["publication_lane"] = route["lane"]
        task["publication_route"] = route

    return {
        "agent": projected_agent,
        "machine": projected_machine,
        "active_tasks": [
            detail["task"]
            for detail in (
                _dashboard_ide_task_dict(task_dict)
                for task_dict in active_task_dicts
            )
        ],
        "active_projects": base["active_projects"],
        "capacity": base["capacity"],
        "active_lease_count": base["active_lease_count"],
        "availability": base["availability"],
    }


def _dashboard_ide_state(
    cp: ControlPlane,
    hermes_startup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the bounded, list-oriented state required by the Fleet IDE.

    The legacy dashboard state is intentionally comprehensive. The Fleet IDE
    needs summaries for navigation and loads one selected task's capped detail
    separately, so returning every secret audit, task evidence blob, history,
    runtime proof, and dispatch explanation makes cold start both slower and
    less reliable without adding anything to the rendered first screen.
    """

    machines = cp.list_machines()
    machines_by_id = {machine.id: machine for machine in machines}
    all_agents = cp.list_agents()
    agents = [
        agent
        for agent in all_agents
        if _dashboard_ide_agent_is_physical(agent, machines_by_id.get(agent.machine_id))
    ]
    tasks = cp.list_tasks()
    task_dicts = [task.to_dict() for task in tasks]
    task_routes = cp.task_publication_routes(
        (task["id"] for task in task_dicts), compact=True
    )
    for task in task_dicts:
        route = task_routes[task["id"]]
        task["publication_lane"] = route["lane"]
        task["publication_route"] = route
    projects = cp._hermes_project_contexts(
        tasks,
        all_agents,
        [item.to_dict() for item in cp.list_project_items()],
        [repository.to_dict() for repository in cp.list_project_repositories()],
        [project.to_dict() for project in cp.list_project_records()],
    )
    workflows = [workflow.to_dict() for workflow in cp.list_workflows()][-120:]
    workflow_runs = cp.workflow_runs_summary()
    streams = [stream.to_dict() for stream in cp.list_agentbus_streams(limit=120)]
    terminal_sessions = _dashboard_terminal_sessions_from_streams(streams)
    messages = [
        message.to_dict()
        for message in cp.list_messages(limit=DASHBOARD_IDE_MESSAGE_LIMIT)
    ]
    notifications = [
        notification.to_dict()
        for notification in cp.list_notifications(limit=DASHBOARD_IDE_NOTIFICATION_LIMIT)
    ]
    findings = [
        finding.to_dict()
        for finding in cp.list_integration_findings(limit=120)
    ]
    secrets = [secret.to_dict() for secret in cp.list_secrets()][-200:]
    runtime_deltas = [
        delta.to_dict()
        for delta in cp.list_runtime_deltas(limit=120)
    ]
    runtime_runs = [run.to_dict() for run in cp.list_runtime_runs()][-120:]
    rollouts = [rollout.to_dict() for rollout in cp.list_rollouts()][-120:]
    fleets = [fleet.to_dict() for fleet in cp.list_fleets()][-120:]

    return {
        "schema": "mac.dashboard_ide.v1",
        "overview": {
            "counts": {
                "machines": len(machines),
                "agents": len(agents),
                "service_agents": len(all_agents) - len(agents),
                "fleets": len(fleets),
                "healthy_agents": sum(
                    1 for agent in agents if agent.health_status == "healthy"
                ),
                "busy_agents": sum(
                    1 for agent in agents if agent.status == "busy"
                ),
                "active_tasks": sum(
                    1
                    for task in tasks
                    if task.state not in TERMINAL_DASHBOARD_STATES
                ),
                "projects": len(projects),
                "workflows": len(workflows),
                "workflow_runs": workflow_runs.get("total", 0),
                "terminal_sessions": len(terminal_sessions),
                "secrets": len(secrets),
                "integration_findings": len(findings),
                "open_integration_findings": sum(
                    1 for finding in findings if finding["status"] == "open"
                ),
            },
            "task_states": _state_counts(task_dicts, "state"),
            "agent_statuses": _state_counts(
                [agent.to_dict() for agent in agents], "status"
            ),
        },
        "project_summaries": [
            _dashboard_ide_project_summary(project) for project in projects
        ],
        "agents": [
            _dashboard_ide_agent(cp, agent, tasks, machines_by_id)
            for agent in agents
        ],
        "tasks": [
            _dashboard_ide_task_dict(task_dict)
            for task_dict in task_dicts
        ],
        "fleets": fleets,
        "workflows": workflows,
        "workflow_drafts": [],
        "workflow_runs": workflow_runs,
        "events": cp.list_events(limit=DASHBOARD_IDE_EVENT_LIMIT),
        "messages": messages,
        "notifications": notifications,
        "observability": {},
        "action_events": [],
        "command_audit": [],
        "runtimes": [],
        "runtime_deltas": runtime_deltas,
        "runtime_runs": runtime_runs,
        "rollouts": rollouts,
        "secrets": secrets,
        "secret_audits": [],
        "service_links": _dashboard_service_links(hermes_startup),
        "integration_findings": [
            _dashboard_ide_finding(finding) for finding in findings
        ],
        "artifacts": [],
        "terminal_sessions": terminal_sessions,
    }


def _dashboard_state(
    cp: ControlPlane,
    hermes_startup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    tenants = [tenant.to_dict() for tenant in cp.list_tenants()]
    users = [user.to_dict() for user in cp.list_users()]
    personas = [persona.to_dict() for persona in cp.list_personas()]
    hermes_instances = [instance.to_dict() for instance in cp.list_persona_instances()]
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
    terminal_sessions = _dashboard_terminal_sessions_from_streams(agentbus_streams)
    artifacts = [artifact.to_dict() for artifact in cp.list_artifacts()]
    bridge_items = [item.to_dict() for item in cp.list_project_items()]
    # beads removed as a read/write source; the status view no longer lists
    # beads repositories (kept as an empty list for dashboard shape stability).
    project_repositories: List[Dict[str, Any]] = []
    memory_records = [
        record.to_dict() for record in cp.search_memory()
    ][-120:]
    nap_schedules = [schedule.to_dict() for schedule in cp.list_nap_schedules()]
    nap_runs = [run.to_dict() for run in cp.list_nap_runs()]
    runtime_runs = [run.to_dict() for run in cp.list_runtime_runs()]
    runtime_deltas = [delta.to_dict() for delta in cp.list_runtime_deltas(limit=120)]
    integration_findings = [
        finding.to_dict() for finding in cp.list_integration_findings(limit=120)
    ]
    integration_observations = [
        observation.to_dict()
        for observation in cp.list_integration_observations(limit=120)
    ]
    openshell_policies = [
        policy.to_dict()
        for policy in cp.list_openshell_policies(include_deleted=True)
    ]
    openshell_assignments = [
        assignment.to_dict()
        for assignment in cp.list_openshell_policy_assignments(active_only=False)
    ]
    openshell_policy_versions = [
        version.to_dict()
        for policy in openshell_policies
        for version in cp.list_openshell_policy_versions(policy["id"])
    ]
    openshell_agent_statuses = [
        cp.get_openshell_status(agent.id)
        for agent in agents
    ]
    action_events = [
        event.to_dict()
        for event in cp.list_action_events(limit=240)
    ]
    task_details = [_dashboard_task_summary(task) for task in tasks[:DASHBOARD_TASK_LIMIT]]
    rollout_statuses = [_dashboard_rollout_status(cp, rollout.id) for rollout in rollouts]
    project_summaries = cp.list_projects()
    hermes_work_contexts = {
        instance["id"]: cp.persona_work_context(instance["id"], task_limit=40)
        for instance in hermes_instances
    }
    hermes_runtime_proofs = {}
    for instance in hermes_instances:
        submitted = _latest_submitted_runtime_proof(instance)
        hermes_runtime_proofs[instance["id"]] = submitted or cp.persona_runtime_proof(
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
                "terminal_sessions": len(terminal_sessions),
                "artifacts": len(artifacts),
                "project_repositories": len(project_repositories),
                "projects": len(project_summaries),
                "memory_records": len(memory_records),
                "integration_findings": len(integration_findings),
                "open_integration_findings": sum(
                    1 for finding in integration_findings if finding["status"] == "open"
                ),
                "openshell_policies": len(openshell_policies),
                "action_events": len(action_events),
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
        "hermes_config_surfaces": build_hermes_config_surfaces(
            fleets,
            agents=[agent.to_dict() for agent in agents],
            agentbus_streams=agentbus_streams,
        ),
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
        "tasks_limited_to": DASHBOARD_TASK_LIMIT,
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
        "terminal_sessions": terminal_sessions,
        "artifacts": artifacts,
        "bridge_items": bridge_items,
        "project_repositories": project_repositories,
        "memory_records": memory_records,
        "nap_schedules": nap_schedules,
        "nap_runs": nap_runs,
        "integration_findings": integration_findings,
        "integration_observations": integration_observations,
        "openshell_policies": openshell_policies,
        "openshell_policy_assignments": openshell_assignments,
        "openshell_policy_versions": openshell_policy_versions,
        "openshell_agent_statuses": openshell_agent_statuses,
        "action_events": action_events,
        "service_links": _dashboard_service_links(hermes_startup),
        "events": cp.list_events(limit=240),
        "command_audit": [
            record.to_dict() for record in cp.list_command_audit(limit=120)
        ],
        "secrets": secrets,
        "secret_audits": secret_audits,
        "runtimes": [runtime.to_dict() for runtime in cp.list_runtimes()],
        "runtime_deltas": runtime_deltas,
        "runtime_runs": runtime_runs,
        "rollouts": rollout_statuses,
        "eval_sets": [eval_set.to_dict() for eval_set in cp.list_eval_sets()],
        "eval_runs": [run.to_dict() for run in cp.list_eval_runs()],
        "observability": cp.observability_summary(),
        "hermes_startup": hermes_startup,
    }


def _dashboard_response(
    cp: ControlPlane,
    principal: TokenPrincipal,
    hermes_startup: Optional[Dict[str, Any]],
    *,
    view: Optional[str] = None,
) -> Dict[str, Any]:
    model = (
        _dashboard_ide_state(cp, hermes_startup)
        if view == "ide"
        else _dashboard_state(cp, hermes_startup)
    )
    now = utcnow()
    model["server_time"] = now
    model["updated_at"] = now
    model["session"] = _dashboard_session(principal)
    return model


def _workflow_plan_id(goal: str, project: Optional[str], nodes: List[Dict[str, Any]]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"goal": goal, "project": project, "nodes": nodes, "time": utcnow()},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    return "plan_%s" % digest


def _workflow_plan_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    out: List[str] = []
    seen = set()
    for item in raw:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _workflow_plan_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _workflow_plan_node_id(value: Any, index: int, used: set) -> str:
    text = str(value or "").strip().lower()
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in text)
    safe = safe.strip("_-") or "task_%d" % index
    base = safe[:48]
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = ("%s_%d" % (base[:42], suffix)).strip("_")
        suffix += 1
    used.add(candidate)
    return candidate


def _normalize_dashboard_workflow_plan(
    raw: Any,
    request: Dict[str, Any],
    *,
    source: str = "model",
) -> Dict[str, Any]:
    if isinstance(raw, str):
        raw = _extract_json_object(raw)
    if not isinstance(raw, dict):
        raise ValidationError("workflow planner returned a non-object response")
    raw_nodes = raw.get("nodes") or raw.get("tasks") or raw.get("steps")
    if not isinstance(raw_nodes, list):
        raise ValidationError("workflow planner response must include nodes/tasks")
    try:
        max_tasks = int(request.get("max_tasks") or 8)
    except (TypeError, ValueError):
        max_tasks = 8
    max_tasks = max(1, min(20, max_tasks))
    used: set = set()
    nodes: List[Dict[str, Any]] = []
    for index, raw_node in enumerate(raw_nodes[:max_tasks], start=1):
        if not isinstance(raw_node, dict):
            continue
        node_id = _workflow_plan_node_id(
            raw_node.get("node_id") or raw_node.get("id") or raw_node.get("key"),
            index,
            used,
        )
        title = str(raw_node.get("title") or raw_node.get("name") or "").strip()
        if not title:
            title = "Task %d" % index
        metadata = raw_node.get("metadata") if isinstance(raw_node.get("metadata"), dict) else {}
        nodes.append(
            {
                "node_id": node_id,
                "title": title[:240],
                "description": str(raw_node.get("description") or raw_node.get("summary") or "").strip(),
                "required_capabilities": _workflow_plan_string_list(
                    raw_node.get("required_capabilities") or raw_node.get("capabilities")
                ),
                "depends_on": _workflow_plan_string_list(
                    raw_node.get("depends_on") or raw_node.get("dependencies") or raw_node.get("parents")
                ),
                "priority": _workflow_plan_int(raw_node.get("priority"), _workflow_plan_int(request.get("priority"), 0)),
                "metadata": metadata,
            }
        )
    if not nodes:
        raise ValidationError("workflow planner produced no task nodes")
    _workflow_plan_topological_order(nodes, allow_external=False)
    goal = str(raw.get("goal") or request.get("goal") or "").strip()
    project = raw.get("project") if raw.get("project") is not None else request.get("project")
    project = str(project).strip() if project is not None else None
    if project == "":
        project = None
    plan_id = str(raw.get("plan_id") or _workflow_plan_id(goal, project, nodes))
    return {
        "schema": "mac.dashboard.workflow_plan.v1",
        "plan_id": plan_id,
        "goal": goal,
        "project": project,
        "source": source,
        "nodes": nodes,
        "created_at": utcnow(),
    }


def _workflow_plan_topological_order(
    nodes: List[Dict[str, Any]],
    *,
    allow_external: bool,
    cp: Optional[ControlPlane] = None,
) -> List[Dict[str, Any]]:
    by_id = {str(node.get("node_id")): node for node in nodes}
    if len(by_id) != len(nodes):
        raise ValidationError("workflow plan node ids must be unique")
    for node in nodes:
        title = str(node.get("title") or "").strip()
        if not title:
            raise ValidationError("workflow plan node title is required")
        normalized_deps = []
        for dep in _workflow_plan_string_list(node.get("depends_on")):
            if dep not in by_id:
                if not allow_external:
                    raise ValidationError("workflow plan dependency %r does not reference a planned node" % dep)
                if cp is not None:
                    cp.get_task(dep)
            normalized_deps.append(dep)
        node["depends_on"] = normalized_deps

    visiting: set = set()
    visited: set = set()
    ordered: List[Dict[str, Any]] = []

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            raise ValidationError("workflow plan contains a dependency cycle")
        visiting.add(node_id)
        node = by_id[node_id]
        for dep in node.get("depends_on") or []:
            if dep in by_id:
                visit(dep)
        visiting.remove(node_id)
        visited.add(node_id)
        ordered.append(node)

    for node in nodes:
        visit(str(node.get("node_id")))
    return ordered


def _extract_json_object(text: str) -> Dict[str, Any]:
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValidationError("workflow planner response did not contain JSON")
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValidationError("workflow planner response JSON is invalid: %s" % exc) from exc
    if not isinstance(parsed, dict):
        raise ValidationError("workflow planner JSON must be an object")
    return parsed


def _chat_completion_content(body: Any) -> str:
    if not isinstance(body, dict):
        raise ValidationError("workflow planner returned an invalid chat response")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValidationError("workflow planner chat response had no choices")
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content") or first.get("text")
    if isinstance(content, list):
        content = "\n".join(
            str(item.get("text") or item.get("content") or "")
            for item in content
            if isinstance(item, dict)
        )
    text = str(content or "").strip()
    if not text:
        raise ValidationError("workflow planner chat response was empty")
    return text


def _dashboard_workflow_plan_prompt(cp: ControlPlane, request: Dict[str, Any]) -> str:
    project = str(request.get("project") or "").strip()
    summaries = cp.list_projects()
    project_summary = next((item for item in summaries if item.get("project") == project), None) if project else None
    context = {
        "goal": request.get("goal"),
        "project": project or None,
        "prompt": request.get("prompt") or "",
        "required_capabilities": request.get("required_capabilities") or [],
        "max_tasks": request.get("max_tasks") or 8,
        "project_summary": project_summary,
        "extra_context": request.get("context") or {},
    }
    if str(request.get("mode") or "legacy").strip().lower() == MANAGED_WORK_PLAN_MODE:
        return (
            "Propose an editable managed work DAG; do not create tasks and do not choose "
            "repository identity, refs, SHAs, package ids, or other controller-owned fields. "
            "Return ONLY JSON with nodes. Every node must explicitly declare effects, "
            "expected_outputs, and verification. Mutation nodes must declare repository-relative "
            "writes/exclusive effects, estimates.confidence, and repository-default verification. "
            "Finish with exactly one integration node that depends directly on every mutation "
            "leaf, followed by exactly one terminal certification node that depends only on the "
            "integration node. Controller stations may declare reads but no writes/exclusive/"
            "external effects. Use this shape: "
            "{\"nodes\":[{\"node_id\":\"short_key\",\"title\":\"task title\","
            "\"description\":\"implementation-grade instructions\",\"kind\":\"mutation\","
            "\"required_capabilities\":[\"python\"],\"depends_on\":[],"
            "\"effects\":{\"reads\":[],\"writes\":[\"src/path\"],\"exclusive\":[]},"
            "\"expected_outputs\":[\"candidate\"],"
            "\"verification\":{\"profile\":\"repository-default\","
            "\"required_evidence\":[\"tests\"]},"
            "\"estimates\":{\"confidence\":\"high\"},\"metadata\":{}}],"
            "\"max_in_flight\":4,\"mutation_wip\":{\"max_tokens\":2}}. "
            "Use integration-default and certification-default verification profiles for the "
            "two controller stations. Keep the plan small and executable.\n\nContext:\n%s"
            % json.dumps(context, sort_keys=True)
        )
    return (
        "Create a concrete MAC task plan for the requested work. "
        "Return ONLY JSON with this shape: "
        "{\"nodes\":[{\"node_id\":\"short_key\",\"title\":\"task title\","
        "\"description\":\"implementation-grade instructions\","
        "\"required_capabilities\":[\"python\"],\"depends_on\":[\"other_node_id\"],"
        "\"priority\":0,\"metadata\":{}}]}. "
        "Use depends_on only for earlier node_id values. Keep the plan small, executable, "
        "and review-gate friendly.\n\nContext:\n%s" % json.dumps(context, sort_keys=True)
    )


def _dashboard_workflow_plan_from_router(
    cp: ControlPlane,
    request: Dict[str, Any],
    *,
    secret_resolver: Any,
    route_observer: Any,
) -> Dict[str, Any]:
    from mac.router_app import build_proxy_from_env

    proxy = build_proxy_from_env(secret_resolver=secret_resolver, route_observer=route_observer)
    if proxy is None:
        raise ValidationError("workflow planner requires configured MAC_ROUTER_PROVIDERS")
    model = str(request.get("model") or "*").strip() or "*"
    status, body = proxy.complete(
        "/chat/completions",
        {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are MAC's workflow planner. Produce only strict JSON. "
                        "Do not create tasks; only draft an editable plan."
                    ),
                },
                {"role": "user", "content": _dashboard_workflow_plan_prompt(cp, request)},
            ],
            "_mac_context": {
                "agent_id": "dashboard-workflow-planner",
                "task_id": "",
                "request_id": _workflow_plan_id(str(request.get("goal") or ""), request.get("project"), []),
            },
        },
        route_context={"agent_id": "dashboard-workflow-planner"},
    )
    if int(status) >= 400:
        raise ValidationError("workflow planner model request failed with HTTP %s" % status)
    return _extract_json_object(_chat_completion_content(body))


def _accept_dashboard_workflow_plan(cp: ControlPlane, request: Dict[str, Any]) -> Dict[str, Any]:
    raw_nodes = request.get("nodes") or []
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValidationError("workflow plan accept requires at least one node")
    nodes = [
        {
            "node_id": str(node.get("node_id") or "").strip(),
            "title": str(node.get("title") or "").strip(),
            "description": str(node.get("description") or ""),
            "required_capabilities": _workflow_plan_string_list(node.get("required_capabilities")),
            "depends_on": _workflow_plan_string_list(node.get("depends_on")),
            "priority": _workflow_plan_int(node.get("priority"), 0),
            "metadata": node.get("metadata") if isinstance(node.get("metadata"), dict) else {},
        }
        for node in raw_nodes
        if isinstance(node, dict)
    ]
    ordered = _workflow_plan_topological_order(nodes, allow_external=True, cp=cp)
    project = str(request.get("project") or "").strip() or None
    plan_id = str(request.get("plan_id") or _workflow_plan_id(str(request.get("goal") or ""), project, nodes))
    actor = str(request.get("actor") or "human")
    root_metadata = request.get("metadata") if isinstance(request.get("metadata"), dict) else {}
    created_by_node: Dict[str, str] = {}
    created_tasks = []
    for index, node in enumerate(ordered, start=1):
        dependency_ids = [
            created_by_node.get(dep, dep)
            for dep in _workflow_plan_string_list(node.get("depends_on"))
        ]
        metadata = dict(node.get("metadata") or {})
        origin = dict(metadata.get("origin") or {}) if isinstance(metadata.get("origin"), dict) else {}
        origin.update(
            {
                "type": "dashboard_workflow_plan",
                "plan_id": plan_id,
                "node_id": node["node_id"],
            }
        )
        metadata["origin"] = origin
        workflow = dict(metadata.get("workflow") or {}) if isinstance(metadata.get("workflow"), dict) else {}
        workflow.update(
            {
                "type": "planned_task_chain",
                "plan_id": plan_id,
                "node_id": node["node_id"],
                "node_index": index,
                "depends_on_nodes": _workflow_plan_string_list(node.get("depends_on")),
                "goal": request.get("goal") or "",
            }
        )
        metadata["workflow"] = workflow
        if root_metadata:
            metadata.setdefault("workflow_plan", root_metadata)
        task = cp.create_task(
            node["title"],
            description=str(node.get("description") or ""),
            project=project,
            priority=_workflow_plan_int(node.get("priority"), 0),
            required_capabilities=_workflow_plan_string_list(node.get("required_capabilities")),
            dependencies=dependency_ids,
            metadata=metadata,
            actor=actor,
        )
        created_by_node[node["node_id"]] = task.id
        created_tasks.append(task.to_dict())
    return {
        "schema": "mac.dashboard.workflow_plan_accept.v1",
        "plan_id": plan_id,
        "project": project,
        "goal": request.get("goal") or "",
        "node_task_ids": created_by_node,
        "created": created_tasks,
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


def _start_hub_tick_loop(app: FastAPI, cp: ControlPlane) -> None:
    """Drive the control-plane tick on the hub's own clock (mac-selfdrive).

    ``ControlPlane.tick()`` is the self-driving heartbeat: it expires stale
    leases, reconciles service-role claims, unblocks dependency-ready tasks,
    **advances the default review workflow (publishing/merging approved work to
    main)**, and dispatches ready tasks. Nothing drove it periodically, so
    approved tasks parked forever waiting for an external ``POST /dispatch/tick``
    — the whole autonomous loop (commit -> review -> merge) stalled at the last
    step. The hub now runs it on a daemon thread so no external clock is needed.

    Gated by ``MAC_HUB_TICK_INTERVAL_SECONDS`` (seconds; >0 enables). Unset/0
    means no thread, so the CLI, the test suite, and stateless mac-api replicas
    don't each spawn a competing ticker; the deploy sets it on hub nodes.
    """
    try:
        interval = float((os.environ.get("MAC_HUB_TICK_INTERVAL_SECONDS") or "0").strip())
    except ValueError:
        interval = 0.0
    if interval <= 0:
        return
    existing = getattr(app.state, "hub_tick_thread", None)
    if existing is not None and existing.is_alive():
        return

    # Event-driven review advancement rides the same gate as the tick: on the
    # hub, review-stage transitions fire the moment their triggering event
    # lands (submit_for_review, hub verdict) instead of waiting for the next
    # sweep; the periodic tick below remains the fallback for anything missed.
    cp.enable_event_driven_review_advance()
    try:
        stale_after = int(float((os.environ.get("MAC_HUB_TICK_STALE_AFTER_SECONDS") or "300").strip()))
    except ValueError:
        stale_after = 300

    stop_event = threading.Event()

    def _loop() -> None:
        # Sleep-first so app construction returns immediately and a process that
        # never lives a full interval (e.g. a unit test) does no tick work.
        while not stop_event.wait(interval):
            try:
                cp.tick(stale_after_seconds=stale_after)
            except Exception:  # noqa: BLE001 - the self-driver must never crash the hub
                logging.getLogger("mac.hub_tick").warning("hub tick failed", exc_info=True)

    thread = threading.Thread(target=_loop, name="mac-hub-tick", daemon=True)
    thread.start()
    app.state.hub_tick_stop_event = stop_event
    app.state.hub_tick_thread = thread


def _stop_hub_tick_loop(app: FastAPI) -> None:
    """Stop the lifespan-owned hub ticker without leaking it across restarts."""
    stop_event = getattr(app.state, "hub_tick_stop_event", None)
    thread = getattr(app.state, "hub_tick_thread", None)
    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=5.0)
    app.state.hub_tick_stop_event = None
    app.state.hub_tick_thread = None


def create_app(
    db_path: Optional[str] = None,
    control_plane: Optional[ControlPlane] = None,
    auth_tokens: Optional[AuthTokenMapping] = None,
    record_http_observations: Optional[bool] = None,
    client_principals_path: Optional[str] = None,
) -> FastAPI:
    # db_path is the explicit SQLite override (e.g. for tests). When it is
    # None, make_store_from_env requires MAC_DATABASE_URL or MAC_DB. This keeps
    # production construction explicit and prevents API import/startup from
    # manufacturing a private client-home authority.
    if control_plane is not None:
        cp = control_plane
    elif db_path is not None:
        cp = ControlPlane(open_postgres_store(db_path))
    else:
        cp = ControlPlane(make_store_from_env())
    # When the caller injects a control_plane or db_path directly (embedded/test
    # mode) and does not supply explicit auth_tokens, default to no-auth so the
    # injected instance behaves hermetically.  Production ``create_app()``
    # (no control_plane, no db_path) always loads tokens from the environment.
    if auth_tokens is not None:
        tokens: Dict[str, TokenPrincipal] = _normalize_auth_tokens(auth_tokens)
    elif control_plane is not None or db_path is not None:
        tokens = {}
    else:
        tokens = _load_auth_tokens_from_env()
    # Production factory invocations (``create_app()``) merge the hub-local,
    # hashed client registry on every request.  Tests and embedded callers that
    # inject a control plane stay hermetic unless they explicitly pass a path.
    # This gives SSH enrollment immediate issuance/renewal/revocation without a
    # control-plane restart while preserving the static admin recovery token.
    from mac.client_principals import ClientPrincipalProvider
    from mac.worker_credentials import (
        WorkerCredentialPolicyProvider,
        WorkerCredentialPrincipalProvider,
    )

    configured_client_path = client_principals_path or os.environ.get(
        "MAC_CLIENT_PRINCIPALS_FILE"
    )
    use_default_client_registry = control_plane is None and db_path is None
    client_principals = (
        ClientPrincipalProvider(
            Path(configured_client_path).expanduser()
            if configured_client_path
            else None
        )
        if configured_client_path or use_default_client_registry
        else None
    )
    client_registry_seen = bool(
        client_principals is not None and client_principals.path.exists()
    )
    worker_principals = WorkerCredentialPrincipalProvider(cp.store)
    worker_identity_policy = WorkerCredentialPolicyProvider(cp.store)

    def _current_auth_tokens() -> Dict[str, TokenPrincipal]:
        nonlocal client_registry_seen
        if client_principals is not None and client_principals.path.exists():
            client_registry_seen = True
        dynamic = (
            _normalize_auth_tokens(client_principals.tokens())
            if client_principals is not None
            else {}
        )
        workers = _normalize_auth_tokens(worker_principals.tokens())
        # Static environment tokens are the recovery authority if an
        # impossible hash collision or duplicate registration occurs.
        merged = {**dynamic, **workers, **tokens}
        if client_registry_seen and not merged:
            # A registry that becomes empty/corrupt after enrollment must not
            # turn a previously authenticated hub into open development mode.
            # This unmatchable hash keeps public routes public while making
            # every scoped route fail closed.
            merged["sha256:" + ("0" * 64)] = TokenPrincipal(
                scopes=frozenset({"read"})
            )
        return merged

    initial_tokens = _current_auth_tokens()
    # mac-853j: refuse to fail-open when the API is bound to a non-loopback
    # interface. Deployments that explicitly want a no-auth dev mode can
    # set MAC_API_ALLOW_OPEN=1, but the default for a 0.0.0.0 hub is
    # fail-closed (the alternative was: any tenant on the network could
    # reach /secrets/{id}/reveal).
    if not initial_tokens:
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
    repository_ref_reconciler = RepositoryRefReconciler(
        cp,
        RepositoryRefReconcilerConfig.from_env(),
    )
    # mac-ghingest: GitHub issues as an asynchronous work generator. The
    # ingestor polls opted-in repos and files idempotent mac tasks; it no-ops
    # for any project that has not set metadata["github_issue_ingest"], so
    # enabling it fleet-wide is safe.
    github_ingestor = GitHubIssueIngestor(cp, GitHubIngestConfig.from_env())
    # CI is a repository lifecycle continuation, not part of the publication
    # transaction.  The monitor periodically reconciles registered GitHub
    # repositories and follows up exact SHAs after MAC lands them.
    cicd_monitor = CICDMonitor(cp, CICDMonitorConfig.from_env())
    # Publication is the only point that has both the durable publication id
    # and the canonical integration SHA.  Give the service layer the running
    # monitor so it can atomically hand that identity to the delayed checker.
    cp._cicd_monitor = cicd_monitor
    # mac-backlog-groom: seed grooming tasks for opted-in repos going idle, so
    # the fleet manufactures its own backlog instead of starving when the
    # human/GitHub-issue queue drains. No-op until a project opts in via
    # metadata["backlog_grooming"].
    backlog_groomer = BacklogGroomer(cp, BacklogGroomerConfig.from_env())
    # mac-model-select: periodically pick the fleet's powerhouse models from a
    # web search of what's currently leading, moderated by what the gateway can
    # actually route — instead of a hard-coded, forever-pinned default. No-op
    # unless MAC_MODEL_SELECT_ENABLED is set.
    model_selection_service = ModelSelectionService(cp, ModelSelectionConfig.from_env())
    scientific_optimizer = cp.optimizer
    # mac-nap-tick: OS-agnostic nap driver inside the hub process. The old
    # systemd timer was useless on a launchd hub and the whole nap → dream →
    # repair pipeline silently died with it. No-op unless MAC_NAP_TICK_ENABLED.
    nap_ticker = NapTicker(cp, NapTickerConfig.from_env())
    # mac-curiosity-review: close the curiosity quarantine loop by filing
    # pinned adjudication tasks. No-op unless MAC_CURIOSITY_REVIEW_ENABLED.
    curiosity_reviewer = CuriosityReviewer(cp, CuriosityReviewerConfig.from_env())
    # mac-self-heal: observe → plan → act → verify over hub invariants (nap
    # liveness, task starvation, daemon heartbeats, silent read paths, stuck
    # quarantines). Violations become fleet tasks; fixes that don't hold are
    # re-filed with escalation. No-op unless MAC_SELF_HEAL_ENABLED.
    self_healing_sentinel = SelfHealingSentinel(cp, SelfHealingConfig.from_env())
    # Durable provisioning requests wake a background HGX reconciler. Provider
    # calls never run on dispatch or HTTP threads; sustained-demand and
    # step/cooldown policy prevent transient backlog from creating a worker
    # cascade. Default-off outside explicitly configured HGX hubs.
    hgx_autoscaler = HgxAutoscaler(cp, HgxAutoscalerConfig.from_env())
    # mac-pg-backup: scheduled, restore-verified PostgreSQL authority
    # backups for the hub — consistent pg_dump, owner-only artifacts,
    # retention, failure telemetry, and a periodic restore-to-scratch drill.
    # Default-ON only when the authority is PostgreSQL (MAC_DATABASE_URL);
    # a no-op on the SQLite tier and with MAC_PG_BACKUP_ENABLED=0. A
    # PostgreSQL backup failure is surfaced loudly, never downgraded to SQLite.
    pg_backup_scheduler = PgBackupScheduler(cp, PgBackupConfig.from_env())
    # The heavy integration/certification line is independent of request
    # handling and default-off. Its trigger only wakes a bounded background
    # controller; Git and OpenShell work never runs on an HTTP thread.
    work_package_pipeline = build_work_package_pipeline_runtime(cp)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Start all autonomous services from the ASGI lifecycle, never while
        # merely importing or constructing the application. Uvicorn factory
        # mode imports the module and then calls create_app(); starting the
        # ticker during construction made a legacy module-level app plus the
        # factory app run competing tick/review threads against one SQLite
        # authority, convoying its process-wide lock and stalling /health.
        _start_hub_tick_loop(_app, cp)
        repository_ref_reconciler.start()
        github_ingestor.start()
        cicd_monitor.start()
        backlog_groomer.start()
        model_selection_service.start()
        scientific_optimizer.start()
        nap_ticker.start()
        curiosity_reviewer.start()
        self_healing_sentinel.start()
        hgx_autoscaler.start()
        pg_backup_scheduler.start()
        work_package_pipeline.start()
        try:
            yield
        finally:
            _stop_hub_tick_loop(_app)
            work_package_pipeline.stop()
            pg_backup_scheduler.stop()
            hgx_autoscaler.stop()
            self_healing_sentinel.stop()
            curiosity_reviewer.stop()
            nap_ticker.stop()
            scientific_optimizer.stop()
            repository_ref_reconciler.stop()
            github_ingestor.stop()
            cicd_monitor.stop()
            backlog_groomer.stop()
            model_selection_service.stop()

    app = FastAPI(title="MAC Control Plane", version="0.1.0", lifespan=lifespan)
    # The Fleet IDE state is list-oriented JSON and compresses by roughly an
    # order of magnitude over remote SSH/Tailscale paths. Streaming responses
    # opt out explicitly below so their first event is never buffered by gzip.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    app.state.control_plane = cp
    app.state.auth_tokens = initial_tokens
    app.state.client_principals = client_principals
    app.state.worker_principals = worker_principals
    app.state.worker_identity_policy = worker_identity_policy
    app.state.managed_work_plan_bridge = cp.managed_work_plans
    app.state.repository_ref_reconciler = repository_ref_reconciler
    app.state.github_ingestor = github_ingestor
    app.state.cicd_monitor = cicd_monitor
    app.state.backlog_groomer = backlog_groomer
    app.state.model_selection_service = model_selection_service
    app.state.scientific_optimizer = scientific_optimizer
    app.state.nap_ticker = nap_ticker
    app.state.curiosity_reviewer = curiosity_reviewer
    app.state.self_healing_sentinel = self_healing_sentinel
    app.state.hgx_autoscaler = hgx_autoscaler
    app.state.work_package_pipeline = work_package_pipeline
    # th-merge-07: TokenHub is retired; its decision-feed consumer (hu-05) and
    # wildcard-ladder refresh are removed with the rest of the standalone-TokenHub
    # integration. Routing decisions now come from the in-mac router.
    app.state.hermes_startup = build_hermes_startup_report()

    def _task_is_package_linked(task_id: str) -> bool:
        from mac.work_package_store import get_work_package_task_link

        return get_work_package_task_link(cp.store, task_id) is not None

    def _package_actor_ready(principal: TokenPrincipal, agent_id: str) -> bool:
        from mac.worker_credentials import package_worker_readiness

        readiness = package_worker_readiness(cp.store, agent_id)
        return bool(
            readiness.get("ready")
            and principal.principal_kind == "worker"
            and principal.client_id == readiness.get("principal_id")
            and principal.worker_credential_version
            == readiness.get("credential_version")
            and principal.credential_fingerprint
            == readiness.get("token_fingerprint")
        )

    def _assert_task_actor(
        principal: TokenPrincipal, task_id: str, claimed_agent_id: str
    ) -> None:
        package_linked = _task_is_package_linked(task_id)
        principal.assert_actor(
            claimed_agent_id,
            package_linked=package_linked,
            package_ready=(
                _package_actor_ready(principal, claimed_agent_id)
                if package_linked
                else False
            ),
        )

    def _assert_review_actor(
        principal: TokenPrincipal, review_id: str, claimed_agent_id: str
    ) -> None:
        review = cp.get_review(review_id)
        _assert_task_actor(principal, review.task_id, claimed_agent_id)
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
        if isinstance(exc, AmbiguousIdError):
            return JSONResponse(
                status_code=400,
                content={"detail": str(exc), "candidates": exc.candidates},
            )
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
        except (MACError, StoreError):
            _log.warning("failed to record http observation for %s", request.url.path, exc_info=True)

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Any:
        started = time.monotonic()
        status_code = 500
        error_name = ""
        public_route = _required_scope(request.method, request.url.path) is None
        try:
            # Public liveness/discovery routes do not need a principal. Avoid
            # consulting the dynamic worker-principal registry for them: that
            # registry is SQLite-backed, so a legitimate long maintenance
            # transaction could otherwise make /health wait on the store lock
            # and provoke the supervisor into killing a busy-but-live hub.
            auth_tokens_for_request = (
                {}
                if public_route
                else await asyncio.to_thread(_current_auth_tokens)
            )
            principal = _authorize_request(
                request.method,
                request.url.path,
                request.headers.get("authorization"),
                auth_tokens_for_request,
            )
            if principal is not None:
                principal = replace(
                    principal,
                    worker_identity_mode=await asyncio.to_thread(
                        lambda: worker_identity_policy.mode
                    ),
                )
            request.state.principal = principal
        except AuthorizationError as exc:
            status_code = 403
            error_name = exc.__class__.__name__
            await asyncio.to_thread(
                _emit_http_observation,
                request,
                status_code,
                started,
                error_name,
            )
            return JSONResponse(status_code=status_code, content={"detail": str(exc)})

        # Public liveness/discovery traffic has no agent identity or durable
        # work to trace. Keep it out of the optional native Relay request scope
        # and flush path so those best-effort exporters can never become part of
        # the supervisor's liveness contract.
        if public_route:
            return await call_next(request)

        # NeMo Relay: open an Agent scope per HTTP request when relay is active.
        # The session_id is taken from the X-Session-Id header when present, or
        # generated from the request path + method so every request maps to a
        # stable, unique scope name.  No-op when relay is absent or disabled.
        relay_session_id = (
            request.headers.get("x-session-id")
            or request.headers.get("x-mac-session-id")
            or "http.%s.%s" % (request.method.lower(), request.url.path.replace("/", ".").strip("."))
        )
        try:
            with _relay_agent_scope(relay_session_id):
                try:
                    response = await call_next(request)
                    status_code = int(getattr(response, "status_code", 500))
                    return response
                except Exception as exc:
                    error_name = exc.__class__.__name__
                    raise
                finally:
                    await asyncio.to_thread(
                        _emit_http_observation,
                        request,
                        status_code,
                        started,
                        error_name,
                    )
        finally:
            await asyncio.to_thread(_relay_flush)

    # th-merge-04: let model-provider keys be `secret:<name>`, resolved
    # decrypt-at-use from the in-mac encrypted key store. Shared by the /v1
    # router and the dashboard workflow planner so human UI planning does not
    # need an agent-scope token or local upstream keys.
    def _router_secret_resolver(name: str) -> Optional[str]:
        secrets = getattr(cp, "secrets", None)
        if secrets is None:
            return None
        try:
            return secrets.resolve_secret_value(name, purpose="router-upstream")
        except Exception:  # noqa: BLE001 - a missing/disabled secret must not break routing
            return None

    def _router_route_observer(detail: Dict[str, Any]) -> None:
        agent_id = str(detail.get("agent_id") or "").strip()
        task_id = str(detail.get("task_id") or "").strip()
        # The task is the durable join for experiment and per-task usage
        # reports.  Agent identity remains available as the source and in the
        # detail object.
        subject_type = "task" if task_id else "agent" if agent_id else None
        subject_id = task_id or agent_id or None
        source = _safe_observation_source(agent_id or "router")
        try:
            cp.record_log(
                "llm.route",
                level=(
                    "error"
                    if int(detail.get("status_code") or 0) >= 500
                    else "warning"
                    if int(detail.get("status_code") or 0) >= 400
                    else "info"
                ),
                layer="router",
                source=source,
                subject_type=subject_type,
                subject_id=subject_id,
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - inference must not fail because telemetry failed
            _log.warning("failed to record llm.route observation", exc_info=True)

    app.include_router(
        build_system_router(
            SystemRouteServices(
                repository_ref_reconciler=repository_ref_reconciler,
                github_ingestor=github_ingestor,
                cicd_monitor=cicd_monitor,
                backlog_groomer=backlog_groomer,
                nap_ticker=nap_ticker,
                curiosity_reviewer=curiosity_reviewer,
                self_healing_sentinel=self_healing_sentinel,
                model_selection_service=model_selection_service,
                work_package_pipeline=work_package_pipeline,
            ),
            get_principal=_get_principal,
        )
    )

    def _a2a_base_url(request: Request) -> str:
        # The externally-visible origin the caller used to reach mac, so the
        # AgentCard advertises a ``url`` a client can actually address. Honor a
        # reverse-proxy's forwarded host/proto when present (the hub may sit
        # behind one); fall back to the request's own base URL.
        forwarded_host = request.headers.get("x-forwarded-host")
        forwarded_proto = request.headers.get("x-forwarded-proto")
        if forwarded_host:
            scheme = (forwarded_proto or request.url.scheme or "https").split(",")[0].strip()
            host = forwarded_host.split(",")[0].strip()
            return "%s://%s" % (scheme, host)
        return str(request.base_url).rstrip("/")

    @app.get("/.well-known/agent-card.json")
    @app.get("/.well-known/agent.json")
    def a2a_agent_card_route(request: Request) -> Dict[str, Any]:
        # A2A AgentCard discovery (Phase 4): the unauthenticated "business card"
        # an external A2A agent fetches to learn mac's identity, A2A endpoint,
        # capabilities, and skills. Pure data; the canonical path is
        # agent-card.json (A2A v0.3+), agent.json is a legacy alias. No
        # control-plane access.
        from mac.a2a.card import agent_card

        return agent_card(_a2a_base_url(request))

    @app.post("/a2a")
    async def a2a_rpc_route(request: Request) -> JSONResponse:
        # A2A JSON-RPC 2.0 endpoint (Phase 4): an external agent delegates work
        # here (message/send -> mac task; tasks/get; tasks/cancel). Requires the
        # agent scope (see _required_scope). Builds the A2AService from the
        # app's control plane, mirroring how other routes use ``cp``.
        from mac.a2a.protocol import (
            ERROR_INVALID_REQUEST,
            ERROR_PARSE,
            rpc_error,
        )
        from mac.a2a.service import A2AService

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - malformed body is a JSON-RPC parse error
            return JSONResponse(content=rpc_error(None, ERROR_PARSE, "invalid JSON body"))
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or "method" not in payload:
            rpc_id = payload.get("id") if isinstance(payload, dict) else None
            return JSONResponse(
                content=rpc_error(
                    rpc_id, ERROR_INVALID_REQUEST, "not a valid JSON-RPC 2.0 request"
                )
            )
        service = A2AService(cp)
        result = service.handle_rpc(
            str(payload.get("method")), payload.get("params"), payload.get("id")
        )
        return JSONResponse(content=result)

    @app.websocket("/acp/ws")
    async def acp_websocket(websocket: WebSocket) -> None:
        # ADR 0006 Phase 2 remote transport: an external ACP client drives a mac
        # agent over WebSocket, reusing the same ACPAgentServer/Peer the stdio
        # path uses. The HTTP auth middleware does not run for websocket scope,
        # so we validate the bearer token here, before accepting the socket.
        #
        # Token sources (first match wins):
        #   * ``?token=<bearer>`` query param, or
        #   * an ``Authorization`` WebSocket subprotocol: clients offer
        #     ``["Authorization", "<bearer>"]`` (the bearer rides as the second
        #     subprotocol value, since browsers can't set Authorization headers
        #     on a WS handshake). We echo ``Authorization`` back as the accepted
        #     subprotocol.
        current_tokens = _current_auth_tokens()
        principal, accepted_subprotocol = _authorize_acp_websocket(websocket, current_tokens)
        if principal is None and current_tokens:
            # tokens configured but no valid principal -> reject (1008 policy).
            await websocket.close(code=1008)
            return

        # Backend selection mirrors serve_stdio: the production MacAgentBackend
        # when an agent command is configured, else the harmless EchoBackend.
        from mac.acp.server import EchoBackend

        if os.environ.get("MAC_ACP_BACKEND_CMD"):
            from mac.acp.backend import MacAgentBackend

            backend: Any = MacAgentBackend()
        else:
            backend = EchoBackend()

        from mac.acp.ws import serve_acp_websocket

        accept_kwargs = (
            {"subprotocol": accepted_subprotocol} if accepted_subprotocol else None
        )
        await serve_acp_websocket(websocket, backend, accept_kwargs=accept_kwargs)

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
        view: Optional[str] = Query(default=None),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        if view != "ide":
            app.state.hermes_startup = build_hermes_startup_report()
        return _dashboard_response(
            cp,
            principal,
            app.state.hermes_startup,
            view=view,
        )

    @app.get("/dashboard/stream")
    async def dashboard_stream(
        request: Request,
        timeout_seconds: float = Query(default=60.0),
        poll_interval_seconds: float = Query(default=1.0),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> StreamingResponse:
        def stream_event(event: str, cursor: int) -> str:
            now = utcnow()
            return json.dumps(
                {
                    "event": event,
                    "server_time": now,
                    "updated_at": now,
                    "observability_sequence": cursor,
                },
                sort_keys=True,
            ) + "\n"

        async def iter_dashboard_states() -> Any:
            latest = cp.list_observability(limit=1)
            cursor = latest[0].sequence if latest else 0
            deadline = time.monotonic() + _agentbus_clamp_timeout(timeout_seconds)
            poll_interval = _agentbus_clamp_poll_interval(poll_interval_seconds)
            yield stream_event("connected", cursor)
            while True:
                if await request.is_disconnected():
                    break
                observations = cp.list_observability(after_sequence=cursor, limit=100)
                if observations:
                    cursor = observations[-1].sequence
                    if any(
                        _dashboard_stream_observation_relevant(observation)
                        for observation in observations
                    ):
                        yield stream_event("updated", cursor)
                    if time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(0)
                    continue
                if time.monotonic() >= deadline:
                    yield stream_event("heartbeat", cursor)
                    break
                await asyncio.sleep(poll_interval)

        return StreamingResponse(
            iter_dashboard_states(),
            media_type="application/x-ndjson",
            headers={"Content-Encoding": "identity"},
        )

    @app.post("/dashboard/workflow-plan/preview")
    def dashboard_workflow_plan_preview(
        body: DashboardWorkflowPlanRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _ensure_payload_bounded(body.context, "dashboard.workflow_plan.context")
        data = _data(body)
        planner = getattr(app.state, "workflow_plan_model", None)
        if callable(planner):
            raw = planner(data)
            if str(data.get("mode") or "legacy").strip().lower() == MANAGED_WORK_PLAN_MODE:
                return app.state.managed_work_plan_bridge.preview(
                    raw,
                    request=data,
                    source="injected",
                ).to_dict()
            return _normalize_dashboard_workflow_plan(raw, data, source="injected")
        raw = _dashboard_workflow_plan_from_router(
            cp,
            data,
            secret_resolver=_router_secret_resolver,
            route_observer=_router_route_observer,
        )
        if str(data.get("mode") or "legacy").strip().lower() == MANAGED_WORK_PLAN_MODE:
            return app.state.managed_work_plan_bridge.preview(
                raw,
                request=data,
                source="model",
            ).to_dict()
        return _normalize_dashboard_workflow_plan(raw, data, source="model")

    @app.post("/dashboard/workflow-plan/accept")
    def dashboard_workflow_plan_accept(
        body: DashboardWorkflowPlanAccept,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _ensure_payload_bounded(body.metadata, "dashboard.workflow_plan.metadata")
        data = _data(body)
        if str(data.get("mode") or "legacy").strip().lower() == MANAGED_WORK_PLAN_MODE:
            principal.require_admin()
            plan = (
                dict(body.plan)
                if isinstance(body.plan, dict)
                else managed_plan_from_dashboard_accept(data)
            )
            _ensure_payload_bounded(plan, "dashboard.managed_work_plan.plan")
            return app.state.managed_work_plan_bridge.accept(
                plan,
                actor=body.actor,
                reason=body.reason,
                tenant_id=body.tenant_id,
                root_task_id=body.root_task_id,
            ).to_dict()
        for index, node in enumerate(data.get("nodes") or [], start=1):
            if isinstance(node, dict):
                _ensure_payload_bounded(
                    node.get("metadata") or {},
                    "dashboard.workflow_plan.nodes.%d.metadata" % index,
                )
        return _accept_dashboard_workflow_plan(cp, data)

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

    @app.put("/dashboard/hermes/fleets/{fleet_id_or_name}/config-surface")
    def dashboard_hermes_config_surface_update(
        fleet_id_or_name: str,
        body: DashboardHermesConfigUpdate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        data = _data(body)
        for key in ("runtime", "config", "env", "plugins", "skills"):
            _ensure_payload_bounded(data.get(key) or {}, "dashboard.hermes_config.%s" % key)
        fleet = cp.get_fleet(fleet_id_or_name).to_dict()
        return update_fleet_hermes_surface(
            fleet,
            data,
            apply_local=bool(data.get("apply_local", True)),
        )

    @app.post("/dashboard/hermes/fleets/{fleet_id_or_name}/config-surface/apply")
    def dashboard_hermes_config_surface_apply(
        fleet_id_or_name: str,
        body: DashboardHermesConfigApply,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        fleet = cp.get_fleet(fleet_id_or_name).to_dict()
        recipients = list(body.recipient_agent_ids or [])
        if not recipients:
            recipients = list(fleet.get("agent_ids") or [])
        recipients = list(dict.fromkeys(str(item) for item in recipients if str(item)))
        if not recipients:
            raise ValidationError("Hermes config apply requires at least one fleet agent")
        sender_agent_id = str(body.sender_agent_id or recipients[0])
        cp.get_agent(sender_agent_id)
        for recipient_id in recipients:
            cp.get_agent(recipient_id)
        payload = fleet_hermes_payload(fleet)
        message = hermes_config_apply_payload(
            payload=payload,
            fleet_id=str(fleet.get("id") or ""),
            fleet_name=str(fleet.get("name") or ""),
            registry_path=os.environ.get("MAC_FLEETS_CONFIG") or os.environ.get("MAC_DEPLOY_FLEETS_CONFIG") or "",
            request_id=body.request_id,
        )
        published = [
            cp.publish_agentbus_content(
                sender_agent_id=sender_agent_id,
                recipient_agent_id=recipient_id,
                content_type=HERMES_CONFIG_APPLY_CONTENT_TYPE,
                topic=HERMES_CONFIG_APPLY_TOPIC,
                payload=message,
            )
            for recipient_id in recipients
        ]
        return {
            "schema": "mac.dashboard.hermes_config_apply.v1",
            "count": len(published),
            "fleet_id": fleet.get("id"),
            "fleet_name": fleet.get("name"),
            "sender_agent_id": sender_agent_id,
            "recipient_agent_ids": recipients,
            "payload_digest": payload_digest(payload),
            "payload_redacted": redacted_hermes_payload(payload),
            "streams": [item["stream"] for item in published],
        }

    @app.get("/dashboard/terminal-sessions")
    def dashboard_terminal_sessions(
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = Query(default=120),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _require_terminal_principal(principal)
        query_agent_id = agent_id
        if principal.agent_id and not principal.is_admin:
            if query_agent_id and query_agent_id != principal.agent_id:
                principal.assert_actor(query_agent_id)
            query_agent_id = principal.agent_id
        sessions = _dashboard_terminal_sessions(
            cp,
            agent_id=query_agent_id,
            status=status,
            limit=limit,
        )
        if principal.agent_id and not principal.is_admin:
            sessions = [
                item for item in sessions
                if principal.agent_id in {item.get("agent_id"), item.get("sender_agent_id")}
            ]
        return {
            "schema": "mac.dashboard.terminal_sessions.v1",
            "terminal_sessions": sessions,
        }

    @app.post("/dashboard/agents/{agent_id}/terminal-sessions")
    def dashboard_terminal_session_open(
        agent_id: str,
        body: DashboardTerminalOpen,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _require_terminal_principal(principal)
        agent = cp.get_agent(agent_id)
        sender_agent_id = str(body.sender_agent_id or agent.id)
        cp.get_agent(sender_agent_id)
        if principal.agent_id and not principal.is_admin:
            principal.assert_actor(agent.id)
            principal.assert_actor(sender_agent_id)
        session_id = _new_terminal_session_id()
        input_stream_id = session_id + ".in"
        output_stream_id = session_id + ".out"
        rows = _clamp_int(body.rows, MIN_TERMINAL_ROWS, MAX_TERMINAL_ROWS, 32)
        cols = _clamp_int(body.cols, MIN_TERMINAL_COLS, MAX_TERMINAL_COLS, 120)
        ttl_seconds = _clamp_int(
            body.ttl_seconds,
            MIN_TERMINAL_TTL_SECONDS,
            MAX_TERMINAL_TTL_SECONDS,
            900,
        )
        if body.shell and len(body.shell) > 512:
            raise ValidationError("terminal shell exceeds 512-byte limit")
        if body.cwd and len(body.cwd) > 4096:
            raise ValidationError("terminal cwd exceeds 4096-byte limit")
        base_headers = {
            "terminal_session_id": session_id,
            "request_id": body.request_id or "",
        }
        input_stream = cp.open_agentbus_stream(
            sender_agent_id=sender_agent_id,
            recipient_agent_id=agent.id,
            content_type=DEBUG_TERMINAL_INPUT_CONTENT_TYPE,
            topic=DEBUG_TERMINAL_INPUT_TOPIC,
            headers={**base_headers, "schema": DEBUG_TERMINAL_INPUT_SCHEMA},
            stream_id=input_stream_id,
        )
        output_stream = cp.open_agentbus_stream(
            sender_agent_id=agent.id,
            recipient_agent_id=sender_agent_id,
            content_type=DEBUG_TERMINAL_OUTPUT_CONTENT_TYPE,
            topic=DEBUG_TERMINAL_OUTPUT_TOPIC,
            headers={**base_headers, "schema": DEBUG_TERMINAL_OUTPUT_SCHEMA},
            stream_id=output_stream_id,
        )
        control = cp.publish_agentbus_content(
            sender_agent_id=sender_agent_id,
            recipient_agent_id=agent.id,
            content_type=DEBUG_TERMINAL_OPEN_CONTENT_TYPE,
            topic=DEBUG_TERMINAL_OPEN_TOPIC,
            payload=debug_terminal_open_payload(
                session_id=session_id,
                input_stream_id=input_stream_id,
                output_stream_id=output_stream_id,
                sender_agent_id=sender_agent_id,
                shell=body.shell,
                cwd=body.cwd,
                rows=rows,
                cols=cols,
                ttl_seconds=ttl_seconds,
                request_id=body.request_id,
            ),
        )
        return {
            "schema": "mac.dashboard.terminal_session.v1",
            "session_id": session_id,
            "agent_id": agent.id,
            "sender_agent_id": sender_agent_id,
            "input_stream_id": input_stream_id,
            "output_stream_id": output_stream_id,
            "rows": rows,
            "cols": cols,
            "ttl_seconds": ttl_seconds,
            "input_stream": input_stream.to_dict(),
            "output_stream": output_stream.to_dict(),
            "control_stream": control.get("stream"),
        }

    @app.post("/dashboard/terminal-sessions/{session_id}/input")
    def dashboard_terminal_session_input(
        session_id: str,
        body: DashboardTerminalInput,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _require_terminal_principal(principal)
        stream = _terminal_stream_for_session(
            cp,
            session_id=session_id,
            stream_id=body.input_stream_id,
            expected_topic=DEBUG_TERMINAL_INPUT_TOPIC,
            expected_content_type=DEBUG_TERMINAL_INPUT_CONTENT_TYPE,
            expected_schema=DEBUG_TERMINAL_INPUT_SCHEMA,
        )
        sender_agent_id = str(body.sender_agent_id or stream["sender_agent_id"])
        if sender_agent_id != stream["sender_agent_id"]:
            raise AuthorizationError("terminal input sender must match input stream sender")
        if principal.agent_id and not principal.is_admin:
            principal.assert_actor(sender_agent_id)
        payload = debug_terminal_input_payload(
            session_id=_validate_terminal_session_id(session_id),
            data_b64=_terminal_input_data_b64(body),
            close=bool(body.close),
        )
        chunk = cp.append_agentbus_chunk(
            body.input_stream_id,
            sender_agent_id=sender_agent_id,
            payload=payload,
            final=bool(body.close),
        )
        return {"schema": "mac.dashboard.terminal_input.v1", "chunk": chunk.to_dict()}

    @app.post("/dashboard/terminal-sessions/{session_id}/resize")
    def dashboard_terminal_session_resize(
        session_id: str,
        body: DashboardTerminalResize,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _require_terminal_principal(principal)
        stream = _terminal_stream_for_session(
            cp,
            session_id=session_id,
            stream_id=body.input_stream_id,
            expected_topic=DEBUG_TERMINAL_INPUT_TOPIC,
            expected_content_type=DEBUG_TERMINAL_INPUT_CONTENT_TYPE,
            expected_schema=DEBUG_TERMINAL_INPUT_SCHEMA,
        )
        sender_agent_id = str(body.sender_agent_id or stream["sender_agent_id"])
        if sender_agent_id != stream["sender_agent_id"]:
            raise AuthorizationError("terminal resize sender must match input stream sender")
        if principal.agent_id and not principal.is_admin:
            principal.assert_actor(sender_agent_id)
        rows = _clamp_int(body.rows, MIN_TERMINAL_ROWS, MAX_TERMINAL_ROWS, 32)
        cols = _clamp_int(body.cols, MIN_TERMINAL_COLS, MAX_TERMINAL_COLS, 120)
        chunk = cp.append_agentbus_chunk(
            body.input_stream_id,
            sender_agent_id=sender_agent_id,
            payload=debug_terminal_input_payload(
                session_id=_validate_terminal_session_id(session_id),
                rows=rows,
                cols=cols,
            ),
        )
        return {
            "schema": "mac.dashboard.terminal_resize.v1",
            "rows": rows,
            "cols": cols,
            "chunk": chunk.to_dict(),
        }

    @app.post("/dashboard/terminal-sessions/{session_id}/close")
    def dashboard_terminal_session_close(
        session_id: str,
        body: DashboardTerminalClose,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _require_terminal_principal(principal)
        stream = _terminal_stream_for_session(
            cp,
            session_id=session_id,
            stream_id=body.input_stream_id,
            expected_topic=DEBUG_TERMINAL_INPUT_TOPIC,
            expected_content_type=DEBUG_TERMINAL_INPUT_CONTENT_TYPE,
            expected_schema=DEBUG_TERMINAL_INPUT_SCHEMA,
        )
        sender_agent_id = str(body.sender_agent_id or stream["sender_agent_id"])
        if sender_agent_id != stream["sender_agent_id"]:
            raise AuthorizationError("terminal close sender must match input stream sender")
        if principal.agent_id and not principal.is_admin:
            principal.assert_actor(sender_agent_id)
        if stream.get("status") != "open":
            return {
                "schema": "mac.dashboard.terminal_close.v1",
                "stream": stream,
                "chunk": None,
            }
        chunk = cp.append_agentbus_chunk(
            body.input_stream_id,
            sender_agent_id=sender_agent_id,
            payload=debug_terminal_input_payload(
                session_id=_validate_terminal_session_id(session_id),
                close=True,
            ),
            final=True,
        )
        return {
            "schema": "mac.dashboard.terminal_close.v1",
            "chunk": chunk.to_dict(),
            "stream": cp.get_agentbus_stream(body.input_stream_id).to_dict(),
        }

    @app.get("/dashboard/terminal-sessions/{session_id}/events")
    async def dashboard_terminal_session_events(
        session_id: str,
        request: Request,
        output_stream_id: str,
        after_sequence: int = Query(default=0),
        timeout_seconds: float = Query(default=30.0),
        poll_interval_seconds: float = Query(default=0.25),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> StreamingResponse:
        _require_terminal_principal(principal)
        stream = _terminal_stream_for_session(
            cp,
            session_id=session_id,
            stream_id=output_stream_id,
            expected_topic=DEBUG_TERMINAL_OUTPUT_TOPIC,
            expected_content_type=DEBUG_TERMINAL_OUTPUT_CONTENT_TYPE,
            expected_schema=DEBUG_TERMINAL_OUTPUT_SCHEMA,
        )
        if principal.agent_id and not principal.is_admin:
            allowed = {stream.get("sender_agent_id"), stream.get("recipient_agent_id")}
            if principal.agent_id not in allowed:
                raise AuthorizationError("token is not bound to this terminal stream")
        accessor_agent_id = str(stream.get("recipient_agent_id") or stream.get("sender_agent_id") or "")

        async def iter_events() -> Any:
            cursor = max(0, int(after_sequence))
            deadline = time.monotonic() + _agentbus_clamp_timeout(timeout_seconds)
            poll_interval = _agentbus_clamp_poll_interval(poll_interval_seconds)
            while True:
                if await request.is_disconnected():
                    break
                chunks = cp.read_agentbus_chunks(
                    accessor_agent_id,
                    output_stream_id,
                    cursor,
                    limit=100,
                )
                for chunk in chunks:
                    cursor = chunk.sequence
                    yield json.dumps(chunk.to_dict(), sort_keys=True) + "\n"
                if chunks:
                    if time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(0)
                    continue
                if cp.get_agentbus_stream(output_stream_id).status != "open":
                    break
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(poll_interval)

        return StreamingResponse(iter_events(), media_type="application/x-ndjson")

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

    @app.post("/humans")
    def register_human(
        body: HumanRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.register_human(**_data(body)).to_dict()

    @app.get("/humans")
    def list_humans(group: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [human.to_dict() for human in cp.list_humans(group=group)]

    @app.get("/humans/resolve")
    def resolve_human(anchor: str = Query()) -> Dict[str, Any]:
        return cp.resolve_identity_chain(anchor).to_dict()

    @app.get("/humans/{human_id}")
    def get_human(human_id: str) -> Dict[str, Any]:
        return cp.get_human(human_id).to_dict()

    @app.delete("/humans/{human_id}")
    def delete_human(human_id: str) -> Dict[str, Any]:
        cp.delete_human(human_id)
        return {"deleted": human_id}

    @app.post("/persona-instances")
    def register_persona_instance(
        body: PersonaInstanceRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.register_persona_instance(**_data(body)).to_dict()

    @app.get("/persona-instances")
    def list_persona_instances(tenant_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [instance.to_dict() for instance in cp.list_persona_instances(tenant_id)]

    @app.get("/persona-instances/{instance_id}/context")
    def persona_context(instance_id: str) -> Dict[str, Any]:
        return cp.persona_context(instance_id)

    @app.get("/persona-instances/{instance_id}/work-context")
    def persona_work_context(
        instance_id: str,
        include_completed: bool = Query(default=True),
        task_limit: int = Query(default=100),
    ) -> Dict[str, Any]:
        return cp.persona_work_context(
            instance_id,
            include_completed=include_completed,
            task_limit=task_limit,
        )

    @app.get("/persona-instances/{instance_id}/runtime-proof")
    def persona_runtime_proof(instance_id: str) -> Dict[str, Any]:
        app.state.hermes_startup = build_hermes_startup_report()
        return cp.persona_runtime_proof(
            instance_id,
            hermes_startup=app.state.hermes_startup,
        )

    @app.post("/persona-instances/{instance_id}/runtime-proof")
    def persona_runtime_proof_with_startup(
        instance_id: str,
        body: PersonaRuntimeProofCreate,
    ) -> Dict[str, Any]:
        proof = cp.persona_runtime_proof(
            instance_id,
            hermes_startup=body.hermes_startup,
        )
        cp.record_persona_runtime_proof(instance_id, proof)
        return proof

    @app.post("/persona-instances/{instance_id}/tasks")
    def create_interaction_task(
        instance_id: str,
        body: InteractionTaskCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        instance = cp.get_persona_instance(instance_id)
        principal.assert_tenant(instance.tenant_id)
        data = _data(body)
        actor = data.pop("actor", "hermes")
        metadata = dict(data.get("metadata") or {})
        publication_lane_policy = data.pop("publication_lane_policy", None)
        if publication_lane_policy is not None:
            metadata["publication_lane_policy"] = publication_lane_policy
            if publication_lane_policy == "managed":
                metadata["no_decompose"] = True
        if str(metadata.get("publication_lane_policy") or "auto").lower() == "legacy":
            principal.require_admin()
        data["metadata"] = metadata
        return cp.create_interaction_task(
            instance_id,
            actor=actor,
            _allow_legacy_publication=principal.is_admin,
            _idempotency_scope=_task_create_idempotency_scope(
                principal,
                surface="hermes-instance:%s" % instance_id,
            ),
            **data,
        ).to_dict()

    @app.post("/persona-instances/{instance_id}/openclaw-executions")
    def begin_openclaw_execution(
        instance_id: str,
        body: OpenClawDirectExecutionBegin,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Any:
        from mac.openclaw_direct_execution import (
            Capability,
            HumanDirective,
            MissingCapabilityError,
            RepositoryTarget,
            SlackProvenance,
        )

        instance = cp.get_persona_instance(instance_id)
        principal.assert_tenant(instance.tenant_id)
        requested = None
        if body.requested_capabilities is not None:
            try:
                requested = [Capability(c) for c in body.requested_capabilities]
            except ValueError as exc:
                return JSONResponse(status_code=400, content={"detail": str(exc)})
        try:
            execution = cp.openclaw_direct_execution.begin_conversation_execution(
                persona_instance_id=instance_id,
                directive=HumanDirective(
                    human_id=body.human_id,
                    authenticated=body.authenticated,
                    text=body.directive_text,
                ),
                slack=SlackProvenance(
                    workspace_id=body.slack_workspace_id,
                    channel_id=body.slack_channel_id,
                    thread_ts=body.slack_thread_ts,
                    message_ts=body.slack_message_ts,
                ),
                repository=RepositoryTarget(
                    repository_id=body.repository_id,
                    repository_name=body.repository_name,
                    base_sha=body.base_sha,
                ),
                agent_id=body.agent_id,
                requested_capabilities=requested,
                deferred=body.deferred,
                delegated=body.delegated,
                autonomous_followup=body.autonomous_followup,
                requested_followup=body.requested_followup,
                metadata=body.metadata,
            )
        except MissingCapabilityError as exc:
            # Fail closed: report the missing capability accurately with a 409,
            # never a fabricated success.
            return JSONResponse(status_code=409, content={"detail": exc.to_dict()})
        return execution.to_dict()

    @app.get("/openclaw-executions/{execution_id}")
    def get_openclaw_execution(
        execution_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        execution = cp.openclaw_direct_execution.get_execution(execution_id)
        principal.assert_tenant(execution.tenant_id)
        return execution.to_dict()

    @app.post("/platform-bindings")
    def register_platform_binding(
        body: PlatformBindingRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        data = _data(body)
        # ``hermes_instance_id`` is the deprecated alias; the service consumes
        # ``persona_instance_id``. The validator has already coalesced them.
        data.pop("hermes_instance_id", None)
        data["persona_instance_id"] = body.persona_instance_id
        return cp.register_platform_binding(**data).to_dict()

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
        publication_lane_policy = data.pop("publication_lane_policy", None)
        if publication_lane_policy is not None:
            metadata["publication_lane_policy"] = publication_lane_policy
            if publication_lane_policy == "managed":
                metadata["no_decompose"] = True
            data["metadata"] = metadata
        effective_publication_policy = str(
            metadata.get("publication_lane_policy") or "auto"
        ).strip().lower()
        if effective_publication_policy == "legacy":
            # Automatic managed admission is a controller-selected hardening
            # of ordinary task creation. Downgrading an otherwise eligible
            # task bypasses that certification/landing path and is therefore
            # an administrative migration operation, not ordinary write scope.
            principal.require_admin()
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
        created = cp.create_task(
            actor=actor,
            _allow_legacy_publication=principal.is_admin,
            _idempotency_scope=_task_create_idempotency_scope(
                principal,
                surface="tasks",
            ),
            **data,
        )
        result = created.to_dict()
        route = cp.task_publication_route(created.id)
        result["publication_lane"] = route["lane"]
        result["publication_route"] = route
        return result

    @app.get("/tasks")
    def list_tasks(
        state: Optional[str] = Query(default=None),
        tenant_id: Optional[str] = Query(default=None),
        view: Optional[str] = Query(default=None),
        project: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        tasks = [task.to_dict() for task in cp.list_tasks(state, tenant_id, project=project, limit=limit)]
        routes = cp.task_publication_routes(
            (task["id"] for task in tasks), compact=True
        )
        for task in tasks:
            route = routes[task["id"]]
            task["publication_lane"] = route["lane"]
            task["publication_route"] = route
        if view == "summary":
            tasks = [
                {k: v for k, v in t.items() if k in _TASK_LIST_SUMMARY_FIELDS}
                for t in tasks
            ]
        return tasks

    # parity-ready-http-01: serve ready/search/stats so the CLI works in hub
    # mode (not just --db). Registered before /tasks/{task_id} so these static
    # paths aren't captured by the path parameter.
    @app.get("/tasks/ready")
    def ready_tasks(
        project: Optional[str] = Query(default=None),
        tenant_id: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [t.to_dict() for t in cp.ready_tasks(project=project, tenant_id=tenant_id, limit=limit)]

    @app.get("/tasks/ready/explain")
    def ready_task_explanations(
        project: Optional[str] = Query(default=None),
        tenant_id: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        # This is an operator overview, not the dispatcher's claim loop. Bound
        # its working set and reuse one fleet snapshot; previously every task
        # reloaded and deserialized every agent plus repeated the task lookup.
        limit_value = min(max(1, int(limit or 100)), 100)
        agents = cp.list_agents()
        return [
            cp.explain_task_dispatch(task, agents=agents)
            for task in cp.ready_tasks(
                project=project,
                tenant_id=tenant_id,
                limit=limit_value,
            )
        ]

    @app.get("/tasks/search")
    def search_tasks(
        q: str = Query(...),
        project: Optional[str] = Query(default=None),
        tenant_id: Optional[str] = Query(default=None),
        limit: int = Query(default=50),
    ) -> List[Dict[str, Any]]:
        return [t.to_dict() for t in cp.search_tasks(q, project=project, tenant_id=tenant_id, limit=limit)]

    @app.get("/tasks/stats")
    def task_stats(
        project: Optional[str] = Query(default=None),
        tenant_id: Optional[str] = Query(default=None),
    ) -> Dict[str, int]:
        return cp.task_stats(project=project, tenant_id=tenant_id)

    # Registered alongside the other static /tasks/* reads so it is not
    # captured by the /tasks/{task_id} path parameter.
    @app.get("/tasks/generator-yield")
    def task_generator_yield() -> Dict[str, Any]:
        return cp.generator_yield_report()

    # Admin-only: applying this re-supervises live tasks in bulk. Registered
    # with the other static /tasks/* routes so /tasks/{task_id} cannot capture
    # it.
    @app.post("/tasks/recover-stranded")
    def task_recover_stranded(
        body: Optional[Dict[str, Any]] = None,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        data = body or {}
        return cp.recover_stranded_dependents(
            limit=data.get("limit", 500),
            dry_run=bool(data.get("dry_run", True)),
            max_rounds=data.get("max_rounds", 10),
        )

    @app.get("/tasks/throughput")
    def task_throughput(
        project: Optional[str] = Query(default=None),
        since_hours: float = Query(default=24.0, gt=0, le=24 * 90),
        warning_seconds: float = Query(default=300.0, gt=0),
        critical_seconds: float = Query(default=600.0, gt=0),
        refresh_limit: int = Query(default=100, ge=0, le=500),
    ) -> Dict[str, Any]:
        """Materialize and report task throughput, stranding, and collisions."""

        return cp.task_flow_report(
            project=project,
            since_hours=since_hours,
            warning_seconds=warning_seconds,
            critical_seconds=critical_seconds,
            refresh_limit=refresh_limit,
        )

    @app.get("/diagnostics")
    def diagnostics(
        check: Optional[List[str]] = Query(default=None),
    ) -> Dict[str, Any]:
        """Hub-native read-only control-plane health report.

        Runs every registered diagnostic (or just the requested ``check``
        names) against this hub's authoritative backend and returns the
        ``mac.diagnostics.report.v1`` document, including the ``data_source``
        identity block. Serving the report here is what lets a remote client
        run diagnostics without direct SQL access or a local database.
        """
        return cp.diagnostics_report(names=check or None)

    @app.get("/tasks/audit")
    def audit_tasks(
        project: Optional[str] = Query(default=None),
        verify_git: bool = Query(default=True),
        offset: int = Query(default=0, ge=0),
        limit: Optional[int] = Query(default=None, ge=1, le=500),
    ) -> Dict[str, Any]:
        """Point-in-time, read-only reconciliation of all task states."""

        return cp.task_ledger_audit(project=project, verify_git=verify_git, offset=offset, limit=limit)

    @app.get("/tasks/{task_id}")
    def get_task(
        task_id: str,
        view: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        if view == "compact":
            return _dashboard_task(cp, task_id, compact=True)
        return cp.task_detail(task_id)

    @app.get("/tasks/{task_id}/dispatch-explain")
    def task_dispatch_explain(task_id: str) -> Dict[str, Any]:
        return cp.explain_task_dispatch(task_id, record_observation=True)

    @app.get("/tasks/{task_id}/publication-route")
    def task_publication_route(task_id: str) -> Dict[str, Any]:
        return cp.task_publication_route(task_id)

    @app.get("/tasks/{task_id}/break-glass-authorizations")
    def list_break_glass_authorizations(
        task_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> List[Dict[str, Any]]:
        principal.require_admin()
        return [
            item.to_dict()
            for item in cp.list_task_break_glass_authorizations(task_id=task_id)
        ]

    @app.post("/tasks/{task_id}/break-glass-authorizations")
    def authorize_break_glass(
        task_id: str,
        body: BreakGlassAuthorizeRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        actor = principal.client_id or principal.agent_id or "admin"
        return cp.authorize_task_break_glass(
            task_id,
            body.agent_id,
            reason=body.reason,
            authorized_by=actor,
            ttl_seconds=body.ttl_seconds,
        ).to_dict()

    @app.post("/break-glass-authorizations/{authorization_id}/revoke")
    def revoke_break_glass(
        authorization_id: str,
        body: BreakGlassRevokeRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        actor = principal.client_id or principal.agent_id or "admin"
        return cp.revoke_task_break_glass(
            authorization_id,
            revoked_by=actor,
            reason=body.reason,
        ).to_dict()

    @app.put("/tasks/{task_id}")
    def update_task(
        task_id: str,
        body: TaskUpdate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        data = _data(body)
        actor = data.pop("actor", "human")
        if data.get("metadata") is not None:
            _ensure_payload_bounded(data["metadata"], "task.metadata")
        return cp.update_task(task_id, actor=actor, **data).to_dict()

    @app.post("/tasks/{task_id}/review-experiment")
    def assign_review_experiment(
        task_id: str, body: ReviewExperimentAssign
    ) -> Dict[str, Any]:
        data = _data(body)
        actor = data.pop("actor", "human")
        return cp.assign_review_experiment(task_id, actor=actor, **data)

    @app.get("/tasks/{task_id}/review-observation")
    def review_observation(task_id: str) -> Dict[str, Any]:
        return cp.review_observation(task_id)

    @app.post("/tasks/{task_id}/review-outcomes")
    def record_review_outcome(
        task_id: str, body: ReviewOutcomeCreate
    ) -> Dict[str, Any]:
        data = _data(body)
        actor = data.pop("actor", "human")
        _ensure_payload_bounded(data.get("detail") or {}, "review_outcome.detail")
        return cp.record_review_outcome(task_id, actor=actor, **data)

    @app.get("/review-experiments/{experiment_id}")
    def review_experiment_report(
        experiment_id: str,
        project: Optional[str] = Query(default=None),
        min_tasks_per_arm: int = Query(default=5, ge=1, le=10000),
        min_validated_outcomes_per_arm: int = Query(default=3, ge=0, le=10000),
    ) -> Dict[str, Any]:
        return cp.review_experiment_report(
            experiment_id,
            project=project,
            min_tasks_per_arm=min_tasks_per_arm,
            min_validated_outcomes_per_arm=min_validated_outcomes_per_arm,
        )

    @app.get("/optimizer/status")
    def scientific_optimizer_status() -> Dict[str, Any]:
        return scientific_optimizer.status()

    @app.post("/optimizer/tick")
    def scientific_optimizer_tick() -> Dict[str, Any]:
        return scientific_optimizer.tick(trigger="operator")

    @app.post("/optimizer/policies")
    def create_scientific_policy(body: ScientificPolicyCreate) -> Dict[str, Any]:
        _ensure_payload_bounded(body.parameters, "scientific_policy.parameters")
        return scientific_optimizer.create_policy(**_data(body))

    @app.get("/optimizer/policies")
    def list_scientific_policies(
        project: Optional[str] = Query(default=None),
        status: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return scientific_optimizer.list_policies(project=project, status=status)

    @app.get("/optimizer/policies/{policy_id}")
    def get_scientific_policy(policy_id: str) -> Dict[str, Any]:
        return scientific_optimizer.get_policy(policy_id)

    @app.post("/optimizer/policies/{policy_id}/promote")
    def promote_scientific_policy(
        policy_id: str, body: ScientificPolicyAction
    ) -> Dict[str, Any]:
        return scientific_optimizer.promote_policy(
            policy_id, actor=body.actor, reason=body.reason
        )

    @app.post("/optimizer/projects/{project}/rollback/{policy_id}")
    def rollback_scientific_policy(
        project: str, policy_id: str, body: ScientificPolicyAction
    ) -> Dict[str, Any]:
        return scientific_optimizer.rollback_policy(
            project, policy_id, actor=body.actor, reason=body.reason
        )

    @app.post("/optimizer/experiments")
    def create_scientific_experiment(
        body: ScientificExperimentCreate,
    ) -> Dict[str, Any]:
        _ensure_payload_bounded(body.guardrails, "scientific_experiment.guardrails")
        _ensure_payload_bounded(body.metadata, "scientific_experiment.metadata")
        return scientific_optimizer.create_experiment(**_data(body))

    @app.get("/optimizer/experiments")
    def list_scientific_experiments(
        project: Optional[str] = Query(default=None),
        state: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return scientific_optimizer.list_experiments(project=project, state=state)

    @app.get("/optimizer/experiments/{experiment_id}")
    def get_scientific_experiment(experiment_id: str) -> Dict[str, Any]:
        return scientific_optimizer.get_experiment(experiment_id)

    @app.get("/optimizer/experiments/{experiment_id}/evidence")
    def get_scientific_experiment_evidence(
        experiment_id: str,
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> Dict[str, Any]:
        return scientific_optimizer.experiment_evidence(experiment_id, limit=limit)

    @app.post("/optimizer/experiments/{experiment_id}/start")
    def start_scientific_experiment(
        experiment_id: str, body: ScientificExperimentAction
    ) -> Dict[str, Any]:
        return scientific_optimizer.start_experiment(experiment_id, actor=body.actor)

    @app.post("/optimizer/experiments/{experiment_id}/pause")
    def pause_scientific_experiment(
        experiment_id: str, body: ScientificExperimentAction
    ) -> Dict[str, Any]:
        return scientific_optimizer.pause_experiment(
            experiment_id, actor=body.actor, reason=body.reason
        )

    @app.post("/optimizer/experiments/{experiment_id}/promote")
    def promote_scientific_experiment(
        experiment_id: str, body: ScientificExperimentAction
    ) -> Dict[str, Any]:
        return scientific_optimizer.promote_experiment(
            experiment_id,
            actor=body.actor,
            reason=body.reason,
        )

    @app.post("/optimizer/experiments/{experiment_id}/observe/{task_id}")
    def observe_scientific_task(experiment_id: str, task_id: str) -> Dict[str, Any]:
        return scientific_optimizer.observe_task(experiment_id, task_id)

    @app.post("/optimizer/experiments/{experiment_id}/analyze")
    def analyze_scientific_experiment(experiment_id: str) -> Dict[str, Any]:
        scientific_optimizer.refresh_experiment(experiment_id)
        return scientific_optimizer.analyze_experiment(experiment_id, actor="operator")

    @app.post("/tasks/{task_id}/children")
    def add_child_tasks(
        task_id: str,
        body: TaskChildrenCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        data = _data(body)
        actor = data.get("actor", "human")
        _assert_task_actor(principal, task_id, actor)
        for index, child in enumerate(data.get("children") or [], start=1):
            if child.get("metadata") is not None:
                _ensure_payload_bounded(child["metadata"], "task.children.%d.metadata" % index)
        return cp.add_child_tasks(
            task_id,
            data.get("children") or [],
            actor=actor,
            lease_id=data.get("lease_id"),
            trusted_internal=principal.is_admin,
        )

    @app.delete("/tasks/{task_id}")
    def delete_task(
        task_id: str,
        force: bool = Query(default=False),
        actor: str = Query(default="human"),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
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

    @app.post("/fleets/{fleet_id_or_name}/observed-agents")
    def observe_fleet_agent(
        fleet_id_or_name: str,
        body: FleetAgentObserve,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        _ensure_payload_bounded(body.metadata, "fleet_observation.metadata")
        return cp.observe_fleet_agent(fleet_id_or_name, **_data(body)).to_dict()

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

    @app.get("/work-packages")
    def list_work_packages(
        state: Optional[str] = Query(default=None),
        project: Optional[str] = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> List[Dict[str, Any]]:
        return cp.list_work_packages(state=state, project=project, limit=limit)

    @app.get("/work-package-telemetry")
    def export_work_package_telemetry(
        package_id: Optional[str] = Query(default=None),
        treatment_route: Optional[str] = Query(default=None),
        eligibility: Optional[str] = Query(default=None),
        station: Optional[str] = Query(default=None),
        since: Optional[str] = Query(default=None),
        limit: int = Query(default=1000, ge=1, le=10000),
    ) -> Dict[str, Any]:
        return cp.work_package_telemetry_export(
            package_id=package_id,
            treatment_route=treatment_route,
            eligibility=eligibility,
            station=station,
            since=since,
            limit=limit,
        )

    @app.get("/work-package-telemetry/comparable-atomic-outcomes")
    def comparable_atomic_execution_outcomes(
        treatment_route: Optional[str] = Query(default=None),
        since: Optional[str] = Query(default=None),
        limit: int = Query(default=1000, ge=1, le=10000),
    ) -> List[Dict[str, Any]]:
        return cp.comparable_atomic_execution_outcomes(
            treatment_route=treatment_route,
            since=since,
            limit=limit,
        )

    @app.post("/work-packages")
    def admit_work_package(
        body: WorkPackageAdmit,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        _ensure_payload_bounded(body.plan, "work_package.plan")
        return cp.admit_work_package(
            body.plan,
            actor=body.actor,
            reason=body.reason,
            tenant_id=body.tenant_id,
            root_task_id=body.root_task_id,
        ).to_dict()

    @app.get("/work-packages/{package_id}")
    def describe_work_package(package_id: str) -> Dict[str, Any]:
        return cp.describe_work_package(package_id)

    @app.get("/work-packages/{package_id}/telemetry")
    def describe_work_package_telemetry(package_id: str) -> Dict[str, Any]:
        # Validate the package identity first so an empty export is not
        # ambiguous with a typo.
        cp.describe_work_package(package_id)
        return cp.work_package_telemetry_export(package_id=package_id)

    @app.get("/work-packages/{package_id}/activation-readiness")
    def work_package_activation_readiness(package_id: str) -> Dict[str, Any]:
        return cp.work_package_activation_readiness(package_id)

    @app.post("/work-packages/{package_id}/activate")
    def activate_work_package(
        package_id: str,
        body: WorkPackageActivate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.activate_work_package(
            package_id,
            expected_plan_version=body.expected_plan_version,
            expected_epoch=body.expected_epoch,
            actor=body.actor,
        )

    @app.post("/work-packages/{package_id}/replan-preview")
    def preview_work_package_replan(
        package_id: str,
        body: WorkPackageReplan,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        _ensure_payload_bounded(body.plan, "work_package.replan.plan")
        return cp.preview_work_package_replan(
            package_id,
            body.plan,
            expected_plan_version=body.expected_plan_version,
            expected_epoch=body.expected_epoch,
            actor=body.actor,
            reason=body.reason,
        )

    @app.post("/work-packages/{package_id}/pause")
    def pause_work_package(
        package_id: str,
        body: WorkPackagePause,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.pause_work_package(
            package_id,
            expected_plan_version=body.expected_plan_version,
            expected_epoch=body.expected_epoch,
            actor=body.actor,
            reason=body.reason,
        )

    @app.post("/work-packages/{package_id}/replan")
    def replan_work_package(
        package_id: str,
        body: WorkPackageReplan,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        _ensure_payload_bounded(body.plan, "work_package.replan.plan")
        return cp.replan_work_package(
            package_id,
            body.plan,
            expected_plan_version=body.expected_plan_version,
            expected_epoch=body.expected_epoch,
            actor=body.actor,
            reason=body.reason,
        )

    @app.post("/work-packages/{package_id}/integration-batches")
    def create_work_package_integration_batch(
        package_id: str,
        body: WorkPackageIntegrationRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.create_work_package_integration_batch(
            package_id,
            body.integration_node_key,
            actor=body.actor,
        ).to_dict()

    @app.post("/work-packages/{package_id}/assemble")
    def assemble_work_package(
        package_id: str,
        body: WorkPackageIntegrationRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.assemble_work_package(
            package_id,
            body.integration_node_key,
            actor=body.actor,
        )

    @app.get("/work-package-integration-batches/{batch_id}")
    def work_package_integration_status(
        batch_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.work_package_integration_status(batch_id)

    @app.post("/work-package-integration-batches/{batch_id}/claim")
    def claim_work_package_integration_batch(
        batch_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.claim_work_package_integration_batch(batch_id).to_dict()

    @app.post("/work-package-integration-batches/{batch_id}/assemble")
    def assemble_work_package_integration_batch(
        batch_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.assemble_work_package_integration_batch(batch_id).to_dict()

    @app.post("/work-package-integration-batches/{batch_id}/certification-jobs")
    def prepare_work_package_certification_job(
        batch_id: str,
        body: WorkPackageCertificationPrepare,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.prepare_work_package_certification_job(
            batch_id,
            body.bundle_path,
            actor=body.actor,
        )

    @app.get("/work-package-certification-jobs/{job_id}")
    def work_package_certification_status(
        job_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.work_package_certification_status(job_id)

    @app.post("/work-package-certification-jobs/{job_id}/claim")
    def claim_work_package_certification_job(
        job_id: str,
        body: WorkPackageCertificationClaim,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.claim_work_package_certification_job(
            job_id,
            owner=body.owner,
        ).to_dict()

    @app.post("/work-package-certification-jobs/{job_id}/ingest")
    def ingest_work_package_certification_result(
        job_id: str,
        body: WorkPackageCertificationIngest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        _ensure_payload_bounded(body.result, "work_package.certification.result")
        return cp.ingest_work_package_certification_result(
            job_id,
            body.result,
            owner=body.owner,
            fence=body.fence,
        ).to_dict()

    @app.post("/work-package-certification-jobs/{job_id}/run")
    def run_work_package_certification_job(
        job_id: str,
        body: WorkPackageCertificationRun,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.run_work_package_certification_job(
            job_id,
            body.bundle_path,
            owner=body.owner,
            result_path=body.result_path,
        ).to_dict()

    @app.post(
        "/work-package-integration-batches/{batch_id}/reject-failed-certification"
    )
    def reject_failed_work_package_certification(
        batch_id: str,
        body: WorkPackageFailedCertification,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.reject_failed_work_package_certification(
            batch_id,
            body.certification_id,
            actor=body.actor,
        )

    @app.post(
        "/work-package-integration-batches/{batch_id}/accept-certification"
    )
    def accept_work_package_certification(
        batch_id: str,
        body: WorkPackageCertificationAccept,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.accept_work_package_certification(
            batch_id,
            body.certification_id,
        ).to_dict()

    @app.post("/work-package-integration-batches/{batch_id}/land")
    def land_work_package(
        batch_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.land_work_package(batch_id).to_dict()

    @app.post(
        "/work-package-integration-batches/{batch_id}/finalize-publication"
    )
    def finalize_work_package_publication(
        batch_id: str,
        body: WorkPackagePublicationFinalize,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.finalize_work_package_publication(
            batch_id,
            actor=body.actor,
            receipt_id=body.receipt_id,
        ).to_dict()

    @app.post("/work-package-finalizations/{finalization_id}/outcomes")
    def record_work_package_finalization_outcome(
        finalization_id: str,
        body: WorkPackageFinalizationOutcome,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        _ensure_payload_bounded(body.detail, "work_package.finalization_outcome.detail")
        return cp.record_work_package_finalization_outcome(
            finalization_id,
            outcome_type=body.outcome_type,
            external_id=body.external_id,
            observed_at=body.observed_at,
            actor=body.actor,
            detail=body.detail,
        )

    @app.post("/work-package-outputs/{evidence_id}/verify")
    def verify_work_package_output(
        evidence_id: str,
        _body: WorkPackageOutputVerify,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.verify_work_package_output(evidence_id).to_dict()

    @app.post("/work-packages/candidates/{candidate_id}/accept")
    def accept_work_package_candidate(
        candidate_id: str,
        body: WorkPackageCandidateAccept,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.accept_work_package_candidate(
            candidate_id,
            actor=body.actor,
        ).to_dict()

    @app.post("/work-packages/candidates/{candidate_id}/reject")
    def reject_work_package_candidate(
        candidate_id: str,
        body: WorkPackageCandidateReject,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.reject_work_package_candidate(
            candidate_id,
            actor=body.actor,
            reason=body.reason,
        ).to_dict()

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

    @app.post("/projects/register")
    def register_project(
        body: ProjectRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Register ``GIT_URL[#BRANCH]`` and create its contract task.

        The canonical registration always includes a branch; ``main`` is used
        when the URL has no fragment.
        """
        data = _data(body)
        actor = data.pop("actor", "human")
        return cp.register_project(actor=actor, **data).to_dict()

    @app.post("/projects/{project}/dispatch")
    def set_project_dispatch(project: str, body: ProjectDispatch) -> Dict[str, Any]:
        return cp.set_project_dispatch(
            project, paused=body.paused, actor=body.actor
        ).to_dict()

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
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        terminal_close = str(body.target_state).strip().lower() in {
            "completed",
            "cancelled",
        }
        operator_close = terminal_close and principal.agent_id is None
        if operator_close:
            # An unbound writer is not worker authority and must not gain an
            # implicit lifecycle bypass by choosing an actor string.  Only the
            # explicit admin/operator principal reaches the trusted close
            # service.  Package tasks still fail closed inside that service.
            principal.require_admin()
            task = cp.close_task(
                task_id,
                body.target_state,
                body.actor,
                body.detail,
                drain_outbox=False,
            )
        else:
            _assert_task_actor(principal, task_id, body.actor)
            task = cp.transition_task(
                task_id,
                body.target_state,
                body.actor,
                body.detail,
                lease_id=body.lease_id,
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
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _assert_task_actor(principal, task_id, agent_id)
        task, lease = cp.claim_task(task_id, agent_id, lease_seconds, sync_beads=False)
        return {"task": task.to_dict(), "lease": lease.to_dict()}

    @app.post("/tasks/{task_id}/reopen")
    def reopen_task(
        task_id: str,
        body: TaskRecoveryRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        # Recovery: return a stuck/terminal task (failed/cancelled/blocked) to
        # OPEN so it can be retried or reconciled. Counterpart to force-complete.
        return cp.reopen_task(task_id, body.actor, body.reason).to_dict()

    @app.post("/tasks/{task_id}/ask")
    def ask_task(
        task_id: str,
        body: TaskAskRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        # Park the task on an unanswered human question. Not a failure: the
        # work is still wanted, so no sweeper or reaper may collect it and its
        # attempt budget is left untouched until someone answers.
        return cp.request_task_input(
            task_id, body.questions, body.actor, why=body.why or ""
        ).to_dict()

    @app.post("/tasks/{task_id}/answer")
    def answer_task(
        task_id: str,
        body: TaskAnswerRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        # Answering returns held work to the dispatch pool, so it carries the
        # same authority as reopen/release.
        return cp.answer_task_input(
            task_id,
            body.answer,
            body.actor,
            disposition=getattr(body, "disposition", None) or "resume",
            replaced_by=getattr(body, "replaced_by", None),
        ).to_dict()

    @app.post("/tasks/{task_id}/force-complete")
    def force_complete_task(
        task_id: str,
        body: TaskRecoveryRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        # Operator override: mark a task COMPLETED regardless of state/review,
        # for reconciling work done out-of-band (e.g. merged via PR) or a task
        # stranded in a terminal state. Bypasses the review gate (audited).
        return cp.force_complete_task(task_id, body.actor, body.reason).to_dict()

    @app.post("/leases/{lease_id}/renew")
    def renew_lease(
        lease_id: str,
        body: LeaseRenewRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _assert_task_actor(
            principal, cp.get_lease(lease_id).task_id, body.agent_id
        )
        return cp.renew_lease(lease_id, body.agent_id, body.lease_seconds).to_dict()

    @app.post("/leases/{lease_id}/delegate")
    def delegate_lease(
        lease_id: str,
        body: LeaseDelegateRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # PR2c (spec §6.3, Option B): the dispatcher holds the lease but
        # the role agent spawned in the task Job authors start /
        # submit_for_review / evidence. This endpoint records the
        # delegation so those calls accept the delegate as a valid
        # actor. Owner remains the sole renew/release authority.
        _assert_task_actor(
            principal, cp.get_lease(lease_id).task_id, body.agent_id
        )
        return cp.delegate_lease(lease_id, body.agent_id, body.to_agent_id).to_dict()

    @app.post("/tasks/{task_id}/start")
    def start_task(
        task_id: str,
        agent_id: str,
        background_tasks: BackgroundTasks,
        lease_id: Optional[str] = Query(default=None),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _assert_task_actor(principal, task_id, agent_id)
        task = cp.start_task(
            task_id,
            agent_id,
            lease_id=lease_id,
            drain_outbox=False,
        )
        background_tasks.add_task(
            cp.drain_task_transition_outbox_best_effort,
            task_id=task_id,
            limit=20,
        )
        return task.to_dict()

    @app.post("/tasks/{task_id}/release")
    def release_task(
        task_id: str,
        body: TaskRelease = TaskRelease(),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.release_task(task_id, actor=body.actor).to_dict()

    @app.post("/tasks/{task_id}/activity")
    def append_task_activity(
        task_id: str,
        body: TaskActivityAppend,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _assert_task_actor(principal, task_id, body.actor)
        return cp.append_task_activity(
            task_id,
            phase=body.phase,
            actor=body.actor,
            summary=body.summary,
            lease_id=body.lease_id,
            trusted_internal=principal.is_admin,
        ).to_dict()

    @app.post("/tasks/{task_id}/submit-for-review")
    def submit_for_review(
        task_id: str,
        agent_id: str,
        background_tasks: BackgroundTasks,
        lease_id: Optional[str] = Query(default=None),
        advance_default_workflow: bool = Query(default=False),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _assert_task_actor(principal, task_id, agent_id)
        task = cp.submit_for_review(
            task_id,
            agent_id,
            lease_id=lease_id,
            drain_outbox=False,
        )
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
        _assert_task_actor(principal, task_id, body.created_by)
        # Admin operators use this same endpoint to reconcile durable evidence
        # for work completed out of band (for example, a commit pushed directly
        # to the canonical branch).  The force-complete endpoint intentionally
        # still requires canonical integration evidence, so an administrator
        # must be able to record that proof without manufacturing a worker
        # lease.  Ordinary writers and worker principals remain lease/review
        # fenced inside ControlPlane.add_evidence.
        evidence = cp.add_evidence(
            task_id=task_id,
            sync_beads=False,
            _trusted_internal=principal.is_admin,
            **_data(body),
        )
        return evidence.to_dict()

    @app.get("/evidence/{evidence_id}/artifacts")
    def list_evidence_artifacts(evidence_id: str) -> List[Dict[str, Any]]:
        return cp.list_evidence_artifacts(evidence_id)

    @app.get("/evidence/{evidence_id}/artifacts/{artifact_id}")
    def get_evidence_artifact(evidence_id: str, artifact_id: str) -> Dict[str, Any]:
        return cp.get_evidence_artifact(evidence_id, artifact_id)

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

    @app.get("/machines/{machine_id}")
    def get_machine(machine_id: str) -> Dict[str, Any]:
        return cp.get_machine(machine_id).to_dict()

    @app.post("/agents")
    def register_agent(
        body: AgentRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        _ensure_payload_bounded(body.resources, "agent.resources")
        data = _data(body)
        requested_agent_id = str(data.get("agent_id") or "")
        if not principal.is_admin:
            principal.assert_actor(requested_agent_id)
            if data.get("instance_kind") is not None:
                # Lifecycle classification is operator policy. A compromised
                # static worker must not be able to relabel itself fungible
                # and thereby opt into replacement/re-attestation behavior.
                principal.require_admin()
        resources = dict(data.get("resources") or {})
        resources.pop("worker_credential_authenticated", None)
        if (
            principal.principal_kind == "worker"
            and principal.agent_id
            and principal.agent_id == requested_agent_id
        ):
            from mac.worker_credentials import authenticated_credential_resource

            authenticated = authenticated_credential_resource(
                agent_id=requested_agent_id,
                principal_id=principal.client_id,
                token_fingerprint=principal.credential_fingerprint,
                credential_version=principal.worker_credential_version,
            )
            if authenticated:
                resources["worker_credential_authenticated"] = authenticated
        data["resources"] = resources
        fleet_id = data.pop("fleet_id", None)
        actor = str(data.get("actor") or "human")
        agent = cp.register_agent(
            **data,
            allow_resurrection=principal.is_admin,
        )
        if fleet_id:
            cp.observe_fleet_agent(
                str(fleet_id),
                agent.id,
                source="agent-registration",
                metadata={"registration_path": "/agents"},
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
        # Rotation discloses the new cleartext signing key and immediately
        # changes which worker-authored evidence the controller will accept.
        # Agent-bound credentials therefore must never be able to rotate their
        # own key (or a peer's); recovery/bootstrap is an operator action.
        principal.require_admin()
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
        if not principal.is_admin:
            # Verification is deliberately available to a worker as a
            # challenge-response health check, but only for its own key. It
            # returns no secret and remains necessary for evidence signing.
            principal.assert_actor(agent_id)
        _ensure_payload_bounded(body.challenge, "agent.attestation.challenge")
        return {
            "agent_id": agent_id,
            "valid": cp.verify_agent_attestation_challenge(
                agent_id,
                body.challenge,
                body.signature,
            ),
        }

    @app.post("/agents/{agent_id}/attestation-key/recover")
    def recover_agent_attestation_key(
        agent_id: str,
        body: AgentAttestationKeyRecover,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, str]:
        # The response contains the new cleartext key. Only the fenced deploy
        # controller may request it; a bound worker can submit a secret-free
        # verify probe but can never rotate or retrieve key material.
        principal.require_admin()
        _ensure_payload_bounded(body.probe, "agent.attestation.recovery_probe")
        return {
            "agent_id": agent_id,
            "attestation_key": cp.recover_agent_attestation_key(
                agent_id, body.probe
            ),
        }

    @app.post("/agents/{agent_id}/report-repository-executor/approve")
    def approve_agent_report_repository_executor(
        agent_id: str,
        body: AgentReportRepositoryExecutorApprove,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        _ensure_payload_bounded(
            body.expected_attestation,
            "agent.report_repository_executor.expected_attestation",
        )
        return cp.approve_agent_report_repository_executor(
            agent_id,
            body.expected_attestation,
            body.expected_startup_timestamp,
            actor=body.actor,
        ).to_dict()

    @app.post("/agents/{agent_id}/report-repository-executor/revoke")
    def revoke_agent_report_repository_executor(
        agent_id: str,
        body: AgentReportRepositoryExecutorRevoke,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.revoke_agent_report_repository_executor(
            agent_id,
            body.reason,
            actor=body.actor,
        ).to_dict()

    # Hub-mediated curiosity quarantine access (task_3a4503f0).
    #
    # The ledger lives inside the mac-openclaw-<agent> sandbox, and dispatched
    # tasks run in a different mac-task-* sandbox that cannot reach it -- so
    # every adjudication task ever filed against the quarantine was
    # unsatisfiable, no matter which host it was pinned to. The hub runs ON the
    # agent host and can invoke the wrapper, and every task sandbox can already
    # reach the hub, so mediating here is what makes the loop closable.
    @app.get("/curiosity/candidates")
    def list_curiosity_candidates(
        status: Optional[str] = None,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # ControlPlane already raises domain errors (ValidationError for a bad
        # request or a failing wrapper, NotFoundError for a host with no
        # OpenClaw ledger at all), which the app's handlers map to status codes.
        return cp.list_curiosity_candidates(status)

    @app.post("/curiosity/candidates/{candidate_id}/{decision}")
    def decide_curiosity_candidate(
        candidate_id: str,
        decision: str,
        body: CuriosityDecision,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        return cp.decide_curiosity_candidate(
            candidate_id,
            decision,
            actor=body.actor,
            reason=body.reason,
            approval_id=body.approval_id,
        )

    @app.get("/agents")
    def list_agents() -> List[Dict[str, Any]]:
        return [agent.to_dict() for agent in cp.list_agents()]

    @app.post("/agents/bulk")
    def bulk_update_agents(
        body: AgentBulkUpdate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
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
        principal.require_admin()
        data = _data(body)
        if data.get("resources") is not None:
            _ensure_payload_bounded(data["resources"], "agent.resources")
        return cp.update_agent(agent_id, **data).to_dict()

    @app.post("/agents/{agent_id}/disable")
    def disable_agent(
        agent_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.disable_agent(agent_id).to_dict()

    @app.delete("/agents/{agent_id}")
    def delete_agent(
        agent_id: str,
        actor: str = Query(default="human"),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
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

    @app.post("/agents/dispatch-hold/release-batch")
    def release_dispatch_holds_batch(
        body: DispatchHoldBatchReleaseRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Commit an exact fleet release epoch as one database transaction."""

        principal.require_admin()
        agents = cp.release_agent_dispatch_holds_batch(
            ((item.agent_id, item.reason) for item in body.holds),
            epoch_id=body.epoch_id,
            expectations={
                item.agent_id: {
                    "generation": item.generation,
                    "baseline_seen": item.baseline_seen,
                    "principal_id": item.principal_id,
                    "require_authenticated": item.require_authenticated,
                    "require_report_executor": item.require_report_executor,
                }
                for item in body.holds
            },
        )
        return {
            "released": True,
            "epoch_id": body.epoch_id,
            "agents": [agent.to_dict() for agent in agents],
        }

    @app.get("/agents/dispatch-hold/authority")
    def dispatch_hold_authority(
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Return the durable hub identity used to bind a fleet transaction."""

        principal.require_global_fleet()
        principal.require_admin()
        return {
            "schema": "mac.fleet_release_hub_authority.v1",
            "hub_authority_id": cp.fleet_release_epochs.hub_authority_id,
        }

    @app.get("/agents/dispatch-hold/epochs/{epoch_id}")
    def dispatch_hold_epoch_status(
        epoch_id: str,
        identity_sha256: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Read one durable fleet-release epoch without replaying it."""

        principal.require_global_fleet()
        principal.require_admin()
        if identity_sha256.startswith("sha256:"):
            return cp.fleet_release_epochs.status(epoch_id, identity_sha256)
        return cp.agent_dispatch_hold_epoch_status(epoch_id, identity_sha256)

    @app.get("/agents/dispatch-hold/epochs/{epoch_id}/readiness")
    def dispatch_hold_epoch_pre_prove_readiness(
        epoch_id: str,
        identity_sha256: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Fail closed unless the exact pending cohort is ready to prove."""

        principal.require_global_fleet()
        principal.require_admin()
        return cp.fleet_release_epochs.pre_prove_readiness(
            epoch_id, identity_sha256
        )

    @app.post("/agents/dispatch-hold/epochs/open")
    def open_fleet_release_epoch(
        body: FleetReleaseEpochOpenRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Atomically reserve one exact cohort without promoting authority."""

        principal.require_global_fleet()
        principal.require_admin()
        participants: List[Dict[str, Any]] = []
        for item in body.participants:
            value = item.model_dump(exclude={"attestation_candidate"})
            if item.attestation_candidate is not None:
                value["attestation_candidate"] = {
                    "key": item.attestation_candidate.key.get_secret_value(),
                }
            participants.append(value)
        return cp.fleet_release_epochs.open_epoch(
            body.epoch_id,
            participants,
            successor_hold_reason=body.successor_hold_reason,
            desired_policy_mode=body.desired_worker_credential_mode,
            actor=principal.client_id or principal.agent_id or "admin",
        )

    @app.post("/agents/dispatch-hold/epochs/{epoch_id}/prove")
    def prove_fleet_release_epoch(
        epoch_id: str,
        body: FleetReleaseEpochProveRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Verify exact post-apply evidence without promoting authority."""

        principal.require_global_fleet()
        principal.require_admin()
        proofs: List[Dict[str, Any]] = []
        for item in body.proofs:
            value = item.model_dump(exclude={"attestation_proof"})
            value["attestation_proof"] = (
                item.attestation_proof.model_dump()
                if item.attestation_proof is not None
                else None
            )
            proofs.append(value)
        return cp.fleet_release_epochs.prove(
            epoch_id,
            body.identity_sha256,
            proofs,
            actor=principal.client_id or principal.agent_id or "admin",
        )

    @app.post("/agents/dispatch-hold/epochs/{epoch_id}/commit")
    def commit_fleet_release_epoch(
        epoch_id: str,
        body: FleetReleaseEpochCommitRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Promote identity, approval, policy, and holds in one transaction."""

        principal.require_global_fleet()
        principal.require_admin()
        return cp.fleet_release_epochs.commit(
            epoch_id,
            body.identity_sha256,
            actor=principal.client_id or principal.agent_id or "admin",
        )

    @app.post("/agents/dispatch-hold/epochs/{epoch_id}/abort")
    def abort_fleet_release_epoch(
        epoch_id: str,
        body: FleetReleaseEpochAbortRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Discard epoch staging and restore each exact pre-open authority."""

        principal.require_global_fleet()
        principal.require_admin()
        return cp.fleet_release_epochs.abort(
            epoch_id,
            body.identity_sha256,
            reason=body.reason,
            disposition=body.disposition,
            actor=principal.client_id or principal.agent_id or "admin",
        )

    @app.post("/agents/dispatch-hold/transition-batch")
    def transition_dispatch_holds_batch(
        body: DispatchHoldBatchTransitionRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Atomically hand an exact fleet hold epoch to one successor hold."""

        principal.require_admin()
        agents = cp.release_agent_dispatch_holds_batch(
            ((item.agent_id, item.reason) for item in body.holds),
            epoch_id=body.epoch_id,
            expectations={
                item.agent_id: {
                    "generation": item.generation,
                    "baseline_seen": item.baseline_seen,
                    "principal_id": item.principal_id,
                    "require_authenticated": item.require_authenticated,
                    "require_report_executor": item.require_report_executor,
                }
                for item in body.holds
            },
            successor_reason=body.successor_reason,
        )
        return {
            "transitioned": True,
            "epoch_id": body.epoch_id,
            "successor_reason": body.successor_reason.strip(),
            "agents": [agent.to_dict() for agent in agents],
        }

    @app.post("/agents/{agent_id}/dispatch-hold")
    def set_dispatch_hold(
        agent_id: str,
        body: DispatchHoldRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.set_agent_dispatch_hold(agent_id, body.reason).to_dict()

    @app.delete("/agents/{agent_id}/dispatch-hold")
    def clear_dispatch_hold(
        agent_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.clear_agent_dispatch_hold(agent_id).to_dict()

    @app.post("/agents/{agent_id}/dispatch-hold/acquire")
    def acquire_dispatch_hold(
        agent_id: str,
        body: DispatchHoldAcquireRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Acquire or replace a hold only if the caller's snapshot is current."""

        principal.require_admin()
        changed, agent = cp.acquire_agent_dispatch_hold(
            agent_id,
            body.reason,
            expected_dispatch_hold=body.expected_dispatch_hold,
            expected_reason=body.expected_reason,
        )
        return {"changed": changed, "agent": agent.to_dict()}

    @app.post("/agents/{agent_id}/dispatch-hold/release")
    def release_dispatch_hold(
        agent_id: str,
        body: DispatchHoldRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Release a hold only while its exact caller-owned reason remains."""

        principal.require_admin()
        released, agent = cp.release_agent_dispatch_hold(agent_id, body.reason)
        return {"released": released, "agent": agent.to_dict()}

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

    @app.get("/fleet/snapshot")
    def fleet_snapshot(
        exclude_agent_id: Optional[str] = Query(default=None),
        limit: int = Query(default=30, ge=1, le=200),
        capability: Optional[str] = Query(default=None, max_length=100),
    ) -> Dict[str, Any]:
        return cp.fleet_snapshot(
            exclude_agent_id=exclude_agent_id, limit=limit, capability=capability
        )

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

    @app.get("/nap-due")
    def list_due_nap_agents(as_of: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return cp.list_due_nap_agents(as_of=as_of)

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

    @app.post("/agents/{agent_id}/nap-cycle")
    def run_nap_cycle(agent_id: str, body: NapCycle) -> Dict[str, Any]:
        vector_writer = _vector_writer_for_memory(
            cp,
            enabled=body.embed_into_medium,
            qdrant_url=body.qdrant_url,
        )
        return cp.run_nap_cycle(
            agent_id,
            actor=body.actor,
            vector_writer=vector_writer,
            embed_into_medium=body.embed_into_medium,
            emit_dream_artifacts=body.emit_dream_artifacts,
        )

    @app.post("/dream/import-logs")
    def import_dream_logs(body: DreamImportLogs) -> Dict[str, Any]:
        vector_writer = _vector_writer_for_memory(
            cp,
            enabled=body.embed and not body.dry_run,
            qdrant_url=body.qdrant_url,
        )
        return cp.import_dream_logs(
            dream_logs_dir=body.dream_logs_dir,
            agent_id=body.agent_id,
            created_by=body.created_by,
            embed=body.embed,
            vector_writer=vector_writer,
            dry_run=body.dry_run,
        )

    @app.post("/agents/{agent_id}/nap-consolidate")
    def consolidate_nap(agent_id: str, body: NapConsolidate) -> Dict[str, Any]:
        vector_writer = _vector_writer_for_memory(
            cp,
            enabled=body.embed_into_medium,
            qdrant_url=body.qdrant_url,
        )
        return cp.consolidate_nap(
            agent_id,
            since=body.since,
            nap_run_id=body.nap_run_id,
            embed_into_medium=body.embed_into_medium,
            emit_dream_artifacts=body.emit_dream_artifacts,
            vector_writer=vector_writer,
            created_by=body.created_by,
        )

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
        data = _data(body)
        resources_value = data.get("resources")
        resources = (
            dict(resources_value)
            if isinstance(resources_value, Mapping)
            else dict(cp.get_agent(agent_id).resources)
        )
        if not isinstance(resources_value, Mapping):
            # Deployment generation is a per-heartbeat proof, not sticky hub
            # state.  A status-only request authenticated with a copied bearer
            # must not inherit the last worker's release generation merely
            # because the API clones resources to attach principal facts.
            resources.pop("deployment_generation", None)
        # This namespace is hub-owned. A legacy/shared token clears any stale
        # authentication proof; a DB-backed exact worker token replaces it
        # with facts derived from the resolved bearer principal.
        resources.pop("worker_credential_authenticated", None)
        if principal.principal_kind == "worker" and principal.agent_id == agent_id:
            from mac.worker_credentials import authenticated_credential_resource

            authenticated = authenticated_credential_resource(
                agent_id=agent_id,
                principal_id=principal.client_id,
                token_fingerprint=principal.credential_fingerprint,
                credential_version=principal.worker_credential_version,
            )
            if authenticated:
                resources["worker_credential_authenticated"] = authenticated
        if resources_value is not None or resources:
            data["resources"] = resources
        return cp.heartbeat_agent(agent_id, **data).to_dict()

    @app.post("/agents/{agent_id}/crash-reports")
    def report_agent_crash(
        agent_id: str,
        body: CrashReportCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_actor(agent_id)
        data = _data(body)
        _ensure_payload_bounded(data.get("metadata"), "crash.metadata")
        _ensure_payload_bounded(data.get("core_metadata"), "crash.core_metadata")
        _ensure_payload_bounded(
            data.get("resource_snapshot"), "crash.resource_snapshot"
        )
        return cp.crashes.ingest(agent_id, data)

    @app.get("/crash-reports")
    def list_crash_reports(
        status: Optional[str] = Query(default=None),
        agent_id: Optional[str] = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> List[Dict[str, Any]]:
        return cp.crashes.list_reports(status=status, agent_id=agent_id, limit=limit)

    @app.get("/crash-reports/{report_id}")
    def get_crash_report(report_id: str) -> Dict[str, Any]:
        return cp.crashes.get_report(report_id)

    @app.post("/crash-reports/{report_id}/resolve")
    def resolve_crash_report(
        report_id: str,
        body: CrashReportResolve,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.crashes.resolve(report_id, actor=body.actor, reason=body.reason)

    @app.post("/agents/{agent_id}/reflect")
    def reflect_agent(
        agent_id: str,
        body: AgentReflectRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_actor(agent_id)
        return cp.publish_agent_reflection(
            agent_id,
            recipient_agent_id=body.recipient_agent_id,
            request_id=body.request_id,
            reflect_timeout=body.reflect_timeout,
        )

    @app.post("/agents/{agent_id}/claim-next")
    def claim_next_for_agent(
        agent_id: str,
        body: AgentClaimNextRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Optional[Dict[str, Any]]:
        principal.assert_actor(agent_id)  # mac-wcfy
        assignment = cp.claim_next_for_agent(
            agent_id,
            lease_seconds=body.lease_seconds,
            dry_run=body.dry_run,
            sync_beads=False,
        )
        return assignment

    @app.post("/agents/{agent_id}/service-claims/sync")
    def sync_agent_service_claims(
        agent_id: str,
        body: ServiceClaimsSyncRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_actor(agent_id)  # an agent syncs only its OWN service claims
        return cp.sync_agent_service_claims(
            agent_id, body.willing_ops, lease_seconds=body.lease_seconds
        )

    @app.get("/service-roles")
    def list_service_roles() -> List[Dict[str, Any]]:
        return [r.to_dict() for r in cp.service_roles.desired_services()]

    @app.get("/service-claims")
    def list_service_claims(op: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        role_id = None
        if op:
            try:
                role_id = cp.service_roles.get_role_by_slug("media:%s" % op).id
            except Exception:  # noqa: BLE001
                return []
        return [c.to_dict() for c in cp.service_roles.list_active_claims(role_id=role_id)]

    @app.post("/agents/{agent_id}/command-audit")
    def record_agent_command_audit(
        agent_id: str,
        body: CommandAuditCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_actor(agent_id)  # mac-wcfy
        return cp.record_command_audit(agent_id=agent_id, **_data(body)).to_dict()

    # Fleet directives -------------------------------------------------
    # Static paths precede /directives/{directive_id} so FastAPI never
    # interprets "effective", "bindings", or "waivers" as directive ids.

    @app.post("/directives")
    def propose_directive(
        body: DirectivePropose,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.directives.propose(body.document, actor=body.actor)

    @app.get("/directives")
    def list_directives(
        state: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return cp.directives.list(state=state)

    @app.get("/directives/effective")
    def effective_directives(
        repository_id: Optional[str] = Query(default=None),
        project: Optional[str] = Query(default=None),
        agent_id: Optional[str] = Query(default=None),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        if agent_id is not None:
            principal.assert_actor(agent_id)
        return cp.directives.effective_snapshot(
            repository_id=repository_id,
            project=project,
            agent_id=agent_id,
        )

    @app.get("/agents/{agent_id}/directives/effective")
    def effective_directives_for_agent(
        agent_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_actor(agent_id)
        return cp.directives.effective_snapshot(agent_id=agent_id)

    @app.get("/directive-bindings")
    def list_directive_bindings(
        target_type: Optional[str] = Query(default=None),
        target_id: Optional[str] = Query(default=None),
        active: bool = Query(default=True),
    ) -> List[Dict[str, Any]]:
        return cp.directives.list_bindings(
            target_type=target_type,
            target_id=target_id,
            active=active,
        )

    @app.post("/directive-bindings")
    def set_directive_binding(
        body: DirectiveBindingSet,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.directives.set_binding(**_data(body))

    @app.get("/directive-waivers")
    def list_directive_waivers(
        directive_id: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return cp.directives.list_waivers(directive_id)

    @app.post("/directive-waivers/{waiver_id}/revoke")
    def revoke_directive_waiver(
        waiver_id: str,
        body: DirectiveWaiverRevoke,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.directives.revoke_waiver(waiver_id, **_data(body))

    @app.post("/agents/{agent_id}/directive-activations/{activation_id}/ack")
    def acknowledge_directive_activation(
        agent_id: str,
        activation_id: str,
        body: DirectiveAck,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_actor(agent_id)
        return cp.directives.acknowledge(
            activation_id,
            agent_id=agent_id,
            digest=body.digest,
        )

    @app.get("/directives/{directive_id}")
    def get_directive(directive_id: str) -> Dict[str, Any]:
        return cp.directives.get(directive_id)

    @app.get("/directives/{directive_id}/versions")
    def list_directive_versions(directive_id: str) -> List[Dict[str, Any]]:
        return cp.directives.versions(directive_id)

    @app.get("/directives/{directive_id}/impact")
    def directive_impact(directive_id: str) -> Dict[str, Any]:
        return cp.directives.impact(directive_id)

    @app.post("/directives/{directive_id}/check")
    def check_directive(
        directive_id: str,
        body: DirectiveCheck,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.directives.check(directive_id, **_data(body))

    @app.post("/directives/{directive_id}/approve")
    def approve_directive(
        directive_id: str,
        body: DirectiveApprove,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.directives.approve(directive_id, **_data(body))

    @app.post("/directives/{directive_id}/activate")
    def activate_directive(
        directive_id: str,
        body: DirectiveActivate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.directives.activate(directive_id, **_data(body))

    @app.post("/directives/{directive_id}/deactivate")
    def deactivate_directive(
        directive_id: str,
        body: DirectiveDeactivate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.directives.deactivate(directive_id, **_data(body))

    @app.post("/directives/{directive_id}/waivers")
    def create_directive_waiver(
        directive_id: str,
        body: DirectiveWaiverCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.directives.create_waiver(directive_id, **_data(body))

    @app.post("/openshell/policies")
    def create_openshell_policy(
        body: OpenShellPolicyCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.create_openshell_policy(**_data(body)).to_dict()

    @app.get("/openshell/policies")
    def list_openshell_policies(
        include_deleted: bool = Query(default=False),
    ) -> List[Dict[str, Any]]:
        return [
            policy.to_dict()
            for policy in cp.list_openshell_policies(include_deleted=include_deleted)
        ]

    @app.get("/openshell/policies/{policy_id}")
    def get_openshell_policy(
        policy_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """One policy INCLUDING its guardrail text (`mac openshell policy show`).

        Admin-gated: creating, updating, deleting and assigning a policy all
        require the global fleet principal, so serving the same policy's source
        to any read token was an asymmetry, not a decision. The list, status and
        dashboard views stay readable — they return identity and checksum, which
        is what drift detection needs.
        """
        principal.require_global_fleet()
        return cp.get_openshell_policy(policy_id, include_deleted=True).to_dict(
            include_text=True
        )

    @app.put("/openshell/policies/{policy_id}")
    def update_openshell_policy(
        policy_id: str,
        body: OpenShellPolicyUpdate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.update_openshell_policy(policy_id, **_data(body)).to_dict()

    @app.delete("/openshell/policies/{policy_id}")
    def delete_openshell_policy(
        policy_id: str,
        actor: str = Query(default="human"),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.delete_openshell_policy(policy_id, actor=actor).to_dict()

    @app.get("/openshell/policies/{policy_id}/versions")
    def list_openshell_policy_versions(
        policy_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> List[Dict[str, Any]]:
        """Version history WITH text, so an operator can diff guardrail changes.

        A history of guardrail sources is exactly as sensitive as the current
        one, so it carries the same admin gate.
        """
        principal.require_global_fleet()
        return [
            version.to_dict(include_text=True)
            for version in cp.list_openshell_policy_versions(policy_id)
        ]

    @app.post("/openshell/policies/{policy_id}/render")
    def render_openshell_policy(
        policy_id: str,
        body: OpenShellPolicyRender,
    ) -> Dict[str, Any]:
        return cp.render_openshell_policy(policy_id, **_data(body))

    @app.get("/openshell/policies/{policy_id}/assignments")
    def list_openshell_policy_assignments(policy_id: str) -> List[Dict[str, Any]]:
        return [
            assignment.to_dict()
            for assignment in cp.list_openshell_policy_assignments(policy_id=policy_id)
        ]

    @app.post("/openshell/policies/{policy_id}/assignments")
    def assign_openshell_policy(
        policy_id: str,
        body: OpenShellPolicyAssign,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.assign_openshell_policy(policy_id, **_data(body)).to_dict()

    @app.get("/agents/{agent_id}/openshell/status")
    def get_agent_openshell_status(agent_id: str) -> Dict[str, Any]:
        return cp.get_openshell_status(agent_id)

    @app.get("/agents/{agent_id}/openshell/policy")
    def get_agent_openshell_policy(
        agent_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """The guardrail policy text assigned to this agent, for self-install.

        Self-only: an agent may fetch the policy it must confine itself with and
        no other agent's. Without this route `mac openshell policy assign` only
        recorded intent — the executor resolved its policy from a file written
        at provision time, so a reassignment never reached a running worker
        until the host was re-bootstrapped.
        """
        principal.assert_actor(agent_id)
        return cp.assigned_openshell_policy(agent_id)

    @app.post("/agents/{agent_id}/openshell/status")
    def report_agent_openshell_status(
        agent_id: str,
        body: OpenShellStatusReport,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_actor(agent_id)
        return cp.report_openshell_status(agent_id, **_data(body)).to_dict()

    @app.post("/action-events")
    def record_action_event(
        body: ActionEventCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        if principal.agent_id is not None:
            if not body.agent_id:
                raise AuthorizationError("agent-scoped action events must include agent_id")
            principal.assert_actor(body.agent_id)
        return cp.record_action_event(**_data(body)).to_dict()

    @app.get("/action-events")
    def list_action_events(
        agent_id: Optional[str] = Query(default=None),
        task_id: Optional[str] = Query(default=None),
        session_id: Optional[str] = Query(default=None),
        sandbox_id: Optional[str] = Query(default=None),
        policy_id: Optional[str] = Query(default=None),
        action_type: Optional[str] = Query(default=None),
        outcome: Optional[str] = Query(default=None),
        since: Optional[str] = Query(default=None),
        until: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            event.to_dict()
            for event in cp.list_action_events(
                agent_id=agent_id,
                task_id=task_id,
                session_id=session_id,
                sandbox_id=sandbox_id,
                policy_id=policy_id,
                action_type=action_type,
                outcome=outcome,
                since=since,
                until=until,
                limit=limit,
            )
        ]

    @app.get("/action-events/export/otlp")
    def export_action_events_otlp(
        agent_id: Optional[str] = Query(default=None),
        task_id: Optional[str] = Query(default=None),
        session_id: Optional[str] = Query(default=None),
        sandbox_id: Optional[str] = Query(default=None),
        policy_id: Optional[str] = Query(default=None),
        action_type: Optional[str] = Query(default=None),
        outcome: Optional[str] = Query(default=None),
        since: Optional[str] = Query(default=None),
        until: Optional[str] = Query(default=None),
        limit: int = Query(default=1000),
    ) -> Dict[str, Any]:
        return cp.export_action_events_otlp(
            agent_id=agent_id,
            task_id=task_id,
            session_id=session_id,
            sandbox_id=sandbox_id,
            policy_id=policy_id,
            action_type=action_type,
            outcome=outcome,
            since=since,
            until=until,
            limit=limit,
        )

    @app.get("/action-events/stream")
    async def action_events_stream(
        request: Request,
        agent_id: Optional[str] = Query(default=None),
        task_id: Optional[str] = Query(default=None),
        session_id: Optional[str] = Query(default=None),
        sandbox_id: Optional[str] = Query(default=None),
        policy_id: Optional[str] = Query(default=None),
        action_type: Optional[str] = Query(default=None),
        outcome: Optional[str] = Query(default=None),
        since: Optional[str] = Query(default=None),
        timeout_seconds: float = Query(default=60.0),
        poll_interval_seconds: float = Query(default=1.0),
    ) -> StreamingResponse:
        async def iter_events() -> Any:
            cursor = since
            deadline = time.monotonic() + _agentbus_clamp_timeout(timeout_seconds)
            poll_interval = _agentbus_clamp_poll_interval(poll_interval_seconds)
            while True:
                if await request.is_disconnected():
                    break
                events = cp.list_action_events(
                    agent_id=agent_id,
                    task_id=task_id,
                    session_id=session_id,
                    sandbox_id=sandbox_id,
                    policy_id=policy_id,
                    action_type=action_type,
                    outcome=outcome,
                    since=cursor,
                    limit=100,
                )
                if events:
                    for event in reversed(events):
                        yield json.dumps(event.to_dict(), sort_keys=True) + "\n"
                        cursor = event.timestamp
                    if time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(0)
                    continue
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(poll_interval)

        return StreamingResponse(iter_events(), media_type="application/x-ndjson")

    @app.post("/agents/{agent_id}/installed-packages")
    def update_agent_installed_packages(
        agent_id: str,
        body: AgentInstalledPackagesUpdate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_actor(agent_id)  # an agent reports only its OWN footprint
        return cp.update_agent_installed_packages(
            agent_id, body.installed_packages, actor=agent_id
        ).to_dict()

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
    def dead_letters(
        tenant_id: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
        cursor: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [
            task.to_dict()
            for task in cp.list_dead_letters(
                tenant_id,
                limit=limit,
                cursor=cursor,
            )
        ]

    @app.get("/dispatch/dead-letters/page")
    def dead_letters_page(
        tenant_id: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
        cursor: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        page = cp.list_dead_letters_page(
            tenant_id,
            limit=limit,
            cursor=cursor,
        )
        return {
            "tasks": [task.to_dict() for task in page["tasks"]],
            "next_cursor": page["next_cursor"],
            "has_more": page["has_more"],
        }

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
        # record_log returns None when the log name is a silenced high-volume
        # idle-poll emitter (mem-04 dropped 1.83M/2.09M rows from these). That's
        # an intentional drop, not an error — report it as filtered rather than
        # calling .to_dict() on None, which 500'd on every silenced poll log.
        event = cp.record_log(**_data(body))
        return event.to_dict() if event is not None else {"filtered": True}

    @app.post("/observability/prune")
    def prune_observability(
        body: ObservabilityPruneRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, int]:
        principal.require_global_fleet()
        return {"removed": cp.prune_observability(**_data(body))}

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

    @app.post("/integrations/findings")
    def record_integration_finding_endpoint(
        body: IntegrationFindingCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        finding = cp.record_integration_finding(
            body.source_kind,
            body.source_id,
            body.finding_type,
            body.title,
            body.detail,
            severity=body.severity,
            fingerprint=body.fingerprint,
            notify=body.notify,
            channels=body.channels,
            notification_body=body.notification_body,
        )
        return finding.to_dict()

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

    # Runtime-neutral public identities and OpenClaw delivery ------------

    @app.post("/communication/identities")
    def configure_communication_identity(
        body: CommunicationIdentityConfig,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.configure_communication_identity(**_data(body)).to_dict()

    @app.get("/communication/identities")
    def list_communication_identities(
        enabled: Optional[bool] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [item.to_dict() for item in cp.list_communication_identities(enabled)]

    @app.get("/communication/identities/{identity_id_or_name}")
    def get_communication_identity(identity_id_or_name: str) -> Dict[str, Any]:
        return cp.get_communication_identity(identity_id_or_name).to_dict()

    @app.delete("/communication/identities/{identity_id_or_name}")
    def delete_communication_identity(
        identity_id_or_name: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        cp.delete_communication_identity(identity_id_or_name)
        return {"deleted": identity_id_or_name}

    @app.post("/communication/accounts")
    def configure_communication_account(
        body: CommunicationAccountConfig,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.configure_communication_account(**_data(body)).to_dict()

    @app.get("/communication/accounts")
    def list_communication_accounts(
        identity_id: Optional[str] = Query(default=None),
        channel: Optional[str] = Query(default=None),
        enabled: Optional[bool] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [
            item.to_dict()
            for item in cp.list_communication_accounts(
                identity_id=identity_id, channel=channel, enabled=enabled
            )
        ]

    @app.get("/communication/accounts/{account_id}")
    def get_communication_account(account_id: str) -> Dict[str, Any]:
        return cp.get_communication_account(account_id).to_dict()

    @app.delete("/communication/accounts/{account_id}")
    def delete_communication_account(
        account_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        cp.delete_communication_account(account_id)
        return {"deleted": account_id}

    @app.post("/communication/representations")
    def configure_representation_binding(
        body: RepresentationBindingConfig,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.configure_representation_binding(**_data(body)).to_dict()

    @app.get("/communication/representations")
    def list_representation_bindings(
        subject_kind: Optional[str] = Query(default=None),
        identity_id: Optional[str] = Query(default=None),
        enabled: Optional[bool] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [
            item.to_dict()
            for item in cp.list_representation_bindings(
                subject_kind=subject_kind, identity_id=identity_id, enabled=enabled
            )
        ]

    @app.delete("/communication/representations/{binding_id}")
    def delete_representation_binding(
        binding_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        cp.delete_representation_binding(binding_id)
        return {"deleted": binding_id}

    @app.get("/agents/{agent_id}/representation")
    def resolve_agent_representation(
        agent_id: str,
        project: Optional[str] = Query(default=None),
        role: Optional[str] = Query(default=None),
        fleet: str = Query(default="default"),
    ) -> Dict[str, Any]:
        return cp.resolve_agent_representation(
            agent_id, project=project, role=role, fleet=fleet
        )

    @app.post("/communication/gateway-leases/acquire")
    def acquire_gateway_identity_lease(
        body: GatewayIdentityLeaseAcquire,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_actor(body.agent_id)
        return cp.acquire_gateway_identity_lease(**_data(body)).to_dict()

    @app.post("/communication/gateway-leases/{lease_id}/renew")
    def renew_gateway_identity_lease(
        lease_id: str,
        body: GatewayIdentityLeaseRenew,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_actor(body.agent_id)
        return cp.renew_gateway_identity_lease(lease_id, **_data(body)).to_dict()

    @app.post("/communication/gateway-leases/{lease_id}/release")
    def release_gateway_identity_lease(
        lease_id: str,
        body: GatewayIdentityLeaseRelease,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_actor(body.agent_id)
        cp.release_gateway_identity_lease(lease_id, **_data(body))
        return {"released": lease_id}

    @app.get("/communication/gateway-leases")
    def list_gateway_identity_leases(
        agent_id: Optional[str] = Query(default=None),
        active_only: bool = Query(default=False),
    ) -> List[Dict[str, Any]]:
        return [
            item.to_dict()
            for item in cp.list_gateway_identity_leases(
                agent_id=agent_id, active_only=active_only
            )
        ]

    @app.post("/communication/deliveries")
    def enqueue_human_message(
        body: HumanMessageCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        if body.origin_agent_id:
            principal.assert_actor(body.origin_agent_id)
        elif not principal.is_admin:
            raise AuthorizationError("agent delivery requires origin_agent_id")
        return cp.enqueue_human_message(**_data(body)).to_dict()

    @app.get("/communication/deliveries")
    def list_human_messages(
        status: Optional[str] = Query(default=None),
        identity_id: Optional[str] = Query(default=None),
        origin_agent_id: Optional[str] = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> List[Dict[str, Any]]:
        return [
            item.to_dict()
            for item in cp.list_human_messages(
                status=status,
                identity_id=identity_id,
                origin_agent_id=origin_agent_id,
                limit=limit,
            )
        ]

    @app.post("/communication/deliveries/claim")
    def claim_human_messages(
        body: HumanMessageClaim,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> List[Dict[str, Any]]:
        principal.assert_actor(body.agent_id)
        return [item.to_dict() for item in cp.claim_human_messages(**_data(body))]

    @app.post("/communication/deliveries/{delivery_id}/ack")
    def acknowledge_human_message(
        delivery_id: str,
        body: HumanMessageAck,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_actor(body.agent_id)
        return cp.acknowledge_human_message(delivery_id, **_data(body)).to_dict()

    @app.post("/communication/deliveries/{delivery_id}/fail")
    def fail_human_message(
        delivery_id: str,
        body: HumanMessageFail,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_actor(body.agent_id)
        return cp.fail_human_message(delivery_id, **_data(body)).to_dict()

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
        _refuse_agent_minted_directives(principal, body.topic)
        return cp.open_agentbus_stream(**_data(body)).to_dict()

    @app.get("/agentbus/streams")
    def list_agentbus_streams(
        agent_id: Optional[str] = Query(default=None),
        status: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> List[Dict[str, Any]]:
        # The agent id is an authorization boundary, not a caller-controlled
        # filter. A bound agent may inspect only streams in which it
        # participates; fleet-wide enumeration remains an admin operation.
        if agent_id is None:
            principal.require_admin()
        else:
            principal.assert_actor(agent_id)
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
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> List[Dict[str, Any]]:
        principal.assert_actor(agent_id)
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
        _refuse_agent_minted_directives(principal, body.topic)
        return cp.publish_agentbus_content(**_data(body))

    @app.post("/agentbus/repo-update")
    def publish_agentbus_repo_update(
        body: AgentBusRepoUpdate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # mac-si4l: /agentbus/repo-update is a restart primitive. Require
        # admin scope for any peer/fleet fanout; a non-admin agent token can
        # only request a repo-update stream addressed to its own future self.
        if not principal.is_admin:
            principal.assert_actor(body.sender_agent_id)
            if body.all_agents:
                raise AuthorizationError(
                    "fleet-wide repo-update (all_agents=true) requires admin scope"
                )
            recipients = set(body.recipient_agent_ids)
            if recipients != {body.sender_agent_id}:
                raise AuthorizationError(
                    "repo-update to peer agents requires admin scope"
                )
        return cp.publish_agentbus_repo_update(
            sender_agent_id=body.sender_agent_id,
            recipient_agent_ids=body.recipient_agent_ids,
            all_agents=body.all_agents,
            repo_path=body.repo_path,
            remote=body.remote,
            branch=body.branch,
            restart=body.restart,
            restart_services=body.restart_services,
            request_id=body.request_id,
            target_sha=body.target_sha,
            desired_generation=body.desired_generation,
            release_id=body.release_id,
        )

    @app.get("/source-convergence")
    def source_convergence_status(
        fleet_id: Optional[str] = Query(default=None),
        limit: int = Query(default=250, ge=1, le=1000),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.source_convergence_status(fleet_id=fleet_id, limit=limit)

    @app.post("/source-convergence/tick")
    def tick_source_convergence(
        limit: int = Query(default=100, ge=1, le=1000),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_admin()
        return cp.tick_source_convergence(limit=limit)

    @app.post("/agentbus/artifact-publish")
    def publish_agentbus_artifact(
        body: AgentBusArtifactPublish,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_actor(body.sender_agent_id)
        return cp.publish_agentbus_artifact(**_data(body))

    @app.get("/agentbus/streams/{stream_id}/events")
    async def agentbus_stream_events(
        stream_id: str,
        request: Request,
        agent_id: str,
        after_sequence: int = Query(default=0),
        timeout_seconds: float = Query(default=30.0),
        poll_interval_seconds: float = Query(default=0.25),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> StreamingResponse:
        # Authorize before we start streaming so denials surface as proper HTTP
        # errors rather than a half-open response.
        principal.assert_actor(agent_id)
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
    def request_review(
        task_id: str,
        body: ReviewRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # Reviewer assignment is a scheduler/operator action. An agent may
        # claim only the review already assigned to its bound identity; it may
        # not nominate itself (or a collaborator) by forging payload fields.
        principal.require_admin()
        actor = principal.client_id or "admin.review-assignment"
        return cp.request_review(
            task_id, body.reviewer_agent_id, actor
        ).to_dict()

    @app.post("/reviews/{review_id}/claim")
    def claim_review(
        review_id: str,
        body: ReviewClaim,
        background_tasks: BackgroundTasks,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _assert_review_actor(principal, review_id, body.reviewer_agent_id)
        claim = cp.claim_review(
            review_id,
            body.reviewer_agent_id,
            executor_evidence_id=body.executor_evidence_id,
            actor=body.reviewer_agent_id,
            sync_beads=False,
        )
        return claim

    @app.post("/reviews/{review_id}/decision")
    def submit_review(
        review_id: str,
        body: ReviewDecision,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _assert_review_actor(principal, review_id, body.reviewer_agent_id)
        return cp.submit_review(review_id, **_data(body)).to_dict()

    @app.post("/reviews/default/tick")
    def default_review_tick(
        limit: int = Query(default=100),
        actor: str = Query(default="operator"),
        tenant_id: Optional[str] = Query(default=None),
        cursor: Optional[str] = Query(default=None),
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
            limit=limit,
            actor=actor,
            tenant_id=tenant_id,
            cursor=cursor,
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
        max_calls = 30
        window_seconds = 60
        # admin tokens are operator-driven and unlikely to enumerate
        if principal.is_admin:
            return
        key = "%s::%s::%s::%s" % (
            principal.tenant_id or "-",
            principal.agent_id or "-",
            principal.client_id or "-",
            route,
        )
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

    @app.post("/secrets/{name}/resolve")
    def resolve_secret(
        name: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # th-merge-07: audited admin reveal-by-name for in-fleet consumers — the
        # Slack fetcher reads slack.<agent>.* from mac's vault now that TokenHub is
        # retired. Requires the `secret` scope (admin inherits it); decrypt-at-use
        # and access-audited via SecretsService.resolve_secret_value. Distinct from
        # the request/reveal handle flow (which is for per-agent scoped access).
        _enforce_secret_rate_limit(principal, "resolve")  # mac-xc8u
        secrets = getattr(cp, "secrets", None)
        if secrets is None:
            raise NotFoundError("secret store unavailable")
        accessor = getattr(principal, "agent_id", None) or "fleet-fetch"
        value = secrets.resolve_secret_value(name, purpose="fleet-fetch", accessor=accessor)
        if value is None:
            raise NotFoundError("secret not found or disabled: %s" % name)
        return {"name": name, "value": value}

    @app.delete("/secrets/{name}")
    def delete_secret(
        name: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # Hard-delete a secret (scrub the value + remove the row). Requires the
        # `secret` scope (admin inherits it; covered by _required_scope for
        # /secrets*). Used to clean up stale/decommissioned secrets, e.g. a spoke's
        # now-unused router key after key centralization.
        secrets = getattr(cp, "secrets", None)
        if secrets is None:
            raise NotFoundError("secret store unavailable")
        actor = getattr(principal, "agent_id", None) or "operator"
        return cp.delete_secret(name, actor=actor)

    @app.post("/secrets/{name}/rotate")
    def rotate_secret(
        name: str,
        body: SecretRotate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # Rotate a secret's value in place, audited as a rotation — vs the
        # DELETE + re-POST dance an operator otherwise has to do (and which loses
        # the secret's id/scopes). Requires the `secret` scope (admin inherits
        # it; covered by _required_scope for /secrets*). Used to swap an upstream
        # provider key in the hub vault, e.g. correcting a media key that was
        # escrowed from the wrong source (nvidia-image vs the chat key).
        actor = getattr(principal, "agent_id", None) or body.actor or "operator"
        return cp.rotate_secret(name, body.value, actor).to_dict()

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

    @app.delete("/artifacts/{artifact_id_or_digest}")
    def delete_artifact(
        artifact_id_or_digest: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        actor = getattr(principal, "agent_id", None) or "operator"
        return cp.delete_artifact(artifact_id_or_digest, actor=actor)

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

    @app.post("/runtime-deltas")
    def propose_runtime_delta(
        body: RuntimeDeltaPropose,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        principal.assert_actor(body.agent_id)
        return cp.propose_runtime_delta(**_data(body)).to_dict()

    @app.get("/runtime-deltas")
    def list_runtime_deltas(
        status: Optional[str] = Query(default=None),
        task_id: Optional[str] = Query(default=None),
        project: Optional[str] = Query(default=None),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> List[Dict[str, Any]]:
        return [
            delta.to_dict()
            for delta in cp.list_runtime_deltas(
                status=status,
                task_id=task_id,
                project=project,
                limit=limit,
            )
        ]

    @app.get("/runtime-deltas/{delta_id}")
    def get_runtime_delta(delta_id: str) -> Dict[str, Any]:
        return cp.get_runtime_delta(delta_id).to_dict()

    @app.post("/runtime-deltas/{delta_id}/validate")
    def validate_runtime_delta(
        delta_id: str,
        body: RuntimeDeltaValidate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.validate_runtime_delta(delta_id, body.actor).to_dict()

    @app.post("/runtime-deltas/{delta_id}/reject")
    def reject_runtime_delta(
        delta_id: str,
        body: RuntimeDeltaReject,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.reject_runtime_delta(delta_id, body.actor, body.reason).to_dict()

    @app.post("/runtime-deltas/{delta_id}/promote")
    def promote_runtime_delta(
        delta_id: str,
        body: RuntimeDeltaPromote,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.promote_runtime_delta(
            delta_id,
            body.actor,
            runtime_name=body.runtime_name,
        ).to_dict()

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

    @app.post("/bridge/repositories")
    def register_project_repository(body: ProjectRepositoryRegister) -> Dict[str, Any]:
        _ensure_payload_bounded(body.metadata, "repository.metadata")
        return cp.register_project_repository(**_data(body)).to_dict()

    @app.get("/bridge/repositories")
    def list_project_repositories(
        enabled: Optional[bool] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [
            repository.to_dict()
            for repository in cp.list_project_repositories(enabled=enabled)
        ]

    # The legacy beads poller/repair endpoints remain removed. The repository
    # registry above is the current contract-backed execution surface.

    @app.post("/memory")
    def add_memory(body: MemoryCreate) -> Dict[str, Any]:
        data = _data(body)
        data.setdefault("task_id", None)
        data.setdefault("evidence_id", None)
        return cp.add_memory(**data).to_dict()

    @app.post("/memory/summarize-actions")
    def memory_summarize_actions(body: MemorySummarizeActions) -> Dict[str, Any]:
        return cp.summarize_actions_to_memory(**_data(body))

    @app.get("/memory")
    def search_memory(
        task_id: Optional[str] = Query(default=None),
        subject_type: Optional[str] = Query(default=None),
        subject_id: Optional[str] = Query(default=None),
        record_type: Optional[str] = Query(default=None, description="Exact record_type filter (e.g. nap_summary)"),
        record_type_prefix: Optional[str] = Query(default=None, description="Prefix match on record_type (e.g. dream:)"),
        created_by: Optional[str] = Query(default=None, description="Filter by creator (e.g. nap-consolidator, agent_rocky)"),
        since: Optional[str] = Query(default=None, description="ISO-8601 lower bound on created_at (inclusive)"),
        until: Optional[str] = Query(default=None, description="ISO-8601 upper bound on created_at (inclusive)"),
        limit: Optional[int] = Query(default=None, ge=1, description="Maximum number of records to return"),
        order: str = Query(default="asc", description="Sort order: asc (oldest first) or desc (newest first)"),
        content_contains: Optional[str] = Query(default=None, description="Case-insensitive substring match on record content"),
    ) -> List[Dict[str, Any]]:
        return [
            record.to_dict()
            for record in cp.search_memory(
                task_id,
                subject_type,
                subject_id,
                record_type=record_type,
                record_type_prefix=record_type_prefix,
                created_by=created_by,
                since=since,
                until=until,
                limit=limit,
                order=order,
                content_contains=content_contains,
            )
        ]

    @app.post("/memory/remembered")
    def remember_memory(body: MemoryRemember) -> Dict[str, Any]:
        return cp.remember_memory(**_data(body)).to_dict()

    @app.get("/memory/remembered")
    def list_remembered_memory(project: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return cp.list_remembered_memory(project=project)

    @app.delete("/memory/remembered/{key}")
    def forget_memory(key: str, project: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        return cp.forget_memory(key, project=project)

    # mem-10: memory-tier health snapshot for operators + future alerter.
    @app.get("/v1/memory/health")
    def memory_health(
        nap_interval_hours: float = Query(default=1.0, ge=0.1, le=720.0),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        return cp.memory_health(nap_interval_hours=nap_interval_hours)

    # mem-09: vector-tier recall.
    @app.get("/v1/memory/recall")
    def recall_memory(
        q: str = Query(..., min_length=1, description="free-form query text"),
        tier: str = Query(default="medium"),
        limit: int = Query(default=5, ge=1, le=100),
        min_score: Optional[float] = Query(default=None),
        project: Optional[str] = Query(default=None),
        tenant_id: Optional[str] = Query(default=None),
        agent_id: Optional[str] = Query(default=None),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> List[Dict[str, Any]]:
        if principal.agent_id and agent_id and principal.agent_id != agent_id:
            raise AuthorizationError("agent token cannot recall a peer agent's memory")
        return cp.recall_memory(
            q,
            tier=tier,
            limit=limit,
            min_score=min_score,
            project=project,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )

    @app.get("/v1/agents/{agent_id}/continuity")
    def get_openclaw_continuity_context(
        agent_id: str,
        q: str = Query(default="", max_length=8000),
        limit: int = Query(default=5, ge=0, le=20),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Return the bound agent's dynamic mood and medium-term memories.

        This endpoint is the narrow runtime bridge used by MAC's OpenClaw
        plugin.  It lives below ``/v1`` so an ordinary bound agent token can
        read its own context without receiving fleet-wide ``read`` scope.
        """
        if principal.agent_id and principal.agent_id != agent_id:
            raise AuthorizationError("agent token cannot read a peer agent's continuity context")
        cp.get_agent(agent_id)
        mood = cp.get_current_mood(agent_id)
        from mac.openclaw_continuity import ContinuityMetrics, recall_continuity

        memories: List[Dict[str, Any]] = []
        metrics = ContinuityMetrics()
        if q.strip() and limit:
            # Selective, provenance-rich recall: a calibrated score floor keeps
            # low-value filler out, and bounded AgentBus recall lets a prior
            # peer conversation resurface labelled with its source and score.
            memories, metrics = recall_continuity(
                agent_id=agent_id,
                query=q,
                limit=limit,
                recall=cp.recall_memory,
                agentbus=getattr(cp, "agentbus", None),
            )
        from mac.mood_policy import render_mood_overlay

        mood_dict = mood.to_dict() if mood is not None else None
        # The learning read-bridge failing silently is how a dead loop went
        # unnoticed for weeks — every serve is now an observable event.  Query
        # contents are never logged; only counts and the source mix are.
        cp.record_log(
            "continuity.context_served",
            subject_type="agent",
            subject_id=agent_id,
            detail={
                "memory_count": len(memories),
                "query_length": len(q.strip()),
                "has_mood": mood_dict is not None,
                **metrics.to_dict(),
            },
        )
        cp.record_metric(
            "continuity.selected_results",
            float(metrics.selected),
            unit="items",
            subject_type="agent",
            subject_id=agent_id,
            detail=metrics.to_dict(),
        )
        return {
            "schema": "mac.openclaw_continuity_context.v1",
            "agent_id": agent_id,
            "mood": mood_dict,
            "mood_prompt": render_mood_overlay(
                str((mood_dict or {}).get("mode") or ""),
                reason=(mood_dict or {}).get("reason"),
            ),
            "memories": memories,
            "recall_metrics": metrics.to_dict(),
        }

    @app.post("/v1/agents/{agent_id}/mood")
    def set_openclaw_agent_mood(
        agent_id: str,
        body: MoodSet,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Allow a bound OpenClaw runtime to self-report only its own mood."""
        if principal.agent_id and principal.agent_id != agent_id:
            raise AuthorizationError("agent token cannot set a peer agent's mood")
        values = _data(body)
        values.setdefault("set_by", agent_id)
        return cp.set_mood(agent_id, **values).to_dict()

    @app.delete("/v1/agents/{agent_id}/mood")
    def clear_openclaw_agent_mood(
        agent_id: str,
        body: MoodClear,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Optional[Dict[str, Any]]:
        """Allow a bound OpenClaw runtime to clear only its own mood."""
        if principal.agent_id and principal.agent_id != agent_id:
            raise AuthorizationError("agent token cannot clear a peer agent's mood")
        values = _data(body)
        values.setdefault("cleared_by", agent_id)
        cleared = cp.clear_mood(agent_id, **values)
        return cleared.to_dict() if cleared is not None else None

    @app.get("/v1/agents/{agent_id}/config-flags")
    def list_agent_config_flags(
        agent_id: str,
        channel: str = Query(default="", max_length=200),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Effective allowlisted config flags for one agent (+channel scope)."""
        if principal.agent_id and principal.agent_id != agent_id:
            raise AuthorizationError(
                "agent token cannot read a peer agent's config flags"
            )
        cp.get_agent(agent_id)
        return {
            "schema": "mac.agent_config_flags.v1",
            "agent_id": agent_id,
            "channel": channel,
            "flags": cp.list_config_flags(agent_id, channel=channel),
        }

    @app.put("/v1/agents/{agent_id}/config-flags/{flag}")
    def set_agent_config_flag(
        agent_id: str,
        flag: str,
        body: ConfigFlagSet,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Allow a bound runtime to set only its own allowlisted config flag."""
        if principal.agent_id and principal.agent_id != agent_id:
            raise AuthorizationError(
                "agent token cannot set a peer agent's config flags"
            )
        values = _data(body)
        values.setdefault("set_by", agent_id)
        return cp.set_config_flag(agent_id, flag, **values)

    @app.delete("/v1/agents/{agent_id}/config-flags/{flag}")
    def clear_agent_config_flag(
        agent_id: str,
        flag: str,
        body: ConfigFlagClear,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Allow a bound runtime to clear only its own config flag override."""
        if principal.agent_id and principal.agent_id != agent_id:
            raise AuthorizationError(
                "agent token cannot clear a peer agent's config flags"
            )
        values = _data(body)
        values.setdefault("cleared_by", agent_id)
        cleared = cp.clear_config_flag(agent_id, flag, **values)
        return {
            "agent_id": agent_id,
            "flag": flag,
            "channel": values.get("channel", ""),
            "cleared": bool(cleared),
        }

    @app.put("/v1/agents/{agent_id}/deploy-config")
    def report_agent_deploy_config(
        agent_id: str,
        body: DeployConfigReport,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Let a bound gateway self-report only its own deploy-config doc."""
        if principal.agent_id and principal.agent_id != agent_id:
            raise AuthorizationError(
                "agent token cannot report a peer agent's deploy config"
            )
        values = _data(body)
        _ensure_payload_bounded(values.get("document"), "agent.deploy_config")
        values.setdefault("reported_by", agent_id)
        schema_name = values.pop("schema_name", None)
        return cp.report_agent_deploy_config(
            agent_id, schema=schema_name, **values
        )

    @app.get("/agentbus/streams/{stream_id}/directive-verification")
    def verify_human_directive_route(
        stream_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Any authenticated agent can verify a cited directive stream is a
        genuine operator-minted human directive (relay-by-citation)."""
        return cp.verify_human_directive(stream_id)

    @app.get("/v1/agents/{agent_id}/agentbus-cursor")
    def get_agentbus_cursor(
        agent_id: str,
        topic: str = Query(max_length=200),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Hub-durable consumer read position (task_0d50e190): survives
        gateway/sandbox rebuilds, unlike a local state file."""
        if principal.agent_id and principal.agent_id != agent_id:
            raise AuthorizationError(
                "agent token cannot read a peer agent's agentbus cursor"
            )
        cursor = cp.get_agentbus_consumer_cursor(agent_id, topic)
        return cursor or {"agent_id": agent_id, "topic": topic, "position": None}

    @app.put("/v1/agents/{agent_id}/agentbus-cursor")
    def set_agentbus_cursor(
        agent_id: str,
        body: AgentBusCursorSet,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        if principal.agent_id and principal.agent_id != agent_id:
            raise AuthorizationError(
                "agent token cannot set a peer agent's agentbus cursor"
            )
        return cp.set_agentbus_consumer_cursor(agent_id, body.topic, body.position)

    @app.post("/agentbus/request")
    async def agentbus_request(
        body: AgentBusRequest,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """First-class request/reply (task_0d50e190): publish a request and
        long-poll its correlated reply hub-side, so every consumer stops
        re-implementing the correlation-id wait loop. A lapsed deadline
        returns a standard mac.agentbus.error.v1 timeout payload instead of
        a bespoke shape per caller."""
        from mac.agentbus_schemas import error_payload

        principal.assert_actor(body.sender_agent_id)
        _refuse_agent_minted_directives(principal, body.topic)
        correlation_id = (body.correlation_id or "").strip() or new_id("corr")
        payload = dict(body.payload)
        payload.setdefault("correlation_id", correlation_id)
        headers = {"correlation_id": correlation_id, "reply_topic": body.reply_topic}
        published = cp.publish_agentbus_content(
            sender_agent_id=body.sender_agent_id,
            recipient_agent_id=body.recipient_agent_id,
            topic=body.topic,
            content_type=body.content_type,
            headers=headers,
            payload=payload,
            task_id=body.task_id,
        )
        deadline_seconds = min(
            max(0.0, float(body.deadline_seconds)), AGENTBUS_MAX_EVENT_TIMEOUT_SECONDS
        )
        deadline = time.monotonic() + deadline_seconds
        reply_payload: Optional[Dict[str, Any]] = None
        reply_stream_id: Optional[str] = None
        while time.monotonic() < deadline:
            for stream in cp.list_agentbus_streams(
                agent_id=body.sender_agent_id, limit=100
            ):
                if (
                    stream.recipient_agent_id == body.sender_agent_id
                    and stream.topic == body.reply_topic
                    and str((stream.headers or {}).get("correlation_id") or "")
                    == correlation_id
                ):
                    chunks = cp.read_agentbus_chunks(
                        body.sender_agent_id, stream.id, limit=100
                    )
                    if chunks:
                        candidate = chunks[-1].payload
                        if isinstance(candidate, dict):
                            reply_payload = candidate
                            reply_stream_id = stream.id
                    break
            if reply_payload is not None:
                break
            await asyncio.sleep(0.35)
        if reply_payload is None:
            return {
                "schema": "mac.agentbus.request.v1",
                "status": "timeout",
                "correlation_id": correlation_id,
                "request_stream": published["stream"],
                "reply": error_payload(
                    "timeout",
                    "no reply within %.1fs" % deadline_seconds,
                    retryable=True,
                    correlation_id=correlation_id,
                ),
            }
        return {
            "schema": "mac.agentbus.request.v1",
            "status": "replied",
            "correlation_id": correlation_id,
            "request_stream": published["stream"],
            "reply_stream_id": reply_stream_id,
            "reply": reply_payload,
        }

    @app.post("/agentbus/human-directive")
    async def publish_human_directive_route(
        body: HumanDirectivePublish,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """The hub-verified human->agent channel: reaches ANY agent —
        including Slack-less and ephemeral ones — with provenance the
        receiver can trust by construction, because this route refuses
        agent-bound tokens entirely."""
        if principal.agent_id:
            raise AuthorizationError(
                "human directives require an operator token, not an agent token"
            )
        from mac.agentbus_schemas import error_payload

        issued_by = (body.issued_by or "").strip() or "human"
        published = cp.publish_human_directive(
            body.target_agent_id,
            body.message,
            issued_by=issued_by,
            task_id=body.task_id,
        )
        wait_seconds = min(
            max(0.0, float(body.wait_seconds)), AGENTBUS_MAX_EVENT_TIMEOUT_SECONDS
        )
        if wait_seconds <= 0:
            return {**published, "status": "queued"}
        correlation_id = published["correlation_id"]
        persona_id = cp.OPERATOR_PERSONA_AGENT_ID
        # A task-scoped directive is acknowledged over task.directive.ack.v1 by
        # the active executor; an ordinary directive is answered over
        # peer.reply.v1 by the agent's persona. Labeling the two distinctly
        # (delivery_kind) is exactly what keeps a conversation mirror from
        # implying a persona chat turn steered the task run (task_60be).
        from mac.executor_directive import (
            DELIVERY_KIND_EXECUTOR,
            DELIVERY_KIND_PERSONA,
            EXECUTOR_ACK_TOPIC,
        )

        reply_topics = ("peer.reply.v1", EXECUTOR_ACK_TOPIC)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            for stream in cp.list_agentbus_streams(agent_id=persona_id, limit=100):
                if (
                    stream.recipient_agent_id == persona_id
                    and stream.topic in reply_topics
                    and str((stream.headers or {}).get("correlation_id") or "")
                    == correlation_id
                ):
                    chunks = cp.read_agentbus_chunks(persona_id, stream.id, limit=100)
                    if chunks and isinstance(chunks[-1].payload, dict):
                        reply = chunks[-1].payload
                        delivery_kind = (
                            DELIVERY_KIND_EXECUTOR
                            if stream.topic == EXECUTOR_ACK_TOPIC
                            else DELIVERY_KIND_PERSONA
                        )
                        status = (
                            "acknowledged"
                            if stream.topic == EXECUTOR_ACK_TOPIC
                            else "replied"
                        )
                        return {
                            **published,
                            "status": status,
                            "delivery_kind": delivery_kind,
                            "reply": reply,
                        }
            await asyncio.sleep(0.35)
        return {
            **published,
            "status": "timeout",
            "reply": error_payload(
                "timeout",
                "no reply within %.1fs" % wait_seconds,
                retryable=True,
                correlation_id=correlation_id,
            ),
        }

    @app.post("/v1/agents/{agent_id}/deregister")
    def deregister_agent_route(
        agent_id: str,
        body: AgentDeregister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Graceful exit for a session/ephemeral agent (task_43f8d6e3).

        The agent (or its spawner) announces departure: an optional final
        human-facing message is enqueued first — it delivers even after the
        agent is gone — then the agent is tombstoned with history preserved.
        """
        if principal.agent_id and principal.agent_id != agent_id:
            raise AuthorizationError(
                "agent token cannot deregister a peer agent"
            )
        values = _data(body)
        values.setdefault("actor", agent_id)
        return cp.deregister_agent(agent_id, **values)

    @app.get("/v1/agents/{agent_id}/effective-config")
    def get_agent_effective_config(
        agent_id: str,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Consolidated per-agent config view: identity + flags + deploy doc.

        The single place to see every "geek knob" an agent runs with,
        instead of chasing launcher scripts, runtime.env, plugin constants,
        and hub metadata across hosts (task_dfdf6ea9).
        """
        if principal.agent_id and principal.agent_id != agent_id:
            raise AuthorizationError(
                "agent token cannot read a peer agent's effective config"
            )
        return cp.effective_agent_config(agent_id)

    @app.post("/v1/agents/{agent_id}/memory")
    def store_agent_memory(
        agent_id: str,
        body: AgentMemoryStore,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        """Let a bound runtime write a durable learning about ITSELF.

        This is the conversational write path Hermes' background review used
        to provide: without it, nothing an OpenClaw agent learns in chat can
        outlive the session. Records land in the raw tier as
        ``agent_learning*`` rows (``created_by = agent_id``), which is exactly
        the population nap consolidation summarizes into the recallable
        medium tier — so stored learnings flow into recall via the existing
        nap → embed pipeline rather than a new one.

        The record_type is constrained to the ``agent_learning`` namespace so
        an agent cannot masquerade as protected tiers (``user``, ``feedback``,
        ``fleet_learning``, ``dream`` — the decay-protected prefixes).
        """
        if principal.agent_id and principal.agent_id != agent_id:
            raise AuthorizationError(
                "agent token cannot write a peer agent's memory"
            )
        cp.get_agent(agent_id)
        record_type = (body.record_type or "agent_learning").strip()
        if record_type != "agent_learning" and not record_type.startswith("agent_learning:"):
            raise ValidationError(
                "record_type must be 'agent_learning' or 'agent_learning:<kind>'"
            )
        content = (body.content or "").strip()
        if not content:
            raise ValidationError("memory content must not be empty")
        if len(content) > 16000:
            raise ValidationError("memory content too long (max 16000 chars)")
        record = cp.add_memory(
            body.task_id,
            "agent",
            agent_id,
            record_type,
            content,
            None,
            agent_id,
        )
        cp.record_log(
            "memory.stored_by_agent",
            subject_type="agent",
            subject_id=agent_id,
            detail={
                "memory_id": record.id,
                "record_type": record_type,
                "content_length": len(content),
            },
        )
        return record.to_dict()

    @app.get("/v1/memory/dreams/recall")
    def recall_dream_artifacts(
        q: str = Query(..., min_length=1, description="free-form query text"),
        tier: str = Query(default="medium"),
        limit: int = Query(default=5, ge=1, le=100),
        min_score: Optional[float] = Query(default=None),
        project: Optional[str] = Query(default=None),
        agent_id: Optional[str] = Query(default=None),
        scope: Optional[str] = Query(default=None),
        kind: Optional[str] = Query(default=None),
        min_confidence: Optional[str] = Query(default=None),
        tenant_id: Optional[str] = Query(default=None),
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> List[Dict[str, Any]]:
        return cp.recall_dream_artifacts(
            q,
            tier=tier,
            limit=limit,
            min_score=min_score,
            project=project,
            agent_id=agent_id,
            scope=scope,
            kind=kind,
            min_confidence=min_confidence,
            tenant_id=tenant_id,
        )

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

    # th-merge-02: optional in-mac OpenAI front door (provider router + recovering
    # breaker). No-op unless MAC_ROUTER_BACKEND=inproc, so the standalone TokenHub
    # path is unchanged by default.
    try:
        from mac.router_app import mount_router

        # media-01 capability auto-routing: compose media bindings from LIVE
        # agents that self-advertised resources["media_routes"] (e.g. a GPU agent
        # announcing image.generate). Queried per request so a GPU agent coming
        # up/down is picked up without a hub restart; a registry hiccup degrades
        # to the static/config table (mount_media_router guards the call).
        def _media_agent_table_provider() -> Dict[str, Any]:
            from mac.media_routing import media_bindings_from_agents

            return media_bindings_from_agents(agent.to_dict() for agent in cp.list_agents())

        if mount_router(
            app,
            secret_resolver=_router_secret_resolver,
            route_observer=_router_route_observer,
            media_agent_table_provider=_media_agent_table_provider,
        ):
            _log.info("in-mac model router mounted (/v1/chat/completions, /v1/embeddings)")
    except Exception as exc:  # noqa: BLE001 - the router must never block app startup
        _log.warning("in-mac model router not mounted: %s", exc)

    return app
