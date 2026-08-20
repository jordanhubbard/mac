"""Classification of task attempt failures.

Provides the enum, named-tuple result type, and heuristics used to categorize a
failed task attempt (for example work, environment, scope, or superseded) from
its recorded events so the control plane can decide how to retry.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any, Dict, List, NamedTuple


JsonDict = Dict[str, Any]


class AttemptFailureClass(str, Enum):
    WORK = "work"
    ENVIRONMENT = "environment"
    SCOPE = "scope"
    SUPERSEDED = "superseded"


class AttemptFailureClassification(NamedTuple):
    failure_class: str
    salvage: JsonDict


_BRANCH_KEYS = (
    "pushed_branch",
    "published_branch",
    "repository_branch",
    "remote_branch",
    "branch",
)
_LESSON_KEYS = (
    "recorded_lessons",
    "lesson_ids",
    "lessons",
    "memory_ids",
    "learning_record_ids",
)
_CHILD_KEYS = (
    "published_children",
    "published_child_task_ids",
    "child_task_ids",
    "children",
)


def _event_value(event: Any, key: str) -> Any:
    if isinstance(event, Mapping):
        return event.get(key)
    return getattr(event, key, None)


def _event_detail(event: Any) -> JsonDict:
    detail = _event_value(event, "detail")
    return dict(detail) if isinstance(detail, Mapping) else {}


def _event_type(event: Any) -> str:
    return str(_event_value(event, "event_type") or "")


def _compact_text(value: Any) -> str:
    try:
        text = json.dumps(value, sort_keys=True, default=str)
    except TypeError:
        text = str(value)
    return re.sub(r"\s+", " ", text).strip().lower()


def _string_list(value: Any) -> List[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Mapping):
        for key in ("id", "task_id", "memory_id", "lesson_id", "branch"):
            item = str(value.get(key) or "").strip()
            if item:
                return [item]
        return []
    if isinstance(value, Iterable):
        result: List[str] = []
        for item in value:
            result.extend(_string_list(item))
        return result
    text = str(value).strip()
    return [text] if text else []


def _extend_unique(target: List[str], values: Iterable[str]) -> None:
    seen = set(target)
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            target.append(item)
            seen.add(item)


def _salvage_from_detail(event_type: str, detail: Mapping[str, Any]) -> JsonDict:
    salvage: JsonDict = {}
    for key in _BRANCH_KEYS:
        branch = str(detail.get(key) or "").strip()
        if branch and branch.lower() not in {"true", "false"}:
            salvage["pushed_branch"] = branch
            break
    checks = detail.get("checks")
    if isinstance(checks, Mapping) and checks.get("branch_pushed") is True:
        for key in _BRANCH_KEYS:
            branch = str(checks.get(key) or "").strip()
            if branch and branch.lower() not in {"true", "false"}:
                salvage["pushed_branch"] = branch
                break
        if "pushed_branch" not in salvage:
            salvage["branch_pushed"] = True
    if detail.get("branch_pushed") is True and "pushed_branch" not in salvage:
        salvage["branch_pushed"] = True

    lessons: List[str] = []
    for key in _LESSON_KEYS:
        _extend_unique(lessons, _string_list(detail.get(key)))
    if "lesson" in event_type or "learning" in event_type or "memory" in event_type:
        for key in ("memory_id", "record_id", "lesson_id", "id"):
            _extend_unique(lessons, _string_list(detail.get(key)))
    if lessons:
        salvage["recorded_lessons"] = lessons

    children: List[str] = []
    for key in _CHILD_KEYS:
        _extend_unique(children, _string_list(detail.get(key)))
    if event_type == "task.children_added" and children:
        salvage["published_children"] = children
    elif children and ("child" in event_type or "children" in event_type):
        salvage["published_children"] = children
    return salvage


def _merge_salvage(target: JsonDict, source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if value in (None, "", [], {}):
            continue
        if key in {"recorded_lessons", "published_children"}:
            current = _string_list(target.get(key))
            _extend_unique(current, _string_list(value))
            if current:
                target[key] = current
            continue
        target.setdefault(key, value)


# Fields that describe WHY a transition happened. Captured process output is
# deliberately excluded: a pytest log routinely contains "timeout" or "no such
# file or directory", and matching markers against it reclassifies a genuine
# work failure as environment or scope.
_CLASSIFIABLE_DETAIL_KEYS = (
    "failure_class",
    "reason",
    "error",
    "disposition",
    "failure",
)
_OUTPUT_KEYS = frozenset(
    {
        "output_tail",
        "stdout",
        "stderr",
        "stdout_tail",
        "stderr_tail",
        "output",
        "log_tail",
        "logs",
        "output_tail_unavailable_reason",
    }
)


def _diagnosis_reason(value: Any) -> str:
    """The diagnosis' own verdict, without the process output it now carries."""

    if isinstance(value, Mapping):
        return " ".join(
            str(value.get(key) or "")
            for key in ("failure", "problem", "remediation")
        )
    return str(value or "")


