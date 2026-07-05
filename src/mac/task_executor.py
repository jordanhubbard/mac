"""Autonomous task executor (extracted from the deploy heredoc — loop-01).

This is the process the MacWorker spawns per claimed task. It builds a prompt,
runs the vendored Hermes agent (``hermes_cli.main chat --query … --yolo``,
agentic, max_turns=90) in the task's git worktree, then derives **honest,
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
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from mac import relay_observability
from mac.agent_command import PROMPT_SENTINEL
from mac.models import metadata_declares_report_deliverable
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

# ---------------------------------------------------------------------------
# Small utilities (ported verbatim from the deploy heredoc)
# ---------------------------------------------------------------------------


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def sha256_text(value: str) -> str:
    return "sha256:%s" % hashlib.sha256(value.encode("utf-8")).hexdigest()


def command_audit_id() -> str:
    seed = "%s:%s" % (time.time_ns(), os.getpid())
    return "cmd_%s" % hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def redacted_arg(value: str) -> str:
    return "<redacted:%s:chars=%d>" % (sha256_text(value), len(value))


def audit_safe_argv(argv: List[str]) -> List[str]:
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
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)[:180]


def local_agent_id() -> str:
    configured = os.environ.get("MAC_AGENT_ID") or os.environ.get("MAC_WORKER_AGENT_ID")
    if configured:
        return configured
    name = os.environ.get("MAC_WORKER_AGENT_NAME") or os.uname().nodename.split(".")[0]
    return "agent_%s" % (safe_path_component(name.lower()).strip("_") or "default")


# ---------------------------------------------------------------------------
# Hub I/O seam — single place all hub calls go through (injectable for tests)
# ---------------------------------------------------------------------------


def _hub_env() -> tuple[str, str]:
    """Return ``(base_url, token)`` from the worker env, or empty strings."""
    base_url = (os.environ.get("MAC_HUB_URL") or os.environ.get("MAC_URL") or "").rstrip("/")
    token = (
        os.environ.get("MAC_WORKER_TOKEN")
        or os.environ.get("MAC_TOKEN")
        or os.environ.get("MAC_API_TOKEN")
        or ""
    )
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
import re as _re

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

    plan_notice = ""
    if is_plan:
        plan_notice = (
            "TASK-SIZING ALERT: This task has been flagged as a likely PLAN "
            "(signals: %s). " % ", ".join(signals)
        )

    task_id = str(task.get("id") or "")
    project = str(task.get("project") or "")
    mac_url = (os.environ.get("MAC_HUB_URL") or os.environ.get("MAC_URL") or "").rstrip("/")
    children_endpoint = "%s/tasks/%s/children" % (mac_url, task_id) if mac_url and task_id else "/tasks/{task_id}/children"

    return "\n".join([
        "Task Sizing and Plan Detection:",
        "%sIf you determine — from the title, description, or early investigation — that this"
        " task represents a PLAN (multiple independent deliverables, phased work, or a"
        " collection of steps each requiring its own evidence trail) rather than a single"
        " atomic work item:" % plan_notice,
        "  1. Do NOT attempt to implement all steps in one run.",
        "  2. Break the work into 2-10 focused child tasks. Each child must be independently"
        "     completable and verifiable by a different agent.",
        "  3. Post the children to the MAC API: POST %s" % children_endpoint,
        "     with JSON body: {\"children\": [{\"title\": \"...\", \"description\": \"...\"},...]}",
        "     The MAC token is in the MAC_TOKEN / MAC_WORKER_TOKEN environment variable.",
        "  4. Write mac-evidence.json with evidence_type=operator_result, a summary field,"
        "     and a result field listing the child task titles you created.",
        "  5. Exit — the parent task will automatically block on its children.",
        "If the task IS a single atomic work item (< 1 day effort, single deliverable,"
        " one clear verification command), execute it directly and skip step 1-5.",
    ])


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
    payload = {"children": children}
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
# Scope estimation (scope-01)
# ---------------------------------------------------------------------------

#: Signal thresholds for the deterministic component of scope estimation.
_SCOPE_LARGE_DESC_WORDS = 200  # description word count → large signal
_SCOPE_LARGE_DESC_CHARS = 800  # description char count → large signal
_SCOPE_LARGE_REPO_CMDS = 3     # number of required_commands → large signal


def _compute_scope_signals(
    title: str,
    description: str,
    metadata: Dict[str, Any],
    prior_lessons: List[Dict[str, Any]],
) -> List[str]:
    """Pure inner function: compute all large-scope signals for a task.

    Contains all existing signal logic (D1-D5) plus the new memory signal:
    if *prior_lessons* is non-empty and contains any ``mac.plan_learning.v1``
    records, one ``memory:prior_decomposition:<task_id>`` signal is appended
    per matching record (auditable via the task_id from the record).

    Parameters
    ----------
    title:
        Task title string.
    description:
        Task description string.
    metadata:
        Task metadata dict (may be empty).
    prior_lessons:
        List of raw memory record dicts (from ``recall_scope_lessons``).
        Each dict may contain a ``content`` field with a JSON-encoded
        ``mac.plan_learning.v1`` blob.  Empty list → no memory signals.

    Returns
    -------
    List[str]
        All detected signals, including memory signals.
    """
    signals: List[str] = []

    # --- deterministic signals ---

    # D1: description word count
    word_count = len(description.split())
    if word_count >= _SCOPE_LARGE_DESC_WORDS:
        signals.append("desc_words:%d" % word_count)

    # D2: description char count (catches dense/technical descriptions)
    if len(description) >= _SCOPE_LARGE_DESC_CHARS:
        signals.append("desc_chars:%d" % len(description))

    # D3: repository contract breadth (number of required_commands)
    rc = (
        _nested_dict(metadata, "execution_contract", "repository_contract")
        or _nested_dict(metadata, "origin", "repository_contract")
        or {}
    )
    toolchain = rc.get("toolchain") if isinstance(rc, dict) else {}
    if isinstance(toolchain, dict):
        required_cmds = toolchain.get("required_commands") or []
        if isinstance(required_cmds, list) and len(required_cmds) >= _SCOPE_LARGE_REPO_CMDS:
            signals.append("repo_required_cmds:%d" % len(required_cmds))

    # D4: plan-detection signals (reuse existing logic)
    is_plan, plan_signals = detect_plan_signals(title, description)
    if is_plan:
        signals.append("plan_detected")
    for ps in plan_signals[:3]:  # cap to avoid oversized rationale
        signals.append("plan_signal:%s" % ps)

    # D5: long title (over-specified tasks tend to be large)
    if len(title) > 100:
        signals.append("long_title:%d" % len(title))

    # --- memory signal: prior decomposition lessons ---
    # Each mac.plan_learning.v1 record found in prior_lessons contributes one
    # auditable large signal so a task with one textual large-signal + one
    # memory hit becomes "large".
    for record in (prior_lessons or []):
        if not isinstance(record, dict):
            continue
        content_raw = record.get("content") or ""
        try:
            data = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        if data.get("schema") != "mac.plan_learning.v1":
            continue
        # Append an auditable signal using the task_id from the stored record.
        prior_task_id = str(data.get("task_id") or "unknown")
        signals.append("memory:prior_decomposition:%s" % prior_task_id)

    return signals


def recall_scope_lessons(task: Dict[str, Any], *, limit: int = 5) -> List[Dict[str, Any]]:
    """Recall prior ``mac.plan_learning.v1`` memory records for *task*.

    Queries the hub ``/memory`` endpoint for ``deployment_learning:<project>``
    records whose content matches the task's family terms (extracted via
    :func:`_plan_family_terms`).  Filters to only ``mac.plan_learning.v1``
    records so only prior plan-decomposed outcomes are surfaced.

    Each returned dict contains at least:

    * ``'id'`` — the ``task_id`` from the stored ``mac.plan_learning.v1``
      content (or ``''`` when absent), identifying which memory records
      influenced the estimate so callers can record provenance.
    * ``'rendered'`` — a short human-readable summary of the lesson (via
      :func:`_format_plan_learning_content`), suitable for logging or prompt
      injection.
    * ``'content'`` — the raw JSON-encoded ``mac.plan_learning.v1`` blob from
      the hub record, preserved for callers that need the full payload.

    Best-effort: returns ``[]`` immediately when the hub is unreachable, the
    env is absent, or no matching records exist.  Never raises.

    Has **no** side effects and does **not** call
    :func:`compute_scope_estimate`.

    Note: ``_task_project``, ``_plan_family_terms``,
    ``DEPLOYMENT_LEARNING_PREFIX``, and ``_format_plan_learning_content`` are
    all defined later in this module; they are resolved at call time (not
    import time) so forward references are safe.
    """
    from urllib.parse import urlencode

    project = _task_project(task)
    family_terms = _plan_family_terms(task)

    records: List[Dict[str, Any]] = []
    seen_task_ids: set = set()

    for term in (family_terms or [""]):
        params: Dict[str, Any] = {
            "subject_type": "project",
            "subject_id": project,
            "record_type": "%s:%s" % (DEPLOYMENT_LEARNING_PREFIX, project),
            "limit": 20,
        }
        if term:
            params["content_contains"] = term
        raw = _hub_get("/memory?%s" % urlencode(params))
        if not isinstance(raw, list):
            continue
        for rec in raw:
            if not isinstance(rec, dict):
                continue
            content_raw = rec.get("content") or ""
            try:
                data = json.loads(content_raw) if isinstance(content_raw, str) else {}
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(data, dict) or data.get("schema") != "mac.plan_learning.v1":
                continue
            # Deduplicate by task_id in the stored record.
            prior_task_id = str(data.get("task_id") or "")
            if prior_task_id in seen_task_ids:
                continue
            seen_task_ids.add(prior_task_id)
            # Build an enriched result dict so callers can record provenance
            # ('id') and display a formatted lesson ('rendered') without having
            # to parse the raw content themselves.  The original hub fields are
            # preserved alongside the new keys for backward compatibility.
            rendered = _format_plan_learning_content(content_raw)
            enriched = dict(rec)
            enriched["id"] = prior_task_id
            enriched["rendered"] = rendered
            records.append(enriched)
            if len(records) >= limit:
                return records

    return records


def compute_scope_estimate(task: Dict[str, Any]) -> Dict[str, Any]:
    """Estimate task scope using deterministic text signals plus memory recall.

    Examines description length, title length, repository contract breadth,
    plan-detection signals, and prior ``mac.plan_learning.v1`` decomposition
    records recalled from the hub to produce a ``{size, rationale,
    estimated_units}`` dict suitable for storing as
    ``metadata.scope_estimate``.

    The public signature is unchanged.  Internally this delegates to
    :func:`_compute_scope_signals` (the pure inner layer) after a best-effort
    call to :func:`recall_scope_lessons`.

    Contracts:

    * Hub unreachable or no prior lessons → output identical to the
      pure-textual estimate (no regression).
    * One prior decomposition lesson match → ``memory:prior_decomposition``
      appears in signals; if combined with one textual signal, size flips to
      ``"large"``.

    Schema (``mac.scope_estimate.v1``)::

        {
            "schema": "mac.scope_estimate.v1",
            "size": "small" | "large",
            "rationale": "<human-readable explanation>",
            "estimated_units": 1 | 2,   # story points
            "signals": ["desc_words:350", "memory:prior_decomposition:task_abc", ...],
        }
    """
    title = str(task.get("title") or "") if isinstance(task, dict) else ""
    description = str(task.get("description") or "") if isinstance(task, dict) else ""
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}

    # Best-effort recall of prior decomposition records (memory signal).
    try:
        prior_lessons = recall_scope_lessons(task)
    except Exception:  # noqa: BLE001
        prior_lessons = []

    signals = _compute_scope_signals(title, description, metadata, prior_lessons)

    # --- decision (any 2+ large-signals → large) ---
    large_signal_count = sum(
        1 for s in signals
        if not s.startswith("plan_signal:")  # plan sub-signals don't double-count
    )
    size = "large" if large_signal_count >= 2 else "small"
    estimated_units = 2 if size == "large" else 1

    if signals:
        rationale = "size=%s based on: %s" % (size, "; ".join(signals[:5]))
    else:
        rationale = "no large-scope signals detected; classified as small"

    return {
        "schema": "mac.scope_estimate.v1",
        "size": size,
        "rationale": rationale,
        "estimated_units": estimated_units,
        "signals": signals,
    }


def needs_scope_estimate(task: Dict[str, Any]) -> bool:
    """Return True when the task should receive a scope estimate.

    Fires only on the FIRST execution attempt (``attempt_count == 1``) and
    only when ``metadata.scope_estimate`` has not already been recorded.
    Handles ``None`` metadata gracefully.
    """
    if not isinstance(task, dict):
        return False
    raw_attempt = task.get("attempt_count")
    try:
        attempt_count = int(raw_attempt or 0)
    except (TypeError, ValueError):
        attempt_count = 0
    if attempt_count != 1:
        return False
    metadata = task.get("metadata")
    if not isinstance(metadata, dict):
        return True  # no metadata at all → never been estimated
    return "scope_estimate" not in metadata


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


def record_scope_estimate(
    task_id: str,
    estimate: Dict[str, Any],
    existing_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Persist ``estimate`` as ``metadata.scope_estimate`` on the task hub record.

    Merges the estimate into the task's existing metadata and PUTs the full
    metadata dict to ``PUT /tasks/{task_id}``.  Best-effort — never raises.

    Returns True when the hub accepted the update.
    """
    if not task_id:
        return False
    merged: Dict[str, Any] = dict(existing_metadata or {})
    merged["scope_estimate"] = estimate
    return _hub_put(
        "/tasks/%s" % task_id,
        {"metadata": merged},
    )


def maybe_preflight_scope_estimate(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compute and record a scope estimate when this is the first attempt.

    Returns the estimate dict when one was computed and attempted to record,
    or None when the task already has an estimate or this is not the first
    attempt.  Best-effort: recording failure is silent (the estimate is still
    returned so the caller can emit telemetry).
    """
    if not needs_scope_estimate(task):
        return None
    task_id = str(task.get("id") or "")
    estimate = compute_scope_estimate(task)
    metadata = task.get("metadata")
    existing: Optional[Dict[str, Any]] = metadata if isinstance(metadata, dict) else None
    record_scope_estimate(task_id, estimate, existing)
    return estimate


# ---------------------------------------------------------------------------
# Planning-phase execution mode (plan-01)
# ---------------------------------------------------------------------------


def is_planning_phase(task: Dict[str, Any]) -> bool:
    """Return True when this task run should PLAN rather than execute.

    A task enters planning-phase execution on its FIRST run when:
    - ``metadata.plan_first=True`` (operator-declared intent), OR
    - ``metadata.scope_estimate.size == "large"`` (computed by scope-01 preflight).

    Child tasks and handoff tasks are excluded so they always execute.
    Tasks already decomposed (have children) are also excluded.

    This is a *pure* function — it never touches the network.
    """
    if not isinstance(task, dict):
        return False

    # Only fire on the first attempt.
    raw_attempt = task.get("attempt_count")
    try:
        attempt_count = int(raw_attempt or 0)
    except (TypeError, ValueError):
        attempt_count = 0
    if attempt_count != 1:
        return False

    metadata = task.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    # Never plan child tasks, handoff tasks, or no_decompose tasks.
    if metadata.get("no_decompose"):
        return False
    relationships = metadata.get("relationships")
    if isinstance(relationships, dict):
        if relationships.get("parent_task_id") or relationships.get("child_task_ids"):
            return False

    # Explicit operator override wins.
    if metadata.get("plan_first"):
        return True

    # Scope-01 estimate: already recorded as metadata.scope_estimate.
    scope_estimate = metadata.get("scope_estimate")
    if isinstance(scope_estimate, dict) and scope_estimate.get("size") == "large":
        return True

    return False


def build_planning_prompt(
    task: Dict[str, Any],
    task_file: "Path",
    lessons: Optional[List[str]] = None,
) -> str:
    """Build the agent prompt for a planning-phase run.

    The agent is instructed to analyse the task scope, consult the
    topology primitive (``mac plan order``) to derive a dependency
    ordering, and post N sized child tasks via POST /tasks/{id}/children
    — one child per independent work item.  It must NOT implement any
    code in this run.

    A plan-evidence manifest (evidence_type=plan_decomposed) with a
    ``children`` list and an ``ordering_rationale`` field is written to
    mac-evidence.json so the deterministic host can verify coverage.
    """
    task_id = str(task.get("id") or "")
    mac_url = (
        os.environ.get("MAC_HUB_URL") or os.environ.get("MAC_URL") or ""
    ).rstrip("/")
    children_endpoint = (
        "%s/tasks/%s/children" % (mac_url, task_id)
        if mac_url and task_id
        else "/tasks/{task_id}/children"
    )

    metadata = task.get("metadata") if isinstance(task, dict) else {}
    scope_estimate = (metadata.get("scope_estimate") or {}) if isinstance(metadata, dict) else {}
    size = scope_estimate.get("size", "unknown")
    signals = scope_estimate.get("signals") or []
    plan_first = bool(metadata.get("plan_first")) if isinstance(metadata, dict) else False

    trigger_reason = (
        "metadata.plan_first=true"
        if plan_first
        else "scope_estimate.size=%s (signals: %s)" % (size, ", ".join(signals) or "none")
    )

    parts = [
        "You are running as a MAC fleet worker in PLANNING MODE. "
        "Your job is to PLAN this task, NOT to implement it.",
        "You are AUTONOMOUS: never ask the operator for confirmation or permission. "
        "Make a reasonable assumption, proceed, and record it when necessary.",
        "First read the versioned execution policy at "
        "$MAC_TASK_WORKSPACE/.mac-executor-policy.txt, then read task.json as the "
        "source of truth.",
        "PLANNING MODE TRIGGER: %s" % trigger_reason,
        "\n".join([
            "PLANNING PHASE INSTRUCTIONS:",
            "  1. Analyse the task description to identify ALL independent deliverables and phases.",
            "  2. Use the topology primitive to derive ordering:",
            "     Run: mac plan order <changed-files or key-modules> --repo $MAC_TASK_REPO_WORKTREE",
            "     (or call mac.planning.order_layers() directly). Use the layer ordering to set",
            "     dependencies[] on child tasks so leaves run before cores.",
            "     If topology information is unavailable, use logical dependency ordering.",
            "  3. Create 2-10 child tasks. Each child MUST:",
            "     a. Be completable and verifiable by ONE agent in a single run.",
            "     b. Have a clear title and description.",
            "     c. Include dependencies=[<sibling-task-ids>] for tasks that must run after others.",
            "        IMPORTANT: at creation time sibling IDs are not yet known; post children in",
            "        topological order and use the returned IDs to set dependencies on later tasks,",
            "        OR post all children in one request with relative ordering implied by list order.",
            "  4. POST the children to: %s" % children_endpoint,
            "     Body: {\"children\": [{\"title\": \"...\", \"description\": \"...\", "
            "\"dependencies\": []}, ...]}",
            "     The MAC token is in MAC_TOKEN / MAC_WORKER_TOKEN environment variable.",
            "  5. Write mac-evidence.json with:",
            "     {",
            "       \"schema\": \"mac.worker_evidence.v1\",",
            "       \"status\": \"complete\",",
            "       \"evidence_type\": \"plan_decomposed\",",
            "       \"summary\": \"<one-sentence description of the plan>\",",
            "       \"children\": [{\"title\": \"...\", \"description\": \"...\"}, ...],",
            "       \"ordering_rationale\": \"<why this order>\",",
            "       \"coverage_claim\": \"<how the children together cover the full parent scope>\"",
            "     }",
            "  6. Exit — the parent task will automatically block on its children.",
            "DO NOT write any code, DO NOT make any code changes, DO NOT run tests.",
            "DO NOT write evidence_type=repo_change — only plan_decomposed is valid here.",
        ]),
    ]

    lessons_section = _lessons_section(lessons or [])
    if lessons_section:
        parts.append(lessons_section)
    parts.append("Read the full task from: %s" % str(task_file))
    parts.append(
        "Finally, for the per-task activity log, print a short plain-language recap "
        "of what you did and how you verified it (1-3 sentences, no code or diff), "
        "wrapped EXACTLY in these two marker lines:\n%s\n<your recap here>\n%s"
        % (MAC_TASK_SUMMARY_BEGIN, MAC_TASK_SUMMARY_END)
    )
    return "\n\n".join(parts)


def is_plan_decomposed_evidence(task_workspace: "Path") -> bool:
    """Return True when mac-evidence.json declares evidence_type=plan_decomposed.

    Used by the git finalizer path: a planning-phase run does not produce
    a repo change, so we must skip the dirty-worktree check and the push step.
    """
    manifest_path = task_workspace / "mac-evidence.json"
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(manifest, dict):
        return False
    return manifest.get("evidence_type") == "plan_decomposed"


def maybe_auto_decompose(task_workspace: Path, task: Dict[str, Any]) -> bool:
    """Read the agent's mac-evidence.json for a ``plan_steps`` key; if found,
    auto-post those steps as child tasks via the MAC API.

    This is the "declarative" path: the agent writes ``plan_steps`` in its
    evidence manifest instead of directly calling the API itself, and the
    executor handles the hub call.  Returns True if children were posted.

    Expected evidence shape::

        {
            "plan_steps": [
                {"title": "Step A", "description": "..."},
                {"title": "Step B", "description": "..."},
                ...
            ]
        }

    Only fires when:
    - ``plan_steps`` is a non-empty list of objects with a ``title`` field.
    - The task is not already a child (no parent_task_id in relationships).
    - The hub env is present.
    """
    manifest_path = task_workspace / "mac-evidence.json"
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(manifest, dict):
        return False

    plan_steps = manifest.get("plan_steps")
    if not isinstance(plan_steps, list) or not plan_steps:
        return False

    # Don't decompose child tasks further, or tasks flagged no_decompose.
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    if isinstance(metadata, dict) and metadata.get("no_decompose"):
        return False
    relationships = metadata.get("relationships") if isinstance(metadata, dict) else {}
    if isinstance(relationships, dict) and relationships.get("parent_task_id"):
        return False

    task_id = str(task.get("id") or "")
    if not task_id:
        return False

    # Normalise plan_steps: must be list of dicts with a title
    children: List[Dict[str, Any]] = []
    for step in plan_steps:
        if isinstance(step, dict) and str(step.get("title") or "").strip():
            child: Dict[str, Any] = {"title": str(step["title"]).strip()}
            if step.get("description"):
                child["description"] = str(step["description"]).strip()
            if step.get("dependencies"):
                child["dependencies"] = step["dependencies"]
            if step.get("required_capabilities"):
                child["required_capabilities"] = step["required_capabilities"]
            children.append(child)

    if not children:
        return False

    result = _hub_post_child_tasks(task_id, children)
    return result is not None


def post_command_audit(agent_id: str, payload: Dict[str, Any]) -> None:
    if not agent_id:
        return
    _hub_post("/agents/%s/command-audit" % agent_id, payload)


def _run_captured(argv: List[str], cwd: Path, timeout: Optional[float]):
    """``subprocess.run(capture_output=True)`` equivalent that kills the WHOLE
    process tree on timeout.

    ``subprocess.run()``'s timeout path kills only the direct child. On
    unsandboxed hosts the agent's surviving children then keep running on the
    node (leaked servers, wedged tool subprocesses) and keep the inherited
    stdout/stderr pipes open, so the post-kill output drain can block
    indefinitely. Run the child in its own session and SIGKILL the process
    group on timeout: everything dies and the pipes are guaranteed to reach
    EOF. Sandboxed runs are unaffected — OpenShell teardown already bounds
    those — but get the same guarantee for the CLI process itself.
    """
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError, PermissionError, OSError):
            proc.kill()
        out, err = proc.communicate()
        raise subprocess.TimeoutExpired(argv, timeout or 0.0, output=out, stderr=err)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)



def clip_process_text(value: str, limit: int = 4000) -> str:
    """Bound process output keeping head AND tail — the tail carries the
    diagnosis (pytest failure summaries, pip errors print last). Mirrors
    worker._truncate_process_text; the head-only cuts this replaces made
    long failures undiagnosable from evidence."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(0, limit // 4)
    tail = limit - head
    marker = "\n… [%d chars omitted] …\n" % (len(text) - head - tail)
    return text[:head] + marker + text[-tail:]


def run_with_stall_watchdog(
    argv: List[str],
    cwd: Path,
    *,
    stall_timeout: Optional[float] = None,
    hard_timeout: Optional[float] = None,
) -> "subprocess.CompletedProcess[str]":
    """Run a command, killing it only when it STOPS MAKING PROGRESS.

    Total-runtime budgets on verification commands have a long history of
    going stale: every time legitimate work grows (a venv bootstrap, a bigger
    suite), the constant kills healthy runs mid-flight, indistinguishable from
    real failures. A progress-based watchdog ends that lineage: the child is
    killed when it emits NO output for ``stall_timeout`` seconds (a genuinely
    hung process goes quiet; a slow suite keeps printing progress). The
    ``hard_timeout`` ceiling remains as a backstop against pathological
    always-printing loops. Either kill takes the whole process group
    (start_new_session), same as ``_run_captured``, and returns rc 124 with an
    explicit marker appended to stderr instead of raising — callers treat it
    as a failed check with a diagnosable reason.

    Defaults: MAC_TEST_STALL_TIMEOUT (300s) / MAC_WORKER_REPOSITORY_TEST_TIMEOUT
    (1800s).
    """
    import signal

    def _env_float(name: str, fallback: float) -> float:
        try:
            value = float(os.environ.get(name, "") or fallback)
            return value if value > 0 else fallback
        except ValueError:
            return fallback

    stall = stall_timeout if stall_timeout is not None else _env_float("MAC_TEST_STALL_TIMEOUT", 300.0)
    hard = hard_timeout if hard_timeout is not None else _env_float("MAC_WORKER_REPOSITORY_TEST_TIMEOUT", 1800.0)

    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    chunks: Dict[str, List[bytes]] = {"out": [], "err": []}
    last_activity = [time.monotonic()]

    def _drain(stream, key: str) -> None:
        for chunk in iter(lambda: stream.read1(65536), b""):
            chunks[key].append(chunk)
            last_activity[0] = time.monotonic()
        stream.close()

    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, "out"), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, "err"), daemon=True),
    ]
    for r in readers:
        r.start()

    started = time.monotonic()
    kill_reason = ""
    while True:
        if proc.poll() is not None:
            break
        now = time.monotonic()
        if now - last_activity[0] > stall:
            kill_reason = "stalled: no output for %.0fs (MAC_TEST_STALL_TIMEOUT)" % stall
        elif now - started > hard:
            kill_reason = "exceeded hard ceiling of %.0fs (MAC_WORKER_REPOSITORY_TEST_TIMEOUT)" % hard
        if kill_reason:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (AttributeError, ProcessLookupError, PermissionError, OSError):
                proc.kill()
            break
        time.sleep(min(1.0, stall / 10.0))
    proc.wait()
    for r in readers:
        r.join(timeout=10.0)
    out = b"".join(chunks["out"]).decode("utf-8", errors="replace")
    err = b"".join(chunks["err"]).decode("utf-8", errors="replace")
    if kill_reason:
        err = (err + "\n" if err else "") + "run_with_stall_watchdog: killed — %s" % kill_reason
        return subprocess.CompletedProcess(argv, 124, out, err)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def run_audited_command(argv: List[str], cwd: Path, task_id, metadata: Dict[str, Any]):
    command_id = command_audit_id()
    agent_id = local_agent_id()
    started_at = utcnow()
    started = time.monotonic()
    argv_hash = sha256_text(json.dumps(argv, separators=(",", ":")))
    base = {
        "command_id": command_id,
        "argv": audit_safe_argv(argv),
        "cwd": str(cwd),
        "task_id": task_id,
        "started_at": started_at,
        "metadata": {"component": "mac-hermes-task-executor", "argv_sha256": argv_hash, **metadata},
    }
    post_command_audit(agent_id, {**base, "phase": "started"})
    timeout = metadata.pop("timeout", None) if isinstance(metadata, dict) else None
    try:
        result = _run_captured(argv, cwd, timeout)
    except subprocess.TimeoutExpired as exc:
        # loop-01 resilience: a wedged TokenHub turn can hang the agent
        # indefinitely. Bound it. The agent may already have written a valid
        # mac-evidence.json before the trailing turn stalled; main() salvages
        # that so verified work isn't discarded just because the run was capped.
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        post_command_audit(
            agent_id,
            {
                **base,
                "phase": "timeout",
                "completed_at": utcnow(),
                "duration_ms": (time.monotonic() - started) * 1000.0,
                "metadata": {**base["metadata"], "timeout_seconds": timeout},
            },
        )
        return subprocess.CompletedProcess(argv, 124, out, err + "\n[executor] agent run timed out after %ss" % timeout)
    except OSError as exc:
        post_command_audit(
            agent_id,
            {
                **base,
                "phase": "error",
                "completed_at": utcnow(),
                "duration_ms": (time.monotonic() - started) * 1000.0,
                "metadata": {**base["metadata"], "error": str(exc)},
            },
        )
        raise
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    post_command_audit(
        agent_id,
        {
            **base,
            "phase": "completed" if result.returncode == 0 else "failed",
            "completed_at": utcnow(),
            "duration_ms": (time.monotonic() - started) * 1000.0,
            "returncode": result.returncode,
            "stdout_sha256": sha256_text(stdout),
            "stderr_sha256": sha256_text(stderr),
            "stdout_bytes": len(stdout.encode("utf-8")),
            "stderr_bytes": len(stderr.encode("utf-8")),
        },
    )
    return result


