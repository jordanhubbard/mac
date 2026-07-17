#!/usr/bin/env python3
"""Audit and digest the Git-visible trusted-certifier build context."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath


SENSITIVE_PARTS = frozenset(
    {
        ".aws",
        ".gnupg",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".ssh",
        "credentials",
        "id_ed25519",
        "id_rsa",
    }
)
SENSITIVE_SUFFIXES = (".age", ".key", ".p12", ".pem", ".pfx", ".secret", ".tfstate")


class ContextError(RuntimeError):
    pass


def _sensitive(path: PurePosixPath) -> bool:
    return any(
        part in SENSITIVE_PARTS or part == ".env" or part.startswith(".env.")
        for part in path.parts
    ) or path.name.endswith(SENSITIVE_SUFFIXES)


def context_manifest(root: Path) -> dict[str, object]:
    root = root.resolve(strict=True)
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise ContextError("git could not enumerate the certifier build context")
    names = sorted(
        {item.decode("utf-8") for item in completed.stdout.split(b"\0") if item}
    )
    if not names:
        raise ContextError("certifier build context is empty")
    aggregate = hashlib.sha256()
    files: list[dict[str, str]] = []
    for raw_name in names:
        relative = PurePosixPath(raw_name)
        if relative.is_absolute() or ".." in relative.parts or str(relative) != raw_name:
            raise ContextError(f"unsafe build-context path: {raw_name}")
        if _sensitive(relative):
            raise ContextError(
                f"secret-shaped file may not enter the certifier image: {raw_name}"
            )
        path = root.joinpath(*relative.parts)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ContextError(f"certifier context path must be a regular file: {raw_name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        mode = "0755" if metadata.st_mode & 0o111 else "0644"
        aggregate.update(
            raw_name.encode("utf-8")
            + b"\0"
            + mode.encode("ascii")
            + b"\0"
            + digest.encode("ascii")
            + b"\n"
        )
        files.append(
            {"path": raw_name, "mode": mode, "digest": "sha256:" + digest}
        )
    return {
        "schema": "mac.certifier_build_context.v1",
        "digest": "sha256:" + aggregate.hexdigest(),
        "file_count": len(files),
        "files": files,
    }


def materialize_context(root: Path, destination: Path, payload: dict[str, object]) -> None:
    """Copy only the audited Git-visible regular files into a new directory."""

    root = root.resolve(strict=True)
    destination = destination.resolve(strict=False)
    if destination == root or root in destination.parents:
        raise ContextError("materialized context must be outside the repository")
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ContextError(f"materialized context already exists: {destination}") from exc

    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        raise ContextError("certifier context manifest has no file inventory")
    observed: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise ContextError("certifier context manifest entry is malformed")
        raw_name = raw.get("path")
        raw_mode = raw.get("mode")
        raw_digest = raw.get("digest")
        if (
            not isinstance(raw_name, str)
            or raw_mode not in {"0644", "0755"}
            or not isinstance(raw_digest, str)
        ):
            raise ContextError("certifier context manifest entry is malformed")
        relative = PurePosixPath(raw_name)
        source = root.joinpath(*relative.parts)
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target, follow_symlinks=False)
        target.chmod(int(raw_mode, 8))
        observed_digest = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        if observed_digest != raw_digest:
            raise ContextError(f"certifier context changed while copying: {raw_name}")
        observed.add(target.relative_to(destination).as_posix())

    actual = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual != observed:
        raise ContextError("materialized certifier context inventory differs")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--print-digest", action="store_true")
    parser.add_argument(
        "--materialize",
        type=Path,
        help="copy the audited allowlist to a new context directory",
    )
    args = parser.parse_args(argv)
    try:
        payload = context_manifest(args.root)
        if args.materialize is not None:
            materialize_context(args.root, args.materialize, payload)
    except (ContextError, OSError, UnicodeError) as exc:
        print(f"certifier context audit failed: {exc}", file=sys.stderr)
        return 1
    if args.print_digest:
        print(payload["digest"])
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
