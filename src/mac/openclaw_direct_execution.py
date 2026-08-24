"""Direct human-driven code execution for OpenClaw Slack personas.

Slack is served by the OpenClaw ``main`` persona, not a Hermes runtime. A direct
hub-authenticated human request that arrives through Slack/OpenClaw may authorize
OpenClaw to *begin the requested code change immediately* when a writable
repository execution context can be established — without forcing the human to
file a MAC task first and without forcing the persona to reply with a freshly
filed task before acting.

This module owns that contract. It is deliberately framed in first-class
OpenClaw/agent terminology (``persona_instance``, ``conversation``,
``execution``). A legacy ``hermes_instance`` identifier is accepted only through
a thin adapter (:func:`legacy_hermes_instance_adapter`) during migration; it is
never exposed as though Slack were running Hermes and no new public contract is
built around that name.

Design contract (task acceptance criteria):

* Direct human execution is distinguished from deferred work. A direct request
  runs in an isolated execution context; deferred / delegated / autonomously
  discovered / explicit follow-up work is filed as a visible MAC task instead
  (:class:`ExecutionMode`).
* A direct code change gets a dedicated read/write conversation worktree at an
  attested base SHA. It never edits the shared host checkout, the persistent
  read-only persona projection, or another task's lease-owned worktree.
* Capabilities are separate and explicit: ``source_inspection``,
  ``write_worktree``, ``publish_branch``, ``merge`` (:class:`Capability`). A
  direct human request may grant ``write_worktree``; it never implies
  ``publish_branch`` or ``merge`` and never bypasses the mandatory gates.
* Before any branch publication or merge, the same mandatory
  tests + evidence + independent-review gates used by ordinary code
  execution must pass, and review must target the exact candidate SHA/tree/diff
  produced by the conversation (:meth:`.can_publish`).
* When a downstream gate is still keyed by ``task_id``, a minimal MAC task /
  execution record is materialized automatically and transparently at execution
  start. That is system bookkeeping, not a manual prerequisite.
* The execution record binds OpenClaw agent/persona identity, immutable instance
  identity, the authenticated human directive, Slack workspace/channel/thread/
  message provenance, repository identity, base SHA, candidate ref/SHA, and an
  idempotency key. Follow-up messages in the same thread attach to the same live
  execution rather than create duplicates.
* The whole thing fails closed: with no writable execution context, repository
  identity, base attestation, or review path, it reports the missing capability
  accurately rather than pretending a code change occurred.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from mac.models import (
    JsonDict,
    NotFoundError,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)


EXECUTION_SCHEMA = "mac.openclaw_conversation_execution.v1"
GIT_SHA_LEN = 40


class ExecutionMode(str, Enum):
    """How a Slack/OpenClaw request maps onto repository work.

    ``DIRECT`` means an authenticated human asked for a code change *now*; the
    persona acts immediately in an isolated writable execution context. Every
    other value is deferred work that must be filed as a visible MAC task
    instead of being executed inline.
    """

    DIRECT = "direct"
    DEFERRED = "deferred"
    DELEGATED = "delegated"
    AUTONOMOUS_FOLLOWUP = "autonomous_followup"
    REQUESTED_FOLLOWUP = "requested_followup"

    @property
    def is_direct(self) -> bool:
        return self is ExecutionMode.DIRECT

    @property
    def files_task(self) -> bool:
        """Deferred/handoff modes must file a visible MAC task."""

        return self is not ExecutionMode.DIRECT


class Capability(str, Enum):
    """Explicitly separated repository capabilities.

    A direct human request may grant ``WRITE_WORKTREE``. It never implies
    ``PUBLISH_BRANCH`` or ``MERGE``; those remain behind the mandatory gates and
    publication/merge controls.
    """

    SOURCE_INSPECTION = "source_inspection"
    WRITE_WORKTREE = "write_worktree"
    PUBLISH_BRANCH = "publish_branch"
    MERGE = "merge"


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    WRITABLE = "writable"
    CANDIDATE_READY = "candidate_ready"
    REVIEWED = "reviewed"
    PUBLISHED = "published"
    MERGED = "merged"
    FAILED_CLOSED = "failed_closed"


# Capabilities an authenticated *direct* human directive is allowed to grant on
# its own. Publication and merge are intentionally excluded.
DIRECT_HUMAN_GRANTABLE: frozenset = frozenset(
    {Capability.SOURCE_INSPECTION, Capability.WRITE_WORKTREE}
)

# Gates that must all pass before a candidate may be published or merged.
MANDATORY_GATES: tuple = ("tests", "evidence", "review")


@dataclass(frozen=True)
class HumanDirective:
    """A hub-authenticated human directive received through Slack/OpenClaw."""

    human_id: str
    authenticated: bool
    text: str = ""

    def require_authenticated(self) -> None:
        if not self.authenticated:
            raise ValidationError(
                "direct execution requires a hub-authenticated human directive"
            )
        if not str(self.human_id or "").strip():
            raise ValidationError("human directive is missing an authenticated human id")


@dataclass(frozen=True)
class SlackProvenance:
    """Slack workspace/channel/thread/message provenance for a conversation.

    ``thread_key`` is the idempotency anchor: follow-up messages in the same
    thread attach to the same live execution instead of forking a duplicate.
    """

    workspace_id: str
    channel_id: str
    thread_ts: str
    message_ts: str = ""

    def validate(self) -> None:
        for name in ("workspace_id", "channel_id", "thread_ts"):
            if not str(getattr(self, name) or "").strip():
                raise ValidationError("slack provenance is missing %s" % name)

    @property
    def thread_key(self) -> str:
        return "%s/%s/%s" % (self.workspace_id, self.channel_id, self.thread_ts)

    def to_dict(self) -> JsonDict:
        return {
            "workspace_id": self.workspace_id,
            "channel_id": self.channel_id,
            "thread_ts": self.thread_ts,
            "message_ts": self.message_ts,
        }


@dataclass(frozen=True)
class RepositoryTarget:
    """Repository identity plus the attested base the change starts from."""

    repository_id: str
    repository_name: str
    base_sha: str

    def validate(self) -> None:
        if not str(self.repository_id or "").strip():
            raise MissingCapabilityError("repository", "repository identity is unavailable")
        base = str(self.base_sha or "").strip().lower()
        if len(base) != GIT_SHA_LEN or any(
            ch not in "0123456789abcdef" for ch in base
        ):
            raise MissingCapabilityError(
                "base_attestation",
                "repository base SHA is not a valid attested 40-hex commit",
            )

    def to_dict(self) -> JsonDict:
        return {
            "repository_id": self.repository_id,
            "repository_name": self.repository_name,
            "base_sha": str(self.base_sha).lower(),
        }


@dataclass
class WritableWorktree:
    """An isolated read/write checkout dedicated to one conversation execution."""

    path: str
    branch: str
    base_sha: str
    isolated: bool = True

    def to_dict(self) -> JsonDict:
        return {
            "path": self.path,
            "branch": self.branch,
            "base_sha": str(self.base_sha).lower(),
            "isolated": bool(self.isolated),
        }


class MissingCapabilityError(RuntimeError):
    """Fail-closed error: a required execution capability is unavailable.

    Reports the missing capability accurately; it must never be swallowed into a
    false claim that a code change occurred.
    """

    def __init__(self, capability: str, reason: str) -> None:
        super().__init__("%s unavailable: %s" % (capability, reason))
        self.capability = capability
        self.reason = reason

    def to_dict(self) -> JsonDict:
        return {
            "schema": "mac.openclaw_execution_failed_closed.v1",
            "status": ExecutionStatus.FAILED_CLOSED.value,
            "missing_capability": self.capability,
            "reason": self.reason,
            "code_change_occurred": False,
        }


# A worktree provisioner takes a repository target + branch and returns a
# WritableWorktree, or raises MissingCapabilityError when no writable, isolated
# checkout can be established. Injected so tests (and the live git-worktree
# provisioner) share the same contract.
WorktreeProvisioner = Callable[[RepositoryTarget, str], WritableWorktree]


def _idempotency_key(persona_instance_id: str, provenance: SlackProvenance) -> str:
    digest = hashlib.sha256(
        ("%s|%s" % (persona_instance_id, provenance.thread_key)).encode("utf-8")
    ).hexdigest()
    return "openclaw-exec-%s" % digest[:32]


def classify_request(
    *,
    directive: HumanDirective,
    deferred: bool = False,
    delegated: bool = False,
    autonomous_followup: bool = False,
    requested_followup: bool = False,
) -> ExecutionMode:
    """Distinguish a direct human execution request from deferred work.

    A direct, authenticated human request that is not explicitly deferred,
    delegated, or a follow-up resolves to :attr:`ExecutionMode.DIRECT` and is
    eligible to begin immediately. Everything else is filed as a MAC task.
    """

    if delegated:
        return ExecutionMode.DELEGATED
    if requested_followup:
        return ExecutionMode.REQUESTED_FOLLOWUP
    if autonomous_followup:
        return ExecutionMode.AUTONOMOUS_FOLLOWUP
    if deferred:
        return ExecutionMode.DEFERRED
    if directive.authenticated and str(directive.human_id or "").strip():
        return ExecutionMode.DIRECT
    # An unauthenticated or identity-less request cannot act directly; it is
    # captured as deferred work for a human to triage.
    return ExecutionMode.DEFERRED


@dataclass
class ConversationExecution:
    """A live execution bound to one OpenClaw Slack conversation thread."""

    id: str
    idempotency_key: str
    persona_instance_id: str
    persona_id: Optional[str]
    agent_id: Optional[str]
    human_id: str
    tenant_id: Optional[str]
    slack: SlackProvenance
    repository: RepositoryTarget
    mode: ExecutionMode
    status: ExecutionStatus
    granted_capabilities: List[Capability]
    task_id: Optional[str] = None
    worktree: Optional[WritableWorktree] = None
    candidate_ref: Optional[str] = None
    candidate_sha: Optional[str] = None
    candidate_tree_digest: Optional[str] = None
    review_target_sha: Optional[str] = None
    gate_results: Dict[str, bool] = field(default_factory=dict)
    metadata: JsonDict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def has_capability(self, capability: Capability) -> bool:
        return capability in self.granted_capabilities

    def gates_passed(self) -> bool:
        return all(bool(self.gate_results.get(gate)) for gate in MANDATORY_GATES)

    def review_matches_candidate(self) -> bool:
        """Review must target the exact candidate SHA the conversation produced."""

        return bool(
            self.candidate_sha
            and self.review_target_sha
            and self.candidate_sha == self.review_target_sha
        )

    def can_publish(self) -> tuple:
        """Return ``(allowed, reason)`` for publishing this candidate.

        Publication is blocked unless the ``PUBLISH_BRANCH`` capability is held,
        a candidate SHA exists, every mandatory gate passed, and review targeted
        the exact candidate SHA/tree.
        """

        if not self.has_capability(Capability.PUBLISH_BRANCH):
            return False, "publish_branch capability not granted"
        if not self.candidate_sha:
            return False, "no candidate SHA produced"
        if not self.gates_passed():
            missing = [g for g in MANDATORY_GATES if not self.gate_results.get(g)]
            return False, "mandatory gates not passed: %s" % ",".join(missing)
        if not self.review_matches_candidate():
            return False, "review did not target the exact candidate SHA"
        return True, ""

    def can_merge(self) -> tuple:
        if not self.has_capability(Capability.MERGE):
            return False, "merge capability not granted"
        allowed, reason = self.can_publish()
        if not allowed:
            return False, reason
        return True, ""

    def to_dict(self) -> JsonDict:
        return {
            "schema": EXECUTION_SCHEMA,
            "id": self.id,
            "idempotency_key": self.idempotency_key,
            "persona_instance_id": self.persona_instance_id,
            "persona_id": self.persona_id,
            "agent_id": self.agent_id,
            "human_id": self.human_id,
            "tenant_id": self.tenant_id,
            "slack": self.slack.to_dict(),
            "repository": self.repository.to_dict(),
            "mode": self.mode.value,
            "status": self.status.value,
            "granted_capabilities": [c.value for c in self.granted_capabilities],
            "task_id": self.task_id,
            "worktree": self.worktree.to_dict() if self.worktree else None,
            "candidate_ref": self.candidate_ref,
            "candidate_sha": self.candidate_sha,
            "candidate_tree_digest": self.candidate_tree_digest,
            "review_target_sha": self.review_target_sha,
            "gate_results": dict(self.gate_results),
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def legacy_hermes_instance_adapter(hermes_instance_id: str) -> str:
    """Adapt a legacy ``hermes_instance`` id to a persona-instance id.

    The persona-instance rename made the two ids identical at the data layer, so
    this adapter is an identity mapping. It exists only so migration-era callers
    can pass the old name *behind an adapter* without the public OpenClaw
    contract growing a ``hermes_instance`` surface.
    """

    value = str(hermes_instance_id or "").strip()
    if not value:
        raise ValidationError("legacy hermes_instance id is required")
    return value


class OpenClawDirectExecutionService:
    """Service owning OpenClaw direct human-driven code execution.

    It persists a :class:`ConversationExecution` per Slack thread, provisions an
    isolated writable worktree for direct requests, materializes the minimal
    task-keyed bookkeeping record when required, records gate results, and gates
    publication/merge behind the mandatory checks.
    """

    def __init__(
        self,
        store: Any,
        *,
        worktree_provisioner: Optional[WorktreeProvisioner] = None,
        get_persona_instance: Optional[Callable[[str], Any]] = None,
        materialize_task: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.store = store
        self._provision_worktree = worktree_provisioner
        self._get_persona_instance = get_persona_instance
        self._materialize_task = materialize_task

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def _row_to_execution(self, row: Any) -> ConversationExecution:
        slack = json_loads(row["slack"], {})
        repo = json_loads(row["repository"], {})
        worktree = json_loads(row["worktree"], None)
        return ConversationExecution(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            persona_instance_id=row["persona_instance_id"],
            persona_id=row["persona_id"],
            agent_id=row["agent_id"],
            human_id=row["human_id"],
            tenant_id=row["tenant_id"],
            slack=SlackProvenance(
                workspace_id=slack.get("workspace_id", ""),
                channel_id=slack.get("channel_id", ""),
                thread_ts=slack.get("thread_ts", ""),
                message_ts=slack.get("message_ts", ""),
            ),
            repository=RepositoryTarget(
                repository_id=repo.get("repository_id", ""),
                repository_name=repo.get("repository_name", ""),
                base_sha=repo.get("base_sha", ""),
            ),
            mode=ExecutionMode(row["mode"]),
            status=ExecutionStatus(row["status"]),
            granted_capabilities=[
                Capability(c) for c in json_loads(row["granted_capabilities"], [])
            ],
            task_id=row["task_id"],
            worktree=(
                WritableWorktree(
                    path=worktree.get("path", ""),
                    branch=worktree.get("branch", ""),
                    base_sha=worktree.get("base_sha", ""),
                    isolated=bool(worktree.get("isolated", True)),
                )
                if worktree
                else None
            ),
            candidate_ref=row["candidate_ref"],
            candidate_sha=row["candidate_sha"],
            candidate_tree_digest=row["candidate_tree_digest"],
            review_target_sha=row["review_target_sha"],
            gate_results=json_loads(row["gate_results"], {}),
            metadata=json_loads(row["metadata"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _persist(self, execution: ConversationExecution) -> None:
        self.store.execute(
            """
            INSERT INTO openclaw_conversation_executions (
                id, idempotency_key, persona_instance_id, persona_id, agent_id,
                human_id, tenant_id, slack, repository, mode, status,
                granted_capabilities, task_id, worktree, candidate_ref,
                candidate_sha, candidate_tree_digest, review_target_sha,
                gate_results, metadata, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                agent_id = excluded.agent_id,
                granted_capabilities = excluded.granted_capabilities,
                task_id = excluded.task_id,
                worktree = excluded.worktree,
                candidate_ref = excluded.candidate_ref,
                candidate_sha = excluded.candidate_sha,
                candidate_tree_digest = excluded.candidate_tree_digest,
                review_target_sha = excluded.review_target_sha,
                gate_results = excluded.gate_results,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                execution.id,
                execution.idempotency_key,
                execution.persona_instance_id,
                execution.persona_id,
                execution.agent_id,
                execution.human_id,
                execution.tenant_id,
                json_dumps(execution.slack.to_dict()),
                json_dumps(execution.repository.to_dict()),
                execution.mode.value,
                execution.status.value,
                json_dumps([c.value for c in execution.granted_capabilities]),
                execution.task_id,
                json_dumps(execution.worktree.to_dict()) if execution.worktree else None,
                execution.candidate_ref,
                execution.candidate_sha,
                execution.candidate_tree_digest,
                execution.review_target_sha,
                json_dumps(execution.gate_results),
                json_dumps(execution.metadata),
                execution.created_at,
                execution.updated_at,
            ),
        )

    def get_execution(self, execution_id: str) -> ConversationExecution:
        row = self.store.query_one(
            "SELECT * FROM openclaw_conversation_executions WHERE id = ?",
            (execution_id,),
        )
        if row is None:
            raise NotFoundError("conversation execution not found: %s" % execution_id)
        return self._row_to_execution(row)

    def find_by_idempotency_key(self, key: str) -> Optional[ConversationExecution]:
        row = self.store.query_one(
            "SELECT * FROM openclaw_conversation_executions WHERE idempotency_key = ?",
            (key,),
        )
        return self._row_to_execution(row) if row is not None else None

    # ------------------------------------------------------------------
    # Direct execution entry point
    # ------------------------------------------------------------------
    def begin_conversation_execution(
        self,
        *,
        persona_instance_id: Optional[str] = None,
        directive: HumanDirective,
        slack: SlackProvenance,
        repository: RepositoryTarget,
        agent_id: Optional[str] = None,
        requested_capabilities: Optional[List[Capability]] = None,
        deferred: bool = False,
        delegated: bool = False,
        autonomous_followup: bool = False,
        requested_followup: bool = False,
        metadata: Optional[JsonDict] = None,
        hermes_instance_id: Optional[str] = None,
    ) -> ConversationExecution:
        """Begin (or re-attach to) a Slack conversation execution.

        For a direct authenticated human request this provisions an isolated
        writable worktree at the attested base SHA and materializes the minimal
        task record automatically. Follow-up messages in the same thread attach
        to the existing live execution rather than duplicate it. Deferred /
        delegated / follow-up work is not executed inline: a visible MAC task is
        filed and returned with :attr:`ExecutionStatus.PENDING`.

        Fails closed (:class:`MissingCapabilityError`) when a direct request has
        no repository identity, base attestation, or writable execution context.
        """

        if persona_instance_id is None and hermes_instance_id is not None:
            # Legacy name accepted only behind the adapter.
            persona_instance_id = legacy_hermes_instance_adapter(hermes_instance_id)
        if not persona_instance_id:
            raise ValidationError("persona_instance_id is required")

        slack.validate()
        mode = classify_request(
            directive=directive,
            deferred=deferred,
            delegated=delegated,
            autonomous_followup=autonomous_followup,
            requested_followup=requested_followup,
        )

        key = _idempotency_key(persona_instance_id, slack)
        existing = self.find_by_idempotency_key(key)
        if existing is not None:
            # Conversation idempotency: a follow-up in the same thread attaches
            # to the same live execution instead of creating a duplicate.
            return existing

        persona_id, tenant_id = self._resolve_persona(persona_instance_id)

        now = utcnow()
        execution = ConversationExecution(
            id=new_id("openclaw-exec"),
            idempotency_key=key,
            persona_instance_id=persona_instance_id,
            persona_id=persona_id,
            agent_id=agent_id,
            human_id=str(directive.human_id or ""),
            tenant_id=tenant_id,
            slack=slack,
            repository=repository,
            mode=mode,
            status=ExecutionStatus.PENDING,
            granted_capabilities=[],
            metadata=ensure_json_object(metadata),
            created_at=now,
            updated_at=now,
        )

        if not mode.is_direct:
            # Deferred / handoff work is filed as a visible MAC task; it is NOT
            # executed inline.
            execution.task_id = self._file_deferred_task(execution, directive)
            self._persist(execution)
            return execution

        # --- Direct human execution path -------------------------------
        directive.require_authenticated()
        repository.validate()

        granted = self._grant_direct_capabilities(requested_capabilities)
        execution.granted_capabilities = granted

        # Auto bookkeeping: current review/pre-push gates are keyed by task_id.
        # Materialize the minimal task record transparently at execution start.
        execution.task_id = self._materialize_bookkeeping_task(execution, directive)

        # Dedicated isolated read/write worktree at the attested base SHA.
        worktree = self._provision_isolated_worktree(execution)
        execution.worktree = worktree
        execution.status = ExecutionStatus.WRITABLE
        execution.updated_at = utcnow()
        self._persist(execution)
        return execution

    # ------------------------------------------------------------------
    def _resolve_persona(self, persona_instance_id: str) -> tuple:
        if self._get_persona_instance is None:
            return None, None
        instance = self._get_persona_instance(persona_instance_id)
        return getattr(instance, "persona_id", None), getattr(instance, "tenant_id", None)

    def _grant_direct_capabilities(
        self, requested: Optional[List[Capability]]
    ) -> List[Capability]:
        if requested is None:
            requested = [Capability.SOURCE_INSPECTION, Capability.WRITE_WORKTREE]
        granted: List[Capability] = []
        for capability in requested:
            if capability in DIRECT_HUMAN_GRANTABLE and capability not in granted:
                granted.append(capability)
        # write_worktree presumes source inspection.
        if Capability.WRITE_WORKTREE in granted and Capability.SOURCE_INSPECTION not in granted:
            granted.insert(0, Capability.SOURCE_INSPECTION)
        return granted

    def _provision_isolated_worktree(
        self, execution: ConversationExecution
    ) -> WritableWorktree:
        if self._provision_worktree is None:
            raise MissingCapabilityError(
                "write_worktree",
                "no writable-worktree provisioner configured for this hub",
            )
        branch = "openclaw/%s/%s" % (
            execution.persona_instance_id,
            execution.id,
        )
        worktree = self._provision_worktree(execution.repository, branch)
        if worktree is None or not worktree.isolated:
            raise MissingCapabilityError(
                "write_worktree",
                "provisioner did not return an isolated writable checkout",
            )
        if str(worktree.base_sha).lower() != execution.repository.base_sha.lower():
            raise MissingCapabilityError(
                "base_attestation",
                "worktree base SHA does not match the attested repository base",
            )
        return worktree

    def _materialize_bookkeeping_task(
        self, execution: ConversationExecution, directive: HumanDirective
    ) -> Optional[str]:
        if self._materialize_task is None:
            return None
        title = "OpenClaw direct execution %s" % execution.slack.thread_key
        metadata = {
            "origin": {
                "type": "openclaw_direct_execution",
                "schema": EXECUTION_SCHEMA,
                "persona_instance_id": execution.persona_instance_id,
                "persona_id": execution.persona_id,
                "agent_id": execution.agent_id,
                "human_id": execution.human_id,
                "slack": execution.slack.to_dict(),
                "repository": execution.repository.to_dict(),
                "conversation_execution_id": execution.id,
                # This record is system bookkeeping created automatically so the
                # task-keyed gates can run; it is not a manual filing prerequisite.
                "auto_materialized": True,
                "bookkeeping_only": True,
            },
        }
        task = self._materialize_task(
            title=title,
            description=directive.text or "OpenClaw direct human code request.",
            metadata=metadata,
            idempotency_key=execution.idempotency_key,
        )
        return getattr(task, "id", None) if task is not None else None

    def _file_deferred_task(
        self, execution: ConversationExecution, directive: HumanDirective
    ) -> Optional[str]:
        if self._materialize_task is None:
            return None
        metadata = {
            "origin": {
                "type": "openclaw_deferred_work",
                "schema": EXECUTION_SCHEMA,
                "persona_instance_id": execution.persona_instance_id,
                "human_id": execution.human_id,
                "slack": execution.slack.to_dict(),
                "conversation_execution_id": execution.id,
                "mode": execution.mode.value,
                "deferred": True,
            },
        }
        task = self._materialize_task(
            title="OpenClaw deferred work (%s)" % execution.mode.value,
            description=directive.text or "OpenClaw deferred/handoff work.",
            metadata=metadata,
            idempotency_key=execution.idempotency_key,
        )
        return getattr(task, "id", None) if task is not None else None

    # ------------------------------------------------------------------
    # Candidate + gate lifecycle
    # ------------------------------------------------------------------
    def record_candidate(
        self,
        execution_id: str,
        *,
        candidate_ref: str,
        candidate_sha: str,
        candidate_tree_digest: Optional[str] = None,
    ) -> ConversationExecution:
        """Record the candidate ref/SHA produced by the conversation execution."""

        execution = self.get_execution(execution_id)
        if execution.worktree is None:
            raise MissingCapabilityError(
                "write_worktree",
                "cannot record a candidate without a writable worktree",
            )
        sha = str(candidate_sha or "").strip().lower()
        if len(sha) != GIT_SHA_LEN or any(ch not in "0123456789abcdef" for ch in sha):
            raise ValidationError("candidate SHA must be a 40-hex commit")
        execution.candidate_ref = candidate_ref
        execution.candidate_sha = sha
        execution.candidate_tree_digest = candidate_tree_digest
        execution.status = ExecutionStatus.CANDIDATE_READY
        execution.updated_at = utcnow()
        self._persist(execution)
        return execution

    def record_gate_result(
        self, execution_id: str, gate: str, passed: bool
    ) -> ConversationExecution:
        if gate not in MANDATORY_GATES:
            raise ValidationError("unknown gate: %s" % gate)
        execution = self.get_execution(execution_id)
        execution.gate_results[gate] = bool(passed)
        execution.updated_at = utcnow()
        self._persist(execution)
        return execution

    def record_review(
        self, execution_id: str, *, reviewed_sha: str, passed: bool
    ) -> ConversationExecution:
        """Record an independent review that targeted a specific SHA.

        The review only counts when it targeted the exact candidate SHA the
        conversation produced.
        """

        execution = self.get_execution(execution_id)
        execution.review_target_sha = str(reviewed_sha or "").strip().lower()
        matches = execution.review_matches_candidate()
        execution.gate_results["review"] = bool(passed and matches)
        if matches and passed:
            execution.status = ExecutionStatus.REVIEWED
        execution.updated_at = utcnow()
        self._persist(execution)
        return execution

    def publish_candidate(self, execution_id: str) -> ConversationExecution:
        """Publish the candidate branch, blocked until every gate passes.

        Fails closed via :class:`MissingCapabilityError` when publication is not
        yet allowed, so a caller can never mistake a blocked candidate for a
        published one.
        """

        execution = self.get_execution(execution_id)
        allowed, reason = execution.can_publish()
        if not allowed:
            raise MissingCapabilityError("publish_branch", reason)
        execution.status = ExecutionStatus.PUBLISHED
        execution.updated_at = utcnow()
        self._persist(execution)
        return execution

    def merge_candidate(self, execution_id: str) -> ConversationExecution:
        execution = self.get_execution(execution_id)
        allowed, reason = execution.can_merge()
        if not allowed:
            raise MissingCapabilityError("merge", reason)
        execution.status = ExecutionStatus.MERGED
        execution.updated_at = utcnow()
        self._persist(execution)
        return execution
