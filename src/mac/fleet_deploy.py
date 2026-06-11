from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


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


def shell_words(items: Iterable[str]) -> str:
    return " ".join(items)
