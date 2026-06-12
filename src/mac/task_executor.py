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
``docs/openshell-nemo-relay-integration.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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

    # Don't decompose child tasks further
    metadata = task.get("metadata") if isinstance(task, dict) else {}
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
    return evidence_type if evidence_type in allowed else "operator_result"


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
    pushed = False
    push_target = "refs/heads/%s" % branch if branch != "HEAD" else "refs/heads/auto/%s" % (task.get("id") or "task")
    push = _git(["push", "origin", "HEAD:%s" % push_target], worktree_path)
    if push.returncode == 0:
        pushed = True
    contract = (metadata.get("origin") or {}).get("repository_contract") or {}
    test_cmd = ((contract.get("test") or {}).get("command") or "scripts/run-contract-tests.sh").strip()
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
    _git(["fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"], worktree_path)
    diff = _git(["diff", "--name-only", "origin/main..HEAD"], worktree_path)
    files_changed = [f for f in (diff.stdout or "").splitlines() if f.strip()]
    final_status = _git(["status", "--porcelain"], worktree_path).stdout.strip()
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "summary": "Deterministic finalizer: commit+push+test for %s" % task.get("id"),
        "repo": {
            "head_sha": head_sha,
            "pushed": pushed,
            "remote_ref": "refs/heads/" + branch if branch != "HEAD" else push_target,
            "dirty": bool(final_status),
            "files_changed": files_changed,
        },
        "tests": tests,
        "checks": [
            {
                "name": "git_finalizer",
                "returncode": 0 if pushed and (tests is None or tests.get("returncode") == 0) else 1,
                "status": "pass" if pushed and (tests is None or tests.get("returncode") == 0) else "fail",
            }
        ],
    }
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
    independent_pass = False
    if review_worktree and Path(review_worktree).is_dir():
        ck = _git(["cat-file", "-e", "%s^{commit}" % exec_head], Path(review_worktree))
        if ck.returncode == 0:
            test_cmd = (
                (((task.get("metadata") or {}).get("origin") or {}).get("repository_contract") or {})
                .get("test", {})
                .get("command", "scripts/run-contract-tests.sh")
            )
            tr = subprocess.run(
                ["bash", "-lc", test_cmd], cwd=review_worktree, capture_output=True, text=True, check=False, timeout=600
            )
            independent_pass = tr.returncode == 0
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


def _hermes_argv(prompt: str) -> List[str]:
    """The agent invocation. Sets PYTHONPATH for the vendored Hermes runtime."""
    hermes_py = Path.home() / ".mac" / "venv" / "bin" / "python"
    hermes_vendored = str(Path.home() / ".mac" / "src" / "mac" / "src" / "mac" / "_hermes")
    os.environ["PYTHONPATH"] = hermes_vendored + os.pathsep + os.environ.get("PYTHONPATH", "")
    return [str(hermes_py), "-m", "hermes_cli.main", "chat", "--query", prompt, "--quiet", "--accept-hooks", "--yolo"]


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
# docs/openshell-nemo-relay-integration.md).
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
# ---------------------------------------------------------------------------

# Forward the env the agent needs to reach the hub + model gateway from inside
# the sandbox. (Network reachability is still gated by the OpenShell policy;
# this only makes the values visible to the process.)
_DEFAULT_OPENSHELL_ENV_PASSTHROUGH = (
    "MAC_HUB_URL,MAC_URL,MAC_WORKER_TOKEN,MAC_TOKEN,MAC_API_TOKEN,"
    "MAC_WORKER_AGENT_ID,MAC_WORKER_AGENT_NAME,MAC_AGENT_ID,"
    "HERMES_GATEWAY_BASE_URL,HERMES_GATEWAY_MODEL,HERMES_SESSION_KEY"
)


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _openshell_enabled() -> bool:
    return _truthy(os.environ.get("MAC_OPENSHELL_SANDBOX"))


def _openshell_env_flags() -> List[str]:
    """``--env NAME=VALUE`` flags for each *set* passthrough variable."""
    names = os.environ.get("MAC_OPENSHELL_ENV_PASSTHROUGH") or _DEFAULT_OPENSHELL_ENV_PASSTHROUGH
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
        flags += ["--env", "%s=%s" % (name, val)]
    return flags


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


def _maybe_wrap_openshell(argv: List[str]) -> List[str]:
    """Wrap *argv* to run inside an OpenShell sandbox, if enabled.

    Returns *argv* unchanged when sandboxing is disabled. When enabled, a policy
    is ALWAYS passed via :func:`_resolve_openshell_policy` (explicit ->
    deployed -> bundled fail-closed default), so OpenShell can never silently
    apply its image-default profile; if no policy can be resolved this raises.
    The sandbox is one-shot (torn down when the agent process exits) unless
    MAC_OPENSHELL_KEEP is set. ``--name`` is omitted by default so OpenShell
    assigns a unique name per run; set MAC_OPENSHELL_SANDBOX_NAME only to debug
    a single run. The original *argv* is always preserved verbatim after the
    ``--`` separator.
    """
    if not _openshell_enabled():
        return list(argv)
    bin_ = (os.environ.get("MAC_OPENSHELL_BIN") or "openshell").strip() or "openshell"
    wrapped: List[str] = [bin_, "sandbox", "create", "--no-auto-providers"]
    # Always pass one of OUR policies — never omit --policy, which would let
    # OpenShell silently apply its own image-default profile. Resolves
    # explicit -> deployed -> bundled fail-closed default, or raises.
    wrapped += ["--policy", _resolve_openshell_policy()]
    name = (os.environ.get("MAC_OPENSHELL_SANDBOX_NAME") or "").strip()
    if name:
        wrapped += ["--name", name]
    if _truthy(os.environ.get("MAC_OPENSHELL_KEEP")):
        wrapped += ["--keep"]
    wrapped += _openshell_env_flags()
    extra = (os.environ.get("MAC_OPENSHELL_CREATE_ARGS") or "").strip()
    if extra:
        wrapped += shlex.split(extra)
    wrapped += ["--", *argv]
    return wrapped


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
    result = runner(
        _maybe_wrap_openshell(_hermes_argv(prompt)),
        task_workspace,
        str(audit_task_id) if audit_task_id else None,
        {"execution_kind": "review" if is_review else "task", "timeout": _agent_timeout()},
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
