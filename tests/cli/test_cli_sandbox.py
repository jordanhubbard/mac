"""Behavioral tests for `mac sandbox bom` and `mac sandbox rollout`.

Both commands are the operator-facing end of the contract-derived image: one
answers "what does the fleet's sandbox need", the other puts a reviewed image
onto workers without interrupting their work. Neither is useful if it only
works when called as a Python function, so these go through the CLI.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from mac.cli import main
from mac.test_support import control_plane_on, dsn_for

DIGEST = "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "a" * 64


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, (json.loads(raw) if raw else None)


def test_bom_derives_the_commands_mac_itself_needs(tmp_path):
    """With no project registered the answer is not empty: the executor's own
    tools are in every sandbox regardless of what any contract says."""
    rc, out = _run(tmp_path, "sandbox", "bom")

    assert rc in (None, 0)
    assert {"git", "python3", "bash"} <= set(out["commands"])


def test_bom_reports_gaps_against_a_containerfile(tmp_path):
    image = tmp_path / "Containerfile"
    image.write_text("FROM debian\nRUN apt-get install -y git\n", encoding="utf-8")

    _rc, out = _run(tmp_path, "sandbox", "bom", "--containerfile", str(image))

    assert "gaps" in out
    assert "tar" in out["gaps"]["missing_packages"]


def test_bom_writes_a_manifest_that_can_be_committed(tmp_path):
    target = tmp_path / "sandbox-bom.json"

    _rc, out = _run(tmp_path, "sandbox", "bom", "--write", str(target))

    assert out["written"] == str(target)
    assert json.loads(target.read_text(encoding="utf-8"))["schema"]


def test_bom_compare_exits_nonzero_on_drift(tmp_path):
    """So CI can fail when contracts move past the reviewed manifest, rather
    than the manifest going stale in silence."""
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps({"commands": ["nothing-like-reality"]}), encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        _run(tmp_path, "sandbox", "bom", "--compare", str(stale))

    assert excinfo.value.code == 1


def test_bom_compare_is_quiet_when_the_manifest_matches(tmp_path):
    current = tmp_path / "current.json"
    _run(tmp_path, "sandbox", "bom", "--write", str(current))

    rc, out = _run(tmp_path, "sandbox", "bom", "--compare", str(current))

    assert rc in (None, 0)
    assert out["has_drift"] is False


def test_rollout_files_a_barrier_task_for_each_worker(tmp_path):
    cp = control_plane_on(dsn_for(tmp_path))
    machine = cp.register_machine("cli-roll-host")
    cp.register_agent(machine.id, "worker1")

    rc, out = _run(tmp_path, "sandbox", "rollout", "--image", DIGEST)

    assert rc in (None, 0)
    assert len(out["filed"]) == 1


def test_rollout_refuses_a_tag(tmp_path):
    """A tag can be repointed after review, so what ships and what was
    reviewed could differ with nothing recording it."""
    rc, out = _run(
        tmp_path,
        "sandbox",
        "rollout",
        "--image",
        "ghcr.io/jordanhubbard/mac-openshell-runtime:latest",
    )

    assert rc not in (None, 0)
