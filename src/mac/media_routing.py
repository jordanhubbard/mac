"""media-01: operation-keyed media routing table + provider adapters.

The chat router (``MAC_ROUTER_PROVIDERS``) already does ordered, keyed,
secret-ref provider routing; media (image/audio/video) was a flat
one-upstream-one-key passthrough (``MAC_ROUTER_<M>_{UPSTREAM,KEY}``). This module
generalizes media to the same shape: a ``"<modality>.<operation>"`` key resolves
to an *ordered list* of provider bindings, and a per-binding **adapter**
translates a single canonical request to/from each provider's wire format. The
router (:func:`mac.router_app.mount_media_router`) walks the list in priority
order and falls back to the next binding on failure.

Pure + dependency-free (stdlib only) so the table/adapters are unit-testable
without FastAPI or a live upstream; the HTTP forward is supplied by the caller.

Canonical media request (the body of ``POST /v1/media/<op>``)::

    {"prompt": str, "width"?: int, "height"?: int, "steps"?: int,
     "seed"?: int, "cfg_scale"?: float, "model"?: str}

Canonical response::

    {"artifacts": [{"base64": "<...>"}], "provider": str, "model": str}

Config (fleets.yaml ``defaults.router.media`` → ``MAC_ROUTER_MEDIA_JSON``)::

    {"image.generate": [
        {"provider": "nvidia-nim", "base_url": "https://ai.api.nvidia.com/v1/genai",
         "model": "black-forest-labs/flux.1-schnell", "key": "secret:nvidia-image",
         "adapter": "nvidia_genai", "priority": 0}]}

If unset, ``image.generate`` is synthesized from the existing flat
``MAC_ROUTER_IMAGE_{UPSTREAM,KEY,MODEL,TIMEOUT}`` (the degenerate single binding),
so existing fleets keep working with no config change.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Tuple

logger = logging.getLogger("mac.media_routing")

DEFAULT_IMAGE_MODEL = "black-forest-labs/flux.1-schnell"


class MediaBinding(NamedTuple):
    provider: str  # display id, e.g. "nvidia-nim"
    base_url: str  # upstream genai base, e.g. https://ai.api.nvidia.com/v1/genai
    key_spec: str  # "secret:<name>" (vault) or a literal bearer; resolved at use
    model: str  # provider model id, e.g. black-forest-labs/flux.1-schnell
    adapter: str  # adapter id (see ADAPTERS)
    timeout: float


# An adapter is a (to_provider, from_provider) pair:
#   to_provider(op, canonical_body, model) -> (path, provider_body)
#   from_provider(status, provider_resp)   -> canonical_resp (dict)
RequestAdapter = Callable[[str, Mapping[str, Any], str], Tuple[str, Dict[str, Any]]]
ResponseAdapter = Callable[[Optional[int], Any], Dict[str, Any]]


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _strip_data_uri(value: str) -> str:
    if isinstance(value, str) and value.startswith("data:"):
        comma = value.find(",")
        if comma != -1:
            return value[comma + 1 :]
    return value


def extract_b64(data: Any) -> Optional[str]:
    """Pull base64 image bytes out of the several NVIDIA genai / OpenAI-images
    response shapes (mirrors the nvidia image plugin's _extract_b64)."""
    if not isinstance(data, dict):
        return None
    artifacts = data.get("artifacts")
    if isinstance(artifacts, list) and artifacts and isinstance(artifacts[0], dict):
        b64 = artifacts[0].get("base64") or artifacts[0].get("b64_json")
        if isinstance(b64, str) and b64:
            return _strip_data_uri(b64)
    for key in ("image", "b64_json"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return _strip_data_uri(value)
    for list_key in ("data", "images"):
        seq = data.get(list_key)
        if isinstance(seq, list) and seq:
            first = seq[0]
            if isinstance(first, str) and first:
                return _strip_data_uri(first)
            if isinstance(first, dict):
                b64 = first.get("b64_json") or first.get("base64") or first.get("image")
                if isinstance(b64, str) and b64:
                    return _strip_data_uri(b64)
    return None


# --- nvidia_genai adapter ---------------------------------------------------

def _is_stability(model: str) -> bool:
    m = model.lower()
    return "stable-diffusion" in m or "sdxl" in m or "stability" in m


def nvidia_genai_request(op: str, body: Mapping[str, Any], model: str) -> Tuple[str, Dict[str, Any]]:
    """Canonical request -> NVIDIA genai (FLUX flat dialect or Stability
    text_prompts dialect). Path is the model id appended to the binding base."""
    prompt = str(body.get("prompt") or "").strip()
    seed = _int(body.get("seed"), 0)
    is_schnell = "schnell" in model.lower()
    steps = _int(body.get("steps"), 4 if is_schnell else (25 if _is_stability(model) else 50))
    if _is_stability(model):
        provider_body = {
            "text_prompts": [{"text": prompt, "weight": 1.0}],
            "cfg_scale": _float(body.get("cfg_scale"), 5.0),
            "sampler": "K_DPM_2_ANCESTRAL",
            "seed": seed,
            "steps": steps,
        }
    else:  # FLUX flat body
        provider_body = {
            "prompt": prompt,
            "mode": "base",
            "cfg_scale": _float(body.get("cfg_scale"), 0.0 if is_schnell else 3.5),
            "width": _int(body.get("width"), 1024),
            "height": _int(body.get("height"), 1024),
            "seed": seed,
            "steps": steps,
        }
    return "/" + model.strip("/"), provider_body


def nvidia_genai_response(status: Optional[int], resp: Any) -> Dict[str, Any]:
    """NVIDIA genai response -> canonical {artifacts:[{base64}]} (or error)."""
    if status and 200 <= status < 300:
        b64 = extract_b64(resp)
        if b64:
            return {"artifacts": [{"base64": b64}]}
        return {"error": {"message": "no recognizable image data in upstream response",
                          "type": "empty_response"}}
    if isinstance(resp, dict):
        return resp
    return {"error": {"message": str(resp)[:500], "type": "upstream_error"}}


def passthrough_request(op: str, body: Mapping[str, Any], model: str) -> Tuple[str, Dict[str, Any]]:
    """No translation: forward the body to /<model> (or / when model is empty).
    For providers that already speak the canonical shape, or opaque upstreams."""
    return ("/" + model.strip("/")) if model else "/", dict(body)


def passthrough_response(status: Optional[int], resp: Any) -> Dict[str, Any]:
    return resp if isinstance(resp, dict) else {"error": {"message": str(resp)[:500]}}


ADAPTERS: Dict[str, Tuple[RequestAdapter, ResponseAdapter]] = {
    "nvidia_genai": (nvidia_genai_request, nvidia_genai_response),
    "passthrough": (passthrough_request, passthrough_response),
}


def build_media_table(env: Optional[Mapping[str, str]] = None) -> Dict[str, List[MediaBinding]]:
    """Build ``{op: [MediaBinding, ...]}`` (priority order) from the env.

    ``MAC_ROUTER_MEDIA_JSON`` (the structured table) takes precedence; any op it
    omits falls back to a degenerate single binding synthesized from the flat
    ``MAC_ROUTER_IMAGE_*`` proxy vars, so existing fleets are unchanged.
    """
    env = os.environ if env is None else env
    table: Dict[str, List[MediaBinding]] = {}

    raw = (env.get("MAC_ROUTER_MEDIA_JSON") or "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            logger.warning("MAC_ROUTER_MEDIA_JSON is not valid JSON; ignoring (%s)", exc)
            data = {}
        if isinstance(data, dict):
            for op, raw_bindings in data.items():
                if not isinstance(raw_bindings, list):
                    continue
                bindings: List[MediaBinding] = []
                for b in sorted(
                    (x for x in raw_bindings if isinstance(x, dict)),
                    key=lambda x: _int(x.get("priority"), 0),
                ):
                    base = str(b.get("base_url") or "").strip().rstrip("/")
                    if not base:
                        continue
                    bindings.append(
                        MediaBinding(
                            provider=str(b.get("provider") or "provider"),
                            base_url=base,
                            key_spec=str(b.get("key") or ""),
                            model=str(b.get("model") or ""),
                            adapter=str(b.get("adapter") or "passthrough"),
                            timeout=_float(b.get("timeout"), 180.0),
                        )
                    )
                if bindings:
                    table[str(op)] = bindings

    # Back-compat: synthesize image.generate from the flat image proxy vars.
    if "image.generate" not in table:
        base = (env.get("MAC_ROUTER_IMAGE_UPSTREAM") or "").strip().rstrip("/")
        key = (env.get("MAC_ROUTER_IMAGE_KEY") or "").strip()
        if base and key:
            table["image.generate"] = [
                MediaBinding(
                    provider="nvidia-nim",
                    base_url=base,
                    key_spec=key,
                    model=(env.get("MAC_ROUTER_IMAGE_MODEL") or DEFAULT_IMAGE_MODEL).strip(),
                    adapter="nvidia_genai",
                    timeout=_float(env.get("MAC_ROUTER_IMAGE_TIMEOUT"), 180.0),
                )
            ]
    return table
