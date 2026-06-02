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
