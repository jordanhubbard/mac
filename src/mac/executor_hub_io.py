"""Hub I/O seam and plan-detection helpers for the autonomous task executor.

Extracted from :mod:`mac.task_executor` so that each seam can be tested and
evolved independently.  No behaviour changes; all function signatures and
return types are identical to the originals.

Sections
--------
* Small leaf utilities (no intra-module dependencies beyond stdlib /
  :mod:`mac.env_config` / :mod:`mac.openshell_runtime`)
* Hub I/O seam — single place all hub HTTP calls go through
* Plan-detection — automatic child-task decomposition heuristics
"""

from __future__ import annotations

import hashlib
import json
import os
import re as _re
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from mac.env_config import resolve_env_chain


# ---------------------------------------------------------------------------
# Small utilities (ported verbatim from the deploy heredoc)
# ---------------------------------------------------------------------------


def utcnow() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def sha256_text(value: str) -> str:
    """Return the SHA-256 digest of the given text as a prefixed hex string."""
    return "sha256:%s" % hashlib.sha256(value.encode("utf-8")).hexdigest()


def command_audit_id() -> str:
    """Generate a unique identifier for a command audit record."""
    seed = "%s:%s" % (time.time_ns(), os.getpid())
    return "cmd_%s" % hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def redacted_arg(value: str) -> str:
    """Return a redacted placeholder describing the given argument value."""
    return "<redacted:%s:chars=%d>" % (sha256_text(value), len(value))


def audit_safe_argv(argv: List[str]) -> List[str]:
    """Return a copy of the argument vector with sensitive values redacted."""
    safe: List[str] = []
    redact_next = False
    for raw in argv:
        arg = str(raw)
        lowered = arg.lower()
        if redact_next:
            safe.append(redacted_arg(arg))
            redact_next = False
            continue
        if lowered in {"--token", "--api-key", "--key", "--secret", "--password"}:
            safe.append(arg)
            redact_next = True
            continue
        if any(marker in lowered for marker in ("bearer ", "token=", "api_key=", "apikey=", "password=", "secret=")):
            safe.append(redacted_arg(arg))
            continue
        if len(arg) > 512:
            safe.append("<truncated:%s:chars=%d>" % (sha256_text(arg), len(arg)))
            continue
        safe.append(arg)
    return safe


def safe_path_component(value: str) -> str:
    """Return a filesystem-safe version of the given string for use in paths."""
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)[:180]


def local_agent_id() -> str:
    """Resolve the local agent identifier from the environment or hostname."""
    configured = resolve_env_chain("MAC_AGENT_ID", "MAC_WORKER_AGENT_ID")
    if configured:
        return configured
    name = resolve_env_chain("MAC_WORKER_AGENT_NAME") or os.uname().nodename.split(".")[0]
    return "agent_%s" % (safe_path_component(name.lower()).strip("_") or "default")


# ---------------------------------------------------------------------------
# Hub I/O seam — single place all hub calls go through (injectable for tests)
# ---------------------------------------------------------------------------


def _hub_env() -> tuple[str, str]:
    """Return ``(base_url, token)`` from the worker env, or empty strings."""
    base_url = resolve_env_chain("MAC_HUB_URL", "MAC_URL").rstrip("/")
    token = resolve_env_chain("MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN")
    return base_url, token


def _hub_post(path: str, payload: Dict[str, Any], *, timeout: float = 5.0) -> bool:
    """POST JSON to the hub. Best-effort: returns False (never raises) when hub
    env is absent or the call fails — telemetry/memory/audit must not break a
    task run."""
    base_url, token = _hub_env()
    if not base_url or not token:
        return False
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=data,
        headers={"Authorization": "Bearer %s" % token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=timeout).read()  # noqa: S310 (operator-configured URL)
        return True
    except Exception:
        return False


def _hub_post_json(path: str, payload: Dict[str, Any], *, timeout: float = 5.0) -> Optional[Any]:
    """POST JSON and return the parsed response body (or None on any failure).

    Like :func:`_hub_post` but surfaces the response so callers that need a
    server-assigned id (e.g. an opened AgentBus stream) can read it. Best-effort:
    returns None when hub env is absent or the call/parse fails."""
    base_url, token = _hub_env()
    if not base_url or not token:
        return None
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=data,
        headers={"Authorization": "Bearer %s" % token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        raw = urllib.request.urlopen(request, timeout=timeout).read()  # noqa: S310 (operator-configured URL)
    except Exception:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def _hub_get(path: str, *, timeout: float = 5.0) -> Optional[Any]:
    """GET JSON from the hub. Best-effort: returns None on any failure."""
    base_url, token = _hub_env()
    if not base_url or not token:
        return None
    request = urllib.request.Request(
        base_url + path,
        headers={"Authorization": "Bearer %s" % token, "Accept": "application/json"},
        method="GET",
    )
    try:
        raw = urllib.request.urlopen(request, timeout=timeout).read()  # noqa: S310
    except Exception:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def _hub_put(path: str, payload: Dict[str, Any], *, timeout: float = 10.0) -> bool:
    """PUT JSON to the hub.  Best-effort: returns False (never raises) when hub
    env is absent or the call fails."""
    base_url, token = _hub_env()
    if not base_url or not token:
        return False
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=data,
        headers={"Authorization": "Bearer %s" % token, "Content-Type": "application/json"},
        method="PUT",
    )
    try:
        urllib.request.urlopen(request, timeout=timeout).read()  # noqa: S310
        return True
    except Exception:
        return False


