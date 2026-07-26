"""Unit tests for the legacy conflict-to-integration context payload builder
(``mac.services.build_conflict_integration_payload``).

These exercise the *pure* builder in isolation: no database, no ControlPlane.
Path-restricted landed-commit provenance is checked against a real temporary
git repo so ``git log`` is exercised for real, not mocked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

from mac.models import ValidationError
from mac.services import build_conflict_integration_payload


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    (repo / path).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.invalid")
    _git(r, "config", "user.name", "t")
    return r


def _runner(repo: Path):
    def run(args: List[str], timeout: int = 60) -> dict:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "returncode": int(completed.returncode),
            "stdout": (completed.stdout or "").strip(),
            "stderr": (completed.stderr or "").strip(),
        }

    return run


def test_payload_shape_and_canonical_baseline(repo: Path):
    base = _commit(repo, "a.py", "one\n", "base")
    # Landed commit on main that touches the conflicted path.
    landed = _commit(repo, "a.py", "one\ntwo\n", "land a.py change")
    # A landed commit that does NOT touch the conflicted path (must be excluded).
    _commit(repo, "b.py", "b\n", "unrelated change")
    main_sha = _git(repo, "rev-parse", "HEAD")
    head = "a" * 40

    payload = build_conflict_integration_payload(
        approved_task_id="task_approved",
        accepted_evidence_id="ev_1",
        reviewed_head_sha=head,
        current_main_sha=main_sha,
        attempt_base_sha=base,
        conflicted_paths=["a.py"],
        git_runner=_runner(repo),
    )

    assert payload["schema"] == "mac.conflict_integration_payload.v1"
    assert payload["kind"] == "legacy_single_task_publication_conflict"
    assert payload["approved_task"] == {
        "task_id": "task_approved",
        "accepted_evidence_id": "ev_1",
        "reviewed_head_sha": head,
    }
    # Current main is always preserved as the canonical baseline.
    assert payload["canonical_baseline"] == {"ref": "main", "main_sha": main_sha}
    assert payload["attempt_base_sha"] == base
    assert payload["conflicted_paths"] == ["a.py"]

    # Provenance is path-restricted: only the a.py commit is listed.
    landed_since = payload["landed_since_base"]
    assert landed_since["computed"] is True
    shas = [c["sha"] for c in landed_since["commits"]]
    assert landed == shas[0] if shas else False
    assert all(c["subject"] != "unrelated change" for c in landed_since["commits"])
    assert landed in shas


def test_dependencies_include_approved_task_explicitly(repo: Path):
    base = _commit(repo, "a.py", "one\n", "base")
    main_sha = _git(repo, "rev-parse", "HEAD")
    payload = build_conflict_integration_payload(
        approved_task_id="task_approved",
        accepted_evidence_id="ev_1",
        reviewed_head_sha="b" * 40,
        current_main_sha=main_sha,
        attempt_base_sha=base,
        conflicted_paths=["a.py"],
        depends_on=["task_other"],
        git_runner=_runner(repo),
    )
    depends_on = payload["dependencies"]["depends_on"]
    assert "task_approved" in depends_on
    assert "task_other" in depends_on


def test_supersession_is_explicit_and_never_inferred(repo: Path):
    base = _commit(repo, "a.py", "one\n", "base")
    main_sha = _git(repo, "rev-parse", "HEAD")
    payload = build_conflict_integration_payload(
        approved_task_id="task_approved",
        accepted_evidence_id="ev_1",
        reviewed_head_sha="c" * 40,
        current_main_sha=main_sha,
        attempt_base_sha=base,
        conflicted_paths=["a.py"],
        git_runner=_runner(repo),
    )
    # Default is undecided: the integration executor sets this, not the builder.
    assert payload["supersession"]["decision"] == "undecided"
    assert payload["supersession"]["decided"] is False
    assert payload["supersession"]["policy"] == "explicit_only_no_timestamp_inference"

    decided = build_conflict_integration_payload(
        approved_task_id="task_approved",
        accepted_evidence_id="ev_1",
        reviewed_head_sha="c" * 40,
        current_main_sha=main_sha,
        attempt_base_sha=base,
        conflicted_paths=["a.py"],
        supersession_decision="supersede",
        superseded_task_id="task_old",
        git_runner=_runner(repo),
    )
    assert decided["supersession"]["decision"] == "supersede"
    assert decided["supersession"]["decided"] is True
    assert decided["supersession"]["superseded_task_id"] == "task_old"


def test_provenance_uncomputed_without_git_runner():
    payload = build_conflict_integration_payload(
        approved_task_id="task_approved",
        accepted_evidence_id="ev_1",
        reviewed_head_sha="d" * 40,
        current_main_sha="e" * 40,
        attempt_base_sha="f" * 40,
        conflicted_paths=["a.py", "a.py", "  "],
    )
    # Duplicate/blank paths are normalized.
    assert payload["conflicted_paths"] == ["a.py"]
    assert payload["landed_since_base"]["computed"] is False
    assert payload["landed_since_base"]["commits"] == []


def test_trailer_correlates_landed_commit_to_task(repo: Path):
    base = _commit(repo, "a.py", "one\n", "base")
    _commit(
        repo,
        "a.py",
        "one\ntwo\n",
        "land change\n\nMac-Task-Id: task_lander",
    )
    main_sha = _git(repo, "rev-parse", "HEAD")
    payload = build_conflict_integration_payload(
        approved_task_id="task_approved",
        accepted_evidence_id="ev_1",
        reviewed_head_sha="a" * 40,
        current_main_sha=main_sha,
        attempt_base_sha=base,
        conflicted_paths=["a.py"],
        git_runner=_runner(repo),
    )
    assert "task_lander" in payload["landed_since_base"]["task_ids"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"approved_task_id": ""},
        {"accepted_evidence_id": ""},
        {"reviewed_head_sha": "not-a-sha"},
        {"current_main_sha": "zzz"},
        {"attempt_base_sha": ""},
        {"conflicted_paths": []},
        {"supersession_decision": "bogus"},
    ],
)
def test_builder_rejects_invalid_inputs(kwargs):
    base_kwargs = dict(
        approved_task_id="task_approved",
        accepted_evidence_id="ev_1",
        reviewed_head_sha="a" * 40,
        current_main_sha="b" * 40,
        attempt_base_sha="c" * 40,
        conflicted_paths=["a.py"],
    )
    base_kwargs.update(kwargs)
    with pytest.raises(ValidationError):
        build_conflict_integration_payload(**base_kwargs)
