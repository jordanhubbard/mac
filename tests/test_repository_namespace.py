from __future__ import annotations

import os
import subprocess
from pathlib import Path

from mac.repository_namespace import attest_git_tree_resource_namespace


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[dict[str, str], Path]:
    work = tmp_path / "work"
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "namespace@example.invalid")
    _git(work, "config", "user.name", "Namespace Test")
    return {"id": "repo", "path": str(work)}, work


def test_exact_plain_tree_resolves_portable_conflict_namespace(tmp_path: Path) -> None:
    repository, work = _repository(tmp_path)
    (work / "README.md").write_text("plain\n", encoding="utf-8")
    executable = work / "verify.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    _git(work, "add", "README.md", "verify.sh")
    _git(work, "commit", "-m", "plain tree")
    sha = _git(work, "rev-parse", "HEAD")

    result = attest_git_tree_resource_namespace(
        repository,
        planning_base_sha=sha,
    )

    assert result == {
        "status": "resolved",
        "case_sensitive": False,
        "unicode_normalization": "NFC",
        "symlink_resolution": "resolved",
        "conflict_policy": "exact",
        "attestor": "git-tree-namespace-v1",
        "planning_base_sha": sha,
    }


def test_symlink_tree_and_unavailable_checkout_stay_serialized(tmp_path: Path) -> None:
    repository, work = _repository(tmp_path)
    (work / "target.txt").write_text("target\n", encoding="utf-8")
    os.symlink("target.txt", work / "alias.txt")
    _git(work, "add", "target.txt", "alias.txt")
    _git(work, "commit", "-m", "tree with alias")
    sha = _git(work, "rev-parse", "HEAD")

    linked = attest_git_tree_resource_namespace(
        repository,
        planning_base_sha=sha,
    )
    missing = attest_git_tree_resource_namespace(
        {"id": "missing", "path": str(tmp_path / "missing")},
        planning_base_sha=sha,
    )

    assert linked["status"] == "unresolved"
    assert missing["status"] == "unresolved"
    assert "case_sensitive" not in linked
    assert "case_sensitive" not in missing
