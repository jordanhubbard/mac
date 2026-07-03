"""Embedding tool (embed-01).

Gives an agent vector embeddings via the in-mac router's ``/v1/embeddings``
proxy, so it can embed text — its own memories, dreams, or arbitrary concepts —
and do vector math (centroid, nearest-neighbour) on them inside ``execute_code``
(``from hermes_tools import embed``; the sandbox also ships ``centroid()`` /
``cosine_similarity()`` / ``nearest_neighbors()`` numpy helpers).

It routes through the gateway exactly like chat does: the agent presents its hub
token and the hub swaps in the upstream key — so it works on the hub and on
spokes with no extra plumbing. The default model is the verified-working
``us/azure/openai/text-embedding-3-small`` (1536-dim); override per call with
``model=`` or fleet-wide with ``MAC_HERMES_EMBED_MODEL``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from tools.registry import registry, tool_error

DEFAULT_EMBED_MODEL = "us/azure/openai/text-embedding-3-small"


def _gateway() -> Tuple[str, str]:
    base = (
        os.environ.get("MAC_HERMES_GATEWAY_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or ""
    ).strip().rstrip("/")
    token = (
        os.environ.get("MAC_HERMES_GATEWAY_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("MAC_API_TOKEN")
        or ""
    ).strip()
    return base, token


def _default_model() -> str:
    return (os.environ.get("MAC_HERMES_EMBED_MODEL") or "").strip() or DEFAULT_EMBED_MODEL


def embed(text: Any, model: Optional[str] = None) -> Dict[str, Any]:
    """Embed one string or a list of strings through the gateway /embeddings
    proxy. Returns {"model", "dim", "count", "embeddings": [[float, ...], ...]}.
    On failure returns a tool_error string."""
    base, token = _gateway()
    if not base or not token:
        return tool_error("embed needs MAC_HERMES_GATEWAY_BASE_URL + a gateway token in the agent env")
    inputs: List[str] = [text] if isinstance(text, str) else [str(t) for t in (text or [])]
    if not inputs:
        return tool_error("embed requires at least one input string")
    payload = {"model": (model or _default_model()), "input": inputs}
    req = urllib.request.Request(
        base + "/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (operator-configured hub)
            out = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return tool_error("embeddings upstream HTTP %s: %s" % (exc.code, exc.read().decode("utf-8", "replace")[:200]))
    except Exception as exc:  # noqa: BLE001
        return tool_error("embed failed: %s" % exc)
    rows = sorted(out.get("data") or [], key=lambda d: d.get("index", 0))
    vectors = [r.get("embedding") for r in rows if r.get("embedding") is not None]
    return {
        "model": out.get("model") or payload["model"],
        "count": len(vectors),
        "dim": len(vectors[0]) if vectors else 0,
        "embeddings": vectors,
    }


EMBED_SCHEMA = {
    "name": "embed",
    "description": (
        "Get vector embeddings for text via the fleet's shared embedding model. Best "
        "used inside execute_code (`from hermes_tools import embed`) so you can embed "
        "your memories, dreams, or concepts and do vector math on them — the sandbox "
        "also exposes `centroid()`, `cosine_similarity()` and `nearest_neighbors()` "
        "(numpy). Pass a string or a list of strings; returns "
        "{model, dim, count, embeddings:[[float,...],...]}. Default model: "
        "us/azure/openai/text-embedding-3-small (1536-dim)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "input": {
                "description": "a string, or a list of strings, to embed",
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
            },
            "model": {"type": "string", "description": "optional embedding model override"},
        },
        "required": ["input"],
    },
}


def _handle_embed(args, **kw):
    try:
        result = embed(args.get("input"), model=args.get("model"))
    except Exception as exc:  # noqa: BLE001
        return tool_error("embed tool error: %s" % exc)
    return result if isinstance(result, str) else json.dumps(result)


def check_embed_requirements() -> bool:
    """Available when the agent has gateway access (base URL + token in env)."""
    base, token = _gateway()
    return bool(base and token)


registry.register(
    name="embed",
    toolset="embed",
    schema=EMBED_SCHEMA,
    handler=_handle_embed,
    check_fn=check_embed_requirements,
    requires_env=[],
    is_async=False,
    emoji="🧮",
)
