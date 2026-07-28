"""Coding-CLI credential fabric (mac.cli_credentials).

Workstation = source of truth; secrets travel over fleet SSH stdin only;
every sync is verified by the worker's own detector."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac import cli_credentials as cc


def _home(tmp_path: Path, *, codex: bool = False, claude_file: bool = False) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    if codex:
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text('{"tokens": "x"}')
        (home / ".codex" / "config.toml").write_text("profile = 'default'\n")
    if claude_file:
        (home / ".claude").mkdir()
        (home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": {}}')
    return home


def test_detects_codex_files_and_claude_credentials_file(tmp_path):
    home = _home(tmp_path, codex=True, claude_file=True)
    sources = cc.detect_local_credentials(environ={}, home=home, keychain=lambda s: "")
    assert sources["codex"].present
    assert set(sources["codex"].files) == {".codex/auth.json", ".codex/config.toml"}
    assert sources["claude"].present
    assert ".claude/.credentials.json" in sources["claude"].files
    assert not sources["cursor"].present


def test_env_keys_win_and_keychain_backfills(tmp_path):
    home = _home(tmp_path)
    keychain = {
        cc.CLAUDE_KEYCHAIN_SERVICE: "sk-ant-api03-KEY",
        cc.CURSOR_KEYCHAIN_SERVICE: "cursor-token-abc",
    }
    sources = cc.detect_local_credentials(
        environ={}, home=home, keychain=lambda s: keychain.get(s, "")
    )
    assert sources["claude"].env == {"ANTHROPIC_API_KEY": "sk-ant-api03-KEY"}
    assert "Keychain" in sources["claude"].origin
    assert sources["cursor"].env == {"CURSOR_AUTH_TOKEN": "cursor-token-abc"}

    explicit = cc.detect_local_credentials(
        environ={"ANTHROPIC_API_KEY": "sk-ant-explicit", "CURSOR_API_KEY": "cur-env"},
        home=home,
        keychain=lambda s: keychain.get(s, ""),
    )
    assert explicit["claude"].env == {"ANTHROPIC_API_KEY": "sk-ant-explicit"}
    assert explicit["claude"].origin == "ANTHROPIC_API_KEY (env)"
    assert explicit["cursor"].env == {"CURSOR_API_KEY": "cur-env"}


def test_cursor_auth_token_precedes_api_key_and_matches_cli_semantics(tmp_path):
    sources = cc.detect_local_credentials(
        environ={
            "CURSOR_AUTH_TOKEN": "browser-login-token",
            "CURSOR_API_KEY": "generated-api-key",
        },
        home=_home(tmp_path),
        keychain=lambda s: "",
    )
    assert sources["cursor"].env == {"CURSOR_AUTH_TOKEN": "browser-login-token"}
    assert sources["cursor"].origin == "CURSOR_AUTH_TOKEN (env)"


def test_keychain_oauth_json_materializes_credentials_file(tmp_path):
    home = _home(tmp_path)
    oauth_blob = '{"claudeAiOauth": {"accessToken": "at"}}'
    sources = cc.detect_local_credentials(
        environ={},
        home=home,
        keychain=lambda s: oauth_blob if s == cc.CLAUDE_KEYCHAIN_SERVICE else "",
    )
    assert sources["claude"].files[".claude/.credentials.json"] == oauth_blob.encode()


def test_manifest_carries_files_b64_and_env(tmp_path):
    home = _home(tmp_path, codex=True)
    sources = cc.detect_local_credentials(
        environ={"ANTHROPIC_API_KEY": "sk-ant-x"}, home=home, keychain=lambda s: ""
    )
    manifest = cc.build_sync_manifest({k: v for k, v in sources.items() if v.present})
    assert manifest["schema"] == "mac.cli_credentials_sync.v1"
    assert base64.b64decode(manifest["files"][".codex/auth.json"]) == b'{"tokens": "x"}'
    assert manifest["env"] == {"ANTHROPIC_API_KEY": "sk-ant-x"}


def _fleets_yaml(tmp_path: Path) -> str:
    path = tmp_path / "fleets.yaml"
    path.write_text(
        "fleets:\n"
        "  demo:\n"
        "    fleet_name: demo\n"
        "    hub_agent: hub\n"
        "    agents:\n"
        "      - name: hub\n"
        "        target: user@hub.local\n"
        "      - name: worker1\n"
        "        target: user@w1.local\n",
        encoding="utf-8",
    )
    return str(path)


def test_sync_agent_ships_manifest_on_stdin_and_parses_verdict(tmp_path):
    calls = {}

    def runner(argv, input):
        calls["argv"] = argv
        calls["stdin"] = input
        report = {"schema": "mac.cli_credentials_apply.v1", "clis": {"codex": {"available": True}}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(report) + "\n", stderr="")

    manifest = {"schema": "mac.cli_credentials_sync.v1", "files": {}, "env": {"X_KEY": "secret"}}
    verdict = cc.sync_agent(
        "demo", "worker1", manifest, fleets_config=_fleets_yaml(tmp_path), runner=runner
    )
    assert verdict["codex"]["available"] is True
    # Secrets travel on stdin only — never in the argv.
    assert "secret" in calls["stdin"]
    assert all("secret" not in str(part) for part in calls["argv"])
    assert calls["argv"][0] == "ssh"
    assert "user@w1.local" in calls["argv"]


def test_sync_agent_fails_loudly_on_remote_error(tmp_path):
    def runner(argv, input):
        return SimpleNamespace(returncode=12, stdout="", stderr="permission denied")

    with pytest.raises(cc.CliCredentialError, match="permission denied"):
        cc.sync_agent(
            "demo", "worker1", {"schema": "mac.cli_credentials_sync.v1"},
            fleets_config=_fleets_yaml(tmp_path), runner=runner,
        )


def test_remote_apply_script_writes_files_env_and_reports(tmp_path, monkeypatch):
    """Execute the actual remote-apply script body in-process against a fake HOME."""
    home = tmp_path / "worker-home"
    (home / ".mac").mkdir(parents=True)
    (home / ".mac" / "mac.env").write_text("MAC_API_TOKEN=tok\n")
    monkeypatch.setenv("HOME", str(home))
    # The apply script os.environ.update()s the synced keys (desired on the
    # worker). Register the key with monkeypatch FIRST so teardown restores
    # it — otherwise it leaks into os.environ and flips coding-CLI detection
    # for unrelated tests later in the suite.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    manifest = cc.build_sync_manifest(
        {
            "codex": cc.CredentialSource(
                cli="codex", origin="t", files={".codex/auth.json": b'{"t":1}'}
            ),
            "claude": cc.CredentialSource(
                cli="claude", origin="t", env={"ANTHROPIC_API_KEY": "sk-ant-1"}
            ),
        }
    )
    import io
    import sys as _sys

    stdin, stdout = io.StringIO(json.dumps(manifest)), io.StringIO()
    monkeypatch.setattr(_sys, "stdin", stdin)
    monkeypatch.setattr(_sys, "stdout", stdout)
    exec(compile(cc._REMOTE_APPLY, "<remote-apply>", "exec"), {"__name__": "__main__"})

    auth = home / ".codex" / "auth.json"
    assert auth.read_bytes() == b'{"t":1}'
    assert oct(auth.stat().st_mode & 0o777) == "0o600"
    env_text = (home / ".mac" / "mac.env").read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-1" in env_text
    assert "MAC_API_TOKEN=tok" in env_text  # existing entries preserved
    report = json.loads(stdout.getvalue().strip().splitlines()[-1])
    assert report["schema"] == "mac.cli_credentials_apply.v1"
    assert set(report["clis"]) == {"claude", "codex", "cursor"}


def test_remote_apply_rejects_traversal_paths(tmp_path, monkeypatch):
    home = tmp_path / "worker-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    manifest = {
        "schema": "mac.cli_credentials_sync.v1",
        "files": {"../evil": base64.b64encode(b"x").decode()},
        "env": {},
    }
    import io
    import sys as _sys

    monkeypatch.setattr(_sys, "stdin", io.StringIO(json.dumps(manifest)))
    with pytest.raises(SystemExit, match="refusing manifest path"):
        exec(compile(cc._REMOTE_APPLY, "<remote-apply>", "exec"), {"__name__": "__main__"})


def test_agents_needing_sync_reads_heartbeat_reports():
    agents = [
        {
            "name": "w1",
            "resources": {
                "coding_clis": {
                    "clis": {
                        "claude": {"on_path": True, "available": False},
                        "codex": {"on_path": True, "available": True},
                        "cursor": {"on_path": False, "available": False},
                    }
                }
            },
        },
        {"name": "w2", "resources": {}},  # never reported -> unknown, not needy
    ]
    assert cc.agents_needing_sync(agents) == {"w1": ["claude"]}


def test_agents_needing_sync_v2_uses_configured_not_executable_proof():
    """v2 gates ``available`` on the executable probe. Needing-sync means the
    credential is missing (``configured`` False), not merely that the same-
    environment probe has not verified an already-credentialed route."""
    agents = [
        {
            "name": "w1",
            "resources": {
                "coding_clis": {
                    "schema": "mac.coding_clis.v2",
                    "clis": {
                        # On PATH, no credential -> genuinely needs a sync.
                        "claude": {
                            "on_path": True,
                            "configured": False,
                            "available": False,
                        },
                        # On PATH + credentialed but not yet verified by the
                        # probe -> a route/sandbox concern, NOT missing secrets.
                        "codex": {
                            "on_path": True,
                            "configured": True,
                            "available": False,
                            "verified": False,
                        },
                        "cursor": {"on_path": False, "configured": False},
                    },
                }
            },
        }
    ]
    assert cc.agents_needing_sync(agents) == {"w1": ["claude"]}


def test_agents_needing_sync_includes_rejected_credentials_not_route_failures():
    agents = [
        {
            "name": "w1",
            "resources": {
                "coding_clis": {
                    "schema": "mac.coding_clis.v2",
                    "clis": {
                        "cursor": {
                            "on_path": True,
                            "configured": True,
                            "available": False,
                            "verification": {
                                "failure_class": "authentication_failed",
                            },
                        },
                        "claude": {
                            "on_path": True,
                            "configured": True,
                            "available": False,
                            "verification": {
                                "failure_class": "sandbox_proxy_unreachable",
                            },
                        },
                    },
                }
            },
        }
    ]
    assert cc.agents_needing_sync(agents) == {"w1": ["cursor"]}


def test_codex_config_model_pin_is_stripped_but_provider_config_kept(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"tokens": "x"}')
    (home / ".codex" / "config.toml").write_text(
        'model = "gpt-5.6-sol"\n'
        'model_reasoning_effort = "xhigh"\n'
        'preferred_auth_method = "chatgpt"\n'
        "\n"
        '[model_providers.custom]\n'
        'name = "custom"\n'
        'base_url = "https://example/v1"\n'
        'model = "keep-me-inside-table"\n'
    )
    sources = cc.detect_local_credentials(environ={}, home=home, keychain=lambda s: "")
    config = sources["codex"].files[".codex/config.toml"].decode()
    # Top-level version-specific pins removed...
    assert "gpt-5.6-sol" not in config
    assert "model_reasoning_effort" not in config
    # ...but non-model top-level keys and scoped provider config preserved.
    assert 'preferred_auth_method = "chatgpt"' in config
    assert "[model_providers.custom]" in config
    assert 'base_url = "https://example/v1"' in config
    assert "keep-me-inside-table" in config  # model= inside a table is untouched
