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
from typing import Any, Callable, Dict, Iterable, List, Mapping, NamedTuple, Optional, Tuple

logger = logging.getLogger("mac.media_routing")

DEFAULT_IMAGE_MODEL = "black-forest-labs/flux.1-schnell"


class MediaBinding(NamedTuple):
    provider: str  # display id, e.g. "nvidia-nim"
    base_url: str  # upstream genai base, e.g. https://ai.api.nvidia.com/v1/genai
    key_spec: str  # "secret:<name>" (vault) or a literal bearer; resolved at use
    model: str  # provider model id, e.g. black-forest-labs/flux.1-schnell
    adapter: str  # adapter id (see ADAPTERS)
    timeout: float
    auth_scheme: str = "Bearer"  # Authorization scheme; FAL uses "Key", OpenAI/NVIDIA "Bearer"
    rank: int = 9  # accelerator tier for load-balancing (0=cuda/rocm, 1=metal, 9=cpu/cloud)


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


# --- openai_images adapter (synchronous, base64) ----------------------------
# OpenAI-compatible /images/generations: the model is a body field (not the
# path), b64_json response_format gives inline base64. Binding base_url is the
# /v1 root (e.g. https://api.openai.com/v1); auth is Bearer (the default).

_OPENAI_SIZES = {"landscape": "1792x1024", "square": "1024x1024", "portrait": "1024x1792"}


def openai_images_request(op: str, body: Mapping[str, Any], model: str) -> Tuple[str, Dict[str, Any]]:
    width, height = _int(body.get("width"), 0), _int(body.get("height"), 0)
    size = "%dx%d" % (width, height) if width and height else "1024x1024"
    provider_body: Dict[str, Any] = {
        "prompt": str(body.get("prompt") or "").strip(),
        "size": size,
        "n": _int(body.get("n"), 1),
        "response_format": "b64_json",
    }
    if model:
        provider_body["model"] = model
    return "/images/generations", provider_body


def openai_images_response(status: Optional[int], resp: Any) -> Dict[str, Any]:
    if status and 200 <= status < 300:
        b64 = extract_b64(resp)  # handles {"data":[{"b64_json":...}]}
        if b64:
            return {"artifacts": [{"base64": b64}]}
        return {"error": {"message": "no image data in OpenAI-images response",
                          "type": "empty_response"}}
    return resp if isinstance(resp, dict) else {"error": {"message": str(resp)[:500]}}


# --- fal adapter (synchronous fal.run; URL artifacts) -----------------------
# FAL: POST https://fal.run/<model> with `Authorization: Key <FAL_KEY>` (NOT
# Bearer — set the binding's auth_scheme to "Key"). Fast models return inline;
# the result carries image *URLs* (CDN), so the canonical artifact is a `url`
# (the mac-hub agent provider downloads it). No base64 inline.

_FAL_SIZES = {"landscape": "landscape_16_9", "square": "square_hd", "portrait": "portrait_16_9"}


def fal_request(op: str, body: Mapping[str, Any], model: str) -> Tuple[str, Dict[str, Any]]:
    width, height = _int(body.get("width"), 0), _int(body.get("height"), 0)
    provider_body: Dict[str, Any] = {
        "prompt": str(body.get("prompt") or "").strip(),
        "num_images": _int(body.get("n"), 1),
    }
    if width and height:
        provider_body["image_size"] = {"width": width, "height": height}
    if body.get("seed") is not None:
        provider_body["seed"] = _int(body.get("seed"), 0)
    if body.get("steps") is not None:
        provider_body["num_inference_steps"] = _int(body.get("steps"), 0)
    return ("/" + model.strip("/")) if model else "/", provider_body


def _extract_urls(data: Any) -> List[str]:
    """Pull image URLs out of FAL's {"images":[{"url":...}]} (or {"image":{"url"}})."""
    urls: List[str] = []
    if not isinstance(data, dict):
        return urls
    images = data.get("images")
    if isinstance(images, list):
        for item in images:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                urls.append(item["url"])
            elif isinstance(item, str):
                urls.append(item)
    single = data.get("image")
    if isinstance(single, dict) and isinstance(single.get("url"), str):
        urls.append(single["url"])
    return urls


def fal_response(status: Optional[int], resp: Any) -> Dict[str, Any]:
    if status and 200 <= status < 300:
        urls = _extract_urls(resp)
        if urls:
            return {"artifacts": [{"url": u} for u in urls]}
        # some FAL models also return inline base64
        b64 = extract_b64(resp)
        if b64:
            return {"artifacts": [{"base64": b64}]}
        return {"error": {"message": "no image url/data in FAL response", "type": "empty_response"}}
    return resp if isinstance(resp, dict) else {"error": {"message": str(resp)[:500]}}


