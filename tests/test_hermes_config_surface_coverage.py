"""Edge contracts for the fleet-managed Hermes configuration surface."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from mac import hermes_config_surface as surface
from mac.models import ValidationError


def test_mapping_and_nested_helpers_cover_replacement_deletion_and_errors(tmp_path):
    missing = tmp_path / "missing.yaml"
    assert surface._read_yaml_mapping(missing) == {}
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert surface._read_yaml_mapping(empty) == {}
    invalid = tmp_path / "list.yaml"
    invalid.write_text("- item\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="mapping"):
        surface._read_yaml_mapping(invalid)

    base = {"nested": {"keep": 1, "replace": {"old": True}}, "scalar": 1}
    override = {"nested": {"replace": "new", "add": 2}, "scalar": {"deep": True}}
    assert surface._deep_merge(base, override) == {
        "nested": {"keep": 1, "replace": "new", "add": 2},
        "scalar": {"deep": True},
    }
    assert base["scalar"] == 1

    value = {"a": "was scalar"}
    surface._nested_set(value, "a.b.c", 3)
    assert value == {"a": {"b": {"c": 3}}}
    with pytest.raises(ValidationError):
        surface._nested_set(value, "a..b", 1)
    surface._nested_delete(value, "a.b.c")
    assert value == {}
    surface._nested_delete(value, "missing.path")

    assert surface._safe_json_value({"ok": True}) == {"ok": True}
    assert "object" in surface._safe_json_value(object())
    assert surface._redacted("") == ""
    assert surface._redacted("secret").startswith("<redacted:")
    assert surface._is_mapping({}) and not surface._is_mapping([])


def test_frontmatter_plugin_and_skill_discovery_survives_bad_files(tmp_path, monkeypatch):
    """Malformed manifests must not break discovery.

    The "bundled" roots came from the vendored Hermes tree, removed
    2026-08-17; discovery now reads only the user/installed roots under
    ~/.hermes, so the fixture builds everything there.
    """
    home = tmp_path / "home"
    plugins = home / "plugins"
    skills = home / "skills"
    user_plugins = plugins
    user_skills = skills
    for path in (plugins, skills):
        path.mkdir(parents=True)
    monkeypatch.setattr(surface, "hermes_home", lambda: home)

    plugin = plugins / "backend"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(
        "name: backend\nkind: backend\nrequires_env: [API_TOKEN]\n", encoding="utf-8"
    )
    bad = plugins / "bad"
    bad.mkdir()
    (bad / "plugin.yaml").write_text("[invalid", encoding="utf-8")
    non_mapping = plugins / "list"
    non_mapping.mkdir()
    (non_mapping / "plugin.yaml").write_text("- item\n", encoding="utf-8")
    deep = plugins / "one" / "two" / "three"
    deep.mkdir(parents=True)
    (deep / "plugin.yaml").write_text("name: too-deep\n", encoding="utf-8")
    records = surface._plugin_manifest_records()
    assert [record["name"] for record in records].count("backend") == 1
    assert records[0]["requires_env"] == ["API_TOKEN"]

    skill = skills / "group" / "writer"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: writer\ndescription: Writes things\ntags: [content]\n---\nbody\n",
        encoding="utf-8",
    )
    hidden = skills / ".hidden"
    hidden.mkdir()
    (hidden / "SKILL.md").write_text("---\nname: hidden\n---\n", encoding="utf-8")
    malformed = skills / "broken"
    malformed.mkdir()
    (malformed / "SKILL.md").write_text("---\n[bad\n---\n", encoding="utf-8")
    assert surface._frontmatter(tmp_path / "absent") == {}
    plain = tmp_path / "plain"
    plain.write_text("body", encoding="utf-8")
    assert surface._frontmatter(plain) == {}
    unfinished = tmp_path / "unfinished"
    unfinished.write_text("---\nname: no-end", encoding="utf-8")
    assert surface._frontmatter(unfinished) == {}
    skill_records = surface._skill_records()
    assert [record["name"] for record in skill_records] == ["broken", "writer"]
    assert skill_records[-1]["category"] == "group"


def test_declared_env_specs_merge_hermes_plugins_and_skills(monkeypatch):
    config = SimpleNamespace(
        REQUIRED_ENV_VARS={
            "REQUIRED_TOKEN": {"description": "required", "password": True},
            "PLAIN_REQUIRED": "invalid-meta",
        },
        OPTIONAL_ENV_VARS={"OPTIONAL_URL": {"category": "network"}},
    )
    monkeypatch.setattr(surface, "_hermes_config_module", lambda: config)
    plugins = [
        {
            "key": "plugin",
            "requires_env": ["PLUGIN_KEY", {"name": "PLUGIN_URL", "url": "https://docs"}],
            "optional_env": [None, {"missing": "name"}],
        }
    ]
    skills = [{"name": "skill", "required_environment_variables": ["SKILL_TOKEN"]}]
    specs = surface._declared_env_specs(plugins, skills)
    by_name = {spec["name"]: spec for spec in specs}
    assert by_name["REQUIRED_TOKEN"]["password"] is True
    assert by_name["PLAIN_REQUIRED"]["prompt"] == "PLAIN_REQUIRED"
    assert by_name["PLUGIN_URL"]["url"] == "https://docs"
    assert by_name["SKILL_TOKEN"]["category"] == "skill"
    monkeypatch.setattr(
        surface,
        "_hermes_config_module",
        lambda: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    assert any(spec["name"] == "PLUGIN_KEY" for spec in surface._declared_env_specs(plugins, []))


def test_fleet_registry_shapes_and_entry_resolution():
    mapped = {"fleets": {"key": {"fleet_name": "named", "hub_agent": "hub"}, "skip": []}}
    assert surface._fleet_entries(mapped) == {"key": mapped["fleets"]["key"]}
    listed = {
        "fleets": [
            {"hub_agent": "hub"},
            {"fleet_name": "named"},
            {"name": "fallback"},
            {},
            "skip",
        ]
    }
    assert set(surface._fleet_entries(listed)) == {"hub", "named", "fallback"}

    for fleet in (
        {"id": "key", "name": "other"},
        {"id": "id", "name": "named"},
        {"id": "id", "name": "hub"},
    ):
        key, entry = surface._find_or_create_fleet_entry(mapped, fleet)
        assert key == "key"
        assert entry is mapped["fleets"]["key"]

    registry = {"fleets": []}
    key, entry = surface._find_or_create_fleet_entry(registry, {"id": "fleet_id", "name": "new"})
    assert key == "new"
    assert registry["fleets"]["new"] is entry


def test_plugin_skill_and_apply_status_state_precedence():
    plugins = [
        {"key": "desired-off", "name": "off"},
        {"key": "desired-on", "name": "on"},
        {"key": "local-off", "name": "local-off"},
        {"key": "local-on", "name": "local-on"},
        {"key": "bundled", "name": "bundled", "source": "bundled", "kind": "backend"},
    ]
    states = surface._plugin_records_with_state(
        plugins,
        {"enabled": ["desired-on"], "disabled": ["desired-off"]},
        {"plugins": {"enabled": ["local-on"], "disabled": ["local-off"]}},
    )
    assert [item["state"] for item in states] == [
        "disabled",
        "enabled",
        "disabled",
        "enabled",
        "auto_enabled",
    ]
    skills = surface._skill_records_with_state(
        [{"name": "desired"}, {"name": "local"}, {"name": "default"}],
        {"disabled": ["desired"]},
        {"skills": {"disabled": ["local"]}},
    )
    assert [item["state_source"] for item in skills] == [
        "fleet_desired",
        "local_config",
        "default",
    ]

    streams = [
        {"id": "old", "topic": "topic", "recipient": "agent", "updated_at": "1"},
        {"id": "new", "topic": "topic", "recipient": "agent", "updated_at": "2"},
        {"id": "skip", "topic": "other", "recipient": "agent"},
    ]
    latest = surface._latest_stream_by_agent(
        streams, topic="topic", agent_field="recipient", agent_ids={"agent"}
    )
    assert latest["agent"]["id"] == "new"


def test_surface_patch_normalization_and_application_covers_all_sections():
    with pytest.raises(ValidationError, match="list"):
        surface._normalize_string_list("not-list", field_name="items")
    with pytest.raises(ValidationError, match="invalid environment"):
        surface.normalize_surface_patch({"env": {"bad-name": "value"}})
    with pytest.raises(ValidationError, match="not dashboard-writable"):
        surface.normalize_surface_patch({"env": {"PATH": "value"}})

    patch = surface.normalize_surface_patch(
        {
            "runtime": {"gateway_model": "model"},
            "config": {"nested.value": {1, 2}},
            "remove_config": ["old.value"],
            "env": {"NEW_VALUE": "set", "EMPTY_VALUE": ""},
            "remove_env": ["OLD_VALUE"],
            "plugins": {"enabled": ["one"], "disabled": ["two"]},
            "skills": {
                "disabled": ["skill"],
                "platform_disabled": {"linux": ["one", ""]},
            },
        }
    )
    hermes = {
        "config": "wrong",
        "env": "wrong",
        "plugins": "wrong",
        "skills": "wrong",
    }
    surface._apply_patch_to_hermes_mapping(hermes, patch)
    assert hermes["gateway_model"] == "model"
    assert hermes["config"]["nested"]["value"].startswith("{")
    assert hermes["env"] == {"NEW_VALUE": "set"}
    assert hermes["plugins"] == {"enabled": ["one"], "disabled": ["two"]}
    assert hermes["skills"]["platform_disabled"] == {"linux": ["one"]}


def test_update_encode_decode_and_module_cli(tmp_path, monkeypatch, capsys):
    registry = tmp_path / "fleets.yaml"
    registry.write_text("fleets:\n  existing:\n    defaults: wrong\n", encoding="utf-8")
    monkeypatch.setattr(
        surface,
        "apply_hermes_surface_payload",
        lambda payload, target_home=None: {"applied": True, "payload": payload, "home": str(target_home)},
    )
    result = surface.update_fleet_hermes_surface(
        {"id": "existing", "name": "existing"},
        {"config": {"model.default": "x"}, "env": {"API_TOKEN": "secret"}},
        apply_local=True,
        path=registry,
    )
    assert result["updated"] and result["local_apply"]["applied"]
    saved = yaml.safe_load(registry.read_text(encoding="utf-8"))
    assert saved["fleets"]["existing"]["defaults"]["hermes"]["config"]["model"]["default"] == "x"

    encoded = surface.encode_deploy_payload({"gateway_model": "model", "env": {"KEY": "value"}})
    assert surface.decode_deploy_payload(encoded)["runtime"]["gateway_model"] == "model"
    assert surface.decode_deploy_payload("") == {"schema": surface.PAYLOAD_SCHEMA}
    with pytest.raises(ValidationError, match="invalid"):
        surface.decode_deploy_payload("not-base64")
    list_payload = base64.b64encode(b"[]").decode("ascii")
    with pytest.raises(ValidationError, match="object"):
        surface.decode_deploy_payload(list_payload)

    input_yaml = tmp_path / "hermes.yaml"
    input_yaml.write_text("gateway_model: model\n", encoding="utf-8")
    assert surface.main(["encode", str(input_yaml)]) == 0
    cli_encoded = capsys.readouterr().out.strip()
    assert surface.decode_deploy_payload(cli_encoded)["runtime"]["gateway_model"] == "model"
    home = tmp_path / "home"
    assert surface.main(["apply", "--payload-b64", cli_encoded, "--hermes-home", str(home)]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is True


def test_slack_account_token_promotion_handles_shapes_and_invalid_files(tmp_path):
    config = {"env": {"SLACK_BOT_TOKEN": "xoxb-existing", "SLACK_APP_TOKEN": "xapp-existing"}}
    surface._promote_slack_accounts_tokens(config, tmp_path)
    assert config["env"]["SLACK_BOT_TOKEN"] == "xoxb-existing"

    accounts = tmp_path / "slack_accounts.json"
    accounts.write_text("not-json", encoding="utf-8")
    config = {}
    surface._promote_slack_accounts_tokens(config, tmp_path)
    assert config == {}
    accounts.write_text(json.dumps({"agents": ["skip", {"bot_token": "bad"}]}), encoding="utf-8")
    surface._promote_slack_accounts_tokens(config, tmp_path)
    assert config == {}
    accounts.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "bot_token": "xoxb-valid",
                        "app_token": "xapp-valid",
                        "user_token": "xoxp-user",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    surface._promote_slack_accounts_tokens(config, tmp_path)
    assert config["env"] == {
        "SLACK_BOT_TOKEN": "xoxb-valid",
        "SLACK_APP_TOKEN": "xapp-valid",
        "SLACK_USER_TOKEN": "xoxp-user",
    }
