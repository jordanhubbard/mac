"""File follow-up tasks for low-confidence dream-cycle repair findings."""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Iterable, Mapping

from mac.config_coercion import bounded_env_int
from mac.dream_cycle_classifier import classify_candidate
from mac.models import JsonDict


DREAM_REPAIR_TASKS_SCHEMA = "mac.dream_repair_tasks.v1"
DREAM_REPAIR_TASK_SCHEMA = "mac.dream_repair_task.v1"
DREAM_REPAIR_ORIGIN_TYPE = "dream_low_confidence_repair"

# Per-invocation cap on how many DISTINCT new tasks a single scan may mint.
# Fingerprint dedup only stops re-filing the SAME finding; a single tick with
# many distinct low-confidence findings could otherwise create an unbounded
# burst of tasks. Conservative default; overridable via env within bounds.
MAX_TASKS_PER_CYCLE_ENV = "MAC_DREAM_REPAIR_MAX_TASKS_PER_CYCLE"
DEFAULT_MAX_TASKS_PER_CYCLE = 10
MIN_MAX_TASKS_PER_CYCLE = 1
MAX_MAX_TASKS_PER_CYCLE = 1000

_MAX_DESCRIPTION_EVIDENCE = 5
_MAX_METADATA_EVIDENCE = 8

