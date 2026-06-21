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

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from mac import relay_observability
from mac.openshell_runtime import (
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
        result = subprocess.run(
            argv, cwd=str(cwd), text=True, capture_output=True, check=False, timeout=timeout
        )
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


def _task_project(task: Dict[str, Any]) -> str:
    return str(task.get("project") or "default")


def _format_learning_content(raw: str) -> str:
    """Render a stored ``mac.deployment_learning.v1`` blob as a one-line lesson."""
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
    results = _hub_get(
        "/v1/memory/recall?%s"
        % urlencode({"q": title, "project": project, "tier": "medium", "limit": max(1, int(limit))})
    )
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            text = str(item.get("content") or item.get("text") or item.get("summary") or "").strip()
            if text:
                lessons.append(text if len(text) <= 500 else text[:497] + "...")
    if lessons:
        return lessons[:limit]

    records = _hub_get("/memory?%s" % urlencode({"subject_type": "project", "subject_id": project}))
    if isinstance(records, list):
        learnings = [
            r
            for r in records
            if isinstance(r, dict) and str(r.get("record_type") or "").startswith(DEPLOYMENT_LEARNING_PREFIX)
        ]
        learnings.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        for record in learnings[:limit]:
            lessons.append(_format_learning_content(str(record.get("content") or "")))
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
    tests = manifest.get("tests") if isinstance(manifest.get("tests"), dict) else None
    checks = manifest.get("checks") if isinstance(manifest.get("checks"), list) else []
    checks_pass = bool(checks) and all(
        (c.get("returncode", 0) == 0 or str(c.get("status", "")).lower() == "pass")
        for c in checks
        if isinstance(c, dict)
    )
    tests_state = None
    if isinstance(tests, dict):
        tests_state = "pass" if (tests.get("returncode") == 0 or tests.get("status") == "pass") else "fail"
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
    summary = {
        "schema": contract.get("schema"),
        "project": contract.get("project"),
        "contract_path": contract.get("contract_path"),
        "platforms": contract.get("platforms"),
        "toolchain": contract.get("toolchain"),
        "bootstrap": contract.get("bootstrap"),
        "test": contract.get("test"),
        "evidence": contract.get("evidence"),
    }
    return "\n".join(
        [
            json.dumps(summary, indent=2, sort_keys=True),
            "For normal repository tasks, MAC prepares a task-owned git worktree before the executor starts.",
            "Use $MAC_TASK_REPO_WORKTREE, or metadata.runtime.repository_worktree in task.json, as the only writable checkout.",
            "Treat origin.repository_path / $MAC_TASK_REPO_SOURCE as read-only registered source state; do not edit it for feature or bug work.",
            "The registered checkout must remain clean. Commit, test, and publish from the task worktree branch, then report the pushed ref in evidence.",
            "Only explicit source-remediation tasks may repair origin.repository_path directly.",
            "Before build or test work, run bootstrap.command from the repository root when the declared tools or bootstrap.creates outputs are missing.",
            "Use test.command as the canonical verification command unless the task explicitly narrows the check.",
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
        or "600"
    )
    try:
        value = float(raw)
        return value if value > 0 else 600.0
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
            "stdout": (completed.stdout or "")[:4000],
            "stderr": (completed.stderr or "")[:4000],
            "duration_ms": int((time.time() - started) * 1000),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "creates": creates,
            "missing_before": missing,
            "returncode": 124,
            "status": "fail",
            "stdout": (exc.stdout or "")[:4000] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[:4000] if isinstance(exc.stderr, str) else "",
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


def build_task_prompt(task: Dict[str, Any], task_file: Path, lessons: Optional[List[str]] = None) -> str:
    parts = [
        "You are running as a MAC fleet worker. Complete the assigned task from first principles.",
        "You are AUTONOMOUS: never ask the operator for confirmation or permission, and never end your turn with a question. If the task is underspecified, make the most reasonable assumption, proceed, and record the assumption in your evidence. Ending with 'should I proceed?' instead of doing the work is a failed run.",
        "Use the task JSON as the source of truth. Preserve secrets and do not print bearer tokens.",
        "When you finish, report the exact outcome, files changed, tests run, and any blockers.",
        "Also write a verifiable evidence manifest to $MAC_TASK_WORKSPACE/mac-evidence.json.",
        "Use schema mac.worker_evidence.v1 with status=complete and evidence_type set to one of repo_change, documentation, investigation, deployment, test, artifact, no_change, or operator_result.",
        "For tasks with a repository runtime contract, default to evidence_type=repo_change. Use operator_result only for tasks that are not tied to a repository contract.",
        "For no-repository planning or operator directive work, use evidence_type=operator_result with summary and result fields describing the completed work.",
        "For repo/code work include repo.head_sha, repo.remote_ref or repo.pr_url, repo.pushed=true, repo.dirty=false, repo.files_changed, and passing tests/checks. Passing tests/checks should use returncode=0, status=pass, result=passed, or boolean/count fields that make success unambiguous. For deployments include targets/services plus passing checks. If you cannot produce this manifest, say why; MAC will not auto-publish unverifiable work.",
        "If the task needs new software, install it only in the task workspace or project worktree, such as a task-local .venv, uv project env, or project-local npm/pnpm install. Do not use sudo, host package managers, global npm/pip/pipx installs, or the shared Hermes/worker virtualenv.",
        "When you add task-local dependencies, include verification.environment_delta in mac-evidence.json with package_manager, commands, added_dependencies, lockfile_path, lockfile_digest, base_runtime_digest when known, and reason. MAC records that as a proposed runtime delta; it does not mutate the fleet runtime until an operator validates and promotes it.",
        "Repository runtime contract:\n%s" % repository_contract_section(task),
    ]
    plan_section = _plan_detection_section(task)
    if plan_section:
        parts.append(plan_section)
    lessons_section = _lessons_section(lessons or [])
    if lessons_section:
        parts.append(lessons_section)
    parts.append("Read the full task from: %s" % str(task_file))
    return "\n\n".join(parts)


def build_review_prompt(task: Dict[str, Any], task_workspace: Path, review_context: Dict[str, Any]) -> str:
    return "\n\n".join(
        [
            "You are running as a MAC fleet reviewer. Review the executor's work independently.",
            "Use the workspace files as the source of truth. Preserve secrets and do not print bearer tokens.",
            "Decide whether the executor evidence actually proves the task was completed and verified.",
            "Approve only when the evidence is coherent, pushed/published when required, and the checks are passing. Reject unverifiable, local-only, failing, or mismatched work.",
            "If MAC_TASK_REPO_WORKTREE is set, use that local review checkout for independent build/test work; it is prepared from the executor evidence remote/ref/head and is safe for review commands.",
            "For repository changes, build the review checkout and run the repository contract test command or the task's declared tests before approving. Look for failures introduced by the change, not just manifest shape.",
            "When you finish, report concise findings and write a review verdict manifest to $MAC_TASK_WORKSPACE/mac-evidence.json.",
            "Use schema mac.worker_evidence.v1 with status=complete, evidence_type=review_verdict, verdict=approved or rejected, reviewed_evidence_id=%s, and review_id=%s."
            % (review_context.get("executor_evidence_id", ""), review_context.get("review_id", "")),
            'A review verdict must also include repo copied from the executor verification repo object, with the same repo.head_sha, plus at least one independent passing check as checks=[{"name":"...","returncode":0}] or status="pass".',
            "Include worktree_digest as sha256:<64 lowercase hex chars>. If you cannot independently verify the executor result, write verdict=rejected and explain the blocker instead of omitting repo/check fields.",
            "Read the original task from executor-task.json and the executor evidence from executor-evidence.json in your workspace (%s)." % str(task_workspace),
        ]
    )


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
    push_target = "refs/heads/%s" % branch if branch != "HEAD" else "refs/heads/auto/%s" % (task.get("id") or "task")
    bootstrap = _run_repository_bootstrap_if_needed(worktree_path, task)
    test_cmd = (_repository_contract_test_command(task) or "scripts/run-contract-tests.sh").strip()
    tests = None
    if test_cmd:
        tr = subprocess.run(
            ["bash", "-lc", test_cmd], cwd=str(worktree_path), capture_output=True, text=True, check=False, timeout=600
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
    bootstrap_ok = bootstrap is None or bootstrap.get("returncode") == 0
    tests_ok = tests is None or tests.get("returncode") == 0
    _git(["fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"], worktree_path)
    diff = _git(["diff", "--name-only", "origin/main..HEAD"], worktree_path)
    files_changed = [f for f in (diff.stdout or "").splitlines() if f.strip()]
    final_status = _git(["status", "--porcelain"], worktree_path).stdout.strip()
    clean = not bool(final_status)
    pushed = False
    if bootstrap_ok and tests_ok and clean:
        push = _git(["push", "origin", "HEAD:%s" % push_target], worktree_path)
        pushed = push.returncode == 0
        push_evidence = {
            "returncode": int(push.returncode),
            "status": "pass" if push.returncode == 0 else "fail",
            "stderr": (push.stderr or "")[:4000],
        }
    elif not clean:
        push_evidence = {
            "returncode": 1,
            "status": "skipped",
            "reason": "worktree dirty after bootstrap/tests",
        }
    else:
        push_evidence = {
            "returncode": 1,
            "status": "skipped",
            "reason": "bootstrap/tests failed",
        }
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "summary": "Deterministic finalizer: commit+test+push for %s" % task.get("id"),
        "repo": {
            "head_sha": head_sha,
            "pushed": pushed,
            "remote_ref": "refs/heads/" + branch if branch != "HEAD" else push_target,
            "dirty": bool(final_status),
            "files_changed": files_changed,
        },
        "tests": tests,
        "push": push_evidence,
        "checks": [
            {
                "name": "git_finalizer",
                "returncode": 0 if pushed and bootstrap_ok and tests_ok and clean else 1,
                "status": "pass" if pushed and bootstrap_ok and tests_ok and clean else "fail",
            }
        ],
    }
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


def run_deterministic_review_verdict(task_workspace: Path, task: Dict[str, Any], review_context: Dict[str, Any]) -> None:
    """mac-jfns: deterministic, signed review verdict from an independent test run."""
    reviewer_agent_id = str(task.get("owner_agent_id") or os.environ.get("MAC_WORKER_AGENT_ID") or "").strip()
    attestation_key = (os.environ.get("MAC_ATTESTATION_KEY") or "").strip()
    if not reviewer_agent_id or not attestation_key:
        return
    executor_evidence_id = str(review_context.get("executor_evidence_id") or "").strip()
    review_id = str(review_context.get("review_id") or "").strip()
    if not executor_evidence_id or not review_id:
        return
    exec_ev_path = task_workspace / "executor-evidence.json"
    if not exec_ev_path.exists():
        return
    try:
        exec_ev = json.loads(exec_ev_path.read_text(encoding="utf-8"))
    except Exception:
        return
    exec_verification = (exec_ev.get("metadata") or {}).get("verification") or {}
    exec_repo = exec_verification.get("repo") or {}
    exec_head = str(exec_repo.get("head_sha") or "").strip()
    if not exec_head:
        return
    review_worktree = os.environ.get("MAC_TASK_REPO_WORKTREE", "").strip()
    tests = None
    bootstrap = None
    independent_pass = False
    if review_worktree and Path(review_worktree).is_dir():
        review_worktree_path = Path(review_worktree)
        ck = _git(["cat-file", "-e", "%s^{commit}" % exec_head], review_worktree_path)
        if ck.returncode == 0:
            bootstrap = _run_repository_bootstrap_if_needed(review_worktree_path, task)
            test_cmd = (_repository_contract_test_command(task) or "scripts/run-contract-tests.sh").strip()
            tr = subprocess.run(
                ["bash", "-lc", test_cmd], cwd=str(review_worktree_path), capture_output=True, text=True, check=False, timeout=600
            )
            bootstrap_ok = bootstrap is None or bootstrap.get("returncode") == 0
            independent_pass = bootstrap_ok and tr.returncode == 0
            tests = {
                "command": test_cmd,
                "returncode": int(tr.returncode),
                "status": "pass" if tr.returncode == 0 else "fail",
            }
    verdict = "approved" if independent_pass else "rejected"
    digest_input = ("%s|%s|%s" % (exec_head, exec_repo.get("remote_ref") or "", verdict)).encode("utf-8")
    import hashlib as _hashlib

    worktree_digest = "sha256:" + _hashlib.sha256(digest_input).hexdigest()
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": verdict,
        "review_id": review_id,
        "reviewed_evidence_id": executor_evidence_id,
        "worktree_digest": worktree_digest,
        "repo": {
            "head_sha": exec_head,
            "remote_ref": exec_repo.get("remote_ref") or "",
            "pushed": True,
            "dirty": False,
        },
        "checks": [
            {
                "name": "review_verdict_finalizer",
                "returncode": 0 if independent_pass else 1,
                "status": "pass" if independent_pass else "fail",
            }
        ],
        "tests": tests,
        "signed_by": reviewer_agent_id,
    }
    if bootstrap is not None:
        manifest["bootstrap"] = bootstrap
    manifest["signature"] = _sign_verdict(attestation_key, manifest)
    (task_workspace / "mac-evidence.json").write_text(
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
    return [_hermes_python(), "-m", "hermes_cli.main", "chat", "--query", prompt, "--quiet", "--accept-hooks", "--yolo"]


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
#   MAC_OPENSHELL_ENV_PASSTHROUGH comma list of env names forwarded via --env
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
    "MAC_WORKER_AGENT_ID,MAC_WORKER_AGENT_NAME,MAC_AGENT_ID,"
    "HERMES_GATEWAY_BASE_URL,HERMES_GATEWAY_MODEL,HERMES_SESSION_KEY,"
    # Model-gateway base_url + api_key live in the agent's ~/.hermes/.env, which
    # is NOT in the sandbox image; the gateway requires auth. Forward them so the
    # sandboxed hermes can authenticate (the *_BASE_URL values have their host
    # loopback rewritten to the sandbox host alias by _openshell_env_flags).
    "MAC_HERMES_GATEWAY_BASE_URL,MAC_HERMES_GATEWAY_API_KEY,MAC_HERMES_GATEWAY_PROVIDER,"
    "OPENAI_BASE_URL,OPENAI_API_KEY,"
    # Coding-agent CLI credentials (see mac.coding_agent). A sandboxed coding
    # agent authenticates safely via these env keys. File-based Codex auth is not
    # forwarded by default because OpenShell uploads are copies: a throwaway
    # sandbox can consume and rotate the refresh token without persisting the
    # replacement back to the host.
    "ANTHROPIC_API_KEY,CURSOR_API_KEY"
)


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


def _openshell_env_flags() -> List[str]:
    """``--env NAME=VALUE`` flags for each *set* passthrough variable.

    URL-valued vars have a host loopback rewritten to the sandbox host alias
    (see :func:`_rewrite_host_local_url`) so a forwarded ``http://127.0.0.1:PORT``
    hub/gateway URL is reachable from inside the sandbox."""
    names = os.environ.get("MAC_OPENSHELL_ENV_PASSTHROUGH") or _DEFAULT_OPENSHELL_ENV_PASSTHROUGH
    alias = _openshell_host_alias()
    flags: List[str] = []
    seen = set()
    for raw in names.split(","):
        name = raw.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        val = os.environ.get(name)
        if val is None:
            continue
        val = _rewrite_host_local_url(val, alias)
        flags += ["--env", "%s=%s" % (name, val)]
    return flags


def _kernel_has_landlock() -> bool:
    """True if the running kernel exposes Landlock (the LSM is listed).

    The operator policy uses ``landlock: best_effort`` because OpenShell's egress
    proxy is incompatible with ``hard_requirement`` on current kernels (it adds a
    directory ReadDir right on its own non-directory proxy path, which Landlock
    ABI >= 3 rejects). best_effort still fully enforces on a Landlock-capable
    kernel, but would silently run UNCONFINED on a kernel without Landlock — so
    the executor performs this precheck to recover the fail-closed guarantee.
    """
    try:
        lsm = Path("/sys/kernel/security/lsm").read_text(encoding="utf-8")
    except OSError:
        return False
    return "landlock" in (lsm or "").split(",")


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


def _sandbox_repository_env_flags(workspace: Path, sandbox_workspace: str) -> List[str]:
    flags: List[str] = []
    mapped_worktree = _sandbox_path_for_workspace_child(
        workspace,
        sandbox_workspace,
        os.environ.get("MAC_TASK_REPO_WORKTREE", ""),
    )
    if mapped_worktree:
        flags += ["--env", "MAC_TASK_REPO_WORKTREE=%s" % mapped_worktree]
    for name in ("MAC_TASK_REPO_BRANCH", "MAC_TASK_REPO_BASE_SHA", "MAC_TASK_REPO_REMOTE"):
        value = os.environ.get(name)
        if value:
            flags += ["--env", "%s=%s" % (name, value)]
    return flags


def _ensure_landlock_or_fail() -> None:
    """Fail closed if the kernel can't enforce Landlock: the operator policy is
    best_effort (forced by OpenShell's proxy/hard_requirement incompatibility),
    which would otherwise run UNCONFINED on a Landlock-less kernel. Override only
    for a deliberate, audited exception."""
    if not _kernel_has_landlock() and not _truthy(os.environ.get("MAC_OPENSHELL_ALLOW_NO_LANDLOCK")):
        raise RuntimeError(
            "OpenShell sandboxing is enabled but the kernel does not expose "
            "Landlock (/sys/kernel/security/lsm has no 'landlock'); the policy's "
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
  export PATH="$MAC_TOOLCHAIN_BIN:$MAC_TOOLCHAIN_ROOT/node_modules/.bin:${JAVA_HOME:+$JAVA_HOME/bin:}$PATH"
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
    export PATH="$JAVA_HOME/bin:$PATH"
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
    export PATH="$MAC_TOOLCHAIN_BIN:$PATH"
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
        if command -v corepack >/dev/null 2>&1; then
          corepack enable --install-directory "$MAC_TOOLCHAIN_BIN" >> "$mac_log" 2>&1 || true
          corepack prepare pnpm@latest --activate >> "$mac_log" 2>&1 || true
        fi
        command -v pnpm >/dev/null 2>&1 && return 0
        command -v npm >/dev/null 2>&1 || return 1
        npm install --prefix "$MAC_TOOLCHAIN_ROOT" pnpm >> "$mac_log" 2>&1
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
    ( cd "$worktree" && bash -lc "$MAC_REPO_BOOTSTRAP_COMMAND" ) >> "$mac_log" 2>&1
    bootstrap_returncode=$?
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


def _sandbox_repository_verification_shell() -> str:
    return "\n".join(
        [
            _sandbox_toolchain_setup_shell(),
            'cd "$MAC_TASK_WORKSPACE"',
            "mac_sandbox_toolchain_setup || true",
            r'''$MAC_SANDBOX_PYTHON - <<'PY'
import json, os, subprocess, time
workspace = os.environ.get("MAC_TASK_WORKSPACE") or os.getcwd()
worktree = os.environ.get("MAC_TASK_REPO_WORKTREE") or workspace
command = os.environ.get("MAC_REPO_TEST_COMMAND", "").strip()
bootstrap_command = os.environ.get("MAC_REPO_BOOTSTRAP_COMMAND", "").strip()
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
                or "600"
            )
        except ValueError:
            timeout = 600.0
        try:
            proc = subprocess.run(
                ["bash", "-lc", bootstrap_command],
                cwd=worktree,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            bootstrap = {
                "command": bootstrap_command,
                "creates": bootstrap_creates,
                "missing_before": missing_before,
                "returncode": int(proc.returncode),
                "status": "pass" if proc.returncode == 0 else "fail",
                "stdout": (proc.stdout or "")[:4000],
                "stderr": (proc.stderr or "")[:4000],
                "duration_ms": int((time.time() - started) * 1000),
            }
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            bootstrap = {
                "command": bootstrap_command,
                "creates": bootstrap_creates,
                "missing_before": missing_before,
                "returncode": 124,
                "status": "fail",
                "stdout": stdout[:4000],
                "stderr": stderr[:4000],
                "duration_ms": int((time.time() - started) * 1000),
                "error": "bootstrap command timed out",
            }
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
    proc = subprocess.run(
        ["bash", "-lc", command],
        cwd=worktree,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=float(os.environ.get("MAC_WORKER_REPOSITORY_TEST_TIMEOUT", "600") or "600"),
        check=False,
    )
    payload = {
        "schema": "mac.sandbox_verification.v1",
        "status": "pass" if proc.returncode == 0 else "fail",
        "command": command,
        "returncode": int(proc.returncode),
        "stdout": (proc.stdout or "")[:4000],
        "stderr": (proc.stderr or "")[:4000],
        "duration_ms": int((time.time() - started) * 1000),
        "worktree": worktree,
        "environment_delta": delta,
    }
    if bootstrap is not None:
        payload["bootstrap"] = bootstrap
with open(result_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
raise SystemExit(0 if payload.get("returncode") == 0 else int(payload.get("returncode") or 1))
PY''',
        ]
    )


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
    argv is shell-quoted (shlex.join) into the cd-wrapper — the prompt is task
    text and must never be able to break out of the command.
    """
    sub = "%s/%s" % (_SANDBOX_WORKDIR, basename)
    argv: List[str] = [_openshell_bin(), "sandbox", "create", "--no-auto-providers"]
    argv += ["--policy", _resolve_openshell_policy(), "--name", name]
    argv += _openshell_env_flags()
    argv += [
        "--env", "MAC_TASK_WORKSPACE=%s" % sub,
        "--env", "MAC_TASK_FILE=%s/task.json" % sub,
    ]
    argv += _sandbox_repository_env_flags(workspace, sub)
    extra = (os.environ.get("MAC_OPENSHELL_CREATE_ARGS") or "").strip()
    if extra:
        argv += shlex.split(extra)
    argv += ["--upload", "%s:%s" % (str(workspace), _SANDBOX_WORKDIR)]
    inner = "\n".join(
        [
            "cd %s" % shlex.quote(sub),
            _sandbox_toolchain_setup_shell(),
            "mac_sandbox_toolchain_setup || true",
            "exec %s" % shlex.join(agent_argv),
        ]
    )
    argv += ["--", "bash", "-lc", inner]
    return argv


def _sandbox_step(args: List[str], *, timeout: float) -> "tuple[bool, str]":
    """Run an openshell lifecycle step (download/delete) out-of-band of the
    audited agent run. Best-effort: returns (ok, message), never raises."""
    try:
        proc = subprocess.run(
            [_openshell_bin(), "sandbox", *args],
            capture_output=True, text=True, timeout=timeout,
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


def _sandbox_run_repository_verification(name: str, basename: str, workspace: Path, task: Any) -> None:
    if not isinstance(task, dict) or not task_is_repo_coupled(task):
        return
    if not _repository_contract_test_command(task):
        return
    sub = "%s/%s" % (_SANDBOX_WORKDIR, basename)
    script_path = workspace / ".mac-sandbox-repository-verify.sh"
    script_path.write_text(_sandbox_repository_verification_shell() + "\n", encoding="utf-8")
    script_path.chmod(0o700)
    sandbox_script = "%s/%s" % (sub, script_path.name)
    ok, msg = _sandbox_step(
        ["upload", name, str(script_path), sandbox_script],
        timeout=120.0,
    )
    if not ok:
        sys.stderr.write("[executor] WARNING: sandbox repository verification upload failed: %s\n" % msg)
        return
    try:
        timeout = float(os.environ.get("MAC_WORKER_REPOSITORY_TEST_TIMEOUT", "600"))
    except ValueError:
        timeout = 600.0
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


def _sandbox_delete(name: str) -> None:
    ok, msg = _sandbox_step(["delete", name], timeout=120.0)
    if not ok:
        sys.stderr.write("[executor] WARNING: sandbox delete failed (possible leak): %s\n" % msg)


def _run_sandboxed(
    runner: Callable[..., Any], agent_argv: List[str], workspace: Path, audit_id: Any, opts: dict
) -> Any:
    """Run the agent through the OpenShell sandbox lifecycle: create (upload the
    workspace + run the agent, keep) -> download results -> delete. The agent
    runs confined; teardown ALWAYS happens (finally), even on failure."""
    _force_child_yolo_env()  # truly silent agent; OpenShell is the guardrail
    _ensure_landlock_or_fail()
    name = _sandbox_name()
    basename = _workspace_basename(workspace)
    create_argv = _build_sandbox_create_argv(name, workspace, basename, agent_argv)
    try:
        result = runner(create_argv, workspace, audit_id, opts)
        _sandbox_run_repository_verification(name, basename, workspace, opts.get("task"))
        _sandbox_download(name, basename, workspace)
        return result
    finally:
        if not _truthy(os.environ.get("MAC_OPENSHELL_KEEP")):
            _sandbox_delete(name)


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


def _record_runner_choice(target: str, rationale: List[str]) -> None:
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
            level="info",
            schema="mac.coding_agent.routing.v1",
            runner=target,
            rationale=list(rationale),
        )
    except Exception:  # noqa: BLE001 - telemetry must never break execution
        pass


def _repo_requires_verified_coding_agent(task: Any) -> bool:
    """Whether repo work must fail closed without a verified coding CLI.

    Default false: Hermes also runs inside the OpenShell sandbox against the
    uploaded worktree, and the deterministic finalizer rejects patch-text-only
    runs because they leave no changed files/tests/evidence. Requiring a coding
    CLI by default made the fleet depend on copied Codex OAuth files, whose
    refresh tokens rotate inside throwaway sandboxes and then break later tasks.

    Operators can restore the old strict behavior with
    ``MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT=1`` after provisioning a durable
    in-sandbox coding-agent auth mechanism.
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


def _build_sandbox_probe_argv(name: str, agent_argv: List[str]) -> List[str]:
    """A minimal ``openshell sandbox create`` argv that runs ``agent_argv`` under
    the SAME policy + env-forwarding + create-args as a real task run, but with no
    workspace upload — used only to verify the coding agent works in-sandbox."""
    argv: List[str] = [_openshell_bin(), "sandbox", "create", "--no-auto-providers"]
    argv += ["--policy", _resolve_openshell_policy(), "--name", name]
    argv += _openshell_env_flags()
    extra = (os.environ.get("MAC_OPENSHELL_CREATE_ARGS") or "").strip()
    if extra:
        argv += shlex.split(extra)
    argv += ["--", "bash", "-lc", "exec %s" % shlex.join(agent_argv)]
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
    probe_argv = _ca.coding_agent_argv(choice, _ca.PREFLIGHT_PROMPT)
    try:
        rc, out = _openshell_probe(_build_sandbox_probe_argv(name, probe_argv), timeout=_coding_agent_preflight_timeout())
    finally:
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
    check does NOT prove the agent works inside the confined sandbox. When the
    agent is unavailable, or confined but not verified, work falls back to
    ``_hermes_argv`` unchanged. Repository work is still run inside OpenShell
    against the uploaded worktree; the deterministic finalizer rejects runs that
    do not produce real changed files/tests/evidence. Operators may opt back
    into the older fail-closed coding-CLI requirement with
    ``MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT=1``.
    """
    from . import coding_agent as _ca

    repo_requires_agent = confined and _repo_requires_verified_coding_agent(task)
    choice = _ca.resolve_coding_agent()
    rationale = list(choice.rationale)
    if not choice.available:
        if repo_requires_agent:
            reason = "no host coding agent is available/authenticated"
            rationale.append(reason)
            _record_runner_choice("coding-agent-required", rationale)
            return _coding_agent_required_failure_argv(reason)
        _record_runner_choice("hermes-gateway", rationale)
        return _hermes_argv(prompt)
    if confined and not _coding_agent_sandbox_ok(choice):
        reason = "%s not verified inside the OpenShell sandbox" % choice.agent
        if repo_requires_agent:
            rationale.append(reason)
            _record_runner_choice("coding-agent-required", rationale)
            return _coding_agent_required_failure_argv(reason)
        rationale.append("%s; using gateway" % reason)
        _record_runner_choice("hermes-gateway", rationale)
        return _hermes_argv(prompt)

    # MCP wiring is unconfined-only: the host config path + host MCP-server
    # interpreter do not reliably resolve inside the sandbox (messaging-MCP parity
    # there is provisioned image-side). Hub parity (mac CLI + runtime context)
    # still applies regardless.
    mcp_path = None if confined else _coding_agent_mcp_config_path(workspace, choice)
    if confined:
        rationale.append("verified inside the OpenShell sandbox")
    _record_runner_choice(choice.agent, rationale)
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
    agent_argv = _agent_argv(prompt, workspace, confined=confined, task=opts.get("task"))
    if wrap:
        return _run_sandboxed(runner, agent_argv, workspace, audit_id, opts)
    return runner(_unsandboxed_agent_argv(agent_argv), workspace, audit_id, opts)


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
    return (
        isinstance(manifest, dict)
        and str(manifest.get("status") or "").lower() == "complete"
        and bool(manifest.get("evidence_type"))
    )


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
    if is_review:
        prompt = build_review_prompt(task, task_workspace, review_context)
        lessons: List[str] = []
    else:
        # Memory feed (in): recall prior deployment lessons so the agent works
        # with the fleet's hindsight. Best-effort — never blocks the run.
        lessons = recall_deployment_lessons(task)
        prompt = build_task_prompt(task, task_file, lessons)
    emit_telemetry(
        "started",
        task_id=task_id,
        kind="review" if is_review else "task",
        recalled_lessons=len(lessons),
        sandboxed=_openshell_enabled(),
    )

    audit_task_id = review_context.get("task_id") if is_review else task_id
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
            run_deterministic_git_finalizer(task_workspace, task)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("git finalizer failed: %s\n" % exc)
    else:
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

    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
