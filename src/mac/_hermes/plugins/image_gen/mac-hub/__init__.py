"""MAC hub image generation backend (media-01).

Routes text-to-image through the in-mac router's **canonical** operation
endpoint ``POST <gateway>/v1/media/image.generate`` (router_app.mount_media_router)
rather than a provider-specific path. The agent sends ONE provider-agnostic
request ``{prompt, width, height, steps, seed, model}``; the hub resolves the
operation's provider binding(s), applies the per-provider adapter, forwards, and
fails over to the next binding on failure — so the dialect + key live on the hub
and the agent needs no per-modality credential (works on spokes too).

This is the agent-side half of media-01: it generalizes the ``nvidia`` plugin
(which posts the NVIDIA flux/stability dialect to ``/v1/genai`` directly) onto
the unified router seam. ``nvidia`` remains available for a direct/on-prem NIM.

Models are passed straight through as the canonical ``model`` (the hub's adapter
maps it to the provider path); the friendly ids below mirror the ``nvidia``
catalog so ``image_gen.model`` behaves the same.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)

logger = logging.getLogger(__name__)

# Friendly id -> the provider model id the hub's adapter forwards (mirrors the
# `nvidia` plugin so image_gen.model is interchangeable between the two).
_MODELS: Dict[str, Dict[str, Any]] = {
    "flux.1-schnell": {"endpoint": "black-forest-labs/flux.1-schnell", "display": "FLUX.1 [schnell]",
                        "speed": "~5s", "strengths": "Fast few-step distillation — default"},
    "flux.1-dev": {"endpoint": "black-forest-labs/flux.1-dev", "display": "FLUX.1 [dev]",
                   "speed": "~15s", "strengths": "High-quality general text-to-image"},
    "sdxl": {"endpoint": "stabilityai/stable-diffusion-xl", "display": "Stable Diffusion XL",
             "speed": "~10s", "strengths": "Classic SDXL"},
}
DEFAULT_MODEL = "flux.1-schnell"

# FLUX accepts explicit width/height; map the canonical aspect to a size.
_SIZES = {"landscape": (1344, 768), "square": (1024, 1024), "portrait": (768, 1344)}


def _base_url() -> str:
    """The router's /v1 base (the agent's gateway base, same one chat uses).
    The canonical media endpoint is ``<base>/media/<op>``."""
    return (
        os.environ.get("MAC_HERMES_GATEWAY_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or ""
    ).strip().rstrip("/")


def _bearer() -> str:
    return (
        os.environ.get("MAC_HERMES_GATEWAY_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()


def _resolve_model(explicit: Optional[str]) -> str:
    env_override = os.environ.get("MAC_HUB_IMAGE_MODEL")
    for candidate in (env_override, explicit):
        if isinstance(candidate, str) and candidate.strip():
            value = candidate.strip()
            if value in _MODELS:
                return _MODELS[value]["endpoint"]
            return value  # already a provider model id (e.g. black-forest-labs/...)
    return _MODELS[DEFAULT_MODEL]["endpoint"]


class MacHubImageGenProvider(ImageGenProvider):
    """Image gen via the in-mac router's canonical /v1/media/image.generate."""

    @property
    def name(self) -> str:
        return "mac-hub"

    @property
    def display_name(self) -> str:
        return "MAC hub (router /v1/media)"

    def is_available(self) -> bool:
        # Available wherever the agent has a gateway bearer + base (the router
        # holds the provider key) — i.e. wherever chat works, spokes included.
        return bool(_bearer() and _base_url())

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {"id": mid, "display": m["display"], "speed": m["speed"],
             "strengths": m["strengths"], "price": "routed via hub"}
            for mid, m in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "MAC hub (router)",
            "badge": "routed",
            "tag": "image gen via the in-mac router /v1/media (no per-agent key)",
            "env_vars": [],
        }

    def generate(self, prompt: str, aspect_ratio: str = DEFAULT_ASPECT_RATIO, **kwargs: Any) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        if not prompt:
            return error_response(error="Prompt is required and must be a non-empty string",
                                  error_type="invalid_argument", provider="mac-hub", aspect_ratio=aspect)
        base, key = _base_url(), _bearer()
        if not base or not key:
            return error_response(
                error=("No gateway base/bearer (MAC_HERMES_GATEWAY_BASE_URL / "
                       "MAC_HERMES_GATEWAY_API_KEY). Image-gen routes through the in-mac "
                       "router; ensure the agent's gateway env is configured (it is wherever chat works)."),
                error_type="auth_required", provider="mac-hub", aspect_ratio=aspect)

        model = _resolve_model(kwargs.get("model"))
        width, height = _SIZES.get(aspect, _SIZES["square"])
        payload = {"prompt": prompt, "width": width, "height": height, "model": model}
        if kwargs.get("seed") is not None:
            payload["seed"] = kwargs["seed"]
        if kwargs.get("steps") is not None:
            payload["steps"] = kwargs["steps"]
        url = "%s/media/image.generate" % base

        try:
            import requests

            response = requests.post(
                url,
                headers={"Authorization": "Bearer %s" % key, "Accept": "application/json",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=180,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("mac-hub media request failed", exc_info=True)
            return error_response(error="mac-hub media request failed: %s" % exc, error_type="api_error",
                                  provider="mac-hub", model=model, prompt=prompt, aspect_ratio=aspect)

        if response.status_code != 200:
            body = (response.text or "")[:500]
            return error_response(error="router /v1/media HTTP %s: %s" % (response.status_code, body),
                                  error_type="api_error", provider="mac-hub", model=model,
                                  prompt=prompt, aspect_ratio=aspect)
        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            return error_response(error="router /v1/media returned non-JSON: %s" % exc,
                                  error_type="empty_response", provider="mac-hub", model=model,
                                  prompt=prompt, aspect_ratio=aspect)

        artifacts = data.get("artifacts") if isinstance(data, dict) else None
        b64 = artifacts[0].get("base64") if isinstance(artifacts, list) and artifacts and isinstance(artifacts[0], dict) else None
        if not b64:
            return error_response(error="router /v1/media response had no image artifact",
                                  error_type="empty_response", provider="mac-hub", model=model,
                                  prompt=prompt, aspect_ratio=aspect)
        try:
            saved_path = save_b64_image(b64, prefix="machub_%s" % model.replace("/", "_"))
        except Exception as exc:  # noqa: BLE001
            return error_response(error="Could not save image to cache: %s" % exc, error_type="io_error",
                                  provider="mac-hub", model=model, prompt=prompt, aspect_ratio=aspect)

        return success_response(image=str(saved_path), model=model, prompt=prompt,
                                aspect_ratio=aspect, provider="mac-hub",
                                extra={"endpoint": "/v1/media/image.generate"})


def register(ctx) -> None:
    """Plugin entry point — wire MacHubImageGenProvider into the registry."""
    ctx.register_image_gen_provider(MacHubImageGenProvider())
