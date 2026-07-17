"""Controller-owned attestation for repository path conflict namespaces.

The work-package compiler may remove its conservative ``repo:*`` mutation lock
only when the controller can prove that path declarations have no hidden Git
tree aliases.  This module deliberately makes that proof narrow: the exact
pinned commit must exist in a registered local checkout, and its recursive tree
must contain neither symbolic links nor gitlinks (submodules).  Otherwise the
caller receives an unresolved attestation and execution stays serialized.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from mac.models import JsonDict


GIT_TREE_NAMESPACE_ATTESTOR = "git-tree-namespace-v1"
_FULL_OBJECT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SAFE_GIT_MODES = frozenset({"100644", "100755"})


def attest_git_tree_resource_namespace(
    repository: Mapping[str, Any],
    *,
    planning_base_sha: str,
    timeout_seconds: int = 30,
) -> JsonDict:
    """Return a secret-free, fail-closed namespace observation.

    Git object IDs make a successful observation immutable.  Case-insensitive
    NFC keys are intentionally used even when the controller filesystem is
    case-sensitive: they are the portable lower bound shared by heterogeneous
    macOS and Linux workers.  A tree containing mode 120000 (symlink), mode
    160000 (gitlink), an unknown mode, or an unreadable object stays unresolved.
    """

    unresolved: JsonDict = {
        "status": "unresolved",
        "attestor": GIT_TREE_NAMESPACE_ATTESTOR,
    }
    sha = str(planning_base_sha or "").strip().lower()
    if not _FULL_OBJECT_ID_RE.fullmatch(sha):
        return unresolved
    try:
        timeout = int(timeout_seconds)
    except (TypeError, ValueError):
        return unresolved
    if timeout < 1 or timeout > 300:
        return unresolved

    local_path = Path(str(repository.get("path") or "")).expanduser()
    if not local_path.is_dir() or not (local_path / ".git").exists():
        return unresolved

    result = _run_git(
        [
            "git",
            "-C",
            str(local_path),
            "ls-tree",
            "-r",
            "--full-tree",
            "--format=%(objectmode)",
            sha,
        ],
        timeout_seconds=timeout,
    )
    if result is None:
        return unresolved
    modes = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
    if any(mode not in _SAFE_GIT_MODES for mode in modes):
        return unresolved
    return {
        "status": "resolved",
        "case_sensitive": False,
        "unicode_normalization": "NFC",
        "symlink_resolution": "resolved",
        "conflict_policy": "exact",
        "attestor": GIT_TREE_NAMESPACE_ATTESTOR,
        "planning_base_sha": sha,
    }


def _run_git(
    command: Sequence[str], *, timeout_seconds: int
) -> subprocess.CompletedProcess[str] | None:
    environment = os.environ.copy()
    environment.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_ASKPASS": "",
            "GIT_TERMINAL_PROMPT": "0",
            "SSH_ASKPASS": "",
        }
    )
    try:
        result = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None
    return result if result.returncode == 0 else None


__all__ = [
    "GIT_TREE_NAMESPACE_ATTESTOR",
    "attest_git_tree_resource_namespace",
]
