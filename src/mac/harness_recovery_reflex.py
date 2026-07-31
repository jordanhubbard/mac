"""Harness recovery reflex: bounded LLM remediation chooser at harness failure seams.

The recovery reflex is an *opt-in* mid-flight escape hatch that lets the
worker attempt autonomous remediation of well-known, transient harness
failures (e.g. an unrecognised package fetch failure, a flaky network call)
before escalating the task to the blocked queue.

Design constraints
------------------
* **Whitelist-only**: the LLM may only pick from a small, pre-approved set of
  remediation strategies.  Any unrecognised string returned by the LLM maps
  to ``escalate``.
* **Bounded**: each attempt_state carries a ``recovery_count`` counter.  Once
  it reaches the hard cap (``RECOVERY_LIMIT = 2``) ``try_recovery`` returns
  immediately without calling the LLM or the dispatch callable.
* **Observability**: every call to ``try_recovery`` emits one structured log
  event via the supplied ``observe`` callable.
* **No live HTTP in tests**: the LLM call is isolated behind a single
  ``_call_llm`` seam that tests can monkeypatch.

Usage
-----
Enable the reflex by setting ``MAC_RECOVERY_REFLEX_ENABLED=1`` in the
worker's environment.  When disabled, ``choose_remediation`` always returns
``"escalate"`` and the LLM is never called.

Public API
----------
``RemediationChoice``
    StrEnum of whitelisted remediation actions.
``choose_remediation(attempt_state, failure_info, llm_fn=None) -> str``
    Select a remediation action for the given failure.  Returns a
    ``RemediationChoice`` value (as a plain string) or ``"escalate"``.
``try_recovery(attempt_state, failure_info, dispatch, observe) -> tuple[bool, str, str]``
    High-level entry point used by the worker harness.  Returns
    ``(recovered, choice, log_message)``.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

JsonDict = Dict[str, Any]

RECOVERY_LIMIT = 2


class RemediationChoice(str, Enum):
    """Whitelisted remediation actions the LLM may choose."""

    retry_fetch = "retry_fetch"
    clear_cache = "clear_cache"
    escalate = "escalate"


# Convenience set for fast membership tests.
_WHITELIST: frozenset[str] = frozenset(m.value for m in RemediationChoice)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _reflex_enabled() -> bool:
    """Return True when MAC_RECOVERY_REFLEX_ENABLED is set to a truthy value."""
    val = os.environ.get("MAC_RECOVERY_REFLEX_ENABLED", "").strip().lower()
    return val in {"1", "true", "yes"}


def _call_llm(prompt: str) -> str:  # pragma: no cover  (seam – mocked in tests)
    """Call the LLM to select a remediation action.

    Returns the raw LLM response string.  Callers are responsible for
    validating it against the whitelist.

    This function is the *only* live-network boundary in this module and is
    always replaced by a mock in the contract test suite.
    """
    # Import lazily so the module can be imported without LLM deps installed.
    try:
        from mac.provider_router import call_llm_simple  # type: ignore[import-untyped]

        return call_llm_simple(prompt)
    except Exception as exc:  # noqa: BLE001
        return "escalate"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def choose_remediation(
    attempt_state: JsonDict,
    failure_info: str,
    *,
    llm_fn: Optional[Callable[[str], str]] = None,
) -> str:
    """Choose a remediation action for *failure_info*.

    Parameters
    ----------
    attempt_state:
        Mutable dict that the caller persists across retry attempts.  Must
        contain (at least) ``recovery_count: int`` and ``recovery_log: list``.
    failure_info:
        Human-readable description of the failure being triaged.
    llm_fn:
        Optional callable ``(prompt: str) -> str``.  When supplied it replaces
        the live ``_call_llm`` implementation, allowing tests to inject a mock
        without monkeypatching the module.

    Returns
    -------
    str
        One of the :class:`RemediationChoice` values (as a plain string).
        Unrecognised LLM output silently maps to ``"escalate"``.
    """
    if not _reflex_enabled():
        return RemediationChoice.escalate.value

    prompt = (
        "You are a worker harness recovery assistant.\n"
        "A task execution failed with the following error:\n\n"
        "  {failure_info}\n\n"
        "Choose exactly one remediation action from this list:\n"
        "  retry_fetch   – re-attempt the failing fetch/download step\n"
        "  clear_cache   – wipe the relevant local cache and retry\n"
        "  escalate      – give up; escalate to human review\n\n"
        "Reply with only the action name and nothing else."
    ).format(failure_info=failure_info)

    _fn = llm_fn if llm_fn is not None else _call_llm
    raw = _fn(prompt).strip().lower()

    if raw in _WHITELIST:
        return raw
    return RemediationChoice.escalate.value


def try_recovery(
    attempt_state: JsonDict,
    failure_info: str,
    dispatch: Optional[Callable[[str, JsonDict], Any]],
    observe: Callable[[str, str, str], None],
    *,
    llm_fn: Optional[Callable[[str], str]] = None,
) -> Tuple[bool, str, str]:
    """Attempt autonomous mid-flight recovery for a harness failure.

    Parameters
    ----------
    attempt_state:
        Mutable dict persisted across retries.  Expected keys:
        - ``recovery_count`` (int, default 0): number of prior recovery attempts.
        - ``recovery_log`` (list, default []): human-readable history entries.
    failure_info:
        Human-readable description of the failure being triaged.
    dispatch:
        Callable ``(action: str, context: dict) -> Any``.  The reflex calls
        this to execute the chosen remediation action.  Pass ``None`` when the
        caller only wants the retry/escalate decision and has no remediation
        dispatcher wired: the reflex then says so in the log instead of
        claiming it dispatched something.
    observe:
        Callable ``(step: str, choice: str, result: str) -> None``.  Called
        once per ``try_recovery`` invocation for observability.
    llm_fn:
        Optional LLM seam override (forwarded to :func:`choose_remediation`).

    Returns
    -------
    (recovered, choice, log_message) : tuple[bool, str, str]
        ``recovered`` – True when the caller should retry the failed step
        (a remediation was dispatched, or none was wired and the reflex chose
        to retry anyway); False on limit/escalation/dispatch failure.
        ``choice``    – The chosen :class:`RemediationChoice` value string.
        ``log_message`` – A brief human-readable explanation.
    """
    count = int(attempt_state.get("recovery_count", 0))
    log: list = list(attempt_state.get("recovery_log", []))

    # --- Hard limit check (no LLM call) ---
    if count >= RECOVERY_LIMIT:
        log_message = "recovery limit reached"
        observe("try_recovery", "escalate", log_message)
        return False, RemediationChoice.escalate.value, log_message

    # --- Ask the LLM (mocked in tests) ---
    choice = choose_remediation(attempt_state, failure_info, llm_fn=llm_fn)

    if choice == RemediationChoice.escalate.value:
        log_message = "LLM chose escalate for: %s" % failure_info
        attempt_state["recovery_count"] = count + 1
        attempt_state["recovery_log"] = log + [log_message]
        observe("try_recovery", choice, log_message)
        return False, choice, log_message

    # --- Dispatch the chosen remediation ---
    # A caller with no remediation dispatcher still gets the retry decision,
    # but must not be told a remediation ran. This log is what operators — and
    # this fleet's own agents — read to judge whether recovery works, so a
    # false "dispatched" here is worse than no record at all.
    if dispatch is None:
        log_message = (
            "chose %s; no remediation dispatcher wired - retrying without "
            "remediation (attempt %d)" % (choice, count + 1)
        )
        recovered = True
    else:
        try:
            dispatch(choice, {"failure_info": failure_info, "attempt": count})
            log_message = "dispatched %s (attempt %d)" % (choice, count + 1)
            recovered = True
        except Exception as exc:  # noqa: BLE001
            log_message = "dispatch(%s) failed: %s" % (choice, exc)
            recovered = False

    attempt_state["recovery_count"] = count + 1
    attempt_state["recovery_log"] = log + [log_message]
    observe("try_recovery", choice, log_message)
    return recovered, choice, log_message
