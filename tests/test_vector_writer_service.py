"""mem-07: vector writer end-to-end coverage.

Two kinds of tests:

1. **Unit tests** with a fake-Qdrant transport that records every PUT
   and serves point queries from an in-memory dict. Prove the writer
   wires the right URLs, builds the right payloads, and records the
   provenance ref in `vector_refs`.

2. **A real round-trip e2e** that writes 3 memory_records about
   distinct topics, embeds them (deterministic hash embedder for
   reproducibility), and asks the recall API to find them by id. Proves
   that "write a memory → embed → store → look back up" works end to
   end inside the writer service, with no real network calls.

To run an integration test against a real Qdrant, set
``QDRANT_URL=http://127.0.0.1:6333`` (or the rocky endpoint) and use
``mac memory backfill`` + ``mac memory recall``. The CLI uses the same
VectorWriterService class.
"""
from __future__ import annotations

import pytest

from mac.services import ControlPlane
from mac.vector_writer_service import VectorWriterService, _hash_embedding


# ---------------------------------------------------------------------------
# Fake Qdrant transport
# ---------------------------------------------------------------------------


class _FakeQdrant:
    """In-process stand-in for Qdrant's REST API.

    Just enough surface for VectorWriterService:
      PUT  /collections/<c>/points?wait=true → upsert
      POST /collections/<c>/points/search    → cosine-rank against stored vectors

    Records every call so tests can assert URL/body shape.
    """

    def __init__(self):
        self.collections: dict[str, dict[str, dict]] = {}
        self.collection_sizes: dict[str, int] = {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method: str, url: str, body, token):
        self.calls.append((method, url, body))
        # Strip query string.
        path = url.split("?", 1)[0]
        # Lift the collection name out of the path.
        parts = path.rsplit("/collections/", 1)
        if len(parts) != 2:
            raise AssertionError("FakeQdrant got non-collections URL: %s" % url)
        rest = parts[1]
        coll, _, suffix = rest.partition("/")
        coll = coll.split("?", 1)[0]
        self.collections.setdefault(coll, {})
        if method == "GET" and not suffix:
            size = self.collection_sizes.get(coll)
            if size is None and self.collections[coll]:
                first = next(iter(self.collections[coll].values()))
                size = len(first.get("vector") or [])
            vectors = {"size": size, "distance": "Cosine"} if size else {}
            return {
                "result": {"config": {"params": {"vectors": vectors}}},
                "status": "ok",
            }
        if method == "PUT" and suffix.startswith("points"):
            for point in (body or {}).get("points", []):
                vector = list(point.get("vector") or [])
                if vector:
                    self.collection_sizes.setdefault(coll, len(vector))
                self.collections[coll][str(point["id"])] = {
                    "id": str(point["id"]),
                    "vector": vector,
                    "payload": point.get("payload") or {},
                }
            return {"result": {"status": "ok"}, "status": "ok"}
        if method == "POST" and suffix == "points/search":
            query_vec = list(body.get("vector") or [])
            limit = int(body.get("limit") or 10)
            # Honor a minimal subset of Qdrant's filter language:
            # filter.must = [{key: <field>, match: {value: <v>}}, ...]
            must_clauses = []
            f = body.get("filter") or {}
            if isinstance(f, dict):
                must_clauses = [c for c in (f.get("must") or []) if isinstance(c, dict)]

            def _matches(point_payload):
                for clause in must_clauses:
                    key = clause.get("key")
                    match = clause.get("match") or {}
                    expected = match.get("value")
                    if point_payload.get(key) != expected:
                        return False
                return True

            # Cosine similarity (vectors are L2-normalized by writer).
            results = []
            for stored in self.collections[coll].values():
                vec = stored["vector"]
                if not vec or len(vec) != len(query_vec):
                    continue
                if must_clauses and not _matches(stored["payload"]):
                    continue
                score = sum(a * b for a, b in zip(vec, query_vec))
                results.append(
                    {"id": stored["id"], "score": score, "payload": stored["payload"]}
                )
            results.sort(key=lambda r: r["score"], reverse=True)
            return {"result": results[:limit], "status": "ok"}
        raise AssertionError("FakeQdrant: unhandled %s %s" % (method, url))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