ADAPTERS: Dict[str, Tuple[RequestAdapter, ResponseAdapter]] = {
    "nvidia_genai": (nvidia_genai_request, nvidia_genai_response),
    "openai_images": (openai_images_request, openai_images_response),
    "fal": (fal_request, fal_response),
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
                            auth_scheme=str(b.get("auth_scheme") or "Bearer"),
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


# --- capability self-registration (agents advertise media routes) -----------
# A GPU agent advertises what it serves in its registration
# ``resources["media_routes"]`` (a list of route dicts); the hub composes the
# routing table from LIVE agents instead of operator hand-config. So plugging in
# a GPU agent that announces e.g. image.generate makes the fleet start using it
# with zero per-caller knowledge — and it's dropped automatically when the agent
# is offline. Route dict shape (all but op/base_url optional)::
#
#     {"op": "image.generate", "base_url": "http://bullwinkle:8189",
#      "model": "sdxl-turbo", "adapter": "openai_images",
#      "key": "secret:...", "auth_scheme": "Bearer", "priority": 0, "timeout": 180}

_OFFLINE_STATUSES = {"offline", "draining", "drained", "retired", "decommissioned"}
_UNHEALTHY = {"unhealthy", "dead", "degraded"}


def _agent_is_routable(agent: Mapping[str, Any]) -> bool:
    """Only route to agents that are up — a down GPU agent must not be a binding
    (failover also covers transient errors, but skipping offline agents avoids a
    guaranteed-failing first hop on every request)."""
    status = str(agent.get("status") or "").strip().lower()
    health = str(agent.get("health_status") or agent.get("health") or "").strip().lower()
    if status in _OFFLINE_STATUSES:
        return False
    if health in _UNHEALTHY:
        return False
    return True


# Gen-routing preference derived from REPORTED hardware: lower rank = preferred.
# The hub uses resources.hardware.accelerator to pick the best gen agent
# automatically — CUDA/ROCm outrank Metal outrank CPU/unknown — so no operator
# has to know which agent has the beefier silicon. (Hardware presence is not a
# running gen server; this orders/qualifies advertised endpoints, it doesn't
# invent one.)
_ACCEL_RANK = {"cuda": 0, "rocm": 0, "metal": 1}
_GEN_CAPABLE_ACCELERATORS = frozenset({"cuda", "rocm", "metal"})


def hardware_gen_rank(accelerator: Optional[str]) -> int:
    """Lower = preferred for gen routing. CUDA/ROCm best, Metal next, CPU/unknown last."""
    return _ACCEL_RANK.get((accelerator or "").strip().lower(), 9)


def is_gen_capable(hardware: Optional[Mapping[str, Any]]) -> bool:
    """Whether an agent's reported hardware can plausibly host media generation
    (a usable accelerator). Candidacy only — hardware presence is NOT a running
    gen server, so this never by itself produces a routable binding."""
    if not isinstance(hardware, Mapping):
        return False
    return str(hardware.get("accelerator") or "none").strip().lower() in _GEN_CAPABLE_ACCELERATORS


def _agent_accelerator(agent: Mapping[str, Any]) -> str:
    resources = agent.get("resources")
    hardware = resources.get("hardware") if isinstance(resources, Mapping) else None
    if isinstance(hardware, Mapping):
        return str(hardware.get("accelerator") or "none").strip().lower()
    return "unknown"


def _agent_vram_budget(agent: Mapping[str, Any]) -> int:
    """VRAM budget for tie-breaking same-tier GPUs: discrete VRAM if reported,
    else system RAM (unified-memory devices report no discrete VRAM)."""
    resources = agent.get("resources")
    hardware = resources.get("hardware") if isinstance(resources, Mapping) else {}
    if not isinstance(hardware, Mapping):
        return 0
    gpu = hardware.get("gpu") if isinstance(hardware.get("gpu"), Mapping) else {}
    return int(gpu.get("vram_mb") or 0) or int(hardware.get("memory_mb") or 0)


def media_bindings_from_agents(agents: Iterable[Mapping[str, Any]]) -> Dict[str, List[MediaBinding]]:
    """Compose ``{op: [MediaBinding]}`` from live agent registrations.

    Each routable agent's ``resources["media_routes"]`` contributes bindings,
    ordered by **reported hardware**: accelerator tier first (CUDA/ROCm → Metal →
    CPU/unknown), then **VRAM** (bigger GPU first), then the route's declared
    ``priority``. Each binding carries its accelerator ``rank`` so the dispatcher
    can load-balance within a tier. Offline/unhealthy agents are skipped.
    """
    staged: Dict[str, List[Tuple[int, int, int, MediaBinding]]] = {}
    for agent in agents:
        if not isinstance(agent, Mapping) or not _agent_is_routable(agent):
            continue
        resources = agent.get("resources")
        routes = resources.get("media_routes") if isinstance(resources, Mapping) else None
        if not isinstance(routes, list):
            continue
        accel_rank = hardware_gen_rank(_agent_accelerator(agent))
        vram = _agent_vram_budget(agent)
        for route in routes:
            if not isinstance(route, Mapping):
                continue
            op = str(route.get("op") or "").strip()
            base = str(route.get("base_url") or "").strip().rstrip("/")
            if not op or not base:
                continue
            binding = MediaBinding(
                provider=str(route.get("provider") or agent.get("name") or agent.get("id") or "agent"),
                base_url=base,
                key_spec=str(route.get("key") or ""),
                model=str(route.get("model") or ""),
                adapter=str(route.get("adapter") or "passthrough"),
                timeout=_float(route.get("timeout"), 180.0),
                auth_scheme=str(route.get("auth_scheme") or "Bearer"),
                rank=accel_rank,
            )
            staged.setdefault(op, []).append((accel_rank, -vram, _int(route.get("priority"), 0), binding))
    # rank asc, VRAM desc (note -vram), priority asc
    return {
        op: [b for _, _, _, b in sorted(items, key=lambda r: (r[0], r[1], r[2]))]
        for op, items in staged.items()
    }


def dispatch_order(bindings: List[MediaBinding], inflight: Optional[Mapping[str, int]] = None) -> List[MediaBinding]:
    """Order bindings for a single request with **least-loaded load balancing**
    within the top accelerator tier.

    The leading bindings sharing the best (lowest) ``rank`` are a pool of
    equivalent GPUs; among them the one with the fewest in-flight requests is
    tried first (ties keep the VRAM/priority order from the resolver), so
    concurrent requests spread across same-tier GPUs instead of hammering one.
    Lower tiers (and the static cloud fallback) keep their order and follow as
    failover. ``inflight`` maps ``base_url`` → current in-flight count.
    """
    if not bindings:
        return []
    inflight = inflight or {}
    top_rank = min(b.rank for b in bindings)
    top = [b for b in bindings if b.rank == top_rank]
    rest = [b for b in bindings if b.rank != top_rank]
    # stable sort by in-flight → preserves resolver order (vram/priority) on ties
    top_balanced = sorted(top, key=lambda b: inflight.get(b.base_url, 0))
    return top_balanced + rest


def gen_capable_agents(agents: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Derive, from reported hardware, which agents COULD host media generation
    (a usable accelerator) — independent of whether they currently advertise a
    gen endpoint. For visibility (`mac agent hardware`) and picking where to
    stand a gen server up. Sorted best-accelerator-first."""
    out: List[Dict[str, Any]] = []
    for agent in agents:
        if not isinstance(agent, Mapping):
            continue
        resources = agent.get("resources")
        hardware = resources.get("hardware") if isinstance(resources, Mapping) else None
        if not is_gen_capable(hardware):
            continue
        gpu = hardware.get("gpu") if isinstance(hardware.get("gpu"), Mapping) else {}
        out.append({
            "agent": agent.get("name") or agent.get("id"),
            "accelerator": hardware.get("accelerator"),
            "gpu": gpu.get("name"),
            "rank": hardware_gen_rank(hardware.get("accelerator")),
            "serving": bool(isinstance(resources, Mapping) and resources.get("media_routes")),
        })
    return sorted(out, key=lambda a: a["rank"])


def compose_media_table(
    static_table: Mapping[str, List[MediaBinding]],
    agent_table: Optional[Mapping[str, List[MediaBinding]]],
    op: str,
) -> List[MediaBinding]:
    """Bindings for ``op``: live agent-advertised first (prefer on-prem GPU),
    then the operator's static/config bindings (cloud fallback). Either side may
    be empty."""
    bindings: List[MediaBinding] = []
    if agent_table:
        bindings.extend(agent_table.get(op, []))
    bindings.extend(static_table.get(op, []))
    return bindings
