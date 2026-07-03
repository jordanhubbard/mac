"""NVIDIA NIM image generation backend.

Exposes NVIDIA-hosted text-to-image NIMs (``ai.api.nvidia.com/v1/genai``) as an
:class:`ImageGenProvider`. Uses the ``NVIDIA_API_KEY`` the fleet already plumbs
for chat routing — no new external secret. Output is base64 → saved under
``$HERMES_HOME/cache/images/``.

Models (each a virtual id so the ``hermes tools`` picker / ``image_gen.model``
config key behave like any other multi-model backend):

    flux.1-dev      ~15s   high-quality general text-to-image (default)
    flux.1-schnell  ~5s    fast, few-step distillation
    sdxl            ~10s   classic Stable Diffusion XL

NVIDIA's genai image endpoints are model-specific and use two request
"dialects": the FLUX models take a flat ``{"prompt", "width", "height",
"steps", "cfg_scale"}`` body, while the Stability (SDXL) models take a
``{"text_prompts": [...]}`` body. Both return base64 in one of a few response
shapes; :func:`_extract_b64` normalizes them.

Selection precedence (first hit wins):

1. ``NVIDIA_IMAGE_MODEL`` env var (escape hatch for scripts / tests)
2. ``image_gen.nvidia.model`` in ``config.yaml``
3. ``image_gen.model`` in ``config.yaml`` (when it's one of our ids)
4. :data:`DEFAULT_MODEL` — ``flux.1-dev``
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Endpoint / catalog
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://ai.api.nvidia.com/v1/genai"


def _base_url() -> str:
    """Resolve the genai image base URL.

    Default: route through the **in-mac router** like chat does. The agent's
    gateway base is ``<hub>/v1`` and the router exposes the image proxy at
    ``/v1/genai`` (``MAC_ROUTER_IMAGE_*``), so the hub's escrowed image key
    (``secret:nvidia-image`` — distinct from the chat key) is used and spokes
    route through the hub. ``NVIDIA_IMAGE_BASE_URL`` overrides (on-prem NIM /
    direct host); falls back to NVIDIA's public host only if no gateway is set.
    """
    raw = (os.environ.get("NVIDIA_IMAGE_BASE_URL") or "").strip()
    if raw:
        return raw.rstrip("/")
    gw = (
        os.environ.get("MAC_HERMES_GATEWAY_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or ""
    ).strip().rstrip("/")
    if gw:
        return gw + "/genai"  # <hub>/v1 -> <hub>/v1/genai (the router image proxy)
    return DEFAULT_BASE_URL


def _bearer() -> str:
    """The bearer to present. Default to the agent's hub/gateway token (the same
    one chat uses) so requests go through the router, which swaps in the vault
    image key. Falls back to a raw NVIDIA key for the direct-host setup."""
    return (
        os.environ.get("MAC_HERMES_GATEWAY_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("NVIDIA_API_KEY")
        or ""
    ).strip()


# ``endpoint`` is the model path appended to the base URL. ``dialect`` selects
# the request-body builder. ``steps`` / ``cfg_scale`` are model-tuned defaults.
_MODELS: Dict[str, Dict[str, Any]] = {
    "flux.1-dev": {
        "endpoint": "black-forest-labs/flux.1-dev",
        "dialect": "flux",
        "display": "FLUX.1 [dev]",
        "speed": "~15s",
        "strengths": "High-quality general text-to-image — default",
        "steps": 50,
        "cfg_scale": 3.5,
    },
    "flux.1-schnell": {
        "endpoint": "black-forest-labs/flux.1-schnell",
        "dialect": "flux",
        "display": "FLUX.1 [schnell]",
        "speed": "~5s",
        "strengths": "Fast few-step distillation",
        "steps": 4,
        "cfg_scale": 0.0,
    },
    "sdxl": {
        "endpoint": "stabilityai/stable-diffusion-xl",
        "dialect": "stability",
        "display": "Stable Diffusion XL",
        "speed": "~10s",
        "strengths": "Classic SDXL",
        "steps": 25,
        "cfg_scale": 5.0,
    },
}

DEFAULT_MODEL = "flux.1-dev"

# FLUX accepts explicit width/height (multiples of 64). SDXL is fixed 1024².
_FLUX_SIZES: Dict[str, Tuple[int, int]] = {
    "landscape": (1344, 768),
    "square": (1024, 1024),
    "portrait": (768, 1344),
}


def _load_image_gen_config() -> Dict[str, Any]:
    """Read the ``image_gen`` section from config.yaml ({} on any failure)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


