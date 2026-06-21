"""Tests for the NVIDIA NIM image-generation backend plugin.

The plugin lives in the vendored Hermes snapshot
(``src/mac/_hermes/plugins/image_gen/nvidia/``). We load it by file path
after putting the vendored runtime on sys.path, so the test exercises the
exact module the agents load — without needing the plugin discovery system.
"""

from __future__ import annotations

import base64
import importlib.util
import os
from pathlib import Path

import pytest

from mac import hermes_vendor

pytestmark = pytest.mark.skipif(
    not hermes_vendor.is_vendored(), reason="no vendored Hermes snapshot present"
)


def _load_plugin():
    """Put the vendored runtime on sys.path and load the nvidia plugin module."""
    hermes_vendor.ensure_on_path()
    plugin_path = (
        Path(hermes_vendor.VENDOR_DIR)
        / "plugins"
        / "image_gen"
        / "nvidia"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("mac_test_nvidia_imagegen", plugin_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# 1x1 transparent PNG, base64 — a real decodable image so save_b64_image works.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _clear_gateway_image_env(monkeypatch):
    for key in (
        "MAC_HERMES_GATEWAY_API_KEY",
        "OPENAI_API_KEY",
        "MAC_HERMES_GATEWAY_BASE_URL",
        "OPENAI_BASE_URL",
        "NVIDIA_IMAGE_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_extract_b64_handles_all_nvidia_shapes():
    mod = _load_plugin()
    assert mod._extract_b64({"artifacts": [{"base64": "AAAA"}]}) == "AAAA"
    assert mod._extract_b64({"image": "BBBB"}) == "BBBB"
    assert mod._extract_b64({"b64_json": "CCCC"}) == "CCCC"
    assert mod._extract_b64({"data": [{"b64_json": "DDDD"}]}) == "DDDD"
    assert mod._extract_b64({"images": ["EEEE"]}) == "EEEE"
    # data: URI prefix is stripped
    assert mod._extract_b64({"image": "data:image/png;base64,FFFF"}) == "FFFF"
    # nothing recognizable
    assert mod._extract_b64({"unexpected": 1}) is None
    assert mod._extract_b64("not a dict") is None


def test_build_payload_dialects_and_aspect():
    mod = _load_plugin()
    flux = mod._MODELS["flux.1-dev"]
    body = mod._build_payload(flux, "a cat", "portrait")
    assert body["prompt"] == "a cat"
    assert (body["width"], body["height"]) == mod._FLUX_SIZES["portrait"]
    assert body["steps"] == flux["steps"]
    assert "text_prompts" not in body

    sdxl = mod._MODELS["sdxl"]
    sbody = mod._build_payload(sdxl, "a dog", "landscape")
    assert sbody["text_prompts"][0]["text"] == "a dog"
    assert "prompt" not in sbody


def test_generate_requires_api_key(monkeypatch):
    mod = _load_plugin()
    _clear_gateway_image_env(monkeypatch)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    result = mod.NvidiaImageGenProvider().generate("a fox", "square")
    assert result["success"] is False
    assert result["error_type"] == "auth_required"


def test_generate_success_saves_image(monkeypatch, tmp_path):
    mod = _load_plugin()
    _clear_gateway_image_env(monkeypatch)
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["auth"] = headers.get("Authorization")
        return _FakeResponse(200, {"artifacts": [{"base64": _PNG_B64}]})

    monkeypatch.setattr("requests.post", fake_post)

    provider = mod.NvidiaImageGenProvider()
    result = provider.generate("a serene mountain lake at dawn", "landscape")

    assert result["success"] is True, result
    assert result["provider"] == "nvidia"
    assert result["model"] == "flux.1-dev"
    # The default model's endpoint was hit with a bearer token.
    assert captured["url"].endswith("black-forest-labs/flux.1-dev")
    assert captured["auth"] == "Bearer nvapi-test"
    # A real file landed under the cache dir.
    saved = Path(result["image"])
    assert saved.exists()
    assert saved.read_bytes() == base64.b64decode(_PNG_B64)


def test_generate_honors_explicit_model_kwarg(monkeypatch, tmp_path):
    mod = _load_plugin()
    _clear_gateway_image_env(monkeypatch)
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        return _FakeResponse(200, {"image": _PNG_B64})

    monkeypatch.setattr("requests.post", fake_post)
    # The tool dispatcher passes image_gen.model through as a `model` kwarg.
    result = mod.NvidiaImageGenProvider().generate(
        "a galaxy", "square", model="flux.1-schnell"
    )
    assert result["success"] is True, result
    assert result["model"] == "flux.1-schnell"
    assert captured["url"].endswith("black-forest-labs/flux.1-schnell")


def test_generate_surfaces_http_error_with_hint(monkeypatch, tmp_path):
    mod = _load_plugin()
    _clear_gateway_image_env(monkeypatch)
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(403, payload=None, text="forbidden")

    monkeypatch.setattr("requests.post", fake_post)
    result = mod.NvidiaImageGenProvider().generate("anything", "square")
    assert result["success"] is False
    assert result["error_type"] == "api_error"
    assert "403" in result["error"]
    assert "image-NIM access" in result["error"]


def test_provider_identity_and_models():
    mod = _load_plugin()
    provider = mod.NvidiaImageGenProvider()
    assert provider.name == "nvidia"
    assert provider.default_model() == "flux.1-dev"
    ids = {m["id"] for m in provider.list_models()}
    assert {"flux.1-dev", "flux.1-schnell", "sdxl"} <= ids
