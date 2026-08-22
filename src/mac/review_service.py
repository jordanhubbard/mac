"""Review + Publication domain service.

A task transitions ``RUNNING → NEEDS_REVIEW → REVIEWING → COMPLETED`` via
this service. Reviewer independence is preferred and can be required by task
policy, but the control plane may authorize a recorded fallback when no
independent reviewer is available. Approving still requires signed verdict
evidence that belongs to the reviewed task and, for agent-generated work, a
different reviewer LLM. Completion requires an approved review pointing at
task evidence.

``publish_task`` is the only path that legitimately moves a task to
COMPLETED — it runs as a single transaction that flips the task row,
records the publication, writes two history rows (publish + transition),
emits the matching observability events, and idles the owning agent.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from mac.models import (
    Agent,
    AgentStatus,
    AuthorizationError,
    Evidence,
    NotFoundError,
    Publication,
    PublicationStatus,
    Review,
    ReviewStatus,
    Task,
    TaskState,
    TransitionError,
    ValidationError,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.messaging_service import MessagingService
from mac.observability_service import ObservabilityService
from mac.review_verdict import (
    consumed_attempt_count,
    verdict_is_harness_failure,
    with_infrastructure_attempt,
)


#: Review decisions a reviewer may submit. ``tests_failed`` and
#: ``infrastructure`` used to arrive here disguised as ``rejected``; see
#: :mod:`mac.review_verdict`.
TERMINAL_REVIEW_STATUSES = frozenset(
    {
        ReviewStatus.APPROVED.value,
        ReviewStatus.CHANGES_REQUESTED.value,
        ReviewStatus.REJECTED.value,
        ReviewStatus.TESTS_FAILED.value,
        ReviewStatus.INFRASTRUCTURE.value,
    }
)

#: Decisions that are a judgement about the WORK, and so spend an attempt and
#: carry feedback back to the executor.
WORK_QUALITY_REVIEW_STATUSES = frozenset(
    {
        ReviewStatus.CHANGES_REQUESTED.value,
        ReviewStatus.REJECTED.value,
        ReviewStatus.TESTS_FAILED.value,
    }
)

#: Decisions that send the task back for another run.
REOPENING_REVIEW_STATUSES = WORK_QUALITY_REVIEW_STATUSES | {
    ReviewStatus.INFRASTRUCTURE.value
}


def _state_value(state: Any) -> str:
    return state.value if hasattr(state, "value") else str(state)


def manifest_llm_model(manifest: Any) -> str:
    """Return the canonical model identity recorded in evidence metadata.

    ``llm.model`` is the canonical field. ``llm_model`` is accepted for
    simple shell-generated manifests, and ``opencode_model`` preserves the
    existing executor field while the fleet rolls forward.
    """
    if not isinstance(manifest, dict):
        return ""
    llm = manifest.get("llm")
    if isinstance(llm, dict):
        model = str(llm.get("model") or "").strip()
        if model:
            return model
    for key in ("llm_model", "opencode_model", "gateway_model"):
        model = str(manifest.get(key) or "").strip()
        if model:
            return model
    return ""


def normalize_llm_model(model: str) -> str:
    """Normalize an LLM model name for case-insensitive comparison."""
    return " ".join(str(model or "").strip().lower().split())


_MODEL_FAMILY_PATTERNS = (
    ("claude", re.compile(r"\bclaude\b", re.IGNORECASE)),
    ("openai-o", re.compile(r"(?:^|[/ :])o[1-9](?:\b|-)", re.IGNORECASE)),
    ("gpt", re.compile(r"\bgpt\b", re.IGNORECASE)),
    ("gemini", re.compile(r"\bgemini\b", re.IGNORECASE)),
    ("grok", re.compile(r"\bgrok\b", re.IGNORECASE)),
    ("deepseek", re.compile(r"\bdeepseek\b", re.IGNORECASE)),
    ("qwen", re.compile(r"\bqwen\b", re.IGNORECASE)),
    ("llama", re.compile(r"\bllama\b", re.IGNORECASE)),
    ("mistral", re.compile(r"\bmistral\b", re.IGNORECASE)),
    ("kimi", re.compile(r"\bkimi\b", re.IGNORECASE)),
    ("glm", re.compile(r"\bglm\b", re.IGNORECASE)),
    ("minimax", re.compile(r"\bminimax\b", re.IGNORECASE)),
)
_MODEL_PROVIDER_NAMES = (
    "anthropic", "openai", "google", "xai", "deepseek", "qwen", "meta",
    "mistral", "moonshot", "zai", "minimax",
)
_FAMILY_PROVIDER = {
    "claude": "anthropic",
    "gpt": "openai",
    "openai-o": "openai",
    "gemini": "google",
    "grok": "xai",
    "deepseek": "deepseek",
    "qwen": "qwen",
    "llama": "meta",
    "mistral": "mistral",
    "kimi": "moonshot",
    "glm": "zai",
    "minimax": "minimax",
}


def manifest_llm_family(manifest: Any) -> str:
    """Return a stable model lineage (Claude/GPT/Gemini/etc.) when known."""
    if not isinstance(manifest, dict):
        return ""
    explicit = normalize_llm_model(str(manifest.get("llm_family") or ""))
    if explicit:
        return explicit
    llm = manifest.get("llm")
    if isinstance(llm, dict):
        explicit = normalize_llm_model(str(llm.get("family") or ""))
        if explicit:
            return explicit
    model = manifest_llm_model(manifest)
    for family, pattern in _MODEL_FAMILY_PATTERNS:
        if pattern.search(model):
            return family
    return ""


def manifest_llm_provider(manifest: Any) -> str:
    """Return the upstream model provider, preferring explicit provenance."""
    if not isinstance(manifest, dict):
        return ""
    explicit = normalize_llm_model(str(manifest.get("llm_provider") or ""))
    if explicit:
        return explicit
    llm = manifest.get("llm")
    if isinstance(llm, dict):
        explicit = normalize_llm_model(str(llm.get("provider") or ""))
        if explicit:
            return explicit
    model = normalize_llm_model(manifest_llm_model(manifest))
    segments = [part for part in re.split(r"[/ :]", model) if part]
    for provider in _MODEL_PROVIDER_NAMES:
        if provider in segments:
            return provider
    return _FAMILY_PROVIDER.get(manifest_llm_family(manifest), "")


def review_diversity_requirements(task: Any) -> Dict[str, bool]:
    """Resolve opt-in and high-risk cross-model review requirements."""
    metadata = getattr(task, "metadata", None)
    if not isinstance(metadata, dict) and isinstance(task, dict):
        metadata = task.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    policy: Dict[str, Any] = {}
    for key in ("review", "default_review"):
        value = metadata.get(key)
        if isinstance(value, dict):
            policy = value
            break
    risk = str(
        policy.get("risk_level")
        or policy.get("risk")
        or metadata.get("risk_level")
        or metadata.get("risk")
        or ""
    ).strip().lower()
    high_risk = policy.get("high_risk") is True or risk in {"high", "critical"}
    return {
        "high_risk": high_risk,
        "different_model_family": high_risk
        or policy.get("require_different_model_family") is True,
        "different_provider": high_risk
        or policy.get("require_different_model_provider") is True
        or policy.get("require_different_provider") is True,
    }


def manifest_requires_cross_llm_review(manifest: Any) -> bool:
    """Return whether the evidence manifest requires cross-LLM review."""
    if not isinstance(manifest, dict):
        return False
    evidence_type = str(manifest.get("evidence_type") or "").strip().lower()
    if evidence_type == "review_verdict":
        return False
    executor = str(manifest.get("executor") or "").strip()
    if executor.startswith("mac-task-executor-"):
        return True
    if manifest.get("agent_generated") is True:
        return True
    if manifest.get("requires_cross_llm_review") is True:
        return True
    return bool(manifest_llm_model(manifest))


def cross_llm_review_problems(
    executor_manifest: Any,
    verdict_manifest: Any,
    *,
    requirements: Optional[Dict[str, bool]] = None,
) -> List[str]:
    """Return problems preventing a valid cross-LLM review of the evidence."""
    if not manifest_requires_cross_llm_review(executor_manifest):
        return []
    executor_model = manifest_llm_model(executor_manifest)
    reviewer_model = manifest_llm_model(verdict_manifest)
    problems: List[str] = []
    if not executor_model:
        problems.append(
            "executor evidence from an agent runner requires llm.model or llm_model"
        )
    if not reviewer_model:
        problems.append(
            "review_verdict evidence requires reviewer llm.model or llm_model"
        )
    if executor_model and reviewer_model and (
        normalize_llm_model(executor_model) == normalize_llm_model(reviewer_model)
    ):
        problems.append(
            "reviewer LLM must differ from executor LLM (both %s)"
            % executor_model
        )
    requirements = requirements or {}
    if executor_model and reviewer_model and requirements.get("different_model_family"):
        executor_family = manifest_llm_family(executor_manifest)
        reviewer_family = manifest_llm_family(verdict_manifest)
        if not executor_family:
            problems.append("executor evidence requires llm.family for family-diverse review")
        if not reviewer_family:
            problems.append("review verdict requires llm.family for family-diverse review")
        if executor_family and reviewer_family and executor_family == reviewer_family:
            problems.append(
                "reviewer LLM family must differ from executor family (both %s)"
                % executor_family
            )
    if executor_model and reviewer_model and requirements.get("different_provider"):
        executor_provider = manifest_llm_provider(executor_manifest)
        reviewer_provider = manifest_llm_provider(verdict_manifest)
        if not executor_provider:
            problems.append("executor evidence requires llm.provider for provider-diverse review")
        if not reviewer_provider:
            problems.append("review verdict requires llm.provider for provider-diverse review")
        if executor_provider and reviewer_provider and executor_provider == reviewer_provider:
            problems.append(
                "reviewer LLM provider must differ from executor provider (both %s)"
                % executor_provider
            )
    return problems


class ReviewService:
    def __init__(
        self,
        store: Any,
        observability: ObservabilityService,
        messaging: MessagingService,
        *,
        get_task: Callable[[str], Task],
        get_agent: Callable[[str], Agent],
        get_evidence: Callable[[str], Evidence],
        transition_task: Callable[..., Task],
        transition_task_in_transaction: Optional[Callable[..., Task]] = None,
        record_history: Callable[..., None],
        find_verdict_evidence: Optional[Callable[..., Any]] = None,
        reviewer_eligibility_check: Optional[Callable[[Task, Agent], Optional[str]]] = None,
        reviewer_fallback_check: Optional[Callable[[Task, Agent], Optional[str]]] = None,
        completion_proof_check: Optional[Callable[[Task], None]] = None,
        drain_task_transition_outbox: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.store = store
        self.observability = observability
        self.messaging = messaging
        self._get_task = get_task
        self._get_agent = get_agent
        self._get_evidence = get_evidence
        self._transition_task = transition_task
        self._transition_task_in_transaction = transition_task_in_transaction
        self._record_history = record_history
        # mac-5u1f: optional verdict-evidence finder. When provided,
        # submit_review uses it to enforce that an APPROVED status
        # comes with a properly signed review_verdict authored by
        # the reviewer themselves — not just any evidence row.
        self._find_verdict_evidence = find_verdict_evidence
        self._reviewer_eligibility_check = reviewer_eligibility_check
        self._reviewer_fallback_check = reviewer_fallback_check
        self._completion_proof_check = completion_proof_check
        self._drain_task_transition_outbox = drain_task_transition_outbox
        # Compatibility alias for integrations that temporarily disabled the
        # older independence-only hook.  It now points at the complete shared
        # eligibility policy.
        self._reviewer_independence_check = reviewer_eligibility_check

    # Reviews -----------------------------------------------------------

    def request_review(
        self, task_id: str, reviewer_agent_id: str, actor: str = "dispatcher"
    ) -> Review:
        task = self._get_task(task_id)
        reviewer = self._get_agent(reviewer_agent_id)
        fallback_reason = self._ensure_reviewer_eligible(
            task, reviewer, reviewer_agent_id
        )
        if task.state not in {
            TaskState.NEEDS_REVIEW.value,
            TaskState.REVIEWING.value,
        }:
            raise TransitionError("task must need review before requesting review")
        now = utcnow()
        review_id: Optional[str] = None
        created = False
        with self.store.transaction() as conn:
            locked = conn.execute(
                "UPDATE tasks SET updated_at = updated_at WHERE id = ?",
                (task_id,),
            )
            if locked.rowcount != 1:
                raise NotFoundError("task not found: %s" % task_id)
            current = conn.execute(
                "SELECT state FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            current_state = str(current["state"])
            if current_state == TaskState.NEEDS_REVIEW.value:
                if self._transition_task_in_transaction is None:
                    raise TransitionError(
                        "transactional task transition is unavailable"
                    )
                transition_detail = {"reviewer_agent_id": reviewer_agent_id}
                if fallback_reason:
                    transition_detail.update(
                        {
                            "reviewer_independence": "fallback",
                            "reviewer_independence_reason": fallback_reason,
                        }
                    )
                self._transition_task_in_transaction(
                    conn,
                    task_id,
                    TaskState.REVIEWING.value,
                    actor,
                    transition_detail,
                )
            elif current_state != TaskState.REVIEWING.value:
                raise TransitionError("task must need review before requesting review")
            existing = conn.execute(
                """
                SELECT * FROM reviews
                WHERE task_id = ? AND reviewer_agent_id = ? AND status = ?
                ORDER BY created_at, id
                LIMIT 1
                """,
                (task_id, reviewer_agent_id, ReviewStatus.PENDING.value),
            ).fetchone()
            if existing is not None:
                review_id = str(existing["id"])
            else:
                review_id = new_id("review")
                conn.execute(
                    """
                    INSERT INTO reviews (id, task_id, reviewer_agent_id, status, reason, evidence_id, created_at, completed_at)
                    VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL)
                    """,
                    (review_id, task_id, reviewer_agent_id, ReviewStatus.PENDING.value, now),
                )
                history_detail = {
                    "review_id": review_id,
                    "reviewer_agent_id": reviewer_agent_id,
                    "reviewer_independence": (
                        "fallback" if fallback_reason else "independent"
                    ),
                }
                if fallback_reason:
                    history_detail["reviewer_independence_reason"] = fallback_reason
                self._record_history(
                    task_id,
                    "task.review_requested",
                    actor,
                    None,
                    None,
                    history_detail,
                    conn=conn,
                )
                created = True
        if self._drain_task_transition_outbox is not None:
            self._drain_task_transition_outbox(task_id=task_id, limit=20)
        if not created:
            return self.get_review(review_id)
        # Notify the reviewer via the control-channel. Imported here to
        # avoid a tight bidirectional dep; messaging is composed in.
        from mac.models import MessageType

        self.messaging.send_message(
            "dispatcher",
            reviewer_agent_id,
            MessageType.REVIEW_REQUEST.value,
            {
                "task_id": task_id,
                "review_id": review_id,
                "reviewer_independence": (
                    "fallback" if fallback_reason else "independent"
                ),
            },
            task_id=task_id,
        )
        return self.get_review(review_id)

    def _ensure_reviewer_eligible(
        self,
        task: Task,
        reviewer: Agent,
        reviewer_agent_id: Optional[str] = None,
    ) -> Optional[str]:
        reviewer_id = str(
            getattr(reviewer, "id", None) or reviewer_agent_id or ""
        ).strip()
        if not reviewer_id:
            raise AuthorizationError("reviewer agent identity is required")
        if "review" not in set(reviewer.capabilities):
            raise AuthorizationError("reviewer agent requires the review capability")
        fallback_reason = (
            self._reviewer_fallback_check(task, reviewer)
            if self._reviewer_fallback_check is not None
            else None
        )
        if self.agent_has_owned_task(task.id, reviewer_id) and not fallback_reason:
            raise AuthorizationError(
                "reviewer cannot review a task it currently or previously owned"
            )
        if (
            self.latest_executor_evidence_author(task.id) == reviewer_id
            and not fallback_reason
        ):
            raise AuthorizationError("reviewer cannot review its own latest evidence")
        eligibility_check = self._reviewer_independence_check
        if eligibility_check is not None:
            problem = eligibility_check(task, reviewer)
            if problem:
                raise AuthorizationError("reviewer eligibility check failed: %s" % problem)
        return fallback_reason

    def submit_review(
        self,
        review_id: str,
        status: str,
        reviewer_agent_id: str,
        reason: Optional[str] = None,
        evidence_id: Optional[str] = None,
    ) -> Review:
        review = self.get_review(review_id)
        if review.reviewer_agent_id != reviewer_agent_id:
            raise AuthorizationError("reviewer does not own review")
        status_value = _state_value(status)
        if status_value not in TERMINAL_REVIEW_STATUSES:
            raise ValidationError("unsupported review decision: %s" % status_value)
        if review.status != ReviewStatus.PENDING.value:
            if (
                review.status == status_value
                and getattr(review, "reason", None) == reason
                and getattr(review, "evidence_id", None) == evidence_id
            ):
                # The completed row is the durable idempotency receipt. A caller
                # retrying after a lost response gets the original result without
                # re-running evidence or task-state checks that may have changed
                # after the decision committed.
                return review
            raise ValidationError(
                "review is already completed with a different decision"
            )
        if status_value == ReviewStatus.APPROVED.value and evidence_id is None:
            raise ValidationError("approving a review requires an evidence_id")
        if evidence_id is not None:
            evidence = self._get_evidence(evidence_id)
            if evidence.task_id != review.task_id:
                raise ValidationError("review evidence must belong to reviewed task")
            # mac-5u1f: an APPROVED review must point at a real signed
            # review_verdict authored by the reviewer, not at any
            # task-attached evidence (which would let the executor's
            # own evidence be passed off as the verdict). Read the
            # executor_evidence_id off the verdict's own manifest and
            # ask the full verdict finder to verify shape + signature.
            if (
                status_value == ReviewStatus.APPROVED.value
                and self._find_verdict_evidence is not None
            ):
                manifest = (
                    evidence.metadata.get("verification")
                    if isinstance(evidence.metadata, dict)
                    else None
                )
                executor_evidence_id_from_manifest = None
                if isinstance(manifest, dict):
                    executor_evidence_id_from_manifest = str(
                        manifest.get("reviewed_evidence_id") or ""
                    ).strip() or None
                if not executor_evidence_id_from_manifest:
                    raise ValidationError(
                        "review approval evidence %s is not a review_verdict "
                        "(missing verification.reviewed_evidence_id)" % evidence_id
                    )
                current_target = self.current_review_target_evidence_id(review.task_id)
                if not current_target:
                    raise ValidationError(
                        "review approval requires a current executor evidence target"
                    )
                if executor_evidence_id_from_manifest != current_target:
                    raise ValidationError(
                        "review approval targets stale executor evidence: %s != %s"
                        % (executor_evidence_id_from_manifest, current_target)
                    )
                verdict, problems = self._find_verdict_evidence(
                    review.task_id,
                    reviewer_agent_id,
                    executor_evidence_id=executor_evidence_id_from_manifest,
                    verdict_evidence_id=evidence_id,
                    not_before=review.created_at,
                )
                if verdict is None:
                    raise ValidationError(
                        "review approval requires signed review_verdict evidence: %s"
                        % ("; ".join(problems) if problems else "no verdict found")
                    )
                executor_evidence = self._get_evidence(executor_evidence_id_from_manifest)
                executor_manifest = (
                    executor_evidence.metadata.get("verification")
                    if isinstance(executor_evidence.metadata, dict)
                    else None
                )
                llm_problems = cross_llm_review_problems(
                    executor_manifest,
                    manifest,
                    requirements=review_diversity_requirements(
                        self._get_task(review.task_id)
                    ),
                )
                if llm_problems:
                    raise ValidationError(
                        "review approval requires a different reviewer LLM: %s"
                        % "; ".join(llm_problems)
                    )
        reviewed_task = self._get_task(review.task_id)
        reviewer = self._get_agent(reviewer_agent_id)
        self._ensure_reviewer_eligible(
            reviewed_task,
            reviewer,
            reviewer_agent_id,
        )
        rejected_feedback = None
        if status_value in WORK_QUALITY_REVIEW_STATUSES:
            rejected_feedback = self._review_feedback_from_evidence(review, evidence_id)
        # Capture what the reviewer SAID for every terminal verdict, approvals
        # included. `reason` is a caller-chosen template, so the review row
        # otherwise records only which way the vote went: a sample of 52
        # reviews on 2026-08-17 held four distinct reason strings and not one
        # finding, which made "did this reviewer improve the result?"
        # unanswerable from the ledger. An approval that cites nothing is
        # itself the interesting datum, so approvals are recorded too.
        verdict_findings = self._verdict_findings(review, evidence_id, status_value)
        now = utcnow()
        # mac-p5a4: the review status UPDATE and the task.review_completed
        # history row were two bare store.execute calls — a crash between
        # them left the review APPROVED with no audit row. Wrap them in
        # one transaction so either both land or neither does.
        task_for_feedback = reviewed_task if rejected_feedback is not None else None
        transition_target: Optional[str] = None
        transition_detail: Optional[Dict[str, Any]] = None
        harness_failure = status_value == ReviewStatus.INFRASTRUCTURE.value
        if status_value in REOPENING_REVIEW_STATUSES:
            # Observed on task_4ce995cb (2026-08-13): a worker submitted a
            # correct one-line regression test three times; all three reviews
            # rejected with "hub contract verification failed" carrying 588
            # collection errors and, on attempt 2, the sandbox UnicodeEncodeError
            # that PR #352 fixed eleven hours later. attempt_count reached 3/3,
            # the task went terminal, and the post-mortem classifier labelled it
            # "scope" -- whose operator remediation is "decompose", advice that
            # was actively wrong for a one-line change. An equivalent task filed
            # afterwards succeeded unchanged (PR #353).
            #
            # The first fix guessed, after the fact and from free text, whether
            # a rejection had really been a harness failure, then refunded an
            # attempt when the guess said so. This one does not guess: the
            # verdict producer says which axis failed, `infrastructure` arrives
            # here as its own status, and attempt CONSUMPTION -- not
            # attempt_count -- is what the harness axis controls.
            #
            # attempt_count is left exactly as the claim path wrote it: it is
            # the honest number of runs started, and rewriting it backwards made
            # the ledger disagree with the leases it was derived from. What
            # changes is how many of those runs count against max_attempts.
            exhausted = not harness_failure and (
                consumed_attempt_count(
                    reviewed_task.attempt_count, reviewed_task.metadata
                )
                >= reviewed_task.max_attempts
            )
            transition_target = (
                TaskState.BLOCKED.value if exhausted else TaskState.OPEN.value
            )
            transition_detail = {
                "review_id": review_id,
                "review_status": status_value,
                "reason": "review rejected after max attempts"
                if exhausted
                else "review did not approve the work",
                "review_verdict_axis": (
                    "harness" if harness_failure else "work_quality"
                ),
                "attempt_consumed": not harness_failure,
            }
            if harness_failure:
                # An infrastructure outcome records nothing about the work --
                # not a failure class, not feedback, not a judgement.
                transition_detail["reason"] = (
                    "review harness failed; the work was not judged"
                )
            if exhausted:
                transition_detail["manual_repair_required"] = True
        with self.store.transaction() as conn:
            locked_task = conn.execute(
                """
                UPDATE tasks SET updated_at = updated_at
                WHERE id = ? AND state = ?
                """,
                (review.task_id, TaskState.REVIEWING.value),
            )
            if locked_task.rowcount != 1:
                raise TransitionError(
                    "reviewed task state changed during submission; retry"
                )
            changed = conn.execute(
                """
                UPDATE reviews
                SET status = ?, reason = ?, evidence_id = ?, completed_at = ?,
                    findings = ?
                WHERE id = ? AND status = ?
                """,
                (
                    status_value,
                    reason,
                    evidence_id,
                    now,
                    json_dumps(verdict_findings),
                    review_id,
                    ReviewStatus.PENDING.value,
                ),
            )
            if changed.rowcount != 1:
                raise ValidationError("review state changed during submission; retry")
            metadata: Optional[Dict[str, Any]] = None
            if rejected_feedback is not None:
                metadata = dict(task_for_feedback.metadata)
                block = metadata.get("review_feedback") if isinstance(metadata.get("review_feedback"), dict) else {}
                history = list(block.get("history") or [])
                latest = block.get("latest")
                if isinstance(latest, dict):
                    history.insert(0, latest)
                metadata["review_feedback"] = self._bounded_review_feedback_block(
                    rejected_feedback,
                    history,
                )
            if harness_failure:
                # Durably record that this run's review ended on the harness
                # axis, in the same transaction as the review row and the
                # transition. attempt_count is untouched; this is the counter
                # that keeps such a run from being charged against
                # max_attempts.
                metadata = with_infrastructure_attempt(
                    reviewed_task.metadata if metadata is None else metadata
                )
            if metadata is not None:
                conn.execute(
                    "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json_dumps(metadata), now, review.task_id),
                )
            self._record_history(
                review.task_id,
                "task.review_completed",
                reviewer_agent_id,
                None,
                None,
                {
                    "review_id": review_id,
                    "status": status_value,
                    "reason": reason,
                    # Whether the work spent an attempt is a property of the
                    # verdict, stated plainly, rather than something an operator
                    # has to infer from a refund that may or may not have fired.
                    "attempt_consumed": status_value in WORK_QUALITY_REVIEW_STATUSES,
                },
                conn=conn,
            )
            if transition_target is not None:
                if self._transition_task_in_transaction is None:
                    raise TransitionError(
                        "transactional task transition is unavailable"
                    )
                self._transition_task_in_transaction(
                    conn,
                    review.task_id,
                    transition_target,
                    reviewer_agent_id,
                    transition_detail,
                )
        if transition_target is not None:
            if self._drain_task_transition_outbox is not None:
                self._drain_task_transition_outbox(task_id=review.task_id, limit=20)
        return self.get_review(review_id)

    def get_review(self, review_id: str) -> Review:
        row = self.store.query_one("SELECT * FROM reviews WHERE id = ?", (review_id,))
        if row is None:
            raise NotFoundError("review not found: %s" % review_id)
        return self._review_from_row(row)

    def list_reviews(self, task_id: str, limit: Optional[int] = None) -> List[Review]:
        limit_value = None if limit is None else max(0, int(limit))
        if limit_value == 0:
            return []
        if limit_value is None:
            rows = self.store.query_all(
                "SELECT * FROM reviews WHERE task_id = ? ORDER BY created_at, id",
                (task_id,),
            )
        else:
            rows = list(
                reversed(
                    self.store.query_all(
                        """
                        SELECT * FROM reviews
                        WHERE task_id = ?
                        ORDER BY created_at DESC, id DESC
                        LIMIT ?
                        """,
                        (task_id, limit_value),
                    )
                )
            )
        return [self._review_from_row(row) for row in rows]

    # Publication -------------------------------------------------------

    def publish_task(
        self,
        task_id: str,
        target: str,
        created_by: str,
        evidence_id: Optional[str] = None,
    ) -> Publication:
        task = self._get_task(task_id)
        if task.state != TaskState.REVIEWING.value:
            raise TransitionError("task must be in review before publication")
        if not self.completion_authorized(task_id):
            raise ValidationError("publication requires approved review and evidence")
        if self._completion_proof_check is not None:
            self._completion_proof_check(task)
        content_hash = None
        requires_pub_evidence = self.task_requires_publication_evidence(task)
        if requires_pub_evidence and evidence_id is None:
            raise ValidationError("publication policy requires publication evidence")
        if evidence_id is not None:
            evidence = self._get_evidence(evidence_id)
            if evidence.task_id != task_id:
                raise ValidationError("publication evidence must belong to task")
            if requires_pub_evidence:
                if evidence.kind != "publication":
                    raise ValidationError("publication policy requires publication evidence")
                if not evidence.checksum:
                    raise ValidationError("publication evidence requires a checksum")
            # mac-er6u: publication content_hash used to be the worker's
            # opaque checksum string verbatim. Validate the shape so a
            # garbage value (or one that accidentally collides with an
            # executor evidence checksum) can't pass through and
            # masquerade as a tamper-evidence anchor. Format must look
            # like ``<algo>:<digest>`` with algo in {sha256, sha512,
            # blake2b} and hex digest of the expected length.
            content_hash = (evidence.checksum or "").strip()
            if content_hash:
                allowed_formats = {
                    "sha256": 64,
                    "sha512": 128,
                    "blake2b": 128,
                }
                algo, sep, digest = content_hash.partition(":")
                if (
                    not sep
                    or algo.lower() not in allowed_formats
                    or len(digest) != allowed_formats[algo.lower()]
                    or not all(c in "0123456789abcdefABCDEF" for c in digest)
                ):
                    raise ValidationError(
                        "publication evidence checksum must be one of "
                        "sha256/sha512/blake2b in <algo>:<hex> form (got %r)"
                        % content_hash
                    )
        owner_agent_id = task.owner_agent_id
        now = utcnow()
        publication_id = new_id("pub")
        with self.store.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET state = ?, owner_agent_id = NULL, lease_id = NULL, leased_until = NULL,
                    completed_at = COALESCE(completed_at, ?), updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (TaskState.COMPLETED.value, now, now, task_id, TaskState.REVIEWING.value),
            )
            if cursor.rowcount != 1:
                raise TransitionError("task state changed during publish; retry")
            conn.execute(
                """
                INSERT INTO publications (id, task_id, target, status, evidence_id, content_hash, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication_id,
                    task_id,
                    target,
                    PublicationStatus.PUBLISHED.value,
                    evidence_id,
                    content_hash,
                    created_by,
                    now,
                ),
            )
            self._record_history(
                task_id,
                "task.published",
                created_by,
                None,
                None,
                {"publication_id": publication_id, "target": target},
                conn=conn,
            )
            self._record_history(
                task_id,
                "task.transitioned",
                created_by,
                TaskState.REVIEWING.value,
                TaskState.COMPLETED.value,
                {"publication_id": publication_id},
                conn=conn,
            )
            if owner_agent_id:
                conn.execute(
                    "UPDATE agents SET status = ?, current_task_id = NULL, updated_at = ? WHERE id = ?",
                    (AgentStatus.IDLE.value, now, owner_agent_id),
                )
        return self.get_publication(publication_id)

    def get_publication(self, publication_id: str) -> Publication:
        row = self.store.query_one(
            "SELECT * FROM publications WHERE id = ?", (publication_id,)
        )
        if row is None:
            raise NotFoundError("publication not found: %s" % publication_id)
        return self._publication_from_row(row)

    def list_publications(
        self,
        task_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Publication]:
        limit_value = None if limit is None else max(0, int(limit))
        if limit_value == 0:
            return []
        if task_id is not None:
            self._get_task(task_id)
            if limit_value is None:
                rows = self.store.query_all(
                    "SELECT * FROM publications WHERE task_id = ? ORDER BY created_at, id",
                    (task_id,),
                )
            else:
                rows = list(
                    reversed(
                        self.store.query_all(
                            """
                            SELECT * FROM publications
                            WHERE task_id = ?
                            ORDER BY created_at DESC, id DESC
                            LIMIT ?
                            """,
                            (task_id, limit_value),
                        )
                    )
                )
        elif limit_value is None:
            rows = self.store.query_all(
                "SELECT * FROM publications ORDER BY created_at, id"
            )
        else:
            rows = list(
                reversed(
                    self.store.query_all(
                        """
                        SELECT * FROM publications
                        ORDER BY created_at DESC, id DESC
                        LIMIT ?
                        """,
                        (limit_value,),
                    )
                )
            )
        return [self._publication_from_row(row) for row in rows]

    # Authorization helpers --------------------------------------------

    def completion_authorized(self, task_id: str) -> bool:
        """Return true only for approval of the current executor evidence.

        Historical approvals from earlier attempts must not authorize a later
        attempt.  ``transition_task(..., needs_review)`` records the immutable
        executor evidence target in task metadata; the approved verdict must
        name that exact evidence row.
        """
        current_target = self.current_review_target_evidence_id(task_id)
        if not current_target:
            return False
        rows = self.store.query_all(
            """
            SELECT r.id, e.metadata FROM reviews r
            JOIN evidence e ON e.id = r.evidence_id AND e.task_id = r.task_id
            WHERE r.task_id = ? AND r.status = ?
            ORDER BY r.completed_at DESC, r.id DESC
            """,
            (task_id, ReviewStatus.APPROVED.value),
        )
        for row in rows:
            try:
                metadata = json.loads(row["metadata"])
            except (TypeError, ValueError):
                continue
            manifest = metadata.get("verification") if isinstance(metadata, dict) else None
            if not isinstance(manifest, dict):
                continue
            if str(manifest.get("reviewed_evidence_id") or "").strip() == current_target:
                return True
        return False

    def current_review_target_evidence_id(self, task_id: str) -> Optional[str]:
        task = self._get_task(task_id)
        target = task.metadata.get("review_target") if isinstance(task.metadata, dict) else None
        if not isinstance(target, dict):
            return None
        evidence_id = str(target.get("executor_evidence_id") or "").strip()
        return evidence_id or None

    def agent_has_owned_task(self, task_id: str, agent_id: str) -> bool:
        task = self._get_task(task_id)
        if task.owner_agent_id == agent_id:
            return True
        prior = self.store.query_one(
            "SELECT 1 FROM leases WHERE task_id = ? AND agent_id = ? LIMIT 1",
            (task_id, agent_id),
        )
        return prior is not None

    def agent_is_current_owner_or_latest_evidence_author(
        self, task_id: str, agent_id: str
    ) -> bool:
        task = self._get_task(task_id)
        if task.owner_agent_id == agent_id:
            return True
        return self.latest_executor_evidence_author(task_id) == agent_id

    def latest_executor_evidence_author(self, task_id: str) -> Optional[str]:
        rows = self.store.query_all(
            """
            SELECT created_by, metadata FROM evidence
            WHERE task_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (task_id,),
        )
        for row in rows:
            try:
                metadata = json.loads(row["metadata"])
            except (TypeError, ValueError):
                metadata = {}
            verification = metadata.get("verification") if isinstance(metadata, dict) else {}
            if isinstance(verification, dict):
                evidence_type = str(verification.get("evidence_type") or "").strip().lower()
                if evidence_type == "review_verdict":
                    continue
            return str(row["created_by"])
        return None

    def task_requires_publication_evidence(self, task: Task) -> bool:
        policy = task.metadata.get("policy") or {}
        if not isinstance(policy, dict):
            return False
        return bool(
            policy.get("require_publication_evidence")
            or policy.get("publication_evidence_required")
        )

    # Feedback persistence helpers ---------------------------------------

    def _bounded_review_findings(self, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        out: List[Dict[str, Any]] = []
        for item in value[:20]:
            if not isinstance(item, dict):
                continue
            out.append({
                "severity": str(item.get("severity") or "")[:64],
                "path": str(item.get("path") or "")[:512],
                "line": item.get("line") if isinstance(item.get("line"), int) else None,
                "message": str(item.get("message") or "")[:2000],
                "recommendation": str(item.get("recommendation") or "")[:2000],
            })
        return out

    def _bounded_review_feedback_block(self, latest: Dict[str, Any], history: List[Any]) -> Dict[str, Any]:
        block = {"latest": latest, "history": history[:5]}
        encoded = json.dumps(block, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= 24 * 1024:
            return block
        trimmed_latest = dict(latest)
        trimmed_latest["feedback"] = str(trimmed_latest.get("feedback") or "")[:4000] + "\n[truncated]"
        trimmed_latest["summary"] = str(trimmed_latest.get("summary") or "")[:1000]
        trimmed_latest["findings"] = list(trimmed_latest.get("findings") or [])[:5]
        block = {"latest": trimmed_latest, "history": []}
        encoded = json.dumps(block, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= 24 * 1024:
            return block
        trimmed_latest["feedback"] = str(trimmed_latest.get("feedback") or "")[:1000] + "\n[truncated]"
        trimmed_latest["findings"] = []
        return {"latest": trimmed_latest, "history": []}

    def _verdict_findings(
        self,
        review: Review,
        evidence_id: Optional[str],
        status_value: str,
    ) -> Dict[str, Any]:
        """What the reviewer said, distilled onto the review row.

        Deliberately small and comparable across reviews so reviewer value can
        actually be measured: `finding_count` answers "did this review cite
        anything specific?" without re-reading evidence metadata, and
        `cited_specifics` separates a reasoned verdict from a rubber stamp or a
        relayed harness failure.

        Returns ``{}`` when there is no verdict evidence to read, so an absent
        record stays distinguishable from a review that genuinely said nothing.
        """
        if not evidence_id:
            return {}
        try:
            evidence = self._get_evidence(evidence_id)
        except Exception:  # noqa: BLE001 - findings must never block a verdict
            return {}
        manifest = (
            evidence.metadata.get("verification")
            if isinstance(evidence.metadata, dict)
            else None
        )
        if not isinstance(manifest, dict):
            return {}
        items = self._bounded_review_findings(manifest.get("findings"))
        summary = str(manifest.get("summary") or "")[:4000]
        feedback = str(manifest.get("feedback") or "")[:8000]
        record: Dict[str, Any] = {
            "schema": "mac.review_findings.v1",
            "status": status_value,
            "verdict": str(manifest.get("verdict") or ""),
            "verdict_evidence_id": evidence.id,
            "summary": summary,
            "feedback": feedback,
            "findings": items,
            "finding_count": len(items),
            # A verdict that names nothing specific is the case worth counting.
            "cited_specifics": bool(items or summary.strip()),
        }
        # The producer already recorded which axis failed. Read it instead of
        # re-deriving "was this infrastructure?" from the reviewer's prose --
        # that reconstruction is exactly what this design removes.
        axes = manifest.get("review_axes")
        if isinstance(axes, dict):
            record["review_axes"] = axes
        record["is_infrastructure"] = verdict_is_harness_failure(manifest.get("verdict"))
        return record

    def _review_feedback_from_evidence(self, review: Review, evidence_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not evidence_id:
            return None
        evidence = self._get_evidence(evidence_id)
        manifest = evidence.metadata.get("verification") if isinstance(evidence.metadata, dict) else None
        if not isinstance(manifest, dict):
            return None
        return {
            "review_id": review.id,
            "reviewer_agent_id": review.reviewer_agent_id,
            "verdict_evidence_id": evidence.id,
            "reviewed_evidence_id": str(manifest.get("reviewed_evidence_id") or ""),
            "verdict": str(manifest.get("verdict") or ""),
            "summary": str(manifest.get("summary") or "")[:4000],
            "feedback": str(manifest.get("feedback") or "")[:8000],
            "findings": self._bounded_review_findings(manifest.get("findings")),
            "created_at": evidence.created_at,
        }

    # Row hydration ----------------------------------------------------

    def _review_from_row(self, row: Any) -> Review:
        return Review(
            row["id"],
            row["task_id"],
            row["reviewer_agent_id"],
            row["status"],
            row["reason"],
            row["evidence_id"],
            row["created_at"],
            row["completed_at"],
            findings=json_loads(self._row_value(row, "findings"), {}),
        )

    @staticmethod
    def _row_value(row: Any, key: str) -> Any:
        """Read a column that older rows may not carry.

        The migration backfills a default, but a row hydrated from a snapshot
        taken before it ran should degrade to "no findings recorded" rather
        than raise.
        """
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return None

    def _publication_from_row(self, row: Any) -> Publication:
        return Publication(
            row["id"],
            row["task_id"],
            row["target"],
            row["status"],
            row["evidence_id"],
            row["content_hash"],
            row["created_by"],
            row["created_at"],
        )
