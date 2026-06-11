"""Tests for fleet-scoped env var resolution (mac-g55y)."""
from __future__ import annotations

from pathlib import Path

import pytest

from mac import fleet_env


def test_scoped_var_normalizes_fleet_name():
    assert fleet_env.scoped_var("MAC_API_TOKEN", "hosta") == "MAC_API_TOKEN__HOSTA"
    assert fleet_env.scoped_var("MAC_API_TOKEN", "devuser-hub") == "MAC_API_TOKEN__DEVUSER_HUB"
    assert fleet_env.scoped_var("MAC_API_TOKEN", "fleet.alpha-1") == "MAC_API_TOKEN__FLEET_ALPHA_1"


def test_scoped_var_rejects_empty_fleet():
    with pytest.raises(ValueError):
        fleet_env.scoped_var("MAC_API_TOKEN", "")
    with pytest.raises(ValueError):
        fleet_env.scoped_var("MAC_API_TOKEN", "---")


def test_resolve_prefers_scoped_form():
    env = {
        "MAC_API_TOKEN": "legacy-flat",
        "MAC_API_TOKEN__HOSTA": "hosta-scoped",
        "MAC_API_TOKEN__DEVUSER_HUB": "jh-scoped",
    }
    assert fleet_env.resolve("MAC_API_TOKEN", fleet="hosta", env=env) == "hosta-scoped"
    assert fleet_env.resolve("MAC_API_TOKEN", fleet="devuser-hub", env=env) == "jh-scoped"


def test_resolve_falls_back_to_legacy_when_no_scoped():
    env = {"MAC_API_TOKEN": "legacy-only"}
    assert fleet_env.resolve("MAC_API_TOKEN", fleet="hosta", env=env) == "legacy-only"


def test_resolve_uses_mac_fleet_env_when_no_explicit_arg():
    env = {
        "MAC_FLEET": "hosta",
        "MAC_API_TOKEN__HOSTA": "hosta-scoped",
        "MAC_API_TOKEN": "legacy",
    }
    assert fleet_env.resolve("MAC_API_TOKEN", env=env) == "hosta-scoped"


def test_resolve_returns_none_when_nothing_set():
    assert fleet_env.resolve("MAC_API_TOKEN", fleet="hosta", env={}) is None


def test_resolve_first_walks_priority_chain():
    env = {
        "MAC_WORKER_TOKEN__HOSTA": "worker-hosta",
        "MAC_API_TOKEN__HOSTA": "api-hosta",
    }
    # MAC_WORKER_TOKEN wins because it's earlier in the list.
    got = fleet_env.resolve_first(["MAC_WORKER_TOKEN", "MAC_API_TOKEN"], fleet="hosta", env=env)
    assert got == "worker-hosta"
    got = fleet_env.resolve_first(["MAC_TOKEN", "MAC_API_TOKEN"], fleet="hosta", env=env)
    assert got == "api-hosta"


def test_parse_env_file_handles_quotes_and_exports(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        '# comment line\n'
        '\n'
        'MAC_API_TOKEN=plain-value\n'
        'export MAC_DEPLOY_HUB_TOKEN="quoted value with spaces"\n'
        "MAC_DEPLOY_ROUTER_DEFAULT_MODEL='single-quoted'\n"
    )
    parsed = fleet_env.parse_env_file(env_path)
    assert parsed["MAC_API_TOKEN"] == "plain-value"
    assert parsed["MAC_DEPLOY_HUB_TOKEN"] == "quoted value with spaces"
    assert parsed["MAC_DEPLOY_ROUTER_DEFAULT_MODEL"] == "single-quoted"


def test_migrate_env_file_adds_scoped_variants(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MAC_API_TOKEN=secret-token\n"
        "MAC_DEPLOY_HUB_TOKEN=hub-token\n"
        "MAC_DB=/tmp/mac.db\n"  # not in FLEET_SCOPED_VARS — left alone
    )
    added, kept = fleet_env.migrate_env_file(env_path, "hosta")
    assert "MAC_API_TOKEN__HOSTA" in added
    assert added["MAC_API_TOKEN__HOSTA"] == "secret-token"
    assert "MAC_DEPLOY_HUB_TOKEN__HOSTA" in added
    assert "MAC_DB__HOSTA" not in added  # shared vars not migrated
    # Legacy keys preserved by default.
    assert kept["MAC_API_TOKEN"] == "secret-token"

    # The rewritten file should have both forms.
    content = env_path.read_text()
    assert "MAC_API_TOKEN=secret-token" in content
    assert "MAC_API_TOKEN__HOSTA=secret-token" in content
    assert "MAC_DB=/tmp/mac.db" in content


def test_migrate_env_file_drops_legacy_when_requested(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("MAC_API_TOKEN=secret-token\n")
    added, kept = fleet_env.migrate_env_file(env_path, "hosta", keep_legacy=False)
    assert added == {"MAC_API_TOKEN__HOSTA": "secret-token"}
    assert kept == {}
    content = env_path.read_text()
    assert "MAC_API_TOKEN=secret-token" not in content
    assert "MAC_API_TOKEN__HOSTA=secret-token" in content


def test_migrate_env_file_is_idempotent(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("MAC_API_TOKEN=secret-token\n")
    fleet_env.migrate_env_file(env_path, "hosta")
    # Second migration for same fleet must not duplicate.
    added2, _ = fleet_env.migrate_env_file(env_path, "hosta")
    assert added2 == {}
    # But a new fleet adds a new scoped key.
    added3, _ = fleet_env.migrate_env_file(env_path, "devuser-hub")
    assert "MAC_API_TOKEN__DEVUSER_HUB" in added3


def test_two_fleets_in_one_env_file_do_not_collide(tmp_path: Path):
    """Acceptance scenario from mac-g55y: after migration, a workstation
    that participates in both hosta and devuser-hub keeps both tokens
    distinct and addressable.
    """
    env_path = tmp_path / ".env"
    env_path.write_text("MAC_API_TOKEN=hosta-token\n")
    fleet_env.migrate_env_file(env_path, "hosta", keep_legacy=False)
    # Now devuser-hub's setup writes its token — into the SCOPED form,
    # not the legacy form. (This mirrors what the updated setup-fleet.py
    # would do.)
    contents = env_path.read_text() + "MAC_API_TOKEN__DEVUSER_HUB=jh-token\n"
    env_path.write_text(contents)
    parsed = fleet_env.parse_env_file(env_path)
    assert parsed["MAC_API_TOKEN__HOSTA"] == "hosta-token"
    assert parsed["MAC_API_TOKEN__DEVUSER_HUB"] == "jh-token"
    # And resolve picks the right one per fleet.
    assert fleet_env.resolve("MAC_API_TOKEN", fleet="hosta", env=parsed) == "hosta-token"
    assert fleet_env.resolve("MAC_API_TOKEN", fleet="devuser-hub", env=parsed) == "jh-token"