# ---------------------------------------------------------------------------
# Telemetry path — executor-scoped observability stream
# ---------------------------------------------------------------------------


def build_telemetry_record(
    event: str, *, task_id: Optional[str], level: str, detail: Dict[str, Any]
) -> Dict[str, Any]:
    """Pure: build the ``/observability/logs`` payload for an executor event."""
    return {
        "name": "executor.%s" % event,
        "level": level,
        "layer": "executor",
        "source": "mac-hermes-task-executor",
        "subject_type": "task" if task_id else None,
        "subject_id": task_id,
        "detail": {"schema": "mac.executor_telemetry.v1", "agent_id": local_agent_id(), **detail},
    }


def emit_telemetry(event: str, *, task_id: Optional[str] = None, level: str = "info", **detail: Any) -> bool:
    """Emit one executor lifecycle observation (best-effort)."""
    return _hub_post("/observability/logs", build_telemetry_record(event, task_id=task_id, level=level, detail=detail))


# ---------------------------------------------------------------------------
# Memory feed — recall prior lessons in, record this run's lesson out
# ---------------------------------------------------------------------------

DEPLOYMENT_LEARNING_PREFIX = "deployment_learning"
_LESSON_PROMPT_BUDGET = 1600
_LESSON_STOPWORDS = {
    "add", "build", "change", "create", "deploy", "deployment", "fix", "implement",
    "improve", "make", "next", "task", "test", "the", "this", "update", "with",
}


def _task_project(task: Dict[str, Any]) -> str:
    return str(task.get("project") or "default")


def _format_learning_content(raw: str) -> str:
    """Render a stored ``mac.deployment_learning.v1`` blob as a one-line lesson."""
    fleet_learning = parse_repository_access_learning(raw)
    if fleet_learning is not None:
        outcome = str(fleet_learning.get("outcome") or "?")
        operation = str(fleet_learning.get("operation") or "repository access")
        host = str(fleet_learning.get("repository_host") or "repository host")
        agent_id = str(fleet_learning.get("agent_id") or "unknown agent")
        source = str(fleet_learning.get("credential_source") or "unknown mechanism")
        failure = str(fleet_learning.get("failure_class") or "").strip()
        recommendation = str(fleet_learning.get("recommendation") or "").strip()
        line = "[fleet %s] %s on %s by %s using %s" % (
            outcome,
            operation,
            host,
            agent_id,
            source,
        )
        if failure:
            line += " — %s" % failure
        if recommendation:
            line += "; %s" % recommendation
        return line[:500]
    try:
        data = json.loads(raw)
    except Exception:
        return raw.strip()[:300]
    if not isinstance(data, dict) or data.get("schema") != "mac.deployment_learning.v1":
        return raw.strip()[:300]
    outcome = data.get("outcome") or "?"
    title = str(data.get("task_title") or data.get("task_id") or "task")
    etype = data.get("evidence_type") or "?"
    err = str(data.get("error_signature") or "").strip()
    line = "[%s] %s (%s)" % (outcome, title, etype)
    if outcome == "failure" and err:
        line += " — failed: %s" % err
    return line[:300]


def _lesson_terms(value: str) -> set[str]:
    return {
        token
        for token in _re.findall(r"[a-z0-9][a-z0-9_-]{2,}", value.lower())
        if token not in _LESSON_STOPWORDS
    }


def _append_lesson_with_budget(lessons: List[str], value: str) -> bool:
    lesson = value.strip()
    if not lesson:
        return True
    used = sum(len(item) for item in lessons)
    remaining = _LESSON_PROMPT_BUDGET - used
    if remaining <= 0:
        return False
    lessons.append(lesson if len(lesson) <= remaining else lesson[: max(0, remaining - 3)] + "...")
    return sum(len(item) for item in lessons) < _LESSON_PROMPT_BUDGET


def recall_deployment_lessons(task: Dict[str, Any], *, limit: int = 5) -> List[str]:
    """Recall prior deployment lessons relevant to this task (best-effort).

    Two-stage so the loop gets smarter *immediately*, not just after the
    embedding pipeline matures:

    1. **Semantic** — the vector recall endpoint (richest; available once the
       nap consolidator has embedded prior learnings).
    2. **Fallback** — a direct read of the project's ``deployment_learning``
       memory records (most-recent-N), so the very next task on a project
       benefits from the last one's outcome even before any embedding.

    Returns short lesson strings; empty when the hub isn't reachable (the loop
    still runs, just without hindsight)."""
    title = str(task.get("title") or "").strip() or "task"
    project = _task_project(task)
    from urllib.parse import urlencode

    lessons: List[str] = []
    # Structured operational learnings are exact routing facts and should not
    # wait for embedding. Pull the common fleet records first, scoped to this
    # project and repository host, then enrich them with semantic recall.
    task_host = repository_host(task_repository_remote(task))
    fleet_records = _hub_get(
        "/memory?%s"
        % urlencode(
            {
                "record_type": REPOSITORY_ACCESS_RECORD_TYPE,
                "order": "desc",
                "limit": 50,
            }
        )
    )
    if isinstance(fleet_records, list):
        for record in fleet_records:
            if not isinstance(record, dict):
                continue
            learning = parse_repository_access_learning(record.get("content"))
            if learning is None:
                continue
            if str(learning.get("project") or "default") != project:
                continue
            learning_host = str(learning.get("repository_host") or "")
            if task_host and learning_host != task_host:
                continue
            rendered = _format_learning_content(str(record.get("content") or ""))
            if rendered in lessons:
                continue
            if not _append_lesson_with_budget(lessons, rendered):
                break
            if len(lessons) >= limit:
                return lessons[:limit]

    semantic_added = False
    results = _hub_get(
        "/v1/memory/recall?%s"
        % urlencode({"q": title, "project": project, "tier": "medium", "limit": max(1, int(limit))})
    )
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            text = str(item.get("content") or item.get("text") or item.get("summary") or "").strip()
            if text and not _append_lesson_with_budget(
                lessons, text if len(text) <= 500 else text[:497] + "..."
            ):
                break
            if text:
                semantic_added = True
            if len(lessons) >= limit:
                break
    if semantic_added:
        return lessons[:limit]

    records = _hub_get("/memory?%s" % urlencode({"subject_type": "project", "subject_id": project}))
    if isinstance(records, list):
        query_terms = _lesson_terms(
            " ".join(
                [
                    title,
                    str(task.get("description") or "")[:1000],
                    str(task.get("project") or ""),
                ]
            )
        )
        learnings = [
            r
            for r in records
            if isinstance(r, dict) and str(r.get("record_type") or "").startswith(DEPLOYMENT_LEARNING_PREFIX)
        ]
        learnings.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        for record in learnings:
            content = str(record.get("content") or "")
            if not query_terms.intersection(_lesson_terms(content)):
                continue
            rendered = _format_learning_content(content)
            if rendered in lessons:
                continue
            if not _append_lesson_with_budget(lessons, rendered):
                break
            if len(lessons) >= limit:
                break
    return lessons[:limit]


def build_learning_record(task: Dict[str, Any], outcome: Dict[str, Any]) -> Dict[str, Any]:
    """Pure: build the ``/memory`` payload distilling this run's outcome into a
    reusable deployment lesson."""
    project = _task_project(task)
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    origin = metadata.get("origin") if isinstance(metadata, dict) else {}
    repo_name = ""
    if isinstance(origin, dict):
        repo_name = str(origin.get("repository_name") or "")
    content = {
        "schema": "mac.deployment_learning.v1",
        "task_id": task.get("id"),
        "task_title": task.get("title"),
        "project": project,
        "repository": repo_name,
        "evidence_type": outcome.get("evidence_type"),
        "outcome": outcome.get("outcome"),
        "signals": outcome.get("signals", {}),
        "error_signature": outcome.get("error_signature") or "",
        "at": utcnow(),
    }
    return {
        "subject_type": "project",
        "subject_id": project,
        "record_type": "%s:%s" % (DEPLOYMENT_LEARNING_PREFIX, project),
        "content": json.dumps(content, sort_keys=True, separators=(",", ":")),
        "task_id": task.get("id"),
        "created_by": "mac-hermes-task-executor",
    }


def record_deployment_learning(task: Dict[str, Any], outcome: Dict[str, Any]) -> bool:
    """Persist a deployment lesson from this run (best-effort)."""
    return _hub_post("/memory", build_learning_record(task, outcome))


# ---------------------------------------------------------------------------
# Plan-outcome learning (plan-learn-01): planning runs feed the learning loop
# ---------------------------------------------------------------------------

_PLAN_LEARNING_SCHEMA = "mac.plan_learning.v1"


def build_plan_learning_record(
    task: Dict[str, Any],
    plan_manifest: Dict[str, Any],
    wall_clock_ms: float,
) -> Dict[str, Any]:
    """Pure: build the ``/memory`` payload for a completed planning run.

    Records plan-specific shape facts (children count, ordering rationale,
    child titles) in a ``mac.plan_learning.v1`` blob so a future planning
    run on a similar task can start from the same decomposition shape.
    """
    project = _task_project(task)
    children = plan_manifest.get("children") or []
    children_count = len(children) if isinstance(children, list) else 0
    children_titles = [
        str(c.get("title") or "")
        for c in (children if isinstance(children, list) else [])
        if isinstance(c, dict) and c.get("title")
    ]
    content = {
        "schema": _PLAN_LEARNING_SCHEMA,
        "task_id": task.get("id"),
        "task_title": task.get("title"),
        "project": project,
        "evidence_type": "plan_decomposed",
        "children_count": children_count,
        "children_titles": children_titles,
        "ordering_rationale": str(plan_manifest.get("ordering_rationale") or ""),
        "coverage_claim": str(plan_manifest.get("coverage_claim") or ""),
        "wall_clock_ms": int(wall_clock_ms),
        "at": utcnow(),
    }
    return {
        "subject_type": "project",
        "subject_id": project,
        "record_type": "%s:%s" % (DEPLOYMENT_LEARNING_PREFIX, project),
        "content": json.dumps(content, sort_keys=True, separators=(",", ":")),
        "task_id": task.get("id"),
        "created_by": "mac-hermes-task-executor",
    }


def record_plan_outcome(
    task: Dict[str, Any],
    task_workspace: "Path",
    wall_clock_ms: float,
) -> bool:
    """Read the plan manifest from task_workspace and record the plan outcome
    as a deployment_learning record.  Best-effort: returns False on any error.

    Called after a successful planning-phase run (evidence_type=plan_decomposed)
    so the next big migration on this project can recall the shape of the first.
    """
    manifest_path = task_workspace / "mac-evidence.json"
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(manifest, dict):
        return False
    if manifest.get("evidence_type") != "plan_decomposed":
        return False
    try:
        payload = build_plan_learning_record(task, manifest, wall_clock_ms)
        return _hub_post("/memory", payload)
    except Exception:  # noqa: BLE001
        return False


def _plan_family_terms(task: Dict[str, Any]) -> List[str]:
    """Extract 2-4 distinctive lowercase words from the task title/description
    to use as content_contains search terms for prior plan lessons.

    Prefers the nouns / objects in the title, skipping common stop-words and
    single-character tokens.  Returns an empty list when nothing meaningful
    can be extracted (fallback: caller skips content_contains search).
    """
    stop = {
        "a", "an", "the", "and", "or", "for", "of", "to", "in", "on",
        "at", "by", "as", "is", "be", "do", "it", "its", "with",
        "add", "fix", "build", "make", "run", "get", "set", "use",
        "task", "tasks", "from", "into", "this", "that", "each",
        "all", "new", "old", "can", "not", "has", "have", "are",
    }
    title = str(task.get("title") or "")
    desc = str(task.get("description") or "")[:300]
    raw = _re.findall(r"[a-z][a-z0-9_-]{2,}", (title + " " + desc).lower())
    seen: List[str] = []
    for tok in raw:
        if tok not in stop and tok not in seen:
            seen.append(tok)
        if len(seen) >= 4:
            break
    return seen


def _format_plan_learning_content(raw: str) -> str:
    """Render a stored ``mac.plan_learning.v1`` blob as a one-line lesson."""
    try:
        data = json.loads(raw)
    except Exception:
        return raw.strip()[:300]
    if not isinstance(data, dict) or data.get("schema") != _PLAN_LEARNING_SCHEMA:
        return ""
    title = str(data.get("task_title") or data.get("task_id") or "task")
    children_count = data.get("children_count", 0)
    children_titles = data.get("children_titles") or []
    ordering = str(data.get("ordering_rationale") or "").strip()
    parts = ["[plan] %s -> %d children" % (title, children_count)]
    if children_titles:
        parts.append("titles: %s" % "; ".join(children_titles[:5]))
    if ordering:
        parts.append("ordering: %s" % ordering[:120])
    return ". ".join(parts)[:400]


def recall_plan_lessons(task: Dict[str, Any], *, limit: int = 3) -> List[str]:
    """Recall prior decompositions for tasks similar to *task* (best-effort).

    Searches ``deployment_learning:<project>`` records whose content contains
    the task's family terms (the nouns / key objects from the title) so the
    second big migration on a project starts from the first one's shape.

    Returns short lesson strings; empty when the hub isn't reachable or no
    prior plan records exist.
    """
    from urllib.parse import urlencode

    project = _task_project(task)
    family_terms = _plan_family_terms(task)

    lessons: List[str] = []

    for term in (family_terms or [""]):
        params: Dict[str, Any] = {
            "subject_type": "project",
            "subject_id": project,
            "record_type": "%s:%s" % (DEPLOYMENT_LEARNING_PREFIX, project),
            "limit": 20,
        }
        if term:
            params["content_contains"] = term
        records = _hub_get("/memory?%s" % urlencode(params))
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            content = str(record.get("content") or "")
            # Only surface plan_learning records
            try:
                data = json.loads(content)
                if not isinstance(data, dict) or data.get("schema") != _PLAN_LEARNING_SCHEMA:
                    continue
            except Exception:
                continue
            rendered = _format_plan_learning_content(content)
            if not rendered or rendered in lessons:
                continue
            if not _append_lesson_with_budget(lessons, rendered):
                break
            if len(lessons) >= limit:
                return lessons[:limit]
        if len(lessons) >= limit:
            break

    return lessons[:limit]


_LESSON_CURATION_PROMPT = """You are the fleet's lesson curator. A task just finished; distill what the NEXT agent working on this project should know.

Task: {title}
Outcome: {outcome} (evidence_type={evidence_type})
Signals: {signals}
Failure hint: {error_signature}

The project already knows these lessons:
{existing}

Write 1-3 NEW lessons, ONE PER LINE, no bullets or numbering. Each lesson must be a single self-contained sentence under 250 characters stating a reusable, project-specific fact or pitfall (environment quirks, commands that worked/failed, gotchas). Ground every lesson in the outcome above - do not speculate. Do NOT restate or rephrase anything the project already knows; only add what is genuinely novel. If nothing NEW generalizes beyond this one task, output exactly: NOTHING
"""


