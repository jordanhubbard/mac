"""Edge and failure-path coverage for the vector writer boundary."""

from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from mac.models import MemoryRecord, ValidationError
from mac.services import ControlPlane
from mac.vector_writer_service import (
    VectorWriterService,
    _hash_embedding,
    tokenhub_embedding_batch_fn,
    tokenhub_embedding_fn,
)


class _Response:
    def __init__(self, payload: object = None, *, raw: bytes | None = None) -> None:
        self.raw = raw if raw is not None else json.dumps(payload).encode()

    def read(self) -> bytes:
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> bool:
        return False


def _record(content: str = "content", **overrides: object) -> MemoryRecord:
    values = {
        "id": "memory-1",
        "task_id": None,
        "subject_type": "topic",
        "subject_id": None,
        "record_type": "note",
        "content": content,
        "evidence_id": None,
        "created_by": "agent",
        "created_at": "2026-01-01T00:00:00Z",
    }
    values.update(overrides)
    return MemoryRecord(**values)


def _writer(*, memory: object | None = None, transport=None, **kwargs) -> VectorWriterService:
    if memory is None:
        memory = SimpleNamespace(list_vector_refs=lambda **_kwargs: [])
    if transport is None:
        transport = lambda *_args: {}
    return VectorWriterService(
        memory=memory,
        qdrant_url="http://qdrant",
        transport=transport,
        auto_env=False,
        **kwargs,
    )


def test_hash_and_constructor_validation() -> None:
    with pytest.raises(ValidationError, match="positive"):
        _hash_embedding("x", 0)
    with pytest.raises(ValidationError, match="qdrant_url"):
        VectorWriterService(memory=object(), qdrant_url="")
    with pytest.raises(ValidationError, match="base_url"):
        tokenhub_embedding_fn(base_url="", api_key="k", model="m")
    with pytest.raises(ValidationError, match="base_url"):
        tokenhub_embedding_batch_fn(base_url="u", api_key="", model="m")


def test_tokenhub_single_embedding_success_and_validation(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def open_ok(request, timeout=None):
        captured["body"] = json.loads(request.data)
        captured["auth"] = request.headers["Authorization"]
        captured["timeout"] = timeout
        return _Response({"data": [{"embedding": [1, 2.5]}]})

    monkeypatch.setattr("urllib.request.urlopen", open_ok)
    embed = tokenhub_embedding_fn(
        base_url="http://tokenhub/v1/", api_key="secret", model="embed", timeout=4
    )
    assert embed("hello") == [1.0, 2.5]
    assert captured == {
        "body": {"model": "embed", "input": "hello", "input_type": "passage"},
        "auth": "Bearer secret",
        "timeout": 4,
    }

    for payload, message in [
        ({}, "missing data"),
        ({"data": [None]}, "missing data"),
        ({"data": [{"embedding": ["bad"]}]}, "non-numeric"),
    ]:
        monkeypatch.setattr("urllib.request.urlopen", lambda *_a, p=payload, **_k: _Response(p))
        with pytest.raises(ValidationError, match=message):
            embed("hello")


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("kind", ["http", "url"])
def test_tokenhub_embedding_network_errors(monkeypatch, batch: bool, kind: str) -> None:
    if kind == "http":
        error = urllib.error.HTTPError(
            "http://tokenhub", 503, "unavailable", {}, io.BytesIO(b"backend down")
        )
        match = "HTTP 503"
    else:
        error = urllib.error.URLError("connection refused")
        match = "unreachable"

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("urllib.request.urlopen", fail)
    factory = tokenhub_embedding_batch_fn if batch else tokenhub_embedding_fn
    embed = factory(base_url="http://tokenhub/v1", api_key="k", model="m")
    with pytest.raises(ValidationError, match=match):
        embed(["x"]) if batch else embed("x")


def test_tokenhub_batch_rejects_non_numeric_items(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_a, **_k: _Response({"data": [{"index": 0, "embedding": None}]}),
    )
    embed = tokenhub_embedding_batch_fn(base_url="http://h", api_key="k", model="m")
    with pytest.raises(ValidationError, match="non-numeric"):
        embed(["x"])


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"result": {"config": {"params": {"vectors": {"size": "7"}}}}}, 7),
        ({"result": {"config": {"params": {"vectors": {"size": "bad"}}}}}, None),
        (
            {
                "result": {
                    "config": {
                        "params": {
                            "vectors": {
                                "bad": {"size": "nope"},
                                "named": {"size": 9},
                            }
                        }
                    }
                }
            },
            9,
        ),
        ({"result": None}, None),
        (None, None),
    ],
)
def test_collection_vector_size_shapes_and_cache(response, expected) -> None:
    calls = 0

    def transport(*_args):
        nonlocal calls
        calls += 1
        return response

    writer = _writer(transport=transport)
    assert writer._collection_vector_size("name with slash") == expected
    assert writer._collection_vector_size("name with slash") == expected
    assert calls == 1


