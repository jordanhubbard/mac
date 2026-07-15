"""Regression guard: the canonical per-run investigation artifact set stays
ignored by, and in sync with, the repository ``.gitignore``.

Committing a per-run investigation artifact at the repository root regressed the
publication merge gate — every task branch modified the same repo-root file, so
every publication merge conflicted on it and failed. Three guards keep that from
recurring:

1. Every canonical artifact, written into the worktree *root*, is ignored by git
   (proved with ``git check-ignore``), while an identically named product file
   nested under ``src/``/``tests/`` is **not** masked (root-anchored patterns).
2. None of the canonical artifacts are currently tracked (``git ls-files``), so a
   future accidental ``git add`` of one fails the suite.
3. The canonical set and the root-anchored ``.gitignore`` block stay in sync in
   both directions, so the two authoritative lists cannot drift apart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mac.investigation_artifacts import (
    PER_RUN_INVESTIGATION_ARTIFACT_GITIGNORE_PATTERNS,
    PER_RUN_INVESTIGATION_ARTIFACTS,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GITIGNORE = REPO_ROOT / ".gitignore"


def _git(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _require_git_repo() -> None:
    result = _git("rev-parse", "--is-inside-work-tree")
    if result.returncode != 0 or result.stdout.strip() != "true":
        pytest.skip("test requires running inside the repository git worktree")


def _root_anchored_gitignore_names() -> set[str]:
    """Root-anchored (leading ``/``) single-path entries in ``.gitignore``.

    Only bare root-anchored file patterns (``/name``) are collected — directory
    patterns (trailing ``/``) and glob patterns are excluded so this reflects
    the per-run artifact block, not unrelated root-anchored rules.
    """

    names: set[str] = set()
    for raw in GITIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("/"):
            continue
        if line.endswith("/"):
            continue
        if any(ch in line for ch in "*?[]!"):
            continue
        names.add(line[1:])
    return names


def test_canonical_set_is_non_empty_and_unique() -> None:
    assert PER_RUN_INVESTIGATION_ARTIFACTS, "canonical artifact set must not be empty"
    assert len(PER_RUN_INVESTIGATION_ARTIFACTS) == len(
        set(PER_RUN_INVESTIGATION_ARTIFACTS)
    ), "canonical artifact set has duplicate entries"


def test_every_canonical_artifact_is_ignored_at_repo_root() -> None:
    """A real write of each artifact into the worktree root would be ignored."""

    _require_git_repo()
    not_ignored = []
    for name in PER_RUN_INVESTIGATION_ARTIFACTS:
        result = _git("check-ignore", "--", name)
        # rc 0 => the path is ignored (stdout echoes the matching path).
        if result.returncode != 0:
            not_ignored.append(name)
    assert not not_ignored, (
        "per-run artifacts NOT ignored at repo root (would be committable and "
        "re-trigger the publication merge-gate regression): %s" % sorted(not_ignored)
    )


def test_root_anchored_patterns_do_not_mask_nested_product_files() -> None:
    """Root-anchoring must be preserved: the same names nested under product
    trees stay trackable, so the ignore rules never hide real source/test files."""

    _require_git_repo()
    masked = []
    for name in PER_RUN_INVESTIGATION_ARTIFACTS:
        for nested in ("src/mac/" + name, "tests/" + name):
            result = _git("check-ignore", "--", nested)
            if result.returncode == 0:
                masked.append(nested)
    assert not masked, (
        "root-anchored ignore rules unexpectedly mask nested product paths: %s"
        % sorted(masked)
    )


def test_no_canonical_artifact_is_tracked() -> None:
    """Inverse guard: none of the artifacts are tracked, so a stray `git add`
    of a per-run artifact fails the suite."""

    _require_git_repo()
    tracked = []
    for name in PER_RUN_INVESTIGATION_ARTIFACTS:
        result = _git("ls-files", "--error-unmatch", "--", name)
        if result.returncode == 0:
            tracked.append(name)
    assert not tracked, (
        "per-run artifacts are tracked by git and must not be: %s" % sorted(tracked)
    )


def test_canonical_set_and_gitignore_block_stay_in_sync() -> None:
    """Both directions: every canonical artifact has a root-anchored `.gitignore`
    entry, and every root-anchored file entry maps to a canonical artifact — so
    the two authoritative lists cannot drift."""

    ignore_names = _root_anchored_gitignore_names()
    canonical_names = set(PER_RUN_INVESTIGATION_ARTIFACTS)

    missing_from_gitignore = sorted(canonical_names - ignore_names)
    missing_from_canonical = sorted(ignore_names - canonical_names)

    assert not missing_from_gitignore, (
        "canonical artifacts with no root-anchored .gitignore entry: %s"
        % missing_from_gitignore
    )
    assert not missing_from_canonical, (
        "root-anchored .gitignore file entries not in the canonical set "
        "(add to PER_RUN_INVESTIGATION_ARTIFACTS or remove from .gitignore): %s"
        % missing_from_canonical
    )


def test_generated_gitignore_patterns_match_canonical_set() -> None:
    assert PER_RUN_INVESTIGATION_ARTIFACT_GITIGNORE_PATTERNS == tuple(
        "/" + name for name in PER_RUN_INVESTIGATION_ARTIFACTS
    )
