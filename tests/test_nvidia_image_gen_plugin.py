"""The NVIDIA image-gen backend must route through the in-mac router (like chat)
so it uses the hub's escrowed image key and works on spokes — not hit
ai.api.nvidia.com directly with the (blanked-on-spokes, image-401) chat key."""
from __future__ import annotations

import sys
from pathlib import Path

HERMES = Path(__file__).resolve().parents[1] / "src" / "mac" / "_hermes"
if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))


def test_routes_through_router_with_gateway_bearer(monkeypatch):
    from plugins.image_gen.nvidia import _base_url, _bearer

    monkeypatch.delenv("NVIDIA_IMAGE_BASE_URL", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)  # blanked on spokes
    monkeypatch.setenv("MAC_HERMES_GATEWAY_BASE_URL", "http://127.0.0.1:18789/v1")
    monkeypatch.setenv("MAC_HERMES_GATEWAY_API_KEY", "hub-token")

    # base URL is the router's image proxy (gateway base + /genai); bearer is the
    # hub token — so the hub swaps in the vault image key.
    assert _base_url() == "http://127.0.0.1:18789/v1/genai"
    assert _bearer() == "hub-token"


def test_available_on_spoke_without_provider_key(monkeypatch):
    from plugins.image_gen.nvidia import ImageGenProvider  # type: ignore[attr-defined]

    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setenv("MAC_HERMES_GATEWAY_API_KEY", "hub-token")
    # provider is discoverable here as a class; instantiate the concrete one
    import plugins.image_gen.nvidia as nv

    provider = next(
        obj() for obj in vars(nv).values()
        if isinstance(obj, type) and getattr(obj, "name", None) is not None
        and obj.__module__ == nv.__name__
    )
    assert provider.is_available() is True


def test_direct_host_override_uses_nvidia_key(monkeypatch):
    from plugins.image_gen.nvidia import _base_url, _bearer

    for k in ("MAC_HERMES_GATEWAY_BASE_URL", "OPENAI_BASE_URL", "MAC_HERMES_GATEWAY_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NVIDIA_IMAGE_BASE_URL", "https://nim.local/v1/genai")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-direct")
    assert _base_url() == "https://nim.local/v1/genai"
    assert _bearer() == "nvapi-direct"
