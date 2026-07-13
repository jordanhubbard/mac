"""Harness recovery helpers: failure context, remediation choices, lesson recall.

Provides the core types and query utilities used by the harness reflex layer
(see :mod:`mac.harness_reflex`) and any other module that needs to reason
about transient harness failures.

Public API
----------
``HarnessFailureContext``
    Lightweight dataclass carrying structured information about a failure.
``RemediationChoice``
    StrEnum of whitelisted remediation actions.
``choose_remediation(ctx, attempt_state, llm_fn=None) -> str``
    Consult the LLM (or a test double) to select a remediation action.
``recall_harness_lessons(step_name, failure_summary) -> list[str]``
    Return a small list of previously-observed lessons for the given step.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

JsonDict = Dict[str, Any]

RECOVERY_LIMIT: int = 2


class RemediationChoice(str, Enum):
    """Whitelisted remediation actions the LLM may choose."""

    retry_fetch = "retry_fetch"
    clear_cache = "clear_cache"
    escalate = "escalate"


# Fast membership set.
_WHITELIST: frozenset[str] = frozenset(m.value for m in RemediationChoice)


@dataclass
class HarnessFailureContext:
    """Structured context for a single harness failure event.

    Attributes
    ----------
    step_name:
        Logical step name where the failure occurred (e.g. ``"fetch_deps"``).
    stderr_tail:
        The last N lines of stderr produced by the failing step.
    task_id:
        Optional task identifier for structured logging.
    extra:
        Arbitrary key/value metadata the caller wants to attach.
    """

    step_name: str
    stderr_tail: str
    task_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_failure_info(self) -> str:
        """Return a compact human-readable description of the failure."""
        parts = [f"step={self.step_name!r}"]
        if self.task_id:
            parts.append(f"task_id={self.task_id!r}")
        if self.stderr_tail:
            parts.append(f"stderr={self.stderr_tail!r}")
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _reflex_enabled() -> bool:
    val = os.environ.get("MAC_RECOVERY_REFLEX_ENABLED", "").strip().lower()
    return val in {"1", "true", "yes"}


def _call_llm(prompt: str) -> str:  # pragma: no cover  (seam – mocked in tests)
    """Live LLM boundary – always monkeypatched in tests."""
    try:
        from mac.provider_router import call_llm_simple  # type: ignore[import-untyped]

        return call_llm_simple(prompt)
    except Exception:  # noqa: BLE001
        return RemediationChoice.escalate.value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def choose_remediation(
    ctx: HarnessFailureContext,
    attempt_state: JsonDict,
    *,
    llm_fn: Optional[Callable[[str], str]] = None,
) -> str:
    """Select a remediation action for *ctx*.

    When ``MAC_RECOVERY_REFLEX_ENABLED`` is falsy, always returns
    ``"escalate"`` without calling the LLM.

    Parameters
    ----------
    ctx:
        Structured failure context.
    attempt_state:
        Mutable per-attempt dict (not modified by this function).
    llm_fn:
        Optional test-double that replaces the live LLM call.

    Returns
    -------
    str
        One of the :class:`RemediationChoice` values (as a plain string).
    """
    if not _reflex_enabled():
        return RemediationChoice.escalate.value

    failure_info = ctx.as_failure_info()
    prompt = (
        "You are a worker harness recovery assistant.\n"
        "A task execution step failed:\n\n"
        "  {failure_info}\n\n"
        "Choose exactly one remediation action from this list:\n"
        "  retry_fetch   – re-attempt the failing fetch/download step\n"
        "  clear_cache   – wipe the relevant local cache and retry\n"
        "  escalate      – give up; escalate to human review\n\n"
        "Reply with only the action name and nothing else."
    ).format(failure_info=failure_info)

    fn = llm_fn if llm_fn is not None else _call_llm
    raw = fn(prompt).strip().lower()
    return raw if raw in _WHITELIST else RemediationChoice.escalate.value


def recall_harness_lessons(step_name: str, failure_summary: str) -> List[str]:
    """Return previously-observed lessons for *step_name*.

    In the baseline implementation this returns a static set of heuristic
    hints; a future version may query the fleet memory store.

    Parameters
    ----------
    step_name:
        Logical step name (e.g. ``"fetch_deps"``, ``"run_tests"``).
    failure_summary:
        Brief description of the failure (used for relevance filtering).

    Returns
    -------
    list[str]
        Zero or more short lesson strings that may inform recovery.
    """
    # Static heuristic lessons – deterministic and network-free.
    lessons_by_step: Dict[str, List[str]] = {
        "fetch_deps": [
            "Transient network errors on PyPI/NPM often resolve on retry.",
            "Cache corruption can cause hash-mismatch failures; clearing helps.",
        ],
        "run_tests": [
            "Flaky tests may pass on a clean retry without cache clearing.",
        ],
        "bootstrap": [
            "Re-running bootstrap usually heals incomplete installations.",
        ],
    }
    lessons = lessons_by_step.get(step_name, [])
    # Simple relevance filter: drop lessons that share no token with summary.
    summary_tokens = set(failure_summary.lower().split())
    if not summary_tokens:
        return lessons
    filtered = [
        lesson
        for lesson in lessons
        if any(tok in lesson.lower() for tok in summary_tokens)
    ]
    return filtered if filtered else lessons
