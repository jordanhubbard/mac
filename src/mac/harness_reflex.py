"""Harness reflex: bounded per-attempt recovery counter and deterministic dispatch table.

The reflex layer sits between the worker harness and the recovery logic.  When
a harness step fails the caller may invoke :func:`try_recovery` which:

1. Checks the bounded counter (``attempt_state['recovery_count']``) – hard cap
   of :data:`RECOVERY_LIMIT` (2) before any LLM call.
2. Builds a :class:`~mac.harness_recovery.HarnessFailureContext`, calls
   :func:`~mac.harness_recovery.recall_harness_lessons` and
   :func:`~mac.harness_recovery.choose_remediation`.
3. Dispatches the chosen action through the deterministic :data:`DISPATCH`
   table.
4. Increments the counter and appends a structured log entry – never raises.

Design constraints
------------------
* Whitelist-only: ``DISPATCH`` maps only non-escalate
  :class:`~mac.harness_recovery.RemediationChoice` values.
* Network-free in tests: all external calls are isolated behind seams that
  tests can monkeypatch.

Public API
----------
``DISPATCH``
    Deterministic mapping of :class:`~mac.harness_recovery.RemediationChoice`
    to callable remediation functions.
``try_recovery(step_name, stderr_tail, task, task_dir, attempt_state, *, llm_fn) -> tuple``
    High-level entry point; see function docstring for the return contract.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from mac.harness_recovery import (
    RECOVERY_LIMIT,
    HarnessFailureContext,
    RemediationChoice,
    choose_remediation,
    recall_harness_lessons,
)
from mac.gitops import sync_worktree_with_canonical, guarded_push  # noqa: F401 (re-exported)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

JsonDict = Dict[str, Any]

# ---------------------------------------------------------------------------
# Deterministic remediation implementations
# ---------------------------------------------------------------------------


def _retry_fetch(ctx: HarnessFailureContext, task: JsonDict, task_dir: Path) -> str:
    """Re-attempt the failing fetch step.

    In the real harness this would re-invoke the package-install step.  Here
    we return a descriptive string; the actual bootstrap is re-run by the
    harness loop after ``try_recovery`` returns ``recovered=True``.
    """
    return "retry_fetch scheduled for step=%r" % ctx.step_name


def _clear_cache(ctx: HarnessFailureContext, task: JsonDict, task_dir: Path) -> str:
    """Wipe recognised cache directories under *task_dir* and retry."""
    cleared: list[str] = []
    cache_names = [".cache", "__pycache__", ".pytest_cache", ".mypy_cache"]
    for name in cache_names:
        candidate = task_dir / name
        if candidate.exists():
            shutil.rmtree(candidate, ignore_errors=True)
            cleared.append(name)
    summary = "cleared: %s" % (", ".join(cleared) if cleared else "nothing")
    return "clear_cache for step=%r – %s" % (ctx.step_name, summary)


# ---------------------------------------------------------------------------
# Public dispatch table
# ---------------------------------------------------------------------------

DISPATCH: Dict[str, Callable[[HarnessFailureContext, JsonDict, Path], str]] = {
    RemediationChoice.retry_fetch.value: _retry_fetch,
    RemediationChoice.clear_cache.value: _clear_cache,
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def try_recovery(
    step_name: str,
    stderr_tail: str,
    task: JsonDict,
    task_dir: Path,
    attempt_state: JsonDict,
    *,
    llm_fn: Optional[Callable[[str], str]] = None,
) -> Tuple[bool, str, str]:
    """Attempt autonomous mid-flight recovery for a harness failure.

    Parameters
    ----------
    step_name:
        Logical name of the step that failed (e.g. ``"fetch_deps"``).
    stderr_tail:
        Last N lines of stderr from the failing step.
    task:
        Task dict (used for task_id and context).
    task_dir:
        Path to the task working directory (used by some remediation actions).
    attempt_state:
        Mutable dict persisted across retry attempts.  Expected keys:
        - ``recovery_count`` (int, default 0)
        - ``recovery_log`` (list, default [])
    llm_fn:
        Optional LLM seam override forwarded to :func:`choose_remediation`.

    Returns
    -------
    (recovered, choice, detail) : tuple[bool, str, str]
        ``recovered`` – True when a non-escalate action was dispatched.
        ``choice``    – The chosen :class:`RemediationChoice` value string.
        ``detail``    – Human-readable outcome description.
    """
    count = int(attempt_state.get("recovery_count", 0))
    log: list = list(attempt_state.get("recovery_log", []))
    task_id: Optional[str] = task.get("id") if isinstance(task, dict) else None
    task_dir = Path(task_dir)

    # --- Bounded counter check (no LLM call) ---
    if count >= RECOVERY_LIMIT:
        detail = "recovery limit reached"
        log_entry = {"step": step_name, "choice": RemediationChoice.escalate.value, "result": detail}
        attempt_state["recovery_log"] = log + [log_entry]
        return False, RemediationChoice.escalate.value, detail

    # --- Build context and recall lessons ---
    ctx = HarnessFailureContext(
        step_name=step_name,
        stderr_tail=stderr_tail,
        task_id=task_id,
    )
    _lessons = recall_harness_lessons(step_name, stderr_tail)

    # --- Ask the LLM (mocked in tests) ---
    choice = choose_remediation(ctx, attempt_state, llm_fn=llm_fn)

    if choice == RemediationChoice.escalate.value:
        detail = "LLM chose escalate for step=%r" % step_name
        attempt_state["recovery_count"] = count + 1
        attempt_state["recovery_log"] = log + [
            {"step": step_name, "choice": choice, "result": detail}
        ]
        return False, choice, detail

    # --- Dispatch the chosen remediation ---
    dispatch_fn = DISPATCH.get(choice)
    try:
        if dispatch_fn is not None:
            outcome = dispatch_fn(ctx, task, task_dir)
        else:
            outcome = "no handler for choice=%r" % choice
        detail = outcome
        recovered = True
    except Exception as exc:  # noqa: BLE001
        detail = "dispatch(%s) raised: %s" % (choice, exc)
        recovered = False

    attempt_state["recovery_count"] = count + 1
    attempt_state["recovery_log"] = log + [
        {"step": step_name, "choice": choice, "result": detail}
    ]
    return recovered, choice, detail