def test_collection_vector_size_tolerates_transport_failure() -> None:
    def fail(*_args):
        raise RuntimeError("offline")

    writer = _writer(transport=fail)
    assert writer._collection_vector_size("medium") is None
    assert writer._collection_vector_size("medium") is None


def test_empty_and_incompatible_embeddings_are_rejected() -> None:
    writer = _writer(embed_fn=lambda _text: [], embedding_dim=2)
    with pytest.raises(ValidationError, match="empty vector"):
        writer._resolve_dim([])
    with pytest.raises(ValidationError, match="empty vector"):
        writer._ensure_collection_compatible_vector("x", "c", [])

    writer._collection_dims["c"] = 3
    with pytest.raises(ValidationError, match="requires 3 dims"):
        writer._ensure_collection_compatible_vector("x", "c", [1.0, 2.0])


def test_collection_model_fallback_cached_and_rediscovered(monkeypatch) -> None:
    refs = [
        SimpleNamespace(embedding_model="", metadata={"embedding_dim": 3}),
        SimpleNamespace(embedding_model="current", metadata={"embedding_dim": 3}),
        SimpleNamespace(embedding_model="bad-meta", metadata={"embedding_dim": "bad"}),
        SimpleNamespace(embedding_model="wrong-dim", metadata={"embedding_dim": 8}),
        SimpleNamespace(embedding_model="broken", metadata={"embedding_dim": 3}),
        SimpleNamespace(embedding_model="wrong-vector", metadata={"embedding_dim": 3}),
        SimpleNamespace(embedding_model="legacy", metadata={"embedding_dim": 3}),
    ]
    memory = SimpleNamespace(list_vector_refs=lambda **_kwargs: refs)
    writer = _writer(memory=memory, embed_fn=lambda _text: [1.0], embedding_model="current")
    writer._allow_collection_model_fallback = True
    writer._env_base_url = "http://tokenhub"
    writer._env_api_key = "key"

    def factory(*, model, **_kwargs):
        if model == "broken":
            return lambda _text: (_ for _ in ()).throw(RuntimeError("bad model"))
        if model == "wrong-vector":
            return lambda _text: [1.0]
        assert model == "legacy"
        return lambda _text: [1.0, 2.0, 3.0]

    monkeypatch.setattr("mac.vector_writer_service.tokenhub_embedding_fn", factory)
    assert writer._try_collection_ref_model("c", "text", 3) == (
        [1.0, 2.0, 3.0],
        "legacy",
        3,
    )
    assert writer._try_collection_ref_model("c", "again", 3)[1] == "legacy"

    writer._collection_embed_overrides["c"] = (
        "stale",
        3,
        lambda _text: (_ for _ in ()).throw(RuntimeError("stale")),
    )
    assert writer._try_collection_ref_model("c", "text", 3)[1] == "legacy"
    writer._collection_embed_overrides["c"] = ("short", 3, lambda _text: [1.0])
    assert writer._try_collection_ref_model("c", "text", 3)[1] == "legacy"


def test_collection_model_fallback_best_effort_guards() -> None:
    writer = _writer()
    assert writer._try_collection_ref_model("c", "x", 3) is None
    writer._allow_collection_model_fallback = True
    assert writer._try_collection_ref_model("c", "x", 3) is None
    writer._env_base_url = "u"
    writer._env_api_key = "k"
    writer._memory = SimpleNamespace(
        list_vector_refs=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("db"))
    )
    assert writer._try_collection_ref_model("c", "x", 3) is None


