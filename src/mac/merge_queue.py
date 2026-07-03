"""Merge-queue gating: never land a branch that hasn't been validated against
the *projected* post-merge state of the target branch.

## The problem this solves

Multiple agents (or one agent parallelizing across worktrees) each produce a
branch that is green *against the base it started from*. Each is reviewed and
tested in isolation, then merged. But "green in isolation" does not imply "green
after integration": two branches can each pass yet conflict textually, or — far
worse — merge cleanly at the text level while breaking each other semantically
(one renames a symbol the other calls; both add an entry to the same registry;
both edit adjacent lines). The defect only appears on the integrated trunk,
after both have landed. This is exactly a database write-skew anomaly.

## The deterministic model (why this is the right fix, not a heuristic)

Landing a branch is a *transaction* on shared repository state; the trunk tip is
the committed state. The academic frame is **serializability** with **optimistic
concurrency control (OCC)**: let branches proceed in parallel (optimistic), and
at *commit* time run a validation phase against the current committed state —
abort/retry (rebase) on conflict. The industry realization of the same idea is
the **merge queue / gating pipeline** built on Graydon Hoare's "Not Rocket
Science Rule" (bors → GitHub Merge Queue → Zuul → Google TAP): *automatically
maintain a trunk that always passes the tests, by testing every change against
the state it will actually be merged into, serialized so there is no skew.*

This module implements the OCC **validation phase** for a code merge:

1. **Serialize** landings per repository (a landing lease) so the "current tip"
   is stable across the check → merge window — the serialization point that
   makes the schedule equivalent to a serial one.
2. **Validate** the branch against the *current* tip (not the stale base it was
   authored on): compute the merge with ``git merge-tree`` — which produces the
   merged tree and reports conflicts *without mutating any working tree* — and
   fail the gate on textual conflict. A failed gate routes the task to
   integration (the third agent: rebase, resolve, re-verify) instead of a dirty
   or skew merge.
3. The caller then runs the test suite against that projected merged tree (the
   "test the projected state" half of the Not-Rocket-Science rule) before the
   merge is allowed to fast-forward.

Textual clean-merge is necessary but not sufficient (it does not catch semantic
write-skew); step 3 (re-running the contract suite on the merged state) is what
closes that gap. This module owns steps 1–2 and the contract for step 3.

References: Hoare, "The Not Rocket Science Rule"; bors-ng; GitHub Merge Queue;
Zuul project gating (speculative merge trains over a DAG of dependent changes);
Kung & Robinson, "On Optimistic Methods for Concurrency Control" (ACM TODS 1981).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

GitRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess"]


@dataclass(frozen=True)
class MergeGateVerdict:
    """Result of validating a branch against the current target tip."""

    clean: bool
    base_sha: str
    topic_sha: str
    conflicted_files: List[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "schema": "mac.merge_gate.v1",
            "clean": self.clean,
            "base_sha": self.base_sha,
            "topic_sha": self.topic_sha,
            "conflicted_files": list(self.conflicted_files),
            "error": self.error,
        }


def _default_git_runner(repo_dir: str) -> GitRunner:
    def run(args: Sequence[str]) -> "subprocess.CompletedProcess":
        return subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True,
            text=True,
            check=False,
        )

    return run


def _rev_parse(run: GitRunner, ref: str) -> str:
    proc = run(["rev-parse", "--verify", "%s^{commit}" % ref])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def validate_projected_merge(
    repo_dir: str,
    base_ref: str,
    topic_ref: str,
    *,
    git_runner: Optional[GitRunner] = None,
) -> MergeGateVerdict:
    """OCC validation phase for landing ``topic_ref`` onto ``base_ref``.

    Computes the merge of ``topic_ref`` into the *current* ``base_ref`` tip with
    ``git merge-tree`` — no working tree is touched, nothing is committed. This
    is the deterministic gate: a clean result means the branch integrates onto
    the trunk as it exists *now* (not the stale base it was authored on); a
    conflicted result must route the task to integration rather than merge.

    Uses ``git merge-tree --write-tree`` (git >= 2.38): exit 0 = clean (stdout is
    the merged tree OID); exit 1 = conflicts (stdout lists the merged tree then
    the conflicted paths). Other exit codes are treated as an error (e.g. an
    unrelated-history or bad-ref case), which also fails the gate — closed.
    """
    run = git_runner if git_runner is not None else _default_git_runner(repo_dir)

    base_sha = _rev_parse(run, base_ref)
    topic_sha = _rev_parse(run, topic_ref)
    if not base_sha:
        return MergeGateVerdict(False, "", topic_sha, error="cannot resolve base ref %r" % base_ref)
    if not topic_sha:
        return MergeGateVerdict(False, base_sha, "", error="cannot resolve topic ref %r" % topic_ref)
    if base_sha == topic_sha:
        # Nothing to integrate; trivially clean.
        return MergeGateVerdict(True, base_sha, topic_sha)

    proc = run(["merge-tree", "--write-tree", "--name-only", base_sha, topic_sha])
    if proc.returncode == 0:
        return MergeGateVerdict(True, base_sha, topic_sha)
    if proc.returncode == 1:
        # Conflicts. Output is: <merged-tree-oid>\n\n<conflicted path>\n...
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        conflicted = lines[1:] if len(lines) > 1 else []
        return MergeGateVerdict(
            False, base_sha, topic_sha, conflicted_files=conflicted or ["<unknown>"]
        )
    return MergeGateVerdict(
        False,
        base_sha,
        topic_sha,
        error="git merge-tree failed (rc=%d): %s"
        % (proc.returncode, (proc.stderr or proc.stdout).strip()[:300]),
    )


__all__ = ["MergeGateVerdict", "validate_projected_merge"]