def curate_lessons_from_outcome(
    task: Dict[str, Any], outcome: Dict[str, Any]
) -> List[str]:
    """LLM-curated lessons from a finished run (the Hermes background-review
    pattern, made outcome-grounded and fleet-shared).

    Hermes forks an LLM every N iterations to journal into per-host text files
    with no outcome signal; here the fork runs ONCE per task, is shown the
    VERIFIED outcome (tests/push/checks signals), and its lessons land in the
    HUB memory service as ``mac.deployment_learning.v1`` records - recalled by
    every agent on the project via the existing lesson recall, and promoted to
    the vector tier by the nap consolidator. Opt-in via
    MAC_LESSON_CURATION_ENABLED; router endpoint from
    MAC_ROUTER_URL/OPENAI_BASE_URL (the eval runner's seam). Best-effort: any
    failure returns [] and the run's outcome is unaffected."""
    if str(os.environ.get("MAC_LESSON_CURATION_ENABLED") or "").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return []
    router_url = str(
        os.environ.get("MAC_ROUTER_URL")
        or os.environ.get("MAC_ROUTER_INTERNAL_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or ""
    ).strip()
    model = str(
        os.environ.get("MAC_LESSON_CURATION_MODEL")
        or os.environ.get("MAC_TASK_MODEL")
        or os.environ.get("MAC_HERMES_GATEWAY_MODEL")
        or ""
    ).strip()
    if not router_url or not model:
        return []
    try:
        from mac.eval_runner import router_model_caller

        # Show the curator what the project already knows (v2 dedup): the first
        # live batch re-derived the same pushed=false insight across three
        # failures. Best-effort — recall failure just means an empty list.
        try:
            existing = recall_deployment_lessons(task, limit=8)
        except Exception:  # noqa: BLE001
            existing = []
        prompt = _LESSON_CURATION_PROMPT.format(
            existing="\n".join("- " + l for l in existing) or "- (none yet)",
            title=str(task.get("title") or "")[:200],
            outcome=outcome.get("outcome"),
            evidence_type=outcome.get("evidence_type"),
            signals=json.dumps(outcome.get("signals") or {}, sort_keys=True)[:400],
            error_signature=str(outcome.get("error_signature") or "none")[:200],
        )
        caller = router_model_caller(
            router_url, token=str(os.environ.get("MAC_API_TOKEN") or "")
        )
        answer, _cites, _ms = caller(model, prompt, "")
    except Exception:  # noqa: BLE001 - curation is advisory.
        return []
    lessons: List[str] = []
    for line in str(answer or "").splitlines():
        text = line.strip().strip("-*\u2022 ").strip()
        if not text or text.upper() == "NOTHING":
            continue
        lessons.append(text[:250])
        if len(lessons) >= 3:
            break
    return lessons


def record_curated_lessons(task: Dict[str, Any], outcome: Dict[str, Any]) -> int:
    """Persist LLM-curated lessons as deployment-learning records (best-effort).
    Returns the number recorded."""
    lessons = curate_lessons_from_outcome(task, outcome)
    recorded = 0
    for lesson in lessons:
        payload = build_learning_record(
            task,
            {
                "evidence_type": outcome.get("evidence_type"),
                "outcome": outcome.get("outcome"),
                "signals": {**(outcome.get("signals") or {}), "curated": True},
                "error_signature": lesson,
            },
        )
        if _hub_post("/memory", payload):
            recorded += 1
    return recorded


def classify_outcome(task_workspace: Path, task: Dict[str, Any], returncode: int) -> Dict[str, Any]:
    """Derive a compact, recall-friendly outcome from the final evidence
    manifest (read from disk) + the executor return code."""
    manifest: Dict[str, Any] = {}
    manifest_path = task_workspace / "mac-evidence.json"
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
        except Exception:
            manifest = {}
    evidence_type = str(manifest.get("evidence_type") or task_evidence_type(task))
    repo = manifest.get("repo") if isinstance(manifest.get("repo"), dict) else {}
    # verification.tests is canonically a LIST of result objects (mac-wjy3), but
    # accept a bare dict for backward compatibility with older manifests.
    tests_raw = manifest.get("tests")
    if isinstance(tests_raw, list):
        test_items = [t for t in tests_raw if isinstance(t, dict)]
    elif isinstance(tests_raw, dict):
        test_items = [tests_raw]
    else:
        test_items = []
    checks = manifest.get("checks") if isinstance(manifest.get("checks"), list) else []
    checks_pass = bool(checks) and all(
        (c.get("returncode", 0) == 0 or str(c.get("status", "")).lower() == "pass")
        for c in checks
        if isinstance(c, dict)
    )
    tests_state = None
    if test_items:
        tests_state = (
            "pass"
            if all(
                (t.get("returncode") == 0 or t.get("status") == "pass")
                for t in test_items
            )
            else "fail"
        )
    # ``repo`` is {} for non-repo evidence (operator_result/documentation/...);
    # in that case pushed/files_changed are N/A (None), NOT False — otherwise a
    # legitimate planning result would be mis-graded a failure.
    signals = {
        "returncode": returncode,
        "pushed": bool(repo.get("pushed")) if repo else None,
        "files_changed": len(repo.get("files_changed") or []) if repo else None,
        "tests": tests_state,
        "checks_pass": checks_pass if checks else None,
    }
    # Success: the run exited cleanly, evidence exists, and (where relevant)
    # it was pushed and tests/checks passed. Absent repo/checks don't fail it.
    success = (
        returncode == 0
        and bool(manifest)
        and tests_state != "fail"
        and (checks_pass if checks else True)
        and (signals["pushed"] is not False)
    )
    return {
        "evidence_type": evidence_type,
        "outcome": "success" if success else "failure",
        "signals": signals,
        "error_signature": "" if success else _error_signature(manifest),
    }


def _error_signature(manifest: Dict[str, Any]) -> str:
    """A short, secret-free failure hint for the lesson (first failing check or
    the manifest summary)."""
    for check in manifest.get("checks") or []:
        if isinstance(check, dict) and check.get("status") == "fail":
            return ("check:%s rc=%s" % (check.get("name"), check.get("returncode")))[:200]
    return str(manifest.get("summary") or "")[:200]


# ---------------------------------------------------------------------------
# Prompt construction (extracted from the heredoc's main(), now testable)
# ---------------------------------------------------------------------------


def repository_contract_section(task: Dict[str, Any]) -> str:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    origin = metadata.get("origin") if isinstance(metadata, dict) else {}
    origin = origin if isinstance(origin, dict) else {}
    contract = origin.get("repository_contract")
    if not isinstance(contract, dict):
        # No build/test contract attached. Distinguish two cases:
        #  (a) a checkout still exists (repository_url/path set) — this is a
        #      repository *onboarding* task whose JOB is to author the contract,
        #      so "report a contract failure" would be exactly wrong; and
        #  (b) no repository at all — then a missing contract is a real failure.
        has_checkout = bool(
            str(origin.get("repository_url") or "").strip()
            or str(origin.get("repository_path") or "").strip()
        )
        if has_checkout:
            return "\n".join(
                [
                    "No repository runtime contract exists yet — this is a repository ONBOARDING task and authoring that contract is part of the deliverable.",
                    "MAC has prepared a clean, writable checkout for you at $MAC_TASK_REPO_WORKTREE (a task branch off the default branch).",
                    "Work entirely inside that checkout. The goal is to UNDERSTAND the repository, not to change its runtime behavior:",
                    "  1. Explore the tree: README/docs, build files and package manifests, CI config, entry points, and the test layout.",
                    "  2. Infer the supported platforms, the required toolchain commands, the bootstrap/setup command, and the canonical test command — only from what the repo actually declares; do not invent commands.",
                    "  3. Author a repository contract at .mac/project.yaml in the checkout using schema mac.repository_contract.v1 with keys: schema, project, platforms, toolchain.required_commands, bootstrap.command, test.command, evidence.required.",
                    "  4. If codegraph is available, run codegraph init for local API/code behavior analysis. Treat .codegraph/ as generated local state, not a deliverable.",
                    "Do NOT push or open a PR — the authored .mac/project.yaml is a local analysis artifact. Include its full content and your architecture summary + prioritized backlog in the evidence (evidence_type=investigation).",
                ]
            )
        return (
            "No repository runtime contract is attached and no checkout was provided. "
            "Do not guess bootstrap or test commands; report this as a task contract failure."
        )
    toolchain = contract.get("toolchain") if isinstance(contract.get("toolchain"), dict) else {}
    bootstrap = contract.get("bootstrap") if isinstance(contract.get("bootstrap"), dict) else {}
    test = contract.get("test") if isinstance(contract.get("test"), dict) else {}
    required_commands = [
        str(item).strip()
        for item in (toolchain.get("required_commands") or [])
        if str(item).strip()
    ]
    summary = "; ".join(
        item
        for item in (
            "project=%s" % contract.get("project") if contract.get("project") else "",
            "required_commands=%s" % ",".join(required_commands) if required_commands else "",
            "bootstrap=%s" % bootstrap.get("command") if bootstrap.get("command") else "",
            "test=%s" % test.get("command") if test.get("command") else "",
        )
        if item
    )
    return "\n".join(
        [
            "Repository contract summary: %s" % (summary or "see task.json"),
            "The complete repository and execution contracts remain in task.json; read them there when more detail is needed.",
            "For normal repository tasks, MAC prepares a task-owned git worktree before the executor starts.",
            "Use $MAC_TASK_REPO_WORKTREE, or metadata.runtime.repository_worktree in task.json, as the only writable checkout.",
            "Treat origin.repository_path / $MAC_TASK_REPO_SOURCE as read-only registered source state; do not edit it for feature or bug work.",
            "The registered checkout must remain clean. Modify and test the task worktree, but do not fetch, rebase, commit, push, or open a PR from the agent process.",
            "The deterministic host finalizer owns canonical freshness, the Git commit, and publication after it harvests the agent's file changes. Report changed files and checks in preliminary evidence; host-finalized evidence supplies the pushed ref.",
            "Only explicit source-remediation tasks may repair origin.repository_path directly.",
            "Before build or test work, run bootstrap.command from the repository root when the declared tools or bootstrap.creates outputs are missing.",
            "Use test.command as the canonical verification command unless the task explicitly narrows the check.",
            "For source, build, dependency, or runtime config changes, run CodeGraph before final evidence: codegraph init or codegraph sync, codegraph affected <changed-files>, and codegraph impact/callers/callees for changed public APIs when applicable. Record the result under codegraph in mac-evidence.json.",
        ]
    )


def task_evidence_type(task: Dict[str, Any]) -> str:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    contract = metadata.get("execution_contract") if isinstance(metadata, dict) else {}
    evidence_type = str(contract.get("evidence_type") or "").strip().lower() if isinstance(contract, dict) else ""
    allowed = {
        "repo_change",
        "documentation",
        "investigation",
        "deployment",
        "test",
        "artifact",
        "no_change",
        "operator_result",
    }
    if evidence_type in allowed:
        return evidence_type
    if task_is_repo_coupled(task):
        return "repo_change"
    return "operator_result"


def task_is_repo_coupled(task: Dict[str, Any]) -> bool:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    if not isinstance(metadata, dict):
        return False
    # A declared report/answer task is non-code: it must not be forced into the
    # repo-change contract (which demands a diff + passing test), and the
    # executor's operator_result fallback is what should fire for it.
    if metadata_declares_report_deliverable(metadata):
        return False
    contract = metadata.get("execution_contract")
    if isinstance(contract, dict):
        if str(contract.get("type") or "").strip().lower() == "repository":
            return True
        if contract.get("repository_required") is True:
            return True
        if isinstance(contract.get("repository_contract"), dict):
            return True
    origin = metadata.get("origin")
    if isinstance(origin, dict) and isinstance(origin.get("repository_contract"), dict):
        return True
    return isinstance(metadata.get("repository_contract"), dict)


def _nested_dict(root: Dict[str, Any], *path: str) -> Dict[str, Any]:
    node: Any = root
    for key in path:
        if not isinstance(node, dict):
            return {}
        node = node.get(key)
    return node if isinstance(node, dict) else {}


def _repository_contract_test_command(task: Dict[str, Any]) -> str:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    if not isinstance(metadata, dict):
        return ""
    candidates = [
        _nested_dict(metadata, "execution_contract", "test"),
        _nested_dict(metadata, "execution_contract", "repository_contract", "test"),
        _nested_dict(metadata, "origin", "repository_contract", "test"),
        _nested_dict(metadata, "repository_contract", "test"),
    ]
    for candidate in candidates:
        command = str(candidate.get("command") or "").strip()
        if command:
            return command
    return ""


def _repository_contract_canonical_remote(task: Dict[str, Any]) -> str:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    if not isinstance(metadata, dict):
        return ""
    candidates = [
        _nested_dict(metadata, "execution_contract", "repository_contract"),
        _nested_dict(metadata, "origin", "repository_contract"),
        _nested_dict(metadata, "repository_contract"),
    ]
    for candidate in candidates:
        remote = str(candidate.get("canonical_remote_url") or "").strip()
        if remote:
            return remote
    return ""


def _repository_contract_canonical_branch(task: Dict[str, Any]) -> str:
    """Return the canonical branch from the task contract, or empty string if absent.

    Precedence mirrors worker.py: execution_contract > origin > runtime context.
    Callers that resolve a fallback (e.g. from env or default) must do so themselves.
    """
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    if not isinstance(metadata, dict):
        return ""
    candidates = [
        _nested_dict(metadata, "execution_contract", "repository_contract"),
        _nested_dict(metadata, "origin", "repository_contract"),
        _nested_dict(metadata, "repository_contract"),
    ]
    for candidate in candidates:
        branch = str(candidate.get("default_branch") or "").strip()
        if branch:
            return branch
    # Also check runtime context (written by worker preparation).
    runtime_raw = metadata.get("runtime")
    runtime: Dict[str, Any] = runtime_raw if isinstance(runtime_raw, dict) else {}
    branch = str(runtime.get("repository_canonical_branch") or "").strip()
    if branch:
        return branch
    return os.environ.get("MAC_TASK_REPO_DEFAULT_BRANCH", "").strip()


def _repository_publication_remote(task: Dict[str, Any]) -> str:
    canonical = _repository_contract_canonical_remote(task)
    if canonical:
        return canonical
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    origin = metadata.get("origin") if isinstance(metadata, dict) else {}
    if isinstance(origin, dict):
        remote = str(origin.get("repository_url") or "").strip()
        if remote:
            return remote
    runtime = metadata.get("runtime") if isinstance(metadata, dict) else {}
    if isinstance(runtime, dict):
        remote = str(runtime.get("repository_canonical_remote_url") or "").strip()
        if remote:
            return remote
    return os.environ.get("MAC_TASK_CANONICAL_REMOTE", "").strip()


def _repository_prepared_base(task: Dict[str, Any]) -> str:
    value = os.environ.get("MAC_TASK_REPO_BASE_SHA", "").strip()
    if value:
        return value
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    runtime = metadata.get("runtime") if isinstance(metadata, dict) else {}
    return str(runtime.get("repository_base_sha") or "").strip() if isinstance(runtime, dict) else ""


def _repository_lease_id(task: Dict[str, Any]) -> str:
    value = os.environ.get("MAC_TASK_REPO_LEASE_ID", "").strip()
    if value:
        return value
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    runtime = metadata.get("runtime") if isinstance(metadata, dict) else {}
    return str(runtime.get("repository_lease_id") or "").strip() if isinstance(runtime, dict) else ""


def _repository_contract_bootstrap(task: Dict[str, Any]) -> Dict[str, Any]:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    if not isinstance(metadata, dict):
        return {}
    candidates = [
        _nested_dict(metadata, "execution_contract", "bootstrap"),
        _nested_dict(metadata, "execution_contract", "repository_contract", "bootstrap"),
        _nested_dict(metadata, "origin", "repository_contract", "bootstrap"),
        _nested_dict(metadata, "repository_contract", "bootstrap"),
    ]
    for candidate in candidates:
        command = str(candidate.get("command") or "").strip()
        if command:
            return {
                "command": command,
                "creates": [
                    str(item).strip()
                    for item in (candidate.get("creates") or [])
                    if str(item).strip()
                ],
            }
    return {}


def _repository_bootstrap_timeout() -> float:
    raw = (
        os.environ.get("MAC_WORKER_REPOSITORY_BOOTSTRAP_TIMEOUT")
        or os.environ.get("MAC_WORKER_REPOSITORY_TEST_TIMEOUT")
        or "1800"
    )
    try:
        value = float(raw)
        return value if value > 0 else 1800.0
    except ValueError:
        return 600.0


def _run_repository_bootstrap_if_needed(
    worktree_path: Path,
    task: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    bootstrap = _repository_contract_bootstrap(task)
    command = str(bootstrap.get("command") or "").strip()
    if not command:
        return None
    creates = bootstrap.get("creates") if isinstance(bootstrap.get("creates"), list) else []
    missing = [
        path
        for path in creates
        if not (worktree_path / str(path)).exists()
    ]
    if creates and not missing:
        return {
            "command": command,
            "creates": creates,
            "returncode": 0,
            "status": "skipped",
            "reason": "declared bootstrap outputs already exist",
        }
    started = time.time()
    try:
        completed = subprocess.run(
            ["bash", "-lc", command],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=False,
            timeout=_repository_bootstrap_timeout(),
        )
        return {
            "command": command,
            "creates": creates,
            "missing_before": missing,
            "returncode": int(completed.returncode),
            "status": "pass" if completed.returncode == 0 else "fail",
            "stdout": clip_process_text(completed.stdout or ""),
            "stderr": clip_process_text(completed.stderr or ""),
            "duration_ms": int((time.time() - started) * 1000),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "creates": creates,
            "missing_before": missing,
            "returncode": 124,
            "status": "fail",
            "stdout": clip_process_text(exc.stdout) if isinstance(exc.stdout, str) else "",
            "stderr": clip_process_text(exc.stderr) if isinstance(exc.stderr, str) else "",
            "duration_ms": int((time.time() - started) * 1000),
            "error": "bootstrap command timed out",
        }


def _lessons_section(lessons: List[str]) -> str:
    """Render recalled deployment lessons as a prompt section (or empty)."""
    if not lessons:
        return ""
    bullets = "\n".join("- %s" % line for line in lessons)
    return (
        "Lessons from prior runs on this project (hindsight from the fleet's memory — "
        "apply what's relevant, ignore what isn't):\n%s" % bullets
    )


def _cooperative_integration_section(task: Dict[str, Any]) -> str:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    coordination = metadata.get("coordination") if isinstance(metadata, dict) else {}
    if not isinstance(coordination, dict) or coordination.get("phase") != "integration":
        return ""
    outputs = coordination.get("child_outputs")
    if not isinstance(outputs, list) or not outputs:
        return ""
    return "\n".join(
        [
            "Cooperative integration contract:",
            "This is the mandatory fan-in pass for independently executed child tasks.",
            "Treat every child output below as an explicit input. Fetch and merge each exact remote_ref/head_sha into this task's integration branch; do not squash, cherry-pick, or merely summarize the children because the final review verifies commit ancestry.",
            "Resolve conflicts, run the repository's complete test contract and CodeGraph on the combined result, and produce new executor evidence for the integrated commit.",
            "If any required child output is missing or cannot be integrated, fail closed and identify that child instead of claiming completion.",
            "Child outputs (JSON):\n%s"
            % json.dumps(outputs, indent=2, sort_keys=True),
        ]
    )


# Marker lines the executor asks the coding agent / reviewer to wrap its plain
# recap in, so the worker can capture a clean human summary for the per-task
# activity log (mac task summary) instead of scraping raw stdout. The worker's
# _extract_marked_summary matches these tolerantly (see worker.py).
MAC_TASK_SUMMARY_BEGIN = "=== MAC TASK SUMMARY ==="
MAC_TASK_SUMMARY_END = "=== END MAC TASK SUMMARY ==="


def build_task_prompt(task: Dict[str, Any], task_file: Path, lessons: Optional[List[str]] = None) -> str:
    parts = [
        "You are running as a MAC fleet worker. Complete the assigned task from first principles.",
        "You are AUTONOMOUS: never ask the operator for confirmation or permission. Make a reasonable assumption, proceed, and record it when necessary.",
        "First read the versioned execution policy at $MAC_TASK_WORKSPACE/.mac-executor-policy.txt, then read task.json as the source of truth.",
        "Repository tasks default to evidence_type=repo_change; use operator_result only when no repository contract exists. Deterministic host code enforces tests, CodeGraph, cleanliness, and publication.",
        "Repository runtime contract:\n%s" % repository_contract_section(task),
    ]
    integration_section = _cooperative_integration_section(task)
    if integration_section:
        parts.append(integration_section)
    parts.append(
        "Finally, for the per-task activity log, print a short plain-language recap "
        "of what you did and how you verified it (1-3 sentences, no code or diff), "
        "wrapped EXACTLY in these two marker lines:\n%s\n<your recap here>\n%s"
        % (MAC_TASK_SUMMARY_BEGIN, MAC_TASK_SUMMARY_END)
    )
    plan_section = _plan_detection_section(task)
    if plan_section:
        parts.append(plan_section)
    lessons_section = _lessons_section(lessons or [])
    if lessons_section:
        parts.append(lessons_section)
    parts.append("Read the full task from: %s" % str(task_file))
    return "\n\n".join(parts)


def build_review_prompt(task: Dict[str, Any], task_workspace: Path, review_context: Dict[str, Any]) -> str:
    parts = [
            "You are running as a MAC fleet reviewer. Review the executor's work independently.",
            "Use the workspace files as the source of truth. Preserve secrets and do not print bearer tokens.",
            "Decide whether the executor evidence actually proves the task was completed and verified.",
            "Approve only when the evidence is coherent, pushed/published when required, and the checks are passing. Reject unverifiable, local-only, failing, or mismatched work.",
            "If MAC_TASK_REPO_WORKTREE is set, use that local review checkout for independent build/test work; it is prepared from the executor evidence remote/ref/head and is safe for review commands.",
            "For repository changes, build the review checkout and run the repository contract test command or the task's declared tests before approving. Look for failures introduced by the change, not just manifest shape.",
            "For source, build, dependency, or runtime config changes, run CodeGraph in the review checkout before approving. Include codegraph in the review verdict; use impact/callers/callees for changed public APIs when applicable.",
            "When you finish, report concise findings and write a review verdict manifest to $MAC_TASK_WORKSPACE/mac-evidence.json.",
            "Use schema mac.worker_evidence.v1 with status=complete, evidence_type=review_verdict, verdict=approved or rejected, reviewed_evidence_id=%s, and review_id=%s."
            % (review_context.get("executor_evidence_id", ""), review_context.get("review_id", "")),
            'A review verdict must also include repo copied from the executor verification repo object, with the same repo.head_sha, plus at least one independent passing check as checks=[{"name":"...","returncode":0}] or status="pass".',
            "Include worktree_digest as sha256:<64 lowercase hex chars>. If you cannot independently verify the executor result, write verdict=rejected and explain the blocker instead of omitting repo/check fields.",
            "Read the original task from executor-task.json and the executor evidence from executor-evidence.json in your workspace (%s)." % str(task_workspace),
            "Finally, for the per-task activity log, print a short plain-language recap "
            "of what you checked and found and whether you'd approve and why (1-3 "
            "sentences, no code or diff), wrapped EXACTLY in these two marker lines:\n"
            "%s\n<your recap here>\n%s" % (MAC_TASK_SUMMARY_BEGIN, MAC_TASK_SUMMARY_END),
        ]
    assignment = _review_experiment_assignment(task)
    if assignment:
        if assignment.get("blind"):
            parts.insert(
                -1,
                "This task is the adjudication phase of blind review experiment %s "
                "(arm %s). The host already ran a discovery pass while "
                "executor-evidence.json was physically withheld. Read "
                "review-independent-findings.json first, then read the executor "
                "evidence. Preserve, refine, or explicitly rebut those findings "
                "in the final findings/feedback; do not silently discard them."
                % (assignment.get("experiment_id"), assignment.get("arm")),
            )
        else:
            parts.insert(
                -1,
                "This review is assigned to experiment %s (arm %s, standard "
                "evidence-aware protocol)."
                % (assignment.get("experiment_id"), assignment.get("arm")),
            )
    return "\n\n".join(parts)


def _review_experiment_assignment(task: Dict[str, Any]) -> Dict[str, Any]:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    assignment = metadata.get("review_experiment") if isinstance(metadata, dict) else {}
    if not isinstance(assignment, dict):
        return {}
    if assignment.get("schema") != "mac.review_experiment.v1":
        return {}
    if not str(assignment.get("experiment_id") or "").strip():
        return {}
    if not str(assignment.get("arm") or "").strip():
        return {}
    return dict(assignment)


def build_blind_review_discovery_prompt(
    task: Dict[str, Any], task_workspace: Path, assignment: Dict[str, Any]
) -> str:
    """Prompt the pre-evidence pass whose treatment is enforced by the host."""
    return "\n\n".join(
        [
            "You are running the discovery phase of a blind MAC fleet review.",
            "The host has physically withheld executor-evidence.json for this phase. Do not look for it, infer its claims, or write a final approval/rejection verdict yet.",
            "Read executor-task.json, inspect the prepared review checkout, its diff and relevant call paths, and run focused checks needed to identify defects or missing requirements independently of the executor's explanation.",
            "Record the result in %s/review-independent-findings.json using schema mac.independent_review_findings.v1. Include experiment_id=%s, arm=%s, findings as a JSON list, and no_findings_reason as a non-empty string when findings is empty. Each finding should have a concise summary and, when applicable, severity, path, line, and supporting check."
            % (str(task_workspace), assignment.get("experiment_id"), assignment.get("arm")),
            "Do not create mac-evidence.json in this discovery phase. The host will restore executor evidence and run a separate adjudication phase after this pass.",
            "Read the original task from %s/executor-task.json." % str(task_workspace),
        ]
    )


def _read_json_object(path: Path, *, max_bytes: int = 1024 * 1024) -> Dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _blind_review_protocol(
    task_workspace: Path,
    assignment: Dict[str, Any],
    result: Any,
    *,
    duration_ms: float,
    evidence_hidden: bool,
) -> Dict[str, Any]:
    findings_path = task_workspace / "review-independent-findings.json"
    independent = _read_json_object(findings_path)
    raw = (
        findings_path.read_bytes()
        if findings_path.is_file() and findings_path.stat().st_size <= 1024 * 1024
        else b""
    )
    findings = independent.get("findings") if isinstance(independent.get("findings"), list) else []
    no_findings_reason = str(independent.get("no_findings_reason") or "").strip()
    valid_findings = (
        independent.get("schema") == "mac.independent_review_findings.v1"
        and str(independent.get("experiment_id") or "").strip()
        == str(assignment.get("experiment_id") or "").strip()
        and str(independent.get("arm") or "").strip()
        == str(assignment.get("arm") or "").strip()
        and (bool(findings) or bool(no_findings_reason))
    )
    return {
        "schema": "mac.review_protocol.v1",
        "experiment_id": assignment.get("experiment_id"),
        "arm": assignment.get("arm"),
        "mode": "blind_discovery_then_adjudication",
        "executor_evidence_hidden": bool(evidence_hidden),
        "discovery_returncode": int(getattr(result, "returncode", 1)),
        "discovery_duration_ms": round(float(duration_ms), 3),
        "discovery_stdout_sha256": sha256_text(getattr(result, "stdout", "") or ""),
        "discovery_stderr_sha256": sha256_text(getattr(result, "stderr", "") or ""),
        "independent_findings_valid": valid_findings,
        "independent_findings_count": len(findings),
        "independent_findings_sha256": (
            "sha256:" + hashlib.sha256(raw).hexdigest() if raw else ""
        ),
        "protocol_compliant": bool(
            evidence_hidden
            and valid_findings
            and int(getattr(result, "returncode", 1)) == 0
        ),
        "recorded_at": utcnow(),
    }


# ---------------------------------------------------------------------------
# Deterministic finalizers + fail-closed fallback (ported, behavior preserved)
# ---------------------------------------------------------------------------

def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def run_deterministic_git_finalizer(task_workspace: Path, task: Dict[str, Any]) -> None:
    """mac-jfns: deterministic repo_change evidence from REAL git state for
    tasks declaring publication_target=git://main."""
    metadata = task.get("metadata") or {}
    publication_target = str(metadata.get("publication_target") or "").strip()
    if not publication_target.startswith("git://"):
        return
    worktree = os.environ.get("MAC_TASK_REPO_WORKTREE", "").strip()
    if not worktree:
        rt = metadata.get("runtime") if isinstance(metadata.get("runtime"), dict) else {}
        worktree = str(rt.get("repository_worktree") or "").strip()
    worktree_path = Path(worktree).expanduser() if worktree else None
    if not worktree_path or not worktree_path.is_dir() or not (worktree_path / ".git").exists():
        return
    status = _git(["status", "--porcelain"], worktree_path)
    if status.stdout.strip():
        _git(["add", "-A"], worktree_path)
        commit_msg = "auto-commit: %s" % task.get("id", "unknown")
        _git(
            ["-c", "user.email=mac-fleet@nvidia.com", "-c", "user.name=MAC fleet", "commit", "-m", commit_msg],
            worktree_path,
        )
    head_sha = _git(["rev-parse", "HEAD"], worktree_path).stdout.strip()
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path).stdout.strip() or "HEAD"
    # Rebase onto the advanced canonical tip BEFORE the contract test runs, so
    # the suite validates the projected published tree. Fleet agents race each
    # other to one canonical branch; a task that took an hour almost always
    # finds main moved, and without this it dies at the publication freshness
    # gate after all its work passed. Clean rebases only — a conflict aborts
    # and the existing gate reports its precise error.
    canonical_sync = sync_worktree_with_canonical(
        worktree_path,
        _repository_publication_remote(task),
        _repository_contract_canonical_branch(task),
    )
    if canonical_sync.get("status") == "rebased":
        head_sha = _git(["rev-parse", "HEAD"], worktree_path).stdout.strip()
    # Purge synced build artifacts before the host build. The agent built in the
    # task SANDBOX (e.g. Linux); those object files / binaries sync back into this
    # worktree, but this finalizer runs on the EXECUTOR HOST, which may be a
    # different OS/arch (a macOS host with a Linux sandbox). A stale foreign
    # bin/nano makes `..._if_needed` skip the rebuild and then the tests run a
    # binary the host can't execute -> spurious "tests failed". `git clean -Xdf`
    # removes only gitignored files (obj/, bin/, caches) and keeps the agent's
    # new untracked SOURCE files, forcing a clean native rebuild.
    _git(["clean", "-Xdf"], worktree_path)
    bootstrap = _run_repository_bootstrap_if_needed(worktree_path, task)
    test_cmd = (_repository_contract_test_command(task) or "scripts/run-contract-tests.sh").strip()
    tests = None
    if test_cmd:
        # Progress-based: kills on output stall, not on a total-runtime guess
        # that goes stale every time legitimate work grows.
        tr = run_with_stall_watchdog(["bash", "-lc", test_cmd], worktree_path)
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
    bootstrap_ok = bootstrap is None or bootstrap.get("returncode") == 0
    tests_ok = tests is None or tests.get("returncode") == 0
    canonical_remote_raw = _repository_publication_remote(task)
    canonical_branch = _repository_contract_canonical_branch(task)
    prepared_base_sha = _repository_prepared_base(task)
    lease_id = _repository_lease_id(task)
    destination_branch = branch if branch != "HEAD" else ""
    publication_target = None
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
        )
        freshness = check_canonical_freshness(publication_target)
    except (OSError, ValueError) as exc:
        freshness = CanonicalFreshnessResult(
            False,
            publication_target,
            head_sha=head_sha,
            error=str(exc),
        )
    freshness_error: Optional[str] = None if freshness.ok else freshness.error
    base_sha = freshness.canonical_tip_sha or prepared_base_sha
    files_changed = list(freshness.files_changed)
    # Record the diff base (canonical tip) so the reviewer can compute a
    # non-empty base..head diff. Without base_sha the review snapshot's
    # files_changed is always [] (which the repo_change validator rejects).
    codegraph = run_codegraph_audit(worktree_path, files_changed)
    codegraph_problems = codegraph_audit_manifest_problems(
        {"repo": {"files_changed": files_changed}, "codegraph": codegraph}
    )
    codegraph_ok = not codegraph_problems
    final_status = _git(["status", "--porcelain"], worktree_path).stdout.strip()
    clean = not bool(final_status)
    freshness_ok = freshness_error is None
    pushed = False
    publication: Optional[CanonicalFreshnessResult] = None
    push_remote_display = (
        freshness.target.remote_display if freshness.target is not None else ""
    )
    if bootstrap_ok and tests_ok and codegraph_ok and clean and freshness_ok:
        assert publication_target is not None
        publication = guarded_push(publication_target)
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
    (task_workspace / "mac-evidence.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
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
        or os.environ.get("MAC_WORKER_AGENT_ID")
        or os.environ.get("MAC_AGENT_ID")
        or ""
    ).strip()
    attestation_key = (os.environ.get("MAC_ATTESTATION_KEY") or "").strip()
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
    exec_head = str(exec_repo.get("head_sha") or "").strip()
    repo_review = bool(exec_head)
    review_worktree = os.environ.get("MAC_TASK_REPO_WORKTREE", "").strip()
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
    if repo_review and review_worktree and Path(review_worktree).is_dir():
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
    digest_input = ("%s|%s|%s" % (exec_head, exec_repo.get("remote_ref") or "", verdict)).encode("utf-8")
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
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _hermes_python() -> str:
    """Resolve the interpreter that can import ``hermes_cli``.

    Host mode: the vendored Hermes runtime under ``~/.mac`` — set PYTHONPATH so
    ``hermes_cli`` resolves. Sandbox image mode: ``MAC_HERMES_PYTHON`` points at
    the in-image interpreter (e.g. ``/opt/mac-venv/bin/python``) whose
    site-packages already contain ``hermes_cli``, so no host PYTHONPATH is
    injected — and the host path would not exist inside the sandbox anyway.
    """
    override = (os.environ.get("MAC_HERMES_PYTHON") or "").strip()
    if override:
        return override
    hermes_py = str(Path.home() / ".mac" / "venv" / "bin" / "python")
    hermes_vendored = str(Path.home() / ".mac" / "src" / "mac" / "src" / "mac" / "_hermes")
    os.environ["PYTHONPATH"] = hermes_vendored + os.pathsep + os.environ.get("PYTHONPATH", "")
    return hermes_py


