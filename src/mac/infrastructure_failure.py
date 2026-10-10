"""Typed executor infrastructure failures.

An attempt can end without published work for reasons that have nothing to do
with the task: the host could not reach the forge when the finalizer fetched
or pushed, or the coding agent's in-sandbox preflight timed out before any
work began. Before this module the worker reported both as the generic
``verification_contract_failed`` ("repo evidence requires changed files"),
which the hub treats as deterministic: non-retryable, manual repair, at
attempt 1 of 4 (task_71cfdfd5 and task_e95cc31a, 2026-10-05).

The signals here come only from the host's own structured records: the
finalizer's git stderr for its own fetch/push, and the executor's preflight
classes in ``coding_agents.runs``. Captured agent output is never searched,
because a test log mentions "timed out" as often as any transport does.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

SCHEMA = "mac.infrastructure_failure.v1"
#: Block reason the worker reports and the hub retries without charging.
BLOCK_REASON = "executor_infrastructure_failure"

#: git/curl/ssh stderr fragments that mean "the remote could not be reached",
#: never "the remote refused". Authentication and permission errors are left
#: out on purpose: retrying a bad credential does not fix it.
_GIT_TRANSPORT_MARKERS = (
    "could not resolve host",
    "temporary failure in name resolution",
    "failed to connect to",
    "could not connect to server",
    "couldn't connect to server",
    "connection timed out",
    "operation timed out",
    "connection reset",
    "connection refused",
    "network is unreachable",
    "no route to host",
    "the remote end hung up unexpectedly",
    "early eof",
    "rpc failed",
    "gnutls_handshake",
    "gnutls recv error",
    "ssl_error_syscall",
    "ssl_connect",
    "the requested url returned error: 500",
    "the requested url returned error: 502",
    "the requested url returned error: 503",
    "the requested url returned error: 504",
    # _run_git's own report of a git command killed at the phase deadline.
    "git operation exceeded finalizer phase budget",
)
_GIT_AUTH_MARKERS = (
    "authentication failed",
    "permission denied",
    "invalid username or password",
    "returned error: 401",
    "returned error: 403",
    "repository not found",
)

#: Coding-agent preflight classes (executor_sandbox) that a later run can
#: clear on its own. Configuration classes (missing binary, policy denial,
#: bad credentials, a probe that fails outright) stay ordinary failures.
TRANSIENT_PREFLIGHT_CLASSES = frozenset(
    {"timeout", "rate_limited", "provider_server_error", "inference_token_unavailable"}
)


def is_git_transport_failure(text: str) -> bool:
    """Whether a git error message reports an unreachable remote."""

    lowered = str(text or "").lower()
    if any(marker in lowered for marker in _GIT_AUTH_MARKERS):
        return False
    return any(marker in lowered for marker in _GIT_TRANSPORT_MARKERS)


def git_transport_failure(phase: str, error: str) -> Optional[Dict[str, Any]]:
    """The finalizer's typed record for a fetch/push the network defeated."""

    if not is_git_transport_failure(error):
        return None
    return {
        "schema": SCHEMA,
        "kind": "git_transport",
        "phase": str(phase or ""),
        "error": str(error or "")[:1000],
    }


def _preflight_failure(manifest: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    coding = manifest.get("coding_agents")
    runs = coding.get("runs") if isinstance(coding, Mapping) else None
    if not isinstance(runs, list) or not runs:
        return None
    last = runs[-1]
    if not isinstance(last, Mapping) or str(last.get("agent") or ""):
        # A coding agent ran: whatever happened next is the task's outcome.
        return None
    skipped = [item for item in last.get("skipped") or [] if isinstance(item, Mapping)]
    preflight = [item for item in skipped if item.get("failure_class") == "preflight_failed"]
    if not preflight:
        return None
    classes = [str(item.get("preflight_failure_class") or "") for item in preflight]
    if not all(cls in TRANSIENT_PREFLIGHT_CLASSES for cls in classes):
        return None
    return {
        "schema": SCHEMA,
        "kind": "coding_agent_preflight",
        "phase": "coding_agent_preflight",
        "agents": [
            {"agent": str(item.get("agent") or ""), "preflight_failure_class": cls}
            for item, cls in zip(preflight, classes)
        ],
        "error": "coding agent preflight failed before any work began: %s"
        % ", ".join(
            "%s (%s)" % (item.get("agent") or "?", cls) for item, cls in zip(preflight, classes)
        ),
    }


def executor_infrastructure_failure(manifest: Any) -> Optional[Dict[str, Any]]:
    """The typed infrastructure cause behind an attempt with no published work.

    ``None`` whenever the evidence claims publication: work that reached the
    remote is judged on its merits, and nothing here may excuse a claim.
    """

    if not isinstance(manifest, Mapping):
        return None
    repo = manifest.get("repo")
    if isinstance(repo, Mapping) and repo.get("pushed") is True:
        return None
    recorded = manifest.get("infrastructure_failure")
    if (
        isinstance(recorded, Mapping)
        and recorded.get("schema") == SCHEMA
        and recorded.get("kind") == "git_transport"
        and is_git_transport_failure(str(recorded.get("error") or ""))
    ):
        return dict(recorded)
    return _preflight_failure(manifest)


def describe(failure: Mapping[str, Any]) -> str:
    """One line naming the cause, for the task's failure diagnosis."""

    kind = str(failure.get("kind") or "infrastructure")
    phase = str(failure.get("phase") or "")
    error = str(failure.get("error") or "").strip()
    label = {
        "git_transport": "the host could not reach the git remote",
        "coding_agent_preflight": "the coding agent's sandbox preflight failed",
    }.get(kind, kind)
    text = label + (" during %s" % phase if phase and kind != "coding_agent_preflight" else "")
    return "%s: %s" % (text, error[:400]) if error else text
