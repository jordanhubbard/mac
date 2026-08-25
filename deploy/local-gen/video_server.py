#!/usr/bin/env python3
"""Minimal local VIDEO-gen server for a GPU agent (media-01 Part B3).

Video renders are slow, so this server is ASYNC: ``POST /v1/video/generate``
starts a render in a background thread and returns ``{"job_id","status"}``;
``GET /v1/video/jobs/{id}`` returns ``{"status", "artifacts":[{base64,content_type}]}``
once done. The hub's ``video_generate`` adapter + ``/v1/media/jobs/{id}`` poll
endpoint (router_app) sit in front of this. Frames are exported to an animated
GIF (Pillow — no ffmpeg dependency).

Configure via env:
    LOCAL_VIDEO_MODEL   HF repo or catalog id (default animatediff text-to-video)
    LOCAL_VIDEO_BASE    SD1.5 base model for AnimateDiff (default emilianJR/epiCRealism)
    LOCAL_GEN_PORT      listen port (default 8191)
    LOCAL_GEN_HOST      bind host (default 0.0.0.0)
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VIDEO_MODEL = os.environ.get(
    "LOCAL_VIDEO_MODEL", "guoyww/animatediff-motion-adapter-v1-5-2"
).strip()
VIDEO_BASE = os.environ.get("LOCAL_VIDEO_BASE", "emilianJR/epiCRealism").strip()
PORT = int(os.environ.get("LOCAL_GEN_PORT", "8191"))
HOST = os.environ.get("LOCAL_GEN_HOST", "0.0.0.0")

_pipe = None
_pipe_lock = threading.Lock()
_device = "cpu"
_jobs: dict = {}
_jobs_lock = threading.Lock()
_seq = 0


def _resolve_repo(model: str) -> str:
    try:
        from mac.local_gen_catalog import get_model

        entry = get_model(model)
        if entry is not None:
            return entry.repo
    except Exception:  # noqa: BLE001
        pass
    return model


def _pipeline():
    global _pipe, _device
    with _pipe_lock:
        if _pipe is not None:
            return _pipe
        import torch
        from diffusers import AnimateDiffPipeline, MotionAdapter

        if torch.cuda.is_available():
            _device, dtype = "cuda", torch.float16
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            _device, dtype = "mps", torch.float32
        else:
            _device, dtype = "cpu", torch.float32
        adapter = MotionAdapter.from_pretrained(_resolve_repo(VIDEO_MODEL), torch_dtype=dtype)
        pipe = AnimateDiffPipeline.from_pretrained(
            VIDEO_BASE, motion_adapter=adapter, torch_dtype=dtype
        )
        _pipe = pipe.to(_device)
        return _pipe


def _frames_to_gif(frames) -> bytes:
    from PIL import Image

    imgs = [f if isinstance(f, Image.Image) else Image.fromarray(f) for f in frames]
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=120, loop=0)
    return buf.getvalue()


def _render(job_id: str, prompt: str, num_frames: int) -> None:
    try:
        out = _pipeline()(prompt=prompt, num_frames=num_frames)
        frames = out.frames[0]
        gif = _frames_to_gif(frames)
        artifact = {"base64": base64.b64encode(gif).decode("ascii"), "content_type": "image/gif"}
        with _jobs_lock:
            _jobs[job_id] = {"status": "completed", "artifacts": [artifact]}
    except Exception as exc:  # noqa: BLE001
        with _jobs_lock:
            _jobs[job_id] = {"status": "failed", "error": {"message": "render failed: %s" % exc}}


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/health", "/healthz"):
            self._json(
                200, {"ok": True, "model": VIDEO_MODEL, "base": VIDEO_BASE, "device": _device}
            )
            return
        if path.startswith("/v1/video/jobs/") or path.startswith("/video/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            with _jobs_lock:
                job = _jobs.get(job_id)
            if job is None:
                self._json(404, {"error": {"message": "unknown job %r" % job_id}})
            else:
                self._json(200, {"job_id": job_id, **job})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        global _seq
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/v1/video/generate", "/video/generate"):
            self._json(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            self._json(400, {"error": {"message": "invalid JSON body"}})
            return
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            self._json(400, {"error": {"message": "prompt is required"}})
            return
        num_frames = int(body.get("num_frames") or 16)
        with _jobs_lock:
            _seq += 1
            job_id = "vjob_%d" % _seq
            _jobs[job_id] = {"status": "running"}
        threading.Thread(target=_render, args=(job_id, prompt, num_frames), daemon=True).start()
        self._json(200, {"job_id": job_id, "status": "running"})

    def log_message(self, *args) -> None:
        return


def main() -> int:
    print(
        "local-video: model=%s base=%s (lazy-load on first request)" % (VIDEO_MODEL, VIDEO_BASE),
        flush=True,
    )
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("local-video: serving on %s:%d" % (HOST, PORT), flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