def _hermes_argv(prompt: str) -> List[str]:
    """The vendored-Hermes-runtime agent invocation (the fallback runner)."""
    argv = [_hermes_python(), "-m", "hermes_cli.main", "chat", "--query", prompt, "--quiet", "--accept-hooks", "--yolo"]
    # Per-task model override (task metadata.model, exported by the worker as
    # MAC_TASK_MODEL). Wins over the deployed gateway default for this run
    # only; llm.route records requested/resolved model so the pin is auditable.
    task_model = (os.environ.get("MAC_TASK_MODEL") or "").strip()
    if task_model:
        argv += ["--model", task_model]
    task_max_iterations = (
        os.environ.get("MAC_TASK_MAX_ITERATIONS") or ""
    ).strip()
    if task_max_iterations.isdigit() and 1 <= int(task_max_iterations) <= 500:
        argv += ["--max-turns", task_max_iterations]
    return argv


@dataclass(frozen=True)
class _AgentCommandBundle:
    workspace: Path
    prompt_file: Path
    command_file: Path
    policy_file: Path
    interpreter: str

    def argv(self, *, sandbox_workspace: Optional[str] = None) -> List[str]:
        if sandbox_workspace:
            command_file = "%s/%s" % (sandbox_workspace.rstrip("/"), self.command_file.name)
            prompt_file = "%s/%s" % (sandbox_workspace.rstrip("/"), self.prompt_file.name)
            interpreter = (
                os.environ.get("MAC_HERMES_PYTHON") or "/opt/mac-venv/bin/python"
            )
        else:
            command_file = str(self.command_file)
            prompt_file = str(self.prompt_file)
            interpreter = self.interpreter
        return [
            interpreter,
            "-m",
            "mac.agent_command",
            "--command-file",
            command_file,
            "--prompt-file",
            prompt_file,
        ]

    def cleanup(self) -> None:
        self.command_file.unlink(missing_ok=True)
        self.prompt_file.unlink(missing_ok=True)
        self.policy_file.unlink(missing_ok=True)


def _write_agent_command_bundle(
    workspace: Path, prompt: str, agent_argv: List[str]
) -> _AgentCommandBundle:
    import uuid

    if agent_argv.count(PROMPT_SENTINEL) != 1:
        raise ValueError("agent argv must contain exactly one private-prompt sentinel")
    workspace.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex
    prompt_file = workspace / (".mac-agent-prompt-%s" % nonce)
    command_file = workspace / (".mac-agent-command-%s.json" % nonce)
    policy_file = workspace / ".mac-executor-policy.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    command_file.write_text(
        json.dumps({"schema": "mac.agent_command.v1", "argv": agent_argv}),
        encoding="utf-8",
    )
    prompt_file.chmod(0o600)
    command_file.chmod(0o600)
    policy_file.write_text(
        (Path(__file__).resolve().parent / "executor-policy.txt").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    policy_file.chmod(0o600)
    interpreter = agent_argv[0] if agent_argv[1:3] == ["-m", "hermes_cli.main"] else sys.executable
    return _AgentCommandBundle(
        workspace=workspace,
        prompt_file=prompt_file,
        command_file=command_file,
        policy_file=policy_file,
        interpreter=interpreter,
    )


def _mcp_serve_argv() -> List[str]:
    """The vendored messaging MCP server command (registered with a coding-agent
    CLI for messaging-tool parity, where the CLI supports per-invocation MCP)."""
    return [_hermes_python(), "-m", "hermes_cli.main", "mcp", "serve"]


# ---------------------------------------------------------------------------
# OpenShell sandbox wrapping (sandbox-01)
#
# The agent already runs ``--yolo`` (Hermes' own permission/approval prompts are
# bypassed). On its own that is unguarded. When OpenShell sandboxing is enabled
# the (still ``--yolo``) Hermes invocation is launched as a *confined child* of
# an OpenShell sandbox, which then becomes the SOLE guardrail authority:
#   * Landlock  — filesystem confinement to declared paths
#   * seccomp   — syscall filtering + privilege drop (never runs as root)
#   * egress    — deny-by-default network proxy driven by a declarative policy
# The policy YAML (MAC_OPENSHELL_POLICY) *is* the guardrail specification.
#
# Default OFF: with MAC_OPENSHELL_SANDBOX unset/false ``_maybe_wrap_openshell``
# returns the argv unchanged, so the executor behaves exactly as before. The
# wrap is a pure argv transform — it does not itself require OpenShell to be
# installed; that is the deployer's responsibility (see
# docs/openshell-sandbox.md).
#
# Knobs (read at wrap time — nothing is frozen at import):
#   MAC_OPENSHELL_SANDBOX          truthy -> enable wrapping
#   MAC_OPENSHELL_BIN             openshell binary (default "openshell")
#   MAC_OPENSHELL_POLICY          explicit policy YAML path (the guardrail spec).
#                                 When unset, the wrap resolves a policy in this
#                                 order and ALWAYS passes one (never the OpenShell
#                                 image default): explicit -> ~/.mac/openshell-
#                                 policy.yaml -> bundled fail-closed default
#                                 (src/mac/openshell/default-policy.yaml).
#   MAC_OPENSHELL_SANDBOX_NAME    fixed sandbox name (debug; default: ephemeral)
#   MAC_OPENSHELL_KEEP            truthy -> --keep (debug; default one-shot teardown)
#   MAC_OPENSHELL_CREATE_ARGS     extra `sandbox create` args (shell-split), e.g.
#                                 "--from my-image" or "--upload /src:/src" used to
#                                 make the Hermes runtime + workspace available
#                                 inside the sandbox
#   MAC_OPENSHELL_ENV_PASSTHROUGH comma list of env names copied through a
#                                 private mode-0600 workspace file
#   MAC_ALLOW_UNSANDBOXED_YOLO    truthy (default "1") -> allow --yolo with no
#                                 sandbox (current fleet, logs a warning). Set
#                                 "0" to fail closed: refuse unguarded YOLO so
#                                 --yolo is only ever used inside the sandbox.
# ---------------------------------------------------------------------------

# Forward the env the agent needs to reach the hub + model gateway from inside
# the sandbox. (Network reachability is still gated by the OpenShell policy;
# this only makes the values visible to the process.)
_DEFAULT_OPENSHELL_ENV_PASSTHROUGH = (
    "MAC_HUB_URL,MAC_URL,MAC_WORKER_TOKEN,MAC_TOKEN,MAC_API_TOKEN,"
    "MAC_WORKER_AGENT_ID,MAC_WORKER_AGENT_NAME,MAC_AGENT_ID,MAC_TASK_ID,MAC_LEASE_ID,"
    "HERMES_GATEWAY_BASE_URL,HERMES_GATEWAY_MODEL,HERMES_SESSION_KEY,HERMES_YOLO_MODE,"
    # Model-gateway base_url + api_key live in the agent's ~/.hermes/.env, which
    # is NOT in the sandbox image; the gateway requires auth. Forward them so the
    # sandboxed hermes can authenticate (the *_BASE_URL values have their host
    # loopback rewritten to the sandbox host alias in the private env file).
    "MAC_HERMES_GATEWAY_BASE_URL,MAC_HERMES_GATEWAY_API_KEY,MAC_HERMES_GATEWAY_PROVIDER,"
    "OPENAI_BASE_URL,OPENAI_API_KEY,"
    # Coding-agent CLI credentials (see mac.coding_agent). A sandboxed coding
    # agent authenticates safely via these env keys. File-based Codex auth is not
    # forwarded by default because OpenShell uploads are copies: a throwaway
    # sandbox can consume and rotate the refresh token without persisting the
    # replacement back to the host.
    "ANTHROPIC_API_KEY,CURSOR_API_KEY"
)

# PATH is an image/runtime invariant, not configuration to import from the
# worker host.  The OpenShell image owns this baseline; repository-contract
# tools are prepended by ``mac_sandbox_toolchain_setup`` below.  Keeping the
# shared runtime value as well as the Containerfile makes custom env
# passthrough fail closed instead of allowing a host virtualenv or
# package-manager shim to leak into sandbox command resolution.
_FORBIDDEN_OPENSHELL_ENV_PASSTHROUGH = frozenset({"PATH"})


def _openshell_enabled() -> bool:
    return _truthy(os.environ.get("MAC_OPENSHELL_SANDBOX"))


_OPENSHELL_HOST_ALIAS_DEFAULT = "host.openshell.internal"
_HOST_LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "[::1]", "::1")


def _openshell_host_alias() -> str:
    """The in-sandbox alias for the host (OpenShell injects this hosts entry).
    A forwarded ``http://127.0.0.1:8789`` is unreachable from inside the sandbox
    (that loopback is the sandbox's own); rewrite it to this alias."""
    return (os.environ.get("MAC_OPENSHELL_HOST_ALIAS") or "").strip() or _OPENSHELL_HOST_ALIAS_DEFAULT


def _rewrite_host_local_url(value: str, alias: str) -> str:
    """Rewrite a URL whose host is the machine's loopback to the sandbox host
    alias, so forwarded service URLs (MAC_HUB_URL, gateway base) resolve from
    inside the sandbox. Only touches values that look like URLs (contain '://')
    and only the authority's loopback host — tokens/other values pass through."""
    if not value or "://" not in value:
        return value
    out = value
    for h in _HOST_LOCAL_HOSTS:
        out = out.replace("://%s" % h, "://%s" % alias).replace("@%s" % h, "@%s" % alias)
    return out


def _openshell_environment() -> Dict[str, str]:
    """Environment copied through a private workspace file, never process argv."""
    names = os.environ.get("MAC_OPENSHELL_ENV_PASSTHROUGH") or _DEFAULT_OPENSHELL_ENV_PASSTHROUGH
    alias = _openshell_host_alias()
    values: Dict[str, str] = {}
    seen = set()
    for raw in names.split(","):
        name = raw.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if name in _FORBIDDEN_OPENSHELL_ENV_PASSTHROUGH:
            raise ValueError(
                "%s may not be forwarded from the host into OpenShell; "
                "the sandbox image and repository toolchain own command resolution"
                % name
            )
        val = os.environ.get(name)
        if val is None:
            continue
        values[name] = _rewrite_host_local_url(val, alias)
    return values


_LANDLOCK_CREATE_RULESET_SYSCALL = 444
_LANDLOCK_CREATE_RULESET_VERSION = 1


def _landlock_abi_version() -> int:
    """Return the kernel Landlock ABI version, or zero when unavailable.

    Querying ``/sys/kernel/security/lsm`` is not reliable inside containers:
    Kubernetes commonly leaves securityfs unmounted even though the shared
    host kernel implements and permits Landlock.  The version-query form of
    ``landlock_create_ruleset(2)`` is the kernel's authoritative feature probe.

    Linux assigned syscall number 444 to ``landlock_create_ruleset`` for the
    architectures MAC supports (including x86_64 and arm64).  An older kernel,
    a blocked syscall, or any execution error returns zero so callers retain
    fail-closed behavior.
    """
    if not sys.platform.startswith("linux"):
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        syscall = libc.syscall
        syscall.restype = ctypes.c_long
        result = syscall(
            ctypes.c_long(_LANDLOCK_CREATE_RULESET_SYSCALL),
            ctypes.c_void_p(),
            ctypes.c_size_t(0),
            ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION),
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return 0
    return int(result) if result > 0 else 0


def _kernel_has_landlock() -> bool:
    """True if the running kernel exposes a usable Landlock ABI.

    The operator policy uses ``landlock: best_effort`` because OpenShell's egress
    proxy is incompatible with ``hard_requirement`` on current kernels (it adds a
    directory ReadDir right on its own non-directory proxy path, which Landlock
    ABI >= 3 rejects). best_effort still fully enforces on a Landlock-capable
    kernel, but would silently run UNCONFINED on a kernel without Landlock — so
    the executor performs this precheck to recover the fail-closed guarantee.
    """
    return _landlock_abi_version() > 0


def _bundled_default_policy() -> Path:
    """Path to the fail-closed OpenShell policy bundled in this package."""
    return Path(__file__).resolve().parent / "openshell" / "default-policy.yaml"


def _resolve_openshell_policy() -> str:
    """Resolve the policy passed to ``openshell sandbox create``.

    Resolution order (first hit wins):
      1. ``MAC_OPENSHELL_POLICY`` (explicit) — must exist, else raise.
      2. ``~/.mac/openshell-policy.yaml`` — the operator-filled fleet policy.
      3. the package's bundled fail-closed default (``openshell/default-policy.yaml``).

    A policy is *always* returned (or we raise) — the wrap never omits
    ``--policy``, so enabling sandboxing can never silently fall back to
    OpenShell's own image-default profile. The bundled default denies all
    network egress, so an unconfigured deployment fails closed (tasks can't
    reach the hub/gateway) rather than running under an unknown profile.
    """
    explicit = (os.environ.get("MAC_OPENSHELL_POLICY") or "").strip()
    if explicit:
        if not Path(explicit).is_file():
            raise FileNotFoundError("MAC_OPENSHELL_POLICY=%r but no such file" % explicit)
        return explicit
    deployed = Path.home() / ".mac" / "openshell-policy.yaml"
    if deployed.is_file():
        return str(deployed)
    bundled = _bundled_default_policy()
    if bundled.is_file():
        return str(bundled)
    raise FileNotFoundError(
        "OpenShell sandboxing is enabled but no policy could be resolved "
        "(set MAC_OPENSHELL_POLICY, install %s, or ship %s). Refusing to run "
        "without an explicit policy." % (deployed, bundled)
    )


# OpenShell sandboxes are container copies with NO bind-mount, so the task git
# worktree must be UPLOADED in and the agent's results DOWNLOADED back out — a
# plain ``create -- argv`` would run the agent against an empty /sandbox and lose
# its edits + evidence on teardown. The run is therefore a lifecycle:
#   create (--upload workspace, run agent in it, KEEP) -> download -> delete.
# ``include_workdir`` in the policy only grants Landlock access to the path; it
# does not copy files. /sandbox is OpenShell's writable workspace root (uploads
# and downloads must live under it).
_SANDBOX_WORKDIR = "/sandbox"
_SANDBOX_HOME = "/tmp"
_SANDBOX_VERIFICATION_FILE = "mac-sandbox-verification.json"


def _openshell_bin() -> str:
    return (os.environ.get("MAC_OPENSHELL_BIN") or "openshell").strip() or "openshell"


def _sandbox_name() -> str:
    """A unique name for the kept sandbox so the download + delete steps can
    target it. Overridable via MAC_OPENSHELL_SANDBOX_NAME (debug a single run)."""
    explicit = (os.environ.get("MAC_OPENSHELL_SANDBOX_NAME") or "").strip()
    if explicit:
        return explicit
    import uuid

    return "mac-task-" + uuid.uuid4().hex[:12]


