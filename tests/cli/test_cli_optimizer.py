from __future__ import annotations

import io
import json
import sys

import pytest

from mac.cli import main


def _run_raw(tmp_path, *args):
    out = io.StringIO()
    err = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = main(["--db", str(tmp_path / "mac.db"), "--json", *args])
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None, err.getvalue()


def _run(tmp_path, *args):
    rc, payload, _ = _run_raw(tmp_path, *args)
    return rc, payload


def _usage_error(tmp_path, *args):
    out = io.StringIO()
    err = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        with pytest.raises(SystemExit) as exc:
            main(["--db", str(tmp_path / "mac.db"), "--json", *args])
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return exc.value.code, err.getvalue()


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


def test_optimizer_cli_status_rejects_unexpected_arguments(tmp_path):
    code, err = _usage_error(tmp_path, "optimizer", "status", "extra")

    assert code == 2
    assert "unrecognized arguments: extra" in err


def test_optimizer_cli_tick_rejects_unknown_options(tmp_path):
    code, err = _usage_error(tmp_path, "optimizer", "tick", "--unknown")

    assert code == 2
    assert "unrecognized arguments: --unknown" in err


def test_optimizer_cli_policy_reports_invalid_parameters(tmp_path):
    rc, payload, err = _run_raw(
        tmp_path,
        "optimizer",
        "policy",
        "create",
        "bad-params",
        "demo",
        "--parameters",
        "[]",
    )

    assert rc == 1
    assert payload is None
    assert "scientific policy parameters must be a JSON object" in err


def test_optimizer_cli_experiment_requires_distinct_policies(tmp_path):
    rc, policy = _run(
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

    rc, payload, err = _run_raw(
        tmp_path,
        "optimizer",
        "experiment",
        "create",
        "bad-experiment",
        "demo",
        policy["id"],
        policy["id"],
        "--hypothesis",
        "A policy cannot be both control and treatment",
        "--primary-metric",
        "cycles_to_accept",
    )

    assert rc == 1
    assert payload is None
    assert "control and treatment policies must differ" in err
