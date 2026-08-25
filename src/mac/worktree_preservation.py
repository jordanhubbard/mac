"""Source-replacement resolvability and linked-worktree preservation primitive.

When a registered source repository is replaced, relocated, or re-registered,
the source path recorded in a task origin can stop pointing at the checkout that
still owns the active task worktrees.  Left unhandled, ``git worktree add``
against the new source cannot see the linked worktrees the old source created,
so those worktrees are orphaned: their administrative ``.git`` pointers dangle
and the verified work inside them becomes unreachable.

This module is the small, injectable, unit-testable primitive that:

1. Resolves a source path across its candidate locations (declared path,
   ``.mac``-home relative, ``mac_home/src/<name>``, ``self_update_repo``) using
   the same candidate surface as :func:`mac.worker._repository_source_candidates`
   so a replaced source is still resolvable.
2. Detects the linked git worktrees registered against the resolved source and
   decides, fail-closed, whether they must be preserved because the resolved
   source path changed.

It performs **pure resolution and a preservation decision only**.  It never runs
an executor or a model, and every git interaction is funnelled through an
injectable ``git_runner`` seam so the decision logic is testable without a live
repository.  The result carries a versioned schema string and mirrors the
preservation schemas used by :mod:`mac.executor_finalizer` and
:mod:`mac.repository_recovery`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

JsonDict = Dict[str, Any]

PRESERVED_SOURCE_WORKTREES_SCHEMA = "mac.preserved_source_worktrees.v1"

GitRunner = Callable[[Path, Sequence[str]], "subprocess.CompletedProcess[str]"]


class SourceWorktreePreservationError(RuntimeError):
    """The resolved source is missing/unresolvable or cannot be inspected."""


def _default_git_runner(repo: Path, args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@dataclass(frozen=True)
class LinkedWorktree:
    """One linked worktree registered against a source repository."""

    path: Path
    head: str
    branch: str
    detached: bool
    locked: bool
    prunable: bool

    def to_dict(self) -> JsonDict:
        return {
            "path": str(self.path),
            "head": self.head,
            "branch": self.branch,
            "detached": self.detached,
            "locked": self.locked,
            "prunable": self.prunable,
        }


@dataclass(frozen=True)
class SourceWorktreePreservation:
    """Resolution + preservation decision for a (possibly replaced) source.

    ``preserve`` is ``True`` only when the resolved source path differs from the
    previously recorded source path *and* linked worktrees exist that would be
    orphaned by the relocation.  ``schema`` is the versioned wire tag; the
    dataclass is frozen so callers cannot mutate a decision after the fact.
    """

    schema: str
    resolved_source: Path
    previous_source: Optional[Path]
    source_changed: bool
    preserve: bool
    linked_worktrees: List[LinkedWorktree] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> JsonDict:
        return {
            "schema": self.schema,
            "resolved_source": str(self.resolved_source),
            "previous_source": (
                str(self.previous_source) if self.previous_source is not None else None
            ),
            "source_changed": self.source_changed,
            "preserve": self.preserve,
            "linked_worktrees": [w.to_dict() for w in self.linked_worktrees],
            "reason": self.reason,
        }


def _normalize(path: Optional[Path]) -> Optional[Path]:
    if path is None:
        return None
    return Path(path).expanduser()


def _same_path(left: Optional[Path], right: Optional[Path]) -> bool:
    if left is None or right is None:
        return False
    return str(left) == str(right)


def resolve_source_path(
    origin: JsonDict,
    self_update_repo: Path,
    *,
    candidate_provider: Optional[Callable[[JsonDict, Path], Sequence[Path]]] = None,
    path_exists: Callable[[Path], bool] = Path.exists,
) -> Path:
    """Resolve the current source path across its candidate locations.

    Mirrors :meth:`mac.worker_repo_prep.RepositoryWorktreePreparer.
    _resolve_repository_source_path`: the first existing candidate wins, and the
    declared path is the fail-closed fallback when nothing exists yet.  The
    candidate surface is injectable so the primitive stays unit-testable without
    importing the worker; by default it uses
    :func:`mac.worker._repository_source_candidates`.
    """

    if candidate_provider is None:
        from mac.worker import _repository_source_candidates  # noqa: PLC0415

        candidate_provider = _repository_source_candidates

    candidates = list(candidate_provider(origin, self_update_repo))
    for candidate in candidates:
        if path_exists(candidate):
            return Path(candidate).expanduser()
    declared = str(origin.get("repository_path") or "").strip()
    return Path(declared).expanduser()


def parse_linked_worktrees(porcelain: str, source: Path) -> List[LinkedWorktree]:
    """Parse ``git worktree list --porcelain`` into linked worktrees only.

    The main worktree (the source checkout itself) is excluded so callers see
    exactly the linked worktrees that would be orphaned by a relocation.
    """

    source_key = str(Path(source).expanduser())
    records: List[LinkedWorktree] = []
    current: JsonDict = {}

    def flush() -> None:
        raw_path = current.get("worktree")
        if not raw_path:
            current.clear()
            return
        wt_path = Path(str(raw_path)).expanduser()
        current.clear()
        if str(wt_path) == source_key:
            return
        records.append(
            LinkedWorktree(
                path=wt_path,
                head=str(current_head or ""),
                branch=str(current_branch or ""),
                detached=bool(current_detached),
                locked=bool(current_locked),
                prunable=bool(current_prunable),
            )
        )

    current_head = ""
    current_branch = ""
    current_detached = False
    current_locked = False
    current_prunable = False
    for raw_line in porcelain.splitlines():
        line = raw_line.rstrip("\n")
        if not line.strip():
            flush()
            current_head = ""
            current_branch = ""
            current_detached = False
            current_locked = False
            current_prunable = False
            continue
        if line.startswith("worktree "):
            # Starting a fresh record; if the previous one was not terminated by
            # a blank line, flush it first.
            if current.get("worktree"):
                flush()
                current_head = ""
                current_branch = ""
                current_detached = False
                current_locked = False
                current_prunable = False
            current["worktree"] = line[len("worktree ") :].strip()
        elif line.startswith("HEAD "):
            current_head = line[len("HEAD ") :].strip()
        elif line.startswith("branch "):
            current_branch = line[len("branch ") :].strip()
        elif line.strip() == "detached":
            current_detached = True
        elif line.strip() == "locked" or line.startswith("locked "):
            current_locked = True
        elif line.strip() == "prunable" or line.startswith("prunable "):
            current_prunable = True
    flush()
    return records


def list_linked_worktrees(source: Path, *, git_runner: GitRunner) -> List[LinkedWorktree]:
    """List linked worktrees registered against ``source`` (fail-closed)."""

    result = git_runner(source, ["worktree", "list", "--porcelain"])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or str(source)
        raise SourceWorktreePreservationError(
            "could not list git worktrees for source %s: %s" % (source, detail)
        )
    return parse_linked_worktrees(result.stdout or "", source)


def decide_source_worktree_preservation(
    origin: JsonDict,
    self_update_repo: Path,
    *,
    previous_source: Optional[Path] = None,
    git_runner: Optional[GitRunner] = None,
    candidate_provider: Optional[Callable[[JsonDict, Path], Sequence[Path]]] = None,
    path_exists: Callable[[Path], bool] = Path.exists,
) -> SourceWorktreePreservation:
    """Resolve the source and decide whether linked worktrees must be preserved.

    Fail-closed contract:

    * The resolved source must exist on disk; an unresolvable source raises
      :class:`SourceWorktreePreservationError` rather than silently returning a
      no-preservation decision.
    * Worktree enumeration failures raise rather than assume "nothing to
      preserve".

    ``preserve`` is ``True`` only when the resolved source path differs from
    ``previous_source`` and at least one linked worktree exists.  Passing
    ``previous_source=None`` (no prior recorded source) is treated as "not
    changed" so a first-time resolution never spuriously requests preservation.
    """

    runner = git_runner if git_runner is not None else _default_git_runner
    resolved = resolve_source_path(
        origin,
        self_update_repo,
        candidate_provider=candidate_provider,
        path_exists=path_exists,
    )
    if not path_exists(resolved):
        raise SourceWorktreePreservationError(
            "resolved repository source path does not exist: %s" % resolved
        )

    prior = _normalize(previous_source)
    resolved_norm = _normalize(resolved)
    source_changed = prior is not None and not _same_path(prior, resolved_norm)

    linked = list_linked_worktrees(resolved, git_runner=runner)

    if not source_changed:
        reason = (
            "resolved source unchanged from previously recorded source"
            if prior is not None
            else "no previously recorded source; nothing to preserve"
        )
        preserve = False
    elif not linked:
        reason = "resolved source changed but no linked worktrees to preserve"
        preserve = False
    else:
        reason = "resolved source changed from %s to %s with %d linked worktree(s) to preserve" % (
            prior,
            resolved_norm,
            len(linked),
        )
        preserve = True

    return SourceWorktreePreservation(
        schema=PRESERVED_SOURCE_WORKTREES_SCHEMA,
        resolved_source=resolved_norm if resolved_norm is not None else resolved,
        previous_source=prior,
        source_changed=source_changed,
        preserve=preserve,
        linked_worktrees=linked,
        reason=reason,
    )
