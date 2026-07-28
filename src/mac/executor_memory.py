"""Memory feed, telemetry, and plan-outcome learning extracted from task_executor (loop-01).

Provides:
* Executor telemetry (build_telemetry_record, emit_telemetry)
* Memory feed: recall prior deployment lessons, record new lessons, curate via LLM
* Plan-outcome learning: record and recall plan decomposition patterns
* classify_outcome: derive run outcome from evidence manifest
"""
from __future__ import annotations

import json
import os
import re as _re
from pathlib import Path
from typing import Any, Dict, List, Optional

from mac.env_config import env_bool, resolve_env_chain
from mac.fleet_env import resolve as fleet_resolve
from mac.fleet_learning import (
    REPOSITORY_ACCESS_RECORD_TYPE,
    parse_repository_access_learning,
    repository_host,
    task_repository_remote,
)
from mac.executor_hub_io import _hub_get, _hub_post, local_agent_id, utcnow

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
        "source": "mac-task-executor",
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


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


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
    if isinstance(data, dict) and data.get("schema") == "mac.dream_memory.v2":
        # A promoted dream memory. The kind carries the polarity, so a
        # practice reads as something to repeat rather than another warning.
        kind = str(data.get("kind") or "fact")
        statement = str(data.get("statement") or "").strip()
        when = str(data.get("applies_when") or "").strip()
        if not statement:
            return ""
        line = "[%s] %s" % (kind, statement)
        if when:
            line += " (when: %s)" % when
        return line[:500]
    if not isinstance(data, dict) or data.get("schema") != "mac.deployment_learning.v1":
        return raw.strip()[:300]
    outcome = data.get("outcome") or "?"
    title = str(data.get("task_title") or data.get("task_id") or "task")
    etype = data.get("evidence_type") or "?"
    err = str(data.get("error_signature") or "").strip()
    line = "[%s] %s (%s)" % (outcome, title, etype)
    if outcome == "failure" and err:
        if err == "untracked_new_files_at_finalize":
            signals = data.get("signals") if isinstance(data.get("signals"), dict) else {}
            untracked = _string_list(signals.get("untracked_files"))
            staged_new = _string_list(signals.get("staged_new_files"))
            details: List[str] = []
            if untracked:
                details.append("untracked files: %s" % ", ".join(untracked[:8]))
            if staged_new:
                details.append("staged new files: %s" % ", ".join(staged_new[:8]))
            if details:
                err = "%s (%s)" % (err, "; ".join(details))
        line += " — failed: %s" % err
    return line[:300]


def _structured_lesson_content(
    raw: str, *, record_type: str, project: str
) -> str:
    """Render only executor-produced, schema-valid operational memories.

    Semantic recall can return any project-scoped memory. Worker prompts must
    not promote arbitrary free-form memories into instructions merely because
    they are embedding-near a task title. Repository-access learnings and
    deployment outcomes have bounded schemas and are the only accepted inputs.
    """
    if record_type == REPOSITORY_ACCESS_RECORD_TYPE:
        learning = parse_repository_access_learning(raw)
        if learning is None:
            return ""
        if str(learning.get("project") or "default") != project:
            return ""
        return _format_learning_content(raw)
    if not record_type.startswith(DEPLOYMENT_LEARNING_PREFIX):
        return ""
    try:
        learning = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(learning, dict):
        return ""
    if learning.get("schema") != "mac.deployment_learning.v1":
        return ""
    if str(learning.get("project") or "default") != project:
        return ""
    return _format_learning_content(raw)


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

    # Promoted dream memories come next. These are the curated, deduplicated
    # distillate of past sessions, so they are worth more per line than raw
    # per-task learnings -- and unlike the previous dream cycle's output, which
    # nothing ever read back, they reach the prompt here.
    for kind in ("practice", "pitfall", "preference", "obligation", "fact"):
        if len(lessons) >= limit:
            return lessons[:limit]
        dream_records = _hub_get(
            "/memory?%s"
            % urlencode(
                {"record_type": "dream_memory:%s" % kind, "order": "desc", "limit": 20}
            )
        )
        if not isinstance(dream_records, list):
            continue
        for record in dream_records:
            if not isinstance(record, dict):
                continue
            # Filter on the record type we actually got back rather than
            # trusting the query filter, so a hub that ignores it cannot leak
            # unrelated learning records into this stage.
            if not str(record.get("record_type") or "").startswith("dream_memory:"):
                continue
            subject = str(record.get("subject_id") or "")
            if subject and subject != project:
                continue
            rendered = _format_learning_content(str(record.get("content") or ""))
            if not rendered or rendered in lessons:
                continue
            if not _append_lesson_with_budget(lessons, rendered):
                return lessons[:limit]
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
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            record_type = str(
                payload.get("record_type") or item.get("record_type") or ""
            ).strip()
            raw = str(
                item.get("content")
                or item.get("text")
                or item.get("summary")
                or ""
            ).strip()
            text = _structured_lesson_content(
                raw,
                record_type=record_type,
                project=project,
            )
            if text and not _append_lesson_with_budget(lessons, text):
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
            rendered = _structured_lesson_content(
                content,
                record_type=str(record.get("record_type") or ""),
                project=project,
            )
            if not rendered:
                continue
            if rendered in lessons:
                continue
            if not _append_lesson_with_budget(lessons, rendered):
                break
            if len(lessons) >= limit:
                break
    return lessons[:limit]


