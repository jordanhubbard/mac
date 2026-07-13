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
    assert (
        'add_remote_env MAC_DEPLOY_DIRECT_HUB "${direct_mesh_hub_flag:-0}"' in text
    )
    assert (
        '"$allow_degraded_services" "$github_review_key_b64" "$direct_mesh_hub"'
        in text
    )


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
    result = subprocess.run(
        ["bash", "-c", snippet], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert "skipping reverse-tunnel wait" in result.stdout


def test_hub_database_maintenance_explicitly_selects_local_authority() -> None:
    function = _function("mac_authority")
    assert '"$VENV/bin/mac" --local-authority --db "$MAC_DB" "$@"' in function
    assert '"$VENV/bin/mac" --hub-url "$MAC_HUB_URL" "$@"' in function


def test_reachable_nonmesh_route_is_direct_hub_eligible() -> None:
    function = _function("uses_direct_mesh_hub")
    snippet = "\n".join(
        [
            function,
            'uses_direct_mesh_hub none "http://100.72.16.110:8789"',
        ]
    )
    result = subprocess.run(
        ["bash", "-c", snippet], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0


def test_nonmesh_shared_services_keep_direct_urls_when_proven() -> None:
    text = _script()
    assert (
        'if [ "$NETWORK_PROVIDER" = "none" ] \\\n  && [ "$DEPLOY_DIRECT_HUB" != "1" ]' in text
    )


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