_URL_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/@\s]+)@")
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|token|secret|password|authorization|"
    r"access[_-]?token|refresh[_-]?token)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;}]+"
)
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,})\b"
)
_LONG_ATOM_RE = re.compile(r"\b[A-Za-z0-9_./+=-]{96,}\b")
_HOME_PATH_RE = re.compile(r"(?i)(/Users|/home)/[A-Za-z0-9._-]+")
_AGENT_ID_RE = re.compile(r"\bagent[_-][A-Za-z0-9_.-]+\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SPACE_RE = re.compile(r"\s+")
_LABEL_RE = re.compile(r"[^A-Za-z0-9_.:/+-]+")


def file_low_confidence_repair_tasks(
    control_plane: Any,
    candidates: Iterable[Mapping[str, Any]],
    *,
    actor: str = "dream-repair",
    project: str | None = None,
    max_tasks_per_cycle: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> JsonDict:
    """Create MAC follow-up tasks for low-confidence dream findings.

    The function is intentionally ControlPlane-shaped rather than tied to the
    concrete class: tests and remote adapters can supply any object with
    ``list_tasks`` and ``create_task`` methods.
    """

    candidate_list = [dict(candidate) for candidate in candidates if isinstance(candidate, Mapping)]
    report: JsonDict = {
        "schema": DREAM_REPAIR_TASKS_SCHEMA,
        "status": "ok",
        "candidates_seen": len(candidate_list),
        "created_count": 0,
        "deduped_count": 0,
        "skipped_count": 0,
        "capped_count": 0,
        "budget": 0,
        "tasks": [],
        "errors": [],
    }
    budget = _resolve_spawn_budget(max_tasks_per_cycle, environ)
    report["budget"] = budget
    created_this_cycle = 0
    try:
        known = _existing_repair_fingerprints(control_plane)
    except Exception as exc:  # noqa: BLE001 - do not risk duplicate task storms.
        report["status"] = "error"
        report["errors"].append(
            {"phase": "list_existing_tasks", "error": _sanitize_text(str(exc), limit=500)}
        )
        return report

    for candidate in candidate_list:
        classification = classify_candidate(candidate)
        affected = _affected_labels(candidate, classification)
        fingerprint = repair_fingerprint(candidate, classification, affected)
        item: JsonDict = {
            "fingerprint": fingerprint,
            "candidate_id": classification.get("candidate_id"),
            "overall_confidence": classification.get("overall_confidence"),
            "affected": affected,
        }
        if classification.get("overall_confidence") != "low":
            item["status"] = "skipped"
            item["reason"] = "confidence_not_low"
            report["skipped_count"] += 1
            report["tasks"].append(item)
            continue
        if not any(affected.values()):
            item["status"] = "skipped"
            item["reason"] = "no_affected_area"
            report["skipped_count"] += 1
            report["tasks"].append(item)
            continue
        if fingerprint in known:
            item["status"] = "deduped"
            item["task_id"] = known[fingerprint]
            report["deduped_count"] += 1
            report["tasks"].append(item)
            continue

        if created_this_cycle >= budget:
            item["status"] = "skipped"
            item["reason"] = "per_cycle_budget_exhausted"
            report["capped_count"] += 1
            report["skipped_count"] += 1
            report["tasks"].append(item)
            continue

        metadata = _task_metadata(candidate, classification, affected, fingerprint)
        task_project = _coerce_project(candidate.get("project")) or project
        try:
            task = control_plane.create_task(
                _task_title(candidate, affected),
                description=_task_description(candidate, classification, affected, fingerprint),
                project=task_project,
                metadata=metadata,
                actor=actor,
            )
        except Exception as exc:  # noqa: BLE001 - isolate one candidate.
            item["status"] = "error"
            item["error"] = _sanitize_text(str(exc), limit=500)
            report["status"] = "error"
            report["errors"].append(
                {
                    "phase": "create_task",
                    "fingerprint": fingerprint,
                    "error": item["error"],
                }
            )
            report["tasks"].append(item)
            continue
        task_id = getattr(task, "id", None)
        known[fingerprint] = task_id
        item["status"] = "created"
        item["task_id"] = task_id
        report["created_count"] += 1
        created_this_cycle += 1
        report["tasks"].append(item)
    return report


def repair_fingerprint(
    candidate: Mapping[str, Any],
    classification: Mapping[str, Any] | None = None,
    affected: Mapping[str, list[str]] | None = None,
) -> str:
    """Stable dedupe key for repeated cycle reports of the same finding."""

    classification = dict(classification or classify_candidate(dict(candidate)))
    affected = dict(affected or _affected_labels(candidate, classification))
    material = {
        "kind": str(candidate.get("kind") or classification.get("kind") or ""),
        "scope": str(candidate.get("scope") or classification.get("scope") or ""),
        "project": _coerce_project(candidate.get("project")),
        "signature": _candidate_signature(candidate),
        "summary": _normalized_fingerprint_text(str(candidate.get("summary") or "")),
        "affected": affected,
    }
    payload = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return "dreamrepair:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _existing_repair_fingerprints(control_plane: Any) -> dict[str, Any]:
    known: dict[str, Any] = {}
    for task in list(control_plane.list_tasks()):
        metadata = getattr(task, "metadata", None) or {}
        if not isinstance(metadata, Mapping):
            continue
        origin = metadata.get("origin") if isinstance(metadata.get("origin"), Mapping) else {}
        repair = (
            metadata.get("dream_repair")
            if isinstance(metadata.get("dream_repair"), Mapping)
            else {}
        )
        if origin.get("type") != DREAM_REPAIR_ORIGIN_TYPE:
            continue
        fingerprint = str(origin.get("fingerprint") or repair.get("fingerprint") or "")
        if fingerprint:
            known[fingerprint] = getattr(task, "id", None)
    return known


def _affected_labels(
    candidate: Mapping[str, Any],
    classification: Mapping[str, Any],
) -> dict[str, list[str]]:
    affected: dict[str, list[str]] = {
        "skills": [],
        "tools": [],
        "providers": [],
        "repo_areas": [],
    }

    for area in classification.get("areas") or []:
        if not isinstance(area, Mapping):
            continue
        area_type = str(area.get("area_type") or "")
        name = _clean_label(area.get("area_name"))
        if not name:
            continue
        if area_type == "skill":
            _append_unique(affected["skills"], name)
        elif area_type == "tool":
            _append_unique(affected["tools"], name)
        elif area_type == "provider":
            _append_unique(affected["providers"], name)
        elif area_type == "repo_area":
            _append_unique(affected["repo_areas"], name)

    dimensions = candidate.get("dimensions") if isinstance(candidate.get("dimensions"), Mapping) else {}
    for key, bucket in (
        ("skills", "skills"),
        ("skill_names", "skills"),
        ("tools", "tools"),
        ("tool_names", "tools"),
        ("providers", "providers"),
        ("repo_areas", "repo_areas"),
    ):
        for label in _labels_from_dimension(dimensions.get(key)):
            _append_unique(affected[bucket], label)

    signature = str(candidate.get("signature") or "")
    for prefix, bucket in (("skill:", "skills"), ("tool:", "tools")):
        if signature.lower().startswith(prefix):
            _append_unique(affected[bucket], _clean_label(signature[len(prefix):]))

    return affected


def _labels_from_dimension(value: Any) -> list[str]:
    labels: list[str] = []
    raw_items = value if isinstance(value, list) else [value]
    for item in raw_items:
        if isinstance(item, Mapping):
            label = _clean_label(item.get("name") or item.get("label") or item.get("id"))
        else:
            label = _clean_label(item)
        if label:
            _append_unique(labels, label)
    return labels


def _task_metadata(
    candidate: Mapping[str, Any],
    classification: Mapping[str, Any],
    affected: Mapping[str, list[str]],
    fingerprint: str,
) -> JsonDict:
    evidence = _candidate_evidence(candidate, limit=_MAX_METADATA_EVIDENCE)
    candidate_summary = _sanitize_text(str(candidate.get("summary") or ""), limit=1200)
    return {
        "origin": {
            "type": DREAM_REPAIR_ORIGIN_TYPE,
            "fingerprint": fingerprint,
            "candidate_id": classification.get("candidate_id"),
            "nap_run_id": _safe_identifier(candidate.get("nap_run_id")),
            "dream_memory_id": _safe_identifier(candidate.get("_dream_memory_id")),
        },
        "evidence_type": "investigation",
        "dream_repair": {
            "schema": DREAM_REPAIR_TASK_SCHEMA,
            "fingerprint": fingerprint,
            "candidate": {
                "kind": classification.get("kind"),
                "scope": classification.get("scope"),
                "project": _coerce_project(candidate.get("project")),
                "task_id": _safe_identifier(candidate.get("task_id")),
                "summary": candidate_summary,
                "evidence": evidence,
            },
            "classification": _sanitize_json(classification),
            "affected": affected,
        },
    }


def _task_title(candidate: Mapping[str, Any], affected: Mapping[str, list[str]]) -> str:
    labels = (
        list(affected.get("skills") or [])
        + list(affected.get("tools") or [])
        + list(affected.get("providers") or [])
        + list(affected.get("repo_areas") or [])
    )
    if labels:
        target = ", ".join(labels[:3])
        if len(labels) > 3:
            target += ", ..."
    else:
        target = str(candidate.get("kind") or "dream finding").replace("_", " ")
    title = "Investigate low-confidence dream finding: %s" % target
    return _truncate(_sanitize_text(title, limit=180), 180)


def _task_description(
    candidate: Mapping[str, Any],
    classification: Mapping[str, Any],
    affected: Mapping[str, list[str]],
    fingerprint: str,
) -> str:
    evidence = _candidate_evidence(candidate, limit=_MAX_DESCRIPTION_EVIDENCE)
    lines = [
        "Review this low-confidence dream-cycle repair finding before changing skills or tools.",
        "",
        "Finding:",
        "- Kind: %s" % _sanitize_text(str(classification.get("kind") or "unknown"), limit=120),
        "- Scope: %s" % _sanitize_text(str(classification.get("scope") or "unknown"), limit=120),
        "- Confidence: %s (%s evidence record(s))"
        % (
            _sanitize_text(str(classification.get("overall_confidence") or "unknown"), limit=60),
            int(classification.get("evidence_count") or 0),
        ),
        "- Fingerprint: %s" % fingerprint,
        "",
        "Affected labels:",
        "- Skills: %s" % _label_line(affected.get("skills")),
        "- Tools: %s" % _label_line(affected.get("tools")),
        "- Providers: %s" % _label_line(affected.get("providers")),
        "- Repo areas: %s" % _label_line(affected.get("repo_areas")),
        "",
        "Candidate summary:",
        _sanitize_text(str(candidate.get("summary") or ""), limit=1200) or "(none)",
        "",
        "Candidate evidence:",
    ]
    if evidence:
        for item in evidence:
            label = item.get("memory_id") or item.get("row_id") or item.get("task_id") or "evidence"
            detail = item.get("excerpt") or item.get("record_type") or item.get("source") or ""
            lines.append("- %s: %s" % (_sanitize_text(str(label), limit=120), detail or "(no excerpt)"))
    else:
        lines.append("- No structured evidence was attached to the candidate.")
    lines.extend(
        [
            "",
            "Acceptance criteria:",
            "- Confirm whether the finding is actionable from the attached evidence.",
            "- If actionable, make the smallest appropriate repair or produce a concrete follow-up plan.",
            "- If not actionable, close with the reason and the evidence gap.",
            "",
            "Keep any result fleet-generic: do not include secrets, host names, personal paths, or local operator identities.",
        ]
    )
    return "\n".join(lines)


def _candidate_evidence(candidate: Mapping[str, Any], *, limit: int) -> list[JsonDict]:
    evidence = candidate.get("evidence")
    if not isinstance(evidence, list):
        return []
    out: list[JsonDict] = []
    keys = (
        "memory_id",
        "row_id",
        "source",
        "record_type",
        "task_id",
        "evidence_id",
        "excerpt",
        "created_at",
    )
    for item in evidence[:limit]:
        if not isinstance(item, Mapping):
            continue
        clean: JsonDict = {}
        for key in keys:
            if key in item and item.get(key) is not None:
                clean[key] = _sanitize_text(str(item.get(key)), limit=500)
        if clean:
            out.append(clean)
    return out


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _sanitize_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value, limit=1200)
    return value


def _sanitize_text(value: str, *, limit: int) -> str:
    text = str(value or "")
    text = _URL_USERINFO_RE.sub(r"\1<redacted>@", text)
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    text = _SECRET_ASSIGN_RE.sub(r"\1<redacted>", text)
    text = _KNOWN_TOKEN_RE.sub("<redacted>", text)
    text = _LONG_ATOM_RE.sub("<redacted>", text)
    text = _HOME_PATH_RE.sub(r"\1/<user>", text)
    text = _AGENT_ID_RE.sub("<agent>", text)
    text = _EMAIL_RE.sub("<email>", text)
    text = _SPACE_RE.sub(" ", text.replace("\r", " ").replace("\n", " ")).strip()
    return _truncate(text, limit)


def _clean_label(value: Any) -> str:
    label = _sanitize_text(str(value or ""), limit=120).strip()
    label = _LABEL_RE.sub("_", label).strip("._-:/+")
    return label[:80]


def _candidate_signature(candidate: Mapping[str, Any]) -> str:
    signature = str(candidate.get("signature") or "").strip()
    if signature:
        return _normalized_fingerprint_text(signature)
    retrieval = candidate.get("retrieval") if isinstance(candidate.get("retrieval"), Mapping) else {}
    terms = retrieval.get("query_terms")
    if isinstance(terms, list) and terms:
        return _normalized_fingerprint_text(" ".join(str(term) for term in terms))
    return ""


def _normalized_fingerprint_text(value: str) -> str:
    return _SPACE_RE.sub(" ", _sanitize_text(value.lower(), limit=2000)).strip()


def _label_line(labels: Iterable[str] | None) -> str:
    clean = [_clean_label(label) for label in (labels or [])]
    clean = [label for label in clean if label]
    return ", ".join(clean) if clean else "(none)"


def _append_unique(values: list[str], value: str) -> None:
    value = _clean_label(value)
    if value and value not in values:
        values.append(value)


def _safe_identifier(value: Any) -> str | None:
    text = _sanitize_text(str(value or ""), limit=160)
    return text or None


def _coerce_project(value: Any) -> str | None:
    text = _sanitize_text(str(value or ""), limit=120)
    return text or None


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _resolve_spawn_budget(
    max_tasks_per_cycle: int | None,
    environ: Mapping[str, str] | None,
) -> int:
    """Resolve the per-cycle spawn cap from an explicit arg or bounded env.

    An explicit ``max_tasks_per_cycle`` wins (clamped to the safe range);
    otherwise ``MAC_DREAM_REPAIR_MAX_TASKS_PER_CYCLE`` is read via the shared
    bounded-env helper, falling back to ``DEFAULT_MAX_TASKS_PER_CYCLE``.
    """

    if max_tasks_per_cycle is not None:
        return max(
            MIN_MAX_TASKS_PER_CYCLE,
            min(MAX_MAX_TASKS_PER_CYCLE, int(max_tasks_per_cycle)),
        )
    env = os.environ if environ is None else environ
    errors: list[str] = []
    return bounded_env_int(
        env,
        MAX_TASKS_PER_CYCLE_ENV,
        DEFAULT_MAX_TASKS_PER_CYCLE,
        MIN_MAX_TASKS_PER_CYCLE,
        MAX_MAX_TASKS_PER_CYCLE,
        errors=errors,
    )


__all__ = [
    "DREAM_REPAIR_ORIGIN_TYPE",
    "DREAM_REPAIR_TASKS_SCHEMA",
    "DREAM_REPAIR_TASK_SCHEMA",
    "file_low_confidence_repair_tasks",
    "repair_fingerprint",
    "DEFAULT_MAX_TASKS_PER_CYCLE",
    "MAX_TASKS_PER_CYCLE_ENV",
]
