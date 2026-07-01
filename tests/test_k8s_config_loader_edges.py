"""Schema rejection coverage for the unified Kubernetes config loader."""

from __future__ import annotations

import pytest
import yaml

from mac.k8s import config_loader


def _valid():
    return {
        "mac_url": "http://mac/",
        "dispatcher": {"machine": {"id": "m"}, "agent": {"id": "a"}},
        "role_machines": [],
        "roles": {},
        "capability_role_aliases": {},
        "projects": [],
        "notifier_channels": [],
    }


def _write(tmp_path, value):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(value))
    return path


def test_missing_unreadable_invalid_yaml_and_non_mapping(monkeypatch, tmp_path) -> None:
    with pytest.raises(SystemExit, match="is missing"):
        config_loader.load_config_file(str(tmp_path / "missing"))
    path = tmp_path / "bad.yaml"
    path.write_text("[unterminated")
    with pytest.raises(SystemExit, match="not valid YAML"):
        config_loader.load_config_file(str(path))
    path.write_text("[]")
    with pytest.raises(SystemExit, match="must decode to a mapping"):
        config_loader.load_config_file(str(path))
    monkeypatch.setattr("builtins.open", lambda *_a, **_k: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(SystemExit, match="could not be read"):
        config_loader.load_config_file(str(path))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(mac_url=""), "mac_url"),
        (lambda d: d.update(dispatcher={}), "dispatcher"),
        (lambda d: d.update(dispatcher={"machine": {}, "agent": "bad"}), "machine, agent"),
        (lambda d: d.update(role_machines={"x": 1}), "role_machines must be a list"),
        (lambda d: d.update(role_machines=["bad"]), r"role_machines\[0\]"),
        (lambda d: d.update(roles=["bad"]), "roles must be a mapping"),
        (lambda d: d.update(capability_role_aliases=["bad"]), "capability_role_aliases"),
        (lambda d: d.update(projects={"x": 1}), "projects must be a list"),
        (lambda d: d.update(projects=["bad"]), r"projects\[0\]"),
        (lambda d: d.update(projects=[{"name": "one", "metadata": ["bad"]}]), "metadata must be a mapping"),
        (lambda d: d.update(attestation_keys=[]), "attestation_keys must be a mapping"),
        (lambda d: d.update(attestation_keys={}), "attestation_keys requires"),
        (lambda d: d.update(fleet=[]), "fleet must be a mapping"),
        (lambda d: d.update(fleet={}), "fleet requires"),
        (lambda d: d.update(notifier_channels={"x": 1}), "notifier_channels must be a list"),
    ],
)
def test_top_level_schema_rejections(tmp_path, mutate, message) -> None:
    data = _valid()
    mutate(data)
    with pytest.raises(SystemExit, match=message):
        config_loader.load_config_file(str(_write(tmp_path, data)))


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("bad", "must be a mapping"),
        ({"capabilities": []}, "capabilities must be non-empty"),
        ({"capabilities": "bad"}, "requires a list"),
        ({"capabilities": ["python"], "attestation_key_secret": {}}, "attestation_key_secret"),
        ({
            "capabilities": ["python"], "attestation_key_secret": {"name": "s", "key": "k"},
            "agent_id": "a", "name": "A", "machine_id": "m", "image": "i", "executor": "e",
            "required_capabilities": "bad",
        }, "required_capabilities"),
    ],
)
def test_role_schema_rejections(raw, message) -> None:
    with pytest.raises(SystemExit, match=message):
        config_loader._parse_role("role", raw)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("bad", "must be a mapping"),
        ({"name": "n", "channel_type": "email"}, "channel_type must be one"),
        ({"name": "n", "channel_type": "slack", "event_types": "bad"}, "event_types must be a list"),
        ({"name": "n", "channel_type": "slack", "target": ["bad"]}, "target must be a mapping"),
        ({"name": "n", "channel_type": "slack", "metadata": ["bad"]}, "metadata must be a mapping"),
    ],
)
def test_notifier_schema_rejections(raw, message) -> None:
    with pytest.raises(SystemExit, match=message):
        config_loader._parse_notifier_channel(0, raw)


def test_full_optional_config_loads(tmp_path) -> None:
    data = _valid()
    data.update({
        "attestation_keys": {"namespace": "ns", "secret_name": "secret"},
        "fleet": {"name": "fleet", "description": "d"},
        "projects": [{"name": "project", "metadata": {"x": 1}}],
        "notifier_channels": [{"name": "alerts", "channel_type": "slack", "enabled": False}],
    })
    loaded = config_loader.load_config_file(str(_write(tmp_path, data)))
    assert loaded.mac_url == "http://mac"
    assert loaded.fleet["name"] == "fleet"
    assert loaded.notifier_channels[0].enabled is False
