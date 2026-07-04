"""Small validation and formatting edges for fleet deployment helpers."""

from __future__ import annotations

import pytest

from mac.fleet_deploy import (
    SshTarget,
    canonicalize_mesh_ssh_target,
    parse_ssh_target,
    shell_words,
)


MESH_STATUS = {
    "BackendState": "Running",
    "Self": {
        "HostName": "operator",
        "DNSName": "operator.example.ts.net.",
        "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0::1"],
    },
    "Peer": {
        "node-key": {
            "HostName": "puck",
            "DNSName": "puck.example.ts.net.",
            "TailscaleIPs": ["100.72.16.110", "fd7a:115c:a1e0::2"],
        }
    },
}


def test_ssh_target_properties_and_shell_words() -> None:
    target = SshTarget("operator@hub", port=2222)
    assert target.ssh_target == "operator@hub"
    assert target.scp_target_prefix == "operator@hub"
    assert shell_words(["ssh", target.ssh_target]) == "ssh operator@hub"


@pytest.mark.parametrize(
    ("value", "port", "message"),
    [
        (" ", None, "required"),
        ("operator@hub", 0, "positive"),
    ],
)
def test_parse_ssh_target_rejects_invalid_values(value, port, message) -> None:
    with pytest.raises(ValueError, match=message):
        parse_ssh_target(value, port=port)


def test_mesh_target_replaces_only_local_mdns_and_preserves_user_and_port() -> None:
    assert canonicalize_mesh_ssh_target(
        "jkh@puck.local:2201",
        provider="tailscale",
        status=MESH_STATUS,
    ) == "jkh@100.72.16.110:2201"
    assert canonicalize_mesh_ssh_target(
        "puck.local",
        provider="headscale",
        status=MESH_STATUS,
    ) == "100.72.16.110"
    assert canonicalize_mesh_ssh_target(
        "jkh@host.example.com:2201",
        provider="none",
    ) == "jkh@host.example.com:2201"
    assert canonicalize_mesh_ssh_target(
        "jkh@192.0.2.10",
        provider="none",
    ) == "jkh@192.0.2.10"


def test_mesh_target_rejects_local_mdns_without_a_resolvable_mesh_peer() -> None:
    with pytest.raises(ValueError, match="configure tailscale/headscale"):
        canonicalize_mesh_ssh_target("puck.local", provider="none")
    with pytest.raises(ValueError, match="no tailscale/headscale peer"):
        canonicalize_mesh_ssh_target(
            "missing.local",
            provider="tailscale",
            status=MESH_STATUS,
        )
