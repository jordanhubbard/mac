"""Catalog of locally-runnable gen models + hardware filtering + media wiring."""
from __future__ import annotations

import pytest

from mac.local_gen_catalog import (
    LOCAL_GEN_MODELS,
    advertised_media_routes,
    get_model,
    media_route_for,
    models_for_hardware,
)


def test_catalog_entries_well_formed():
    for m in LOCAL_GEN_MODELS:
        assert m.id and m.repo and m.op and m.modality in ("image", "audio", "video")
        assert m.accelerators and all(a in ("cuda", "metal", "cpu") for a in m.accelerators)
        # routable ops + their adapters (image via openai_images; audio via the
        # binary audio adapters — media-01 Part B1).
        _ROUTABLE_ADAPTERS = {
            "image.generate": {"openai_images"},
            "audio.tts": {"openai_audio_speech"},
            "audio.music": {"audio_music"},
            "audio.asr": {"openai_audio_transcription"},
            "video.generate": {"video_generate"},
        }
        if m.routable:
            assert m.op in _ROUTABLE_ADAPTERS, "unexpected routable op %s" % m.op
            assert m.adapter in _ROUTABLE_ADAPTERS[m.op]


def test_cuda_big_vram_runs_flux_and_sdxl():
    hw = {"accelerator": "cuda", "gpu": {"vram_mb": 49000}}
    ids = {m.id for m in models_for_hardware(hw)}
    assert {"sd15", "sdxl-turbo", "sdxl", "flux.1-schnell"} <= ids


def test_metal_excludes_cuda_only_models():
    hw = {"accelerator": "metal", "memory_mb": 64000}  # M-series unified mem
    ids = {m.id for m in models_for_hardware(hw)}
    assert "sdxl-turbo" in ids and "sd15" in ids
    assert "flux.1-schnell" not in ids  # flux is cuda-only in the catalog


def test_unified_memory_uses_system_ram_when_vram_unknown():
    # GB10: nvidia-smi reports no discrete VRAM (vram_mb 0) but 122GB unified.
    hw = {"accelerator": "cuda", "gpu": {"name": "NVIDIA GB10", "vram_mb": 0}, "memory_mb": 122000}
    ids = {m.id for m in models_for_hardware(hw)}
    assert "flux.1-schnell" in ids  # big model allowed via unified-memory budget


def test_small_vram_excludes_large_models():
    hw = {"accelerator": "cuda", "gpu": {"vram_mb": 6000}}
    ids = {m.id for m in models_for_hardware(hw)}
    assert "sd15" in ids and "flux.1-schnell" not in ids and "sdxl" not in ids


def test_no_accelerator_runs_nothing_gpu():
    assert models_for_hardware({"accelerator": "none", "memory_mb": 8000}) == []
    assert models_for_hardware(None) == []


def test_media_route_for_builds_advertisement():
    route = media_route_for("sdxl-turbo", "http://natasha:8189/v1/")
    assert route == {
        "op": "image.generate", "base_url": "http://natasha:8189/v1",
        "model": "sdxl-turbo", "adapter": "openai_images", "key": "", "auth_scheme": "Bearer",
    }


def test_media_route_for_unknown_model_raises():
    with pytest.raises(ValueError):
        media_route_for("not-a-real-model", "http://x")


def test_get_model():
    assert get_model("sdxl-turbo").repo == "stabilityai/sdxl-turbo"
    assert get_model("nope") is None


# --- durable self-advertisement (catalog-driven, GPU-gated) -----------------

def test_advertised_routes_gpu_agent():
    routes = advertised_media_routes("sdxl-turbo", "http://natasha:8189/v1",
                                     {"accelerator": "cuda", "gpu": {"vram_mb": 0}, "memory_mb": 122000})
    assert routes == [{"op": "image.generate", "base_url": "http://natasha:8189/v1",
                       "model": "sdxl-turbo", "adapter": "openai_images", "key": "", "auth_scheme": "Bearer"}]


def test_advertised_routes_cpu_agent_is_empty():
    # GPU-gating: a CPU agent with MAC_AGENT_GEN_MODEL set advertises nothing.
    assert advertised_media_routes("sdxl-turbo", "http://x:8189/v1", {"accelerator": "none", "memory_mb": 8000}) == []


def test_advertised_routes_requires_model_base_and_runnable():
    hw = {"accelerator": "cuda", "gpu": {"vram_mb": 6000}}  # small VRAM
    assert advertised_media_routes("", "http://x/v1", hw) == []         # no model
    assert advertised_media_routes("sdxl-turbo", "", hw) == []          # no base_url
    assert advertised_media_routes("nope", "http://x/v1", hw) == []     # unknown model
    assert advertised_media_routes("flux.1-schnell", "http://x/v1", hw) == []  # 24GB model, 6GB GPU