def _workspace_basename(workspace: Path) -> str:
    """OpenShell's ``upload <dir> /sandbox`` nests the dir under its basename
    (-> /sandbox/<basename>); that is where the agent runs and what we download."""
    return os.path.basename(str(workspace).rstrip("/")) or "workspace"


def _sandbox_path_for_workspace_child(workspace: Path, sandbox_workspace: str, value: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        rel = Path(raw).expanduser().resolve().relative_to(workspace.expanduser().resolve())
    except (OSError, ValueError):
        return None
    return "%s/%s" % (sandbox_workspace.rstrip("/"), str(rel).replace(os.sep, "/"))


def _sandbox_repository_environment(workspace: Path, sandbox_workspace: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    mapped_worktree = _sandbox_path_for_workspace_child(
        workspace,
        sandbox_workspace,
        os.environ.get("MAC_TASK_REPO_WORKTREE", ""),
    )
    if mapped_worktree:
        values["MAC_TASK_REPO_WORKTREE"] = mapped_worktree
    for name in (
        "MAC_TASK_REPO_BRANCH",
        "MAC_TASK_REPO_LEASE_ID",
        "MAC_TASK_REPO_BASE_SHA",
        "MAC_TASK_REPO_REMOTE",
        "MAC_TASK_CANONICAL_REMOTE",
        "MAC_TASK_REPO_DEFAULT_BRANCH",
    ):
        value = os.environ.get(name)
        if value:
            values[name] = value
    return values


def _ensure_landlock_or_fail() -> None:
    """Fail closed if the kernel can't enforce Landlock: the operator policy is
    best_effort (forced by OpenShell's proxy/hard_requirement incompatibility),
    which would otherwise run UNCONFINED on a Landlock-less kernel. Override only
    for a deliberate, audited exception.

    On macOS the host kernel is *never* the enforcement point — OpenShell
    sandboxes run as Linux containers inside the Docker (Desktop) Linux VM, whose
    LinuxKit kernel does not surface ``/sys/kernel/security/lsm`` to containers.
    seccomp + namespaces + the deny-by-default egress proxy still enforce there;
    Landlock path-confinement is the only piece waived. macOS Docker-based fleet
    nodes therefore set ``MAC_OPENSHELL_ALLOW_NO_LANDLOCK=1`` as the documented
    posture (see ADR 0008 amendment / docs/openshell-sandbox.md)."""
    if _kernel_has_landlock() or _truthy(os.environ.get("MAC_OPENSHELL_ALLOW_NO_LANDLOCK")):
        return
    if sys.platform == "darwin":
        raise RuntimeError(
            "OpenShell sandboxing is enabled on macOS, where the host kernel "
            "cannot expose Landlock (sandboxes run in the Docker Desktop Linux "
            "VM; its LinuxKit kernel does not surface /sys/kernel/security/lsm). "
            "seccomp + namespaces + the egress proxy still enforce. Set "
            "MAC_OPENSHELL_ALLOW_NO_LANDLOCK=1 to accept this posture (the "
            "documented default for macOS Docker fleet nodes)."
        )
    raise RuntimeError(
        "OpenShell sandboxing is enabled but the Landlock ABI syscall is "
        "unavailable or blocked; the policy's "
        "filesystem confinement (best_effort) would not be enforced. Refusing "
        "to run (fail closed). Use a Landlock-capable kernel (>=5.13, ABI>=3 "
        "recommended), or set MAC_OPENSHELL_ALLOW_NO_LANDLOCK=1 to override."
    )


def _sandbox_toolchain_setup_shell() -> str:
    """Shell function injected into the task sandbox before agent/test work."""
    return r'''
mac_sandbox_toolchain_setup() {
  set +e
  MAC_SANDBOX_PYTHON="${MAC_SANDBOX_PYTHON:-/opt/mac-venv/bin/python}"
  [ -x "$MAC_SANDBOX_PYTHON" ] || MAC_SANDBOX_PYTHON="$(command -v python3 || command -v python || true)"
  [ -n "$MAC_SANDBOX_PYTHON" ] || return 0
  export MAC_TOOLCHAIN_ROOT="${MAC_TOOLCHAIN_ROOT:-${MAC_TASK_WORKSPACE:-$PWD}/.mac-toolchain}"
  export MAC_TOOLCHAIN_BIN="$MAC_TOOLCHAIN_ROOT/bin"
  mkdir -p "$MAC_TOOLCHAIN_BIN"
  export MAC_SANDBOX_BASE_PATH="${MAC_SANDBOX_BASE_PATH:-/opt/mac-venv/bin:/usr/local/bin:/usr/bin:/bin}"
  mac_refresh_sandbox_path() {
    MAC_SANDBOX_PATH_PREFIX="$MAC_TOOLCHAIN_BIN:$MAC_TOOLCHAIN_ROOT/node_modules/.bin"
    [ -n "${JAVA_HOME:-}" ] && MAC_SANDBOX_PATH_PREFIX="$MAC_SANDBOX_PATH_PREFIX:$JAVA_HOME/bin"
    export MAC_SANDBOX_PATH_PREFIX
    export PATH="$MAC_SANDBOX_PATH_PREFIX:$MAC_SANDBOX_BASE_PATH"
    hash -r 2>/dev/null || true
  }
  mac_refresh_sandbox_path
  eval "$("$MAC_SANDBOX_PYTHON" - "$MAC_TASK_FILE" <<'PY'
import json, shlex, sys
try:
    loaded = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    loaded = {}
task = loaded.get("task", loaded) if isinstance(loaded, dict) else {}
metadata = task.get("metadata") if isinstance(task, dict) else {}
if not isinstance(metadata, dict):
    metadata = {}
contracts = []
for path in (
    ("execution_contract", "repository_contract"),
    ("origin", "repository_contract"),
    ("repository_contract",),
):
    node = metadata
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    if isinstance(node, dict):
        contracts.append(node)
required, seen = [], set()
creates = []
bootstrap = ""
test = ""
for contract in contracts:
    toolchain = contract.get("toolchain") if isinstance(contract.get("toolchain"), dict) else {}
    for command in toolchain.get("required_commands") or []:
        command = str(command).strip()
        if command and command not in seen:
            seen.add(command)
            required.append(command)
    if not bootstrap:
        boot = contract.get("bootstrap") if isinstance(contract.get("bootstrap"), dict) else {}
        bootstrap = str(boot.get("command") or "").strip()
        creates = [str(item).strip() for item in (boot.get("creates") or []) if str(item).strip()]
    if not test:
        test_block = contract.get("test") if isinstance(contract.get("test"), dict) else {}
        test = str(test_block.get("command") or "").strip()
print("export MAC_REPO_REQUIRED_COMMANDS=%s" % shlex.quote(" ".join(required)))
print("export MAC_REPO_BOOTSTRAP_COMMAND=%s" % shlex.quote(bootstrap))
print("export MAC_REPO_BOOTSTRAP_CREATES=%s" % shlex.quote("\n".join(creates)))
print("export MAC_REPO_TEST_COMMAND=%s" % shlex.quote(test))
PY
)"
  mac_log="$MAC_TOOLCHAIN_ROOT/provisioning.log"
  mac_note() { printf '%s\n' "$*" >> "$mac_log"; }
  mac_install_java_local() {
    command -v curl >/dev/null 2>&1 || return 1
    command -v tar >/dev/null 2>&1 || return 1
    arch="$(uname -m 2>/dev/null || echo x64)"
    case "$arch" in
      x86_64|amd64) arch="x64" ;;
      aarch64|arm64) arch="aarch64" ;;
      *) return 1 ;;
    esac
    mkdir -p "$MAC_TOOLCHAIN_ROOT/java"
    curl -fsSL "https://api.adoptium.net/v3/binary/latest/17/ga/linux/${arch}/jre/hotspot/normal/eclipse?project=jdk" -o "$MAC_TOOLCHAIN_ROOT/jre.tar.gz" || return 1
    tar -xzf "$MAC_TOOLCHAIN_ROOT/jre.tar.gz" -C "$MAC_TOOLCHAIN_ROOT/java" --strip-components=1 || return 1
    export JAVA_HOME="$MAC_TOOLCHAIN_ROOT/java"
    mac_refresh_sandbox_path
  }
  mac_install_gh_local() {
    command -v curl >/dev/null 2>&1 || return 1
    command -v tar >/dev/null 2>&1 || return 1
    arch="$(uname -m 2>/dev/null || echo x64)"
    case "$arch" in
      x86_64|amd64) asset_arch="linux_amd64" ;;
      aarch64|arm64) asset_arch="linux_arm64" ;;
      armv6l|armv7l) asset_arch="linux_armv6" ;;
      *) return 1 ;;
    esac
    release_json="$MAC_TOOLCHAIN_ROOT/gh-release.json"
    curl -fsSL https://api.github.com/repos/cli/cli/releases/latest -o "$release_json" || return 1
    url="$("$MAC_SANDBOX_PYTHON" - "$release_json" "$asset_arch" <<'PYGH'
import json, sys
path, asset_arch = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
for asset in data.get("assets") or []:
    name = str(asset.get("name") or "")
    url = str(asset.get("browser_download_url") or "")
    if name.endswith("%s.tar.gz" % asset_arch) and url:
        print(url)
        break
PYGH
)"
    [ -n "$url" ] || return 1
    mkdir -p "$MAC_TOOLCHAIN_ROOT/gh"
    curl -fsSL "$url" -o "$MAC_TOOLCHAIN_ROOT/gh.tar.gz" || return 1
    tar -xzf "$MAC_TOOLCHAIN_ROOT/gh.tar.gz" -C "$MAC_TOOLCHAIN_ROOT/gh" --strip-components=1 || return 1
    [ -x "$MAC_TOOLCHAIN_ROOT/gh/bin/gh" ] || return 1
    ln -sf "$MAC_TOOLCHAIN_ROOT/gh/bin/gh" "$MAC_TOOLCHAIN_BIN/gh"
    mac_refresh_sandbox_path
  }
  mac_install_node_local() {
    command -v curl >/dev/null 2>&1 || return 1
    command -v tar >/dev/null 2>&1 || return 1
    narch="$(uname -m 2>/dev/null || echo x64)"
    case "$narch" in
      x86_64|amd64) narch="x64" ;;
      aarch64|arm64) narch="arm64" ;;
      *) return 1 ;;
    esac
    nver="${MAC_SANDBOX_NODE_VERSION:-v22.12.0}"
    mkdir -p "$MAC_TOOLCHAIN_ROOT/node"
    curl -fsSL "https://nodejs.org/dist/${nver}/node-${nver}-linux-${narch}.tar.xz" -o "$MAC_TOOLCHAIN_ROOT/node.tar.xz" >> "$mac_log" 2>&1 || return 1
    tar -xJf "$MAC_TOOLCHAIN_ROOT/node.tar.xz" -C "$MAC_TOOLCHAIN_ROOT/node" --strip-components=1 >> "$mac_log" 2>&1 || return 1
    for b in node npm npx corepack; do
      [ -x "$MAC_TOOLCHAIN_ROOT/node/bin/$b" ] && ln -sf "$MAC_TOOLCHAIN_ROOT/node/bin/$b" "$MAC_TOOLCHAIN_BIN/$b"
    done
    mac_refresh_sandbox_path
    [ -x "$MAC_TOOLCHAIN_BIN/node" ] || return 1
  }
  mac_ensure_modern_node() {
    # The base sandbox ships Node 18, but modern pnpm (v10, the corepack/repo
    # default) requires Node >=22 and aborts otherwise, failing every Node test
    # target. If node is missing or older than v20, install a pinned Node 22 into
    # the task toolchain and shadow the stale one on PATH (MAC_TOOLCHAIN_BIN is
    # already PATH-first). Override the version with MAC_SANDBOX_NODE_VERSION.
    nmajor="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
    case "$nmajor" in ''|*[!0-9]*) nmajor=0 ;; esac
    if [ "$nmajor" -ge 20 ] 2>/dev/null; then
      return 0
    fi
    mac_note "node major=$nmajor (<20); installing pinned modern node"
    if mac_install_node_local; then
      return 0
    fi
    # Modern Node couldn't be fetched (e.g. nodejs.org is not on the sandbox
    # egress allowlist -> curl 403). Fall back to a Node-18-compatible pnpm
    # (pnpm@9, installed from the allowlisted npm registry via corepack) placed
    # in MAC_TOOLCHAIN_BIN so it SHADOWS any system pnpm@10 that would reject
    # Node 18 ("requires Node >=22"). Lets pnpm install / Node tests run on 18.
    mac_note "modern node unavailable; pinning Node-18-compatible pnpm instead"
    mac_install_command pnpm || mac_note "could not pin Node-18-compatible pnpm"
  }
  mac_install_command() {
    cmd="$1"
    case "$cmd" in
      gh)
        if [ "$(id -u 2>/dev/null || echo 1)" = "0" ] && command -v apt-get >/dev/null 2>&1 && command -v curl >/dev/null 2>&1; then
          mkdir -p -m 755 /etc/apt/keyrings >> "$mac_log" 2>&1 || true
          curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg >> "$mac_log" 2>&1 || true
          chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg >> "$mac_log" 2>&1 || true
          echo "deb [arch=$(dpkg --print-architecture 2>/dev/null || echo amd64) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list 2>> "$mac_log" || true
          DEBIAN_FRONTEND=noninteractive apt-get update >> "$mac_log" 2>&1 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends gh >> "$mac_log" 2>&1 && return 0
        fi
        mac_install_gh_local
        ;;
      pnpm)
        # Pin pnpm to a version compatible with the sandbox's Node. pnpm@latest
        # (v10) demands Node >=22.13, but the base sandbox image ships Node 18, so
        # `pnpm` aborts ("requires at least Node.js v22.13") and every Node test
        # target fails. pnpm@9 supports Node >=18.12, so it runs on the sandbox's
        # Node 18 and on newer Node alike. Override with MAC_SANDBOX_PNPM_VERSION.
        # Install a REAL pnpm@<ver> binary into the toolchain bin (PATH-first) so
        # it SHADOWS any system/corepack pnpm. A `corepack prepare ... --activate`
        # only leaves a shim that RE-RESOLVES to the newer version at run time
        # (which is why pnpm install kept hitting "requires Node v22" even after
        # we "pinned" 9), so prefer a concrete npm-installed binary.
        pnpm_ver="${MAC_SANDBOX_PNPM_VERSION:-9}"
        if command -v npm >/dev/null 2>&1; then
          npm install --no-fund --no-audit --prefix "$MAC_TOOLCHAIN_ROOT" "pnpm@${pnpm_ver}" >> "$mac_log" 2>&1
          if [ -x "$MAC_TOOLCHAIN_ROOT/node_modules/.bin/pnpm" ]; then
            ln -sf "$MAC_TOOLCHAIN_ROOT/node_modules/.bin/pnpm" "$MAC_TOOLCHAIN_BIN/pnpm"
            mac_refresh_sandbox_path
            command -v pnpm >/dev/null 2>&1 && return 0
          fi
        fi
        if command -v corepack >/dev/null 2>&1; then
          corepack enable --install-directory "$MAC_TOOLCHAIN_BIN" >> "$mac_log" 2>&1 || true
          corepack prepare "pnpm@${pnpm_ver}" --activate >> "$mac_log" 2>&1 || true
        fi
        command -v pnpm >/dev/null 2>&1 && return 0
        return 1
        ;;
      lein)
        command -v curl >/dev/null 2>&1 || return 1
        curl -fsSL https://raw.githubusercontent.com/technomancy/leiningen/stable/bin/lein -o "$MAC_TOOLCHAIN_BIN/lein" >> "$mac_log" 2>&1 || return 1
        chmod +x "$MAC_TOOLCHAIN_BIN/lein"
        ;;
      java)
        if [ "$(id -u 2>/dev/null || echo 1)" = "0" ] && command -v apt-get >/dev/null 2>&1; then
          DEBIAN_FRONTEND=noninteractive apt-get update >> "$mac_log" 2>&1 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openjdk-17-jre-headless >> "$mac_log" 2>&1 && return 0
        fi
        mac_install_java_local
        ;;
      node|npm)
        [ "$(id -u 2>/dev/null || echo 1)" = "0" ] || return 1
        command -v apt-get >/dev/null 2>&1 || return 1
        DEBIAN_FRONTEND=noninteractive apt-get update >> "$mac_log" 2>&1 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nodejs npm >> "$mac_log" 2>&1
        ;;
      make)
        [ "$(id -u 2>/dev/null || echo 1)" = "0" ] || return 1
        command -v apt-get >/dev/null 2>&1 || return 1
        DEBIAN_FRONTEND=noninteractive apt-get update >> "$mac_log" 2>&1 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends make >> "$mac_log" 2>&1
        ;;
      *)
        return 1
        ;;
    esac
  }
  # Force a modern Node BEFORE the per-command loop: node may already be present
  # (so the loop would skip it) yet be too old for the repo's pnpm. Only for repos
  # whose toolchain actually uses Node, to avoid an unnecessary download.
  case " $MAC_REPO_REQUIRED_COMMANDS " in
    *" node "*|*" npm "*|*" pnpm "*) mac_ensure_modern_node ;;
  esac
  # Pin a pnpm that READS the repo's declared config. The base image ships pnpm
  # 11, which DROPPED reading pnpm settings from package.json (onlyBuiltDependencies
  # etc.) and from .npmrc — so repos that declare config there get a broken/
  # incomplete install: native build scripts are ignored ("ERR_PNPM_IGNORED_BUILDS")
  # and devDeps like jest/vitest end up half-linked ("Cannot find module .../jest").
  # pnpm 9 reads package.json + .npmrc config and installs completely on Node 18-22,
  # and (unlike pnpm 11) does not run the high-concurrency release-age metadata pass
  # that the egress proxy can't sustain. When pnpm is required and the system pnpm
  # is >=10, install a task-local pnpm@<ver> PATH-first so the repo's config is
  # honored. Override/opt out with MAC_SANDBOX_PNPM_VERSION.
  case " $MAC_REPO_REQUIRED_COMMANDS " in
    *" pnpm "*)
      mac_pnpm_major="$(pnpm --version 2>/dev/null | cut -d. -f1)"
      case "$mac_pnpm_major" in ''|*[!0-9]*) mac_pnpm_major=0 ;; esac
      mac_pnpm_want="${MAC_SANDBOX_PNPM_VERSION:-9}"
      if [ "$mac_pnpm_want" != "system" ] && [ "$mac_pnpm_major" -ge 10 ] 2>/dev/null \
         && [ ! -x "$MAC_TOOLCHAIN_BIN/pnpm" ]; then
        mac_install_command pnpm && mac_note "pinned task-local pnpm@${mac_pnpm_want} (image pnpm ${mac_pnpm_major} ignores package.json/.npmrc config)" \
          || mac_note "could not pin compatible pnpm; using system pnpm ${mac_pnpm_major}"
      fi
      ;;
  esac
  for cmd in $MAC_REPO_REQUIRED_COMMANDS; do
    command -v "$cmd" >/dev/null 2>&1 && continue
    mac_note "missing command before provisioning: $cmd"
    mac_install_command "$cmd" || mac_note "could not provision command: $cmd"
  done
  missing_after=""
  for cmd in $MAC_REPO_REQUIRED_COMMANDS; do
    command -v "$cmd" >/dev/null 2>&1 || missing_after="$missing_after $cmd"
  done
  worktree="${MAC_TASK_REPO_WORKTREE:-$PWD}"
  needs_bootstrap=0
  bootstrap_ran=0
  bootstrap_returncode=0
  bootstrap_status="skipped"
  if [ -n "$MAC_REPO_BOOTSTRAP_COMMAND" ]; then
    if [ -z "$MAC_REPO_BOOTSTRAP_CREATES" ]; then
      needs_bootstrap=1
    else
      while IFS= read -r create_path; do
        [ -z "$create_path" ] && continue
        [ -e "$worktree/$create_path" ] || needs_bootstrap=1
      done <<EOF
$MAC_REPO_BOOTSTRAP_CREATES
EOF
    fi
  fi
  if [ "$needs_bootstrap" = "1" ] && [ -d "$worktree" ]; then
    bootstrap_ran=1
    mac_note "running bootstrap.command: $MAC_REPO_BOOTSTRAP_COMMAND"
    # pnpm >=10.16 reads install tuning ONLY from pnpm-workspace.yaml (camelCase) —
    # NOT .npmrc, env vars, or `pnpm config set --global` (all verified ignored).
    # The deny-by-default L7 egress proxy resets high-concurrency registry fetches
    # (UND_ERR_SOCKET / ERR_PNPM_META_FETCH_FAIL) and pnpm's release-age supply-
    # chain pass amplifies it by fetching metadata for every lockfile entry. Cap
    # network concurrency + disable the release-age pass DURING install by
    # appending to pnpm-workspace.yaml, then RESTORE the file so the worktree stays
    # clean for the contract dirty-check (installed node_modules persist). Gated on
    # the file existing, so non-pnpm repos are untouched. See ADR 0009.
    mac_ws_yaml="$worktree/pnpm-workspace.yaml"
    mac_ws_tuned=0
    # Only relevant for pnpm >=10 (which reads these from pnpm-workspace.yaml and
    # runs the release-age pass). Under the pinned pnpm 9 the file isn't consulted
    # for these keys, so skip the edit to avoid an unknown-setting warning.
    mac_eff_pnpm="$(pnpm --version 2>/dev/null | cut -d. -f1)"
    case "$mac_eff_pnpm" in ''|*[!0-9]*) mac_eff_pnpm=0 ;; esac
    if [ "$mac_eff_pnpm" -ge 10 ] 2>/dev/null && [ -f "$mac_ws_yaml" ] && ! grep -q "networkConcurrency:" "$mac_ws_yaml" 2>/dev/null; then
      if cp "$mac_ws_yaml" "$MAC_TOOLCHAIN_ROOT/pnpm-workspace.yaml.macbak" 2>/dev/null; then
        mac_ws_tuned=1
        {
          printf '\n# mac: temporary install tuning for the constrained sandbox egress proxy\n'
          printf 'networkConcurrency: %s\n' "${MAC_SANDBOX_NETWORK_CONCURRENCY:-2}"
          printf 'minimumReleaseAge: 0\n'
        } >> "$mac_ws_yaml"
        mac_note "tuned pnpm-workspace.yaml for install (networkConcurrency=${MAC_SANDBOX_NETWORK_CONCURRENCY:-2}, minimumReleaseAge=0)"
      fi
    fi
    # `bash -lc` runs the login profile, which RESETS PATH to the system default
    # and discards the toolchain bin we prepended above. Re-assert the toolchain
    # PATH (and clear bash's command hash) INSIDE the login shell, after the
    # profile runs, so the pinned tools win.
    ( cd "$worktree" && bash -lc 'export PATH="$MAC_SANDBOX_PATH_PREFIX:$MAC_SANDBOX_BASE_PATH"; hash -r 2>/dev/null || true; '"$MAC_REPO_BOOTSTRAP_COMMAND" ) >> "$mac_log" 2>&1
    bootstrap_returncode=$?
    # Restore the original pnpm-workspace.yaml so the worktree is not left dirty.
    if [ "$mac_ws_tuned" = "1" ]; then
      mv -f "$MAC_TOOLCHAIN_ROOT/pnpm-workspace.yaml.macbak" "$mac_ws_yaml" 2>/dev/null || true
    fi
    if [ "$bootstrap_returncode" = "0" ]; then
      bootstrap_status="pass"
    else
      bootstrap_status="fail"
      mac_note "bootstrap.command failed"
    fi
  fi
  export MAC_REPO_BOOTSTRAP_SETUP_RAN="$bootstrap_ran"
  export MAC_REPO_BOOTSTRAP_SETUP_RETURNCODE="$bootstrap_returncode"
  export MAC_REPO_BOOTSTRAP_SETUP_STATUS="$bootstrap_status"
  "$MAC_SANDBOX_PYTHON" - <<'PY' >/dev/null 2>&1 || true
import json, os, shutil
root = os.environ.get("MAC_TOOLCHAIN_ROOT") or ""
if not root:
    raise SystemExit(0)
required = [item for item in os.environ.get("MAC_REPO_REQUIRED_COMMANDS", "").split() if item]
delta = {
    "schema": "mac.sandbox_environment_delta.v1",
    "package_manager": "sandbox-toolchain",
    "commands": required,
    "missing_after": [item for item in required if shutil.which(item) is None],
    "toolchain_root": root,
    "reason": "repository_contract.toolchain.required_commands",
}
os.makedirs(root, exist_ok=True)
with open(os.path.join(root, "environment-delta.json"), "w", encoding="utf-8") as handle:
    json.dump(delta, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
  return 0
}
'''


def _sandbox_repository_verification_shell(
    environment: Optional[Mapping[str, str]] = None,
) -> str:
    # Verification runs through a fresh ``openshell sandbox exec`` process, so
    # it does not inherit the private environment sourced by the agent process.
    # Re-export only non-secret workspace/repository paths needed to re-read the
    # task's repository contract and run its test gate.
    exports = [
        "export %s=%s" % (name, shlex.quote(value))
        for name, value in sorted((environment or {}).items())
        if _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
    ]
    return "\n".join(
        [
            *exports,
            _sandbox_toolchain_setup_shell(),
            'cd "$MAC_TASK_WORKSPACE"',
            "mac_sandbox_toolchain_setup || true",
            r'''$MAC_SANDBOX_PYTHON - <<'PY'
import json, os, signal, subprocess, tempfile, time
workspace = os.environ.get("MAC_TASK_WORKSPACE") or os.getcwd()
worktree = os.environ.get("MAC_TASK_REPO_WORKTREE") or workspace
command = os.environ.get("MAC_REPO_TEST_COMMAND", "").strip()
bootstrap_command = os.environ.get("MAC_REPO_BOOTSTRAP_COMMAND", "").strip()
# `bash -lc` re-runs the login profile, which resets PATH to the system default
# and drops the toolchain bin we prepended during setup — so repo bootstrap/test
# commands would resolve a stale system tool (e.g. pnpm@10 that demands Node 22)
# instead of the pinned toolchain one (pnpm@9). Re-assert the toolchain PATH (and
# clear bash's command hash) INSIDE the login shell so the pinned tools win.
_TC_PATH_PREFIX = (
    'export PATH="$MAC_SANDBOX_PATH_PREFIX:$MAC_SANDBOX_BASE_PATH"; '
    'hash -r 2>/dev/null || true; '
)
bootstrap_creates = [
    item.strip()
    for item in os.environ.get("MAC_REPO_BOOTSTRAP_CREATES", "").splitlines()
    if item.strip()
]
result_path = os.path.join(workspace, "mac-sandbox-verification.json")
delta_path = os.path.join(os.environ.get("MAC_TOOLCHAIN_ROOT", ""), "environment-delta.json")
delta = {}
try:
    with open(delta_path, encoding="utf-8") as handle:
        delta = json.load(handle)
except Exception:
    delta = {}

def missing_bootstrap_outputs():
    return [
        path
        for path in bootstrap_creates
        if not os.path.exists(os.path.join(worktree, path))
    ]

def clip(value, limit=4000):
    # Keep head AND tail — pytest/pip print the diagnosis LAST; a head-only
    # cut hid every long failure from evidence (observed live, repeatedly).
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head
    marker = "\n… [%d chars omitted] …\n" % (len(text) - head - tail)
    return text[:head] + marker + text[-tail:]

def run_bounded_bash(command, timeout):
    """Run a verifier command and terminate its whole process group.

    Output goes to files rather than pipes. A background descendant can inherit
    a pipe after the login shell exits, causing ``communicate()`` to wait until
    the full repository timeout even though the declared command already
    completed. Files let ``wait()`` observe the command process directly; any
    descendants left in its process group are then killed as verifier debris.
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
            proc = subprocess.Popen(
                ["bash", "-lc", _TC_PATH_PREFIX + command],
                cwd=worktree,
                env=os.environ.copy(),
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                start_new_session=True,
            )
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (AttributeError, ProcessLookupError, PermissionError, OSError):
                    proc.kill()
                proc.wait()
            else:
                # The command process exited. Do not allow background children
                # from the verifier to leak into later sandbox steps.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (AttributeError, ProcessLookupError, PermissionError, OSError):
                    pass
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read()
            stderr = stderr_file.read()
            return (
                124 if timed_out else int(proc.returncode),
                stdout or "",
                stderr or "",
                timed_out,
            )

bootstrap = None
if bootstrap_command:
    setup_ran = os.environ.get("MAC_REPO_BOOTSTRAP_SETUP_RAN") == "1"
    try:
        setup_returncode = int(os.environ.get("MAC_REPO_BOOTSTRAP_SETUP_RETURNCODE") or "0")
    except ValueError:
        setup_returncode = 1
    setup_status = os.environ.get("MAC_REPO_BOOTSTRAP_SETUP_STATUS") or (
        "pass" if setup_returncode == 0 else "fail"
    )
    missing_before = missing_bootstrap_outputs()
    if not bootstrap_creates:
        bootstrap = {
            "command": bootstrap_command,
            "creates": bootstrap_creates,
            "returncode": setup_returncode,
            "status": setup_status,
            "reason": "bootstrap.creates omitted; setup phase ran bootstrap before verification"
            if setup_ran
            else "bootstrap.creates omitted; setup phase did not run bootstrap",
        }
    elif not missing_before:
        bootstrap = {
            "command": bootstrap_command,
            "creates": bootstrap_creates,
            "returncode": 0,
            "status": "skipped",
            "reason": "declared bootstrap outputs already exist",
        }
    else:
        started = time.time()
        try:
            timeout = float(
                os.environ.get("MAC_WORKER_REPOSITORY_BOOTSTRAP_TIMEOUT")
                or os.environ.get("MAC_WORKER_REPOSITORY_TEST_TIMEOUT")
                or "1800"
            )
        except ValueError:
            timeout = 1800.0
        returncode, stdout, stderr, timed_out = run_bounded_bash(bootstrap_command, timeout)
        bootstrap = {
            "command": bootstrap_command,
            "creates": bootstrap_creates,
            "missing_before": missing_before,
            "returncode": returncode,
            "status": "pass" if returncode == 0 else "fail",
            "stdout": clip(stdout),
            "stderr": clip(stderr),
            "duration_ms": int((time.time() - started) * 1000),
        }
        if timed_out:
            bootstrap["error"] = "bootstrap command timed out after %ss" % timeout
    if bootstrap.get("returncode") == 0 and bootstrap_creates:
        missing_after = missing_bootstrap_outputs()
        if missing_after:
            bootstrap = dict(bootstrap)
            bootstrap["returncode"] = 1
            bootstrap["status"] = "fail"
            bootstrap["missing_after"] = missing_after
            bootstrap["error"] = "bootstrap command did not create declared outputs"

if not command:
    payload = {
        "schema": "mac.sandbox_verification.v1",
        "status": "fail",
        "command": "",
        "returncode": 1,
        "stderr": "repository contract test.command is missing",
        "environment_delta": delta,
    }
elif bootstrap is not None and bootstrap.get("returncode") != 0:
    payload = {
        "schema": "mac.sandbox_verification.v1",
        "status": "fail",
        "command": command,
        "returncode": int(bootstrap.get("returncode") or 1),
        "stderr": "repository bootstrap failed before sandbox verification tests",
        "worktree": worktree,
        "environment_delta": delta,
        "bootstrap": bootstrap,
    }
else:
    started = time.time()
    try:
        timeout = float(os.environ.get("MAC_WORKER_REPOSITORY_TEST_TIMEOUT", "1800") or "1800")
    except ValueError:
        timeout = 1800.0
    returncode, stdout, stderr, timed_out = run_bounded_bash(command, timeout)
    payload = {
        "schema": "mac.sandbox_verification.v1",
        "status": "pass" if returncode == 0 else "fail",
        "command": command,
        "returncode": returncode,
        "stdout": clip(stdout),
        "stderr": clip(stderr),
        "duration_ms": int((time.time() - started) * 1000),
        "worktree": worktree,
        "environment_delta": delta,
    }
    if timed_out:
        payload["error"] = "repository test command timed out after %ss" % timeout
    if bootstrap is not None:
        payload["bootstrap"] = bootstrap
with open(result_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
raise SystemExit(0 if payload.get("returncode") == 0 else int(payload.get("returncode") or 1))
PY''',
        ]
    )


def _write_private_shell_env(path: Path, values: Mapping[str, str]) -> Path:
    env_lines = [
        "export %s=%s" % (name, shlex.quote(value))
        for name, value in sorted(values.items())
        if _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
    ]
    path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _write_sandbox_runtime_files(
    workspace: Path, sandbox_workspace: str
) -> tuple[Path, Path]:
    env_values: Dict[str, str] = {
        **_openshell_environment(),
        **_sandbox_repository_environment(workspace, sandbox_workspace),
        "MAC_TASK_WORKSPACE": sandbox_workspace,
        "MAC_TASK_FILE": "%s/task.json" % sandbox_workspace.rstrip("/"),
        # OpenShell runs the image as its unprivileged sandbox user. Hermes'
        # uploaded config is deliberately rooted under /tmp, so HOME belongs in
        # the private environment file rather than the process-visible
        # MAC_OPENSHELL_CREATE_ARGS argv.
        "HOME": _SANDBOX_HOME,
        # Never inherit the worker host's executable search path.  The image
        # runtime is the stable baseline and task-local contract tools are
        # prepended when the toolchain setup file is sourced.
        "MAC_SANDBOX_BASE_PATH": _SANDBOX_BASE_PATH,
        "PATH": _SANDBOX_BASE_PATH,
    }
    env_file = _write_private_shell_env(
        workspace / ".mac-openshell-env.sh", env_values
    )

    toolchain_file = workspace / ".mac-sandbox-toolchain.sh"
    toolchain_file.write_text(_sandbox_toolchain_setup_shell(), encoding="utf-8")
    toolchain_file.chmod(0o700)
    return env_file, toolchain_file


def _build_sandbox_create_argv(
    name: str, workspace: Path, basename: str, agent_argv: List[str]
) -> List[str]:
    """``openshell sandbox create`` argv that uploads the task workspace, runs the
    agent inside it, and KEEPS the sandbox so results can be downloaded.

    A policy is ALWAYS passed (explicit -> deployed -> bundled fail-closed
    default) so OpenShell can never silently apply its own image-default profile.
    The host workspace is uploaded to /sandbox (landing at /sandbox/<basename>);
    the agent runs there with $MAC_TASK_WORKSPACE/$MAC_TASK_FILE repointed at the
    in-sandbox paths (the host paths don't exist inside the sandbox), so its
    evidence manifest is written where ``download`` later fetches it. The agent
    ``agent_argv`` is the private-file wrapper, not the underlying prompt-bearing
    command. Secrets and the toolchain body are sourced from uploaded mode-0600
    files, keeping the host's process list small and credential-free.
    """
    if "mac.agent_command" not in agent_argv:
        raise ValueError("sandbox agent argv must use the private-file command wrapper")
    sub = "%s/%s" % (_SANDBOX_WORKDIR, basename)
    argv: List[str] = [_openshell_bin(), "sandbox", "create", "--no-auto-providers"]
    argv += ["--policy", _resolve_openshell_policy(), "--name", name]
    extra = (os.environ.get("MAC_OPENSHELL_CREATE_ARGS") or "").strip()
    if extra:
        extra_argv = shlex.split(extra)
        if "--env" in extra_argv or "--" in extra_argv:
            raise ValueError(
                "MAC_OPENSHELL_CREATE_ARGS may not contain --env or --; "
                "use MAC_OPENSHELL_ENV_PASSTHROUGH for private environment transfer"
            )
        argv += extra_argv
    argv += ["--upload", "%s:%s" % (str(workspace), _SANDBOX_WORKDIR)]
    inner = "\n".join(
        [
            "cd %s" % shlex.quote(sub),
            "set -a",
            ". ./.mac-openshell-env.sh",
            "set +a",
            "rm -f ./.mac-openshell-env.sh",
            'if [ -n "${MAC_TASK_REPO_WORKTREE:-}" ] && [ -d "$MAC_TASK_REPO_WORKTREE" ] && [ ! -e /sandbox/mac-clone ]; then ln -s "$MAC_TASK_REPO_WORKTREE" /sandbox/mac-clone || true; fi',
            ". ./.mac-sandbox-toolchain.sh",
            "rm -f ./.mac-sandbox-toolchain.sh",
            "mac_sandbox_toolchain_setup || true",
            # The workspace is tar-uploaded, so its files can be owned by a
            # different uid than the sandbox user; without a safe.directory
            # whitelist every git command against uploaded paths dies with
            # "dubious ownership" (the sandbox is single-purpose and isolated,
            # so trusting all paths inside it is safe). Env form, not --global,
            # so it reaches every git subprocess regardless of HOME.
            "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*'",
            # A host git worktree stores `.git` as a pointer into a host-only
            # common directory.  That pointer is invalid after OpenShell uploads
            # the workspace, and host credentials/remotes must not be copied
            # into the sandbox merely to make Git usable.  Replace it with a
            # credential-free snapshot repository so the agent can inspect its
            # own diff and run tools that expect Git.  The download merger
            # deliberately excludes this sandbox-only `.git` directory; the
            # deterministic host finalizer commits and publishes the harvested
            # file changes using the real task worktree.
            'if [ -n "${MAC_TASK_REPO_WORKTREE:-}" ] && [ -d "$MAC_TASK_REPO_WORKTREE" ] && command -v git >/dev/null 2>&1; then',
            '  rm -rf "$MAC_TASK_REPO_WORKTREE/.git"',
            '  git -C "$MAC_TASK_REPO_WORKTREE" init -q',
            '  git -C "$MAC_TASK_REPO_WORKTREE" config user.email mac-sandbox@invalid',
            '  git -C "$MAC_TASK_REPO_WORKTREE" config user.name "MAC OpenShell sandbox"',
            '  git -C "$MAC_TASK_REPO_WORKTREE" add -A',
            '  git -C "$MAC_TASK_REPO_WORKTREE" commit -q --allow-empty -m "MAC OpenShell sandbox baseline"',
            "fi",
            "exec %s" % shlex.join(agent_argv),
        ]
    )
    argv += ["--", "bash", "-c", inner]
    return argv


def _sandbox_step(args: List[str], *, timeout: float) -> "tuple[bool, str]":
    """Run an openshell lifecycle step (download/delete) out-of-band of the
    audited agent run. Best-effort: returns (ok, message), never raises."""
    try:
        proc = _run_captured(
            [_openshell_bin(), "sandbox", *args],
            Path.cwd(),
            timeout,
        )
        return proc.returncode == 0, (proc.stderr or proc.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001 - teardown must never mask the run
        return False, str(exc)


_SANDBOX_DOWNLOAD_RUNTIME_ROOT_NAMES = {
    ".venv",
    "venv",
    "node_modules",
}


def _relative_path_or_none(path: Path, root: Path) -> Optional[Path]:
    try:
        return path.expanduser().resolve().relative_to(root.expanduser().resolve())
    except (OSError, ValueError):
        return None


def _sandbox_repository_roots(workspace: Path, download_root: Path) -> set[Path]:
    roots: set[Path] = set()

    env_worktree = (os.environ.get("MAC_TASK_REPO_WORKTREE") or "").strip()
    if env_worktree:
        rel = _relative_path_or_none(Path(env_worktree), workspace)
        if rel is not None:
            roots.add(rel)

    for context_file in (workspace / "repository-worktree.json", download_root / "repository-worktree.json"):
        try:
            context = json.loads(context_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(context, dict):
            continue
        worktree = str(context.get("repository_worktree") or "").strip()
        if not worktree:
            continue
        rel = _relative_path_or_none(Path(worktree), workspace)
        if rel is not None:
            roots.add(rel)

    return roots


def _path_is_under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _sandbox_download_path_is_git_backup(rel_path: Path) -> bool:
    return any(part.startswith(".git.bak") for part in rel_path.parts)


def _sandbox_download_path_excluded(rel_path: Path, repository_roots: set[Path]) -> bool:
    # Git metadata is never a legitimate file payload. Copying a sandbox .git
    # directory over a host git-worktree .git file caused the live P0 failure.
    # OpenShell transfers can also materialize a sibling .git.bak* when a
    # container checkout and host git-worktree metadata differ; treat that as
    # transfer metadata too, while preserving real repo files like .gitignore.
    if ".git" in rel_path.parts or _sandbox_download_path_is_git_backup(rel_path):
        return True
    for root in repository_roots:
        for name in _SANDBOX_DOWNLOAD_RUNTIME_ROOT_NAMES:
            runtime_root = root / name
            if _path_is_under(rel_path, runtime_root):
                return True
    return False


def _merge_sandbox_download_tree(download_root: Path, workspace: Path) -> None:
    """Merge a downloaded sandbox workspace into the host workspace.

    OpenShell downloads a tar archive. Extracting directly over a git worktree is
    unsafe for repo tasks because task worktrees may use a host ``.git`` file
    while the sandbox checkout may contain a ``.git`` directory. Keep host git
    metadata and container-local dependency caches out of the merge; the
    deterministic finalizer rebuilds/tests from the host worktree.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    repository_roots = _sandbox_repository_roots(workspace, download_root)
    source_files: set[Path] = set()
    source_dirs: set[Path] = {Path(".")}

    for root, dirs, files in os.walk(download_root, topdown=True, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(download_root)
        if rel_root != Path("."):
            source_dirs.add(rel_root)
        kept_dirs: List[str] = []
        for name in dirs:
            rel = rel_root / name
            if _sandbox_download_path_excluded(rel, repository_roots):
                continue
            source_dirs.add(rel)
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            rel = rel_root / name
            if not _sandbox_download_path_excluded(rel, repository_roots):
                source_files.add(rel)

    for root, dirs, files in os.walk(workspace, topdown=False, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(workspace)
        for name in files:
            rel = rel_root / name
            if _sandbox_download_path_is_git_backup(rel):
                (root_path / name).unlink(missing_ok=True)
                continue
            if _sandbox_download_path_excluded(rel, repository_roots) or rel in source_files:
                continue
            (root_path / name).unlink(missing_ok=True)
        for name in dirs:
            rel = rel_root / name
            target = root_path / name
            if _sandbox_download_path_is_git_backup(rel):
                shutil.rmtree(target, ignore_errors=True)
                continue
            if _sandbox_download_path_excluded(rel, repository_roots) or rel in source_dirs:
                continue
            if target.is_symlink() or target.is_file():
                target.unlink(missing_ok=True)
            else:
                shutil.rmtree(target, ignore_errors=True)

    for root, dirs, files in os.walk(download_root, topdown=True, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(download_root)
        kept_dirs = []
        for name in dirs:
            rel = rel_root / name
            src = root_path / name
            if _sandbox_download_path_excluded(rel, repository_roots):
                continue
            if src.is_symlink():
                dst = workspace / rel
                if dst.exists() or dst.is_symlink():
                    if dst.is_dir() and not dst.is_symlink():
                        shutil.rmtree(dst)
                    else:
                        dst.unlink()
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.symlink_to(os.readlink(src))
            else:
                (workspace / rel).mkdir(parents=True, exist_ok=True)
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            rel = rel_root / name
            if _sandbox_download_path_excluded(rel, repository_roots):
                continue
            src = root_path / name
            dst = workspace / rel
            if dst.exists() or dst.is_symlink():
                if dst.is_dir() and not dst.is_symlink():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_symlink():
                dst.symlink_to(os.readlink(src))
            else:
                shutil.copy2(src, dst)


def _sandbox_run_repository_verification(
    name: str, basename: str, workspace: Path, task: Any
) -> Optional[bool]:
    if not isinstance(task, dict) or not task_is_repo_coupled(task):
        return None
    if not _repository_contract_test_command(task):
        return None
    sub = "%s/%s" % (_SANDBOX_WORKDIR, basename)
    script_path = workspace / ".mac-sandbox-repository-verify.sh"
    verification_environment = {
        **_sandbox_repository_environment(workspace, sub),
        "HOME": _SANDBOX_HOME,
        "MAC_TASK_FILE": "%s/task.json" % sub,
        "MAC_TASK_WORKSPACE": sub,
    }
    script_path.write_text(
        _sandbox_repository_verification_shell(verification_environment) + "\n",
        encoding="utf-8",
    )
    script_path.chmod(0o700)
    sandbox_script = "%s/%s" % (sub, script_path.name)
    ok, msg = _sandbox_step(
        ["upload", name, str(script_path), sandbox_script],
        timeout=120.0,
    )
    if not ok:
        sys.stderr.write("[executor] WARNING: sandbox repository verification upload failed: %s\n" % msg)
        return False
    try:
        timeout = float(os.environ.get("MAC_WORKER_REPOSITORY_TEST_TIMEOUT", "1800"))
    except ValueError:
        timeout = 1800.0
    ok, msg = _sandbox_step(
        [
            "exec",
            "--name",
            name,
            "--workdir",
            sub,
            "--timeout",
            str(max(1, int(timeout))),
            "--no-tty",
            "--",
            "bash",
            sandbox_script,
        ],
        timeout=timeout + 90.0,
    )
    if not ok:
        sys.stderr.write("[executor] WARNING: sandbox repository verification failed: %s\n" % msg)
    return ok


def _sandbox_download(name: str, basename: str, workspace: Path) -> bool:
    """Sync the agent's edits (+ the evidence manifest) from the kept sandbox
    back into the host workspace. Best-effort: a failure is logged, not fatal —
    completeness is still judged by the evidence manifest on the host."""
    sub = "%s/%s" % (_SANDBOX_WORKDIR, basename)
    temp_parent = str(workspace.parent) if workspace.parent.is_dir() else None
    with tempfile.TemporaryDirectory(
        prefix=".%s-openshell-download-" % workspace.name,
        dir=temp_parent,
    ) as tmp:
        download_root = Path(tmp)
        ok, msg = _sandbox_step(["download", name, sub, str(download_root)], timeout=300.0)
        if ok:
            try:
                _merge_sandbox_download_tree(download_root, workspace)
            except Exception as exc:  # noqa: BLE001 - download sync is best-effort
                ok = False
                msg = "sandbox download merge failed: %s" % exc
    if not ok:
        sys.stderr.write("[executor] WARNING: sandbox download failed: %s\n" % msg)
    return ok


def _sandbox_delete(name: str) -> bool:
    ok, msg = _sandbox_step(["delete", name], timeout=120.0)
    if not ok:
        sys.stderr.write("[executor] WARNING: sandbox delete failed (possible leak): %s\n" % msg)
    return ok


def _sandbox_progress_interval() -> float:
    raw = (os.environ.get("MAC_OPENSHELL_PROGRESS_INTERVAL") or "5").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 5.0


def _sandbox_progress_snapshot(
    name: str, basename: str, workspace: Path
) -> Optional[Dict[str, str]]:
    sub = "%s/%s" % (_SANDBOX_WORKDIR, basename)
    mapped_repo = _sandbox_path_for_workspace_child(
        workspace, sub, os.environ.get("MAC_TASK_REPO_WORKTREE", "")
    )
    repo = mapped_repo or ""
    base = os.environ.get("MAC_TASK_REPO_BASE_SHA", "").strip()
    script = "\n".join(
        [
            "set -eu",
            "repo=%s" % shlex.quote(repo),
            "base=%s" % shlex.quote(base),
            "head=",
            "changed_count=0",
            "changed_digest=",
            'if [ -n "$repo" ] && [ -d "$repo" ]; then',
            '  head="$(git -C "$repo" rev-parse HEAD 2>/dev/null || true)"',
            '  changed="$( { git -C "$repo" status --porcelain 2>/dev/null; [ -n "$base" ] && git -C "$repo" diff --name-only "$base..HEAD" 2>/dev/null || true; } | sort -u )"',
            '  changed_count="$(printf %s "$changed" | sed "/^$/d" | wc -l | tr -d " ")"',
            '  changed_digest="$(printf %s "$changed" | sha256sum 2>/dev/null | cut -d" " -f1 || true)"',
            "fi",
            'printf "ready=1\\nhead=%%s\\nchanged_count=%%s\\nchanged_digest=%%s\\nmanifest=%%s\\n" "$head" "$changed_count" "$changed_digest" "$( [ -f %s/mac-evidence.json ] && echo 1 || echo 0 )"'
            % shlex.quote(sub),
        ]
    )
    ok, output = _sandbox_step(
        [
            "exec",
            "--name",
            name,
            "--workdir",
            sub,
            "--timeout",
            "15",
            "--no-tty",
            "--",
            "bash",
            "-c",
            script,
        ],
        timeout=30.0,
    )
    if not ok:
        return None
    snapshot: Dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {
            "ready",
            "head",
            "changed_count",
            "changed_digest",
            "manifest",
        }:
            snapshot[key] = value.strip()
    return snapshot if snapshot.get("ready") == "1" else None


class _SandboxProgressMonitor:
    """Transition-based observer of the real sandbox workspace."""

    def __init__(self, name: str, basename: str, workspace: Path, task_id: Any) -> None:
        self.name = name
        self.basename = basename
        self.workspace = workspace
        self.task_id = str(task_id) if task_id else None
        self.interval = _sandbox_progress_interval()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.ready = False
        self.mutated = False
        self.manifest_seen = False
        self.last_head = ""
        self.changed_file_count = 0
        self.changed_file_digest = ""
        self.stopped = False

    def start(self) -> None:
        if self.interval <= 0:
            return
        self.thread = threading.Thread(
            target=self._loop,
            name="mac-sandbox-progress-%s" % self.name,
            daemon=True,
        )
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop_event.wait(self.interval):
            self.observe()

    def observe(self) -> None:
        snapshot = _sandbox_progress_snapshot(
            self.name, self.basename, self.workspace
        )
        if snapshot is None:
            return
        if not self.ready:
            self.ready = True
            emit_telemetry(
                "sandbox_ready",
                task_id=self.task_id,
                sandbox=self.name,
                state="ready",
            )
        head = snapshot.get("head", "")
        try:
            changed_count = int(snapshot.get("changed_count") or 0)
        except ValueError:
            changed_count = 0
        changed = changed_count > 0 or (
            bool(head)
            and bool(os.environ.get("MAC_TASK_REPO_BASE_SHA"))
            and head != os.environ.get("MAC_TASK_REPO_BASE_SHA")
        )
        self.changed_file_count = changed_count
        self.changed_file_digest = snapshot.get("changed_digest", "")
        if changed and not self.mutated:
            self.mutated = True
            emit_telemetry(
                "sandbox_first_mutation",
                task_id=self.task_id,
                sandbox=self.name,
                state="sandbox_dirty",
                changed_file_count=changed_count,
                changed_file_digest=snapshot.get("changed_digest", ""),
                head_sha=head,
            )
        if head and head != self.last_head:
            self.last_head = head
            emit_telemetry(
                "sandbox_head_observed",
                task_id=self.task_id,
                sandbox=self.name,
                head_sha=head,
            )
        if snapshot.get("manifest") == "1" and not self.manifest_seen:
            self.manifest_seen = True
            emit_telemetry(
                "sandbox_manifest_observed",
                task_id=self.task_id,
                sandbox=self.name,
            )

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        if self.interval <= 0:
            return
        self.observe()
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=min(2.0, self.interval + 0.5))
        if not self.ready:
            emit_telemetry(
                "sandbox_observation_unavailable",
                task_id=self.task_id,
                level="warning",
                sandbox=self.name,
                state="unknown",
            )
        elif not self.mutated:
            emit_telemetry(
                "sandbox_no_effect",
                task_id=self.task_id,
                level="warning",
                sandbox=self.name,
                state="sandbox_clean",
            )

    def evidence(self) -> Dict[str, object]:
        return {
            "ready_observed": self.ready,
            "mutation_observed": self.mutated,
            "manifest_observed": self.manifest_seen,
            "head_sha": self.last_head,
            "changed_file_count": self.changed_file_count,
            "changed_file_digest": self.changed_file_digest,
        }


def _run_sandboxed(
    runner: Callable[..., Any], agent_argv: List[str], workspace: Path, audit_id: Any, opts: dict
) -> Any:
    """Run the agent through the OpenShell sandbox lifecycle: create (upload the
    workspace + run the agent, keep) -> download results -> delete. The agent
    runs confined. Harvest is attempted before teardown on every exit path,
    including runner exceptions and cancellation, so a useful partial patch or
    evidence manifest is not destroyed with the sandbox."""
    _force_child_yolo_env()  # truly silent agent; OpenShell is the guardrail
    _ensure_landlock_or_fail()
    name = _sandbox_name()
    basename = _workspace_basename(workspace)
    sandbox_workspace = "%s/%s" % (_SANDBOX_WORKDIR, basename)
    runtime_files = _write_sandbox_runtime_files(workspace, sandbox_workspace)
    try:
        create_argv = _build_sandbox_create_argv(name, workspace, basename, agent_argv)
    except Exception:
        for path in runtime_files:
            path.unlink(missing_ok=True)
        raise
    runner_completed = False
    harvested = False
    kept = _truthy(os.environ.get("MAC_OPENSHELL_KEEP"))
    emit_telemetry(
        "sandbox_started",
        task_id=str(audit_id) if audit_id else None,
        sandbox=name,
        workspace=basename,
    )
    progress = _SandboxProgressMonitor(name, basename, workspace, audit_id)
    progress.start()
    try:
        result = runner(create_argv, workspace, audit_id, opts)
        runner_completed = True
        progress.stop()
        emit_telemetry(
            "sandbox_agent_completed",
            task_id=str(audit_id) if audit_id else None,
            level="info" if int(getattr(result, "returncode", 1)) == 0 else "warning",
            sandbox=name,
            returncode=int(getattr(result, "returncode", 1)),
        )
        verification_expected = (
            isinstance(opts.get("task"), dict)
            and task_is_repo_coupled(opts["task"])
            and bool(_repository_contract_test_command(opts["task"]))
        )
        if verification_expected:
            emit_telemetry(
                "sandbox_verification_started",
                task_id=str(audit_id) if audit_id else None,
                sandbox=name,
            )
        verification = _sandbox_run_repository_verification(
            name, basename, workspace, opts.get("task")
        )
        if verification is not None:
            emit_telemetry(
                "sandbox_verification_completed",
                task_id=str(audit_id) if audit_id else None,
                level="info" if verification else "warning",
                sandbox=name,
                passed=verification,
            )
        return result
    finally:
        active_error = sys.exc_info()[1]
        progress.stop()
        harvested = _sandbox_download(name, basename, workspace)
        salvage = {
            "schema": "mac.openshell_salvage.v1",
            "sandbox": name,
            "runner_completed": runner_completed,
            "harvest_attempted": True,
            "harvested": harvested,
            "kept": kept,
            "error": str(active_error) if active_error is not None else "",
            "progress": progress.evidence(),
            "at": utcnow(),
        }
        try:
            (workspace / "openshell-salvage.json").write_text(
                json.dumps(salvage, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            sys.stderr.write("[executor] WARNING: could not write sandbox salvage record: %s\n" % exc)
        emit_telemetry(
            "sandbox_harvested",
            task_id=str(audit_id) if audit_id else None,
            level="info" if harvested else "warning",
            sandbox=name,
            runner_completed=runner_completed,
            harvested=harvested,
        )
        deleted = False
        if not kept:
            deleted = _sandbox_delete(name)
            emit_telemetry(
                "sandbox_deleted",
                task_id=str(audit_id) if audit_id else None,
                level="info" if deleted else "warning",
                sandbox=name,
                deleted=deleted,
            )
        for path in runtime_files:
            path.unlink(missing_ok=True)
        (workspace / ".mac-sandbox-repository-verify.sh").unlink(missing_ok=True)


def _force_child_yolo_env() -> None:
    """Make the agent subprocess inherit HERMES_YOLO_MODE=1.

    Hermes freezes its YOLO/approval bypass from HERMES_YOLO_MODE at *import*
    time (tools/approval.py: ``_YOLO_MODE_FROZEN``). The ``--yolo`` CLI flag sets
    that env only AFTER Hermes has already imported approval.py, so the freeze
    can capture False and ``--yolo`` silently FAILS to bypass approval — the
    agent still prompts. Setting the env here, in the executor, before the child
    is spawned (the child inherits ``os.environ``) guarantees it is present at
    the child's process start, before any import, so the freeze captures True
    and approval is genuinely bypassed. This is the executor-side fix for the
    import-order freeze; ``approvals.mode=off`` in the deployed config.yaml is
    the config-side lever that covers the gateway too.
    """
    os.environ["HERMES_YOLO_MODE"] = "1"


def _unsandboxed_agent_argv(agent_argv: List[str]) -> List[str]:
    """Gate an already-built agent argv for an UNSANDBOXED run.

    The agent runs with its own approval bypass (Hermes ``--yolo`` or a coding
    agent's ``--dangerously-*``); running that unsandboxed is unguarded,
    permitted ONLY via ``MAC_ALLOW_UNSANDBOXED_YOLO`` (default "1" to preserve
    the current live fleet; "0" fails closed). Raises when fail-closed. The
    sandboxed path does not go through here — see :func:`_invoke_agent`.
    """
    default_unsandboxed = "0" if _openshell_required_for_local_agent() else "1"
    if _truthy(os.environ.get("MAC_ALLOW_UNSANDBOXED_YOLO", default_unsandboxed)):
        _force_child_yolo_env()
        sys.stderr.write(
            "[executor] WARNING: launching an approval-bypassed agent WITHOUT an "
            "OpenShell sandbox (MAC_OPENSHELL_SANDBOX unset) — the agent's own "
            "approval gate is disabled and there is no sandbox confinement. Enable "
            "MAC_OPENSHELL_SANDBOX=1 (with a policy), or set "
            "MAC_ALLOW_UNSANDBOXED_YOLO=0 to fail closed.\n"
        )
        return agent_argv
    raise RuntimeError(
        "refusing to launch an approval-bypassed agent without an OpenShell sandbox: "
        "MAC_OPENSHELL_SANDBOX is unset and MAC_ALLOW_UNSANDBOXED_YOLO is disabled. "
        "Set MAC_OPENSHELL_SANDBOX=1 with a policy to enforce silently, or "
        "MAC_ALLOW_UNSANDBOXED_YOLO=1 to explicitly allow unsandboxed YOLO."
    )


def _record_runner_choice(
    target: str, rationale: List[str], *, task_id: str = ""
) -> None:
    """Make the coding-agent-vs-gateway routing decision legible (best-effort).

    Mirrors :func:`mac.agent_provider.record_provider_decision`: a secret-free
    line so an operator (or the agent) can answer "why did this task run on
    Claude / Codex / Cursor / the gateway?" rather than facing a silent choice.
    """
    sys.stderr.write(
        "[executor] coding-agent routing: %s (%s)\n" % (target, "; ".join(rationale) or "no rationale")
    )
    try:
        emit_telemetry(
            "runner_selected",
            task_id=task_id or None,
            level="info",
            schema="mac.coding_agent.routing.v1",
            runner=target,
            rationale=list(rationale),
        )
    except Exception:  # noqa: BLE001 - telemetry must never break execution
        pass


def _repo_requires_verified_coding_agent(task: Any) -> bool:
    """Whether repo work must fail closed without a verified coding CLI.

    Default true (fail-closed): fleet-wide since the hermes retirement sequence
    set MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT=1 as the build_mac_env default
    and bootstrap-openshell.sh writes it unconditionally on every host.  Hermes
    chat fallback is no longer acceptable for repository tasks in the OpenShell
    executor path.

    Operators who have provisioned a durable in-sandbox coding-agent auth
    mechanism and want to re-enable the Hermes fallback may set
    ``MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT=0`` in mac.env; the setdefault
    in build_mac_env will not clobber an explicit override.
    """
    if not (isinstance(task, dict) and task_is_repo_coupled(task)):
        return False
    return _truthy(os.environ.get("MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT"))


def _coding_agent_required_failure_argv(reason: str) -> List[str]:
    msg = (
        "repository tasks under OpenShell require a verified in-sandbox coding "
        "agent; %s" % (reason or "no coding agent was verified")
    )
    code = "import sys; sys.stderr.write(%r + '\\n'); raise SystemExit(42)" % msg
    return [_hermes_python(), "-c", code]


def _coding_agent_auth_is_safe_for_openshell(choice: Any) -> bool:
    """Whether the selected coding-agent auth can be copied into OpenShell safely.

    Codex OAuth state in ``~/.codex/auth.json`` is a rotating credential. Because
    OpenShell currently supports upload-copy semantics rather than a persistent
    writable mount for this path, a preflight or task sandbox can consume the
    refresh token and leave the host copy stale. Treat that auth source as
    unavailable under OpenShell unless the operator explicitly opts into the
    risk for a one-off debug run.
    """
    if (
        getattr(choice, "agent", "") == "codex"
        and getattr(choice, "auth_source", "") == "~/.codex/auth.json"
        and not _truthy(os.environ.get("MAC_OPENSHELL_ALLOW_CODEX_FILE_AUTH"))
    ):
        sys.stderr.write(
            "[executor] coding-agent sandbox preflight (codex): skipped "
            "(~/.codex/auth.json is rotating file auth; using Hermes gateway)\n"
        )
        return False
    return True


def _coding_agent_mcp_config_path(workspace: Path, choice: Any) -> Optional[str]:
    """Materialize an MCP config registering the messaging server, return its path.

    Only when messaging-MCP is enabled and the agent supports per-invocation MCP
    (Claude Code). Best-effort: any failure returns ``None`` — hub parity via the
    ``mac`` CLI + runtime context is unaffected. Not used on the sandboxed path
    (the host config-file path and host MCP-server interpreter do not resolve
    inside the sandbox — see :func:`_agent_argv`).
    """
    try:
        from . import coding_agent as _ca

        if not (_ca.messaging_mcp_enabled(os.environ) and _ca.supports_per_invocation_mcp(choice.agent)):
            return None
        doc = _ca.mcp_config_document(_mcp_serve_argv(), name="hermes")
        path = workspace / ".mac-coding-agent-mcp.json"
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001 - MCP wiring is best-effort parity, not required
        return None


# Per-process cache of the in-sandbox preflight verdict, keyed by (agent, binary).
# The worker is long-lived; we verify once and reuse, so the LLM-backed probe
# does not run on every task.
_SANDBOX_PREFLIGHT_CACHE: Dict[tuple, bool] = {}


def _coding_agent_preflight_timeout() -> float:
    raw = (os.environ.get("MAC_CODING_AGENT_PREFLIGHT_TIMEOUT") or "").strip()
    try:
        val = float(raw)
        return val if val > 0 else 180.0
    except ValueError:
        return 180.0


def _build_sandbox_probe_argv(
    name: str, agent_argv: List[str], private_dir: Path
) -> List[str]:
    """Build a credential-free process argv for the coding-agent probe."""
    if "mac.agent_command" not in agent_argv:
        raise ValueError("sandbox probe must use the private-file command wrapper")
    argv: List[str] = [_openshell_bin(), "sandbox", "create", "--no-auto-providers"]
    argv += ["--policy", _resolve_openshell_policy(), "--name", name]
    extra = (os.environ.get("MAC_OPENSHELL_CREATE_ARGS") or "").strip()
    if extra:
        extra_argv = shlex.split(extra)
        if "--env" in extra_argv or "--" in extra_argv:
            raise ValueError("MAC_OPENSHELL_CREATE_ARGS may not contain --env or --")
        argv += extra_argv
    sandbox_dir = "/sandbox/%s" % private_dir.name
    argv += ["--upload", "%s:/sandbox" % private_dir]
    inner = "\n".join(
        [
            "cd %s" % shlex.quote(sandbox_dir),
            "set -a",
            ". ./.mac-openshell-env.sh",
            "set +a",
            "rm -f ./.mac-openshell-env.sh",
            "exec %s" % shlex.join(agent_argv),
        ]
    )
    argv += ["--", "bash", "-lc", inner]
    return argv


def _openshell_probe(create_argv: List[str], *, timeout: float) -> "tuple[int, str]":
    """Run a one-shot ``sandbox create`` probe; return (returncode, combined output).
    Best-effort: any failure returns a non-zero code (never raises)."""
    try:
        proc = subprocess.run(create_argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001 - a probe failure must mean "not ready", not a crash
        return 1, str(exc)


def _run_coding_agent_preflight(choice: Any) -> bool:
    """Verify, inside a throwaway OpenShell sandbox, that the coding agent runs
    end-to-end: it must execute, authenticate, reach the provider, and echo the
    sentinel back. Proves the agent will actually work for a real sandboxed task
    (binary present + creds resolvable + egress allowed) — host-side availability
    is NOT sufficient. The probe sandbox is always deleted."""
    from . import coding_agent as _ca

    name = "mac-codingcap-%s-%d" % (choice.agent, os.getpid())
    with tempfile.TemporaryDirectory(prefix="mac-coding-agent-probe-") as tmp:
        private_dir = Path(tmp)
        probe_argv = _ca.coding_agent_argv(choice, PROMPT_SENTINEL)
        bundle = _write_agent_command_bundle(
            private_dir, _ca.PREFLIGHT_PROMPT, probe_argv
        )
        _write_private_shell_env(
            private_dir / ".mac-openshell-env.sh",
            {**_openshell_environment(), "HOME": _SANDBOX_HOME},
        )
        sandbox_dir = "/sandbox/%s" % private_dir.name
        try:
            rc, out = _openshell_probe(
                _build_sandbox_probe_argv(
                    name,
                    bundle.argv(sandbox_workspace=sandbox_dir),
                    private_dir,
                ),
                timeout=_coding_agent_preflight_timeout(),
            )
        finally:
            bundle.cleanup()
            _sandbox_step(["delete", name], timeout=60.0)
    ok = rc == 0 and _ca.PREFLIGHT_SENTINEL in out
    sys.stderr.write(
        "[executor] coding-agent sandbox preflight (%s): %s\n"
        % (choice.agent, "OK" if ok else "FAILED (rc=%s) — falling back to gateway" % rc)
    )
    return ok


def _coding_agent_sandbox_ok(choice: Any) -> bool:
    """Whether a coding agent may be used on the SANDBOXED path.

    ``MAC_CODING_AGENT_SANDBOX`` modes:
      * ``verify`` (default) — gate on a cached in-sandbox preflight that actually
        runs the agent; only enable when it works there.
      * ``trust`` / ``1`` — assume the sandbox image is provisioned; skip the probe.
      * ``off`` / ``0`` — never use a coding agent when sandboxed (always Hermes).
    """
    mode = (os.environ.get("MAC_CODING_AGENT_SANDBOX") or "verify").strip().lower()
    if mode in {"off", "0", "false", "no"}:
        return False
    if mode in {"trust", "1", "true", "yes", "skip"}:
        return True
    if not _coding_agent_auth_is_safe_for_openshell(choice):
        return False
    key = (choice.agent, choice.binary)
    if key not in _SANDBOX_PREFLIGHT_CACHE:
        _SANDBOX_PREFLIGHT_CACHE[key] = _run_coding_agent_preflight(choice)
    return _SANDBOX_PREFLIGHT_CACHE[key]


def _agent_argv(prompt: str, workspace: Path, *, confined: bool, task: Any = None) -> List[str]:
    """Pick the agent runner: a coding-agent CLI when one is available + authed
    (and — when OpenShell-confined — verified to actually work inside the sandbox),
    else the vendored Hermes -> LLM gateway argv.

    Coding-agent CLIs (Claude Code, Codex, Cursor) authenticate against a
    subscription/seat rather than a metered API token, so they are preferred for
    cost (see :mod:`mac.coding_agent`). Full mac-runtime parity on this path: the
    CLI runs in the prepared checkout (the ``mac`` CLI + runtime context + hub
    env give it the same hub tool surface Hermes has), receives the same
    structured task/evidence prompt, and — where the CLI supports per-invocation
    MCP, on the unconfined path — the messaging MCP server.

    When OpenShell confinement is in effect (``confined`` — per-task wrap or the
    production supervisor) enablement is gated on :func:`_coding_agent_sandbox_ok`
    (a real in-sandbox preflight by default), because a host-side ``which``/cred
    check does NOT prove the agent works inside the confined sandbox.

    Fail-closed is now the fleet-wide default: ``MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT``
    defaults to ``1`` via build_mac_env and bootstrap-openshell.sh, so when no
    verified coding CLI is present the executor fails with the
    ``coding-agent-required`` path instead of degrading to the Hermes chat
    fallback.  Operators may opt out by setting
    ``MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT=0`` in mac.env.
    """
    from . import coding_agent as _ca

    repo_requires_agent = confined and _repo_requires_verified_coding_agent(task)
    task_id = str(task.get("id") or "").strip() if isinstance(task, dict) else ""
    choice = _ca.resolve_coding_agent()
    rationale = list(choice.rationale)
    if not choice.available:
        if repo_requires_agent:
            reason = "no host coding agent is available/authenticated"
            rationale.append(reason)
            _record_runner_choice(
                "coding-agent-required", rationale, task_id=task_id
            )
            return _coding_agent_required_failure_argv(reason)
        _record_runner_choice("hermes-gateway", rationale, task_id=task_id)
        return _hermes_argv(prompt)
    if confined and not _coding_agent_sandbox_ok(choice):
        reason = "%s not verified inside the OpenShell sandbox" % choice.agent
        if repo_requires_agent:
            rationale.append(reason)
            _record_runner_choice(
                "coding-agent-required", rationale, task_id=task_id
            )
            return _coding_agent_required_failure_argv(reason)
        rationale.append("%s; using gateway" % reason)
        _record_runner_choice("hermes-gateway", rationale, task_id=task_id)
        return _hermes_argv(prompt)

    # MCP wiring is unconfined-only: the host config path + host MCP-server
    # interpreter do not reliably resolve inside the sandbox (messaging-MCP parity
    # there is provisioned image-side). Hub parity (mac CLI + runtime context)
    # still applies regardless.
    mcp_path = None if confined else _coding_agent_mcp_config_path(workspace, choice)
    if confined:
        rationale.append("verified inside the OpenShell sandbox")
    _record_runner_choice(choice.agent, rationale, task_id=task_id)
    return _ca.coding_agent_argv(choice, prompt, mcp_config_path=mcp_path)


def _executor_backend() -> str:
    """Which agent runtime drives a task: ``hermes`` (default) or ``acp``.

    ACP (ADR 0006) is opt-in via ``MAC_EXECUTOR_BACKEND=acp`` so Hermes stays the
    default until parity; the external agent command is ``MAC_ACP_AGENT_CMD``."""
    return (os.environ.get("MAC_EXECUTOR_BACKEND") or "hermes").strip().lower()


def _acp_agent_argv() -> List[str]:
    """The external ACP agent command (shell-split). Required for backend=acp."""
    import shlex

    return shlex.split((os.environ.get("MAC_ACP_AGENT_CMD") or "").strip())


def _acp_update_action_event(audit_id: Any, session_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Map one ACP ``session/update`` notification to a mac action-event record."""
    inner = params.get("update") or {}
    return {
        "task_id": audit_id,
        "session_id": session_id or params.get("sessionId"),
        "actor": "mac-acp",
        "action_type": "acp.session_update",
        "action_name": str(inner.get("sessionUpdate") or "update"),
        "outcome": "unknown",
        "severity": "info",
        "attributes": {"acp_update": inner},
    }


def _acp_permission_handler(audit_id: Any) -> Callable[[Any], Any]:
    """ACP ``session/request_permission`` handler (Phase 3).

    Evaluates each request through :func:`mac.acp.permission.evaluate_permission`
    instead of blanket auto-approving. The OpenShell *kernel sandbox* remains the
    real gate — when sandboxed the decision short-circuits to allow ("sandbox-
    enforced") and the ACP prompt is advisory. Unsandboxed, the decision consults
    the parsed OpenShell *policy* (network lockdown denies egress; an empty
    read_write set denies writes); with no policy it defaults to allow (Phase-1
    parity), flippable to deny via ``MAC_ACP_PERMISSION_MODE=deny``.

    On *allow* it selects the first ``allow``-kind option the agent offered (else
    the first option); on *deny* it selects a ``reject``-kind option if one is
    offered, else returns a CANCELLED outcome. Every decision + its reason is
    recorded to ``/action-events`` (``attributes.permission_reason``)."""
    from mac.acp.permission import evaluate_permission, load_openshell_policy
    from mac.acp.protocol import PermissionOutcome, RequestPermissionResult

    sandboxed = _openshell_enabled()
    # Load the policy only when it can actually change the decision (unsandboxed
    # under policy mode). Best-effort: a missing/unreadable policy -> None.
    policy = None if sandboxed else load_openshell_policy()

    def _handler(params: Any) -> Any:
        options = list(getattr(params, "options", None) or [])
        tool_call = getattr(params, "tool_call", {}) or {}
        decision = evaluate_permission(tool_call, policy=policy, sandboxed=sandboxed)

        if decision.allow:
            chosen = next((o for o in options if str(o.kind or "").startswith("allow")), None)
            chosen = chosen or (options[0] if options else None)
        else:
            # Prefer an explicit reject option when the agent offered one.
            chosen = next((o for o in options if str(o.kind or "").startswith("reject")), None)

        _hub_post(
            "/action-events",
            {
                "task_id": audit_id,
                "session_id": getattr(params, "session_id", None),
                "actor": "mac-acp",
                "action_type": "acp.permission",
                "action_name": str(tool_call.get("title") or tool_call.get("toolCallId") or "tool_call"),
                "outcome": "allowed" if decision.allow else "denied",
                "severity": "info",
                "attributes": {
                    "tool_call": tool_call,
                    "permission_reason": decision.reason,
                    "allowed": decision.allow,
                },
            },
        )
        if chosen is not None:
            return RequestPermissionResult(outcome=PermissionOutcome.SELECTED, option_id=chosen.option_id)
        return RequestPermissionResult(outcome=PermissionOutcome.CANCELLED)

    return _handler


#: AgentBus content_type for a mirrored ACP session/update chunk.
_ACP_AGENTBUS_CONTENT_TYPE = "application/vnd.mac.acp.update+json"
#: AgentBus topic for the mirrored ACP update stream.
_ACP_AGENTBUS_TOPIC = "acp.session_update"


class _AcpAgentBusMirror:
    """Best-effort mirror of an ACP ``session/update`` stream onto AgentBus.

    Lifecycle (all via :func:`_hub_post`, so failures never raise):

      * :meth:`open`   -> ``POST /agentbus/streams`` once at run start. mac is
        both sender and recipient (a self-stream: the worker agent is the only
        party, and AgentBus requires a concrete recipient), so the worker's own
        ``local_agent_id`` is used for both ends.
      * :meth:`append` -> ``POST /agentbus/streams/{id}/chunks`` per update.
      * :meth:`close`  -> ``POST /agentbus/streams/{id}/close`` at run end.

    If the open fails (no hub env, AgentBus error, unknown agent) the mirror is
    inert: :attr:`stream_id` stays ``None`` and append/close are no-ops, so the
    /action-events path is unaffected."""

    def __init__(self, audit_id: Any) -> None:
        self._agent_id = local_agent_id()
        self._audit_id = audit_id
        self.stream_id: Optional[str] = None

    def open(self) -> None:
        payload: Dict[str, Any] = {
            "sender_agent_id": self._agent_id,
            "recipient_agent_id": self._agent_id,
            "topic": _ACP_AGENTBUS_TOPIC,
            "content_type": _ACP_AGENTBUS_CONTENT_TYPE,
            "headers": {"schema": "mac.acp.session_update.v1", "task_id": self._audit_id},
        }
        if self._audit_id:
            payload["task_id"] = str(self._audit_id)
        resp = _hub_post_json("/agentbus/streams", payload)
        if isinstance(resp, dict):
            sid = resp.get("id")
            if isinstance(sid, str) and sid:
                self.stream_id = sid

    def append(self, session_id: str, params: Dict[str, Any]) -> None:
        if not self.stream_id:
            return
        _hub_post(
            "/agentbus/streams/%s/chunks" % self.stream_id,
            {
                "sender_agent_id": self._agent_id,
                "content_type": _ACP_AGENTBUS_CONTENT_TYPE,
                "payload": {"sessionId": session_id, "update": params.get("update") or {}},
            },
        )

    def close(self, status: str = "closed") -> None:
        if not self.stream_id:
            return
        # The close handler takes sender_agent_id + status as query params.
        _hub_post(
            "/agentbus/streams/%s/close?sender_agent_id=%s&status=%s"
            % (self.stream_id, urllib.parse.quote(self._agent_id), urllib.parse.quote(status)),
            {},
        )


def _invoke_acp_agent(
    prompt: str, workspace: Path, audit_id: Any, opts: dict, *, executor: Any = None
) -> "subprocess.CompletedProcess":
    """Drive an external ACP agent (ADR 0006) for one task turn.

    Streams every ``session/update`` to the hub's ``/action-events`` ledger and
    bridges ``session/request_permission`` through :func:`_acp_permission_handler`.
    Returns a :class:`subprocess.CompletedProcess` so the downstream
    finalizer/evidence flow is unchanged — the deterministic git finalizer
    remains the real proof of work regardless of which agent produced it."""
    from mac.acp import ACPExecutor
    from mac.acp.protocol import ContentBlockType, SessionUpdateKind, StopReason

    if executor is None:
        argv = _acp_agent_argv()
        if not argv:
            return subprocess.CompletedProcess(
                ["acp"], 1, "", "MAC_EXECUTOR_BACKEND=acp but MAC_ACP_AGENT_CMD is unset"
            )
        executor = ACPExecutor(argv, cwd=str(workspace))

    text_chunks: List[str] = []
    # AgentBus mirror (ADR 0006 Phase 3): mirror the session/update stream onto an
    # AgentBus chunk stream alongside the /action-events ledger. Best-effort and
    # failure-tolerant — if the open fails we simply skip the chunk posts. Only
    # runs under backend=acp, so the Hermes path takes on no extra latency.
    mirror = _AcpAgentBusMirror(audit_id)
    mirror.open()

    def _on_update(params: Dict[str, Any]) -> None:
        inner = params.get("update") or {}
        if inner.get("sessionUpdate") in (
            SessionUpdateKind.AGENT_MESSAGE_CHUNK,
            SessionUpdateKind.AGENT_THOUGHT_CHUNK,
        ):
            content = inner.get("content") or {}
            if isinstance(content, dict) and content.get("type") == ContentBlockType.TEXT:
                text_chunks.append(str(content.get("text") or ""))
        _hub_post("/action-events", _acp_update_action_event(audit_id, str(params.get("sessionId") or ""), params))
        mirror.append(str(params.get("sessionId") or ""), params)

    argv_label = list(getattr(executor, "_argv", ["acp"]))
    try:
        run = executor.run(
            prompt,
            on_update=_on_update,
            on_permission=_acp_permission_handler(audit_id),
            timeout=opts.get("timeout"),
        )
    except Exception as exc:  # noqa: BLE001 - a backend failure must finalize, not crash the loop
        mirror.close(status="errored")
        return subprocess.CompletedProcess(argv_label, 1, "".join(text_chunks), "ACP agent run failed: %s" % exc)
    mirror.close()
    rc = 0 if run.stop_reason == StopReason.END_TURN else 1
    stderr = "" if rc == 0 else "ACP agent stopped with reason: %s" % run.stop_reason
    return subprocess.CompletedProcess(argv_label, rc, "".join(text_chunks), stderr)


def _invoke_agent(
    runner: Callable[..., Any], prompt: str, workspace: Path, audit_id: Any, opts: dict
) -> Any:
    """Run the agent for one task, atomically coupling --yolo to enforcement.

    Invariant: an approval-bypassed agent (Hermes ``--yolo`` or a coding agent's
    ``--dangerously-*``) is only used when the run is confined by OpenShell, so we
    never launch an *unguarded* bypass agent.
      * backend=acp      -> drive an external ACP agent (ADR 0006); confinement
        is the OpenShell sandbox + the permission bridge.
      * sandbox enabled  -> full OpenShell lifecycle (upload workspace, run the
        agent confined, download results, delete). Fails closed if no policy
        resolves or the kernel can't enforce Landlock.
      * sandbox disabled -> direct run, gated by MAC_ALLOW_UNSANDBOXED_YOLO.
    The agent argv is a detected coding-agent CLI when one is available + authed,
    else the Hermes -> gateway argv (see :func:`_agent_argv`).
    Returns the runner's result (carries .returncode)."""
    if _executor_backend() == "acp":
        return _invoke_acp_agent(prompt, workspace, audit_id, opts)
    # `wrap` is the per-task OpenShell wrap launch model; `confined` is whether
    # OpenShell confinement is in effect by EITHER model — the per-task wrap or
    # the production supervisor (which runs this whole process inside a sandbox,
    # with MAC_OPENSHELL_SANDBOX off but the agent required). Coding-agent
    # enablement is gated on `confined`, not `wrap`.
    wrap = _openshell_enabled()
    confined = wrap or _openshell_required_for_local_agent()
    agent_argv = _agent_argv(
        PROMPT_SENTINEL, workspace, confined=confined, task=opts.get("task")
    )
    bundle = _write_agent_command_bundle(workspace, prompt, agent_argv)
    try:
        if wrap:
            sandbox_workspace = "%s/%s" % (
                _SANDBOX_WORKDIR,
                _workspace_basename(workspace),
            )
            return _run_sandboxed(
                runner,
                bundle.argv(sandbox_workspace=sandbox_workspace),
                workspace,
                audit_id,
                opts,
            )
        return runner(
            _unsandboxed_agent_argv(bundle.argv()), workspace, audit_id, opts
        )
    finally:
        bundle.cleanup()


def _agent_timeout() -> Optional[float]:
    """Bound a single agent run so a wedged TokenHub turn can't hang the loop
    forever. Default 900s; set MAC_EXECUTOR_AGENT_TIMEOUT=0 to disable."""
    raw = (os.environ.get("MAC_EXECUTOR_AGENT_TIMEOUT") or "").strip()
    if not raw:
        return 900.0
    try:
        val = float(raw)
    except ValueError:
        return 900.0
    return val if val > 0 else None


def _manifest_is_complete(task_workspace: Path) -> bool:
    """True when the agent (or a deterministic finalizer) already wrote a
    complete, typed evidence manifest — i.e. real verified work exists."""
    path = task_workspace / "mac-evidence.json"
    if not path.exists():
        return False
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not (
        isinstance(manifest, dict)
        and str(manifest.get("status") or "").lower() == "complete"
        and bool(manifest.get("evidence_type"))
    ):
        return False
    if str(manifest.get("evidence_type") or "").strip().lower() != "review_verdict":
        return True

    # A deterministic review finalizer always writes a complete, signed
    # manifest, including when the model never produced a semantic verdict.
    # Do not let the generic timeout-salvage path turn that fail-closed record
    # into a successful review execution.
    if str(manifest.get("semantic_verdict") or "").strip().lower() not in {
        "approved",
        "rejected",
    }:
        return False
    experiment = manifest.get("review_experiment")
    if isinstance(experiment, dict) and experiment.get("blind"):
        protocol = experiment.get("protocol")
        if not isinstance(protocol, dict) or protocol.get("protocol_compliant") is not True:
            return False
    return True


def main(*, runner: Callable[..., Any] = run_audited_command) -> int:
    task_file = Path(os.environ["MAC_TASK_FILE"])
    task_workspace = Path(os.environ["MAC_TASK_WORKSPACE"])
    task_payload = json.loads(task_file.read_text(encoding="utf-8"))
    task = task_payload.get("task", task_payload)
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    review_context = metadata.get("review_context") if isinstance(metadata, dict) else None
    is_review = isinstance(review_context, dict)
    task_id = task.get("id") if isinstance(task, dict) else None

    # NeMo Relay: open an Agent scope for this executor run (no-op when
    # relay is absent or MAC_RELAY_OBSERVABILITY != '1').
    session_id = str(task_id or "unknown")
    with relay_observability.create_agent_scope(session_id):
        try:
            rc = _run_executor(
                runner=runner,
                task=task,
                task_file=task_file,
                task_workspace=task_workspace,
                task_id=task_id,
                review_context=review_context,
                is_review=is_review,
            )
        finally:
            relay_observability.flush()
    return rc


def _run_executor(
    *,
    runner: Callable[..., Any],
    task: Any,
    task_file: Path,
    task_workspace: Path,
    task_id: Any,
    review_context: Any,
    is_review: bool,
) -> int:
    """Inner executor body extracted so the relay scope wraps the whole run."""
    started = time.monotonic()
    # Planning-phase flag — determined after the scope estimate below.
    _is_planning = False
    if is_review:
        prompt = build_review_prompt(task, task_workspace, review_context)
        lessons: List[str] = []
    else:
        # Memory feed (in): recall prior deployment lessons so the agent works
        # with the fleet's hindsight. Best-effort — never blocks the run.
        lessons = recall_deployment_lessons(task)
        # Prompt is built after planning-phase decision below.
        prompt = ""
    emit_telemetry(
        "started",
        task_id=task_id,
        kind="review" if is_review else "task",
        recalled_lessons=len(lessons),
        sandboxed=_openshell_enabled(),
    )

    # Scope-estimate preflight (scope-01): on the FIRST attempt of a non-review
    # task, compute a deterministic scope estimate and record it as
    # metadata.scope_estimate on the hub.  Best-effort — never blocks the run.
    if not is_review:
        try:
            estimate = maybe_preflight_scope_estimate(task)
            if estimate is not None:
                emit_telemetry(
                    "scope_estimated",
                    task_id=task_id,
                    size=estimate.get("size"),
                    estimated_units=estimate.get("estimated_units"),
                    signals=estimate.get("signals", []),
                )
                # Merge the just-computed estimate into the local task dict so
                # is_planning_phase() can read it without another hub round-trip.
                metadata_local = task.get("metadata")
                if not isinstance(metadata_local, dict):
                    metadata_local = {}
                    task["metadata"] = metadata_local  # type: ignore[index]
                metadata_local.setdefault("scope_estimate", estimate)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("scope estimate preflight failed: %s\n" % exc)

    # Planning-phase execution (plan-01): when scope_estimate=large or
    # metadata.plan_first=true, the first run PLANS instead of executing.
    if not is_review:
        try:
            _is_planning = is_planning_phase(task)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("planning phase check failed: %s\n" % exc)
            _is_planning = False

    if not is_review:
        if _is_planning:
            # plan-learn-01: enrich the planning prompt with prior decomposition
            # shapes for similar tasks so the second big migration starts from
            # the first one's shape.  Best-effort — never blocks the run.
            try:
                plan_lessons = recall_plan_lessons(task)
            except Exception:  # noqa: BLE001
                plan_lessons = []
            combined_lessons = (lessons or []) + (plan_lessons or [])
            prompt = build_planning_prompt(task, task_file, combined_lessons)
            emit_telemetry("planning_phase_started", task_id=task_id, level="info")
        else:
            prompt = build_task_prompt(task, task_file, lessons)

    audit_task_id = review_context.get("task_id") if is_review else task_id
    assignment = _review_experiment_assignment(task) if is_review else {}
    blind_protocol_failed = False
    if assignment.get("blind"):
        executor_evidence = task_workspace / "executor-evidence.json"
        legacy_withheld_evidence = (
            task_workspace / ".mac-withheld-executor-evidence.json"
        )
        independent_findings = task_workspace / "review-independent-findings.json"
        evidence_hidden = False
        evidence_payload: Optional[bytes] = None
        discovery_started = time.monotonic()
        if legacy_withheld_evidence.exists():
            if not executor_evidence.exists():
                legacy_withheld_evidence.replace(executor_evidence)
            else:
                legacy_withheld_evidence.unlink()
        if independent_findings.exists():
            independent_findings.replace(
                task_workspace / "review-independent-findings.previous.json"
            )
        if executor_evidence.exists():
            # Hold the bounded evidence payload in the host process rather than
            # renaming it inside the workspace. A dotfile in the workspace is
            # still visible to both direct and OpenShell agent invocations.
            evidence_payload = executor_evidence.read_bytes()
            executor_evidence.unlink()
            evidence_hidden = True
        try:
            discovery_result = _invoke_agent(
                runner,
                build_blind_review_discovery_prompt(task, task_workspace, assignment),
                task_workspace,
                str(audit_task_id) if audit_task_id else None,
                {
                    "execution_kind": "review_discovery",
                    "timeout": _agent_timeout(),
                    "task": task,
                },
            )
        finally:
            if evidence_payload is not None:
                executor_evidence.write_bytes(evidence_payload)
        discovery_duration_ms = (time.monotonic() - discovery_started) * 1000.0
        discovery_manifest = task_workspace / "mac-evidence.json"
        if discovery_manifest.exists():
            discovery_manifest.replace(
                task_workspace / "review-independent-draft-evidence.json"
            )
        protocol = _blind_review_protocol(
            task_workspace,
            assignment,
            discovery_result,
            duration_ms=discovery_duration_ms,
            evidence_hidden=evidence_hidden,
        )
        (task_workspace / "review-protocol.json").write_text(
            json.dumps(protocol, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        emit_telemetry(
            "review_discovery_completed",
            task_id=task_id,
            returncode=discovery_result.returncode,
            protocol_compliant=protocol["protocol_compliant"],
            findings=protocol["independent_findings_count"],
            duration_ms=discovery_duration_ms,
        )
        blind_protocol_failed = not bool(protocol["protocol_compliant"])

    if blind_protocol_failed:
        # The blind treatment is already invalid. Running adjudication would
        # spend a second model budget on a sample that can no longer be used,
        # and historically allowed a missing discovery artifact to masquerade
        # as a semantic code rejection. Preserve the discovery output and make
        # the review attempt fail distinctly so reviewer selection can retry or
        # choose another eligible reviewer without re-executing the patch.
        result = subprocess.CompletedProcess(
            getattr(discovery_result, "args", ["review_discovery"]),
            65,
            getattr(discovery_result, "stdout", "") or "",
            "\n".join(
                part
                for part in (
                    (getattr(discovery_result, "stderr", "") or "").strip(),
                    "blind review discovery protocol was not completed",
                )
                if part
            ),
        )
        emit_telemetry(
            "review_protocol_failed",
            task_id=task_id,
            level="warning",
            phase="discovery",
            protocol_compliant=False,
        )
    else:
        result = _invoke_agent(
            runner,
            prompt,
            task_workspace,
            str(audit_task_id) if audit_task_id else None,
            {"execution_kind": "review" if is_review else "task", "timeout": _agent_timeout(), "task": task},
        )
    emit_telemetry(
        "agent_completed",
        task_id=task_id,
        returncode=result.returncode,
        duration_ms=(time.monotonic() - started) * 1000.0,
    )

    if not is_review:
        try:
            # Planning-phase runs produce evidence_type=plan_decomposed, not a
            # repo change.  Skip the git finalizer so a clean worktree is not
            # treated as a failure.  The agent is responsible for posting the
            # children and writing the plan manifest.
            if _is_planning and is_plan_decomposed_evidence(task_workspace):
                emit_telemetry("planning_phase_completed", task_id=task_id, level="info")
                # plan-learn-01: record this plan outcome so future planning
                # runs on similar tasks can recall the decomposition shape.
                try:
                    record_plan_outcome(
                        task,
                        task_workspace,
                        wall_clock_ms=(time.monotonic() - started) * 1000.0,
                    )
                except Exception:  # noqa: BLE001
                    pass
            else:
                run_deterministic_git_finalizer(task_workspace, task)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("git finalizer failed: %s\n" % exc)
    elif not blind_protocol_failed:
        try:
            run_deterministic_review_verdict(task_workspace, task, review_context)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("review verdict finalizer failed: %s\n" % exc)

    # Task-sizing: if the agent wrote plan_steps in its evidence, auto-post them
    # as child tasks so the parent blocks on the children.  Best-effort.
    if not is_review:
        try:
            decomposed = maybe_auto_decompose(task_workspace, task)
            if decomposed:
                emit_telemetry("plan_decomposed", task_id=task_id, level="info")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("auto-decompose failed: %s\n" % exc)

    write_fallback_evidence_manifest(task_workspace, task, result, review_context)

    # loop-01 resilience: if the run was bounded/failed (e.g. a wedged TokenHub
    # trailing turn) but the agent or a deterministic finalizer already wrote a
    # complete, typed manifest, don't discard that verified work — finalize as
    # success. The downstream verification gate still validates the content.
    rc = result.returncode
    if rc != 0 and _manifest_is_complete(task_workspace):
        emit_telemetry("evidence_salvaged", task_id=task_id, level="warning", original_returncode=rc)
        rc = 0

    # Memory feed (out): distill this run's outcome into a deployment lesson the
    # nap consolidator will promote into the vector tier — so the fleet's recall
    # gets richer with every task. Reviews don't feed deployment lessons.
    if not is_review:
        outcome = classify_outcome(task_workspace, task, rc)
        emit_telemetry(
            "finalized",
            task_id=task_id,
            level="info" if outcome["outcome"] == "success" else "warning",
            evidence_type=outcome["evidence_type"],
            outcome=outcome["outcome"],
            signals=outcome["signals"],
        )
        record_deployment_learning(task, outcome)
        record_curated_lessons(task, outcome)

    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
