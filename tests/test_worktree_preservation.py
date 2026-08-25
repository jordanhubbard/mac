from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mac.worktree_preservation import (
    PRESERVED_SOURCE_WORKTREES_SCHEMA,
    LinkedWorktree,
    SourceWorktreePreservation,
    SourceWorktreePreservationError,
    decide_source_worktree_preservation,
    list_linked_worktrees,
    parse_linked_worktrees,
    resolve_source_path,
)


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _make_source_with_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "src"
    _git(tmp_path, "init", "-q", str(source))
    _git(source, "config", "user.email", "tests@example.invalid")
    _git(source, "config", "user.name", "tests")
    (source / "f").write_text("hi\n", encoding="utf-8")
    _git(source, "add", "f")
    _git(source, "commit", "-qm", "init")
    linked = tmp_path / "linked1"
    _git(source, "worktree", "add", "-q", str(linked))
    return source, linked


PORCELAIN = """\
worktree /repo/src
HEAD aaad4190275c5b0ec025ca41b6aa923c9cfd924d
branch refs/heads/master

worktree /repo/linked1
HEAD aaad4190275c5b0ec025ca41b6aa923c9cfd924d
branch refs/heads/linked1

worktree /repo/linked2
HEAD bbbd4190275c5b0ec025ca41b6aa923c9cfd924d
detached
locked
"""


def test_parse_linked_worktrees_excludes_main_and_flags_state():
    linked = parse_linked_worktrees(PORCELAIN, Path("/repo/src"))
    assert [str(w.path) for w in linked] == ["/repo/linked1", "/repo/linked2"]
    assert linked[0].branch == "refs/heads/linked1"
    assert linked[0].detached is False
    assert linked[1].detached is True
    assert linked[1].locked is True


def test_resolve_source_path_prefers_first_existing_candidate(tmp_path: Path):
    existing = tmp_path / "here"
    existing.mkdir()
    missing = tmp_path / "gone"
    origin = {"repository_path": str(missing)}

    def candidates(_origin, _self_update):
        return [missing, existing]

    resolved = resolve_source_path(origin, tmp_path / "self", candidate_provider=candidates)
    assert resolved == existing


def test_resolve_source_path_falls_back_to_declared_when_none_exist(tmp_path: Path):
    declared = tmp_path / "declared"
    origin = {"repository_path": str(declared)}

    resolved = resolve_source_path(
        origin,
        tmp_path / "self",
        candidate_provider=lambda *_: [tmp_path / "a", tmp_path / "b"],
    )
    assert resolved == declared


def test_list_linked_worktrees_fails_closed_on_git_error(tmp_path: Path):
    def failing_runner(_repo, _args):
        return subprocess.CompletedProcess([], returncode=1, stdout="", stderr="boom")

    with pytest.raises(SourceWorktreePreservationError):
        list_linked_worktrees(tmp_path, git_runner=failing_runner)


def test_decide_preserve_when_source_changed_and_worktrees_exist(tmp_path: Path):
    source, linked = _make_source_with_linked_worktree(tmp_path)
    origin = {"repository_path": str(source)}

    decision = decide_source_worktree_preservation(
        origin,
        tmp_path / "self",
        previous_source=tmp_path / "old-source",
        candidate_provider=lambda *_: [source],
    )
    assert isinstance(decision, SourceWorktreePreservation)
    assert decision.schema == PRESERVED_SOURCE_WORKTREES_SCHEMA
    assert decision.source_changed is True
    assert decision.preserve is True
    assert str(linked) in [str(w.path) for w in decision.linked_worktrees]
    # Round-trips to a JSON-safe dict carrying the versioned schema.
    payload = decision.to_dict()
    assert payload["schema"] == PRESERVED_SOURCE_WORKTREES_SCHEMA
    assert payload["preserve"] is True


def test_decide_no_preserve_when_source_unchanged(tmp_path: Path):
    source, _ = _make_source_with_linked_worktree(tmp_path)
    origin = {"repository_path": str(source)}

    decision = decide_source_worktree_preservation(
        origin,
        tmp_path / "self",
        previous_source=source,
        candidate_provider=lambda *_: [source],
    )
    assert decision.source_changed is False
    assert decision.preserve is False


def test_decide_no_preserve_when_no_previous_source(tmp_path: Path):
    source, _ = _make_source_with_linked_worktree(tmp_path)
    origin = {"repository_path": str(source)}

    decision = decide_source_worktree_preservation(
        origin,
        tmp_path / "self",
        previous_source=None,
        candidate_provider=lambda *_: [source],
    )
    assert decision.source_changed is False
    assert decision.preserve is False


def test_decide_no_preserve_when_changed_but_no_linked_worktrees(tmp_path: Path):
    source = tmp_path / "src"
    _git(tmp_path, "init", "-q", str(source))
    _git(source, "config", "user.email", "tests@example.invalid")
    _git(source, "config", "user.name", "tests")
    (source / "f").write_text("hi\n", encoding="utf-8")
    _git(source, "add", "f")
    _git(source, "commit", "-qm", "init")
    origin = {"repository_path": str(source)}

    decision = decide_source_worktree_preservation(
        origin,
        tmp_path / "self",
        previous_source=tmp_path / "old",
        candidate_provider=lambda *_: [source],
    )
    assert decision.source_changed is True
    assert decision.preserve is False
    assert decision.linked_worktrees == []


def test_decide_fails_closed_on_unresolvable_source(tmp_path: Path):
    origin = {"repository_path": str(tmp_path / "nope")}
    with pytest.raises(SourceWorktreePreservationError):
        decide_source_worktree_preservation(
            origin,
            tmp_path / "self",
            previous_source=tmp_path / "old",
            candidate_provider=lambda *_: [tmp_path / "nope"],
        )


def test_git_runner_seam_is_injectable(tmp_path: Path):
    existing = tmp_path / "src"
    existing.mkdir()
    origin = {"repository_path": str(existing)}
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_runner(repo, args):
        calls.append((str(repo), tuple(args)))
        return subprocess.CompletedProcess(
            [],
            returncode=0,
            stdout="worktree %s\nHEAD deadbeef\nbranch refs/heads/main\n\n"
            "worktree %s/../linked\nHEAD deadbeef\nbranch refs/heads/wt\n" % (existing, existing),
            stderr="",
        )

    decision = decide_source_worktree_preservation(
        origin,
        tmp_path / "self",
        previous_source=tmp_path / "old",
        candidate_provider=lambda *_: [existing],
        git_runner=fake_runner,
    )
    assert calls, "injected git_runner should have been used"
    assert decision.preserve is True
    assert len(decision.linked_worktrees) == 1
