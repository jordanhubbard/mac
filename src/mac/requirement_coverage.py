"""Deterministic coverage of a task's enumerated requirements.

Structural evidence validation answers whether an executor produced a valid
MAC evidence envelope, and the semantic acceptance gate answers a single
value question.  Neither answers "does this change cover every requirement the
task statement enumerated?".  Live 2026-10-07: task_8361a260 asked for three
fixes and its reviewer approved a diff that touched only the first, reporting
``finding_count 0`` because nothing checked the other two.

This module parses the requirements out of a task description -- an inline
numbered list like ``... (1) a, (2) b, (3) c``, a line-oriented numbered list,
or an ``Acceptance``/``Requirements`` section -- and checks an explicit
coverage mapping recorded in the evidence.  Each requirement must be mapped to
the changed work or a check and marked addressed.  Unmapped or unaddressed
items fail closed and are named in the problems, so an acceptance item that
needs a live rollout the worker cannot perform is reported as such instead of
passed.

The mapping lives under ``requirements`` in the executor evidence (or in the
reviewer verdict, which takes precedence)::

    "requirements": [
        {"id": "1", "addressed": true, "evidence": ["src/mac/example.py"]},
        {"id": "2", "addressed": false},
    ]
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Mapping, Optional


REQUIREMENT_COVERAGE_SCHEMA = "mac.requirement_coverage.v1"
STATUS_NOT_REQUIRED = "not_required"
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
MAX_REQUIREMENTS = 50
MAX_REQUIREMENT_TEXT = 500

_INLINE_NUMBERED = re.compile(r"\((\d{1,3})\)\s*")
_LINE_NUMBERED = re.compile(r"^\s*(?:[-*]\s+)?\(?(\d{1,3})\)?[.)]\s+(\S.*)$")
_BULLET = re.compile(r"^\s*[-*]\s+(\S.*)$")
_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*)?(acceptance(?:\s+criteria)?|requirements?)\s*:?\s*$",
    re.IGNORECASE,
)
_ANY_HEADING = re.compile(r"^\s*(?:#{1,6}\s+\S|\S[^:]{0,60}:\s*$)")
_UNADDRESSED = re.compile(r"^(?:unaddressed|not[_ ]?addressed|fail|failed|rejected|missing)$")


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _requirement(identifier: str, text: str) -> Optional[Dict[str, str]]:
    text = _text(text)[:MAX_REQUIREMENT_TEXT]
    if not text:
        return None
    return {"id": identifier, "text": text}


def _inline_requirements(line: str) -> List[Dict[str, str]]:
    matches = list(_INLINE_NUMBERED.finditer(line))
    if len(matches) < 2:
        return []
    out: List[Dict[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        item = _requirement(match.group(1), line[match.end() : end].strip(" .,;"))
        if item is not None:
            out.append(item)
    return out


def _section_requirements(description: str) -> List[Dict[str, str]]:
    lines = description.splitlines()
    out: List[Dict[str, str]] = []
    index = 0
    while index < len(lines):
        if _HEADING.match(lines[index]):
            index += 1
            while index < len(lines):
                line = lines[index]
                if not line.strip():
                    index += 1
                    continue
                if _ANY_HEADING.match(line) and not _BULLET.match(line):
                    break
                numbered = _LINE_NUMBERED.match(line)
                bullet = _BULLET.match(line)
                if numbered:
                    item = _requirement(numbered.group(1), numbered.group(2))
                elif bullet:
                    item = _requirement("A%d" % (len(out) + 1), bullet.group(1))
                else:
                    item = _requirement("A%d" % (len(out) + 1), line)
                if item is not None:
                    out.append(item)
                index += 1
            continue
        index += 1
    return out


def _numbered_list_requirements(description: str) -> List[Dict[str, str]]:
    lines = description.splitlines()
    out: List[Dict[str, str]] = []
    index = 0
    while index < len(lines):
        if not _LINE_NUMBERED.match(lines[index]):
            index += 1
            continue
        run: List[Dict[str, str]] = []
        while index < len(lines):
            match = _LINE_NUMBERED.match(lines[index])
            if match is None:
                break
            item = _requirement(match.group(1), match.group(2))
            if item is not None:
                run.append(item)
            index += 1
        if len(run) >= 2:
            out.extend(run)
    return out


def parse_task_requirements(description: Any) -> List[Dict[str, str]]:
    """Return the enumerated requirements in a task description.

    Deterministic and side-effect free.  An empty list means the statement did
    not enumerate requirements, so coverage is not required.
    """
    text = str(description or "")
    if not text.strip():
        return []
    found: List[Dict[str, str]] = []
    seen = set()
    for line in text.splitlines():
        for item in _inline_requirements(line):
            key = _text(item["text"]).lower()
            if key not in seen:
                seen.add(key)
                found.append(item)
    for item in _numbered_list_requirements(text) + _section_requirements(text):
        key = _text(item["text"]).lower()
        if key not in seen:
            seen.add(key)
            found.append(item)
    return found[:MAX_REQUIREMENTS]


def _coverage_entries(manifest: Any) -> List[Mapping[str, Any]]:
    if not isinstance(manifest, Mapping):
        return []
    raw = manifest.get("requirements")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, Mapping)]
    if isinstance(raw, Mapping):
        entries: List[Mapping[str, Any]] = []
        for key, value in raw.items():
            if isinstance(value, Mapping):
                entries.append({**value, "id": value.get("id") or str(key)})
        return entries
    return []


def _entry_identifier(entry: Mapping[str, Any]) -> str:
    return _text(
        entry.get("id") or entry.get("requirement") or entry.get("label") or entry.get("name")
    ).lower()


def _entry_for(
    requirement: Mapping[str, str], entries: List[Mapping[str, Any]]
) -> Optional[Mapping[str, Any]]:
    identifier = _text(requirement.get("id")).lower()
    if identifier:
        for entry in entries:
            if _entry_identifier(entry) == identifier:
                return entry
    needle = _text(requirement.get("text")).lower()
    for entry in entries:
        candidate = _text(entry.get("requirement") or entry.get("text")).lower()
        if needle and candidate == needle:
            return entry
    return None


def _entry_evidence(entry: Mapping[str, Any]) -> List[str]:
    value = (
        entry.get("evidence")
        or entry.get("files")
        or entry.get("paths")
        or entry.get("checks")
        or entry.get("tests")
        or entry.get("artifacts")
    )
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if _text(item)]


def _entry_addressed(entry: Mapping[str, Any]) -> bool:
    if entry.get("addressed") is False:
        return False
    status = _text(entry.get("status")).lower()
    if status and _UNADDRESSED.match(status):
        return False
    if _entry_evidence(entry):
        return True
    return entry.get("addressed") is True


def evaluate_requirement_coverage(
    description: Any,
    executor_manifest: Any,
    reviewer_manifest: Any = None,
) -> Dict[str, Any]:
    """Return a deterministic requirement-coverage result.

    ``status`` is ``not_required`` when the statement enumerates nothing,
    ``pass`` when every requirement maps to addressed evidence, and ``fail``
    otherwise.  ``unaddressed`` names the items that must be reported back.
    """
    requirements = parse_task_requirements(description)
    base: Dict[str, Any] = {
        "schema": REQUIREMENT_COVERAGE_SCHEMA,
        "required": bool(requirements),
        "status": STATUS_NOT_REQUIRED if not requirements else STATUS_FAIL,
        "requirements": requirements,
        "unaddressed": [],
        "problems": [],
    }
    if not requirements:
        return base
    base["contract_digest"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(requirements, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    entries = _coverage_entries(reviewer_manifest) + _coverage_entries(executor_manifest)
    problems: List[str] = []
    unaddressed: List[Dict[str, str]] = []
    for requirement in requirements:
        entry = _entry_for(requirement, entries)
        if entry is None:
            problems.append(
                "requirement %s is not mapped to the change or evidence: %s"
                % (requirement["id"], requirement["text"])
            )
            unaddressed.append(requirement)
            continue
        if not _entry_addressed(entry):
            problems.append(
                "requirement %s is not addressed: %s" % (requirement["id"], requirement["text"])
            )
            unaddressed.append(requirement)
    base["unaddressed"] = unaddressed
    base["problems"] = problems
    base["status"] = STATUS_PASS if not problems else STATUS_FAIL
    return base


def requirement_coverage_problems(
    description: Any,
    executor_manifest: Any,
    reviewer_manifest: Any = None,
) -> List[str]:
    """Convenience wrapper returning only the fail-closed problem strings."""
    return list(
        evaluate_requirement_coverage(description, executor_manifest, reviewer_manifest).get(
            "problems", []
        )
    )