def recall_prior_attempt_lessons(task: Dict[str, Any], *, limit: int = 2) -> List[str]:
    """Surface THIS task's own prior-attempt outcomes when it is being retried.

    The generic ``recall_deployment_lessons`` recall is project-scoped and
    ranked by title/term overlap, so a task's *own* previous failed attempt is
    not prioritized — it competes with every other project lesson and may not
    surface at all. But the single most useful piece of hindsight when
    re-running a task is "what happened last time I ran THIS exact task."

    This is deliberately exact-match (``content.task_id == task.id``), not
    semantic: retries are the highest-value moment and exact identity carries
    zero retrieval noise. Only fires on a genuine retry (``attempt_count > 1``);
    first attempts have no prior self to recall and pay nothing. Best-effort —
    an unreachable hub just means the retry runs without self-hindsight.
    """
    try:
        attempt = int(task.get("attempt_count") or 0)
    except (TypeError, ValueError):
        attempt = 0
    if attempt <= 1:
        return []
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        return []
    project = _task_project(task)
    from urllib.parse import urlencode

    records = _hub_get(
        "/memory?%s" % urlencode({"subject_type": "project", "subject_id": project})
    )
    if not isinstance(records, list):
        return []
    own: List[Dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if not str(record.get("record_type") or "").startswith(DEPLOYMENT_LEARNING_PREFIX):
            continue
        content = str(record.get("content") or "")
        try:
            data = json.loads(content)
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("schema") != "mac.deployment_learning.v1":
            continue
        if str(data.get("task_id") or "") != task_id:
            continue
        own.append(record)
    if not own:
        return []
    own.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    lessons: List[str] = []
    for record in own[: max(1, int(limit))]:
        rendered = _format_learning_content(str(record.get("content") or ""))
        if not rendered:
            continue
        line = "Your previous attempt at THIS task recorded: %s" % rendered
        if not _append_lesson_with_budget(lessons, line):
            break
        if len(lessons) >= limit:
            break
    return lessons


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
        "created_by": "mac-task-executor",
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
        "created_by": "mac-task-executor",
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
    if not env_bool("MAC_LESSON_CURATION_ENABLED"):
        return []
    router_url = resolve_env_chain("MAC_ROUTER_URL", "MAC_ROUTER_INTERNAL_URL") or os.environ.get("OPENAI_BASE_URL", "").strip()
    model = resolve_env_chain("MAC_LESSON_CURATION_MODEL", "MAC_TASK_MODEL", "MAC_HERMES_GATEWAY_MODEL")
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
            existing="\n".join("- " + lesson for lesson in existing) or "- (none yet)",
            title=str(task.get("title") or "")[:200],
            outcome=outcome.get("outcome"),
            evidence_type=outcome.get("evidence_type"),
            signals=json.dumps(outcome.get("signals") or {}, sort_keys=True)[:400],
            error_signature=str(outcome.get("error_signature") or "none")[:200],
        )
        # Resolve fleet-aware so a legacy flat MAC_API_TOKEN can't shadow the
        # scoped MAC_API_TOKEN__<FLEET> form; fleet derives from MAC_FLEET
        # (mac-g55y). Preserve the previous trim/empty-default behavior.
        caller = router_model_caller(
            router_url,
            token=(
                fleet_resolve("MAC_API_TOKEN", fleet=os.environ.get("MAC_FLEET"))
                or ""
            ).strip(),
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