def test_embed_backfill_and_recall_validation_paths(monkeypatch) -> None:
    writer = _writer(memory=SimpleNamespace(get_memory=lambda _id: object()))
    with pytest.raises(ValidationError, match="non-MemoryRecord"):
        writer.embed_memory("x")

    cp = ControlPlane.in_memory()
    writer = _writer(memory=cp.memory)
    with pytest.raises(ValidationError, match="unknown memory tier"):
        writer.backfill(tier="unknown")
    with pytest.raises(ValidationError, match="unknown memory tier"):
        writer.recall("x", tier="unknown")

    record = cp.add_memory(
        task_id=None,
        subject_type="topic",
        subject_id=None,
        record_type="note",
        content="will fail",
        evidence_id=None,
        created_by="test",
    )
    monkeypatch.setattr(
        writer, "embed_memory", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("qdrant down"))
    )
    report = writer.backfill()
    assert report["failures"] == [{"memory_id": record.id, "error": "qdrant down"}]


def test_recall_builds_filters_and_normalizes_hits() -> None:
    calls: list[tuple[str, object]] = []

    def transport(method, _url, body, _token):
        calls.append((method, body))
        if method == "GET":
            return {}
        return {
            "result": [
                "invalid",
                {"id": 123, "score": None, "payload": None},
            ]
        }

    writer = _writer(transport=transport, embed_fn=lambda _text: [1.0])
    hits = writer.recall(
        "query",
        limit=0,
        score_threshold=0.25,
        filter_payload={"must": [{"key": "existing"}]},
        project="project-a",
        tenant_id="tenant-a",
        agent_id="agent-a",
    )
    search = calls[-1][1]
    assert search["limit"] == 1
    assert search["score_threshold"] == 0.25
    # Caller's own clause + embedding-space guard + project/tenant/agent.
    assert len(search["filter"]["must"]) == 5
    assert {"key": "agent_id", "match": {"value": "agent-a"}} in search["filter"]["must"]
    assert {
        "key": "embedding_model",
        "match": {"value": "text-embedding-3-small"},
    } in search["filter"]["must"]
    assert hits == [
        {
            "memory_id": None,
            "task_id": None,
            "subject_type": None,
            "subject_id": None,
            "score": 0.0,
            "summary": None,
            "point_id": "123",
            "payload": {},
        }
    ]


@pytest.mark.parametrize(
    ("subject_type", "subject_id", "content", "expected"),
    [
        ("project", "project-a", "plain", {"project": "project-a"}),
        ("nap_summary", "agent-a", "plain", {"agent_id": "agent-a"}),
        (
            "topic",
            None,
            json.dumps({"schema": "mac.deployment_learning.v1", "project": "learned"}),
            {"project": "learned"},
        ),
        (
            "topic",
            None,
            json.dumps(
                {
                    "schema": "mac.dream.v1",
                    "retrieval": "invalid",
                    "kind": "insight",
                    "scope": "project",
                    "confidence": "high",
                    "confidence_score": "invalid",
                    "summary": "dream summary",
                }
            ),
            {"dream_kind": "insight", "dream_confidence_score": None},
        ),
    ],
)
def test_payload_specializations(subject_type, subject_id, content, expected) -> None:
    payload = (
        _writer()
        ._build_payload(
            _record(content, subject_type=subject_type, subject_id=subject_id), tier="medium"
        )
        .to_dict()
    )
    for key, value in expected.items():
        assert payload.get(key) == value


def test_urllib_transport_success_empty_and_errors(monkeypatch) -> None:
    writer = _writer()
    requests: list[object] = []

    def open_json(request, timeout=None):
        requests.append(request)
        assert timeout == 10
        return _Response({"ok": True})

    monkeypatch.setattr("urllib.request.urlopen", open_json)
    assert writer._urllib_transport("POST", "http://q/x", {"a": 1}, "token") == {"ok": True}
    request = requests[-1]
    assert request.headers["Authorization"] == "Bearer token"
    assert request.headers["Content-type"] == "application/json"

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: _Response(raw=b""))
    assert writer._urllib_transport("GET", "http://q/x", None, None) is None

    errors = [
        (
            urllib.error.HTTPError("http://q/x", 500, "bad", {}, io.BytesIO(b"qdrant detail")),
            "HTTP 500",
        ),
        (urllib.error.URLError("refused"), "unreachable"),
    ]
    for error, message in errors:
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, error=error, **_k: (_ for _ in ()).throw(error),
        )
        with pytest.raises(ValidationError, match=message):
            writer._urllib_transport("GET", "http://q/x", None, None)
