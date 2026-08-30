"""Fail-closed hub bind policy for Tailscale/Headscale fleets.

The overlay encrypts only packets that traverse it. Binding ``0.0.0.0`` (or a
LAN/public address) lets the same HTTP API, including bearer tokens, arrive on
every NIC. This module is stdlib-only so ``deploy_env`` can import it before
FastAPI exists.

Loopback stays allowed so health checks and local enrollment keep working.
Mesh reachability is the CGNAT address from ``tailscale ip -4`` (Darwin has
utun, not ``tailscale0``), plus Tailscale's IPv6 ULA if present.
"""

from __future__ import annotations

import ipaddress
import socket
import subprocess
from typing import Callable, List, Mapping, Optional, Sequence

MESH_PROVIDERS = frozenset({"tailscale", "headscale"})
TAILSCALE_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_ULA_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
UNSPECIFIED_HOSTS = frozenset({"0.0.0.0", "::", "*", "[::]"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class MeshBindError(ValueError):
    """Configured bind addresses are not legal for this network provider."""


def mesh_provider_enabled(provider: str) -> bool:
    return str(provider or "").strip().lower() in MESH_PROVIDERS


def parse_bind_hosts(raw: str) -> List[str]:
    """Split ``MAC_BIND_HOST`` into host tokens. Empty means loopback default."""
    text = str(raw or "").strip()
    if not text:
        return ["127.0.0.1"]
    hosts: List[str] = []
    seen = set()
    for token in text.split(","):
        host = token.strip().strip("[]")
        if not host or host in seen:
            continue
        seen.add(host)
        hosts.append(host)
    return hosts or ["127.0.0.1"]


def format_bind_hosts(hosts: Sequence[str]) -> str:
    return ",".join(parse_bind_hosts(",".join(hosts)))


def _parse_ip(host: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    text = str(host or "").strip().strip("[]")
    if not text or text in UNSPECIFIED_HOSTS or text == "localhost":
        return None
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def is_unspecified_host(host: str) -> bool:
    text = str(host or "").strip().strip("[]")
    if text in UNSPECIFIED_HOSTS:
        return True
    ip = _parse_ip(text)
    return bool(ip and ip.is_unspecified)


def is_loopback_host(host: str) -> bool:
    text = str(host or "").strip().strip("[]")
    if text in LOOPBACK_HOSTS:
        return True
    ip = _parse_ip(text)
    return bool(ip and ip.is_loopback)


def is_tailscale_range(host: str) -> bool:
    ip = _parse_ip(host)
    if ip is None:
        return False
    return ip in TAILSCALE_CGNAT_V4 or ip in TAILSCALE_ULA_V6


def is_allowed_mesh_bind_host(host: str, *, mesh_ips: Sequence[str] = ()) -> bool:
    """True when this listen address cannot be a public/LAN NIC on a mesh hub."""
    if is_unspecified_host(host):
        return False
    if is_loopback_host(host):
        return True
    if is_tailscale_range(host):
        return True
    normalized = str(host or "").strip().strip("[]")
    allowed = {str(item or "").strip().strip("[]") for item in mesh_ips if str(item or "").strip()}
    return normalized in allowed


def hosts_include_non_loopback(hosts: Sequence[str]) -> bool:
    return any(not is_loopback_host(host) for host in hosts)


def lookup_tailscale_ipv4(
    *,
    environ: Optional[Mapping[str, str]] = None,
    run: Optional[RunCommand] = None,
) -> str:
    """Prefer a live ``tailscale ip -4``; fall back to ``MAC_TAILSCALE_IP``."""
    env = environ or {}
    runner = run or subprocess.run
    try:
        completed = runner(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        completed = None
    if completed is not None and completed.returncode == 0:
        live = (completed.stdout or "").strip().splitlines()
        if live and _parse_ip(live[0].strip()):
            return live[0].strip()
    fallback = str(env.get("MAC_TAILSCALE_IP") or "").strip()
    return fallback if _parse_ip(fallback) else ""


def mesh_bind_problems(
    hosts: Sequence[str],
    *,
    network_provider: str,
    mesh_ips: Sequence[str] = (),
) -> List[str]:
    if not mesh_provider_enabled(network_provider):
        return []
    problems: List[str] = []
    for host in hosts:
        if is_allowed_mesh_bind_host(host, mesh_ips=mesh_ips):
            continue
        problems.append(
            "mesh bind refused: %r is not loopback or Tailscale/Headscale "
            "(will not listen on 0.0.0.0, LAN, or a public NIC)" % host
        )
    return problems


def deploy_mac_bind_host(
    requested: str,
    *,
    network_provider: str,
    is_hub: bool,
    tailscale_ip: str,
) -> str:
    """Bind value written to mac.env.

    Mesh hubs: loopback plus the Tailscale IPv4. ``0.0.0.0`` is rewritten when
    the overlay address is known, and refused when it is not. An explicit
    LAN/public address is an error. Never fall back to all-interfaces.
    """
    provider = str(network_provider or "").strip().lower()
    requested_hosts = parse_bind_hosts(requested)
    if not mesh_provider_enabled(provider):
        return format_bind_hosts(requested_hosts)
    if not is_hub:
        return "127.0.0.1"
    mesh_ip = str(tailscale_ip or "").strip()
    explicit = [
        host
        for host in requested_hosts
        if not is_unspecified_host(host) and not is_loopback_host(host)
    ]
    for host in explicit:
        if not is_allowed_mesh_bind_host(host, mesh_ips=(mesh_ip,) if mesh_ip else ()):
            raise MeshBindError(
                "mesh hub bind refused: %r is not loopback or Tailscale/Headscale"
                % host
            )
    if any(is_unspecified_host(host) for host in requested_hosts):
        if not mesh_ip or not _parse_ip(mesh_ip):
            raise MeshBindError(
                "mesh hub bind refused: MAC_TAILSCALE_IP is missing; "
                "will not bind 0.0.0.0. Join Tailscale/Headscale first."
            )
    if mesh_ip:
        if not is_allowed_mesh_bind_host(mesh_ip, mesh_ips=(mesh_ip,)):
            raise MeshBindError(
                "mesh hub bind refused: Tailscale IP %r is not a usable listen address"
                % mesh_ip
            )
        return format_bind_hosts(["127.0.0.1", mesh_ip])
    return "127.0.0.1"


def serve_bind_hosts(
    requested: str,
    *,
    network_provider: str,
    is_hub: bool,
    environ: Optional[Mapping[str, str]] = None,
    lookup: Optional[Callable[..., str]] = None,
) -> List[str]:
    """Listen addresses for the hub process.

    Mesh hubs always include loopback and the live Tailscale IPv4 (env fallback).
    """
    env = environ or {}
    requested_hosts = parse_bind_hosts(requested)
    if not mesh_provider_enabled(network_provider):
        return requested_hosts
    if not is_hub:
        return ["127.0.0.1"]
    finder = lookup or lookup_tailscale_ipv4
    mesh_ip = finder(environ=env)
    if not mesh_ip:
        raise MeshBindError(
            "mesh hub bind refused: no Tailscale IPv4 from `tailscale ip -4` "
            "or MAC_TAILSCALE_IP; will not bind 0.0.0.0"
        )
    illegal = [
        host
        for host in requested_hosts
        if not is_unspecified_host(host)
        and not is_loopback_host(host)
        and not is_allowed_mesh_bind_host(host, mesh_ips=(mesh_ip,))
    ]
    if illegal:
        raise MeshBindError(
            "; ".join(
                "mesh bind refused: %r is not loopback or Tailscale/Headscale "
                "(will not listen on 0.0.0.0, LAN, or a public NIC)" % host
                for host in illegal
            )
        )
    return parse_bind_hosts(format_bind_hosts(["127.0.0.1", mesh_ip]))


def bind_sockets(hosts: Sequence[str], port: int) -> List[socket.socket]:
    """Open one SOCK_STREAM socket per host. Caller owns close."""
    sockets: List[socket.socket] = []
    try:
        for host in hosts:
            ip = _parse_ip(host)
            family = socket.AF_INET6 if ip is not None and ip.version == 6 else socket.AF_INET
            bind_host = "127.0.0.1" if is_loopback_host(host) and family == socket.AF_INET else host
            if host in {"localhost", ""}:
                bind_host = "127.0.0.1"
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((bind_host, port))
            sock.listen(2048)
            sockets.append(sock)
    except Exception:
        for sock in sockets:
            sock.close()
        raise
    return sockets


def runtime_bind_error(
    *,
    bind_host: str,
    network_provider: str,
    mesh_ip: str = "",
) -> Optional[str]:
    """Message for create_app. None when the configured bind is legal."""
    hosts = parse_bind_hosts(bind_host)
    mesh_ips = [mesh_ip] if mesh_ip.strip() else []
    problems = mesh_bind_problems(
        hosts, network_provider=network_provider, mesh_ips=mesh_ips
    )
    return "; ".join(problems) if problems else None
