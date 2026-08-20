"""Task lineage: what replaced this row, and what did this row replace.

MAC carried no lineage at all. ``--disposition superseded`` required a
replacement *task*, so supersession by a merged pull request -- the common case
for operator work -- was inexpressible and ended up in free text, and nothing
could answer "what replaced task X" without a human reading cancellation
reasons.

horde-claw-fleet ADR-0121 carries ``retry_of`` / replacement lineage precisely
so its ``decide_retry_success_supersession`` has something to fire against.
This module is the MAC equivalent: a durable, queryable, bidirectional lineage
recorded in task metadata under ``lineage``.

Three forward relations, named from the successor's point of view, mirroring
the retry kinds in :mod:`mac.retry_kinds`:

``retry_of``
    Same scope, transient failure. The successor is another go at the same work.
``amends``
    The scope was wrong. The successor carries a *revised* scope, which is why
    a scope failure must never re-dispatch the original.
``replaces``
    The work is now someone else's. The successor -- a task or a merged pull
    request -- supersedes the prior row.

A reference is a task id *or* a pull request, so supersession can name a merged
pull request rather than only a replacement task.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional


JsonDict = Dict[str, Any]

LINEAGE_METADATA_KEY = "lineage"
LINEAGE_SCHEMA = "mac.task_lineage.v1"

#: Forward relations, keyed by the successor row.
FORWARD_RELATIONS = ("retry_of", "amends", "replaces")

#: The reverse name each forward relation is reported under on the prior row.
REVERSE_RELATIONS = {
    "retry_of": "retried_by",
    "amends": "amended_by",
    "replaces": "replaced_by",
}

_TASK_ID_RE = re.compile(r"^task_[0-9a-f]{32}$")
_PR_URL_RE = re.compile(
    r"^https?://[^/\s]+/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)/?$",
    re.IGNORECASE,
)
_PR_SLUG_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+)#(?P<number>\d+)$"
)


class LineageError(ValueError):
    """Raised when a lineage relation or reference is not expressible."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> JsonDict:
    return dict(value) if isinstance(value, Mapping) else {}


def normalize_reference(value: Any) -> JsonDict:
    """Parse a lineage target into ``{"kind": ..., "ref": ...}``.

    Accepted forms -- a task id, a pull request URL, an ``owner/repo#123``
    slug, or an already-normalized mapping. Bare ``#123`` is deliberately
    rejected: a pull-request number without a repository is not resolvable
    across the fleet, and an unresolvable pointer is what free text already
    was.
    """

    if isinstance(value, Mapping):
        kind = _text(value.get("kind")).lower()
        ref = _text(value.get("ref") or value.get("task_id") or value.get("url"))
        if kind == "task" and _TASK_ID_RE.fullmatch(ref):
            return {"kind": "task", "ref": ref}
        if kind == "pull_request" and ref:
            return normalize_reference(ref)
        return normalize_reference(ref)

    text = _text(value)
    if not text:
        raise LineageError("lineage reference must not be empty")
    if _TASK_ID_RE.fullmatch(text):
        return {"kind": "task", "ref": text}
    url_match = _PR_URL_RE.fullmatch(text)
    if url_match:
        return {
            "kind": "pull_request",
            "ref": text.rstrip("/"),
            "repository": "%s/%s"
            % (url_match.group("owner"), url_match.group("repo")),
            "number": int(url_match.group("number")),
        }
    slug_match = _PR_SLUG_RE.fullmatch(text)
    if slug_match:
        return {
            "kind": "pull_request",
            "ref": text,
            "repository": "%s/%s"
            % (slug_match.group("owner"), slug_match.group("repo")),
            "number": int(slug_match.group("number")),
        }
    raise LineageError(
        "lineage reference %r is not a task_<32 hex> id, a pull request URL, "
        "or an owner/repo#number slug" % text
    )


def record_lineage(
    metadata: Optional[Mapping[str, Any]],
    relation: str,
    reference: Any,
    *,
    reason: str = "",
    at: str = "",
    actor: str = "",
) -> JsonDict:
    """Return *metadata* with one forward lineage entry added.

    Pure: the input mapping is not mutated. Re-recording the same
    ``(relation, ref)`` pair is idempotent, so a replayed sweep does not grow
    the chain. A reason is required -- lineage without a reason is the free
    text this module exists to replace.
    """

    relation = _text(relation).lower()
    if relation not in FORWARD_RELATIONS:
        raise LineageError(
            "unsupported lineage relation %r; expected one of %s"
            % (relation, ", ".join(FORWARD_RELATIONS))
        )
    reason = _text(reason)
    if not reason:
        raise LineageError("lineage requires a reason (non-empty)")
    target = normalize_reference(reference)

    updated = dict(metadata or {})
    lineage = _mapping(updated.get(LINEAGE_METADATA_KEY))
    lineage["schema"] = LINEAGE_SCHEMA
    entries = [
        _mapping(entry)
        for entry in lineage.get("entries") or []
        if isinstance(entry, Mapping)
    ]
    entry: JsonDict = {"relation": relation, "target": target, "reason": reason}
    if at:
        entry["at"] = _text(at)
    if actor:
        entry["actor"] = _text(actor)
    duplicate = any(
        _text(existing.get("relation")) == relation
        and _mapping(existing.get("target")).get("ref") == target["ref"]
        for existing in entries
    )
    if not duplicate:
        entries.append(entry)
    lineage["entries"] = entries
    # Flat convenience keys, one per relation, holding the most recent target.
    # These are what `terminal_evidence.lineage_authorization` reads, and what
    # a SQL `json_extract` can reach without walking the entry list.
    lineage[relation] = dict(target)
    updated[LINEAGE_METADATA_KEY] = lineage
    return updated


