"""Direct mesh hub paths must not wait for a nonexistent reverse tunnel."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy-mac-fleet.sh"
NODE_INSTALL_SCRIPT = ROOT / "deploy" / "fleet-node-install.sh"


def _script() -> str:
    return (
        DEPLOY_SCRIPT.read_text(encoding="utf-8")
        + "\n"
        + NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    )


def _function(name: str) -> str:
    match = re.search(
        r"^%s\(\) \{\n.*?^}$" % re.escape(name),
        _script(),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    return match.group(0)


def test_direct_mesh_flag_reaches_remote_deploy() -> None:
    text = _script()
    assert 'direct_mesh_hub_flag="${6:-0}"' in text
    assert 'add_remote_env MAC_DEPLOY_DIRECT_HUB "${direct_mesh_hub_flag:-0}"' in text
    assert '"$github_review_key_b64" "$direct_mesh_hub" 1 apply-phase2' in text


def test_direct_hub_guard_precedes_reverse_tunnel_poll() -> None:
    function = _function("wait_for_hub_reverse_tunnel")
    assert function.index("DEPLOY_DIRECT_HUB") < function.index("seq 1 24")
    assert "skipping reverse-tunnel wait" in function


def test_direct_hub_guard_returns_without_polling() -> None:
    function = _function("wait_for_hub_reverse_tunnel")
    snippet = "\n".join(
        [
            "log() { printf '%s\\n' \"$*\"; }",
            "curl() { return 99; }",
            "HUB_TUNNEL_PUBKEY=present",
            "DEPLOY_DIRECT_HUB=1",
            "NETWORK_PROVIDER=tailscale",
            function,
            "wait_for_hub_reverse_tunnel",
        ]
    )
    result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "skipping reverse-tunnel wait" in result.stdout


def test_stopped_spoke_uses_proven_direct_route_for_deploy_health() -> None:
    text = _script()
    start = text.index('log "verifying hub health and local executor startup report"')
    end = text.index('"$PY" - "$LOG_DIR/startup-hermes.json"', start)
    block = text[start:end]

    assert block.count("verify_or_defer_hub_health") == 2
    assert 'curl -fsS "$MAC_HUB_URL/health"' not in block


def test_deploy_health_route_selection_preserves_tunnel_fallback(tmp_path: Path) -> None:
    function = _function("verify_or_defer_hub_health")

    def select(direct: str, deferred: str) -> subprocess.CompletedProcess[str]:
        log_dir = tmp_path / (direct + deferred)
        log_dir.mkdir()
        (log_dir / "health.json").write_text("stale\n", encoding="utf-8")
        result = subprocess.run(
            [
                "bash",
                "-c",
                "\n".join(
                    [
                        'truthy() { [ "$1" = 1 ]; }',
                        "control_plane_enabled() { return 1; }",
                        "log() { printf '%s\\n' \"$*\" >&2; }",
                        "curl() { printf '%s\\n' \"$2\"; }",
                        'DEPLOY_DIRECT_HUB="$1"',
                        'DEFER_AGENT_RESTART="$2"',
                        'HUB_URL="http://10.57.228.137:8789"',
                        'MAC_HUB_URL="http://127.0.0.1:18789"',
                        f'LOG_DIR="{log_dir}"',
                        function,
                        "verify_or_defer_hub_health",
                    ]
                ),
                "select",
                direct,
                deferred,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return result

    direct = select("1", "1")
    assert direct.returncode == 0, direct.stderr
    assert (tmp_path / "11" / "health.json").read_text(encoding="utf-8").strip() == (
        "http://10.57.228.137:8789/health"
    )

    legacy = select("0", "0")
    assert legacy.returncode == 0, legacy.stderr
    assert (tmp_path / "00" / "health.json").read_text(encoding="utf-8").strip() == (
        "http://127.0.0.1:18789/health"
    )

    deferred = select("0", "1")
    assert deferred.returncode == 0, deferred.stderr
    assert deferred.stdout == ""
    assert "deferring tunnel-routed hub health" in deferred.stderr
    assert not (tmp_path / "01" / "health.json").exists()


def test_hub_database_maintenance_explicitly_selects_local_authority() -> None:
    function = _function("mac_authority")
    assert '"$VENV/bin/mac" --local-authority --db "$dsn" "$@"' in function
    assert '"$VENV/bin/mac" --hub-url "$MAC_HUB_URL" "$@"' in function


def test_mac_authority_prefers_mac_database_url_over_mac_db(tmp_path: Path) -> None:
    # deploy_env.py's build_mac_env drops MAC_DB from mac.env whenever a
    # Postgres DSN is configured -- which is now the default, via
    # install_or_validate_control_plane_database's Postgres auto-install
    # (postgres-01). A Postgres-backed hub crashed here with "MAC_DB: unbound
    # variable" (fleet-node-install.sh runs under `set -u`) because
    # mac_authority() still hardcoded --db "$MAC_DB" unconditionally.
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    fake_mac = venv / "mac"
    fake_mac.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n", encoding="utf-8")
    fake_mac.chmod(0o755)

    function = _function("control_plane_enabled") + "\n" + _function("mac_authority")
    snippet = "\n".join(
        [
            "set -u",
            "die() { printf '%s\\n' \"$*\" >&2; return 1; }",
            "AGENT=hazel3",
            "SHARED_SERVICES_MANAGER_AGENT=hazel3",
            "MAC_DATABASE_URL=postgresql://mac:secret@127.0.0.1:5432/mac",
            f"VENV={tmp_path / 'venv'}",
            function,
            "mac_authority admin init",
        ]
    )
    result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "--db postgresql://mac:secret@127.0.0.1:5432/mac admin init" in result.stdout


def test_mac_authority_fails_closed_with_neither_dsn_variable_set() -> None:
    function = _function("control_plane_enabled") + "\n" + _function("mac_authority")
    snippet = "\n".join(
        [
            "set -u",
            "die() { printf '%s\\n' \"$*\" >&2; return 1; }",
            "AGENT=hazel3",
            "SHARED_SERVICES_MANAGER_AGENT=hazel3",
            function,
            "mac_authority admin init",
        ]
    )
    result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "neither MAC_DATABASE_URL nor MAC_DB is set" in result.stderr


def test_first_time_control_plane_init_uses_explicit_schema_migrations() -> None:
    # Deploy owns schema application while the hub is quiesced. It must not
    # route first-time bootstrap through ordinary control-plane startup.
    text = _script()
    assert "mac_authority init" not in text
    assert "mac_authority admin init" not in text
    assert "mac-schema-migrate" in text


def test_reachable_nonmesh_route_is_direct_hub_eligible() -> None:
    function = _function("uses_direct_mesh_hub")
    snippet = "\n".join(
        [
            function,
            'uses_direct_mesh_hub none "http://100.72.16.110:8789"',
        ]
    )
    result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=False)
    assert result.returncode == 0


def test_nonmesh_shared_services_keep_direct_urls_when_proven() -> None:
    text = _script()
    assert 'if [ "$NETWORK_PROVIDER" = "none" ] \\\n  && [ "$DEPLOY_DIRECT_HUB" != "1" ]' in text


def test_remote_fallback_derives_mesh_path_but_not_tunnel_path() -> None:
    text = _script()
    start = text.index('NETWORK_PROVIDER="${MAC_DEPLOY_NETWORK_PROVIDER:-tailscale}"')
    end = text.index("# gketun-02: network=none", start)
    block = text[start:end]

    def derive(provider: str, hub_url: str) -> str:
        result = subprocess.run(
            [
                "bash",
                "-c",
                "\n".join(
                    [
                        'MAC_DEPLOY_DIRECT_HUB=""',
                        'MAC_DEPLOY_NETWORK_PROVIDER="$1"',
                        'HUB_URL="$2"',
                        block,
                        'printf "%s" "$DEPLOY_DIRECT_HUB"',
                    ]
                ),
                "derive",
                provider,
                hub_url,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    assert derive("tailscale", "http://100.72.16.110:8789") == "1"
    assert derive("headscale", "http://hub.example:8789") == "1"
    assert derive("none", "http://127.0.0.1:18789") == "0"
    assert derive("tailscale", "http://127.0.0.1:8789") == "0"