def _hub_post_child_tasks(task_id: str, children: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """POST child task specs to /tasks/{task_id}/children.  Returns the parsed
    response dict on success, None on any failure.  Best-effort / never raises.
    """
    if not task_id or not children:
        return None
    base_url, token = _hub_env()
    if not base_url or not token:
        return None
    path = "/tasks/%s/children" % task_id
    payload = {
        "children": children,
        "actor": os.environ.get("MAC_AGENT_ID", "").strip() or "mac-task-runner",
        "lease_id": os.environ.get("MAC_LEASE_ID", "").strip() or None,
    }
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=data,
        headers={"Authorization": "Bearer %s" % token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        raw = urllib.request.urlopen(request, timeout=10.0).read()  # noqa: S310
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Task-sizing / plan-detection (automatic child-task decomposition)
# ---------------------------------------------------------------------------

# Phrases that suggest a task title describes a *plan* rather than a single
# atomic unit of work.  A match on any of these increases the plan-signal
# count.  All comparisons are lower-cased.
_PLAN_TITLE_KEYWORDS: List[str] = [
    # coordination verbs
    "implement and",
    "build and",
    "design and",
    "create and",
    "set up and",
    "configure and",
    "deploy and",
    "add and",
    "write and",
    "refactor and",
    "migrate and",
    "update and",
    # multi-step indicators
    "end-to-end",
    "end to end",
    "full pipeline",
    "full workflow",
    "complete pipeline",
    "complete workflow",
    "phase 1",
    "phase 2",
    "phase one",
    "phase two",
    "multiple",
    "several",
    # planning language
    "plan and",
    "plan for",
    "architecture for",
    "roadmap",
    "epic",
    "initiative",
    "overview of",
    "breakdown of",
    "decompose",
    "scaffold and",
    "scaffold the",
]

# Patterns in the *description* field that strongly suggest a list-of-tasks
# was embedded.  We look for numbered lists (1. / 1) / Step 1) and bullet
# clusters of 3 or more items.
_NUMBERED_STEP_RE = _re.compile(
    r"(?m)^\s*(?:\d+[\.\):]|step\s+\d+[\.\):]?)\s+\S",
    _re.IGNORECASE,
)
_BULLET_RE = _re.compile(r"(?m)^\s*[-*•]\s+\S")


def detect_plan_signals(title: str, description: str) -> tuple:
    """Analyse *title* and *description* for signals that this task is actually
    a multi-step plan rather than a single atomic work item.

    Returns ``(is_plan: bool, signals: List[str])``.

    Heuristics (any 2+ signals → is_plan=True):

    1. Title contains a plan keyword/phrase (``_PLAN_TITLE_KEYWORDS``).
    2. Title is very long (> 120 chars, suggests over-specification).
    3. Description contains 3+ numbered steps.
    4. Description contains 5+ bullet points.
    5. Description word-count exceeds 300 words (dense spec).
    6. Title contains " and " linking two distinct verb clauses.
    """
    signals: List[str] = []
    title_lower = (title or "").lower().strip()
    desc = description or ""

    # Signal 1: plan keyword in title
    for kw in _PLAN_TITLE_KEYWORDS:
        if kw in title_lower:
            signals.append("plan_keyword:%r" % kw)
            break  # one keyword match is enough for signal 1

    # Signal 2: title length
    if len(title_lower) > 120:
        signals.append("long_title:%d_chars" % len(title_lower))

    # Signal 3: numbered steps in description
    numbered_matches = _NUMBERED_STEP_RE.findall(desc)
    if len(numbered_matches) >= 3:
        signals.append("numbered_steps:%d" % len(numbered_matches))

    # Signal 4: bullet cluster in description
    bullet_matches = _BULLET_RE.findall(desc)
    if len(bullet_matches) >= 5:
        signals.append("bullet_cluster:%d" % len(bullet_matches))

    # Signal 5: long description
    word_count = len(desc.split())
    if word_count > 300:
        signals.append("long_description:%d_words" % word_count)

    # Signal 6: conjunctive title with two verb clauses (e.g. "Implement X and add Y")
    if " and " in title_lower:
        # Heuristic: presence of a verb before *and* and another verb after
        _verb_re = _re.compile(
            r"\b(?:implement|build|create|add|write|design|configure|deploy|set up"
            r"|migrate|refactor|update|fix|test|verify|expose|scaffold|wire)\b",
            _re.IGNORECASE,
        )
        parts = title_lower.split(" and ", 1)
        if _verb_re.search(parts[0]) and _verb_re.search(parts[1]):
            signals.append("conjunctive_verb_title")

    is_plan = len(signals) >= 2
    return is_plan, signals


def _plan_detection_section(task: Dict[str, Any]) -> str:
    """Build the prompt section that tells the agent how to handle plan detection.

    If the task already has children (parent_task metadata) or is itself a
    child task, we skip — the agent should just execute, not re-decompose.
    """
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    # Handoff / plan-note guard: an operator can mark a task no_decompose
    # (`mac task create --no-decompose`) so the executor won't suggest breaking
    # it up — and the hub refuses the children call as a backstop.
    if isinstance(metadata, dict) and metadata.get("no_decompose"):
        return ""
    relationships = metadata.get("relationships") if isinstance(metadata, dict) else {}
    if isinstance(relationships, dict):
        # Already a child task or already has children — don't recurse
        if relationships.get("parent_task_id") or relationships.get("child_task_ids"):
            return ""

    title = str(task.get("title") or "")
    description = str(task.get("description") or "")
    is_plan, signals = detect_plan_signals(title, description)

    # The five-step fan-out recipe used to be printed on EVERY task, with the
    # sizing verdict only ever adding a prefix when it fired. A task the
    # detector scored as atomic still got the recipe and one hedged sentence
    # permitting it not to split -- and split anyway. Decomposition is now the
    # submitter's declaration, and this section says so either way.
    from mac.task_decomposition import prompt_section

    return prompt_section(task, is_plan=is_plan, signals=signals)
