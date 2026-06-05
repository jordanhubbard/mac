"""Catalog of generative models that run locally on a GPU agent, and which a
given agent's reported hardware can actually serve.

This turns "agent has a GPU" into a concrete model list, and ties the models to
media-01 routing: each entry names the media ``op`` it serves and the router
``adapter`` a local OpenAI-compatible server should be routed through. Used to
(a) surface what each agent could run (`mac agent hardware`), and (b) pick a
model + build the ``media_routes`` advertisement when standing a local gen
server up (see ``deploy/local-gen/openai_image_server.py``).

Pure data + filters (stdlib only). VRAM figures are approximate fp16 minimums;
``vram_min_mb=0`` means "runs on CPU / no hard floor". Filtering uses the agent's
reported GPU VRAM, falling back to system memory for unified-memory devices
(Apple Metal, NVIDIA GB10) where ``nvidia-smi`` reports no discrete VRAM.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple


class LocalGenModel(NamedTuple):
    id: str  # short id used in media_routes/model selection
    modality: str  # image | audio | video
    op: str  # media-01 op: image.generate | audio.music | audio.tts | audio.asr | video.generate
    repo: str  # HF repo / weights id the local server loads
    vram_min_mb: int  # approximate fp16 minimum (0 = CPU-able / no floor)
    accelerators: Tuple[str, ...]  # accelerators it runs on: cuda | metal | cpu
    framework: str  # diffusers | transformers
    adapter: str  # media-01 router adapter for the local OpenAI-compatible server
    routable: bool  # True if media-01 can route this op end-to-end today
    notes: str = ""


# Curated, intentionally small. `routable=True` is image.generate (the op the
# router + openai_images adapter serve today via a local diffusers server);
# audio/video are enumerated for capability but need the audio/video transport
# (binary/multipart/async) before they route end-to-end.
LOCAL_GEN_MODELS: Tuple[LocalGenModel, ...] = (
    # ---- image: text-to-image (routable today) ----
    LocalGenModel("sd15", "image", "image.generate", "stable-diffusion-v1-5/stable-diffusion-v1-5",
                  4000, ("cuda", "metal", "cpu"), "diffusers", "openai_images", True,
                  "Stable Diffusion 1.5 — lightweight, runs almost anywhere"),
    LocalGenModel("sdxl-turbo", "image", "image.generate", "stabilityai/sdxl-turbo",
                  7000, ("cuda", "metal"), "diffusers", "openai_images", True,
                  "SDXL-Turbo — 1-4 step, fast; great default"),
    LocalGenModel("sdxl", "image", "image.generate", "stabilityai/stable-diffusion-xl-base-1.0",
                  10000, ("cuda", "metal"), "diffusers", "openai_images", True,
                  "SDXL base 1.0 — higher quality, ~25 steps"),
    LocalGenModel("sd3.5-medium", "image", "image.generate", "stabilityai/stable-diffusion-3.5-medium",
                  10000, ("cuda",), "diffusers", "openai_images", True,
                  "Stable Diffusion 3.5 Medium"),
    LocalGenModel("flux.1-schnell", "image", "image.generate", "black-forest-labs/FLUX.1-schnell",
                  24000, ("cuda",), "diffusers", "openai_images", True,
                  "FLUX.1 [schnell] — high quality few-step; large (fp16 ~24GB)"),
    LocalGenModel("flux.1-dev", "image", "image.generate", "black-forest-labs/FLUX.1-dev",
                  24000, ("cuda",), "diffusers", "openai_images", True,
                  "FLUX.1 [dev] — highest quality; large"),
    # ---- audio (enumerated; needs audio transport to route) ----
    LocalGenModel("musicgen-small", "audio", "audio.music", "facebook/musicgen-small",
                  3000, ("cuda", "metal"), "transformers", "passthrough", False,
                  "MusicGen small — text-to-music"),
    LocalGenModel("bark", "audio", "audio.tts", "suno/bark",
                  4000, ("cuda", "metal"), "transformers", "passthrough", False,
                  "Bark — text-to-speech (binary audio; needs TTS transport)"),
    LocalGenModel("whisper-large-v3", "audio", "audio.asr", "openai/whisper-large-v3",
                  10000, ("cuda",), "transformers", "passthrough", False,
                  "Whisper large v3 — speech-to-text (multipart; needs ASR transport)"),
    LocalGenModel("stable-audio-open", "audio", "audio.generate", "stabilityai/stable-audio-open-1.0",
                  8000, ("cuda",), "diffusers", "passthrough", False,
                  "Stable Audio Open — text-to-audio"),
    # ---- video (enumerated; needs async video transport to route) ----
    LocalGenModel("svd", "video", "video.generate", "stabilityai/stable-video-diffusion-img2vid-xt",
                  15000, ("cuda",), "diffusers", "passthrough", False,
                  "Stable Video Diffusion (image-to-video)"),
    LocalGenModel("animatediff", "video", "video.generate", "guoyww/animatediff-motion-adapter-v1-5-2",
                  12000, ("cuda",), "diffusers", "passthrough", False,
                  "AnimateDiff — text-to-video"),
)

_BY_ID = {m.id: m for m in LOCAL_GEN_MODELS}


def get_model(model_id: str) -> Optional[LocalGenModel]:
    return _BY_ID.get((model_id or "").strip())


def _memory_budget_mb(hardware: Mapping[str, Any]) -> int:
    """The VRAM budget to filter on: discrete GPU VRAM if reported, else system
    memory (unified-memory devices — Apple Metal, NVIDIA GB10 — report no
    discrete VRAM via nvidia-smi but share the system RAM with the GPU)."""
    gpu = hardware.get("gpu") if isinstance(hardware.get("gpu"), Mapping) else {}
    vram = int(gpu.get("vram_mb") or 0)
    if vram:
        return vram
    return int(hardware.get("memory_mb") or 0)


def models_for_hardware(hardware: Optional[Mapping[str, Any]]) -> List[LocalGenModel]:
    """Catalog models an agent's reported hardware can run: accelerator must
    match, and (when the VRAM budget is known) it must clear the model's
    minimum. Unknown budget (0) is permissive — list it, don't hide it."""
    if not isinstance(hardware, Mapping):
        return []
    accel = str(hardware.get("accelerator") or "none").strip().lower()
    if accel not in ("cuda", "rocm", "metal", "cpu"):
        return []
    budget = _memory_budget_mb(hardware)
    out: List[LocalGenModel] = []
    for model in LOCAL_GEN_MODELS:
        if accel not in model.accelerators:
            continue
        if model.vram_min_mb and budget and budget < model.vram_min_mb:
            continue
        out.append(model)
    return out


def media_route_for(model_id: str, base_url: str, *, key_spec: str = "", auth_scheme: str = "Bearer") -> Dict[str, Any]:
    """Build the ``media_routes`` advertisement dict for serving ``model_id`` at
    ``base_url`` (what a GPU agent sets in MAC_AGENT_MEDIA_ROUTES). Raises on an
    unknown model so a typo fails loudly rather than advertising nothing."""
    model = get_model(model_id)
    if model is None:
        raise ValueError("unknown local gen model %r (see LOCAL_GEN_MODELS)" % model_id)
    return {
        "op": model.op,
        "base_url": base_url.rstrip("/"),
        "model": model.id,
        "adapter": model.adapter,
        "key": key_spec,
        "auth_scheme": auth_scheme,
    }
