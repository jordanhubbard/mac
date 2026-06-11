"""The embed tool routes through the gateway /embeddings proxy and is exposed to
the code sandbox (from hermes_tools import embed) alongside numpy vector-math
helpers, so an agent can review its memories/dreams: embed -> centroid ->
nearest_neighbors."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERMES = Path(__file__).resolve().parents[1] / "src" / "mac" / "_hermes"
if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))

from tools import embedding_tool  # noqa: E402
from tools.code_execution_tool import generate_hermes_tools_module  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def gateway_env(monkeypatch):
    monkeypatch.setenv("MAC_HERMES_GATEWAY_BASE_URL", "http://127.0.0.1:18789/v1")
    monkeypatch.setenv("MAC_HERMES_GATEWAY_API_KEY", "hub-token")
    monkeypatch.delenv("MAC_HERMES_EMBED_MODEL", raising=False)


def test_embed_posts_to_gateway_embeddings(gateway_env, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({
            "model": "us/azure/openai/text-embedding-3-small",
            "data": [
                {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                {"index": 0, "embedding": [0.1, 0.2, 0.3]},
            ],
        })

    monkeypatch.setattr(embedding_tool.urllib.request, "urlopen", fake_urlopen)
    out = embedding_tool.embed(["alpha", "beta"])

    assert captured["url"] == "http://127.0.0.1:18789/v1/embeddings"
    assert captured["auth"] == "Bearer hub-token"
    assert captured["body"] == {"model": "us/azure/openai/text-embedding-3-small", "input": ["alpha", "beta"]}
    # results sorted by index; dim/count populated
    assert out["count"] == 2 and out["dim"] == 3
    assert out["embeddings"][0] == [0.1, 0.2, 0.3]


def test_embed_default_model_overridable(gateway_env, monkeypatch):
    monkeypatch.setenv("MAC_HERMES_EMBED_MODEL", "custom/embed-v2")
    captured = {}
    monkeypatch.setattr(
        embedding_tool.urllib.request, "urlopen",
        lambda req, timeout=None: captured.update(json.loads(req.data.decode())) or _Resp({"data": []}),
    )
    embedding_tool.embed("x")
    assert captured["model"] == "custom/embed-v2"
    assert captured["input"] == ["x"]


def test_embed_requires_gateway(monkeypatch):
    for k in ("MAC_HERMES_GATEWAY_BASE_URL", "OPENAI_BASE_URL", "MAC_HERMES_GATEWAY_API_KEY", "OPENAI_API_KEY", "MAC_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert embedding_tool.check_embed_requirements() is False
    assert "gateway" in embedding_tool.embed("x").lower()


def test_embed_tool_registered():
    from tools.registry import registry
    assert "embed" in registry._tools


def test_sandbox_exposes_embed_and_vector_helpers():
    src = generate_hermes_tools_module(["embed", "web_search"], transport="uds")
    assert "def embed(" in src
    # the generated module is self-contained python: exec it and exercise the
    # numpy helpers exactly as an agent would in the sandbox.
    ns: dict = {}
    exec(compile(src, "hermes_tools_generated", "exec"), ns)
    assert {"centroid", "cosine_similarity", "nearest_neighbors"} <= ns.keys()

    assert ns["centroid"]([[0, 0, 2], [0, 0, 4]]) == [0.0, 0.0, 3.0]
    assert ns["cosine_similarity"]([1, 0], [1, 0]) == pytest.approx(1.0)
    assert ns["cosine_similarity"]([1, 0], [0, 1]) == pytest.approx(0.0)

    ranked = ns["nearest_neighbors"]([1, 0, 0], [[1, 0, 0], [0, 1, 0], [0.9, 0.1, 0.0]], k=2)
    assert [r["index"] for r in ranked] == [0, 2]
    assert ranked[0]["score"] == pytest.approx(1.0)
