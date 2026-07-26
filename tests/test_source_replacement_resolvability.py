"""Source-replacement resolvability + linked-worktree preservation coverage.

``tests/test_worktree_preservation.py`` exercises the primitive with fully
injected ``candidate_provider`` seams, so it never proves that a *replaced*
source stays resolvable across the REAL candidate surface the production worker
uses (:func:`mac.worker._repository_source_candidates`).  This module closes
that gap for ``mac.worktree_preservation`` by asserting the candidate ordering
end-to-end through :func:`~mac.worktree_preservation.resolve_source_path`:

* a source that moves to any candidate location (declared path, ``.mac``-home
  relative fallback, ``mac_home/src/<name>``, and the ``self_update_repo`` slot
  that is promoted to the front for ``repository_name == 'mac'`` /
  ``source == 'repo-beads-mac'``) is still resolved after the declared path
  disappears;
* resolution fails closed to the declared path when nothing exists yet, and the
  preservation decision raises rather than silently returning "nothing to
  preserve" when the resolved source is unresolvable or its worktrees cannot be
  enumerated;
* an existing linked worktree tied to the OLD source is preserved (not
  orphaned) when the resolved source path changes, using an injected git seam so
  no live remote/worktree is required; and
* the versioned schema string is emitted on both the decision and its dict.

Follows the injectable-seam patterns in ``tests/test_new_file_recovery_staging.py``
and ``tests/test_repository_recovery.py``: the ``MAC_HOME`` relocation knob and
the ``git_runner`` seam are the only interfaces the tests reach through, so the
real candidate resolver runs without touching a fleet host's live ``~/.mac``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mac.worktree_preservation import (
    PRESERVED_SOURCE_WORKTREES_SCHEMA,
    SourceWorktreePreservation,
    SourceWorktreePreservationError,
    decide_source_worktree_preservation,
    resolve_source_path,
)


@pytest.fixture(autouse=True)
def _hermetic_mac_home(monkeypatch, tmp_path):
    """Pin ``MAC_HOME`` so the real candidate resolver is deterministic.

    ``mac.worker._repository_source_candidates`` consults
    ``mac_paths.mac_home()`` (``MAC_HOME`` override, else ``~/.mac``).  A bare
    ``pytest`` run on a fleet host would otherwise leak the live home into the
    candidate list; anchoring it to a temp home keeps the ordering assertions
    hermetic regardless of environment.
    """

    home = tmp_path / "machome"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MAC_HOME", str(home))
    return home


def _porcelain(source: Path, *linked: Path) -> str:
    lines = [
        "worktree %s" % source,
        "HEAD aaad4190275c5b0ec025ca41b6aa923c9cfd924d",
        "branch refs/heads/main",
        "",
    ]
    for wt in linked:
        lines += [
            "worktree %s" % wt,
            "HEAD bbbd4190275c5b0ec025ca41b6aa923c9cfd924d",
            "branch refs/heads/%s" % wt.name,
            "",
        ]
    return "\n".join(lines) + "\n"


def _runner_for(source: Path, *linked: Path):
    """A ``git_runner`` seam returning porcelain for ``source`` + ``linked``."""

    calls: list[tuple[str, tuple[str, ...]]] = []

    def runner(repo, args):
        calls.append((str(repo), tuple(args)))
        return subprocess.CompletedProcess(
            [], returncode=0, stdout=_porcelain(source, *linked), stderr=""
        )

    return runner, calls


# ---------------------------------------------------------------------------
# (a) Resolvability across each candidate location using the REAL candidate
#     surface (mac.worker._repository_source_candidates), i.e. no injected
#     candidate_provider.
# ---------------------------------------------------------------------------


def test_resolves_declared_path_when_it_exists(tmp_path, _hermetic_mac_home):
    declared = tmp_path / "declared" / "mac"
    declared.mkdir(parents=True)
    origin = {"repository_path": str(declared), "repository_name": "widget"}

    resolved = resolve_source_path(origin, tmp_path / "self_update_repo")

    assert resolved == declared


def test_resolves_mac_home_relative_fallback_after_replacement(
    tmp_path, _hermetic_mac_home
):
    # Declared path lives under a ``.mac`` root that no longer exists, but the
    # same suffix under the (relocated) MAC_HOME does -> the .mac-home relative
    # candidate keeps the replaced source resolvable.
    declared = tmp_path / "old_home" / ".mac" / "src" / "widget"
    origin = {"repository_path": str(declared), "repository_name": "widget"}
    relocated = _hermetic_mac_home / "src" / "widget"
    relocated.mkdir(parents=True)

    resolved = resolve_source_path(origin, tmp_path / "self_update_repo")

    assert resolved == relocated
    assert not declared.exists()


def test_resolves_mac_home_src_name_candidate(tmp_path, _hermetic_mac_home):
    # Declared path is gone and shares no ``.mac`` suffix with MAC_HOME, so the
    # ``mac_home/src/<name>`` candidate is what keeps it resolvable.
    declared = tmp_path / "somewhere" / "gone"
    origin = {"repository_path": str(declared), "repository_name": "widget"}
    by_name = _hermetic_mac_home / "src" / "widget"
    by_name.mkdir(parents=True)

    resolved = resolve_source_path(origin, tmp_path / "self_update_repo")

    assert resolved == by_name


def test_self_update_repo_is_preferred_for_repository_name_mac(
    tmp_path, _hermetic_mac_home
):
    # For repository_name == 'mac' the self_update_repo is inserted at the FRONT
    # of the candidate list, so it wins even when the declared path also exists.
    declared = tmp_path / "declared_mac"
    declared.mkdir()
    self_update_repo = tmp_path / "self_update_repo"
    self_update_repo.mkdir()
    origin = {"repository_path": str(declared), "repository_name": "mac"}

    resolved = resolve_source_path(origin, self_update_repo)

    assert resolved == self_update_repo


def test_self_update_repo_is_preferred_for_source_repo_beads_mac(
    tmp_path, _hermetic_mac_home
):
    declared = tmp_path / "declared_beads"
    declared.mkdir()
    self_update_repo = tmp_path / "self_update_repo"
    self_update_repo.mkdir()
    origin = {"repository_path": str(declared), "source": "repo-beads-mac"}

    resolved = resolve_source_path(origin, self_update_repo)

    assert resolved == self_update_repo


def test_declared_path_wins_over_self_update_for_non_mac_source(
    tmp_path, _hermetic_mac_home
):
    # A regular project does NOT promote self_update_repo to the front, so an
    # existing declared path is resolved even though self_update_repo exists.
    declared = tmp_path / "declared_widget"
    declared.mkdir()
    self_update_repo = tmp_path / "self_update_repo"
    self_update_repo.mkdir()
    origin = {"repository_path": str(declared), "repository_name": "widget"}

    resolved = resolve_source_path(origin, self_update_repo)

    assert resolved == declared


# ---------------------------------------------------------------------------
# (b) Fail-closed behaviour when the source is unresolvable / missing.
# ---------------------------------------------------------------------------


def test_resolve_falls_back_to_declared_when_no_candidate_exists(
    tmp_path, _hermetic_mac_home
):
    declared = tmp_path / "declared" / "gone"
    origin = {"repository_path": str(declared), "repository_name": "widget"}

    resolved = resolve_source_path(origin, tmp_path / "self_update_repo")

    # Fail-closed fallback is the declared path even though nothing exists yet.
    assert resolved == declared
    assert not resolved.exists()


def test_decide_fails_closed_when_resolved_source_missing(
    tmp_path, _hermetic_mac_home
):
    declared = tmp_path / "declared" / "gone"
    origin = {"repository_path": str(declared), "repository_name": "widget"}
    runner, _calls = _runner_for(declared)  # never reached; resolution fails first

    with pytest.raises(SourceWorktreePreservationError):
        decide_source_worktree_preservation(
            origin,
            tmp_path / "self_update_repo",
            previous_source=tmp_path / "old",
            git_runner=runner,
        )


def test_decide_fails_closed_on_worktree_enumeration_error(
    tmp_path, _hermetic_mac_home
):
    source = _hermetic_mac_home / "src" / "widget"
    source.mkdir(parents=True)
    origin = {"repository_path": str(tmp_path / "gone"), "repository_name": "widget"}

    def failing_runner(_repo, _args):
        return subprocess.CompletedProcess([], returncode=128, stdout="", stderr="fatal")

    with pytest.raises(SourceWorktreePreservationError):
        decide_source_worktree_preservation(
            origin,
            tmp_path / "self_update_repo",
            previous_source=tmp_path / "old",
            git_runner=failing_runner,
        )


# ---------------------------------------------------------------------------
# (c) Worktree preservation decision via injected git seams (no live worktree).
# ---------------------------------------------------------------------------


def test_replaced_source_preserves_existing_linked_worktree(
    tmp_path, _hermetic_mac_home
):
    # Source has moved to the mac_home/src/<name> candidate; the old declared
    # path is gone.  A linked worktree tied to the (now relocated) source must
    # be preserved, not orphaned.
    relocated = _hermetic_mac_home / "src" / "widget"
    relocated.mkdir(parents=True)
    linked = tmp_path / "worktrees" / "task-a"
    origin = {"repository_path": str(tmp_path / "old" / "widget"), "repository_name": "widget"}
    runner, calls = _runner_for(relocated, linked)

    decision = decide_source_worktree_preservation(
        origin,
        tmp_path / "self_update_repo",
        previous_source=tmp_path / "old" / "widget",
        git_runner=runner,
    )

    assert isinstance(decision, SourceWorktreePreservation)
    assert decision.resolved_source == relocated
    assert decision.source_changed is True
    assert decision.preserve is True
    assert [str(w.path) for w in decision.linked_worktrees] == [str(linked)]
    # The injected git seam is the only worktree interface reached.
    assert calls and calls[0][1][:2] == ("worktree", "list")


def test_unchanged_source_does_not_request_preservation(
    tmp_path, _hermetic_mac_home
):
    source = _hermetic_mac_home / "src" / "widget"
    source.mkdir(parents=True)
    linked = tmp_path / "worktrees" / "task-a"
    origin = {"repository_path": str(tmp_path / "gone"), "repository_name": "widget"}
    runner, _calls = _runner_for(source, linked)

    decision = decide_source_worktree_preservation(
        origin,
        tmp_path / "self_update_repo",
        previous_source=source,  # same as the resolved candidate
        git_runner=runner,
    )

    assert decision.source_changed is False
    assert decision.preserve is False


def test_changed_source_without_linked_worktrees_does_not_preserve(
    tmp_path, _hermetic_mac_home
):
    source = _hermetic_mac_home / "src" / "widget"
    source.mkdir(parents=True)
    origin = {"repository_path": str(tmp_path / "gone"), "repository_name": "widget"}
    runner, _calls = _runner_for(source)  # no linked worktrees

    decision = decide_source_worktree_preservation(
        origin,
        tmp_path / "self_update_repo",
        previous_source=tmp_path / "old",
        git_runner=runner,
    )

    assert decision.source_changed is True
    assert decision.preserve is False
    assert decision.linked_worktrees == []


# ---------------------------------------------------------------------------
# (d) Versioned schema string is emitted correctly.
# ---------------------------------------------------------------------------


def test_versioned_schema_string_on_decision_and_dict(tmp_path, _hermetic_mac_home):
    source = _hermetic_mac_home / "src" / "widget"
    source.mkdir(parents=True)
    linked = tmp_path / "worktrees" / "task-a"
    origin = {"repository_path": str(tmp_path / "gone"), "repository_name": "widget"}
    runner, _calls = _runner_for(source, linked)

    decision = decide_source_worktree_preservation(
        origin,
        tmp_path / "self_update_repo",
        previous_source=tmp_path / "old",
        git_runner=runner,
    )

    assert PRESERVED_SOURCE_WORKTREES_SCHEMA == "mac.preserved_source_worktrees.v1"
    assert decision.schema == PRESERVED_SOURCE_WORKTREES_SCHEMA
    payload = decision.to_dict()
    assert payload["schema"] == PRESERVED_SOURCE_WORKTREES_SCHEMA
    assert payload["resolved_source"] == str(source)
    assert payload["preserve"] is True
    assert payload["linked_worktrees"][0]["path"] == str(linked)
