"""Behavioral tests for `mac fleet creds-status` / `mac fleet creds-sync`."""
from __future__ import annotations

import io
import json
import sys

from mac.test_support import control_plane_on, dsn_for, store_on
from mac.cli import main
from mac.services import ControlPlane
from mac.test_support import ephemeral_store


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


def _register_agent_with_cli_status(tmp_path, name, clis):
    cp = control_plane_on(dsn_for(tmp_path))
    machine = cp.register_machine("%s-host" % name, resources={"cpu": 4})
    cp.register_agent(
        machine.id,
        name,
        capabilities=["python"],
        resources={"coding_clis": {"schema": "mac.coding_clis.v1", "clis": clis}},
    )


def test_fleet_creds_status_flags_agents_needing_sync(tmp_path):
    _register_agent_with_cli_status(
        tmp_path,
        "needy",
        {
            "claude": {"on_path": True, "available": False, "detail": "no key"},
            "codex": {"on_path": True, "available": True, "auth_source": "~/.codex/auth.json"},
            "cursor": {"on_path": False, "available": False},
        },
    )
    _register_agent_with_cli_status(
        tmp_path,
        "healthy",
        {
            "claude": {"on_path": True, "available": True, "auth_source": "ANTHROPIC_API_KEY"},
            "codex": {"on_path": True, "available": True, "auth_source": "~/.codex/auth.json"},
            "cursor": {"on_path": True, "available": True, "auth_source": "CURSOR_API_KEY"},
        },
    )

    rc, out = _run(tmp_path, "fleet", "creds-status")
    assert rc in (None, 0)
    rows = {row["agent"]: row for row in out["agents"]}
    assert rows["needy"]["claude"] == "NEEDS SYNC"
    assert rows["needy"]["codex"].startswith("ok")
    assert rows["needy"]["cursor"] == "not installed"
    assert rows["healthy"]["claude"].startswith("ok")
    assert out["needs_sync"] == {"needy": ["claude"]}


def test_fleet_creds_status_handles_agents_without_reports(tmp_path):
    cp = control_plane_on(dsn_for(tmp_path))
    machine = cp.register_machine("old-host", resources={})
    cp.register_agent(machine.id, "old-worker", capabilities=["python"], resources={})

    rc, out = _run(tmp_path, "fleet", "creds-status")
    assert rc in (None, 0)
    assert "no coding_clis report" in out["agents"][0]["status"]
    assert out["needs_sync"] == {}


def test_fleet_creds_status_distinguishes_configured_from_verified_route(tmp_path):
    cp = control_plane_on(dsn_for(tmp_path))
    machine = cp.register_machine("route-host", resources={})
    cp.register_agent(
        machine.id,
        "route-worker",
        capabilities=["python"],
        resources={
            "coding_clis": {
                "schema": "mac.coding_clis.v2",
                "clis": {
                    "claude": {"on_path": False, "configured": False},
                    "codex": {
                        "on_path": True,
                        # v2 contract: available tracks the executable proof, so
                        # an on-PATH + configured but unverified route is NOT
                        # available and must never render as "ok".
                        "available": False,
                        "configured": True,
                        "verified": False,
                        "verification": {"failure_class": "endpoint_protocol_mismatch"},
                    },
                    "cursor": {"on_path": False, "configured": False},
                },
            }
        },
    )

    _rc, out = _run(tmp_path, "fleet", "creds-status")

    row = out["agents"][0]
    assert row["codex"] == "ROUTE UNAVAILABLE (endpoint_protocol_mismatch)"
    assert not row["codex"].startswith("ok")
    # configured means credentialed; it is a sandbox/route concern, not missing
    # secrets, so it does not appear in needs_sync.
    assert out["needs_sync"] == {}


def test_fleet_creds_status_on_path_unexecutable_never_shows_available(tmp_path):
    """A v2 CLI that is on PATH but not verified by the same-environment probe
    (available=False) is reported ROUTE UNAVAILABLE, never ok/available."""
    cp = control_plane_on(dsn_for(tmp_path))
    machine = cp.register_machine("probe-host", resources={})
    cp.register_agent(
        machine.id,
        "probe-worker",
        capabilities=["python"],
        resources={
            "coding_clis": {
                "schema": "mac.coding_clis.v2",
                "clis": {
                    "claude": {
                        "on_path": True,
                        "available": False,
                        "configured": True,
                        "verified": False,
                        "verification": {"failure_class": "agent_binary_missing"},
                    },
                    "codex": {"on_path": False, "configured": False},
                    "cursor": {
                        "on_path": True,
                        "available": False,
                        "configured": True,
                        "verified": False,
                        "verification": {"failure_class": "sandbox_unavailable"},
                    },
                },
            }
        },
    )

    _rc, out = _run(tmp_path, "fleet", "creds-status")

    row = out["agents"][0]
    assert row["claude"] == "ROUTE UNAVAILABLE (agent_binary_missing)"
    assert row["cursor"] == "ROUTE UNAVAILABLE (sandbox_unavailable)"
    assert not row["claude"].startswith("ok")
    assert not row["cursor"].startswith("ok")
    assert out["needs_sync"] == {}


def test_fleet_creds_sync_dry_run_moves_no_secret(tmp_path, monkeypatch):
    # This workstation's portable credential: an env API key (no keychain, no
    # files). Dry-run must report the plan and never invoke ssh.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    fleets = tmp_path / "fleets.yaml"
    fleets.write_text(
        "fleets:\n"
        "  demo:\n"
        "    fleet_name: demo\n"
        "    hub_agent: hub\n"
        "    agents:\n"
        "      - name: worker1\n"
        "        target: user@w1.local\n",
        encoding="utf-8",
    )
    _register_agent_with_cli_status(
        tmp_path,
        "worker1",
        {"claude": {"on_path": True, "available": False}},
    )

    rc, out = _run(
        tmp_path,
        "fleet", "creds-sync",
        "--fleet", "demo",
        "--fleets-config", str(fleets),
        "--cli", "claude",
        "--dry-run",
    )
    assert rc in (None, 0)
    assert out["dry_run"] is True
    assert out["agents"] == ["worker1"]  # lazy targeting from heartbeat report
    assert out["env_keys"] == ["ANTHROPIC_API_KEY"]
    # The secret value itself never appears in output.
    assert "sk-ant-test" not in json.dumps(out)


def test_fleet_creds_sync_refuses_unknown_cli(tmp_path, capsys):
    rc, _out = _run(tmp_path, "fleet", "creds-sync", "--fleet", "demo", "--cli", "gemini")
    assert rc not in (None, 0)
    assert "unknown coding CLI" in capsys.readouterr().err
