#!/usr/bin/env python3
"""Minimal local AUDIO-gen server for a GPU agent (media-01 Part B1).

Serves text-to-speech (Bark) at ``POST /v1/audio/speech`` and text-to-music
(MusicGen) at ``POST /v1/audio/music``, each returning a **binary WAV** body —
exactly what the in-mac router's ``openai_audio_speech`` / ``audio_music``
adapters consume (the hub's binary-aware forwarder base64-wraps the body into a
canonical ``{"artifacts":[{"base64"}]}``). Mirrors the shape of
``openai_image_server.py``: stdlib HTTP, torch/transformers imported lazily
(present only on GPU agents), pipelines warm-loaded at startup.

Configure via env:
    LOCAL_AUDIO_TTS_MODEL    HF repo or catalog id (default suno/bark)
    LOCAL_AUDIO_MUSIC_MODEL  HF repo or catalog id (default facebook/musicgen-small)
    LOCAL_GEN_PORT           listen port (default 8190)
    LOCAL_GEN_HOST           bind host (default 0.0.0.0)

Run (on the GPU agent):
    LOCAL_AUDIO_TTS_MODEL=suno/bark ~/gen/venv/bin/python audio_server.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TTS_MODEL = os.environ.get("LOCAL_AUDIO_TTS_MODEL", "suno/bark").strip()
MUSIC_MODEL = os.environ.get("LOCAL_AUDIO_MUSIC_MODEL", "facebook/musicgen-small").strip()
PORT = int(os.environ.get("LOCAL_GEN_PORT", "8190"))
HOST = os.environ.get("LOCAL_GEN_HOST", "0.0.0.0")

_pipes: dict = {}
_pipes_lock = threading.Lock()
_device = "cpu"


def _resolve_repo(model: str) -> str:
    """Accept either an HF repo id or a mac.local_gen_catalog short id."""
    try:
        from mac.local_gen_catalog import get_model

        entry = get_model(model)
        if entry is not None:
            return entry.repo
    except Exception:  # noqa: BLE001 - catalog is optional on the agent
        pass
    return model


def _pick_device():
    global _device
    import torch

    if torch.cuda.is_available():
        _device = "cuda"
        return 0
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        _device = "mps"
        return "mps"
    _device = "cpu"
    return -1


def _pipeline(kind: str):
    """Lazily build + cache the transformers pipeline for ``kind`` in {tts,music}."""
    with _pipes_lock:
        if kind in _pipes:
            return _pipes[kind]
        from transformers import pipeline

        device = _pick_device()
        if kind == "tts":
            pipe = pipeline("text-to-speech", model=_resolve_repo(TTS_MODEL), device=device)
        else:
            pipe = pipeline("text-to-audio", model=_resolve_repo(MUSIC_MODEL), device=device)
        _pipes[kind] = pipe
        return pipe


def _to_wav(audio, sampling_rate: int) -> bytes:
    """float/np audio (mono or (channels, samples)) -> 16-bit PCM WAV bytes."""
    import numpy as np

    arr = np.asarray(audio, dtype="float32")
    if arr.ndim > 1:
        arr = arr.reshape(arr.shape[-1]) if arr.shape[0] == 1 else arr.mean(axis=0)
    peak = float(np.max(np.abs(arr))) if arr.size else 0.0
    if peak > 1.0:
        arr = arr / peak
    pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(sampling_rate or 24000))
        wav.writeframes(pcm.tobytes())
    return buf.getvalue()


def _generate(kind: str, text: str) -> bytes:
    out = _pipeline(kind)(text)
    if isinstance(out, list):
        out = out[0]
    audio = out["audio"] if isinstance(out, dict) else out
    sr = int(out.get("sampling_rate", 24000)) if isinstance(out, dict) else 24000
    return _to_wav(audio, sr)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/health", "/healthz"):
            self._json(200, {"ok": True, "tts": TTS_MODEL, "music": MUSIC_MODEL, "device": _device})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            self._json(400, {"error": {"message": "invalid JSON body"}})
            return
        if path in ("/v1/audio/speech", "/audio/speech"):
            kind, text = "tts", str(body.get("input") or body.get("prompt") or "").strip()
        elif path in ("/v1/audio/music", "/audio/music"):
            kind, text = "music", str(body.get("prompt") or body.get("input") or "").strip()
        else:
            self._json(404, {"error": {"message": "not found"}})
            return
        if not text:
            self._json(400, {"error": {"message": "input/prompt is required"}})
            return
        try:
            wav = _generate(kind, text)
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": {"message": "generation failed: %s" % exc}})
            return
        self._send(200, wav, "audio/wav")

    def log_message(self, *args) -> None:  # quiet default access logging
        return


def main() -> int:
    print("local-audio: warm-loading TTS=%s music=%s" % (TTS_MODEL, MUSIC_MODEL), flush=True)
    try:
        _pipeline("tts")  # warm-load TTS so the first request isn't a cold start
    except Exception as exc:  # noqa: BLE001
        print("local-audio: WARNING failed to warm-load TTS: %s" % exc, flush=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("local-audio: serving on %s:%d (device=%s)" % (HOST, PORT, _device), flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