@pytest.fixture()
def fake_qdrant() -> _FakeQdrant:
    return _FakeQdrant()


@pytest.fixture()
def writer(cp, fake_qdrant) -> VectorWriterService:
    return VectorWriterService(
        memory=cp.memory,
        qdrant_url="http://fake.invalid:6333",
        embedding_dim=64,  # smaller dim → faster tests; same logic
        transport=fake_qdrant,
    )


def _add_memory(cp, content, **kwargs):
    return cp.add_memory(
        task_id=None,
        subject_type=kwargs.get("subject_type", "topic"),
        subject_id=kwargs.get("subject_id", None),
        record_type=kwargs.get("record_type", "note"),
        content=content,
        evidence_id=None,
        created_by=kwargs.get("created_by", "human"),
    )


# ---------------------------------------------------------------------------
# Unit-level behavior
# ---------------------------------------------------------------------------


def test_embed_memory_upserts_point_and_records_vector_ref(cp, writer, fake_qdrant):
    """A single embed call → one upsert to Qdrant, one vector_refs row,
    and the payload contains everything the schema requires."""
    record = _add_memory(cp, "the slack notifier broke last week")
    ref = writer.embed_memory(record.id)

    # vector_refs row landed with the right fingerprint.
    assert ref.memory_id == record.id
    assert ref.collection == "mac_memory_medium"
    assert ref.vector_db == "qdrant"
    assert ref.embedding_model == "text-embedding-3-small"

    # The point itself is in fake Qdrant.
    assert "mac_memory_medium" in fake_qdrant.collections
    points = fake_qdrant.collections["mac_memory_medium"]
    assert ref.point_id in points
    stored = points[ref.point_id]
    assert stored["payload"]["memory_id"] == record.id
    assert stored["payload"]["tier"] == "medium"
    assert stored["payload"]["schema"] == "mac.memory.v1"
    assert stored["payload"]["summary"].startswith("the slack notifier")


def test_embed_memory_is_idempotent_for_same_memory_id(cp, writer, fake_qdrant):
    """Re-embedding the same memory replaces the Qdrant point in place
    (point_id is deterministic) and returns the existing vector_refs
    row — no duplicate rows on Qdrant or in mac.db."""
    record = _add_memory(cp, "the slack notifier broke last week")
    first = writer.embed_memory(record.id)
    second = writer.embed_memory(record.id)
    assert first.id == second.id  # same ref row
    assert first.point_id == second.point_id
    assert len(fake_qdrant.collections["mac_memory_medium"]) == 1
    refs = cp.memory.list_vector_refs(memory_id=record.id)
    assert len(refs) == 1


def test_embed_memory_round_trip_recall_finds_the_record(cp, writer, fake_qdrant):
    """The actual 'does memory work' test:
    1. write three memories about distinct topics
    2. embed all three
    3. ask recall() with the EXACT text of one of them
    4. that memory must be the top hit

    Uses the deterministic hash embedding by default. Same text →
    same vector, so the query text matches its own stored vector
    perfectly (cosine score = 1.0). Lower-scoring hits prove the
    other memories also landed cleanly."""
    a = _add_memory(cp, "the slack notifier broke last week")
    b = _add_memory(cp, "ship the qdrant memory tier writer")
    c = _add_memory(cp, "rocky's mac.db is 3GB; need a prune timer")
    for r in (a, b, c):
        writer.embed_memory(r.id)

    hits = writer.recall("ship the qdrant memory tier writer", limit=3)
    assert len(hits) == 3
    top = hits[0]
    assert top["payload"]["memory_id"] == b.id
    # The exact-match hit scores near 1.0 (vectors are L2-normalized).
    assert top["score"] > 0.99
    # The other memories should rank lower (different topics → different
    # hashed vectors → near-zero similarity).
    for hit in hits[1:]:
        assert hit["score"] < top["score"]


def test_recall_handles_no_matching_memories(cp, writer):
    """No memories embedded yet → recall returns an empty list, not None."""
    hits = writer.recall("anything", limit=5)
    assert hits == []


