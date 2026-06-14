"""Phase 2b — export the fleet's vector memory for vetting (+ a vetted prune).

The soul snapshot (Phase 1/2) covers the editable identity text and references
the binary memory blobs. This adds the *semantic* memory layer: export each
agent's Qdrant memories into a flat, greppable JSONL so an operator can find
stale facts (a long-dead teammate, a retired directive) that wouldn't surface
from the soul text alone — and then prune the vetted point ids.

Transport is injectable: pass ``scroll(collection) -> iterable[point]`` and
``delete(collection, ids)`` callables; the HTTP implementations talk to Qdrant's
REST API. Pure record-shaping (:func:`export_memory_records`) is unit-tested
with a fake; the network layer is a thin wrapper.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

DEFAULT_COLLECTIONS: Sequence[str] = ("mac_memory_medium", "mac_memory_long")

# Payload fields worth surfacing for vetting (see the live Qdrant schema).
_VET_FIELDS = ("memory_id", "agent_id", "summary", "tags", "tier", "subject_type",
               "subject_id", "created_at")


def export_memory_records(
    scroll: Callable[[str], Iterable[Dict[str, Any]]],
    collections: Sequence[str] = DEFAULT_COLLECTIONS,
    *,
    agent_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Flatten Qdrant points across *collections* into vetting records.

    ``scroll(collection)`` yields points shaped ``{id, payload}``. Records keep
    the point ``id`` + ``collection`` (needed to prune) plus the vetting fields.
    Filters to ``agent_id`` when given.
    """
    out: List[Dict[str, Any]] = []
    for col in collections:
        for point in scroll(col):
            payload = point.get("payload") or {}
            if agent_id is not None and str(payload.get("agent_id")) != str(agent_id):
                continue
            rec: Dict[str, Any] = {"collection": col, "id": point.get("id")}
            for f in _VET_FIELDS:
                if f in payload:
                    rec[f] = payload[f]
            out.append(rec)
    return out


def search_records(records: Sequence[Dict[str, Any]], needle: str) -> List[Dict[str, Any]]:
    """Case-insensitive substring match over each record's text (the vetting
    convenience the operator would otherwise grep for)."""
    n = needle.lower()
    return [r for r in records if n in json.dumps(r, default=str).lower()]


def prune_points(
    delete: Callable[[str, List[Any]], Any],
    collection: str,
    ids: Sequence[Any],
) -> Dict[str, Any]:
    """Delete the vetted point ids from *collection*. No-op for an empty list."""
    ids = [i for i in ids if i is not None]
    if not ids:
        return {"collection": collection, "deleted": 0, "skipped": "no ids"}
    delete(collection, ids)
    return {"collection": collection, "deleted": len(ids)}


# ---------------------------------------------------------------------------
# Qdrant REST transport
# ---------------------------------------------------------------------------


class QdrantClient:
    """Minimal Qdrant REST client: scroll (paginated) + delete-by-id."""

    def __init__(self, base_url: str, *, timeout: float = 15.0, page: int = 256) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.page = page

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8", "replace"))

    def scroll(self, collection: str) -> Iterable[Dict[str, Any]]:
        offset = None
        while True:
            body: Dict[str, Any] = {"limit": self.page, "with_payload": True, "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            result = self._post("/collections/%s/points/scroll" % collection, body).get("result", {})
            points = result.get("points", []) or []
            for p in points:
                yield p
            offset = result.get("next_page_offset")
            if not offset or not points:
                break

    def delete(self, collection: str, ids: List[Any]) -> Dict[str, Any]:
        return self._post("/collections/%s/points/delete" % collection, {"points": list(ids)})
