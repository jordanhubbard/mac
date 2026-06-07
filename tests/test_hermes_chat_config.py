import json

from mac.hermes_chat_config import parse_env_file, sync

TOKEN = "validtok_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
STALE = "stale_tokenhub_bearer_zzzzzzzzzzzzzzzzzzzzzzzzzzzzz"


def _mac_env(tmp_path):
    p = tmp_path / "mac.env"
    p.write_text(
        "\n".join(
            [
                "MAC_HERMES_GATEWAY_BASE_URL=http://127.0.0.1:8789/v1",
                "OPENAI_BASE_URL=http://127.0.0.1:8789/v1",
                "CUSTOM_BASE_URL=http://127.0.0.1:8789/v1",
                "MAC_HERMES_GATEWAY_API_KEY=%s" % TOKEN,
                "OPENAI_API_KEY=%s" % TOKEN,
                "MAC_HERMES_GATEWAY_PROVIDER=custom",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return p


def _stale_hermes_home(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / ".env").write_text(
        "\n".join(
            [
                "OPENAI_BASE_URL=http://100.125.137.89:8090/v1",
                "CUSTOM_BASE_URL=http://100.125.137.89:8090/v1",
                "MAC_HERMES_GATEWAY_BASE_URL=http://100.125.137.89:8090/v1",
                "MAC_HERMES_GATEWAY_API_KEY=%s" % STALE,
                "SLACK_BOT_TOKEN=xoxb-keepme",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                "  provider: tokenhub",
                "  base_url: http://100.125.137.89:8090/v1/",
                "providers:",
                "  tokenhub:",
                "    api: http://100.125.137.89:8090/v1/",
                "    name: tokenhub",
                "    key: %s" % STALE,
                "fallback_providers: []",
                "agent:",
                "  max_turns: 90",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (home / "auth.json").write_text(
        json.dumps(
            {
                "credential_pool": {"custom:tokenhub": {"k": 1}, "anthropic": {"k": 2}},
                "providers": {},
                "version": 1,
            }
        ),
        encoding="utf-8",
    )
    return home


def test_sync_migrates_tokenhub_runtime_config_to_router(tmp_path):
    mac = _mac_env(tmp_path)
    home = _stale_hermes_home(tmp_path)

    result = sync(home, mac)

    # ~/.hermes/.env: chat vars repointed; unrelated tokens preserved
    henv = parse_env_file(home / ".env")
    assert henv["OPENAI_BASE_URL"] == "http://127.0.0.1:8789/v1"
    assert henv["MAC_HERMES_GATEWAY_BASE_URL"] == "http://127.0.0.1:8789/v1"
    assert henv["MAC_HERMES_GATEWAY_API_KEY"] == TOKEN
    assert henv["SLACK_BOT_TOKEN"] == "xoxb-keepme"

    # config.yaml: model points at the router; a custom provider with api_key (NOT key)
    cfg = (home / "config.yaml").read_text(encoding="utf-8")
    assert "provider: custom" in cfg
    assert "base_url: http://127.0.0.1:8789/v1/" in cfg
    assert "  custom:" in cfg
    assert "api_key: %s" % TOKEN in cfg
    assert result["config_custom_provider"] is True

    # auth.json: stale custom:* pool entry gone, others kept
    auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    assert "custom:tokenhub" not in auth["credential_pool"]
    assert "anthropic" in auth["credential_pool"]
    assert "custom:tokenhub" in result["pool_cleared"]

    # idempotent: a second run doesn't duplicate the custom provider block
    sync(home, mac)
    cfg2 = (home / "config.yaml").read_text(encoding="utf-8")
    assert cfg2.count("  custom:\n") == 1
    assert cfg2.count("api_key: %s" % TOKEN) == 1


def test_sync_creates_chat_config_on_stub_config(tmp_path):
    # Regression: a freshly-initialized node's config.yaml is a stub with no
    # `model:`/`providers:` scaffold. The patch-only version no-op'd, leaving the
    # node with no chat provider (HTTP 403). sync must CREATE the structure.
    import yaml

    mac = _mac_env(tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / ".env").write_text("SLACK_BOT_TOKEN=xoxb-keepme\n", encoding="utf-8")
    (home / "config.yaml").write_text("web:\n  search_backend: firecrawl\n", encoding="utf-8")

    result = sync(home, mac)
    assert result["config_custom_provider"] is True

    d = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert d["model"]["provider"] == "custom"
    assert d["model"]["base_url"] == "http://127.0.0.1:8789/v1/"
    assert d["providers"]["custom"]["api_key"] == TOKEN
    assert d["providers"]["custom"]["api"] == "http://127.0.0.1:8789/v1/"
    assert d["providers"]["custom"]["transport"] == "chat_completions"
    # unrelated existing section preserved
    assert d["web"]["search_backend"] == "firecrawl"

    # idempotent: re-running keeps exactly one custom block
    sync(home, mac)
    cfg2 = (home / "config.yaml").read_text(encoding="utf-8")
    assert cfg2.count("  custom:\n") == 1
    assert cfg2.count("api_key: %s" % TOKEN) == 1


def test_router_env_emits_wildcard_models_from_spec(tmp_path):
    # Regression (#3): the spec's router.wildcard_models must flow to
    # MAC_ROUTER_WILDCARD_MODELS so the router substitutes `*` for an allowed model.
    from pathlib import Path

    from mac.fleet_setup import build_setup_plan

    spec = {
        "schema": "mac.fleet_setup.v1",
        "fleet": {"name": "gke", "hub": "gke-hub", "hub_url": "http://gke-hub:8789"},
        "agents": [{"name": "gke-hub", "target": "horde@gke-hub", "os": "linux"}],
        "router": {
            "backend": "inproc",
            "providers": [{"id": "nvidia", "key_env": "NVIDIA_API_KEY"}],
            "wildcard_models": "azure/anthropic/claude-sonnet-4-6|azure/anthropic/claude-opus-4-6",
            "default_model": "azure/anthropic/claude-sonnet-4-6",
        },
        "network": {"provider": "none"},
    }
    plan = build_setup_plan(
        spec,
        root=Path(__file__).resolve().parents[1],
        fleets_config=tmp_path / "fleets.yaml",
        env_file=tmp_path / ".env",
        env={"NVIDIA_API_KEY": "nv-secret"},
    )
    assert plan["status"] == "pass"
    ev = plan["env_values"]
    assert ev["MAC_ROUTER_WILDCARD_MODELS"] == "azure/anthropic/claude-sonnet-4-6|azure/anthropic/claude-opus-4-6"
    assert ev["MAC_ROUTER_DEFAULT_MODEL"] == "azure/anthropic/claude-sonnet-4-6"


def test_router_env_emits_modality_upstreams_and_keys_from_spec(tmp_path):
    # Cluster-init: optional image/audio/video URL + key (DISTINCT from the chat
    # key) flow to MAC_DEPLOY_ROUTER_<M>_UPSTREAM + NVIDIA_<M>_API_KEY for the
    # deploy to wire the router proxies + escrow as secret:nvidia-<m>.
    from pathlib import Path

    from mac.fleet_setup import build_setup_plan

    spec = {
        "schema": "mac.fleet_setup.v1",
        "fleet": {"name": "gke", "hub": "gke-hub", "hub_url": "http://gke-hub:8789"},
        "agents": [{"name": "gke-hub", "target": "horde@gke-hub", "os": "linux"}],
        "router": {
            "backend": "inproc",
            "providers": [{"id": "nvidia", "key_env": "NVIDIA_API_KEY"}],
            "image": {"url": "https://ai.api.nvidia.com/v1/genai", "key": "nvapi-img"},
            "audio": {"url": "https://ai.api.nvidia.com/v1/audio", "key": "nvapi-aud"},
            "video": {"url": "https://video.example/v1/video", "key_env": "MY_VIDEO_KEY"},
        },
        "network": {"provider": "none"},
    }
    plan = build_setup_plan(
        spec,
        root=Path(__file__).resolve().parents[1],
        fleets_config=tmp_path / "fleets.yaml",
        env_file=tmp_path / ".env",
        env={"NVIDIA_API_KEY": "nv-secret", "MY_VIDEO_KEY": "vid-secret"},
    )
    assert plan["status"] == "pass"
    ev = plan["env_values"]
    assert ev["MAC_DEPLOY_ROUTER_IMAGE_UPSTREAM"] == "https://ai.api.nvidia.com/v1/genai"
    assert ev["NVIDIA_IMAGE_API_KEY"] == "nvapi-img"  # inline key, distinct from chat key
    assert ev["MAC_DEPLOY_ROUTER_AUDIO_UPSTREAM"] == "https://ai.api.nvidia.com/v1/audio"
    assert ev["NVIDIA_AUDIO_API_KEY"] == "nvapi-aud"
    assert ev["MAC_DEPLOY_ROUTER_VIDEO_UPSTREAM"] == "https://video.example/v1/video"
    assert ev["MY_VIDEO_KEY"] == "vid-secret"  # key_env read from the environment


# --- image_gen provider default (hub-routed mac-hub /v1/media) --------------
from pathlib import Path  # noqa: E402

from mac.hermes_chat_config import ensure_image_gen_provider  # noqa: E402


def _home_with_config(tmp_path, body: str) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(body, encoding="utf-8")
    return home


def test_image_gen_defaults_to_mac_hub_when_unset(tmp_path):
    # Fresh config with no image_gen block -> default to the hub-routed provider.
    home = _home_with_config(tmp_path, "model:\n  provider: custom\n  base_url: http://x/v1/\n")
    assert ensure_image_gen_provider(home) == "mac-hub"
    text = (home / "config.yaml").read_text(encoding="utf-8")
    assert "image_gen:" in text and "provider: mac-hub" in text
    # idempotent: re-running respects the value just written
    assert ensure_image_gen_provider(home) == "mac-hub"


def test_image_gen_respects_explicit_provider(tmp_path):
    home = _home_with_config(tmp_path, "image_gen:\n  provider: fal\n")
    assert ensure_image_gen_provider(home) == "fal"  # never override an explicit choice
    assert (home / "config.yaml").read_text(encoding="utf-8").count("provider:") == 1


def test_image_gen_inserts_provider_into_existing_block(tmp_path):
    home = _home_with_config(tmp_path, "image_gen:\n  model: flux.1-schnell\n")
    assert ensure_image_gen_provider(home) == "mac-hub"
    text = (home / "config.yaml").read_text(encoding="utf-8")
    assert "provider: mac-hub" in text and "model: flux.1-schnell" in text


def test_image_gen_noop_without_config(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    assert ensure_image_gen_provider(home) == ""  # no config.yaml -> nothing to do


def test_sync_reports_image_gen_provider(tmp_path):
    home = _stale_hermes_home(tmp_path)
    result = sync(home, _mac_env(tmp_path))
    assert result["image_gen_provider"] == "mac-hub"


def test_image_gen_migrates_prior_deploy_default_nvidia_to_mac_hub(tmp_path):
    # A fleet still carrying the old deploy default (nvidia) migrates forward;
    # both are hub-routed to the same upstream so it's behavior-preserving.
    home = _home_with_config(tmp_path, "image_gen:\n  provider: nvidia\n")
    assert ensure_image_gen_provider(home) == "mac-hub"
    text = (home / "config.yaml").read_text(encoding="utf-8")
    assert "provider: mac-hub" in text and "provider: nvidia" not in text


def test_image_gen_does_not_migrate_genuine_alternative(tmp_path):
    # fal/openai/etc. are real backend choices, not deploy-managed defaults.
    home = _home_with_config(tmp_path, "image_gen:\n  provider: openai\n")
    assert ensure_image_gen_provider(home) == "openai"
    assert "provider: openai" in (home / "config.yaml").read_text(encoding="utf-8")
