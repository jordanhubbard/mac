"""Behavioral CLI tests for the fleet validate and fleet doctor commands.

fleet validate / fleet doctor require a valid mac.fleet_setup.v1 YAML spec.
These tests create a minimal spec in tmp_path and confirm exit code + schema
in the returned JSON — they do not need a real SSH/hub connection.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.cli import main


def _run(tmp_path, *args):
    """Run `mac --db <tmp> <args>` and return (rc, parsed_output)."""
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", str(tmp_path / "mac.db"), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def _minimal_spec(tmp_path, *, hub_name="testhub", fleet_name="test-fleet"):
    """Write a minimal mac.fleet_setup.v1 spec into tmp_path and return its path."""
    spec = {
        "schema": "mac.fleet_setup.v1",
        "hub": {"name": hub_name},
        "fleet_name": fleet_name,
        "agents": [],
    }
    path = tmp_path / "fleet-spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


def _minimal_fleets_yaml(tmp_path, hub_name="testhub"):
    path = tmp_path / "fleets.yaml"
    path.write_text(
        f"version: 1\nfleets:\n  test-fleet:\n    default: true\n    agents: []\n",
        encoding="utf-8",
    )
    return str(path)


# ---------------------------------------------------------------------------
# fleet validate
# ---------------------------------------------------------------------------


def test_fleet_validate_minimal_spec(tmp_path):
    spec_path = _minimal_spec(tmp_path)
    fleets_path = _minimal_fleets_yaml(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    rc, result = _run(
        tmp_path,
        "fleet", "validate",
        "--spec", spec_path,
        "--fleets-config", fleets_path,
        "--env-file", str(env_file),
    )
    assert rc == 0
    assert result is not None
    assert result.get("schema") == "mac.fleet_setup_plan.v1"


def test_fleet_validate_missing_hub_emits_errors(tmp_path):
    """A spec without hub.name should surface validation errors but still return 0
    (validate reports, not exits non-zero)."""
    spec = {"schema": "mac.fleet_setup.v1"}
    spec_path = tmp_path / "bad-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    fleets_path = _minimal_fleets_yaml(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    rc, result = _run(
        tmp_path,
        "fleet", "validate",
        "--spec", str(spec_path),
        "--fleets-config", fleets_path,
        "--env-file", str(env_file),
    )
    # Validation errors go into result["errors"], command still exits 0
    assert rc == 0
    assert result is not None
    errors = result.get("errors", [])
    assert len(errors) > 0


# ---------------------------------------------------------------------------
# fleet doctor
# ---------------------------------------------------------------------------


def test_fleet_doctor_minimal_spec(tmp_path):
    spec_path = _minimal_spec(tmp_path)
    fleets_path = _minimal_fleets_yaml(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    rc, result = _run(
        tmp_path,
        "fleet", "doctor",
        "--spec", spec_path,
        "--fleets-config", fleets_path,
        "--env-file", str(env_file),
    )
    assert rc == 0
    assert result is not None
    assert result.get("schema") == "mac.fleet_setup_doctor.v1"
    assert "checks" in result
    assert "status" in result


def test_fleet_doctor_exposes_fleet_name(tmp_path):
    spec_path = _minimal_spec(tmp_path, fleet_name="my-fleet")
    fleets_path = _minimal_fleets_yaml(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    rc, result = _run(
        tmp_path,
        "fleet", "doctor",
        "--spec", spec_path,
        "--fleets-config", fleets_path,
        "--env-file", str(env_file),
    )
    assert rc == 0
    assert result["fleet_name"] == "my-fleet"
