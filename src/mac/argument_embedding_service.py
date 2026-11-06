"""argument_embedding_service.py

Bridges deliberation resolution to Qdrant semantic indexing.

At deliberation resolution time, extract each argument's claim text,
embed it via the fleet embedding API (hub /v1/embeddings), and upsert
to the deliberation_arguments Qdrant collection.

This is the v2 Spark-side layer: the v1 deliberation_service stores
arguments as JSONB local to the deliberation record.  This service
makes those arguments queryable across all past deliberations via
semantic similarity.

Collection schema (deliberation_arguments, 2048-dim Cosine):
  payload:
    argument_claim:   str   - the actual argument text
    argument_type:    str   - empirical | authority | preference | principle
    deliberation_id:  str   - links back to the deliberation record
    resolution_path:  str   - authority | unanimity | expiry
    agent_id:         str   - who made the argument
    created_at:       str   - ISO 8601
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from typing import Any

QDRANT_URL = "http://100.72.16.110:6333"
EMBED_URL = "http://100.72.16.110:8789/v1/embeddings"
EMBED_MODEL = "azure/openai/text-embedding-3-large"
EMBED_DIM = 2048
COLLECTION = "deliberation_arguments"


class ArgumentEmbeddingService:
    """Embed deliberation arguments and upsert them to Qdrant.

    Parameters
    ----------
    worker_token:
        MAC worker token for the embedding API.
    qdrant_url:
        Base URL of the Qdrant instance (no trailing slash).
    """

    def __init__(
        self,
        *,
        worker_token: str,
        qdrant_url: str = QDRANT_URL,
        embed_url: str = EMBED_URL,
    ) -> None:
        self._token = worker_token
        self._qdrant = qdrant_url
        self._embed_url = embed_url

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def index_resolved_deliberation(
        self,
        *,
        deliberation_id: str,
        resolution_path: str,
        arguments: list[dict[str, Any]],
    ) -> int:
        """Embed and upsert all arguments from a resolved deliberation.

        Returns the number of arguments upserted.
        """
        if not arguments:
            return 0

        # Filter to arguments with actual claim text
        eligible = [a for a in arguments if a.get("claim", "").strip()]
        if not eligible:
            return 0

        points = []
        for arg in eligible:
            claim_text = arg["claim"].strip()
            vec = self._embed(claim_text)
            point_id = self._stable_id(deliberation_id, arg.get("id", claim_text))
            points.append(
                {
                    "id": point_id,
                    "vector": vec,
                    "payload": {
                        "argument_claim": claim_text,
                        "argument_type": arg.get("argument_type", "principle"),
                        "deliberation_id": deliberation_id,
                        "resolution_path": resolution_path,
                        "agent_id": arg.get("agent_id", ""),
                        "created_at": arg.get("created_at", ""),
                    },
                }
            )

        self._upsert(points)
        return len(points)

    def search_similar_arguments(
        self,
        query_text: str,
        *,
        top_k: int = 5,
        argument_type_filter: str | None = None,
        resolution_path_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find past arguments semantically similar to query_text.

        Optional filters narrow by argument_type or resolution_path.
        """
        vec = self._embed(query_text)

        body: dict[str, Any] = {
            "vector": vec,
            "limit": top_k,
            "with_payload": True,
        }

        conditions = []
        if argument_type_filter:
            conditions.append({"key": "argument_type", "match": {"value": argument_type_filter}})
        if resolution_path_filter:
            conditions.append(
                {"key": "resolution_path", "match": {"value": resolution_path_filter}}
            )
        if conditions:
            body["filter"] = {"must": conditions}

        url = f"{self._qdrant}/collections/{COLLECTION}/points/search"
        resp = self._qdrant_request("POST", url, body)
        return resp.get("result", [])

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _embed(self, text: str) -> list[float]:
        """Return a 2048-dim embedding for text."""
        payload = json.dumps(
            {
                "model": EMBED_MODEL,
                "input": text,
                "dimensions": EMBED_DIM,
            }
        ).encode()
        req = urllib.request.Request(
            self._embed_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        return data["data"][0]["embedding"]

    def _upsert(self, points: list[dict[str, Any]]) -> None:
        """Upsert points into the deliberation_arguments collection."""
        url = f"{self._qdrant}/collections/{COLLECTION}/points"
        self._qdrant_request("PUT", url, {"points": points})

    def _qdrant_request(
        self,
        method: str,
        url: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())  # type: ignore[no-any-return]
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Qdrant {method} {url} → HTTP {e.code}: {body_text}") from e

    @staticmethod
    def _stable_id(deliberation_id: str, argument_id: str) -> int:
        """Deterministic Qdrant point ID from deliberation+argument IDs.

        Qdrant requires unsigned int or UUID.  We use a stable 32-bit hash
        (positive, since Python's hash() is not stable across processes).
        """
        raw = f"{deliberation_id}:{argument_id}"
        return int(hashlib.sha256(raw.encode()).hexdigest(), 16) % (2**31)
