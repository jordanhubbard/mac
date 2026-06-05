#!/usr/bin/env python3
"""Minimal OpenAI-images-compatible local image-gen server for a GPU agent.

Loads a diffusers text-to-image pipeline on the local accelerator (CUDA / Apple
MPS / CPU) and serves ``POST /v1/images/generations`` →
``{"data":[{"b64_json": "<png base64>"}]}`` — exactly the shape the in-mac
router's ``openai_images`` adapter consumes. So a GPU agent runs this, advertises
a media route (MAC_AGENT_MEDIA_ROUTES, see mac.local_gen_catalog.media_route_for),
and the hub routes ``/v1/media/image.generate`` to its GPU with cloud failover.

stdlib HTTP server; torch/diffusers imported lazily (present only on GPU agents).
Configure via env:
    LOCAL_GEN_MODEL  HF repo or a mac.local_gen_catalog id (default stabilityai/sdxl-turbo)
    LOCAL_GEN_PORT   listen port (default 8189)
    LOCAL_GEN_HOST   bind host (default 0.0.0.0)

Run (on the GPU agent):
    LOCAL_GEN_MODEL=stabilityai/sdxl-turbo ~/gen/venv/bin/python openai_image_server.py
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = os.environ.get("LOCAL_GEN_MODEL", "stabilityai/sdxl-turbo").strip()
PORT = int(os.environ.get("LOCAL_GEN_PORT", "8189"))
HOST = os.environ.get("LOCAL_GEN_HOST", "0.0.0.0")

_pipe = None
_pipe_lock = threading.Lock()
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


def _pipeline():
    global _pipe, _device
    if _pipe is not None:
        return _pipe
    with _pipe_lock:
        if _pipe is not None:
            return _pipe
        import torch
        from diffusers import AutoPipelineForText2Image

        if torch.cuda.is_available():
            _device = "cuda"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            _device = "mps"
        else:
            _device = "cpu"
        dtype = torch.float16 if _device == "cuda" else torch.float32
        repo = _resolve_repo(MODEL)
        print("local-gen: loading %s on %s (%s)" % (repo, _device, dtype), flush=True)
        pipe = AutoPipelineForText2Image.from_pretrained(repo, torch_dtype=dtype)
        _pipe = pipe.to(_device)
        return _pipe


def _generate(prompt: str, steps: int, width: int, height: int, seed):
    import torch

    pipe = _pipeline()
    kwargs = {"prompt": prompt, "num_inference_steps": max(1, steps)}
    # Turbo/distilled models want guidance 0; others a sane default.
    kwargs["guidance_scale"] = 0.0 if "turbo" in MODEL.lower() else 5.0
    if width and height:
        kwargs["width"], kwargs["height"] = width, height
    if seed is not None:
        kwargs["generator"] = torch.Generator(device=_device).manual_seed(int(seed))
    image = pipe(**kwargs).images[0]
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/health", "/healthz"):
            self._send(200, {"ok": True, "model": MODEL, "device": _device})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        if self.path.rstrip("/") not in ("/v1/images/generations", "/images/generations"):
            self._send(404, {"error": {"message": "unsupported path %r" % self.path}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:  # noqa: BLE001
            self._send(400, {"error": {"message": "bad request: %s" % exc}})
            return
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            self._send(400, {"error": {"message": "prompt is required"}})
            return
        default_steps = 2 if "turbo" in MODEL.lower() else 25
        try:
            b64 = _generate(
                prompt,
                int(body.get("steps") or default_steps),
                int(body.get("width") or 0),
                int(body.get("height") or 0),
                body.get("seed"),
            )
            self._send(200, {"data": [{"b64_json": b64}], "model": MODEL})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": {"message": "generation failed: %s" % exc}})

    def log_message(self, *args) -> None:  # noqa: ANN002 - quiet
        return


def main() -> int:
    print("local-gen server: model=%s host=%s port=%d" % (MODEL, HOST, PORT), flush=True)
    try:
        _pipeline()  # warm-load so the first request isn't a cold start
        print("local-gen: pipeline ready on %s" % _device, flush=True)
    except Exception as exc:  # noqa: BLE001
        print("local-gen: WARNING failed to warm-load pipeline: %s" % exc, file=sys.stderr, flush=True)
    ThreadingHTTPServer((HOST, PORT), _Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