def _detail_reason(detail: Mapping[str, Any]) -> str:
    parts = []
    for key in _CLASSIFIABLE_DETAIL_KEYS:
        parts.append(str(detail.get(key) or ""))
    parts.append(_diagnosis_reason(detail.get("diagnosis")))
    problems = detail.get("problems")
    if isinstance(problems, list):
        parts.extend(str(item) for item in problems)
    else:
        parts.append(str(problems or ""))
    return " ".join(parts).lower()


def _classifiable_detail(detail: Mapping[str, Any]) -> JsonDict:
    """Drop captured process output before the detail is flattened for matching."""

    trimmed: JsonDict = {}
    for key, value in detail.items():
        if key in _OUTPUT_KEYS:
            continue
        if key == "diagnosis":
            trimmed[key] = _diagnosis_reason(value)
            continue
        trimmed[key] = value
    return trimmed


def _has_marker(blob: str, marker: str) -> bool:
    if marker in {"saml", "sso", "decompose", "decomposition"}:
        return re.search(r"(?<![a-z0-9_])%s(?![a-z0-9_])" % re.escape(marker), blob) is not None
    return marker in blob


def _class_from_history(events: Iterable[Any]) -> str:
    saw_scope = False
    saw_environment = False
    for event in events:
        detail = _event_detail(event)
        event_type = _event_type(event).lower()
        blob = " ".join(
            [
                event_type,
                _detail_reason(detail),
                _compact_text(_classifiable_detail(detail)),
            ]
        )
        if (
            "superseded" in blob
            or "duplicate" in blob
            or str(detail.get("failure_class") or "").strip().lower() == "superseded"
        ):
            return AttemptFailureClass.SUPERSEDED.value
        shared_control_plane_transport = (
            (
                "mac api" in blob
                or "/evidence" in blob
                or "evidence post failed" in blob
            )
            and any(
                _has_marker(blob, marker)
                for marker in (
                    "timed out",
                    "timeout",
                    "connection reset",
                    "connection refused",
                    "service unavailable",
                    "bad gateway",
                    "gateway timeout",
                    "temporary failure",
                )
            )
        )
        if shared_control_plane_transport:
            saw_environment = True
            continue
        if any(
            _has_marker(blob, marker)
            for marker in (
                "timed out",
                "timeout",
                "rc=124",
                "returncode 124",
                "code: 124",
                "context length",
                "context window",
                "too large",
                "split into child",
                "decompose",
                "decomposition",
                "plan_first",
            )
        ):
            saw_scope = True
        if any(
            _has_marker(blob, marker)
            for marker in (
                "heartbeat_offline",
                "lease_expired",
                "lease expired",
                "agent went offline",
                # "worker_exception" and "executor_failed" used to live here.
                # worker.py stamps executor_failed on EVERY non-zero executor
                # exit -- including a correct "the agent's tests failed" -- so
                # matching them classified real work failures as environment
                # and made the ledger's 61% environment rate an artifact of the
                # classifier reading its own transport label.
                "could not clone",
                "authentication failed",
                "permission denied",
                "saml",
                "sso",
                "network",
                "connection reset",
                "connection refused",
                "no route to host",
                "temporary failure",
                "rate limit",
                "command not found",
                "no such file or directory",
                "sandbox_hub_environment_fault",
                "hub_unreachable",
                "hub_credentials_missing",
                "hub_url_missing",
                "hub_url_and_credentials_missing",
                "hub_probe_exception",
                # A sandbox egress policy refused the destination
                # (``sandbox_policy_denied`` from the coding-agent preflight).
                # The repair is the sandbox policy/route, so this is an
                # environment failure and must not be retried as new work.
                "policy_denied",
                "not permitted by policy",
                "denied by policy",
                "blocked by policy",
                # The worker had no execution boundary, so the executor
                # refused to launch. worker.py names this precisely rather than
                # leaving it to be scraped out of stdout -- scraping is what
                # made this classifier read its own transport label and call
                # real test failures environment faults.
                #
                # It is the WORKER's environment, not the task's work: the same
                # task on a worker that has OpenShell runs normally, so it must
                # stay retryable and move rather than burn the task. Observed
                # live 2026-08-02: every task dispatched to a
                # container-provisioned worker died at attempt 1 of 3 with two
                # attempts unused.
                "executor_execution_boundary_unavailable",
            )
        ):
            saw_environment = True
    if saw_scope:
        return AttemptFailureClass.SCOPE.value
    if saw_environment:
        return AttemptFailureClass.ENVIRONMENT.value
    return AttemptFailureClass.WORK.value


def classify_attempt_failure(attempt_history: Iterable[Any]) -> AttemptFailureClassification:
    """Classify a task's exhausted attempt history and retain durable salvage pointers."""

    events = list(attempt_history or [])
    salvage: JsonDict = {}
    for event in events:
        _merge_salvage(salvage, _salvage_from_detail(_event_type(event), _event_detail(event)))
    return AttemptFailureClassification(
        failure_class=_class_from_history(events),
        salvage=salvage,
    )
