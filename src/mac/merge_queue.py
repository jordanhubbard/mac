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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from mac.contract_output import capture_failure_window, failure_reason_line

GitRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess"]
ContractTestRunner = Callable[[str, str, str, str], Tuple[int, str]]


@dataclass(frozen=True)
class MergeGateVerdict:
    """Result of validating a branch against the current target tip."""

    clean: bool
    base_sha: str
    topic_sha: str
    conflicted_files: List[str] = field(default_factory=list)
    error: str = ""
    merged_tree_sha: str = ""

    def to_dict(self) -> dict:
        return {
            "schema": "mac.merge_gate.v1",
            "clean": self.clean,
            "base_sha": self.base_sha,
            "topic_sha": self.topic_sha,
            "conflicted_files": list(self.conflicted_files),
            "error": self.error,
            "merged_tree_sha": self.merged_tree_sha,
        }


@dataclass(frozen=True)
class ProjectedMergeContractVerdict:
    """Full repository-contract result for one projected publication tree."""

    passed: bool
    base_sha: str
    topic_sha: str
    merged_tree_sha: str
    projected_sha: str
    test_command: str
    test_returncode: int = -1
    output_tail: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "schema": "mac.projected_merge_contract_gate.v1",
            "passed": self.passed,
            "base_sha": self.base_sha,
            "topic_sha": self.topic_sha,
            "merged_tree_sha": self.merged_tree_sha,
            "projected_sha": self.projected_sha,
            "test_command": self.test_command,
            "test_returncode": self.test_returncode,
            "output_tail": self.output_tail,
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


def _rev_parse_object(run: GitRunner, ref: str) -> str:
    proc = run(["rev-parse", "--verify", ref])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _rev_parse(run: GitRunner, ref: str) -> str:
    return _rev_parse_object(run, "%s^{commit}" % ref)


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
        tree_sha = _rev_parse_object(run, "%s^{tree}" % base_sha)
        if not tree_sha:
            return MergeGateVerdict(
                False,
                base_sha,
                topic_sha,
                error="cannot resolve projected tree for %s" % base_sha,
            )
        return MergeGateVerdict(
            True, base_sha, topic_sha, merged_tree_sha=tree_sha
        )

    proc = run(["merge-tree", "--write-tree", "--name-only", base_sha, topic_sha])
    if proc.returncode == 0:
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        if not lines:
            return MergeGateVerdict(
                False,
                base_sha,
                topic_sha,
                error="git merge-tree returned no projected tree",
            )
        return MergeGateVerdict(
            True, base_sha, topic_sha, merged_tree_sha=lines[0]
        )
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



def _failure_excerpt(exc: BaseException, *, head: int = 220, tail: int = 320) -> str:
    """Keep the head AND tail of a gate failure.

    Taking the first 500 characters spent every one of them on the argv --
    `openshell sandbox create --no-auto-providers --policy ... --label ...` is
    itself about that long -- so the part that says what actually happened
    ("timed out after 2400 seconds", "returned non-zero exit status 3") was cut
    off every time. Two separate debugging sessions ended with the same
    unfinished sentence, and one of them chased an OpenShell bug that did not
    exist.
    """

    text = str(exc).strip()
    if len(text) <= head + tail:
        return text
    return "%s ... [%d chars omitted] ... %s" % (
        text[:head],
        len(text) - head - tail,
        text[-tail:],
    )


