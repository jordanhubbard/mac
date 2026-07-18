"""Autonomous task executor (extracted from the deploy heredoc — loop-01).

This is the process the MacWorker spawns per claimed task. It builds a prompt,
runs an authenticated coding-agent CLI inside a mandatory OpenShell sandbox in
the task's git worktree, then derives **honest,
deterministic** evidence from real git state (or, for non-repo work, records the
agent's output as an *unverified* operator_result — never a fabricated pass).

Previously this lived as ~500 lines of Python inside a bash heredoc in
``deploy/deploy-mac-fleet.sh`` — untestable and prone to drift. It now lives
here as an importable, unit-tested module; the deploy writes only a 2-line shim
that calls :func:`main`.

Three capabilities beyond the original:

* **Telemetry path** — every run emits executor-scoped observations
  (``layer="executor"``, ``executor.*``) to the hub so the autonomous loop is
  visible distinctly from the per-command audit trail.
* **Memory feed (deployment gets smarter over time)** — before running, the
  executor *recalls* prior "deployment lessons" for the project and injects
  them into the agent prompt; after running, it *records* a structured
  ``deployment_learning`` memory from the outcome. The nap consolidator
  (mem-08) later promotes those records into the vector tier, so recall
  improves with every task the fleet completes.
* **Automatic task sizing** — before running the agent, the executor inspects
  the task title and description for "plan" signals (conjunctions of verbs,
  numbered steps, multi-phase language, excessive scope).  When signals are
  found the agent receives an explicit instruction to call ``add_child_tasks``
  via the MAC API and write evidence_type=plan_decomposed, which causes the
  parent to block on its children.  A post-run hook (``maybe_auto_decompose``)
  also reads the agent's output for a ``plan_steps`` JSON block and auto-posts
  child tasks when the agent explicitly declares them.

All hub I/O is best-effort and gated on hub env (URL + token): absent those,
the executor still runs and writes evidence — it just doesn't emit telemetry,
recall, or record. The HTTP seam (:func:`_hub_post` / :func:`_hub_get`) and the
agent runner are injectable so the logic is testable without a live hub.

Optional OpenShell sandboxing (sandbox-01): the agent already runs ``--yolo``
(Hermes' own approval prompts bypassed). When ``MAC_OPENSHELL_SANDBOX`` is set,
:func:`_maybe_wrap_openshell` launches that invocation as a confined child of an
OpenShell sandbox, which then enforces *all* guardrails (filesystem, syscall,
and deny-by-default network egress) from a declarative policy. Default OFF —
the wrap is a pure argv transform, so behavior is unchanged unless enabled. See
``docs/openshell-sandbox.md``.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re as _re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from mac import relay_observability
from mac.agent_command import PROMPT_SENTINEL
from mac.models import (
    REPORT_REPOSITORY_ACCESS_SCHEMA,
    REPORT_REPOSITORY_READ_ONLY_MODE,
    metadata_declares_read_only_report_repository,
    metadata_declares_report_deliverable,
)
from mac.repository_access_env import read_only_repository_content_digest
from mac.codegraph_audit import (
    codegraph_audit_check,
    codegraph_audit_manifest_problems,
    codegraph_audit_passed,
    run_codegraph_audit,
)
from mac.fleet_learning import (
    REPOSITORY_ACCESS_RECORD_TYPE,
    parse_repository_access_learning,
    repository_host,
    task_repository_remote,
)
from mac.gitops import (
    CanonicalFreshnessResult,
    check_canonical_freshness,
    guarded_push,
    resolve_canonical_publication_target,
    sync_worktree_with_canonical,
)
from mac.openshell_runtime import (
    SANDBOX_BASE_PATH as _SANDBOX_BASE_PATH,
    openshell_required_for_local_agent as _openshell_required_for_local_agent,
    truthy as _truthy,
)
from mac.env_config import (
    env_bool,
    env_str,
    resolve_env_chain,
)
from mac.review_failure_classifier import (
    FinalizerRefusalKind,
    classify_finalizer_refusal,
)
from mac.repository_recovery import RepositoryRecoveryError

PRESERVED_EXECUTOR_WORKTREE_FILENAME = "preserved-executor-worktree.json"
PRESERVED_EXECUTOR_EVIDENCE_FILENAME = "executor-evidence-preserved.json"
BREAK_GLASS_AUTHORIZATION_SCHEMA = "mac.break_glass_authorization.v1"


class PreservationMissing(RuntimeError):
    """Raised when preserved executor recovery state is absent or unusable."""


@dataclass(frozen=True)
class PreservedExecutorState:
    """Recovery payload saved before a new-file finalizer refusal overwrites evidence."""

    snapshot_path: Path
    evidence_path: Path
    worktree_path: Path
    base_sha: str
    task_branch: str
    untracked_files: List[str]
    staged_new_files: List[str]
    status_porcelain: List[str]
    timestamp: str
    evidence_type: str
    summary: str
    executor_evidence: Dict[str, Any]


def load_preserved_executor_state(workspace: Path) -> PreservedExecutorState:
    """Load the executor state preserved by a new-file finalizer refusal.

    Recovery must fail closed when either artifact is missing; silently returning
    ``None`` would force callers to rediscover whether there is reusable verified
    work.
    """
    snapshot_path = workspace / PRESERVED_EXECUTOR_WORKTREE_FILENAME
    evidence_path = workspace / PRESERVED_EXECUTOR_EVIDENCE_FILENAME
    if not snapshot_path.exists():
        raise PreservationMissing("%s is missing" % snapshot_path.name)
    if not evidence_path.exists():
        raise PreservationMissing("%s is missing" % evidence_path.name)
    try:
        snapshot_raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
        evidence_raw = json.loads(evidence_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise PreservationMissing("preserved executor state is not valid JSON") from exc
    if not isinstance(snapshot_raw, dict):
        raise PreservationMissing("%s must contain a JSON object" % snapshot_path.name)
    if not isinstance(evidence_raw, dict):
        raise PreservationMissing("%s must contain a JSON object" % evidence_path.name)
    return PreservedExecutorState(
        snapshot_path=snapshot_path,
        evidence_path=evidence_path,
        worktree_path=Path(str(snapshot_raw.get("worktree_path") or "")),
        base_sha=str(snapshot_raw.get("base_sha") or ""),
        task_branch=str(snapshot_raw.get("task_branch") or ""),
        untracked_files=_string_list(snapshot_raw.get("untracked_files")),
        staged_new_files=_string_list(snapshot_raw.get("staged_new_files")),
        status_porcelain=_string_list(snapshot_raw.get("status_porcelain")),
        timestamp=str(snapshot_raw.get("timestamp") or ""),
        evidence_type=str(snapshot_raw.get("evidence_type") or ""),
        summary=str(snapshot_raw.get("summary") or ""),
        executor_evidence=evidence_raw,
    )


NEW_FILE_RECOVERY_SCHEMA = "mac.new_file_recovery.v1"


def recover_from_new_file_refusal(
    task_workspace,
    task: Dict[str, Any],
    *,
    git_runner: Optional[Callable[..., Any]] = None,
    push_runner: Optional[Callable[[Any], Any]] = None,
) -> Dict[str, Any]:
    """Recover a task refused solely for leaving new files uncommitted.

    A new-file finalizer refusal preserves the verified worktree and the
    original executor evidence instead of dropping the agent's work. This
    service reconstitutes that work into a publishable commit:

    1. Load the preserved executor state (fails closed when it is unusable).
    2. Confirm — via :func:`classify_finalizer_refusal` on the preserved
       refusal manifest — that the refusal really was a new-file refusal.
    3. ``git add`` every preserved new file (untracked + staged-but-uncommitted).
    4. Commit them with provenance metadata (task id + recovery reason).
    5. Sync with canonical and attempt a guarded push through the existing
       gitops infrastructure.
    6. Return a structured ``mac.new_file_recovery.v1`` result.

    The ``git_runner`` and ``push_runner`` seams are injectable so the logic is
    unit-testable without a live git worktree or remote. ``git_runner`` defaults
    to :func:`_git` and is called as ``git_runner(args, cwd)``; ``push_runner``
    defaults to :func:`guarded_push` and is called as ``push_runner(target)``.

    Raises
    ------
    RepositoryRecoveryError
        When the preserved state is missing/unusable, the refusal was not a
        new-file refusal, no new files can be recovered, or the commit/push
        fails.
    """
    workspace = Path(task_workspace)
    run_git = git_runner if git_runner is not None else _git
    publisher = push_runner if push_runner is not None else guarded_push

    try:
        preserved = load_preserved_executor_state(workspace)
    except PreservationMissing as exc:
        raise RepositoryRecoveryError(
            "preserved executor state is unusable: %s" % exc
        ) from exc

    refusal_manifest = _read_executor_evidence_payload(workspace)
    repo_section = refusal_manifest.get("repo")
    if not isinstance(repo_section, dict):
        repo_section = {}
    checks_section = refusal_manifest.get("checks")
    if not isinstance(checks_section, list):
        checks_section = []
    refusal_kind = classify_finalizer_refusal(refusal_manifest, repo_section, checks_section)
    if refusal_kind not in (
        FinalizerRefusalKind.untracked_new_files,
        FinalizerRefusalKind.staged_new_files,
    ):
        raise RepositoryRecoveryError(
            "preserved refusal is not a new-file refusal: %s" % refusal_kind.value
        )

    worktree = preserved.worktree_path
    if not worktree or not worktree.is_dir():
        raise RepositoryRecoveryError(
            "preserved worktree is missing: %s" % worktree
        )

    new_files = sorted(
        {
            path
            for path in (*preserved.untracked_files, *preserved.staged_new_files)
            if str(path).strip()
        }
    )
    if not new_files:
        raise RepositoryRecoveryError("preserved state lists no new files to recover")

    recovered_files: List[str] = []
    for path in new_files:
        added = run_git(["add", "--", path], worktree)
        if getattr(added, "returncode", 1) != 0:
            raise RepositoryRecoveryError(
                "could not stage preserved new file: %s" % path
            )
        recovered_files.append(path)

    staged = run_git(["diff", "--cached", "--quiet"], worktree)
    if getattr(staged, "returncode", 0) != 1:
        raise RepositoryRecoveryError("recovery produced no staged repository change")

    task_id = str(task.get("id") or preserved.executor_evidence.get("task", {}).get("id") or "").strip()
    if not task_id:
        raise RepositoryRecoveryError("preserved task has no id")
    title = str(task.get("title") or task_id).strip()
    commit = run_git(
        [
            "-c",
            "user.email=mac-recovery@nvidia.com",
            "-c",
            "user.name=MAC recovery",
            "commit",
            "-m",
            "Recover MAC task %s: %s" % (task_id, title[:100]),
            "-m",
            "MAC-Recovery-Reason: new-file-finalizer-refusal\nMAC-Recovery-Kind: %s"
            % refusal_kind.value,
        ],
        worktree,
    )
    if getattr(commit, "returncode", 1) != 0:
        detail = (getattr(commit, "stderr", "") or getattr(commit, "stdout", "") or "").strip()
        raise RepositoryRecoveryError("recovery commit failed: %s" % detail)

    canonical_remote = _repository_publication_remote(task)
    canonical_branch = _repository_contract_canonical_branch(task)
    prepared_base_sha = preserved.base_sha or _repository_prepared_base(task)
    lease_id = _repository_lease_id(task)
    destination_branch = preserved.task_branch or _repository_task_branch(task)

    try:
        sync = sync_worktree_with_canonical(worktree, canonical_remote, canonical_branch)
        if str(sync.get("status") or "") not in {"fresh", "rebased"}:
            raise RepositoryRecoveryError(
                "recovery could not synchronize with canonical branch: %s"
                % (sync.get("reason") or sync.get("status"))
            )
        if not lease_id:
            raise RepositoryRecoveryError(
                "repository context is missing repository_lease_id"
            )
        target = resolve_canonical_publication_target(
            worktree=worktree,
            canonical_remote=canonical_remote,
            canonical_branch=canonical_branch,
            destination_branch=destination_branch,
            prepared_base_sha=prepared_base_sha,
            isolation_key="new-file-recovery-%s-%s" % (task_id, lease_id),
        )
        publication = publisher(target)
    except (OSError, ValueError) as exc:
        raise RepositoryRecoveryError(
            "guarded recovery push could not be prepared: %s" % exc
        ) from exc

    if not getattr(publication, "ok", False) or not getattr(publication, "remote_verified", False):
        raise RepositoryRecoveryError(
            "guarded recovery push failed: %s" % (getattr(publication, "error", "") or "unknown error")
        )

    return {
        "schema": NEW_FILE_RECOVERY_SCHEMA,
        "status": "complete",
        "task_id": task_id,
        "refusal_kind": refusal_kind.value,
        "recovered_files": recovered_files,
        "recovery_head_sha": getattr(publication, "head_sha", ""),
        "canonical_tip_sha": getattr(publication, "canonical_tip_sha", ""),
        "remote_ref": "refs/heads/%s" % destination_branch if destination_branch else "",
        "remote_verified": bool(getattr(publication, "remote_verified", False)),
        "error": None,
    }


# ---------------------------------------------------------------------------
# Small utilities, hub I/O seam, and plan-detection
# (Extracted to mac.executor_hub_io — re-exported here for backward compat)
# ---------------------------------------------------------------------------
from mac.executor_hub_io import (  # noqa: E402,F401 - compatibility re-exports
    utcnow,
    sha256_text,
    command_audit_id,
    redacted_arg,
    audit_safe_argv,
    safe_path_component,
    local_agent_id,
    _hub_env,
    _hub_post,
    _hub_post_json,
    _hub_get,
    _hub_put,
    _hub_post_child_tasks,
    _PLAN_TITLE_KEYWORDS,
    _NUMBERED_STEP_RE,
    _BULLET_RE,
    detect_plan_signals,
    _plan_detection_section,
)
from mac.executor_memory import (  # noqa: E402,F401 - compatibility re-exports
    DEPLOYMENT_LEARNING_PREFIX,
    _LESSON_CURATION_PROMPT,
    _LESSON_PROMPT_BUDGET,
    _LESSON_STOPWORDS,
    _PLAN_LEARNING_SCHEMA,
    _append_lesson_with_budget,
    _format_learning_content,
    _format_plan_learning_content,
    _lesson_terms,
    _plan_family_terms,
    _string_list,
    _structured_lesson_content,
    _task_project,
    build_learning_record,
    build_plan_learning_record,
    build_telemetry_record,
    curate_lessons_from_outcome,
    emit_telemetry,
    recall_deployment_lessons,
    recall_plan_lessons,
    recall_prior_attempt_lessons,
    record_curated_lessons,
    record_deployment_learning,
    record_plan_outcome,
)
from mac.executor_scope import (  # noqa: E402,F401 - compatibility re-exports
    MAC_TASK_SUMMARY_BEGIN,
    MAC_TASK_SUMMARY_END,
    NEW_FILE_COMMIT_RULE,
    _SCOPE_LARGE_DESC_CHARS,
    _SCOPE_LARGE_DESC_WORDS,
    _SCOPE_LARGE_REPO_CMDS,
    _compute_scope_signals,
    _lessons_section,
    _nested_dict,
    build_planning_prompt,
    compute_scope_estimate,
    is_plan_decomposed_evidence,
    is_planning_phase,
    maybe_auto_decompose,
    maybe_preflight_scope_estimate,
    needs_scope_estimate,
    recall_scope_lessons,
    record_scope_estimate,
)
from mac.executor_prompt import (
    _run_captured,
    _blind_review_protocol,
    _read_json_object,
    _repository_contract_canonical_branch,
    _repository_contract_canonical_remote,
    _repository_contract_test_command,
    _repository_bootstrap_timeout,
    _repository_lease_id,
    _repository_prepared_base,
    _repository_publication_remote,
    _repository_task_branch,
    _review_experiment_assignment,
    _run_repository_bootstrap_if_needed,
    clip_process_text,
    run_with_stall_watchdog,
    task_evidence_type,
    task_is_repo_coupled,
)


def _git(args, cwd, *, timeout: Optional[float] = None):
    argv = ["git", *args]
    if timeout is None:
        return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, check=False)
    return _run_captured(argv, Path(cwd), timeout)


def _split_porcelain_status(status_text: str) -> tuple[List[str], List[str], List[str]]:
    """Return ``(tracked_lines, untracked_paths, added_paths)`` from porcelain v1.

    This remains part of the preserved-refusal compatibility surface. New
    executions stage all repository changes at the authoritative host boundary.
    """
    tracked_lines: List[str] = []
    untracked_paths: List[str] = []
    added_paths: List[str] = []
    for line in str(status_text or "").splitlines():
        if not line:
            continue
        if line.startswith("?? "):
            untracked_paths.append(line[3:])
            continue
        tracked_lines.append(line)
        xy = line[:2]
        if ("A" in xy or "C" in xy) and "R" not in xy:
            added_paths.append(line[3:])
    return tracked_lines, untracked_paths, added_paths


def _untracked_finalize_message(untracked_paths: List[str]) -> str:
    return (
        "untracked files present at finalize time — agent must commit ALL new files "
        "before declaring done: %s" % ", ".join(untracked_paths)
    )


def _new_file_finalize_message(paths: List[str]) -> str:
    return (
        "new files staged at finalize time — agent must commit ALL new files "
        "before declaring done: %s" % ", ".join(paths)
    )


def _read_executor_evidence_payload(task_workspace: Path) -> Dict[str, Any]:
    evidence_path = task_workspace / "mac-evidence.json"
    if not evidence_path.exists():
        return {}
    try:
        loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _preserve_executor_state_before_refusal(
    task_workspace: Path,
    task: Dict[str, Any],
    worktree_path: Path,
    *,
    branch: str,
    status_stdout: str,
    untracked_paths: List[str],
    staged_new_paths: List[str],
) -> None:
    executor_evidence = _read_executor_evidence_payload(task_workspace)
    (task_workspace / PRESERVED_EXECUTOR_EVIDENCE_FILENAME).write_text(
        json.dumps(executor_evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    snapshot = {
        "schema": "mac.preserved_executor_worktree.v1",
        "timestamp": utcnow(),
        "worktree_path": str(worktree_path),
        "base_sha": _repository_prepared_base(task),
        "task_branch": _repository_task_branch(task, branch),
        "untracked_files": list(untracked_paths),
        "staged_new_files": list(staged_new_paths),
        "status_porcelain": [line for line in status_stdout.splitlines() if line],
        "evidence_type": str(executor_evidence.get("evidence_type") or ""),
        "summary": str(executor_evidence.get("summary") or ""),
    }
    (task_workspace / PRESERVED_EXECUTOR_WORKTREE_FILENAME).write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_git_finalizer_refusal_manifest(
    task_workspace: Path,
    task: Dict[str, Any],
    worktree_path: Path,
    message: str,
    *,
    status_stdout: str,
    untracked_paths: List[str],
    staged_new_paths: List[str],
) -> None:
    head_sha = _git(["rev-parse", "HEAD"], worktree_path).stdout.strip()
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path).stdout.strip() or "HEAD"
    _preserve_executor_state_before_refusal(
        task_workspace,
        task,
        worktree_path,
        branch=branch,
        status_stdout=status_stdout,
        untracked_paths=untracked_paths,
        staged_new_paths=staged_new_paths,
    )
    # Determine the structured refusal kind from the paths provided so downstream
    # services can read the reason without re-parsing problems[].
    if untracked_paths:
        refusal_kind_value = FinalizerRefusalKind.untracked_new_files.value
    elif staged_new_paths:
        refusal_kind_value = FinalizerRefusalKind.staged_new_files.value
    else:
        refusal_kind_value = FinalizerRefusalKind.untracked_new_files.value
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "fail",
        "evidence_type": "repo_change",
        "finalizer_refusal_kind": refusal_kind_value,
        "preserved_worktree_snapshot": True,
        "summary": message,
        "problems": [message],
        "repo": {
            "head_sha": head_sha,
            "base_sha": _repository_prepared_base(task),
            "pushed": False,
            "remote_ref": "refs/heads/" + branch if branch != "HEAD" else "",
            "dirty": True,
            "files_changed": [],
            "status_porcelain": [line for line in status_stdout.splitlines() if line],
            "untracked_files": untracked_paths,
            "staged_new_files": staged_new_paths,
        },
        "tests": None,
        "push": {
            "returncode": 1,
            "status": "skipped",
            "reason": message,
        },
        "checks": [
            {
                "name": "git_finalizer",
                "returncode": 1,
                "status": "fail",
                "stderr": message,
            }
        ],
    }
    (task_workspace / "mac-evidence.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _load_harness_recovery_log(task_workspace: Path) -> List[Dict[str, Any]]:
    """Read harness-recovery-log.json from task_workspace if present.

    Returns a list of recovery step records [{step, choice, result}, ...].
    Returns an empty list when the file is absent, empty, or unparseable.
    """
    log_path = task_workspace / "harness-recovery-log.json"
    if not log_path.exists():
        return []
    try:
        raw = json.loads(log_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def _record_recovery_learnings(
    task_workspace: Path,
    task: Dict[str, Any],
    outcome: Dict[str, Any],
) -> None:
    """Feed each harness recovery choice+outcome into the deployment-learning loop.

    Reads harness-recovery-log.json and posts one learning record per entry so
    the fleet's selection algorithm can improve future recovery choices.
    Best-effort: silently returns on any error.
    """
    recovery_log = _load_harness_recovery_log(task_workspace)
    if not recovery_log:
        return
    for entry in recovery_log:
        if not isinstance(entry, dict):
            continue
        recovery_outcome = {
            "evidence_type": outcome.get("evidence_type", "recovery"),
            "outcome": outcome.get("outcome", "unknown"),
            "signals": dict(outcome.get("signals") or {}),
            "error_signature": outcome.get("error_signature") or "",
            "recovery_step": entry.get("step"),
            "recovery_choice": entry.get("choice"),
            "recovery_result": entry.get("result"),
        }
        try:
            record_deployment_learning(task, recovery_outcome)
        except Exception:  # noqa: BLE001
            pass


_FINALIZER_PHASE_DEFAULTS: Dict[str, float] = {
    "repository_snapshot": 60.0,
    "canonical_sync": 300.0,
    "cleanup": 120.0,
    "bootstrap": 1800.0,
    "contract_tests": 1800.0,
    "publication_preflight": 180.0,
    "codegraph_audit": 300.0,
    "guarded_push": 180.0,
    "evidence_writeback": 60.0,
    "lesson_curation": 60.0,
}


def _finalizer_phase_timeout(phase: str) -> float:
    env_key = "MAC_FINALIZER_%s_TIMEOUT" % phase.upper().replace("-", "_")
    raw = env_str(env_key)
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    if phase == "bootstrap":
        return _repository_bootstrap_timeout()
    if phase == "contract_tests":
        try:
            value = float(env_str("MAC_WORKER_REPOSITORY_TEST_TIMEOUT") or 1800.0)
            return value if value > 0 else 1800.0
        except ValueError:
            return 1800.0
    return _FINALIZER_PHASE_DEFAULTS.get(phase, 600.0)


class _FinalizerPhaseContext:
    """Persist and emit one bounded finalizer phase's lifecycle.

    The context supplies the budget to the operation it encloses; subprocess
    boundaries must use ``timeout`` so the limit is enforced where the process
    tree can be terminated safely.  ``finalizer-progress.json`` is written at
    phase start as well as completion, leaving a durable active-phase marker if
    the executor itself is killed.
    """

    def __init__(
        self,
        task_workspace: Path,
        task_id: Optional[str],
        phase: str,
        *,
        partial_evidence_fn: Optional[Callable[..., None]] = None,
    ) -> None:
        self.task_workspace = task_workspace
        self.task_id = task_id
        self.phase = phase
        self.timeout = _finalizer_phase_timeout(phase)
        self.partial_evidence_fn = partial_evidence_fn
        self.started_at = ""
        self._started = 0.0
        self.deadline = 0.0
        self._status = "pass"
        self._reason = ""

    @property
    def remaining(self) -> float:
        return max(0.001, self.deadline - time.monotonic())

    def _write_progress(self, status: str, **extra: Any) -> None:
        payload = {
            "schema": "mac.finalizer_progress.v1",
            "task_id": self.task_id,
            "phase": self.phase,
            "status": status,
            "started_at": self.started_at,
            "budget_seconds": self.timeout,
            **extra,
        }
        path = self.task_workspace / "finalizer-progress.json"
        temporary = self.task_workspace / "finalizer-progress.json.tmp"
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(path)

    def mark_failed(self, reason: str = "") -> None:
        self._status = "fail"
        self._reason = str(reason or "")

    def __enter__(self) -> "_FinalizerPhaseContext":
        self._started = time.monotonic()
        self.deadline = self._started + self.timeout
        self.started_at = utcnow()
        self._write_progress(
            "running",
            deadline_monotonic=self.deadline,
        )
        emit_telemetry(
            "finalizer_phase_started",
            task_id=self.task_id,
            phase=self.phase,
            budget_seconds=self.timeout,
        )
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, _traceback: Any) -> bool:
        elapsed_ms = (time.monotonic() - self._started) * 1000.0
        timed_out = bool(
            exc_type is not None
            and issubclass(exc_type, (TimeoutError, subprocess.TimeoutExpired))
        )
        cancelled = bool(
            exc_type is not None and issubclass(exc_type, (KeyboardInterrupt, SystemExit))
        )
        if timed_out:
            status = "timeout"
        elif cancelled:
            status = "cancelled"
        elif exc_type is not None:
            status = "fail"
        else:
            status = self._status
        reason = self._reason or (str(exc_value) if exc_value is not None else "")
        self._write_progress(
            status,
            completed_at=utcnow(),
            elapsed_ms=elapsed_ms,
            reason=reason,
        )
        event = (
            "finalizer_phase_timeout"
            if timed_out
            else "finalizer_phase_cancelled" if cancelled else "finalizer_phase_completed"
        )
        emit_telemetry(
            event,
            task_id=self.task_id,
            level="warning" if status != "pass" else "info",
            phase=self.phase,
            status=status,
            budget_seconds=self.timeout,
            elapsed_ms=elapsed_ms,
            reason=reason,
        )
        if (timed_out or cancelled or exc_type is not None) and self.partial_evidence_fn:
            try:
                self.partial_evidence_fn(phase=self.phase, reason=status)
            except Exception:  # noqa: BLE001 - retain the original exception.
                pass
        return False


def _write_partial_finalizer_evidence(
    task_workspace: Path,
    task: Dict[str, Any],
    *,
    phase: str,
    reason: str,
    head_sha: str = "",
    base_sha: str = "",
    branch: str = "",
    files_changed: Optional[List[str]] = None,
    bootstrap: Optional[Dict[str, Any]] = None,
    tests: Optional[Dict[str, Any]] = None,
    codegraph: Optional[Dict[str, Any]] = None,
) -> None:
    manifest: Dict[str, Any] = {
        "schema": "mac.worker_evidence.v1",
        "status": "fail",
        "partial": True,
        "evidence_type": "repo_change",
        "summary": "Deterministic finalizer interrupted during %s: %s" % (phase, reason),
        "finalizer_interrupted": {"phase": phase, "reason": reason},
        "repo": {
            "head_sha": head_sha,
            "base_sha": base_sha,
            "pushed": False,
            "remote_ref": "refs/heads/" + branch if branch and branch != "HEAD" else "",
            "dirty": True,
            "files_changed": list(files_changed or []),
        },
        "tests": [tests] if tests is not None else None,
        "push": {
            "returncode": 124 if reason == "timeout" else 1,
            "status": "skipped",
            "reason": "finalizer interrupted during %s: %s" % (phase, reason),
        },
        "checks": [
            {
                "name": "git_finalizer",
                "returncode": 124 if reason == "timeout" else 1,
                "status": "fail",
            }
        ],
    }
    if bootstrap is not None:
        manifest["bootstrap"] = bootstrap
    if codegraph is not None:
        manifest["codegraph"] = codegraph
    (task_workspace / "mac-evidence.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def run_deterministic_git_finalizer(task_workspace: Path, task: Dict[str, Any]) -> None:
    """mac-jfns: deterministic repo_change evidence from REAL git state for
    tasks declaring publication_target=git://main."""
    metadata = task.get("metadata") or {}
    publication_target = str(metadata.get("publication_target") or "").strip()
    if not publication_target.startswith("git://"):
        return
    worktree = env_str("MAC_TASK_REPO_WORKTREE")
    if not worktree:
        rt = metadata.get("runtime") if isinstance(metadata.get("runtime"), dict) else {}
        worktree = str(rt.get("repository_worktree") or "").strip()
    worktree_path = Path(worktree).expanduser() if worktree else None
    if not worktree_path or not worktree_path.is_dir() or not (worktree_path / ".git").exists():
        return
    task_id = str(task.get("id") or "") or None
    emit_telemetry("finalizer_started", task_id=task_id)
    progress: Dict[str, Any] = {
        "head_sha": "",
        "base_sha": _repository_prepared_base(task),
        "branch": "",
        "files_changed": [],
        "bootstrap": None,
        "tests": None,
        "codegraph": None,
    }

    def _partial_evidence(*, phase: str, reason: str) -> None:
        _write_partial_finalizer_evidence(
            task_workspace,
            task,
            phase=phase,
            reason=reason,
            head_sha=str(progress["head_sha"]),
            base_sha=str(progress["base_sha"]),
            branch=str(progress["branch"]),
            files_changed=list(progress["files_changed"]),
            bootstrap=progress["bootstrap"],
            tests=progress["tests"],
            codegraph=progress["codegraph"],
        )

    with _FinalizerPhaseContext(
        task_workspace,
        task_id,
        "repository_snapshot",
        partial_evidence_fn=_partial_evidence,
    ) as phase:
        status = _git(
            ["status", "--porcelain"], worktree_path, timeout=phase.remaining
        )
        tracked_lines, untracked_paths, staged_new_paths = _split_porcelain_status(
            status.stdout
        )
        if tracked_lines or untracked_paths or staged_new_paths:
            add = _git(["add", "-A"], worktree_path, timeout=phase.remaining)
            if add.returncode != 0:
                phase.mark_failed(clip_process_text(add.stderr or add.stdout))
            commit_msg = "auto-commit: %s" % task.get("id", "unknown")
            commit = _git(
                [
                    "-c",
                    "user.email=mac-fleet@nvidia.com",
                    "-c",
                    "user.name=MAC fleet",
                    "commit",
                    "-m",
                    commit_msg,
                ],
                worktree_path,
                timeout=phase.remaining,
            )
            if commit.returncode != 0:
                phase.mark_failed(clip_process_text(commit.stderr or commit.stdout))
        head_sha = _git(
            ["rev-parse", "HEAD"], worktree_path, timeout=phase.remaining
        ).stdout.strip()
        branch = (
            _git(
                ["rev-parse", "--abbrev-ref", "HEAD"],
                worktree_path,
                timeout=phase.remaining,
            ).stdout.strip()
            or "HEAD"
        )
        progress["head_sha"] = head_sha
        progress["branch"] = branch
    # Rebase onto the advanced canonical tip BEFORE the contract test runs, so
    # the suite validates the projected published tree. Fleet agents race each
    # other to one canonical branch; a task that took an hour almost always
    # finds main moved, and without this it dies at the publication freshness
    # gate after all its work passed. Clean rebases only — a conflict aborts
    # and the existing gate reports its precise error.
    with _FinalizerPhaseContext(
        task_workspace,
        task_id,
        "canonical_sync",
        partial_evidence_fn=_partial_evidence,
    ) as phase:
        canonical_sync = sync_worktree_with_canonical(
            worktree_path,
            _repository_publication_remote(task),
            _repository_contract_canonical_branch(task),
            timeout=phase.remaining,
        )
        if canonical_sync.get("status") == "rebased":
            head_sha = _git(
                ["rev-parse", "HEAD"], worktree_path, timeout=phase.remaining
            ).stdout.strip()
            progress["head_sha"] = head_sha
        if canonical_sync.get("status") not in {"fresh", "rebased"}:
            phase.mark_failed(str(canonical_sync.get("reason") or canonical_sync.get("status")))
    # Purge synced build artifacts before the host build. The agent built in the
    # task SANDBOX (e.g. Linux); those object files / binaries sync back into this
    # worktree, but this finalizer runs on the EXECUTOR HOST, which may be a
    # different OS/arch (a macOS host with a Linux sandbox). A stale foreign
    # bin/nano makes `..._if_needed` skip the rebuild and then the tests run a
    # binary the host can't execute -> spurious "tests failed". `git clean -Xdf`
    # removes only gitignored files (obj/, bin/, caches) and keeps the agent's
    # new untracked SOURCE files, forcing a clean native rebuild.
    with _FinalizerPhaseContext(
        task_workspace,
        task_id,
        "cleanup",
        partial_evidence_fn=_partial_evidence,
    ) as phase:
        cleanup = _git(["clean", "-Xdf"], worktree_path, timeout=phase.remaining)
        if cleanup.returncode != 0:
            phase.mark_failed(clip_process_text(cleanup.stderr or cleanup.stdout))
    with _FinalizerPhaseContext(
        task_workspace,
        task_id,
        "bootstrap",
        partial_evidence_fn=_partial_evidence,
    ) as phase:
        bootstrap = _run_repository_bootstrap_if_needed(
            worktree_path, task, timeout=phase.remaining
        )
        progress["bootstrap"] = bootstrap
        if bootstrap is not None and bootstrap.get("returncode") != 0:
            phase.mark_failed(str(bootstrap.get("error") or bootstrap.get("status")))
    test_cmd = (_repository_contract_test_command(task) or "scripts/run-contract-tests.sh").strip()
    tests = None
    if test_cmd:
        with _FinalizerPhaseContext(
            task_workspace,
            task_id,
            "contract_tests",
            partial_evidence_fn=_partial_evidence,
        ) as phase:
            # Progress watchdog plus a phase hard ceiling; both terminate the
            # complete verifier process group.
            tr = run_with_stall_watchdog(
                ["bash", "-lc", test_cmd],
                worktree_path,
                hard_timeout=phase.remaining,
            )
            tail = (tr.stdout or "") + "\n" + (tr.stderr or "")
            import re as _re

            passed = failed = total = None
            m = _re.search(r"(\d+) passed", tail)
            if m:
                passed = int(m.group(1))
            m = _re.search(r"(\d+) failed", tail)
            if m:
                failed = int(m.group(1))
            if passed is not None or failed is not None:
                total = (passed or 0) + (failed or 0)
            tests = {
                "command": test_cmd,
                "returncode": int(tr.returncode),
                "passed": passed,
                "failed": failed,
                "total": total,
                "status": "pass" if tr.returncode == 0 else "fail",
            }
            progress["tests"] = tests
            if tr.returncode != 0:
                phase.mark_failed(clip_process_text(tr.stderr or tr.stdout))
    bootstrap_ok = bootstrap is None or bootstrap.get("returncode") == 0
    tests_ok = tests is None or tests.get("returncode") == 0
    canonical_remote_raw = _repository_publication_remote(task)
    canonical_branch = _repository_contract_canonical_branch(task)
    prepared_base_sha = _repository_prepared_base(task)
    lease_id = _repository_lease_id(task)
    destination_branch = branch if branch != "HEAD" else ""
    publication_target = None
    with _FinalizerPhaseContext(
        task_workspace,
        task_id,
        "publication_preflight",
        partial_evidence_fn=_partial_evidence,
    ) as phase:
        try:
            if not lease_id:
                raise ValueError("repository context is missing repository_lease_id")
            publication_target = resolve_canonical_publication_target(
                worktree=worktree_path,
                canonical_remote=canonical_remote_raw,
                canonical_branch=canonical_branch,
                destination_branch=destination_branch,
                prepared_base_sha=prepared_base_sha,
                isolation_key="%s-%s" % (str(task.get("id") or "task"), lease_id),
                timeout=phase.remaining,
            )
            freshness = check_canonical_freshness(
                publication_target, timeout=phase.remaining
            )
        except (OSError, ValueError) as exc:
            freshness = CanonicalFreshnessResult(
                False,
                publication_target,
                head_sha=head_sha,
                error=str(exc),
            )
        if not freshness.ok:
            phase.mark_failed(freshness.error)
        freshness_error: Optional[str] = None if freshness.ok else freshness.error
        base_sha = freshness.canonical_tip_sha or prepared_base_sha
        files_changed = list(freshness.files_changed)
        progress["base_sha"] = base_sha
        progress["files_changed"] = files_changed
    # Record the diff base (canonical tip) so the reviewer can compute a
    # non-empty base..head diff. Without base_sha the review snapshot's
    # files_changed is always [] (which the repo_change validator rejects).
    with _FinalizerPhaseContext(
        task_workspace,
        task_id,
        "codegraph_audit",
        partial_evidence_fn=_partial_evidence,
    ) as phase:
        codegraph = run_codegraph_audit(
            worktree_path, files_changed, timeout=phase.remaining
        )
        progress["codegraph"] = codegraph
        if str(codegraph.get("status") or "") not in {"pass", "skipped"}:
            phase.mark_failed(str(codegraph.get("reason") or "codegraph audit failed"))
    codegraph_problems = codegraph_audit_manifest_problems(
        {"repo": {"files_changed": files_changed}, "codegraph": codegraph}
    )
    codegraph_ok = not codegraph_problems
    final_status = _git(
        ["status", "--porcelain"],
        worktree_path,
        timeout=_finalizer_phase_timeout("publication_preflight"),
    ).stdout.strip()
    clean = not bool(final_status)
    freshness_ok = freshness_error is None
    pushed = False
    publication: Optional[CanonicalFreshnessResult] = None
    push_remote_display = (
        freshness.target.remote_display if freshness.target is not None else ""
    )
    if bootstrap_ok and tests_ok and codegraph_ok and clean and freshness_ok:
        assert publication_target is not None
        with _FinalizerPhaseContext(
            task_workspace,
            task_id,
            "guarded_push",
            partial_evidence_fn=_partial_evidence,
        ) as phase:
            publication = guarded_push(publication_target, timeout=phase.remaining)
            if not publication.ok or not publication.remote_verified:
                phase.mark_failed(publication.error)
        push_remote_display = (
            publication.target.remote_display
            if publication.target is not None
            else push_remote_display
        )
        pushed = publication.ok and publication.remote_verified
        if publication.canonical_tip_sha:
            base_sha = publication.canonical_tip_sha
        if not publication.ok:
            freshness_error = publication.error
            freshness_ok = False
        push_evidence = {
            "remote": push_remote_display,
            "returncode": int(publication.push_returncode or (0 if pushed else 1)),
            "status": "pass" if pushed else "fail",
            "stderr": clip_process_text(publication.push_stderr or publication.error),
        }
    elif not clean:
        push_evidence = {
            "remote": push_remote_display,
            "returncode": 1,
            "status": "skipped",
            "reason": "worktree dirty after bootstrap/tests",
        }
    elif not codegraph_ok:
        push_evidence = {
            "remote": push_remote_display,
            "returncode": 1,
            "status": "skipped",
            "reason": "codegraph audit failed",
            "problems": codegraph_problems,
        }
    elif not freshness_ok:
        push_evidence = {
            "remote": push_remote_display,
            "returncode": 1,
            "status": "skipped",
            "reason": "canonical freshness check failed",
            "freshness_error": freshness_error,
        }
    else:
        push_evidence = {
            "remote": push_remote_display,
            "returncode": 1,
            "status": "skipped",
            "reason": "bootstrap/tests failed",
        }
    all_ok = pushed and bootstrap_ok and tests_ok and codegraph_ok and clean and freshness_ok
    integration_target = publication.target if publication is not None else publication_target
    integrated_on_canonical = bool(
        all_ok
        and integration_target is not None
        and integration_target.destination_branch == integration_target.canonical_branch
    )
    canonical_integration = {
        "schema": "mac.canonical_integration.v1",
        "status": "pass" if integrated_on_canonical else "fail",
        "canonical_ref": (
            "refs/heads/%s" % integration_target.canonical_branch
            if integration_target is not None
            else ""
        ),
        # guarded_push verifies the destination ref after push.  When that
        # destination is canonical, its verified SHA is the durable completion
        # proof rather than the pre-push freshness tip.
        "canonical_tip_sha": head_sha if integrated_on_canonical else "",
        "head_sha": head_sha,
        "remote_verified": bool(
            integrated_on_canonical
            and publication is not None
            and publication.remote_verified
        ),
    }
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "summary": "Deterministic finalizer: commit+test+push for %s" % task.get("id"),
        "repo": {
            "head_sha": head_sha,
            "base_sha": base_sha,
            "pushed": pushed,
            "remote_ref": "refs/heads/" + branch if branch != "HEAD" else "",
            "push_remote": push_remote_display,
            "dirty": bool(final_status),
            "files_changed": files_changed,
            "freshness": (publication or freshness).evidence(),
            "canonical_sync": canonical_sync,
        },
        "canonical_integration": canonical_integration,
        "codegraph": codegraph,
        # mac-wjy3: verification.tests is the CANONICAL list of test-result
        # objects. The strict evidence validator rejects a bare dict (treats it
        # as tests:null/missing), so a require_tests task whose finalizer ran the
        # suite once must still present a one-element LIST, not the raw dict.
        "tests": [tests] if tests is not None else None,
        "push": push_evidence,
        "checks": (
            ([codegraph_audit_check(codegraph)] if str(codegraph.get("status") or "") != "skipped" else [])
            + [
            {
                "name": "git_finalizer",
                "returncode": 0 if all_ok else 1,
                "status": "pass" if all_ok else "fail",
            }
        ]),
    }
    if freshness_error is not None:
        manifest["freshness_error"] = freshness_error
    if bootstrap is not None:
        manifest["bootstrap"] = bootstrap
    recovery_log = _load_harness_recovery_log(task_workspace)
    if recovery_log:
        manifest["recovery"] = recovery_log
    with _FinalizerPhaseContext(
        task_workspace,
        task_id,
        "evidence_writeback",
        partial_evidence_fn=_partial_evidence,
    ) as phase:
        (task_workspace / "mac-evidence.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if not all_ok:
            phase.mark_failed(
                str(
                    push_evidence.get("reason")
                    or push_evidence.get("stderr")
                    or "finalizer failed"
                )
            )
    emit_telemetry(
        "finalizer_completed",
        task_id=task_id,
        level="info" if all_ok else "warning",
        all_ok=all_ok,
        pushed=pushed,
        head_sha=head_sha,
    )


def _sign_verdict(key: str, manifest: Dict[str, Any]) -> str:
    """HMAC-SHA256 → base64url; matches mac.services.sign_verification_manifest."""
    import base64 as _base64
    import hashlib as _hashlib
    import hmac as _hmac

    filtered = {k: v for k, v in manifest.items() if k != "signature"}
    blob = json.dumps(filtered, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = _hmac.new(key.encode("ascii"), blob, _hashlib.sha256).digest()
    return "v1:" + _base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _cooperative_integration_check(
    task: Dict[str, Any], worktree: Path
) -> Optional[Dict[str, Any]]:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    coordination = metadata.get("coordination") if isinstance(metadata, dict) else {}
    if not isinstance(coordination, dict) or coordination.get("phase") != "integration":
        return None
    outputs = coordination.get("child_outputs")
    required = []
    problems: List[str] = []
    for output in outputs if isinstance(outputs, list) else []:
        if not isinstance(output, dict):
            problems.append("cooperative integration contains a malformed child output")
            continue
        repo = output.get("repo") if isinstance(output.get("repo"), dict) else {}
        head_sha = str(repo.get("head_sha") or "").strip()
        evidence_id = str(output.get("executor_evidence_id") or "").strip()
        task_id = str(output.get("task_id") or "unknown").strip()
        status = str(output.get("status") or "").strip()
        if status != "ready" or not head_sha or not evidence_id:
            problems.append(
                "cooperative child %s has no verifiable completed output" % task_id
            )
            continue
        required.append((evidence_id, head_sha))
    verified: List[str] = []
    if not required:
        problems.append("cooperative integration has no verifiable child commit inputs")
    for evidence_id, head_sha in required:
        exists = _git(["cat-file", "-e", "%s^{commit}" % head_sha], worktree)
        if exists.returncode != 0:
            problems.append("child evidence %s commit %s is missing" % (evidence_id, head_sha))
            continue
        ancestor = _git(["merge-base", "--is-ancestor", head_sha, "HEAD"], worktree)
        if ancestor.returncode != 0:
            problems.append(
                "child evidence %s commit %s is not an ancestor of the integrated HEAD"
                % (evidence_id, head_sha)
            )
            continue
        verified.append(evidence_id)
    return {
        "status": "pass" if not problems else "fail",
        "required_child_evidence_ids": [item[0] for item in required],
        "verified_child_evidence_ids": verified,
        "problems": problems,
    }


def run_deterministic_review_verdict(task_workspace: Path, task: Dict[str, Any], review_context: Dict[str, Any]) -> None:
    """Finalize a semantic review with deterministic independent checks.

    The review agent owns the semantic verdict.  Deterministic checks may veto
    an approval, but they must never turn a semantic rejection into an
    approval.  This distinction is important for defects that are not captured
    by the repository's test suite (design errors, unsafe behavior, incomplete
    requirements, and similar review findings).
    """
    review_claim = review_context.get("review_claim")
    if not isinstance(review_claim, dict):
        review_claim = {}
    reviewer_agent_id = str(
        task.get("owner_agent_id")
        or review_context.get("reviewer_agent_id")
        or review_claim.get("reviewer_agent_id")
        or resolve_env_chain("MAC_WORKER_AGENT_ID", "MAC_AGENT_ID")
        or ""
    ).strip()
    attestation_key = env_str("MAC_ATTESTATION_KEY")
    if not reviewer_agent_id or not attestation_key:
        return
    executor_evidence_id = str(review_context.get("executor_evidence_id") or "").strip()
    review_id = str(review_context.get("review_id") or "").strip()
    if not executor_evidence_id or not review_id:
        return
    manifest_path = task_workspace / "mac-evidence.json"
    semantic_manifest: Dict[str, Any] = {}
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                semantic_manifest = loaded
        except Exception:
            semantic_manifest = {}

    semantic_verdict = str(semantic_manifest.get("verdict") or "").strip().lower()
    semantic_valid = (
        str(semantic_manifest.get("schema") or "").strip() == "mac.worker_evidence.v1"
        and str(semantic_manifest.get("status") or "").strip().lower() == "complete"
        and str(semantic_manifest.get("evidence_type") or "").strip().lower()
        == "review_verdict"
        and semantic_verdict in {"approved", "rejected"}
    )

    exec_ev_path = task_workspace / "executor-evidence.json"
    exec_ev: Dict[str, Any] = {}
    if exec_ev_path.exists():
        try:
            loaded = json.loads(exec_ev_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                exec_ev = loaded
        except Exception:
            exec_ev = {}
    exec_verification = (exec_ev.get("metadata") or {}).get("verification") or {}
    exec_repo = exec_verification.get("repo") or {}
    exec_access = exec_verification.get("repository_access") or {}
    exec_head = str(exec_repo.get("head_sha") or "").strip()
    repo_review = bool(exec_head)
    read_only_report_review = (
        metadata_declares_read_only_report_repository(task.get("metadata"))
        and isinstance(exec_access, dict)
        and exec_access.get("schema") == REPORT_REPOSITORY_ACCESS_SCHEMA
        and exec_access.get("mode") == REPORT_REPOSITORY_READ_ONLY_MODE
        and not repo_review
    )
    review_worktree = env_str("MAC_TASK_REPO_WORKTREE")
    tests = None
    bootstrap = None
    codegraph = None
    integration = None
    # Non-repository work has no checkout/test contract.  Its independent
    # check is the semantic review itself.  Repository work must additionally
    # prove the exact executor commit exists in the prepared review checkout
    # and pass bootstrap, tests, and CodeGraph.
    independent_pass = semantic_valid and not repo_review
    independent_problem = ""
    if read_only_report_review:
        independent_pass = False
        if review_worktree and Path(review_worktree).is_dir():
            review_worktree_path = Path(review_worktree)
            expected_head = str(exec_access.get("base_sha") or "").strip()
            expected_tree = str(exec_access.get("base_tree") or "").strip()
            expected_refs = str(exec_access.get("refs_digest") or "").strip()
            expected_content = str(exec_access.get("content_digest") or "").strip()
            status = _git(["status", "--porcelain"], review_worktree_path)
            checked_out = _git(["rev-parse", "HEAD"], review_worktree_path)
            tree = _git(["rev-parse", "HEAD^{tree}"], review_worktree_path)
            refs = _git(
                ["for-each-ref", "--format=%(refname) %(objectname)"],
                review_worktree_path,
            )
            remotes = _git(["remote"], review_worktree_path)
            observed_refs = (
                hashlib.sha256(refs.stdout.encode("utf-8")).hexdigest()
                if refs.returncode == 0
                else ""
            )
            invariant_ok = (
                status.returncode == 0
                and not status.stdout.strip()
                and checked_out.returncode == 0
                and checked_out.stdout.strip() == expected_head
                and tree.returncode == 0
                and tree.stdout.strip() == expected_tree
                and refs.returncode == 0
                and observed_refs == expected_refs
                and remotes.returncode == 0
                and not remotes.stdout.strip()
                and all((expected_head, expected_tree, expected_refs, expected_content))
            )
            if invariant_ok:
                cleaned = _git(
                    ["clean", "-fdx", "-e", ".codegraph/"],
                    review_worktree_path,
                )
                try:
                    observed_content = read_only_repository_content_digest(
                        review_worktree_path
                    )
                except OSError:
                    observed_content = ""
                invariant_ok = (
                    cleaned.returncode == 0
                    and observed_content == expected_content
                )
            independent_pass = semantic_valid and invariant_ok
            if not independent_pass:
                independent_problem = (
                    "independent read-only review checkout did not match the "
                    "executor exact-base proof"
                )
        else:
            independent_problem = "exact read-only review checkout is unavailable"
    elif repo_review and review_worktree and Path(review_worktree).is_dir():
        review_worktree_path = Path(review_worktree)
        ck = _git(["cat-file", "-e", "%s^{commit}" % exec_head], review_worktree_path)
        checked_out = _git(["rev-parse", "HEAD"], review_worktree_path)
        checked_out_head = checked_out.stdout.strip() if checked_out.returncode == 0 else ""
        if ck.returncode == 0 and checked_out_head == exec_head:
            bootstrap = _run_repository_bootstrap_if_needed(review_worktree_path, task)
            test_cmd = (_repository_contract_test_command(task) or "scripts/run-contract-tests.sh").strip()
            tr = run_with_stall_watchdog(["bash", "-lc", test_cmd], review_worktree_path)
            bootstrap_ok = bootstrap is None or bootstrap.get("returncode") == 0
            codegraph = run_codegraph_audit(review_worktree_path, exec_repo.get("files_changed") or [])
            integration = _cooperative_integration_check(task, review_worktree_path)
            integration_ok = integration is None or integration.get("status") == "pass"
            independent_pass = (
                bootstrap_ok
                and tr.returncode == 0
                and codegraph_audit_passed(codegraph)
                and integration_ok
            )
            tests = {
                "command": test_cmd,
                "returncode": int(tr.returncode),
                "status": "pass" if tr.returncode == 0 else "fail",
            }
            if not independent_pass:
                independent_problem = "independent bootstrap, tests, or CodeGraph failed"
        elif ck.returncode != 0:
            independent_problem = "executor commit is not present in the review checkout"
        else:
            independent_problem = "review checkout HEAD does not match the executor commit"
    elif repo_review:
        independent_problem = "exact review checkout is unavailable"

    verdict = (
        "approved"
        if semantic_valid and semantic_verdict == "approved" and independent_pass
        else "rejected"
    )
    digest_head = str(exec_access.get("base_sha") or "") if read_only_report_review else exec_head
    digest_input = ("%s|%s|%s" % (digest_head, exec_repo.get("remote_ref") or "", verdict)).encode("utf-8")
    import hashlib as _hashlib

    worktree_digest = "sha256:" + _hashlib.sha256(digest_input).hexdigest()
    repo_manifest = dict(exec_repo) if isinstance(exec_repo, dict) else {}
    manifest: Dict[str, Any] = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": verdict,
        "semantic_verdict": semantic_verdict or "invalid",
        "result": "review_completed",
        "returncode": 0,
        "review_id": review_id,
        "reviewed_evidence_id": executor_evidence_id,
        "worktree_digest": worktree_digest,
        "checks": [
            {
                "name": "semantic_review",
                "returncode": 0 if semantic_valid else 1,
                "status": "pass" if semantic_valid else "fail",
            },
            *(
                [codegraph_audit_check(codegraph)]
                if isinstance(codegraph, dict) and str(codegraph.get("status") or "") != "skipped"
                else []
            ),
            *(
                [
                    {
                        "name": "cooperative_integration",
                        "returncode": 0 if integration.get("status") == "pass" else 1,
                        "status": integration.get("status"),
                    }
                ]
                if isinstance(integration, dict)
                else []
            ),
            {
                "name": "review_verdict_finalizer",
                "returncode": 0 if independent_pass else 1,
                "status": "pass" if independent_pass else "fail",
            }
        ],
        # mac-wjy3: canonical list shape (see run_deterministic_git_finalizer).
        "tests": [tests] if tests is not None else None,
        "signed_by": reviewer_agent_id,
    }
    if repo_manifest:
        manifest["repo"] = repo_manifest
    if read_only_report_review:
        manifest["repository_access"] = {
            **exec_access,
            "independent_review_verified": independent_pass,
        }
    for key in ("summary", "feedback", "findings", "llm", "llm_model", "opencode_model", "gateway_model"):
        if key in semantic_manifest:
            manifest[key] = semantic_manifest[key]
    if verdict == "rejected" and not any(
        manifest.get(key) for key in ("feedback", "summary", "findings")
    ):
        if not semantic_valid:
            manifest["feedback"] = "review agent did not produce a valid semantic verdict"
        elif semantic_verdict == "rejected":
            manifest["feedback"] = "semantic reviewer rejected the executor result"
        else:
            manifest["feedback"] = independent_problem or "independent verification failed"
    elif verdict == "rejected" and independent_problem and semantic_verdict == "approved":
        existing = str(manifest.get("feedback") or "").strip()
        manifest["feedback"] = "; ".join(
            part for part in (existing, independent_problem) if part
        )
    if bootstrap is not None:
        manifest["bootstrap"] = bootstrap
    if codegraph is not None:
        manifest["codegraph"] = codegraph
    if integration is not None:
        manifest["integration"] = integration
    assignment = _review_experiment_assignment(task)
    if assignment:
        protocol = _read_json_object(task_workspace / "review-protocol.json")
        independent = _read_json_object(
            task_workspace / "review-independent-findings.json"
        )
        experiment_record = dict(assignment)
        if assignment.get("blind"):
            experiment_record["protocol"] = protocol or {
                "schema": "mac.review_protocol.v1",
                "mode": "blind_discovery_then_adjudication",
                "protocol_compliant": False,
                "problem": "blind discovery protocol record is missing",
            }
        else:
            experiment_record["protocol"] = {
                "schema": "mac.review_protocol.v1",
                "mode": "standard_evidence_aware",
                "protocol_compliant": True,
            }
        manifest["review_experiment"] = experiment_record
        if independent.get("schema") == "mac.independent_review_findings.v1":
            manifest["independent_findings"] = (
                independent.get("findings")
                if isinstance(independent.get("findings"), list)
                else []
            )
            manifest["independent_no_findings_reason"] = str(
                independent.get("no_findings_reason") or ""
            ).strip()
    manifest["signature"] = _sign_verdict(attestation_key, manifest)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def write_fallback_evidence_manifest(task_workspace: Path, task: Dict[str, Any], result, review_context) -> None:
    """autonomy-loop fix (loop-01): the fallback must never fabricate *verified*
    completion. It records the agent's output as an UNVERIFIED operator_result
    (never a fake repo_change/test, no synthetic passing check), so a
    proof-requiring task with no real evidence fails the verification gate
    honestly instead of auto-publishing chatter."""
    if result.returncode != 0 or isinstance(review_context, dict):
        return
    if task_is_repo_coupled(task):
        return
    manifest_path = task_workspace / "mac-evidence.json"
    if manifest_path.exists():
        return
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    result_text = stdout or stderr or ""
    summary = next((line.strip() for line in result_text.splitlines() if line.strip()), "")
    if len(summary) > 240:
        summary = summary[:237].rstrip() + "..."
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "operator_result",
        "summary": summary,
        "result": result_text[-20000:],
        "task": {"id": task.get("id"), "title": task.get("title"), "project": task.get("project")},
    }
    recovery_log = _load_harness_recovery_log(task_workspace)
    if recovery_log:
        manifest["recovery"] = recovery_log
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
