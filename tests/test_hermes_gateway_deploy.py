"""Contract tests for MAC's host-level Hermes chat gateway installer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.process_e2e

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "hermes" / "install-hermes-gateway.sh"

FAKE_HERMES = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["FAKE_HERMES_CALLS"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")

scenario = os.environ.get("FAKE_HERMES_SCENARIO", "healthy")

if args[:2] == ["gateway", "status"]:
    if scenario == "unsupervised":
        print("Gateway is running as a detached process (not supervised).")
    elif scenario == "unhealthy":
        print("Gateway is supervised by launchd, but reports Unhealthy.")
    else:
        print("Gateway is supervised by launchd and Running.")
    raise SystemExit(0)

raise SystemExit(0)
"""

FAKE_MAC = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["FAKE_MAC_CALLS"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
raise SystemExit(0)
"""


def _prepare_bin(tmp_path: Path, calls_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_hermes = bin_dir / "hermes"
    fake_hermes.write_text(FAKE_HERMES, encoding="utf-8")
    fake_hermes.chmod(0o755)
    fake_mac = bin_dir / "mac"
    fake_mac.write_text(FAKE_MAC, encoding="utf-8")
    fake_mac.chmod(0o755)
    return bin_dir


def _run(
    tmp_path: Path,
    subcommand: str,
    *,
    scenario: str = "healthy",
    extra_env: dict | None = None,
) -> tuple[subprocess.CompletedProcess[str], list]:
    calls_path = tmp_path / "hermes-calls.jsonl"
    calls_path.write_text("", encoding="utf-8")
    mac_calls_path = tmp_path / "mac-calls.jsonl"
    mac_calls_path.write_text("", encoding="utf-8")
    bin_dir = _prepare_bin(tmp_path, calls_path)
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "HERMES_HOME": str(home / ".hermes"),
        "MAC_HERMES_OPENCLAW_SOURCE": str(home / "no-openclaw-here"),
        "FAKE_HERMES_CALLS": str(calls_path),
        "FAKE_HERMES_SCENARIO": scenario,
        "FAKE_MAC_CALLS": str(mac_calls_path),
    }
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["bash", str(INSTALLER), subcommand],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines() if line
    ]
    return result, calls


def test_verify_passes_when_gateway_is_supervised(tmp_path):
    result, calls = _run(tmp_path, "verify", scenario="healthy")
    assert result.returncode == 0, result.stderr
    assert ["gateway", "status", "--deep"] in calls


def test_verify_fails_when_gateway_is_unsupervised(tmp_path):
    result, _calls = _run(tmp_path, "verify", scenario="unsupervised")
    assert result.returncode != 0
    assert "supervised" in result.stderr.lower()


def test_verify_fails_when_gateway_is_unhealthy(tmp_path):
    result, _calls = _run(tmp_path, "verify", scenario="unhealthy")
    assert result.returncode != 0
    assert "not healthy" in result.stderr.lower()


def test_withdraw_stops_the_gateway_without_uninstalling(tmp_path):
    result, calls = _run(tmp_path, "withdraw")
    assert result.returncode == 0, result.stderr
    assert ["gateway", "stop"] in calls
    assert not any(call[:2] == ["gateway", "uninstall"] for call in calls)


def test_configure_gateway_sets_only_provided_fields(tmp_path):
    result, calls = _run(
        tmp_path,
        "prepare",
        extra_env={
            "MAC_HERMES_GATEWAY_MODEL": "azure/anthropic/claude-sonnet-4-6",
            "MAC_HERMES_GATEWAY_PROVIDER": "custom",
            "MAC_HERMES_GATEWAY_BASE_URL": "",
        },
    )
    assert result.returncode == 0, result.stderr
    assert ["config", "set", "model", "azure/anthropic/claude-sonnet-4-6", "--force"] in calls
    assert ["config", "set", "provider", "custom", "--force"] in calls
    assert not any(call[:3] == ["config", "set", "base_url"] for call in calls)


def test_configure_gateway_always_requires_mention_by_default(tmp_path):
    _result, calls = _run(tmp_path, "prepare")
    assert ["config", "set", "slack.require_mention", "true", "--force"] in calls


def test_free_response_channel_is_set_when_directory_resolves_the_name(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    directory = hermes_home / "channel_directory.json"
    directory.write_text(
        json.dumps({"channels": [{"name": "rockyandfriends", "id": "C0AMSBEU7CJ"}]}),
        encoding="utf-8",
    )
    result, calls = _run(
        tmp_path,
        "prepare",
        extra_env={"MAC_HERMES_SLACK_HOME_CHANNEL_NAME": "rockyandfriends"},
    )
    assert result.returncode == 0, result.stderr
    assert [
        "config",
        "set",
        "slack.free_response_channels",
        "C0AMSBEU7CJ",
        "--force",
    ] in calls


def test_free_response_channel_is_skipped_gracefully_when_unresolvable(tmp_path):
    result, calls = _run(
        tmp_path,
        "prepare",
        extra_env={"MAC_HERMES_SLACK_HOME_CHANNEL_NAME": "somechannel"},
    )
    assert result.returncode == 0, result.stderr
    assert not any(call[:3] == ["config", "set", "slack.free_response_channels"] for call in calls)


def test_prepare_skips_install_when_hermes_already_on_path(tmp_path):
    result, calls = _run(tmp_path, "prepare")
    assert result.returncode == 0, result.stderr
    # No shell-installer invocation is observable through the fake hermes
    # binary's own call log (it's already "installed"); prepare should reach
    # gateway install regardless.
    assert ["gateway", "install", "--force", "--start-now", "--start-on-login"] in calls


def test_prepare_ports_credentials_via_mac_human_interface(tmp_path):
    result, calls = _run(tmp_path, "prepare")
    assert result.returncode == 0, result.stderr
    mac_calls_path = tmp_path / "mac-calls.jsonl"
    mac_calls = [
        json.loads(line) for line in mac_calls_path.read_text(encoding="utf-8").splitlines() if line
    ]
    assert [
        "human-interface",
        "port",
        "--from",
        "openclaw",
        "--to",
        "hermes",
        "--apply",
    ] in mac_calls


def test_prepare_migrates_claw_state_when_openclaw_home_present(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    openclaw_home = home / ".openclaw"
    openclaw_home.mkdir(parents=True, exist_ok=True)
    result, calls = _run(
        tmp_path,
        "prepare",
        extra_env={"MAC_HERMES_OPENCLAW_SOURCE": str(openclaw_home)},
    )
    assert result.returncode == 0, result.stderr
    assert [
        "claw",
        "migrate",
        "--source",
        str(openclaw_home),
        "--preset",
        "full",
        "--overwrite",
        "--yes",
    ] in calls


def test_prepare_skips_claw_migrate_when_no_openclaw_home(tmp_path):
    result, calls = _run(tmp_path, "prepare")
    assert result.returncode == 0, result.stderr
    assert not any(call[:2] == ["claw", "migrate"] for call in calls)


def test_finalize_runs_verify(tmp_path):
    result, calls = _run(tmp_path, "finalize", scenario="healthy")
    assert result.returncode == 0, result.stderr
    assert ["gateway", "status", "--deep"] in calls


def test_finalize_fails_when_gateway_unhealthy(tmp_path):
    result, _calls = _run(tmp_path, "finalize", scenario="unhealthy")
    assert result.returncode != 0


def test_unknown_subcommand_fails_closed(tmp_path):
    result, _calls = _run(tmp_path, "bogus")
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()
