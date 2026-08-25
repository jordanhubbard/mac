"""No-follow identities for deployment-approved executable artifacts."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Iterable, Tuple


def nofollow_regular_file_identity(path: str | Path) -> Tuple[str, str]:
    """Return ``(absolute_path, sha256:...)`` without following symlinks.

    Every path component and the final file must be a real directory/regular
    file. The descriptor identity is compared with the pre-open ``lstat`` result
    so a concurrent replacement cannot be attested under the old pathname.
    """

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("approved artifact path must be absolute")
    current = Path(candidate.anchor)
    for part in candidate.parts[1:-1]:
        current /= part
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("approved artifact parent is not a real directory")
    before = os.lstat(candidate)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError("approved artifact is not a no-follow regular file")
    descriptor = os.open(
        candidate,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("approved artifact descriptor is not a regular file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("approved artifact changed while it was opened")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return str(candidate), "sha256:%s" % digest.hexdigest()


def nofollow_source_bundle_digest(
    root: str | Path,
    relative_roots: Iterable[str] = ("src/mac", "pyproject.toml"),
) -> Tuple[str, str]:
    """Hash the complete importable MAC source bundle without symlinks."""

    base = Path(root).expanduser()
    if not base.is_absolute() or base.is_symlink() or not base.is_dir():
        raise ValueError("MAC source root must be an absolute real directory")
    entries: list[tuple[str, str]] = []
    for relative in relative_roots:
        target = base / relative
        candidates = [target] if target.is_file() else sorted(target.rglob("*"))
        for path in candidates:
            rel = str(path.relative_to(base))
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_symlink():
                raise ValueError("MAC source bundle contains a symlink: %s" % rel)
            if path.is_dir():
                continue
            _absolute, digest = nofollow_regular_file_identity(path)
            entries.append((rel, digest))
    if not entries:
        raise ValueError("MAC source bundle is empty")
    canonical = "".join("%s\0%s\n" % item for item in sorted(entries))
    return str(base), "sha256:%s" % hashlib.sha256(canonical.encode()).hexdigest()
