#!/usr/bin/env python3
"""Run worker credential validation from a digest-bound release archive.

The installed MAC package may predate ``worker_credentials validate-current``.
This bootstrap verifies the staged archive without importing MAC, extracts only
its ``src/mac`` package into an owner-private temporary directory, and asks the
already-managed interpreter to run the candidate module from those exact bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _stable_archive(path: Path, expected_sha256: str) -> bytes:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise ValueError("staged release archive digest is invalid")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 1 <= before.st_size <= MAX_ARCHIVE_BYTES
        ):
            raise ValueError("staged release archive is unsafe")
        chunks: list[bytes] = []
        remaining = before.st_size
        digest = hashlib.sha256()
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("staged release archive grew while reading")
        after = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if remaining or identity(before) != identity(after):
            raise ValueError("staged release archive changed while reading")
        if digest.hexdigest() != expected_sha256:
            raise ValueError("staged release archive digest differs")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _extract_candidate_package(raw: bytes, destination: Path) -> Path:
    package_root = destination / "src" / "mac"
    seen: set[PurePosixPath] = set()
    found_module = False
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("staged release archive contains an unsafe path")
            if relative.parts[:2] != ("src", "mac"):
                continue
            if relative in seen:
                raise ValueError("staged release archive contains a duplicate package path")
            seen.add(relative)
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            if not member.isfile():
                raise ValueError("staged release package contains a non-regular member")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("staged release package member is unreadable")
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    while chunk := source.read(1024 * 1024):
                        stream.write(chunk)
            finally:
                source.close()
            if relative == PurePosixPath("src/mac/worker_credentials.py"):
                found_module = True
    if not found_module or not (package_root / "__init__.py").is_file():
        raise ValueError("staged release lacks the worker credential package")
    return destination / "src"


def validate(*, archive: Path, archive_sha256: str, python: Path, agent_id: str) -> int:
    raw = _stable_archive(archive, archive_sha256)
    with tempfile.TemporaryDirectory(prefix="mac-worker-credential-candidate-") as temporary:
        root = Path(temporary)
        os.chmod(root, 0o700)
        source = _extract_candidate_package(raw, root)
        environment = dict(os.environ)
        prior_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(source) + (os.pathsep + prior_path if prior_path else "")
        result = subprocess.run(
            [
                str(python),
                "-m",
                "mac.worker_credentials",
                "validate-current",
                "--agent-id",
                agent_id,
            ],
            env=environment,
            check=False,
        )
        return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--agent-id", required=True)
    args = parser.parse_args()
    try:
        return validate(
            archive=args.archive,
            archive_sha256=args.archive_sha256,
            python=args.python,
            agent_id=args.agent_id,
        )
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"candidate worker credential validation bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
