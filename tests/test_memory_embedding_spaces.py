"""One collection, one embedding space.

The 2026-08-21 audit found ``mac_memory_medium`` holding vectors from two
embedders — 601 points from ``nvcf/nvidia/llama-3.2-nv-embedqa-1b-v2`` and 66
from ``azure/openai/text-embedding-3-large``. Qdrant compares them anyway:
same dimension, incommensurable spaces. Similarity search returned wrong
neighbours and raised nothing, which is the worst failure shape there is.

Two defences are pinned here. Recall filters to the query's own space, so a
mixed collection degrades to *fewer* results rather than *wrong* ones. And
``reconcile_embedding_spaces`` re-embeds the stragglers so the collection
goes back to holding one space.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from mac.models import MAC_MEMORY_COLLECTIONS, MacMemoryTier, ValidationError
from mac.services import ControlPlane
from mac.vector_writer_service import VectorWriterService

MEDIUM = MAC_MEMORY_COLLECTIONS[MacMemoryTier.MEDIUM.value]

NVIDIA = "nvcf/nvidia/llama-3.2-nv-embedqa-1b-v2"
AZURE = "azure/openai/text-embedding-3-large"


class _FakeQdrant:
    """Upsert + scroll + cosine search, honouring ``filter.must`` matches."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict]] = {}
        self.searches: list[dict] = []

    def __call__(self, method, url, body=None, token=None):
        path = url.split("?", 1)[0]
        coll, _, suffix = path.rsplit("/collections/", 1)[1].partition("/")
        self.collections.setdefault(coll, {})
        if method == "GET" and not suffix:
            points = self.collections[coll]
            size = len(next(iter(points.values()))["vector"]) if points else None
            return {
                "result": {
                    "points_count": len(points),
                    "config": {"params": {"vectors": {"size": size} if size else {}}},
                }
            }
        if method == "PUT" and suffix.startswith("points"):
            for point in (body or {}).get("points", []):
                self.collections[coll][str(point["id"])] = {
                    "id": str(point["id"]),
                    "vector": list(point.get("vector") or []),
                    "payload": point.get("payload") or {},
                }
            return {"result": {"status": "ok"}}
        if method == "POST" and suffix == "points/scroll":
            return {
                "result": {
                    "points": [
                        {"id": p["id"], "payload": p["payload"]}
                        for p in self.collections[coll].values()
                    ],
                    "next_page_offset": None,
                }
            }
        if method == "POST" and suffix == "points/search":
            self.searches.append(dict(body or {}))
            must = ((body or {}).get("filter") or {}).get("must") or []

            def _matches(payload):
                return all(
                    payload.get(c.get("key")) == (c.get("match") or {}).get("value")
                    for c in must
                    if isinstance(c, dict)
                )

            query = list((body or {}).get("vector") or [])
            hits = []
            for stored in self.collections[coll].values():
                if len(stored["vector"]) != len(query):
                    continue
                if must and not _matches(stored["payload"]):
                    continue
                hits.append(
                    {
                        "id": stored["id"],
                        "score": sum(a * b for a, b in zip(stored["vector"], query)),
                        "payload": stored["payload"],
                    }
                )
            hits.sort(key=lambda h: h["score"], reverse=True)
            return {"result": hits[: int((body or {}).get("limit") or 10)]}
        raise AssertionError("FakeQdrant: unhandled %s %s" % (method, url))


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


@pytest.fixture()
def fake_qdrant():
    return _FakeQdrant()


def _writer(cp, fake_qdrant, model):
    """A writer pinned to one model. Same dim, so both land in one
    collection exactly as the two live embedders did."""
    return VectorWriterService(
        memory=cp.memory,
        qdrant_url="http://fake.invalid:6333",
        embedding_model=model,
        embedding_dim=32,
        transport=fake_qdrant,
    )


@pytest.fixture()
def nvidia_writer(cp, fake_qdrant):
    return _writer(cp, fake_qdrant, NVIDIA)


@pytest.fixture()
def azure_writer(cp, fake_qdrant):
    return _writer(cp, fake_qdrant, AZURE)


