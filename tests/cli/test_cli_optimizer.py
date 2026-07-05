from __future__ import annotations

import io
import json
import sys

from mac.cli import main


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", str(tmp_path / "mac.db"), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def test_optimizer_cli_policy_and_experiment_lifecycle(tmp_path):
    rc, control = _run(
        tmp_path,
        "optimizer",
        "policy",
        "create",
        "baseline",
        "demo",
        "--parameters",
        "{}",
    )
    assert rc == 0
    rc, active = _run(
        tmp_path,
        "optimizer",
        "policy",
        "promote",
        control["id"],
        "--reason",
        "CLI baseline",
    )
    assert rc == 0
    assert active["status"] == "active"

    rc, treatment = _run(
        tmp_path,
        "optimizer",
        "policy",
        "create",
        "plan-first",
        "demo",
        "--parameters",
        '{"plan_first":true}',
    )
    assert rc == 0
    rc, experiment = _run(
        tmp_path,
        "optimizer",
        "experiment",
        "create",
        "reduce-rework",
        "demo",
        control["id"],
        treatment["id"],
        "--hypothesis",
        "Planning reduces rework",
        "--primary-metric",
        "cycles_to_accept",
        "--min-samples-per-arm",
        "2",
        "--max-samples-per-arm",
        "2",
        "--no-auto-promote",
    )
    assert rc == 0
    rc, running = _run(
        tmp_path,
        "optimizer",
        "experiment",
        "start",
        experiment["id"],
    )
    assert rc == 0
    assert running["state"] == "running"

    rc, analysis = _run(
        tmp_path,
        "optimizer",
        "experiment",
        "analyze",
        experiment["id"],
    )
    assert rc == 0
    assert analysis["status"] == "collecting"
    rc, evidence = _run(
        tmp_path,
        "optimizer",
        "experiment",
        "evidence",
        experiment["id"],
    )
    assert rc == 0
    assert evidence["experiment"]["id"] == experiment["id"]


def test_optimizer_cli_status_and_tick(tmp_path):
    rc, status = _run(tmp_path, "optimizer", "status")
    assert rc == 0
    assert status["schema"] == "mac.scientific_optimizer_service.v1"
    rc, report = _run(tmp_path, "optimizer", "tick")
    assert rc == 0
    assert report["status"] == "ok"
