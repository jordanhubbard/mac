"""Direct mesh hub paths must not wait for a nonexistent reverse tunnel."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy-mac-fleet.sh"


def _script() -> str:
    return DEPLOY_SCRIPT.read_text(encoding="utf-8")


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