def _add(cp, content):
    return cp.add_memory(
        task_id=None,
        subject_type="topic",
        subject_id=None,
        record_type="note",
        content=content,
        evidence_id=None,
        created_by="agent_one",
    )


# ---------------------------------------------------------------------------
# Recall must not mix spaces
# ---------------------------------------------------------------------------


def test_recall_returns_only_the_query_s_own_embedding_space(
    cp, nvidia_writer, azure_writer
):
    """A hit from the other space is not a worse hit, it is a meaningless
    one — the score is a dot product between vectors that share nothing but
    a dimension count."""
    mine = _add(cp, "the deploy gate rejects unsigned evidence")
    theirs = _add(cp, "the deploy gate rejects unsigned evidence")
    nvidia_writer.embed_memory(mine.id)
    azure_writer.embed_memory(theirs.id)

    hits = nvidia_writer.recall("deploy gate", limit=10)

    assert [h["memory_id"] for h in hits] == [mine.id]
    assert {h["payload"]["embedding_model"] for h in hits} == {NVIDIA}


def test_recall_can_be_asked_to_look_across_spaces(cp, nvidia_writer, azure_writer):
    """Escape hatch for inspecting what the other space holds."""
    mine = _add(cp, "alpha")
    theirs = _add(cp, "beta")
    nvidia_writer.embed_memory(mine.id)
    azure_writer.embed_memory(theirs.id)

    hits = nvidia_writer.recall("alpha", limit=10, strict_embedding_space=False)

    assert {h["memory_id"] for h in hits} == {mine.id, theirs.id}


def test_recall_combines_the_space_filter_with_project_scoping(
    cp, nvidia_writer, fake_qdrant
):
    """The new clause must not displace the existing server-side filters."""
    record = _add(cp, "something")
    nvidia_writer.embed_memory(record.id)

    nvidia_writer.recall("something", project="mac", limit=5)

    must = fake_qdrant.searches[-1]["filter"]["must"]
    keys = {clause["key"] for clause in must}
    assert keys == {"embedding_model", "project"}


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def test_report_names_both_spaces_without_writing(cp, nvidia_writer, azure_writer):
    for content in ("a", "b", "c"):
        nvidia_writer.embed_memory(_add(cp, content).id)
    azure_writer.embed_memory(_add(cp, "d").id)

    report = nvidia_writer.embedding_space_report()

    assert report["target_model"] == NVIDIA
    assert report["embedding_models"] == {NVIDIA: 3, AZURE: 1}
    assert report["mismatched"] == 1


def test_reconcile_re_embeds_the_minority_space(
    cp, nvidia_writer, azure_writer, fake_qdrant
):
    keeper = _add(cp, "already in the majority space")
    straggler = _add(cp, "left behind by the model switch")
    nvidia_writer.embed_memory(keeper.id)
    azure_writer.embed_memory(straggler.id)

    report = nvidia_writer.reconcile_embedding_spaces()

    assert report["mismatched"] == 1
    assert report["reembedded_memory_ids"] == [straggler.id]
    models = {
        point["payload"]["embedding_model"]
        for point in fake_qdrant.collections[MEDIUM].values()
    }
    assert models == {NVIDIA}
    # Same point id, replaced in place: no duplicate left behind.
    assert len(fake_qdrant.collections[MEDIUM]) == 2


def test_reconcile_restamps_the_provenance_ledger(cp, nvidia_writer, azure_writer):
    """vector_refs is UNIQUE on (db, collection, point_id) and point ids are
    deterministic, so a re-embed cannot insert a second row. Without an
    update the ledger keeps naming a model that is no longer in the
    collection — the exact provenance lie the audit had to untangle by
    hand."""
    straggler = _add(cp, "left behind by the model switch")
    azure_writer.embed_memory(straggler.id)
    assert [r.embedding_model for r in cp.memory.list_vector_refs(
        memory_id=straggler.id
    )] == [AZURE]

    nvidia_writer.reconcile_embedding_spaces()

    refs = cp.memory.list_vector_refs(memory_id=straggler.id, collection=MEDIUM)
    assert [ref.embedding_model for ref in refs] == [NVIDIA]