def _resolve_model(explicit: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """Decide which model id to use and return ``(model_id, meta)``.

    ``explicit`` is the model the tool dispatcher passed through from
    ``image_gen.model`` config; it wins over config re-reads but not over the
    ``NVIDIA_IMAGE_MODEL`` env escape hatch.
    """
    env_override = os.environ.get("NVIDIA_IMAGE_MODEL")
    if env_override and env_override in _MODELS:
        return env_override, _MODELS[env_override]

    if isinstance(explicit, str) and explicit in _MODELS:
        return explicit, _MODELS[explicit]

    cfg = _load_image_gen_config()
    nvidia_cfg = cfg.get("nvidia") if isinstance(cfg.get("nvidia"), dict) else {}
    candidate: Optional[str] = None
    if isinstance(nvidia_cfg, dict):
        value = nvidia_cfg.get("model")
        if isinstance(value, str) and value in _MODELS:
            candidate = value
    if candidate is None:
        top = cfg.get("model")
        if isinstance(top, str) and top in _MODELS:
            candidate = top

    if candidate is not None:
        return candidate, _MODELS[candidate]
    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


def _build_payload(meta: Dict[str, Any], prompt: str, aspect: str) -> Dict[str, Any]:
    """Build the request body for the model's dialect."""
    if meta["dialect"] == "stability":
        # Stability (SDXL) dialect — text_prompts array, fixed-ish geometry.
        return {
            "text_prompts": [{"text": prompt, "weight": 1.0}],
            "cfg_scale": meta["cfg_scale"],
            "sampler": "K_DPM_2_ANCESTRAL",
            "seed": 0,
            "steps": meta["steps"],
        }
    # FLUX dialect — flat body with explicit width/height.
    width, height = _FLUX_SIZES.get(aspect, _FLUX_SIZES["square"])
    return {
        "prompt": prompt,
        "mode": "base",
        "cfg_scale": meta["cfg_scale"],
        "width": width,
        "height": height,
        "seed": 0,
        "steps": meta["steps"],
    }


def _strip_data_uri(value: str) -> str:
    """Strip a leading ``data:image/...;base64,`` prefix if present."""
    if isinstance(value, str) and value.startswith("data:"):
        comma = value.find(",")
        if comma != -1:
            return value[comma + 1 :]
    return value


def _extract_b64(data: Any) -> Optional[str]:
    """Pull base64 image bytes out of the several NVIDIA genai response shapes.

    Handles: ``{"artifacts":[{"base64": ...}]}`` (Stability/SDXL and some FLUX),
    ``{"image": ...}`` / ``{"b64_json": ...}`` (FLUX flat), and the
    OpenAI-compatible ``{"data":[{"b64_json": ...}]}`` / ``{"images":[...]}``.
    """
    if not isinstance(data, dict):
        return None
    artifacts = data.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        first = artifacts[0]
        if isinstance(first, dict):
            b64 = first.get("base64") or first.get("b64_json")
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


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class NvidiaImageGenProvider(ImageGenProvider):
    """NVIDIA NIM ``genai`` image backend — FLUX.1 + SDXL."""

    @property
    def name(self) -> str:
        return "nvidia"

    @property
    def display_name(self) -> str:
        return "NVIDIA NIM"

    def is_available(self) -> bool:
        # Available whenever the agent has a hub/gateway bearer (it routes image-gen
        # through the in-mac router, which holds the image key) — so it works on
        # spokes too, where raw provider keys are intentionally absent.
        return bool(_bearer())

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": "NVIDIA API credits",
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "NVIDIA NIM",
            "badge": "api-key",
            "tag": "FLUX.1 / SDXL via ai.api.nvidia.com",
            "env_vars": [
                {
                    "key": "NVIDIA_API_KEY",
                    "prompt": "NVIDIA API key (build.nvidia.com)",
                    "url": "https://build.nvidia.com/",
                },
            ],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider="nvidia",
                aspect_ratio=aspect,
            )

        api_key = _bearer()
        if not api_key:
            return error_response(
                error=(
                    "No gateway bearer available (MAC_HERMES_GATEWAY_API_KEY / "
                    "OPENAI_API_KEY). Image-gen routes through the in-mac router, "
                    "which holds the image key — ensure the agent's gateway env "
                    "is configured (it is, wherever chat works)."
                ),
                error_type="auth_required",
                provider="nvidia",
                aspect_ratio=aspect,
            )

        model_id, meta = _resolve_model(kwargs.get("model"))
        url = f"{_base_url()}/{meta['endpoint']}"
        payload = _build_payload(meta, prompt, aspect)

        try:
            import requests

            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=120,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("NVIDIA image request failed", exc_info=True)
            return error_response(
                error=f"NVIDIA image request failed: {exc}",
                error_type="api_error",
                provider="nvidia",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if response.status_code != 200:
            body = (response.text or "")[:500]
            hint = ""
            if response.status_code in (401, 403):
                hint = (
                    " — the NVIDIA_API_KEY may lack image-NIM access; enable "
                    "the model at build.nvidia.com."
                )
            elif response.status_code == 404:
                hint = f" — model endpoint '{meta['endpoint']}' not found/enabled for this key."
            return error_response(
                error=f"NVIDIA genai HTTP {response.status_code}: {body}{hint}",
                error_type="api_error",
                provider="nvidia",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            return error_response(
                error=f"NVIDIA genai returned non-JSON response: {exc}",
                error_type="empty_response",
                provider="nvidia",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        b64 = _extract_b64(data)
        if not b64:
            return error_response(
                error="NVIDIA genai response contained no recognizable image data",
                error_type="empty_response",
                provider="nvidia",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            saved_path = save_b64_image(b64, prefix=f"nvidia_{model_id}")
        except Exception as exc:  # noqa: BLE001
            return error_response(
                error=f"Could not save image to cache: {exc}",
                error_type="io_error",
                provider="nvidia",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        return success_response(
            image=str(saved_path),
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider="nvidia",
            extra={"endpoint": meta["endpoint"], "steps": meta["steps"]},
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entry point — wire ``NvidiaImageGenProvider`` into the registry."""
    ctx.register_image_gen_provider(NvidiaImageGenProvider())
