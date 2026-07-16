"""Tests for the advisory pre-dispatch conflict gate (mac.predispatch_conflict).

Like the merge-queue gate it wraps, this exercises the prediction against a real
temporary git repo so ``git merge-tree`` behavior is real, not mocked. The
module is a fail-open (advisory) decision layer on top of
``mac.merge_queue.validate_projected_merge``: it must predict a conflict against
the current base tip *and* against in-flight refs expected to land first, and it
must honor the ``advisory`` fail policy on ref/merge errors.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mac.predispatch_conflict import (
    PredispatchVerdict,
    check_predispatch_conflict,
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


# ---------------------------------------------------------------------------
# Positive case: the base tip moved under the topic on the lines it edits.
# ---------------------------------------------------------------------------


def test_conflict_with_current_base_tip_is_predicted(repo: Path):
    # Topic edits line1; main then advances on the SAME line -> the topic was
    # authored on a now-stale base and would conflict if dispatched now.
    _branch_commit(repo, "topic", "f.txt", "line1-FROM-TOPIC\nline2\nline3\n")
    (repo / "f.txt").write_text("line1-FROM-MAIN\nline2\nline3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main advances")

    verdict = check_predispatch_conflict(str(repo), "main", "topic")

    assert verdict.would_conflict is True
    assert verdict.conflicting_ref == ""  # conflicts with the base tip itself
    assert "f.txt" in verdict.conflicted_files
    assert verdict.advisory is True
    assert verdict.error == ""


# ---------------------------------------------------------------------------
# Clean / no-conflict case.
# ---------------------------------------------------------------------------


def test_clean_case_passes(repo: Path):
    _branch_commit(repo, "topic", "new_file.txt", "brand new\n")

    verdict = check_predispatch_conflict(str(repo), "main", "topic")

    assert verdict.would_conflict is False
    assert verdict.conflicting_ref == ""
    assert verdict.conflicted_files == []
    assert verdict.error == ""


def test_nonoverlapping_edits_same_file_are_clean(repo: Path):
    _branch_commit(repo, "topic", "f.txt", "line1-EDITED\nline2\nline3\n")

    verdict = check_predispatch_conflict(str(repo), "main", "topic")

    assert verdict.would_conflict is False


def test_up_to_date_topic_is_clean(repo: Path):
    verdict = check_predispatch_conflict(str(repo), "main", "main")

    assert verdict.would_conflict is False
    assert verdict.conflicted_files == []
    assert verdict.error == ""


# ---------------------------------------------------------------------------
# In-flight refs: clean against base, but conflicts with a peer that lands first.
# ---------------------------------------------------------------------------


def test_conflict_with_in_flight_ref_is_predicted(repo: Path):
    # topic and other both edit line1 differently; each is clean against main,
    # but if `other` lands first, dispatching `topic` now would conflict.
    _branch_commit(repo, "topic", "f.txt", "line1-TOPIC\nline2\nline3\n")
    _branch_commit(repo, "other", "f.txt", "line1-OTHER\nline2\nline3\n")

    verdict = check_predispatch_conflict(
        str(repo), "main", "topic", in_flight_refs=["other"]
    )

    assert verdict.would_conflict is True
    assert verdict.conflicting_ref == "other"
    assert "f.txt" in verdict.conflicted_files
    assert verdict.error == ""


def test_in_flight_ref_touching_other_files_is_clean(repo: Path):
    _branch_commit(repo, "topic", "f.txt", "line1-TOPIC\nline2\nline3\n")
    _branch_commit(repo, "other", "unrelated.txt", "unrelated\n")

    verdict = check_predispatch_conflict(
        str(repo), "main", "topic", in_flight_refs=["other"]
    )

    assert verdict.would_conflict is False
    assert verdict.conflicting_ref == ""


def test_base_conflict_reported_before_in_flight_refs(repo: Path):
    # A conflict with the base tip short-circuits before in-flight refs are
    # consulted: conflicting_ref stays "" even though a peer ref is supplied.
    _branch_commit(repo, "topic", "f.txt", "line1-FROM-TOPIC\nline2\nline3\n")
    (repo / "f.txt").write_text("line1-FROM-MAIN\nline2\nline3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main advances")
    _branch_commit(repo, "other", "unrelated.txt", "unrelated\n")

    verdict = check_predispatch_conflict(
        str(repo), "main", "topic", in_flight_refs=["other"]
    )

    assert verdict.would_conflict is True
    assert verdict.conflicting_ref == ""


# ---------------------------------------------------------------------------
# Fail policy: advisory (fail-open, default) vs. fail-closed.
# ---------------------------------------------------------------------------


def test_bad_ref_is_advisory_fail_open(repo: Path):
    # Default advisory: an unresolvable ref is reported but does NOT block.
    verdict = check_predispatch_conflict(str(repo), "main", "does-not-exist")

    assert verdict.advisory is True
    assert verdict.would_conflict is False
    assert "topic ref" in verdict.error


def test_bad_ref_can_fail_closed(repo: Path):
    # advisory=False turns an unresolvable ref into a predicted conflict,
    # mirroring the land-time gate.
    verdict = check_predispatch_conflict(
        str(repo), "main", "does-not-exist", advisory=False
    )

    assert verdict.advisory is False
    assert verdict.would_conflict is True
    assert "topic ref" in verdict.error


def test_bad_in_flight_ref_fail_open_reports_ref(repo: Path):
    _branch_commit(repo, "topic", "new_file.txt", "brand new\n")

    verdict = check_predispatch_conflict(
        str(repo), "main", "topic", in_flight_refs=["ghost"]
    )

    assert verdict.would_conflict is False  # advisory fail-open
    assert verdict.conflicting_ref == "ghost"
    assert verdict.error


def test_bad_in_flight_ref_fail_closed_blocks(repo: Path):
    _branch_commit(repo, "topic", "new_file.txt", "brand new\n")

    verdict = check_predispatch_conflict(
        str(repo), "main", "topic", in_flight_refs=["ghost"], advisory=False
    )

    assert verdict.would_conflict is True
    assert verdict.conflicting_ref == "ghost"
    assert verdict.error


# ---------------------------------------------------------------------------
# Verdict serialization contract.
# ---------------------------------------------------------------------------


def test_verdict_serializes():
    v = PredispatchVerdict(
        would_conflict=True,
        base_ref="main",
        topic_ref="topic",
        conflicting_ref="other",
        conflicted_files=["a.py"],
    )
    d = v.to_dict()

    assert d["schema"] == "mac.predispatch_conflict.v1"
    assert d["would_conflict"] is True
    assert d["base_ref"] == "main" and d["topic_ref"] == "topic"
    assert d["conflicting_ref"] == "other"
    assert d["conflicted_files"] == ["a.py"]
    assert d["advisory"] is True
    assert d["error"] == ""
