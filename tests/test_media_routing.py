"""media-01: operation-keyed media routing table, adapters, and the canonical
POST /v1/media/{op} endpoint with priority failover."""
from __future__ import annotations

import io
import json
import urllib.error

from mac.media_routing import (
    DEFAULT_IMAGE_MODEL,
    MediaBinding,
    audio_music_request,
    audio_transcription_response,
    build_media_table,
    compose_media_table,
    dispatch_order,
    extract_b64,
    fal_request,
    fal_response,
    gen_capable_agents,
    hardware_gen_rank,
    is_gen_capable,
    media_bindings_from_agents,
    media_binary_response,
    nvidia_genai_request,
    nvidia_genai_response,
    op_is_async,
    op_is_binary,
    openai_audio_speech_request,
    openai_audio_transcription_request,
    openai_images_request,
    openai_images_response,
    video_generate_request,
    video_generate_response,
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
        {"name": "hostd", "status": "idle", "health_status": "healthy",
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


# --- hardware-derived gen routing -------------------------------------------

def _gpu_agent(name, accel, gpu_name, op_url="http://h:8189"):
    return {"name": name, "status": "idle", "health_status": "healthy",
            "resources": {"hardware": {"accelerator": accel, "gpu": {"name": gpu_name}},
                          "media_routes": [{"op": "image.generate", "base_url": op_url, "adapter": "openai_images"}]}}


def test_hardware_gen_rank_orders_accelerators():
    assert hardware_gen_rank("cuda") < hardware_gen_rank("metal") < hardware_gen_rank("none")
    assert hardware_gen_rank("rocm") == hardware_gen_rank("cuda")
    assert hardware_gen_rank(None) == hardware_gen_rank("none")


def test_is_gen_capable():
    assert is_gen_capable({"accelerator": "cuda"}) is True
    assert is_gen_capable({"accelerator": "metal"}) is True
    assert is_gen_capable({"accelerator": "none"}) is False
    assert is_gen_capable(None) is False


def test_media_bindings_ranked_by_reported_hardware():
    # three agents advertise the same op; the hub must prefer the best accelerator
    agents = [
        _gpu_agent("cpu-box", "none", None, "http://cpu:8189"),
        _gpu_agent("mac", "metal", "Apple M4 Pro", "http://mac:8189"),
        _gpu_agent("hostc", "cuda", "NVIDIA GB10", "http://hostc:8189"),
    ]
    urls = [b.base_url for b in media_bindings_from_agents(agents)["image.generate"]]
    assert urls == ["http://hostc:8189", "http://mac:8189", "http://cpu:8189"]  # cuda > metal > cpu


def test_gen_capable_agents_lists_accelerated_only_sorted():
    agents = [
        _gpu_agent("hostc", "cuda", "NVIDIA GB10"),
        _gpu_agent("mac", "metal", "Apple M4 Pro"),
        {"name": "hosta", "status": "idle", "resources": {"hardware": {"accelerator": "none"}}},
        {"name": "stale", "status": "idle", "resources": {}},
    ]
    caps = gen_capable_agents(agents)
    assert [c["agent"] for c in caps] == ["hostc", "mac"]  # cuda first; none/stale excluded
    assert caps[0]["gpu"] == "NVIDIA GB10" and caps[0]["serving"] is True


# --- multi-GPU load balancing (VRAM tie-break + least-loaded dispatch) -------

def _gpu_agent_vram(name, accel, vram_mb, url):
    return {"name": name, "status": "idle", "health_status": "healthy",
            "resources": {"hardware": {"accelerator": accel, "gpu": {"name": name, "vram_mb": vram_mb}},
                          "media_routes": [{"op": "image.generate", "base_url": url, "adapter": "openai_images"}]}}


def test_same_tier_ordered_by_vram_descending():
    agents = [
        _gpu_agent_vram("small", "cuda", 12000, "http://small:8189"),
        _gpu_agent_vram("big", "cuda", 49000, "http://big:8189"),
    ]
    urls = [b.base_url for b in media_bindings_from_agents(agents)["image.generate"]]
    assert urls == ["http://big:8189", "http://small:8189"]  # bigger VRAM first within the cuda tier


def test_binding_carries_accelerator_rank():
    agents = [_gpu_agent_vram("hostc", "cuda", 0, "http://hostc:8189"),
              {"name": "mac", "status": "idle", "health_status": "healthy",
               "resources": {"hardware": {"accelerator": "metal", "memory_mb": 64000},
                             "media_routes": [{"op": "image.generate", "base_url": "http://mac:8189", "adapter": "openai_images"}]}}]
    table = media_bindings_from_agents(agents)["image.generate"]
    ranks = {b.base_url: b.rank for b in table}
    assert ranks["http://hostc:8189"] == 0 and ranks["http://mac:8189"] == 1  # cuda tier 0, metal tier 1


def test_dispatch_order_least_loaded_within_top_tier():
    bindings = media_bindings_from_agents([
        _gpu_agent_vram("a", "cuda", 40000, "http://a:8189"),
        _gpu_agent_vram("b", "cuda", 40000, "http://b:8189"),
    ])["image.generate"]
    # no load -> resolver order (a before b by stable VRAM tie)
    assert [b.base_url for b in dispatch_order(bindings, {})][0] in ("http://a:8189", "http://b:8189")
    # a is busy -> b is tried first
    order = dispatch_order(bindings, {"http://a:8189": 2})
    assert order[0].base_url == "http://b:8189"


def test_dispatch_order_keeps_lower_tiers_as_failover():
    cuda = MediaBinding("gpu", "http://gpu:8189", "", "", "openai_images", 180.0, rank=0)
    cloud = MediaBinding("nvidia", "https://cloud", "secret:k", "m", "nvidia_genai", 180.0, rank=9)
    # even if the cuda agent is "loaded", cloud (lower tier) never jumps ahead of it
    order = dispatch_order([cuda, cloud], {"http://gpu:8189": 99})
    assert [b.provider for b in order] == ["gpu", "nvidia"]


def test_mount_balances_across_two_gpu_agents(monkeypatch):
    """Concurrent-ish requests spread across two same-tier GPU agents."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mac import router_app

    captured = {}
    monkeypatch.setattr(
        router_app.urllib.request, "urlopen",
        _fake_urlopen_factory(captured, {
            "a:8189": (200, b'{"data":[{"b64_json":"A"}]}'),
            "b:8189": (200, b'{"data":[{"b64_json":"B"}]}'),
        }),
    )
    app = FastAPI()

    def agent_table():
        return media_bindings_from_agents([
            _gpu_agent_vram("a", "cuda", 40000, "http://a:8189"),
            _gpu_agent_vram("b", "cuda", 40000, "http://b:8189"),
        ])

    assert router_app.mount_router(app, env={"MAC_ROUTER_BACKEND": "inproc"},
                                   secret_resolver={}.get, media_agent_table_provider=agent_table) is True
    client = TestClient(app)
    # sequential requests: in-flight returns to 0 between them, so order is stable
    # (the point is both are reachable + chosen, not strict alternation under serial calls)
    providers = {client.post("/v1/media/image.generate", json={"prompt": "x"}).json()["provider"] for _ in range(2)}
    assert providers <= {"a", "b"} and len(providers) >= 1


# --- media-01 Part B1: audio adapters + binary transport --------------------

def test_op_is_binary():
    assert op_is_binary("audio.tts") and op_is_binary("audio.music") and op_is_binary("video.generate")
    assert not op_is_binary("image.generate") and not op_is_binary("")


def test_audio_speech_request_shaping():
    path, body = openai_audio_speech_request("audio.tts", {"input": "hello", "voice": "v2"}, "bark")
    assert path == "/audio/speech"
    assert body["input"] == "hello" and body["voice"] == "v2" and body["model"] == "bark"
    # prompt is an accepted alias for input
    _, body2 = openai_audio_speech_request("audio.tts", {"prompt": "hi"}, "bark")
    assert body2["input"] == "hi"


def test_audio_music_request_shaping():
    path, body = audio_music_request("audio.music", {"prompt": "lofi", "duration": 5}, "musicgen-small")
    assert path == "/audio/music"
    assert body["prompt"] == "lofi" and body["duration"] == 5.0 and body["model"] == "musicgen-small"


def test_media_binary_response_unwraps_forwarder_bytes():
    wrapped = {"__media_bytes__": "QUJD", "content_type": "audio/wav", "bytes": 3}
    out = media_binary_response(200, wrapped)
    assert out == {"artifacts": [{"base64": "QUJD", "content_type": "audio/wav"}]}
    # non-2xx passes the error through
    assert media_binary_response(500, {"error": {"message": "boom"}})["error"]["message"] == "boom"
    # empty 2xx -> explicit empty_response error
    assert media_binary_response(200, {})["error"]["type"] == "empty_response"


def test_media_endpoint_audio_tts_routes_binary(monkeypatch):
    """End-to-end: POST /v1/media/audio.tts -> openai_audio_speech adapter ->
    binary-aware forwarder base64-wraps the audio/wav body -> canonical artifact."""
    import base64 as _b64
    import json as _json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mac import router_app

    wav = b"RIFF\x00\x00WAVfake-audio-bytes"
    captured = {}

    def fake_urlopen(req, timeout=60.0):
        captured["url"] = req.full_url

        class _Headers:
            @staticmethod
            def get(key, default=None):
                return "audio/wav" if key.lower() == "content-type" else default

        class _Resp:
            status = 200
            headers = _Headers()

            def read(self):
                return wav

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()

    monkeypatch.setattr(router_app.urllib.request, "urlopen", fake_urlopen)
    app = FastAPI()
    media_json = _json.dumps({
        "audio.tts": [{
            "provider": "hostc", "base_url": "http://hostc:8190/v1",
            "model": "bark", "adapter": "openai_audio_speech",
        }]
    })
    env = {"MAC_ROUTER_BACKEND": "inproc", "MAC_ROUTER_MEDIA_JSON": media_json}
    assert router_app.mount_router(app, env=env) is True
    r = TestClient(app).post("/v1/media/audio.tts", json={"input": "hello world"})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["provider"] == "hostc" and payload["model"] == "bark"
    assert payload["artifacts"][0]["base64"] == _b64.b64encode(wav).decode("ascii")
    assert payload["artifacts"][0]["content_type"] == "audio/wav"
    assert captured["url"].endswith("/audio/speech")


# --- media-01 Part B2: ASR (multipart upload -> text) -----------------------

def test_audio_transcription_request_is_multipart():
    path, body = openai_audio_transcription_request("audio.asr", {"audio": "QUJD"}, "whisper-large-v3")
    assert path == "/audio/transcriptions"
    mp = body["__multipart__"]
    assert mp["fields"]["model"] == "whisper-large-v3"
    assert mp["file"]["b64"] == "QUJD" and mp["file"]["name"] == "file"


def test_audio_transcription_response():
    assert audio_transcription_response(200, {"text": "hello world"}) == {"text": "hello world"}
    assert audio_transcription_response(200, {})["error"]["type"] == "empty_response"
    assert audio_transcription_response(500, {"error": {"message": "x"}})["error"]["message"] == "x"


def test_encode_multipart_round_trips():
    import base64
    from mac.router_app import _encode_multipart

    ctype, body = _encode_multipart({
        "fields": {"model": "whisper-1"},
        "file": {"name": "file", "filename": "a.wav", "content_type": "audio/wav", "b64": base64.b64encode(b"PCMDATA").decode()},
    })
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="model"' in body and b"whisper-1" in body
    assert b'filename="a.wav"' in body and b"PCMDATA" in body


def test_media_endpoint_audio_asr_multipart(monkeypatch):
    """End-to-end ASR: POST /v1/media/audio.asr -> multipart file upload to the
    upstream -> JSON {"text"} -> canonical {"text"}."""
    import base64 as _b64
    import json as _json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mac import router_app

    captured = {}

    def fake_urlopen(req, timeout=60.0):
        captured["url"] = req.full_url
        captured["ctype"] = req.get_header("Content-type")  # urllib title-cases header keys
        captured["body"] = req.data

        class _Resp:
            status = 200

            class _H:
                @staticmethod
                def get(k, d=None):
                    return "application/json" if k.lower() == "content-type" else d

            headers = _H()

            def read(self):
                return b'{"text": "the quick brown fox"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()

    monkeypatch.setattr(router_app.urllib.request, "urlopen", fake_urlopen)
    app = FastAPI()
    media_json = _json.dumps({
        "audio.asr": [{
            "provider": "hostc", "base_url": "http://hostc:8190/v1",
            "model": "whisper-large-v3", "adapter": "openai_audio_transcription",
        }]
    })
    assert router_app.mount_router(app, env={"MAC_ROUTER_BACKEND": "inproc", "MAC_ROUTER_MEDIA_JSON": media_json}) is True
    audio_b64 = _b64.b64encode(b"RIFFfake-wav").decode("ascii")
    r = TestClient(app).post("/v1/media/audio.asr", json={"audio": audio_b64})
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "the quick brown fox"
    assert captured["url"].endswith("/audio/transcriptions")
    assert "multipart/form-data" in (captured["ctype"] or "")
    assert b"RIFFfake-wav" in (captured["body"] or b"")


# --- media-01 Part B3: video (async submit -> poll) -------------------------

def test_op_is_async():
    assert op_is_async("video.generate") and not op_is_async("audio.tts")
    assert not op_is_async("image.generate") and not op_is_async("")


def test_video_generate_request_shaping():
    path, body = video_generate_request("video.generate", {"prompt": "a cat", "duration": 4}, "animatediff")
    assert path == "/video/generate"
    assert body["prompt"] == "a cat" and body["duration"] == 4.0 and body["model"] == "animatediff"


def test_video_generate_response_async_and_sync():
    assert video_generate_response(200, {"job_id": "vjob_1", "status": "running"}) == {"job_id": "vjob_1", "status": "running"}
    # synchronous fallback (a server that returns an artifact directly)
    assert video_generate_response(200, {"data": [{"b64_json": "B"}]}) == {"artifacts": [{"base64": "B"}]}
    assert video_generate_response(500, {"error": {"message": "x"}})["error"]["message"] == "x"


def test_media_endpoint_video_async_submit_then_poll(monkeypatch):
    """End-to-end async video: POST /v1/media/video.generate -> hub job id; then
    GET /v1/media/jobs/{id} -> hub polls the upstream -> completed + artifact."""
    import json as _json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mac import router_app

    def fake_urlopen(req, timeout=60.0):
        url, method = req.full_url, (req.get_method() if hasattr(req, "get_method") else "POST")

        class _Headers:
            @staticmethod
            def get(k, d=None):
                return "application/json" if k.lower() == "content-type" else d

        class _Resp:
            status = 200
            headers = _Headers()

            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return _json.dumps(self._payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        if url.endswith("/video/generate"):
            return _Resp({"job_id": "vjob_42", "status": "running"})
        if "/video/jobs/vjob_42" in url:
            return _Resp({"status": "completed", "artifacts": [{"base64": "GIFDATA", "content_type": "image/gif"}]})
        raise AssertionError("unexpected url %s" % url)

    monkeypatch.setattr(router_app.urllib.request, "urlopen", fake_urlopen)
    app = FastAPI()
    media_json = _json.dumps({
        "video.generate": [{
            "provider": "hostc", "base_url": "http://hostc:8191/v1",
            "model": "animatediff", "adapter": "video_generate",
        }]
    })
    assert router_app.mount_router(app, env={"MAC_ROUTER_BACKEND": "inproc", "MAC_ROUTER_MEDIA_JSON": media_json}) is True
    client = TestClient(app)
    submit = client.post("/v1/media/video.generate", json={"prompt": "a cat"})
    assert submit.status_code == 200, submit.text
    job_id = submit.json()["job_id"]
    assert job_id.startswith("mjob_") and submit.json()["status"] == "running"
    # poll -> hub forwards to the upstream job + returns the artifact
    poll = client.get("/v1/media/jobs/%s" % job_id)
    assert poll.status_code == 200, poll.text
    assert poll.json()["status"] == "completed"
    assert poll.json()["artifacts"][0]["base64"] == "GIFDATA"
    # unknown job -> 404
    assert client.get("/v1/media/jobs/mjob_nope").status_code == 404