def test_backfill_skips_already_embedded_memories(cp, writer, fake_qdrant):
    """backfill is idempotent: running twice doesn't double-embed."""
    a = _add_memory(cp, "topic one")
    b = _add_memory(cp, "topic two")
    first = writer.backfill()
    assert first["embedded_now"] == 2
    assert first["already_embedded"] == 0
    second = writer.backfill()
    assert second["embedded_now"] == 0
    assert second["already_embedded"] == 2
    # Each memory has exactly one point in Qdrant.
    assert len(fake_qdrant.collections["mac_memory_medium"]) == 2


def test_backfill_honors_limit_param(cp, writer):
    """`limit` caps how many memories one backfill pass embeds."""
    for i in range(5):
        _add_memory(cp, f"memory number {i}")
    report = writer.backfill(limit=2)
    assert report["embedded_now"] == 2
    assert report["total_memories"] == 5


def test_writer_refuses_unknown_tier(cp, writer):
    record = _add_memory(cp, "x")
    with pytest.raises(Exception, match="unknown memory tier"):
        writer.embed_memory(record.id, tier="cold")


def test_env_backend_tokenhub_requires_full_config(cp, monkeypatch):
    """MAC_MEMORY_EMBED_BACKEND=tokenhub without the rest of the env
    raises ValidationError at writer-init time, rather than silently
    falling back to the hash stub."""
    from mac.vector_writer_service import VectorWriterService
    monkeypatch.setenv("MAC_MEMORY_EMBED_BACKEND", "tokenhub")
    monkeypatch.delenv("MAC_MEMORY_EMBED_MODEL", raising=False)
    monkeypatch.delenv("MAC_MEMORY_EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MAC_MEMORY_EMBED_API_KEY", raising=False)
    with pytest.raises(Exception, match="requires MAC_MEMORY_EMBED_MODEL"):
        VectorWriterService(memory=cp.memory, qdrant_url="http://x:1")


def test_env_backend_unknown_value_rejected(cp, monkeypatch):
    monkeypatch.setenv("MAC_MEMORY_EMBED_BACKEND", "magic")
    with pytest.raises(Exception, match="unknown MAC_MEMORY_EMBED_BACKEND"):
        from mac.vector_writer_service import VectorWriterService
        VectorWriterService(memory=cp.memory, qdrant_url="http://x:1")


# mem-store-02: real embeddings are the default ("auto") whenever a model +
# endpoint are configured; otherwise a safe hash fallback.

def test_auto_backend_uses_real_embedder_when_configured(monkeypatch):
    import mac.vector_writer_service as v
    monkeypatch.delenv("MAC_MEMORY_EMBED_BACKEND", raising=False)  # default = auto
    monkeypatch.setenv("MAC_MEMORY_EMBED_BASE_URL", "http://hub/v1")
    monkeypatch.setenv("MAC_MEMORY_EMBED_API_KEY", "k")
    monkeypatch.setenv("MAC_MEMORY_EMBED_MODEL", "embed-1")
    assert v.resolve_embed_fn_from_env() is not None  # real embedder, no opt-in needed