def lineage_entries(metadata: Optional[Mapping[str, Any]]) -> List[JsonDict]:
    """Return the forward lineage entries recorded on one task's metadata.

    Entries recorded before this module existed are recovered from the
    repository-ref lifecycle's ``replacement_task_id`` pointer, so lineage is
    queryable for rows cancelled as duplicate/superseded under the old
    contract. Those rows named their *successor*, which is the reverse
    direction, so they are reported from the successor's side by
    :func:`build_lineage_index` rather than synthesised here.
    """

    metadata = _mapping(metadata)
    lineage = _mapping(metadata.get(LINEAGE_METADATA_KEY))
    entries = [
        _mapping(entry)
        for entry in lineage.get("entries") or []
        if isinstance(entry, Mapping)
    ]
    if entries:
        return entries
    recovered: List[JsonDict] = []
    for relation in FORWARD_RELATIONS:
        target = lineage.get(relation)
        if target in (None, "", {}):
            continue
        try:
            recovered.append(
                {
                    "relation": relation,
                    "target": normalize_reference(target),
                    "reason": _text(lineage.get("reason")) or "recorded lineage",
                }
            )
        except LineageError:
            continue
    return recovered


def _legacy_replacement(metadata: Mapping[str, Any]) -> JsonDict:
    """The pre-lineage ``replacement_*`` pointer, as a normalized reference."""

    lifecycle = _mapping(_mapping(metadata).get("repository_ref_lifecycle"))
    for key in ("replacement_task_id", "replacement_pull_request"):
        raw = _text(lifecycle.get(key))
        if not raw:
            continue
        try:
            return normalize_reference(raw)
        except LineageError:
            continue
    return {}


def build_lineage_index(tasks: Iterable[Mapping[str, Any]]) -> JsonDict:
    """Index a collection of task dicts into bidirectional lineage.

    Returns ``{"forward": {...}, "reverse": {...}}`` where ``forward[task_id]``
    lists the entries that row itself declares (what it retries, amends, or
    replaces) and ``reverse[ref]`` lists the entries pointing *at* ``ref``
    (what retried, amended, or replaced it). Reverse keys are task ids and pull
    request refs alike, which is what makes "superseded by PR #498" queryable.
    """

    forward: Dict[str, List[JsonDict]] = {}
    reverse: Dict[str, List[JsonDict]] = {}
    for task in tasks or ():
        task = _mapping(task)
        task_id = _text(task.get("id"))
        if not task_id:
            continue
        metadata = _mapping(task.get("metadata"))
        entries = list(lineage_entries(metadata))
        for entry in entries:
            relation = _text(entry.get("relation")).lower()
            target = _mapping(entry.get("target"))
            ref = _text(target.get("ref"))
            if relation not in FORWARD_RELATIONS or not ref:
                continue
            forward.setdefault(task_id, []).append(
                {"relation": relation, "target": target, "reason": _text(entry.get("reason"))}
            )
            reverse.setdefault(ref, []).append(
                {
                    "relation": REVERSE_RELATIONS[relation],
                    "source": {"kind": "task", "ref": task_id},
                    "reason": _text(entry.get("reason")),
                }
            )
        # A legacy cancellation named its successor from the prior row. That is
        # a `replaces` edge owned by the successor, recorded from the wrong
        # side; project it into both directions so old rows stay queryable.
        legacy = _legacy_replacement(metadata)
        legacy_ref = _text(legacy.get("ref"))
        if legacy_ref and legacy_ref != task_id:
            already = any(
                _text(_mapping(entry.get("target")).get("ref")) == legacy_ref
                for entry in entries
            )
            if not already:
                reason = "recorded by the prior row's cancellation disposition"
                reverse.setdefault(task_id, []).append(
                    {
                        "relation": "replaced_by",
                        "source": legacy,
                        "reason": reason,
                    }
                )
                if legacy.get("kind") == "task":
                    forward.setdefault(legacy_ref, []).append(
                        {
                            "relation": "replaces",
                            "target": {"kind": "task", "ref": task_id},
                            "reason": reason,
                        }
                    )
    return {"forward": forward, "reverse": reverse}


def lineage_view(task_id: str, tasks: Iterable[Mapping[str, Any]]) -> JsonDict:
    """Answer the acceptance question for one task.

    ``replaces`` is what this row supersedes, retries, or amends; ``replaced_by``
    is what superseded, retried, or amended it. Both are lists because a scope
    amendment legitimately fans out into several successors.
    """

    task_id = _text(task_id)
    index = build_lineage_index(tasks)
    return {
        "schema": LINEAGE_SCHEMA,
        "task_id": task_id,
        "replaces": list(index["forward"].get(task_id, [])),
        "replaced_by": list(index["reverse"].get(task_id, [])),
    }
