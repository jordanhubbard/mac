"""media-01: operation-keyed media routing table, adapters, and the canonical
POST /v1/media/{op} endpoint with priority failover."""
from __future__ import annotations

import io
import urllib.error

from mac.media_routing import (
    DEFAULT_IMAGE_MODEL,
    build_media_table,
    extract_b64,
    nvidia_genai_request,
    nvidia_genai_response,
)


# --- table ------------------------------------------------------------------

def test_empty_env_yields_empty_table():
    assert build_media_table({}) == {}


def test_back_compat_synthesizes_image_generate_from_flat_vars():
    table = build_media_table(
        {
            "MAC_ROUTER_IMAGE_UPSTREAM": "https://ai.api.nvidia.com/v1/genai/",
            "MAC_ROUTER_IMAGE_KEY": "secret:nvidia-image",
        }
    )
    assert list(table) == ["image.generate"]
    (binding,) = table["image.generate"]
    assert binding.base_url == "https://ai.api.nvidia.com/v1/genai"  # trailing slash stripped
    assert binding.key_spec == "secret:nvidia-image"
    assert binding.adapter == "nvidia_genai"
    assert binding.model == DEFAULT_IMAGE_MODEL


def test_explicit_json_table_orders_by_priority_and_wins():
    env = {
        "MAC_ROUTER_MEDIA_JSON": (
            '{"image.generate": ['
            '{"provider":"fal","base_url":"https://fal.run","model":"m","key":"secret:fal","adapter":"fal","priority":2},'
            '{"provider":"nvidia-nim","base_url":"https://ai.api.nvidia.com/v1/genai","model":"black-forest-labs/flux.1-schnell","key":"secret:nvidia-image","adapter":"nvidia_genai","priority":0}]}'
        ),
        # flat vars present but JSON already covers image.generate -> JSON wins
        "MAC_ROUTER_IMAGE_UPSTREAM": "https://ignored",
        "MAC_ROUTER_IMAGE_KEY": "secret:ignored",
    }
    table = build_media_table(env)
    providers = [b.provider for b in table["image.generate"]]
    assert providers == ["nvidia-nim", "fal"]  # priority 0 before 2


def test_invalid_json_is_ignored_and_falls_back():
    table = build_media_table(
        {"MAC_ROUTER_MEDIA_JSON": "{not json",
         "MAC_ROUTER_IMAGE_UPSTREAM": "https://u", "MAC_ROUTER_IMAGE_KEY": "k"}
    )
    assert "image.generate" in table  # fell back to the flat-var binding


# --- nvidia_genai adapter ---------------------------------------------------

def test_nvidia_genai_request_flux_schnell_dialect():
    path, body = nvidia_genai_request("image.generate", {"prompt": "a duck"}, "black-forest-labs/flux.1-schnell")
    assert path == "/black-forest-labs/flux.1-schnell"
    assert body["prompt"] == "a duck"
    assert body["steps"] == 4 and body["cfg_scale"] == 0.0  # schnell defaults
    assert "text_prompts" not in body  # flat (flux) dialect


def test_nvidia_genai_request_stability_dialect():
    _, body = nvidia_genai_request("image.generate", {"prompt": "x"}, "stabilityai/stable-diffusion-xl")
    assert body["text_prompts"] == [{"text": "x", "weight": 1.0}]  # stability dialect


def test_nvidia_genai_response_normalizes_to_artifacts():
    assert nvidia_genai_response(200, {"image": "B64"}) == {"artifacts": [{"base64": "B64"}]}
    assert nvidia_genai_response(200, {"artifacts": [{"base64": "Z"}]}) == {"artifacts": [{"base64": "Z"}]}
    err = nvidia_genai_response(401, {"detail": "Authentication failed"})
    assert err == {"detail": "Authentication failed"}  # error passed through


def test_extract_b64_handles_openai_images_shape():
    assert extract_b64({"data": [{"b64_json": "data:image/png;base64,ABC"}]}) == "ABC"


