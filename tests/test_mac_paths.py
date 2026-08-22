"""mac_paths must be behavior-preserving (env unset -> today's literals) AND a
reliable relocation knob (MAC_HOME/HERMES_HOME move everything together)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mac import mac_paths

_HOME_ENV = [
    "MAC_HOME", "HERMES_HOME", "MAC_DB", "MAC_JOURNAL_DIR",
    "MAC_FLEETS_CONFIG", "MAC_DEPLOY_ENV_FILE", "MAC_OPENCLAW_HOST_DIR",
]


@pytest.fixture()
def clean_home(tmp_path, monkeypatch):
    for var in _HOME_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_defaults_match_legacy_literals(clean_home):
    home = clean_home
    assert mac_paths.mac_home() == home / ".mac"
    assert mac_paths.gateway_home() == home / ".mac" / "openclaw"
    assert mac_paths.mac_env_file() == home / ".mac" / "mac.env"
    assert mac_paths.deploy_env_file() == home / ".mac" / ".env"
    assert mac_paths.fleets_config() == home / ".mac" / "fleets.yaml"
    assert mac_paths.ledger_db() == home / ".mac" / "mac.db"
    assert mac_paths.journal_dir() == home / ".mac" / "journal"
    assert mac_paths.gateway_env_file() == home / ".mac" / "openclaw" / ".env"
    assert mac_paths.dream_logs_dir() == home / ".mac" / "openclaw" / "dream_logs"
    assert mac_paths.openclaw_home() == home / ".mac" / "openclaw"


def test_mac_home_relocates_all_derived_paths_together(clean_home, monkeypatch):
    root = clean_home / "relocated"
    monkeypatch.setenv("MAC_HOME", str(root))
    assert mac_paths.mac_home() == root
    assert mac_paths.ledger_db() == root / "mac.db"
    assert mac_paths.journal_dir() == root / "journal"
    assert mac_paths.fleets_config() == root / "fleets.yaml"
    assert mac_paths.openclaw_home() == root / "openclaw"
    # Phase 2 (2026-08-21): the gateway home now relocates WITH MAC_HOME, which
    # is what "all derived paths together" was always supposed to mean. It used
    # to sit outside at ~/.hermes, so moving MAC_HOME left it behind.
    assert mac_paths.gateway_home() == root / "openclaw"


def test_hermes_home_relocates_gateway_paths(clean_home, monkeypatch):
    gw = clean_home / "gw"
    monkeypatch.setenv("HERMES_HOME", str(gw))
    assert mac_paths.gateway_home() == gw
    assert mac_paths.gateway_env_file() == gw / ".env"
    assert mac_paths.dream_logs_dir() == gw / "dream_logs"
    assert mac_paths.legacy_gateway_scripts_dir() == gw / "scripts"


def test_legacy_scripts_stay_on_evicted_hermes_after_phase2(clean_home):
    """Phase 2 moved live gateway state under MAC_HOME; the read-only
    fallback must still name the pre-untangle tree, or unmigrated hosts
    lose ~/.hermes/scripts and the runner fallback becomes a no-op."""
    assert mac_paths.gateway_home() == clean_home / ".mac" / "openclaw"
    assert mac_paths.legacy_gateway_scripts_dir() == clean_home / ".hermes" / "scripts"
    assert mac_paths.legacy_gateway_scripts_dir() != (
        mac_paths.gateway_home() / "scripts"
    )


def test_per_file_overrides_win(clean_home, monkeypatch):
    monkeypatch.setenv("MAC_DB", "/tmp/custom.db")
    monkeypatch.setenv("MAC_JOURNAL_DIR", "/tmp/j")
    monkeypatch.setenv("MAC_FLEETS_CONFIG", "/tmp/f.yaml")
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/tmp/dep.env")
    assert mac_paths.ledger_db() == Path("/tmp/custom.db")
    assert mac_paths.journal_dir() == Path("/tmp/j")
    assert mac_paths.fleets_config() == Path("/tmp/f.yaml")
    assert mac_paths.deploy_env_file() == Path("/tmp/dep.env")


def test_blank_env_falls_back_to_default(clean_home, monkeypatch):
    monkeypatch.setenv("MAC_HOME", "   ")
    assert mac_paths.mac_home() == clean_home / ".mac"


def test_env_paths_are_expanded(clean_home, monkeypatch):
    monkeypatch.setenv("MAC_HOME", "~/somewhere")
    assert mac_paths.mac_home() == Path.home() / "somewhere"
