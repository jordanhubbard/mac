"""Apply a reviewed Hermes source patch to an exact staged checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_reviewed_patch(stage: Path, manifest_path: Path) -> str:
    stage = stage.resolve()
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    patch = manifest_path.with_name(str(manifest["patch"]))
    if _digest(patch) != manifest["patch_sha256"]:
        raise RuntimeError("reviewed Hermes patch digest mismatch")
    head = subprocess.run(
        ["git", "-C", str(stage), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != manifest["upstream_commit"]:
        raise RuntimeError("staged Hermes checkout is not the reviewed upstream commit")
    files = manifest["files"]
    changed = set(
        subprocess.run(
            ["git", "-C", str(stage), "diff", "HEAD", "--name-only", "-z"],
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.rstrip("\0")
        .split("\0")
    ) - {""}
    untracked = subprocess.run(
        ["git", "-C", str(stage), "ls-files", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if not changed <= set(files) or untracked:
        raise RuntimeError("staged Hermes checkout has unrelated source modifications")
    digests = {name: _digest(stage / name) for name in files}
    if all(digests[name] == spec["patched_sha256"] for name, spec in files.items()):
        return "already_patched"
    if not all(digests[name] == spec["original_sha256"] for name, spec in files.items()):
        raise RuntimeError("staged Hermes patch inputs do not match reviewed source")
    subprocess.run(["git", "-C", str(stage), "apply", "--check", str(patch)], check=True)
    subprocess.run(["git", "-C", str(stage), "apply", str(patch)], check=True)
    if any(_digest(stage / name) != spec["patched_sha256"] for name, spec in files.items()):
        raise RuntimeError("staged Hermes patch output does not match reviewed result")
    return "patched"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    print(apply_reviewed_patch(args.stage, args.manifest))


if __name__ == "__main__":
    main()