# --- mounted endpoint -------------------------------------------------------

def _fake_urlopen_factory(captured, by_url):
    """by_url: dict substring -> (status, raw_bytes). A 4xx/5xx raises HTTPError
    so urllib_forwarder returns (code, body) like the real path."""

    def fake_urlopen(req, timeout=60.0):
        captured.setdefault("urls", []).append(req.full_url)
        for needle, (status, raw) in by_url.items():
            if needle in req.full_url:
                if status >= 400:
                    raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(raw))

                class _Resp:
                    def read(self):
                        return raw

                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                _Resp.status = status
                return _Resp()
        raise AssertionError("unexpected url %s" % req.full_url)

    return fake_urlopen


def test_media_endpoint_generates_and_normalizes(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mac import router_app

    captured = {}
    monkeypatch.setattr(
        router_app.urllib.request,
        "urlopen",
        _fake_urlopen_factory(captured, {"flux.1-schnell": (200, b'{"artifacts":[{"base64":"DUCKB64"}]}')}),
    )
    app = FastAPI()
    env = {
        "MAC_ROUTER_BACKEND": "inproc",
        "MAC_ROUTER_IMAGE_UPSTREAM": "https://ai.api.nvidia.com/v1/genai",
        "MAC_ROUTER_IMAGE_KEY": "secret:nvidia-image",
    }
    assert router_app.mount_router(app, env=env, secret_resolver={"nvidia-image": "K"}.get) is True
    r = TestClient(app).post("/v1/media/image.generate", json={"prompt": "a duck", "seed": 1})
    assert r.status_code == 200
    payload = r.json()
    assert payload["artifacts"] == [{"base64": "DUCKB64"}]
    assert payload["provider"] == "nvidia-nim" and payload["model"] == DEFAULT_IMAGE_MODEL
    # forwarded to <base>/<model>
    assert captured["urls"][0].endswith("/v1/genai/" + DEFAULT_IMAGE_MODEL)


def test_media_endpoint_unknown_op_is_404(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mac import router_app

    app = FastAPI()
    env = {
        "MAC_ROUTER_BACKEND": "inproc",
        "MAC_ROUTER_IMAGE_UPSTREAM": "https://u",
        "MAC_ROUTER_IMAGE_KEY": "secret:nvidia-image",
    }
    assert router_app.mount_router(app, env=env, secret_resolver={"nvidia-image": "K"}.get) is True
    r = TestClient(app).post("/v1/media/video.generate", json={"prompt": "x"})
    assert r.status_code == 404


def test_media_endpoint_fails_over_to_next_binding(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mac import router_app

    captured = {}
    # primary (401) -> failover to secondary (200)
    monkeypatch.setattr(
        router_app.urllib.request,
        "urlopen",
        _fake_urlopen_factory(
            captured,
            {
                "primary-model": (401, b'{"detail":"Authentication failed"}'),
                "secondary-model": (200, b'{"artifacts":[{"base64":"OK"}]}'),
            },
        ),
    )
    app = FastAPI()
    env = {
        "MAC_ROUTER_BACKEND": "inproc",
        "MAC_ROUTER_MEDIA_JSON": (
            '{"image.generate": ['
            '{"provider":"p","base_url":"https://a","model":"primary-model","key":"secret:k","adapter":"nvidia_genai","priority":0},'
            '{"provider":"s","base_url":"https://b","model":"secondary-model","key":"secret:k","adapter":"nvidia_genai","priority":1}]}'
        ),
    }
    assert router_app.mount_router(app, env=env, secret_resolver={"k": "K"}.get) is True
    r = TestClient(app).post("/v1/media/image.generate", json={"prompt": "x"})
    assert r.status_code == 200
    assert r.json()["artifacts"] == [{"base64": "OK"}]
    assert r.json()["provider"] == "s"  # failed over to the secondary binding
    assert len(captured["urls"]) == 2  # tried primary, then secondary
