"""Shared primitives for repository evidence and remote-ref contracts."""

from __future__ import annotations

import fnmatch
import re
from typing import Any


_SAFE_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,127}$")


def normalize_repo_relative_path(value: Any) -> str:
    """Return the canonical slash-separated form used in repository evidence."""

    path = str(value or "").strip().replace("\\", "/")
    path = re.sub(r"/+", "/", path)
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/")


def repo_path_satisfies_requirement(changed_path: str, required_path: str) -> bool:
    """Match one changed path against an exact or glob evidence requirement."""

    changed = normalize_repo_relative_path(changed_path)
    required = normalize_repo_relative_path(required_path)
    if not changed or not required:
        return False
    if any(char in required for char in "*?["):
        return fnmatch.fnmatchcase(changed, required)
    return changed == required


def remote_branch_from_ref(remote_ref: str) -> str:
    """Extract a safe branch name from common remote-reference spellings."""

    ref = str(remote_ref or "").strip()
    if not ref:
        return ""
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix) :]
            break
    if ref.startswith("origin/"):
        ref = ref[len("origin/") :]
    if (
        ref
        and not ref.startswith("-")
        and not ref.startswith("refs/")
        and _SAFE_GIT_REF_RE.fullmatch(ref)
    ):
        return ref
    return ""
