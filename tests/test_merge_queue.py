"""Tests for the merge-queue projected-merge gate (mac.merge_queue).

Uses a real temporary git repo so the gate is exercised against actual
``git merge-tree`` behavior, not a mock.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mac.merge_queue import validate_projected_merge


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.invalid")
    _git(r, "config", "user.name", "t")
    (r / "f.txt").write_text("line1\nline2\nline3\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def _branch_commit(repo: Path, branch: str, path: str, content: str) -> None:
    _git(repo, "checkout", "-q", "-b", branch, "main")
    (repo / path).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "%s change" % branch)
    _git(repo, "checkout", "-q", "main")


def test_clean_merge_different_files(repo: Path):
    _branch_commit(repo, "topic", "new_file.txt", "brand new\n")
    verdict = validate_projected_merge(str(repo), "main", "topic")
    assert verdict.clean is True
    assert verdict.conflicted_files == []
    assert verdict.base_sha and verdict.topic_sha


def test_clean_merge_nonoverlapping_edits(repo: Path):
    # Edit disjoint regions of the same file -> clean 3-way merge.
    _branch_commit(repo, "topic", "f.txt", "line1-EDITED\nline2\nline3\n")
    verdict = validate_projected_merge(str(repo), "main", "topic")
    assert verdict.clean is True


def test_conflicting_edits_same_lines(repo: Path):
    # main advances on the SAME line the topic branch edits -> conflict, i.e.
    # the topic was authored on a now-stale base. The gate must catch this.
    _branch_commit(repo, "topic", "f.txt", "line1-FROM-TOPIC\nline2\nline3\n")
    # Advance main on the same first line.
    (repo / "f.txt").write_text("line1-FROM-MAIN\nline2\nline3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main advances")

    verdict = validate_projected_merge(str(repo), "main", "topic")
    assert verdict.clean is False
    assert "f.txt" in verdict.conflicted_files


def test_up_to_date_branch_is_trivially_clean(repo: Path):
    verdict = validate_projected_merge(str(repo), "main", "main")
    assert verdict.clean is True


def test_bad_ref_fails_closed(repo: Path):
    verdict = validate_projected_merge(str(repo), "main", "does-not-exist")
    assert verdict.clean is False
    assert "topic ref" in verdict.error


def test_verdict_serializes():
    from mac.merge_queue import MergeGateVerdict

    v = MergeGateVerdict(False, "abc", "def", conflicted_files=["a.py"])
    d = v.to_dict()
    assert d["schema"] == "mac.merge_gate.v1"
    assert d["clean"] is False and d["conflicted_files"] == ["a.py"]
