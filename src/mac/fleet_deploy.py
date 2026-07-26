"""Fleet deployment helpers over SSH.

Models SSH targets and cleanup paths and provides parsing, normalization, and
Tailscale-mesh canonicalization utilities used to deploy and manage MAC fleet
nodes across remote hosts.
"""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional


@dataclass(frozen=True)
class SshTarget:
    user_host: str
    port: Optional[int] = None

    @property
    def ssh_target(self) -> str:
        return self.user_host

    @property
    def scp_target_prefix(self) -> str:
        return self.user_host

    def ssh_args(self) -> List[str]:
        return ["-p", str(self.port)] if self.port is not None else []

    def scp_args(self) -> List[str]:
        return ["-P", str(self.port)] if self.port is not None else []


@dataclass(frozen=True)
class CleanupPath:
    path: Path
    reason: str
    retain_days: int


def parse_ssh_target(value: str, *, port: Optional[int] = None) -> SshTarget:
    text = (value or "").strip()
    if not text:
        raise ValueError("SSH target is required")
    parsed_port = port
    user_host = text
    # Accept user@host:2201 and host:2201 for deploy config convenience.
    # Bracketed IPv6 should be supplied via ~/.ssh/config alias or --ssh-port.
    if text.count(":") == 1 and not text.endswith(":"):
        candidate_host, candidate_port = text.rsplit(":", 1)
        if candidate_port.isdigit():
            user_host = candidate_host
            parsed_port = int(candidate_port)
    if parsed_port is not None and parsed_port <= 0:
        raise ValueError("SSH port must be positive")
    return SshTarget(user_host=user_host, port=parsed_port)


def normalize_ssh_target(value: str, *, port: Optional[int] = None) -> str:
    target = parse_ssh_target(value, port=port)
    return (
        "%s:%d" % (target.user_host, target.port)
        if target.port is not None
        else target.user_host
    )


def canonicalize_mesh_ssh_target(
    value: str,
    *,
    provider: str,
    port: Optional[int] = None,
    status: Optional[Mapping[str, Any]] = None,
) -> str:
    """Replace an mDNS-only SSH hostname with its durable mesh IPv4 address.

    ``*.local`` names are only meaningful on the current multicast-DNS link.
    Tailscale and Headscale both expose their peer inventory through
    ``tailscale status --json``; use that inventory to persist the peer's
    stable mesh address instead.  Ordinary DNS names and literal addresses are
    deliberately left unchanged.

    Resolution fails closed.  Persisting the original ``*.local`` name would
    create a registry entry that becomes invalid as soon as the operator moves
    to another network.
    """

    normalized = normalize_ssh_target(value, port=port)
    parsed = parse_ssh_target(normalized)
    user, separator, host = parsed.user_host.rpartition("@")
    host = host if separator else parsed.user_host
    mdns_host = host.rstrip(".")
    if not mdns_host.casefold().endswith(".local"):
        return normalized

    mesh_provider = (provider or "none").strip().casefold()
    if mesh_provider not in {"tailscale", "headscale"}:
        raise ValueError(
            "SSH target %r uses link-local mDNS; configure tailscale/headscale "
            "or provide a durable IP/DNS name" % host
        )

    mesh_status = dict(status) if status is not None else _tailscale_status()
    mesh_ip = _mesh_ipv4_for_mdns_host(mdns_host, mesh_status)
    user_host = "%s@%s" % (user, mesh_ip) if separator else mesh_ip
    return "%s:%d" % (user_host, parsed.port) if parsed.port is not None else user_host


