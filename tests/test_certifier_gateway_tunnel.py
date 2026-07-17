from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_certifier_gateway_tunnel_is_loopback_only_and_fail_closed() -> None:
    script = (
        ROOT / "deploy" / "openshell" / "install-certifier-gateway-tunnel.sh"
    ).read_text(encoding="utf-8")

    assert '"-L",' in script
    assert 'f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}"' in script
    assert '"BatchMode=yes"' in script
    assert '"PasswordAuthentication=no"' in script
    assert '"KbdInteractiveAuthentication=no"' in script
    assert '"StrictHostKeyChecking=yes"' in script
    assert '"ExitOnForwardFailure=yes"' in script
    assert '"KeepAlive": True' in script
    assert 'OPENSHELL_GATEWAY_ENDPOINT="$endpoint"' in script
    assert '"$OPENSH_BIN" status' in script
    assert "certifier OpenShell tunnel did not become healthy" in script
    assert "openshell gateway select" not in script


def test_linux_gateway_firewall_allows_only_exact_openshell_bridge() -> None:
    bootstrap = (
        ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh"
    ).read_text(encoding="utf-8")

    assert "chain=MAC_OPENSH_GW" in bootstrap
    assert '-i lo -j RETURN' in bootstrap
    assert '-i "$bridge_iface" -j RETURN' in bootstrap
    assert '-i docker0 -j RETURN' not in bootstrap
    assert "-i 'br+' -j RETURN" not in bootstrap
    assert 'network_name="openshell-docker"' in bootstrap
    assert 'bridge_iface="br-${network_id:0:12}"' in bootstrap
    assert '"$ipt" -A "$chain" -j DROP' in bootstrap
    assert '-C INPUT -p tcp --dport 17670 -j "$chain"' in bootstrap
