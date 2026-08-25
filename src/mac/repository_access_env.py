"""Process-environment fence for read-only repository inspection tasks."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import MutableMapping


REPOSITORY_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "MAC_TASK_GIT_TOKEN",
        "GITEA_TOKEN",
        "GITEA_USER",
        "GITLAB_TOKEN",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_PARAMETERS",
    }
)
_GIT_CONFIG_INDEXED_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def fence_read_only_repository_environment(
    environment: MutableMapping[str, str],
) -> None:
    """Remove repository write credentials and ambient Git config injection.

    Model-provider and MAC API credentials are intentionally untouched.  Git
    gets non-interactive, empty global/system configuration and an SSH command
    that cannot discover ambient identities.
    """

    for name in tuple(environment):
        if name in REPOSITORY_CREDENTIAL_ENV_NAMES or name.startswith(_GIT_CONFIG_INDEXED_PREFIXES):
            environment.pop(name, None)
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_SSH_COMMAND": (
                "ssh -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityFile=/dev/null"
            ),
        }
    )


def read_only_repository_content_digest(root: Path) -> str:
    """Hash all repository content except Git metadata and CodeGraph cache."""

    root = Path(root).resolve()
    digest = hashlib.sha256()
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        rel_dir = current_path.relative_to(root)
        retained_dirs = []
        for name in sorted(dirs):
            if rel_dir == Path(".") and name in {".git", ".codegraph"}:
                continue
            path = current_path / name
            info = path.lstat()
            if not stat.S_ISLNK(info.st_mode):
                retained_dirs.append(name)
                continue
            # os.walk reports symlinks-to-directories in ``dirs`` even with
            # followlinks=False.  Hash the link itself before pruning it from
            # traversal; otherwise changing ``link -> existing-a`` into
            # ``link -> existing-b`` is invisible to the repository proof.
            rel = path.relative_to(root).as_posix()
            payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
            digest.update(b"L\0")
            digest.update(rel.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(str(stat.S_IMODE(info.st_mode)).encode("ascii"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(payload).digest())
        dirs[:] = retained_dirs
        for name in sorted(files):
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            if rel == ".git" or rel.startswith((".git/", ".codegraph/")):
                continue
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
                kind = b"L"
            elif stat.S_ISREG(info.st_mode):
                file_digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        file_digest.update(chunk)
                payload_digest = file_digest.digest()
                kind = b"F"
            else:
                payload_digest = hashlib.sha256(b"").digest()
                kind = b"O"
            if kind == b"L":
                payload_digest = hashlib.sha256(payload).digest()
            digest.update(kind)
            digest.update(b"\0")
            digest.update(rel.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(str(mode).encode("ascii"))
            digest.update(b"\0")
            digest.update(payload_digest)
    return digest.hexdigest()