def validate_projected_merge_contract(
    repo_dir: str,
    base_ref: str,
    topic_ref: str,
    test_command: str,
    *,
    test_runner: ContractTestRunner,
    merge_gate: Optional[MergeGateVerdict] = None,
) -> ProjectedMergeContractVerdict:
    """Run the full repository contract on the CURRENT-main projected tree.

    The projected tree comes from :func:`validate_projected_merge`. It is
    materialized in a disposable standalone clone, never in the caller's main
    worktree. ``test_runner`` owns the execution boundary (the control plane
    supplies its existing OpenShell verifier); this module only prepares the
    exact checkout and refuses publication on every preparation or test error.
    """

    command = str(test_command or "").strip()
    gate = merge_gate or validate_projected_merge(repo_dir, base_ref, topic_ref)

    def verdict(
        passed: bool,
        *,
        projected_sha: str = "",
        returncode: int = -1,
        output_tail: str = "",
        error: str = "",
    ) -> ProjectedMergeContractVerdict:
        return ProjectedMergeContractVerdict(
            passed=passed,
            base_sha=gate.base_sha,
            topic_sha=gate.topic_sha,
            merged_tree_sha=gate.merged_tree_sha,
            projected_sha=projected_sha,
            test_command=command,
            test_returncode=returncode,
            # NOT a tail. `run-contract-tests.sh` prints the failure first and
            # a ~14KB whole-repo coverage table afterwards, so the last 2000
            # bytes of a failing run are the coverage table and a generic
            # "ssh exited with status 1" -- the reason is thousands of bytes
            # earlier. This gate's runner is the hub verifier, which already
            # anchored its capture; a tail here re-truncated the anchored
            # window back out and put a genuine rejection behind the transport
            # message a second time. capture_failure_window composes with
            # itself, so applying it twice keeps the reason.
            output_tail=capture_failure_window(output_tail),
            error=error,
        )

    if not gate.clean:
        return verdict(False, error=gate.error or "projected merge is not clean")
    if not gate.merged_tree_sha:
        return verdict(False, error="projected merge has no merged tree")
    if not command:
        return verdict(False, error="repository contract test command is empty")

    try:
        with tempfile.TemporaryDirectory(prefix="mac-projected-merge-") as raw:
            checkout = Path(raw) / "repo"
            clone = subprocess.run(
                [
                    "git",
                    "clone",
                    "--no-checkout",
                    "--no-hardlinks",
                    "--",
                    str(Path(repo_dir).resolve()),
                    str(checkout),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if clone.returncode != 0:
                return verdict(
                    False,
                    error="could not clone projected merge checkout: %s"
                    % ((clone.stderr or clone.stdout) or "non-zero exit").strip()[:500],
                )
            run = _default_git_runner(str(checkout))
            base_contains_topic = run(
                ["merge-base", "--is-ancestor", gate.topic_sha, gate.base_sha]
            ).returncode == 0
            topic_contains_base = run(
                ["merge-base", "--is-ancestor", gate.base_sha, gate.topic_sha]
            ).returncode == 0
            if base_contains_topic:
                projected_sha = gate.base_sha
            elif topic_contains_base:
                projected_sha = gate.topic_sha
            else:
                commit = run(
                    [
                        "-c",
                        "user.name=MAC Merge Queue",
                        "-c",
                        "user.email=merge-queue@mac.invalid",
                        "commit-tree",
                        gate.merged_tree_sha,
                        "-p",
                        gate.base_sha,
                        "-p",
                        gate.topic_sha,
                        "-m",
                        "MAC projected publication gate",
                    ]
                )
                projected_sha = commit.stdout.strip() if commit.returncode == 0 else ""
                if not projected_sha:
                    return verdict(
                        False,
                        error="could not commit projected merge tree: %s"
                        % ((commit.stderr or commit.stdout) or "non-zero exit").strip()[:500],
                    )
            branch = "mac-projected-publication"
            checkout_result = run(["checkout", "-q", "-B", branch, projected_sha])
            if checkout_result.returncode != 0:
                return verdict(
                    False,
                    projected_sha=projected_sha,
                    error="could not check out projected merge: %s"
                    % (
                        (checkout_result.stderr or checkout_result.stdout)
                        or "non-zero exit"
                    ).strip()[:500],
                )
            actual_tree = _rev_parse_object(run, "HEAD^{tree}")
            if actual_tree != gate.merged_tree_sha:
                return verdict(
                    False,
                    projected_sha=projected_sha,
                    error="projected checkout tree does not match merge-tree result",
                )
            returncode, output = test_runner(
                str(checkout), branch, projected_sha, command
            )
            rc = int(returncode)
            tail = str(output or "")
            if rc != 0:
                # Name the reason in `error`, not only in `output_tail`. The
                # caller reads `error or output_tail`, so a constant here meant
                # the tail was never consulted and every refused publication
                # reported the same eight words -- true of every failing gate
                # and diagnostic of none. The eviction reason it feeds is cut
                # to 200 characters, which is why this is one line rather than
                # an excerpt.
                return verdict(
                    False,
                    projected_sha=projected_sha,
                    returncode=rc,
                    output_tail=tail,
                    error="full repository contract test failed: %s"
                    % (failure_reason_line(tail) or "no output captured"),
                )
            return verdict(
                True,
                projected_sha=projected_sha,
                returncode=0,
                output_tail=tail,
            )
    except Exception as exc:  # noqa: BLE001 - publication gate must fail closed.
        return verdict(
            False, error="projected contract gate failed: %s" % _failure_excerpt(exc)
        )


__all__ = [
    "MergeGateVerdict",
    "ProjectedMergeContractVerdict",
    "validate_projected_merge",
    "validate_projected_merge_contract",
]
