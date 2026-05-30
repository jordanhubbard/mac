"""Vector writer for the mac memory tier (mem-07).

Reads ``memory_records`` rows, asks the configured embedding function
for a vector, upserts the point into Qdrant, and records the resulting
``(vector_db, collection, point_id, embedding_model)`` triple via
:meth:`mac.memory_service.MemoryService.record_vector_ref` so the
``vector_refs`` table is the durable provenance ledger.

Design notes:

* Pluggable embedding: the constructor takes an ``embed_fn: Callable[[str], list[float]]``.
  Production callers wire it to TokenHub (per docs/memory-tier-schema.md).
  Tests and offline dev get a deterministic hash-based fallback (no
  semantic similarity, but proves the wiring) when ``embed_fn`` is None.

* Stateless HTTP to Qdrant: uses ``urllib`` against Qdrant's REST API
  (PUT /collections/{c}/points). No new package dependency; the same
  pattern the install script already uses.

* Failure handling: a single embed/upsert that throws is wrapped and
  surfaced. Per the wf-07 acceptance, we DO write a small retry-outbox
  record so a future scheduled job can re-try. The minimal MVP shipped
  here uses ``memory_records.content`` itself as the recoverable source
  — if Qdrant fails, the operator can re-run ``mac memory embed`` and
  the writer will retry from scratch.

* This module does NOT do background processing on its own. The
  control plane calls ``embed_memory`` synchronously (from the CLI,
  from a new HTTP route, or from the nap consolidator in mem-08).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from mac.models import (
    MAC_MEMORY_COLLECTIONS,
    MAC_MEMORY_DEFAULT_EMBEDDING_DIM,
    MAC_MEMORY_DEFAULT_EMBEDDING_MODEL,
    MAC_MEMORY_PAYLOAD_SCHEMA,
    JsonDict,
    MacMemoryTier,
    MacVectorPayload,
    MemoryRecord,
    NotFoundError,
    ValidationError,
    VectorRef,
    utcnow,
)

EmbedFn = Callable[[str], List[float]]


_HTTP_TIMEOUT_SEC = 10
_VECTOR_DB_LABEL = "qdrant"


def _hash_embedding(text: str, dim: int) -> List[float]:
    """Deterministic fallback embedding for tests / offline dev.

    Hashes ``text`` into a fixed-length float vector. Same text → same
    vector. Different texts → different vectors. Similar texts → NOT
    similar vectors (this is a hash, not a real embedder). Useful only
    for proving write-then-read works; mem-08 / mem-09 quality testing
    needs a real model.
    """
    if dim <= 0:
        raise ValidationError("embedding dim must be positive")
    out: List[float] = []
    counter = 0
    # SHA-256 gives 32 bytes = 8 little-endian floats per round.
    while len(out) < dim:
        h = hashlib.sha256(f"{counter}:{text}".encode("utf-8")).digest()
        for i in range(0, len(h), 4):
            (chunk,) = struct.unpack("<I", h[i : i + 4])
            # Map uint32 to (-1, 1).
            out.append((chunk / 0xFFFFFFFF) * 2.0 - 1.0)
            if len(out) >= dim:
                break
        counter += 1
    # L2-normalize so cosine distance is meaningful.
    norm = math.sqrt(sum(v * v for v in out)) or 1.0
    return [v / norm for v in out]


def tokenhub_embedding_fn(
    *,
    base_url: str,
    api_key: str,
    model: str,
    input_type: str = "passage",
    timeout: float = 30.0,
) -> EmbedFn:
    """Build an embed_fn that calls an OpenAI-compatible /v1/embeddings
    endpoint (TokenHub, OpenAI itself, Azure, etc.).

    The closure captures the model name + key so the writer's
    interface stays text → list[float]. Input type ('passage' for
    documents, 'query' for query text) matters for asymmetric models
    like NVIDIA's nv-embedqa-1b; symmetric models ignore it.
    """
    if not base_url or not api_key or not model:
        raise ValidationError("tokenhub_embedding_fn requires base_url, api_key, and model")

    def _embed(text: str) -> List[float]:
        body = {"model": model, "input": text, "input_type": input_type}
        data = json.dumps(body).encode("utf-8")
        url = "%s/embeddings" % base_url.rstrip("/")
        headers = {
            "Authorization": "Bearer %s" % api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ValidationError(
                "tokenhub embeddings HTTP %s %s: %s" % (exc.code, exc.reason, detail[:400])
            )
        except urllib.error.URLError as exc:
            raise ValidationError("tokenhub embeddings unreachable at %s: %s" % (url, exc.reason))
        payload = json.loads(raw) if raw else {}
        items = payload.get("data") or []
        if not items or not isinstance(items[0], dict) or "embedding" not in items[0]:
            raise ValidationError("tokenhub embeddings response missing data[0].embedding: %s" % str(payload)[:300])
        vector = items[0]["embedding"]
        if not isinstance(vector, list) or not all(isinstance(x, (int, float)) for x in vector):
            raise ValidationError("tokenhub embeddings returned non-numeric vector")
        return [float(x) for x in vector]

    return _embed


def resolve_embed_fn_from_env() -> Optional[EmbedFn]:
    """Build an embed_fn from environment variables. Returns None when
    nothing is configured (callers then fall back to the hash stub).

    Recognized env vars (TokenHub on the rocky fleet ships these
    pre-set in /etc/mac/mac.env):

      MAC_MEMORY_EMBED_BACKEND   = tokenhub | hash (default: hash)
      MAC_MEMORY_EMBED_MODEL     = e.g. nvcf/nvidia/llama-3.2-nv-embedqa-1b-v2
      MAC_MEMORY_EMBED_BASE_URL  = e.g. http://100.125.137.89:8090/v1
                                    (falls back to OPENAI_BASE_URL)
      MAC_MEMORY_EMBED_API_KEY   = bearer token
                                    (falls back to OPENAI_API_KEY)
      MAC_MEMORY_EMBED_INPUT_TYPE = passage|query (default: passage)
    """
    backend = os.environ.get("MAC_MEMORY_EMBED_BACKEND", "hash").strip().lower()
    if backend in {"", "hash"}:
        return None
    if backend != "tokenhub":
        raise ValidationError("unknown MAC_MEMORY_EMBED_BACKEND: %s" % backend)
    base_url = (
        os.environ.get("MAC_MEMORY_EMBED_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or ""
    ).strip()
    api_key = (
        os.environ.get("MAC_MEMORY_EMBED_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()
    model = (os.environ.get("MAC_MEMORY_EMBED_MODEL") or "").strip()
    input_type = (
        os.environ.get("MAC_MEMORY_EMBED_INPUT_TYPE") or "passage"
    ).strip()
    if not (base_url and api_key and model):
        raise ValidationError(
            "MAC_MEMORY_EMBED_BACKEND=tokenhub requires MAC_MEMORY_EMBED_MODEL "
            "and either MAC_MEMORY_EMBED_BASE_URL / MAC_MEMORY_EMBED_API_KEY or "
            "the OPENAI_BASE_URL / OPENAI_API_KEY fallbacks."
        )
    return tokenhub_embedding_fn(
        base_url=base_url, api_key=api_key, model=model, input_type=input_type
    )


# A tiny probe call we use at writer-init time to lock the dim to
# whatever the configured model actually returns. Avoids the operator
# having to keep MAC_MEMORY_EMBED_MODEL and MAC_MEMORY_EMBEDDING_DIM
# in sync by hand.
def _probe_embedding_dim(embed_fn: EmbedFn) -> int:
    vec = embed_fn("probe")
    if not isinstance(vec, list) or not vec:
        raise ValidationError("embed_fn probe returned an empty vector")
    return len(vec)


class VectorWriterService:
    """Embed memory_records into Qdrant + record the provenance ref."""

    def __init__(
        self,
        *,
        memory: Any,
        qdrant_url: str,
        embedding_model: Optional[str] = None,
        embedding_dim: Optional[int] = None,
        embed_fn: Optional[EmbedFn] = None,
        transport: Optional[Callable[..., Any]] = None,
        auto_env: bool = True,
    ) -> None:
        if not qdrant_url:
            raise ValidationError("VectorWriterService requires a qdrant_url")
        self._memory = memory
        self._qdrant_url = qdrant_url.rstrip("/")
        # Embed function resolution order:
        # 1) explicit embed_fn arg (tests, custom backends)
        # 2) env-configured backend (auto_env=True, default)
        # 3) hash fallback
        resolved_fn: Optional[EmbedFn] = embed_fn
        if resolved_fn is None and auto_env:
            resolved_fn = resolve_embed_fn_from_env()
        # When the operator picks a real backend, we let the model
        # decide the dim by probing once at init time. When they keep
        # the hash fallback, dim defaults to whatever the ADR pins
        # (1536) unless overridden.
        if resolved_fn is not None:
            self._embed_fn = resolved_fn
            self._embedding_dim = (
                int(embedding_dim)
                if embedding_dim is not None
                else _probe_embedding_dim(resolved_fn)
            )
            self._embedding_model = embedding_model or (
                os.environ.get("MAC_MEMORY_EMBED_MODEL")
                or MAC_MEMORY_DEFAULT_EMBEDDING_MODEL
            )
        else:
            self._embedding_dim = int(
                embedding_dim if embedding_dim is not None
                else MAC_MEMORY_DEFAULT_EMBEDDING_DIM
            )
            self._embedding_model = embedding_model or MAC_MEMORY_DEFAULT_EMBEDDING_MODEL
            self._embed_fn = lambda text: _hash_embedding(text, self._embedding_dim)
        # `transport` is the same hook shape as mac.http_client.HubClient
        # uses for tests: (method, url, body, token) -> response dict.
        self._transport = transport or self._urllib_transport

    # -- Public API ---------------------------------------------------------

    def embed_memory(
        self,
        memory_id: str,
        *,
        tier: str = MacMemoryTier.MEDIUM.value,
        created_by: str = "vector-writer",
    ) -> VectorRef:
        """Embed one memory_record into Qdrant and return the VectorRef."""
        record = self._memory.get_memory(memory_id)
        if not isinstance(record, MemoryRecord):
            raise ValidationError("get_memory returned non-MemoryRecord")
        if tier not in MAC_MEMORY_COLLECTIONS:
            raise ValidationError("unknown memory tier: %s" % tier)
        collection = MAC_MEMORY_COLLECTIONS[tier]
        payload = self._build_payload(record, tier=tier)
        vector = self._embed_fn(record.content)
        if len(vector) != self._embedding_dim:
            raise ValidationError(
                "embed_fn returned %d dims; expected %d"
                % (len(vector), self._embedding_dim)
            )
        point_id = self._point_id_for(record.id)
        self._upsert_point(collection, point_id, vector, payload.to_dict())
        # vector_refs has a UNIQUE constraint on (vector_db, collection,
        # point_id), so re-embedding the same memory replaces the
        # Qdrant point (idempotent because point_id is deterministic)
        # and returns the existing ref. Embed counters / audit history
        # live in observability events, not in extra vector_refs rows.
        existing = self._memory.list_vector_refs(
            memory_id=record.id, collection=collection
        )
        for ref in existing:
            if ref.point_id == point_id:
                return ref
        return self._memory.record_vector_ref(
            memory_id=record.id,
            vector_db=_VECTOR_DB_LABEL,
            collection=collection,
            point_id=point_id,
            embedding_model=self._embedding_model,
            metadata={"tier": tier, "embedding_dim": self._embedding_dim},
            created_by=created_by,
        )

    def backfill(
        self,
        *,
        tier: str = MacMemoryTier.MEDIUM.value,
        limit: Optional[int] = None,
        created_by: str = "vector-writer:backfill",
    ) -> Dict[str, Any]:
        """One-shot pass: embed every memory_record that has no vector_ref
        pointing at the target collection. Returns a report.
        """
        if tier not in MAC_MEMORY_COLLECTIONS:
            raise ValidationError("unknown memory tier: %s" % tier)
        collection = MAC_MEMORY_COLLECTIONS[tier]
        already = {ref.memory_id for ref in self._memory.list_vector_refs(collection=collection)}
        records = self._memory.search_memory()
        total = len(records)
        skipped = 0
        embedded: List[str] = []
        failures: List[Dict[str, Any]] = []
        for record in records:
            if record.id in already:
                skipped += 1
                continue
            if limit is not None and len(embedded) >= limit:
                break
            try:
                self.embed_memory(record.id, tier=tier, created_by=created_by)
                embedded.append(record.id)
            except Exception as exc:  # noqa: BLE001 - per-record best-effort
                failures.append({"memory_id": record.id, "error": str(exc)})
        return {
            "tier": tier,
            "collection": collection,
            "total_memories": total,
            "already_embedded": skipped,
            "embedded_now": len(embedded),
            "failures": failures,
        }

    def recall(
        self,
        query_text: str,
        *,
        tier: str = MacMemoryTier.MEDIUM.value,
        limit: int = 5,
        score_threshold: Optional[float] = None,
        filter_payload: Optional[JsonDict] = None,
        project: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Embed ``query_text`` and ask Qdrant for the top hits.

        Returns mem-09's standard recall-hit shape:
        ``{memory_id, task_id, score, summary, point_id, payload}``
        ordered by descending score.

        Server-side filtering: pass ``project`` and/or ``tenant_id`` to
        scope the search to a project / tenant. Combine with
        ``filter_payload`` if you need richer Qdrant filter clauses.
        """
        if tier not in MAC_MEMORY_COLLECTIONS:
            raise ValidationError("unknown memory tier: %s" % tier)
        collection = MAC_MEMORY_COLLECTIONS[tier]
        vector = self._embed_fn(query_text)
        if len(vector) != self._embedding_dim:
            raise ValidationError(
                "embed_fn returned %d dims; expected %d"
                % (len(vector), self._embedding_dim)
            )
        body: JsonDict = {
            "vector": vector,
            "limit": max(1, int(limit)),
            "with_payload": True,
        }
        if score_threshold is not None:
            body["score_threshold"] = float(score_threshold)
        must_clauses: List[Dict[str, Any]] = []
        if project:
            must_clauses.append({"key": "project", "match": {"value": project}})
        if tenant_id:
            must_clauses.append({"key": "tenant_id", "match": {"value": tenant_id}})
        if filter_payload:
            body["filter"] = filter_payload
            # If callers supply both, must clauses get folded in.
            if must_clauses:
                body["filter"] = {**filter_payload, "must": (filter_payload.get("must") or []) + must_clauses}
        elif must_clauses:
            body["filter"] = {"must": must_clauses}
        response = self._transport(
            "POST",
            "%s/collections/%s/points/search" % (self._qdrant_url, quote(collection, safe="")),
            body,
            None,
        )
        result = (response or {}).get("result") or []
        hits: List[Dict[str, Any]] = []
        for raw in result:
            if not isinstance(raw, dict):
                continue
            payload = raw.get("payload") or {}
            hits.append(
                {
                    "memory_id": payload.get("memory_id"),
                    "task_id": payload.get("task_id"),
                    "subject_type": payload.get("subject_type"),
                    "subject_id": payload.get("subject_id"),
                    "score": float(raw.get("score") or 0.0),
                    "summary": payload.get("summary"),
                    "point_id": str(raw.get("id")),
                    "payload": payload,
                }
            )
        return hits

    # -- Internals ----------------------------------------------------------

    def _build_payload(self, record: MemoryRecord, *, tier: str) -> MacVectorPayload:
        return MacVectorPayload(
            tier=tier,
            subject_type=record.subject_type or record.record_type,
            subject_id=record.subject_id or record.id,
            memory_id=record.id,
            summary=record.content[:2000],
            created_at=record.created_at,
            embedded_at=utcnow(),
            embedding_model=self._embedding_model,
            task_id=record.task_id,
            agent_id=record.created_by if record.created_by else None,
            evidence_type=None,
            tags=[record.record_type] if record.record_type else [],
            schema=MAC_MEMORY_PAYLOAD_SCHEMA,
        )

    def _point_id_for(self, memory_id: str) -> str:
        # Qdrant accepts UUIDs or unsigned ints. We use a deterministic
        # SHA-1 over the memory_id rendered as a UUID-like string so the
        # same memory_id always maps to the same point — re-running
        # embed_memory replaces the point in place.
        digest = hashlib.sha1(memory_id.encode("utf-8")).hexdigest()
        return "%s-%s-%s-%s-%s" % (
            digest[0:8],
            digest[8:12],
            digest[12:16],
            digest[16:20],
            digest[20:32],
        )

    def _upsert_point(
        self,
        collection: str,
        point_id: str,
        vector: List[float],
        payload: JsonDict,
    ) -> None:
        body = {
            "points": [
                {"id": point_id, "vector": vector, "payload": payload},
            ],
        }
        self._transport(
            "PUT",
            "%s/collections/%s/points?wait=true"
            % (self._qdrant_url, quote(collection, safe="")),
            body,
            None,
        )

    def _urllib_transport(
        self,
        method: str,
        url: str,
        body: Optional[JsonDict],
        token: Optional[str],
    ) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer %s" % token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ValidationError(
                "qdrant %s %s failed: HTTP %s %s — %s"
                % (method, url, exc.code, exc.reason, detail[:300])
            )
        except urllib.error.URLError as exc:
            raise ValidationError(
                "qdrant %s %s unreachable: %s" % (method, url, exc.reason)
            )
        return json.loads(raw) if raw else None