def _tailscale_status() -> Mapping[str, Any]:
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            "cannot resolve .local target through the active mesh: "
            "tailscale status is unavailable"
        ) from exc
    if result.returncode != 0:
        raise ValueError(
            "cannot resolve .local target through the active mesh: "
            "tailscale status failed"
        )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("tailscale status returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("BackendState") != "Running":
        raise ValueError("tailscale/headscale client is not running")
    return payload


def _mesh_ipv4_for_mdns_host(host: str, status: Mapping[str, Any]) -> str:
    short_name = host.rstrip(".")[:-6].rstrip(".").casefold()
    if not short_name:
        raise ValueError("invalid .local SSH hostname: %r" % host)

    nodes: List[Mapping[str, Any]] = []
    self_node = status.get("Self")
    if isinstance(self_node, Mapping):
        nodes.append(self_node)
    peers = status.get("Peer")
    if isinstance(peers, Mapping):
        nodes.extend(node for node in peers.values() if isinstance(node, Mapping))

    matches: List[str] = []
    for node in nodes:
        hostname = str(node.get("HostName") or "").rstrip(".").casefold()
        dns_name = str(node.get("DNSName") or "").rstrip(".").casefold()
        names = {hostname, dns_name, dns_name.split(".", 1)[0]}
        if short_name not in names:
            continue
        for candidate in node.get("TailscaleIPs") or []:
            try:
                address = ipaddress.ip_address(str(candidate))
            except ValueError:
                continue
            if address.version == 4:
                matches.append(str(address))
                break

    unique = sorted(set(matches))
    if not unique:
        raise ValueError(
            "no tailscale/headscale peer matches %r; provide its mesh IP or a durable DNS name"
            % host
        )
    if len(unique) > 1:
        raise ValueError("multiple mesh peers match %r; provide the mesh IP explicitly" % host)
    return unique[0]


def cleanup_retention_plan(home: Path, mac_home: Path) -> List[CleanupPath]:
    return [
        CleanupPath(mac_home / "backups", "generated MAC deploy backups", 14),
        CleanupPath(mac_home / "logs", "generated MAC deploy logs and manifests", 30),
        CleanupPath(Path("/tmp"), "stale MAC deploy archives", 2),
        CleanupPath(home / ".acc" / "build", "obsolete ACC build output", 14),
        CleanupPath(home / ".acc" / "dist", "obsolete ACC distribution output", 14),
        CleanupPath(home / ".acc" / "deploy", "obsolete ACC deploy output", 14),
        CleanupPath(home / ".acc" / "logs", "obsolete ACC deploy logs", 14),
        CleanupPath(home / ".acc" / ".pytest_cache", "obsolete ACC test cache", 14),
        CleanupPath(home / ".acc" / "hermes-agent", "obsolete ACC Hermes checkout", 30),
        CleanupPath(home / ".agentfs" / "reviews", "AgentFS review scratch", 14),
        CleanupPath(home / "AgentFS" / "reviews", "AgentFS review scratch", 14),
    ]


def cleanup_path_strings(home: Path, mac_home: Path) -> List[str]:
    return [
        "%s|%s|%d" % (item.path, item.reason, item.retain_days)
        for item in cleanup_retention_plan(home, mac_home)
    ]


def phase_failure_evidence_dir(mac_home: Path) -> Path:
    """Directory where secret-safe fleet phase-failure evidence is preserved.

    Records produced by
    ``mac.fleet_node_install.capture_phase_failure_evidence`` are persisted
    here so a failed install can be diagnosed after the fact. This directory is
    deliberately excluded from the deploy cleanup sweep (see
    :func:`preserved_cleanup_paths` and :func:`is_cleanup_protected_path`) —
    unlike generated logs/backups, failure evidence must survive cleanup.
    """
    return Path(mac_home) / "phase-failure-evidence"


def preserved_cleanup_paths(home: Path, mac_home: Path) -> List[Path]:
    """Paths that the deploy cleanup sweep must never delete.

    Currently the sole entry is the phase-failure evidence directory, but the
    helper returns a list so future preserve-through-cleanup artifacts can be
    added without changing the cleanup call sites.
    """
    return [phase_failure_evidence_dir(mac_home)]


def is_cleanup_protected_path(
    candidate: Path, home: Path, mac_home: Path
) -> bool:
    """Return ``True`` if *candidate* is a preserved path or lives under one.

    Cleanup callers consult this before deleting a candidate path so that
    secret-safe failure evidence (and any other preserved artifact) is never
    swept away, even when a broader retention entry would otherwise cover it.
    """
    candidate = Path(candidate)
    for protected in preserved_cleanup_paths(home, mac_home):
        if candidate == protected:
            return True
        try:
            candidate.relative_to(protected)
        except ValueError:
            continue
        return True
    return False


def shell_words(items: Iterable[str]) -> str:
    return " ".join(items)


def write_owner_only_file(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically write *text* to *path* with mode 0o600 (owner read/write only).

    Uses a tempfile-then-replace pattern matching ``write_ide_handoff_file`` so
    the target path is never visible to other processes in a partially-written
    state.  The temporary file is created in the same directory as *path* to
    ensure ``os.replace`` is always within a single filesystem.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if tmp.exists():
            tmp.unlink()


def ensure_owner_only_directory(path: Path) -> None:
    """Create *path* (and any missing parents) with mode 0o700 (owner only).

    If the directory already exists its permissions are tightened to 0o700.
    Parent directories that must be created receive the default umask-filtered
    permissions; only the final component is guaranteed to be 0o700.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
