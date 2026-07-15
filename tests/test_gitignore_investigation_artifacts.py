"""Regression test for the hardened repo-root ``.gitignore``.

Per-run investigation / executor / review artifacts are written into each task
workspace as operational state, never product content. The repo-root
``.gitignore`` pins each of these names with a root anchor (leading ``/``) so
that (a) every task branch stops modifying the same repo-root file and (b)
legitimate product files sharing those names elsewhere under ``src/`` or
``tests/`` are never masked.

This test asserts, deterministically and hermetically, that:

* every documented root-anchored per-run artifact name is ignored by the
  checked-in ``.gitignore``; and
* a same-named file nested under ``src/`` or ``tests/`` is NOT ignored.

It uses ``git check-ignore --no-index`` against the tracked ``.gitignore`` so
it never mutates the worktree or leaves stray files behind.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Root-anchored per-run artifact names documented in the repo-root .gitignore.
IGNORED_ROOT_ARTIFACTS = (
    "mac-evidence.json",
    "review-independent-findings.json",
    "review-independent-findings.previous.json",
    "review-independent-draft-evidence.json",
    "review-protocol.json",
    "review-result.json",
    "executor-evidence.json",
    "executor-evidence-preserved.json",
    "executor-task.json",
    "executor-policy.txt",
    "finalizer-progress.json",
    "harness-recovery-log.json",
    "mac-sandbox-verification.json",
    "openshell-salvage.json",
    "preserved-executor-worktree.json",
    "worker-result.json",
    "environment-contract.json",
    "environment-delta.json",
    ".mac-executor-policy.txt",
    ".mac-agentbus-control.json",
    ".mac-withheld-executor-evidence.json",
    "audit-findings.txt",
)

# Root-anchored (leading ``/``) artifact names. These MUST only match at the
# repo root, so a same-named file nested under ``src/`` or ``tests/`` stays
# tracked. ``mac-evidence.json`` is intentionally excluded here: it is pinned
# WITHOUT a leading ``/`` in .gitignore (ingested from the workspace at any
# depth), so it is expected to match nested paths too and is covered only by
# the positive assertions above.
ROOT_ANCHORED_ARTIFACTS = tuple(
    name for name in IGNORED_ROOT_ARTIFACTS if name != "mac-evidence.json"
)

# Same names nested under product dirs must stay tracked (never masked).
NOT_IGNORED_NESTED_ARTIFACTS = (
    "tests/executor-evidence.json",
    "src/mac/review-result.json",
    "src/environment-contract.json",
)


def _check_ignore(relpath: str) -> bool:
    """Return True iff ``git check-ignore`` reports ``relpath`` as ignored.

    Uses ``--no-index`` so the decision comes purely from the tracked
    ``.gitignore`` rules and does not depend on worktree contents. ``git
    check-ignore`` exits 0 when the path is ignored, 1 when it is not, and >1
    on error.
    """
    result = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "--no-index", "--", relpath],
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise AssertionError(
            f"git check-ignore failed for {relpath!r}: "
            f"rc={result.returncode} stderr={result.stderr.strip()!r}"
        )
    return result.returncode == 0


@pytest.mark.parametrize("name", IGNORED_ROOT_ARTIFACTS)
def test_root_anchored_artifact_is_ignored(name: str) -> None:
    assert _check_ignore(name), (
        f"repo-root artifact {name!r} should be ignored by the checked-in "
        ".gitignore but is not"
    )


@pytest.mark.parametrize("relpath", NOT_IGNORED_NESTED_ARTIFACTS)
def test_nested_same_named_file_is_not_ignored(relpath: str) -> None:
    assert not _check_ignore(relpath), (
        f"nested product path {relpath!r} must NOT be ignored; the root-anchored "
        ".gitignore rule is masking legitimate files elsewhere"
    )


@pytest.mark.parametrize("name", ROOT_ANCHORED_ARTIFACTS)
def test_root_anchored_artifact_does_not_mask_nested_files(name: str) -> None:
    """Each root-anchored pattern must apply only at the repo root: the same
    name nested under ``src/`` or ``tests/`` stays tracked."""
    for parent in ("src", "tests"):
        relpath = f"{parent}/{name}"
        assert not _check_ignore(relpath), (
            f"root-anchored rule for {name!r} is masking nested product path "
            f"{relpath!r}"
        )


def test_gitignore_documents_full_artifact_set() -> None:
    """Guardrail: every documented artifact name is literally present in the
    checked-in .gitignore, so this regression list stays in sync with the file
    it protects."""
    gitignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    lines = {line.strip().lstrip("/") for line in gitignore_text.splitlines()}
    missing = [name for name in IGNORED_ROOT_ARTIFACTS if name not in lines]
    assert not missing, f".gitignore is missing entries for: {missing}"
