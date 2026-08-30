"""Fail-closed bind policy for Tailscale/Headscale hubs."""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from mac.mesh_bind import (
    MeshBindError,
    bind_sockets,
    deploy_mac_bind_host,
    is_allowed_mesh_bind_host,
    lookup_tailscale_ipv4,
    parse_bind_hosts,
    runtime_bind_error,
    serve_bind_hosts,
)
from mac.models import ValidationError
from mac.services import ControlPlane


def test_parse_bind_hosts_splits_and_defaults() -> None:
    assert parse_bind_hosts("") == ["127.0.0.1"]
    assert parse_bind_hosts("127.0.0.1,100.64.0.1") == ["127.0.0.1", "100.64.0.1"]
    assert parse_bind_hosts("127.0.0.1, 127.0.0.1") == ["127.0.0.1"]


def test_cgnat_and_loopback_are_mesh_safe_unspecified_is_not() -> None:
    assert is_allowed_mesh_bind_host("127.0.0.1")
    assert is_allowed_mesh_bind_host("100.64.1.8")
    assert is_allowed_mesh_bind_host("fd7a:115c:a1e0::1")
    assert not is_allowed_mesh_bind_host("0.0.0.0")
    assert not is_allowed_mesh_bind_host("::")
    assert not is_allowed_mesh_bind_host("10.0.0.5")
    assert not is_allowed_mesh_bind_host("8.8.8.8")
    assert is_allowed_mesh_bind_host("10.0.0.5", mesh_ips=("10.0.0.5",))


def test_deploy_rewrites_wildcard_when_tailscale_ip_known() -> None:
    assert (
        deploy_mac_bind_host(
            "0.0.0.0",
            network_provider="tailscale",
            is_hub=True,
            tailscale_ip="100.72.16.110",
        )
        == "127.0.0.1,100.72.16.110"
    )


def test_deploy_refuses_wildcard_without_tailscale_ip() -> None:
    with pytest.raises(MeshBindError, match="will not bind 0.0.0.0"):
        deploy_mac_bind_host(
            "0.0.0.0",
            network_provider="headscale",
            is_hub=True,
            tailscale_ip="",
        )


def test_deploy_refuses_lan_bind_on_mesh_hub() -> None:
    with pytest.raises(MeshBindError, match="10.0.0.8"):
        deploy_mac_bind_host(
            "10.0.0.8",
            network_provider="tailscale",
            is_hub=True,
            tailscale_ip="100.64.0.1",
        )


def test_deploy_leaves_non_mesh_wildcard_alone() -> None:
    assert (
        deploy_mac_bind_host(
            "0.0.0.0",
            network_provider="none",
            is_hub=True,
            tailscale_ip="",
        )
        == "0.0.0.0"
    )


def test_deploy_forces_spoke_loopback() -> None:
    assert (
        deploy_mac_bind_host(
            "0.0.0.0",
            network_provider="tailscale",
            is_hub=False,
            tailscale_ip="100.64.0.1",
        )
        == "127.0.0.1"
    )


def test_runtime_error_for_mesh_wildcard() -> None:
    err = runtime_bind_error(
        bind_host="0.0.0.0",
        network_provider="tailscale",
    )
    assert err is not None
    assert "0.0.0.0" in err
    assert runtime_bind_error(bind_host="127.0.0.1", network_provider="tailscale") is None
    assert runtime_bind_error(bind_host="0.0.0.0", network_provider="") is None


def test_serve_mesh_hub_uses_live_ip_not_wildcard() -> None:
    hosts = serve_bind_hosts(
        "0.0.0.0",
        network_provider="tailscale",
        is_hub=True,
        environ={"MAC_TAILSCALE_IP": "100.64.9.9"},
        lookup=lambda environ=None: "100.64.9.9",
    )
    assert hosts == ["127.0.0.1", "100.64.9.9"]


def test_serve_mesh_hub_without_ip_refuses() -> None:
    with pytest.raises(MeshBindError, match="will not bind 0.0.0.0"):
        serve_bind_hosts(
            "127.0.0.1",
            network_provider="tailscale",
            is_hub=True,
            environ={},
            lookup=lambda environ=None: "",
        )


def test_lookup_prefers_tailscale_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="100.64.0.2\n")

    assert lookup_tailscale_ipv4(environ={"MAC_TAILSCALE_IP": "100.64.0.1"}, run=fake_run) == (
        "100.64.0.2"
    )


def test_lookup_falls_back_to_env_when_cli_missing() -> None:
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError("tailscale")

    assert (
        lookup_tailscale_ipv4(environ={"MAC_TAILSCALE_IP": "100.64.0.3"}, run=fake_run)
        == "100.64.0.3"
    )


def test_bind_sockets_loopback_ephemeral() -> None:
    sockets = bind_sockets(["127.0.0.1"], 0)
    try:
        assert sockets[0].getsockname()[0] == "127.0.0.1"
        assert sockets[0].getsockname()[1] > 0
    finally:
        for sock in sockets:
            sock.close()


def test_bind_sockets_can_listen_on_two_loopback_aliases() -> None:
    """Same port, two addresses — the pattern used for 127.0.0.1 + CGNAT."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    sockets = bind_sockets(["127.0.0.1"], port)
    try:
        assert sockets[0].getsockname()[1] == port
    finally:
        for sock in sockets:
            sock.close()


def test_create_app_refuses_mesh_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    from mac.api import create_app

    monkeypatch.setenv("MAC_API_TOKEN", "tok")
    monkeypatch.setenv("MAC_LOCAL_CONSOLE_ENABLED", "0")
    monkeypatch.setenv("MAC_NETWORK_PROVIDER", "tailscale")
    monkeypatch.setenv("MAC_BIND_HOST", "0.0.0.0")
    with pytest.raises(ValidationError, match="mesh bind refused"):
        create_app(control_plane=ControlPlane.in_memory())


def test_create_app_allows_mesh_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    from mac.api import create_app

    monkeypatch.setenv("MAC_API_TOKEN", "tok")
    monkeypatch.setenv("MAC_LOCAL_CONSOLE_ENABLED", "0")
    monkeypatch.setenv("MAC_NETWORK_PROVIDER", "tailscale")
    monkeypatch.setenv("MAC_BIND_HOST", "127.0.0.1")
    create_app(control_plane=ControlPlane.in_memory())


def test_build_mac_env_expands_mesh_hub_bind(tmp_path) -> None:
    from mac import deploy_env

    cfg = deploy_env.DeployEnvConfig(
        paths=deploy_env.DeployPaths(tmp_path / "mac.env", tmp_path / ".mac", tmp_path),
        control=deploy_env.ControlConfig(
            port="8789",
            hub_url="http://100.64.0.1:8789",
            hub_token="hub-token",
            bind_host="0.0.0.0",
            supervisor_kind="systemd",
            network_provider="tailscale",
        ),
        gateway=deploy_env.GatewayConfig("", "", "", ""),
        worker=deploy_env.WorkerConfig("loop", "python", "", "", "0"),
        services=deploy_env.SharedServicesConfig("", "6333", "", "3002"),
        identity=deploy_env.DeployIdentity("hub", "hub", "fleet"),
    )
    values = deploy_env.build_mac_env(
        {},
        cfg,
        environ={"MAC_TAILSCALE_IP": "100.64.0.1", "MAC_API_ALLOW_OPEN": "1"},
    )
    assert values["MAC_BIND_HOST"] == "127.0.0.1,100.64.0.1"
    assert values["MAC_NETWORK_PROVIDER"] == "tailscale"