def test_reconcile_dry_run_changes_nothing(cp, nvidia_writer, azure_writer, fake_qdrant):
    straggler = _add(cp, "left behind")
    azure_writer.embed_memory(straggler.id)

    report = nvidia_writer.reconcile_embedding_spaces(dry_run=True)

    assert report["dry_run"] is True
    assert report["reembedded_memory_ids"] == [straggler.id]
    models = {
        point["payload"]["embedding_model"]
        for point in fake_qdrant.collections[MEDIUM].values()
    }
    assert models == {AZURE}


def test_reconcile_reports_points_it_cannot_rebuild(
    cp, nvidia_writer, azure_writer, fake_qdrant
):
    """A point with no ``memory_id`` has no source text to re-embed from.
    Deleting it is the operator's call, so it is reported rather than
    quietly skipped."""
    straggler = _add(cp, "left behind")
    azure_writer.embed_memory(straggler.id)
    for point in fake_qdrant.collections[MEDIUM].values():
        point["payload"].pop("memory_id", None)

    report = nvidia_writer.reconcile_embedding_spaces()

    assert report["reembedded"] == 0
    assert len(report["orphaned"]) == 1


def test_reconcile_is_a_no_op_on_a_single_space_collection(cp, nvidia_writer):
    for content in ("a", "b"):
        nvidia_writer.embed_memory(_add(cp, content).id)

    report = nvidia_writer.reconcile_embedding_spaces()

    assert report["mismatched"] == 0
    assert report["reembedded"] == 0


def test_control_plane_reconcile_routes_to_the_writer(cp, nvidia_writer, azure_writer):
    straggler = _add(cp, "left behind")
    azure_writer.embed_memory(straggler.id)

    report = cp.reconcile_memory_embedding_spaces(vector_writer=nvidia_writer)

    assert report["reembedded_memory_ids"] == [straggler.id]


def test_control_plane_report_only_makes_no_writes(cp, nvidia_writer, azure_writer):
    azure_writer.embed_memory(_add(cp, "left behind").id)

    report = cp.reconcile_memory_embedding_spaces(
        vector_writer=nvidia_writer, report_only=True
    )

    assert report["mismatched"] == 1
    assert "reembedded" not in report


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tier": "sample"},
        {"limit": "sample"},
        {"limit": 0},
        {"scan_limit": "sample"},
    ],
)
def test_control_plane_reconcile_rejects_unusable_arguments(cp, nvidia_writer, kwargs):
    """An unknown tier or a junk bound is a rejected request, not a traceback."""
    with pytest.raises(ValidationError):
        cp.reconcile_memory_embedding_spaces(vector_writer=nvidia_writer, **kwargs)


@pytest.mark.parametrize("report_only", [False, True])
def test_control_plane_reconcile_rejects_an_incapable_writer(cp, report_only):
    """Say the writer cannot do this, rather than AttributeError mid-scan."""
    with pytest.raises(ValidationError):
        cp.reconcile_memory_embedding_spaces(
            vector_writer=SimpleNamespace(recall=lambda **_: []),
            report_only=report_only,
        )


# ---------------------------------------------------------------------------
# Ledger primitives
# ---------------------------------------------------------------------------


def test_update_vector_ref_rewrites_model_and_metadata(cp, nvidia_writer):
    record = _add(cp, "a memory")
    ref = nvidia_writer.embed_memory(record.id)

    updated = cp.memory.update_vector_ref(
        ref.id, embedding_model=AZURE, metadata={"tier": "medium", "note": "moved"}
    )

    assert updated.embedding_model == AZURE
    assert updated.metadata["note"] == "moved"


def test_delete_vector_ref_removes_the_row(cp, nvidia_writer):
    record = _add(cp, "a memory")
    ref = nvidia_writer.embed_memory(record.id)

    cp.memory.delete_vector_ref(ref.id)

    assert cp.memory.list_vector_refs(memory_id=record.id) == []
