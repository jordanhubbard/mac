"""`mac admin fleet release | select-release | clear-release` (task_822aada4)."""

from __future__ import annotations

from tests.cli.test_cli_extended import _run

A = "a" * 40


def test_select_show_and_clear_the_release(tmp_path):
    rc, selected = _run(
        tmp_path, "admin", "fleet", "select-release", A, "--reason", "ship", "--converge-minutes", "30"
    )
    assert rc in (0, None)
    assert selected["commit_sha"] == A and selected["converge_by"]

    rc, shown = _run(tmp_path, "admin", "fleet", "release")
    assert rc in (0, None)
    assert shown["selection"]["commit_sha"] == A
    assert shown["schema"] == "mac.release_drift.v1"

    rc, cleared = _run(tmp_path, "admin", "fleet", "clear-release")
    assert rc in (0, None)
    assert cleared["cleared"] is True
