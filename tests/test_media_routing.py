"""media-01: operation-keyed media routing table, adapters, and the canonical
POST /v1/media/{op} endpoint with priority failover."""
from __future__ import annotations

import io
import json
import urllib.error

from mac.media_routing import (
    DEFAULT_IMAGE_MODEL,
    MediaBinding,
    build_media_table,
    compose_media_table,
    extract_b64,
    fal_request,
    fal_response,
    media_bindings_from_agents,
    nvidia_genai_request,
    nvidia_genai_response,
    openai_images_request,
    openai_images_response,
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
        captured.setdefault("auths", []).append(req.get_header("Authorization"))
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


def test_media_endpoint_honors_caller_model(monkeypatch):
    """A caller may request a model; it overrides the binding default but stays
    on the binding's upstream (path traversal rejected)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mac import router_app

    captured = {}
    monkeypatch.setattr(
        router_app.urllib.request,
        "urlopen",
        _fake_urlopen_factory(captured, {"flux.1-dev": (200, b'{"artifacts":[{"base64":"X"}]}')}),
    )
    app = FastAPI()
    env = {
        "MAC_ROUTER_BACKEND": "inproc",
        "MAC_ROUTER_IMAGE_UPSTREAM": "https://ai.api.nvidia.com/v1/genai",
        "MAC_ROUTER_IMAGE_KEY": "secret:nvidia-image",
    }
    assert router_app.mount_router(app, env=env, secret_resolver={"nvidia-image": "K"}.get) is True
    r = TestClient(app).post(
        "/v1/media/image.generate",
        json={"prompt": "x", "model": "black-forest-labs/flux.1-dev"},
    )
    assert r.status_code == 200
    assert r.json()["model"] == "black-forest-labs/flux.1-dev"  # caller model honored
    assert captured["urls"][0].endswith("/v1/genai/black-forest-labs/flux.1-dev")


def test_media_endpoint_rejects_path_traversal_model(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mac import router_app

    captured = {}
    monkeypatch.setattr(
        router_app.urllib.request,
        "urlopen",
        _fake_urlopen_factory(captured, {"flux.1-schnell": (200, b'{"artifacts":[{"base64":"X"}]}')}),
    )
    app = FastAPI()
    env = {
        "MAC_ROUTER_BACKEND": "inproc",
        "MAC_ROUTER_IMAGE_UPSTREAM": "https://ai.api.nvidia.com/v1/genai",
        "MAC_ROUTER_IMAGE_KEY": "secret:nvidia-image",
    }
    assert router_app.mount_router(app, env=env, secret_resolver={"nvidia-image": "K"}.get) is True
    r = TestClient(app).post("/v1/media/image.generate", json={"prompt": "x", "model": "../../etc/passwd"})
    assert r.status_code == 200  # fell back to the binding default model
    assert captured["urls"][0].endswith("/v1/genai/" + DEFAULT_IMAGE_MODEL)


# --- openai_images + fal adapters -------------------------------------------

def test_openai_images_adapter_request_and_response():
    path, body = openai_images_request("image.generate", {"prompt": "x", "width": 1024, "height": 1024}, "dall-e-3")
    assert path == "/images/generations"
    assert body == {"prompt": "x", "size": "1024x1024", "n": 1, "response_format": "b64_json", "model": "dall-e-3"}
    assert openai_images_response(200, {"data": [{"b64_json": "AB"}]}) == {"artifacts": [{"base64": "AB"}]}
    assert "error" in openai_images_response(401, {"error": {"message": "no"}})


def test_fal_adapter_request_and_url_response():
    path, body = fal_request("image.generate", {"prompt": "x", "seed": 7, "steps": 4, "n": 2}, "fal-ai/flux/schnell")
    assert path == "/fal-ai/flux/schnell"
    assert body["prompt"] == "x" and body["seed"] == 7 and body["num_inference_steps"] == 4 and body["num_images"] == 2
    # FAL returns image URLs -> canonical `url` artifacts (the agent downloads them)
    assert fal_response(200, {"images": [{"url": "https://cdn/x.png"}]}) == {"artifacts": [{"url": "https://cdn/x.png"}]}


def test_build_table_reads_auth_scheme():
    env = {"MAC_ROUTER_MEDIA_JSON": json.dumps(
        {"image.generate": [{"provider": "fal", "base_url": "https://fal.run", "model": "m",
                             "key": "secret:fal", "adapter": "fal", "auth_scheme": "Key"}]}
    )}
    (binding,) = build_media_table(env)["image.generate"]
    assert binding.auth_scheme == "Key"


def test_default_binding_auth_scheme_is_bearer():
    (binding,) = build_media_table(
        {"MAC_ROUTER_IMAGE_UPSTREAM": "https://u", "MAC_ROUTER_IMAGE_KEY": "k"}
    )["image.generate"]
    assert binding.auth_scheme == "Bearer"


def test_mount_forwards_with_binding_auth_scheme(monkeypatch):
    """A fal binding must send `Authorization: Key <...>`, not Bearer."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mac import router_app

    captured = {}
    monkeypatch.setattr(
        router_app.urllib.request, "urlopen",
        _fake_urlopen_factory(captured, {"flux/schnell": (200, b'{"images":[{"url":"https://cdn/x.png"}]}')}),
    )
    app = FastAPI()
    env = {
        "MAC_ROUTER_BACKEND": "inproc",
        "MAC_ROUTER_MEDIA_JSON": json.dumps(
            {"image.generate": [{"provider": "fal", "base_url": "https://fal.run",
                                 "model": "fal-ai/flux/schnell", "key": "secret:fal",
                                 "adapter": "fal", "auth_scheme": "Key"}]}
        ),
    }
    assert router_app.mount_router(app, env=env, secret_resolver={"fal": "FALKEY"}.get) is True
    r = TestClient(app).post("/v1/media/image.generate", json={"prompt": "x"})
    assert r.status_code == 200
    assert r.json()["artifacts"] == [{"url": "https://cdn/x.png"}]
    assert captured["auths"][0] == "Key FALKEY"  # Key scheme, not Bearer


# --- capability auto-registration (live agents advertise media routes) ------

def test_media_bindings_from_agents_priority_and_offline_skip():
    agents = [
        {"name": "bullwinkle", "status": "idle", "health_status": "healthy",
         "resources": {"media_routes": [
             {"op": "image.generate", "base_url": "http://bw:8189", "adapter": "openai_images", "priority": 1},
             {"op": "image.generate", "base_url": "http://bw:8190", "adapter": "passthrough", "priority": 0}]}},
        {"name": "down-gpu", "status": "offline",
         "resources": {"media_routes": [{"op": "image.generate", "base_url": "http://down:9"}]}},
        {"name": "cpu", "status": "idle", "resources": {}},
    ]
    table = media_bindings_from_agents(agents)
    assert [b.base_url for b in table["image.generate"]] == ["http://bw:8190", "http://bw:8189"]  # priority asc; offline dropped


def test_media_bindings_from_agents_skips_unhealthy():
    agents = [{"name": "x", "status": "idle", "health_status": "unhealthy",
               "resources": {"media_routes": [{"op": "image.generate", "base_url": "http://x:1"}]}}]
    assert media_bindings_from_agents(agents) == {}


def test_compose_prefers_live_agent_then_static():
    static = {"image.generate": [MediaBinding("nvidia", "https://nv", "secret:k", "m", "nvidia_genai", 180.0)]}
    agent = {"image.generate": [MediaBinding("bw", "http://bw:8189", "", "sdxl", "openai_images", 180.0)]}
    assert [b.provider for b in compose_media_table(static, agent, "image.generate")] == ["bw", "nvidia"]


def test_media_endpoint_prefers_live_agent_over_static(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mac import router_app

    captured = {}
    monkeypatch.setattr(
        router_app.urllib.request, "urlopen",
        _fake_urlopen_factory(captured, {"bw:8189": (200, b'{"data":[{"b64_json":"GPU"}]}')}),
    )
    app = FastAPI()
    env = {  # static config = cloud fallback
        "MAC_ROUTER_BACKEND": "inproc",
        "MAC_ROUTER_IMAGE_UPSTREAM": "https://ai.api.nvidia.com/v1/genai",
        "MAC_ROUTER_IMAGE_KEY": "secret:nvidia-image",
    }

    def agent_table():
        return media_bindings_from_agents([
            {"name": "bw", "status": "idle", "health_status": "healthy",
             "resources": {"media_routes": [{"op": "image.generate", "base_url": "http://bw:8189",
                                             "model": "sdxl", "adapter": "openai_images"}]}}])

    assert router_app.mount_router(app, env=env, secret_resolver={}.get,
                                   media_agent_table_provider=agent_table) is True
    r = TestClient(app).post("/v1/media/image.generate", json={"prompt": "x"})
    assert r.status_code == 200
    assert r.json()["provider"] == "bw"  # routed to the live GPU agent, not cloud
    assert "bw:8189" in captured["urls"][0]
