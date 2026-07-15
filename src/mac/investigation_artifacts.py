"""Canonical per-run investigation artifact set.

Every fleet task writes a family of per-run operational files into its *task
workspace*: the executor/worker/review diagnostics the hub ingests directly
from that workspace (never from git). They are per-run operational state, NOT
product content.

Committing any of them at the repository root regressed the publication merge
gate: every task branch modified the same repo-root file, so every publication
merge conflicted on it and failed. The repository ``.gitignore`` keeps them out
of the tree with **root-anchored** patterns (leading ``/``) so legitimate
product files elsewhere under ``src/`` or ``tests/`` are never masked.

This module is the *single source of truth* for that filename set. Both the
``.gitignore`` enforcement and the regression test that guards it derive from
:data:`PER_RUN_INVESTIGATION_ARTIFACTS`; nothing hardcodes a second copy, so the
two cannot drift. See ``tests/test_per_run_artifact_gitignore.py`` and the
matching ``.gitignore`` block.
"""

from __future__ import annotations

from typing import Tuple

#: Root-anchored per-run executor/worker/review investigation artifacts.
#:
#: Each name is written into the *task workspace root* by ``src/mac/worker.py``,
#: ``src/mac/executor_finalizer.py``, ``src/mac/executor_prompt.py``, and
#: ``src/mac/executor_sandbox.py``. The corresponding ``.gitignore`` entries are
#: root-anchored (``/<name>``): an actual write of any of these into the
#: worktree root must be ignored by git, while an identically named product file
#: nested under ``src/`` or ``tests/`` stays trackable.
PER_RUN_INVESTIGATION_ARTIFACTS: Tuple[str, ...] = (
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
    "repository-worktree.json",
    "recovery-evidence.json",
    "recovery-test.stdout.txt",
    "recovery-test.stderr.txt",
    "stdout.txt",
    "stderr.txt",
    ".mac-agentbus-control-pending-repo-update.json",
    ".mac-executor-policy.txt",
    ".mac-agentbus-control.json",
    ".mac-withheld-executor-evidence.json",
    # Stray developer audit/scratch notes written at the repo root during a
    # run; per-run operational state, not product content.
    "audit-findings.txt",
)

#: The root-anchored ``.gitignore`` pattern for each artifact (leading ``/``).
PER_RUN_INVESTIGATION_ARTIFACT_GITIGNORE_PATTERNS: Tuple[str, ...] = tuple(
    "/" + name for name in PER_RUN_INVESTIGATION_ARTIFACTS
)


__all__ = [
    "PER_RUN_INVESTIGATION_ARTIFACTS",
    "PER_RUN_INVESTIGATION_ARTIFACT_GITIGNORE_PATTERNS",
]