def test_auto_backend_falls_back_to_hash_when_unconfigured(monkeypatch):
    import mac.vector_writer_service as v
    for k in (
        "MAC_MEMORY_EMBED_BACKEND", "MAC_MEMORY_EMBED_MODEL", "MAC_MEMORY_EMBED_BASE_URL",
        "MAC_MEMORY_EMBED_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    assert v.resolve_embed_fn_from_env() is None  # hash stub


def test_batch_embed_preserves_order_and_short_circuits(monkeypatch):
    import json
    import mac.vector_writer_service as v

    class _Resp:
        def __init__(self, b): self._b = b
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    captured = {}

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        captured["n"] = len(body["input"])
        # return OUT OF ORDER to prove we sort by index
        data = [{"index": i, "embedding": [float(i)]} for i in reversed(range(len(body["input"])))]
        return _Resp(json.dumps({"data": data}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    fn = v.tokenhub_embedding_batch_fn(base_url="http://h/v1", api_key="k", model="m")
    assert fn([]) == []                       # empty short-circuits (no call)
    out = fn(["a", "b", "c"])
    assert out == [[0.0], [1.0], [2.0]]        # sorted back into input order
    assert captured["n"] == 3                  # one call for all three


def test_batch_embed_rejects_count_mismatch(monkeypatch):
    import json
    import mac.vector_writer_service as v
    from mac.models import ValidationError

    class _Resp:
        def __init__(self, b): self._b = b
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _Resp(json.dumps({"data": [{"index": 0, "embedding": [1.0]}]}).encode()),
    )
    fn = v.tokenhub_embedding_batch_fn(base_url="http://h/v1", api_key="k", model="m")
    with pytest.raises(ValidationError, match="returned 1 vectors for 2 inputs"):
        fn(["a", "b"])


def test_env_backend_hash_default_keeps_static_dim(cp, monkeypatch, fake_qdrant):
    """With the hash backend (default), dim comes from the kwarg or
    MAC_MEMORY_DEFAULT_EMBEDDING_DIM — no probe call."""
    monkeypatch.delenv("MAC_MEMORY_EMBED_BACKEND", raising=False)
    from mac.vector_writer_service import VectorWriterService
    w = VectorWriterService(
        memory=cp.memory,
        qdrant_url="http://x:1",
        embedding_dim=128,
        transport=fake_qdrant,
    )
    record = _add_memory(cp, "test")
    ref = w.embed_memory(record.id)
    # Vector in fake Qdrant has the asked-for dim.
    point = fake_qdrant.collections["mac_memory_medium"][ref.point_id]
    assert len(point["vector"]) == 128


def test_backend_dim_locked_lazily_from_first_vector(cp, fake_qdrant):
    """With a real embed_fn and no pinned dim, the writer does NOT probe
    at construction — it locks the dim to the first embedded vector and
    then enforces it. A later vector of a different length is rejected."""
    from mac.models import ValidationError
    from mac.vector_writer_service import VectorWriterService

    state = {"dim": 16}
    w = VectorWriterService(
        memory=cp.memory,
        qdrant_url="http://x:1",
        embed_fn=lambda text: [0.1] * state["dim"],
        transport=fake_qdrant,
    )
    # Nothing probed at init: dim is still unknown until the first embed.
    assert w._embedding_dim is None
    r1 = _add_memory(cp, "first")
    ref = w.embed_memory(r1.id)
    assert len(fake_qdrant.collections["mac_memory_medium"][ref.point_id]["vector"]) == 16
    assert w._embedding_dim == 16
    # Backend now returns a different dim → the locked dim is enforced.
    state["dim"] = 32
    r2 = _add_memory(cp, "second")
    with pytest.raises(ValidationError, match="expected 16"):
        w.embed_memory(r2.id)


def test_recall_returns_mem09_standard_shape(cp, writer, fake_qdrant):
    """mem-09: recall hits expose memory_id / task_id / summary at the
    top level, not buried under payload."""
    record = _add_memory(cp, "feature: workflow inspector slide-in")
    writer.embed_memory(record.id)
    hits = writer.recall("feature: workflow inspector slide-in", limit=1)
    assert hits, "expected at least one hit"
    hit = hits[0]
    # Standard mem-09 fields exposed directly:
    assert hit["memory_id"] == record.id
    assert "task_id" in hit  # may be None when memory has none
    assert hit["score"] > 0.99
    assert hit["summary"].startswith("feature: workflow inspector")
    assert hit["subject_type"]
    # The full payload is still there for callers that want it.
    assert hit["payload"]["memory_id"] == record.id


def test_recall_project_filter_scopes_to_payload_project(cp, writer, fake_qdrant):
    """mem-09: server-side project filter; only matching points return."""
    a = cp.add_memory(
        task_id=None, subject_type="topic", subject_id=None,
        record_type="note", content="alpha topic", evidence_id=None,
        created_by="agent_one",
    )
    b = cp.add_memory(
        task_id=None, subject_type="topic", subject_id=None,
        record_type="note", content="beta topic", evidence_id=None,
        created_by="agent_one",
    )
    writer.embed_memory(a.id)
    writer.embed_memory(b.id)
    # Force a project on one of the stored points so we can filter.
    points = fake_qdrant.collections["mac_memory_medium"]
    for point in points.values():
        if point["payload"]["memory_id"] == a.id:
            point["payload"]["project"] = "proj-alpha"
    hits = writer.recall("alpha topic", limit=5, project="proj-alpha")
    assert hits, "filtered query should still return the alpha point"
    assert all(h["payload"].get("project") == "proj-alpha" for h in hits)
    # The unfiltered baseline returns both points.
    baseline = writer.recall("alpha topic", limit=5)
    assert len(baseline) == 2


def test_embed_fn_returning_wrong_dim_is_rejected(cp, fake_qdrant):
    """If a misconfigured embed_fn returns the wrong dim, the writer
    refuses rather than silently corrupting the Qdrant collection."""
    cp_local = ControlPlane.in_memory()
    record = cp_local.add_memory(
        task_id=None, subject_type="x", subject_id=None, record_type="n",
        content="hello", evidence_id=None, created_by="human",
    )
    writer = VectorWriterService(
        memory=cp_local.memory,
        qdrant_url="http://fake.invalid:6333",
        embedding_dim=64,
        embed_fn=lambda text: [0.1] * 32,  # wrong dim
        transport=fake_qdrant,
    )
    with pytest.raises(Exception, match="returned 32 dims; expected 64"):
        writer.embed_memory(record.id)


def test_hash_fallback_adapts_to_existing_collection_dim(cp, fake_qdrant):
    """Offline hash mode should still match an already-provisioned collection."""
    fake_qdrant.collection_sizes["mac_memory_medium"] = 2048
    record = _add_memory(cp, "collection dimension controls hash fallback")
    writer = VectorWriterService(
        memory=cp.memory,
        qdrant_url="http://fake.invalid:6333",
        transport=fake_qdrant,
        auto_env=False,
    )

    ref = writer.embed_memory(record.id)

    point = fake_qdrant.collections["mac_memory_medium"][ref.point_id]
    assert len(point["vector"]) == 2048
    assert ref.metadata["embedding_dim"] == 2048


def test_env_model_drift_falls_back_to_collection_vector_ref_model(
    cp,
    fake_qdrant,
    monkeypatch,
):
    """If env drifts to a different-sized model, collection provenance wins.

    The fallback is collection-scoped: repairing legacy medium recall must not
    switch future long-memory writes away from the current configured model.
    """
    record = _add_memory(cp, "legacy model memory survives env drift")
    legacy = VectorWriterService(
        memory=cp.memory,
        qdrant_url="http://fake.invalid:6333",
        embedding_model="legacy-2048",
        embedding_dim=2048,
        transport=fake_qdrant,
        auto_env=False,
    )
    legacy.embed_memory(record.id)

    monkeypatch.setenv("MAC_MEMORY_EMBED_BASE_URL", "http://hub/v1")
    monkeypatch.setenv("MAC_MEMORY_EMBED_API_KEY", "k")
    monkeypatch.setenv("MAC_MEMORY_EMBED_MODEL", "drifted-1536")

    def fake_tokenhub_embedding_fn(*, base_url, api_key, model, input_type="passage", timeout=30.0):
        if model == "drifted-1536":
            return lambda text: [0.1] * 1536
        if model == "legacy-2048":
            return lambda text: _hash_embedding(text, 2048)
        raise AssertionError("unexpected model %s" % model)

    monkeypatch.setattr(
        "mac.vector_writer_service.tokenhub_embedding_fn",
        fake_tokenhub_embedding_fn,
    )

    drifted = VectorWriterService(
        memory=cp.memory,
        qdrant_url="http://fake.invalid:6333",
        transport=fake_qdrant,
    )
    hits = drifted.recall("legacy model memory survives env drift", limit=1)

    assert hits[0]["memory_id"] == record.id
    assert hits[0]["score"] > 0.99
    assert drifted._embedding_model == "drifted-1536"

    long_record = _add_memory(cp, "new long memory uses current env model")
    long_ref = drifted.embed_memory(long_record.id, tier="long")
    long_point = fake_qdrant.collections["mac_memory_long"][long_ref.point_id]
    assert len(long_point["vector"]) == 1536
    assert long_point["payload"]["embedding_model"] == "drifted-1536"
    assert long_ref.embedding_model == "drifted-1536"
    assert long_ref.metadata["embedding_dim"] == 1536
