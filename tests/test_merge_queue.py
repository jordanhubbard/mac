"""Tests for the merge-queue projected-merge gate (mac.merge_queue).

Uses a real temporary git repo so the gate is exercised against actual
``git merge-tree`` behavior, not a mock.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mac.merge_queue import (
    validate_projected_merge,
    validate_projected_merge_contract,
)


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
    assert verdict.merged_tree_sha


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


def test_full_contract_runs_on_disposable_current_main_projection(repo: Path):
    _branch_commit(repo, "topic", "topic.txt", "topic\n")
    (repo / "main.txt").write_text("main\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main advances")
    observed = {}

    def runner(remote: str, branch: str, head_sha: str, command: str):
        checkout = Path(remote)
        observed.update(
            {
                "branch": branch,
                "head_sha": head_sha,
                "command": command,
                "checkout_head": _git(checkout, "rev-parse", "HEAD"),
                "topic": (checkout / "topic.txt").read_text(),
                "main": (checkout / "main.txt").read_text(),
            }
        )
        return 0, "full suite passed"

    verdict = validate_projected_merge_contract(
        str(repo),
        "main",
        "topic",
        "make full-contract",
        test_runner=runner,
    )

    assert verdict.passed is True
    assert verdict.test_command == "make full-contract"
    assert verdict.test_returncode == 0
    assert verdict.output_tail == "full suite passed"
    assert observed == {
        "branch": "mac-projected-publication",
        "head_sha": verdict.projected_sha,
        "command": "make full-contract",
        "checkout_head": verdict.projected_sha,
        "topic": "topic\n",
        "main": "main\n",
    }
    # The caller's CURRENT main worktree is never mutated by projection.
    assert not (repo / "topic.txt").exists()
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_full_contract_failure_is_fail_closed(repo: Path):
    _branch_commit(repo, "topic", "topic.txt", "topic\n")

    verdict = validate_projected_merge_contract(
        str(repo),
        "main",
        "topic",
        "scripts/run-contract-tests.sh",
        test_runner=lambda *_args: (17, "suite failed"),
    )

    assert verdict.passed is False
    assert verdict.test_returncode == 17
    assert verdict.output_tail == "suite failed"
    # The error names the reason, not just the category: it is what the caller
    # reads (`error or output_tail`) and what the eviction record keeps.
    assert verdict.error == "full repository contract test failed: suite failed"


def test_full_contract_never_runs_for_conflict_or_empty_command(repo: Path):
    _branch_commit(repo, "topic", "f.txt", "topic\nline2\nline3\n")
    (repo / "f.txt").write_text("main\nline2\nline3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main conflict")
    calls = []

    def runner(*args):
        calls.append(args)
        return 0, ""

    conflict = validate_projected_merge_contract(
        str(repo), "main", "topic", "full-test", test_runner=runner
    )
    empty = validate_projected_merge_contract(
        str(repo), "main", "main", "", test_runner=runner
    )

    assert conflict.passed is False
    assert empty.passed is False
    assert empty.error == "repository contract test command is empty"
    assert calls == []


def test_full_contract_runner_exception_is_fail_closed(repo: Path):
    _branch_commit(repo, "topic", "topic.txt", "topic\n")

    def runner(*_args):
        raise RuntimeError("sandbox unavailable")

    verdict = validate_projected_merge_contract(
        str(repo), "main", "topic", "full-test", test_runner=runner
    )

    assert verdict.passed is False
    assert "sandbox unavailable" in verdict.error
