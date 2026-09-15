"""Apply a reviewed Hermes source patch to an exact staged checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Sequence


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_reviewed_patches(stage: Path, manifest_paths: Sequence[Path]) -> list[str]:
    stage = stage.resolve()
    if not manifest_paths:
        raise RuntimeError("at least one reviewed Hermes patch manifest is required")
    reviewed: list[tuple[Path, dict[str, Any], Path]] = []
    upstream_commits: set[str] = set()
    allowed_files: set[str] = set()
    for manifest_path in manifest_paths:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        patch = manifest_path.with_name(str(manifest["patch"]))
        if _digest(patch) != manifest["patch_sha256"]:
            raise RuntimeError("reviewed Hermes patch digest mismatch")
        files = set(manifest["files"])
        if allowed_files & files:
            raise RuntimeError("reviewed Hermes patch manifests overlap")
        allowed_files.update(files)
        upstream_commits.add(str(manifest["upstream_commit"]))
        reviewed.append((manifest_path, manifest, patch))
    if len(upstream_commits) != 1:
        raise RuntimeError("reviewed Hermes patches do not share an upstream commit")
    head = subprocess.run(
        ["git", "-C", str(stage), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != upstream_commits.pop():
        raise RuntimeError("staged Hermes checkout is not the reviewed upstream commit")
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
    if not changed <= allowed_files or untracked:
        raise RuntimeError("staged Hermes checkout has unrelated source modifications")
    results: list[str] = []
    for _manifest_path, manifest, patch in reviewed:
        files = manifest["files"]
        digests = {name: _digest(stage / name) for name in files}
        if all(digests[name] == spec["patched_sha256"] for name, spec in files.items()):
            results.append("already_patched")
            continue
        if not all(digests[name] == spec["original_sha256"] for name, spec in files.items()):
            raise RuntimeError("staged Hermes patch inputs do not match reviewed source")
        subprocess.run(["git", "-C", str(stage), "apply", "--check", str(patch)], check=True)
        subprocess.run(["git", "-C", str(stage), "apply", str(patch)], check=True)
        if any(_digest(stage / name) != spec["patched_sha256"] for name, spec in files.items()):
            raise RuntimeError("staged Hermes patch output does not match reviewed result")
        results.append("patched")
    return results


def apply_reviewed_patch(stage: Path, manifest_path: Path) -> str:
    return apply_reviewed_patches(stage, [manifest_path])[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", type=Path)
    parser.add_argument("manifest", type=Path, nargs="+")
    args = parser.parse_args()
    print("\n".join(apply_reviewed_patches(args.stage, args.manifest)))


if __name__ == "__main__":
    main()
