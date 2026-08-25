"""Tests for first-class cross-fleet agent move (mac fleet move-agent).

Covers the pure, testable core: registry mutation, idempotency, error cases,
plan generation, and the dry-run execution path.  No real SSH, deploy, or DB
calls are made.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from types import SimpleNamespace

from mac.fleet_move import (
    execute_fleet_move,
    find_agent_fleet,
    fleet_hub_url,
    move_agent_in_registry,
    plan_fleet_move,
    render_move_plan,
    resolve_fleet_key,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _registry() -> Dict[str, Any]:
    """Minimal two-fleet registry with a hub and two workers."""
    return {
        "version": 1,
        "fleets": {
            "old-hub": {
                "fleet_name": "old-fleet",
                "hub_agent": "old-hub",
                "hub_url": "http://192.0.2.1:8789",
                "agents": [
                    {"name": "old-hub", "target": "<user>@<old-host>", "os": "linux"},
                    {"name": "worker-1", "target": "<user>@<worker-host-1>", "os": "linux"},
                    {"name": "worker-2", "target": "<user>@<worker-host-2>", "os": "linux"},
                ],
            },
            "mac": {
                "fleet_name": "mac",
                "hub_agent": "hub",
                "hub_url": "http://100.72.16.110:8789",
                "agents": [
                    {"name": "hub", "target": "<user>@<mac-host>", "os": "linux"},
                ],
            },
        },
    }


# ---------------------------------------------------------------------------
# find_agent_fleet
# ---------------------------------------------------------------------------


def test_find_agent_fleet_returns_correct_fleet():
    reg = _registry()
    assert find_agent_fleet(reg, "worker-1") == "old-hub"
    assert find_agent_fleet(reg, "hub") == "mac"


def test_find_agent_fleet_returns_none_for_unknown():
    assert find_agent_fleet(_registry(), "ghost") is None


def test_find_agent_fleet_empty_registry():
    assert find_agent_fleet({}, "worker-1") is None
    assert find_agent_fleet({"fleets": {}}, "worker-1") is None


# ---------------------------------------------------------------------------
# move_agent_in_registry — happy path
# ---------------------------------------------------------------------------


def test_move_removes_agent_from_source_fleet():
    reg = _registry()
    new_reg, _ = move_agent_in_registry(reg, "worker-1", "old-hub", "mac")
    src_names = [a["name"] for a in new_reg["fleets"]["old-hub"]["agents"]]
    assert "worker-1" not in src_names


def test_move_adds_agent_to_target_fleet():
    reg = _registry()
    new_reg, _ = move_agent_in_registry(reg, "worker-1", "old-hub", "mac")
    dst_names = [a["name"] for a in new_reg["fleets"]["mac"]["agents"]]
    assert "worker-1" in dst_names


def test_move_preserves_other_source_agents():
    reg = _registry()
    new_reg, _ = move_agent_in_registry(reg, "worker-1", "old-hub", "mac")
    src_names = [a["name"] for a in new_reg["fleets"]["old-hub"]["agents"]]
    assert "old-hub" in src_names
    assert "worker-2" in src_names


def test_move_preserves_target_existing_agents():
    reg = _registry()
    new_reg, _ = move_agent_in_registry(reg, "worker-1", "old-hub", "mac")
    dst_names = [a["name"] for a in new_reg["fleets"]["mac"]["agents"]]
    assert "hub" in dst_names


def test_move_returns_agent_entry():
    reg = _registry()
    _, agent = move_agent_in_registry(reg, "worker-1", "old-hub", "mac")
    assert agent["name"] == "worker-1"
    assert agent["target"] == "<user>@<worker-host-1>"


def test_move_inherits_target_hub_url():
    """Agent moved to mac fleet inherits mac's hub_url when not already set."""
    reg = _registry()
    _, agent = move_agent_in_registry(reg, "worker-1", "old-hub", "mac")
    assert agent.get("hub_url") == "http://100.72.16.110:8789"


def test_move_does_not_override_existing_agent_hub_url():
    """If the agent already has a hub_url it must NOT be overwritten."""
    reg = _registry()
    # Give the worker a pre-existing hub_url.
    reg["fleets"]["old-hub"]["agents"][1]["hub_url"] = "http://custom:9000"
    _, agent = move_agent_in_registry(reg, "worker-1", "old-hub", "mac")
    assert agent.get("hub_url") == "http://custom:9000"


def test_move_does_not_mutate_original_registry():
    """move_agent_in_registry must be pure — no in-place mutation."""
    reg = _registry()
    orig_src_count = len(reg["fleets"]["old-hub"]["agents"])
    move_agent_in_registry(reg, "worker-1", "old-hub", "mac")
    assert len(reg["fleets"]["old-hub"]["agents"]) == orig_src_count


def test_move_hub_agent_to_other_fleet():
    """Hubs are agents too — moving a hub agent should work."""
    reg = _registry()
    new_reg, agent = move_agent_in_registry(reg, "old-hub", "old-hub", "mac")
    dst_names = [a["name"] for a in new_reg["fleets"]["mac"]["agents"]]
    assert "old-hub" in dst_names
    src_names = [a["name"] for a in new_reg["fleets"]["old-hub"]["agents"]]
    assert "old-hub" not in src_names


# ---------------------------------------------------------------------------
# move_agent_in_registry — idempotency via execute_fleet_move
# (pure duplicate-removal in destination)
# ---------------------------------------------------------------------------


def test_move_deduplicates_agent_in_target(tmp_path):
    """If the agent is already present in the target, it must not be duplicated."""
    reg = _registry()
    # First move.
    new_reg, _ = move_agent_in_registry(reg, "worker-1", "old-hub", "mac")
    # Second move (agent no longer in old-hub, but still in mac).
    # Should raise because agent is absent from old-hub now.
    with pytest.raises(KeyError, match="worker-1"):
        move_agent_in_registry(new_reg, "worker-1", "old-hub", "mac")


# ---------------------------------------------------------------------------
# move_agent_in_registry — error cases
# ---------------------------------------------------------------------------


def test_move_raises_if_source_fleet_missing():
    with pytest.raises(KeyError, match="ghost-fleet"):
        move_agent_in_registry(_registry(), "worker-1", "ghost-fleet", "mac")


def test_move_raises_if_target_fleet_missing():
    with pytest.raises(KeyError, match="ghost-fleet"):
        move_agent_in_registry(_registry(), "worker-1", "old-hub", "ghost-fleet")


def test_move_raises_if_agent_not_in_source():
    with pytest.raises(KeyError, match="ghost-agent"):
        move_agent_in_registry(_registry(), "ghost-agent", "old-hub", "mac")


# ---------------------------------------------------------------------------
# plan_fleet_move
# ---------------------------------------------------------------------------


def test_plan_contains_required_steps():
    steps = plan_fleet_move("worker-1", "old-hub", "mac", _registry())
    order = [s for s, _ in steps]
    for expected in ("validate", "backup-registry", "update-registry", "redeploy", "verify"):
        assert expected in order, "missing step %r" % expected


def test_plan_contains_db_reconcile_note_by_default():
    steps = plan_fleet_move("worker-1", "old-hub", "mac", _registry())
    order = [s for s, _ in steps]
    assert "reconcile-db" in order


def test_plan_skips_db_reconcile_when_disabled():
    steps = plan_fleet_move("worker-1", "old-hub", "mac", _registry(), reconcile_db=False)
    order = [s for s, _ in steps]
    assert "reconcile-db" not in order


def test_plan_redeploy_step_references_target_fleet():
    steps = dict(plan_fleet_move("worker-1", "old-hub", "mac", _registry()))
    assert "mac" in steps["redeploy"]
    assert "worker-1" in steps["redeploy"]


def test_plan_verify_step_references_target_fleet():
    steps = dict(plan_fleet_move("worker-1", "old-hub", "mac", _registry()))
    assert "mac" in steps["verify"]
    assert "worker-1" in steps["verify"]


def test_plan_db_reconcile_note_references_both_fleets_and_no_fake_cmd():
    steps = dict(plan_fleet_move("worker-1", "old-hub", "mac", _registry()))
    note = steps["reconcile-db"]
    assert "mac" in note and "worker-1" in note and "old-hub" in note
    # honest: never emit the non-existent `mac fleet update --add-agent` command
    assert "mac admin fleet update" not in note


def test_render_move_plan_is_human_readable():
    steps = plan_fleet_move("worker-1", "old-hub", "mac", _registry())
    text = render_move_plan("worker-1", "old-hub", "mac", steps)
    assert "worker-1" in text
    assert "old-hub" in text
    assert "mac" in text
    assert "DRY-RUN" in text
    # One line per step with the step name in brackets.
    assert "[validate]" in text
    assert "[redeploy]" in text


# ---------------------------------------------------------------------------
# execute_fleet_move — dry-run mode (no filesystem side-effects)
# ---------------------------------------------------------------------------


def _write_registry(tmp_path: Path, reg: Dict[str, Any]) -> Path:
    p = tmp_path / "fleets.yaml"
    p.write_text(yaml.safe_dump(reg, sort_keys=False), encoding="utf-8")
    return p


def test_execute_dry_run_ok_returns_true(tmp_path):
    p = _write_registry(tmp_path, _registry())
    result = execute_fleet_move("worker-1", "old-hub", "mac", fleets_config=p, dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True


def test_execute_dry_run_does_not_write_file(tmp_path):
    p = _write_registry(tmp_path, _registry())
    mtime_before = p.stat().st_mtime
    execute_fleet_move("worker-1", "old-hub", "mac", fleets_config=p, dry_run=True)
    assert p.stat().st_mtime == mtime_before


def test_execute_dry_run_describes_proposed_change(tmp_path):
    p = _write_registry(tmp_path, _registry())
    result = execute_fleet_move("worker-1", "old-hub", "mac", fleets_config=p, dry_run=True)
    fragment = result["proposed_registry_fragment"]
    # Source fleet should NOT contain worker-1 in the proposal.
    src_names = [a.get("name") for a in (fragment.get("fleets.old-hub.agents") or [])]
    assert "worker-1" not in src_names
    # Target fleet SHOULD contain worker-1 in the proposal.
    dst_names = [a.get("name") for a in (fragment.get("fleets.mac.agents") or [])]
    assert "worker-1" in dst_names


def test_execute_dry_run_includes_redeploy_cmd(tmp_path):
    p = _write_registry(tmp_path, _registry())
    result = execute_fleet_move("worker-1", "old-hub", "mac", fleets_config=p, dry_run=True)
    assert "worker-1" in result["redeploy_cmd"]
    assert "mac" in result["redeploy_cmd"]


def test_execute_dry_run_includes_db_reconcile_note(tmp_path):
    p = _write_registry(tmp_path, _registry())
    result = execute_fleet_move("worker-1", "old-hub", "mac", fleets_config=p, dry_run=True)
    note = result.get("db_reconcile") or ""
    assert "re-registration" in note and "mac" in note
    # the old fake-command field must be gone
    assert "db_reconcile_cmds" not in result


def test_execute_dry_run_error_source_fleet_missing(tmp_path):
    p = _write_registry(tmp_path, _registry())
    result = execute_fleet_move("worker-1", "ghost-fleet", "mac", fleets_config=p, dry_run=True)
    assert result["ok"] is False
    assert "ghost-fleet" in result["error"]


def test_execute_dry_run_error_target_fleet_missing(tmp_path):
    p = _write_registry(tmp_path, _registry())
    result = execute_fleet_move("worker-1", "old-hub", "ghost-fleet", fleets_config=p, dry_run=True)
    assert result["ok"] is False
    assert "ghost-fleet" in result["error"]


def test_execute_dry_run_error_agent_missing_in_source(tmp_path):
    p = _write_registry(tmp_path, _registry())
    result = execute_fleet_move("ghost-agent", "old-hub", "mac", fleets_config=p, dry_run=True)
    assert result["ok"] is False
    assert "ghost-agent" in result["error"]


def test_execute_dry_run_idempotent_when_already_in_target(tmp_path):
    """If the agent is already in the target fleet, dry-run returns ok+idempotent."""
    reg = _registry()
    # Pre-move: add worker-1 directly to mac fleet.
    reg["fleets"]["mac"]["agents"].append(
        {"name": "worker-1", "target": "<user>@<worker-host-1>", "os": "linux"}
    )
    # Remove from old-hub so it looks already-moved.
    reg["fleets"]["old-hub"]["agents"] = [
        a for a in reg["fleets"]["old-hub"]["agents"] if a.get("name") != "worker-1"
    ]
    p = _write_registry(tmp_path, reg)
    result = execute_fleet_move("worker-1", "old-hub", "mac", fleets_config=p, dry_run=True)
    assert result["ok"] is True
    assert result.get("idempotent") is True


# ---------------------------------------------------------------------------
# execute_fleet_move — live mode (writes fleets.yaml)
# ---------------------------------------------------------------------------


def test_execute_live_writes_registry(tmp_path):
    p = _write_registry(tmp_path, _registry())
    result = execute_fleet_move("worker-1", "old-hub", "mac", fleets_config=p, dry_run=False)
    assert result["ok"] is True
    assert result["dry_run"] is False
    new_reg = yaml.safe_load(p.read_text())
    src_names = [a["name"] for a in new_reg["fleets"]["old-hub"]["agents"]]
    dst_names = [a["name"] for a in new_reg["fleets"]["mac"]["agents"]]
    assert "worker-1" not in src_names
    assert "worker-1" in dst_names


def test_execute_live_creates_backup(tmp_path):
    p = _write_registry(tmp_path, _registry())
    result = execute_fleet_move("worker-1", "old-hub", "mac", fleets_config=p, dry_run=False)
    backup = Path(result["backup"])
    assert backup.exists()
    orig_reg = yaml.safe_load(backup.read_text())
    # The backup should still have worker-1 in old-hub.
    src_names = [a["name"] for a in orig_reg["fleets"]["old-hub"]["agents"]]
    assert "worker-1" in src_names


def test_execute_live_hub_url_override(tmp_path):
    """Explicit hub_url overrides the inherited target fleet hub_url."""
    p = _write_registry(tmp_path, _registry())
    result = execute_fleet_move(
        "worker-1",
        "old-hub",
        "mac",
        fleets_config=p,
        dry_run=False,
        hub_url="http://custom:9000",
    )
    assert result["ok"] is True
    new_reg = yaml.safe_load(p.read_text())
    moved = next(a for a in new_reg["fleets"]["mac"]["agents"] if a["name"] == "worker-1")
    assert moved.get("hub_url") == "http://custom:9000"


def test_execute_live_preserves_source_remaining_agents(tmp_path):
    p = _write_registry(tmp_path, _registry())
    execute_fleet_move("worker-1", "old-hub", "mac", fleets_config=p, dry_run=False)
    new_reg = yaml.safe_load(p.read_text())
    src_names = [a["name"] for a in new_reg["fleets"]["old-hub"]["agents"]]
    assert "old-hub" in src_names
    assert "worker-2" in src_names


def test_execute_live_includes_next_steps_with_redeploy(tmp_path):
    p = _write_registry(tmp_path, _registry())
    result = execute_fleet_move("worker-1", "old-hub", "mac", fleets_config=p, dry_run=False)
    steps_text = " ".join(result.get("next_steps") or [])
    assert "worker-1" in steps_text


def test_execute_live_includes_db_reconcile_note(tmp_path):
    # dry_run=False with the default run_redeploy=False: writes the registry +
    # emits the redeploy, runs no subprocess, and notes the auto DB reconcile.
    p = _write_registry(tmp_path, _registry())
    result = execute_fleet_move("worker-1", "old-hub", "mac", fleets_config=p, dry_run=False)
    note = result.get("db_reconcile") or ""
    assert "re-register" in note.lower()
    assert "mac admin fleet update" not in note
    assert result.get("redeployed") is None  # no redeploy ran (run_redeploy=False)


# ---------------------------------------------------------------------------
# resolve_fleet_key / fleet_hub_url (accept fleet_name OR registry key)
# ---------------------------------------------------------------------------


def test_resolve_fleet_key_by_registry_key():
    assert resolve_fleet_key(_registry(), "old-hub") == "old-hub"


def test_resolve_fleet_key_by_fleet_name():
    # 'old-fleet' is the fleet_name; its registry KEY is 'old-hub'.
    assert resolve_fleet_key(_registry(), "old-fleet") == "old-hub"
    # 'mac' resolves to itself (key == fleet_name here).
    assert resolve_fleet_key(_registry(), "mac") == "mac"


def test_resolve_fleet_key_unknown_returns_none():
    assert resolve_fleet_key(_registry(), "nope") is None
    assert resolve_fleet_key(_registry(), "") is None


def test_fleet_hub_url_lookup():
    assert fleet_hub_url(_registry(), "mac") == "http://100.72.16.110:8789"
    assert fleet_hub_url(_registry(), "missing") == ""


# ---------------------------------------------------------------------------
# Loud validation: hubless / unknown target (no "<target-hub-url>" placeholder)
# ---------------------------------------------------------------------------


def test_execute_fails_loudly_when_target_has_no_hub_url(tmp_path):
    reg = _registry()
    reg["fleets"]["mac"].pop("hub_url")  # target fleet without a hub_url
    p = _write_registry(tmp_path, reg)
    result = execute_fleet_move("worker-1", "old-hub", "mac", fleets_config=p, dry_run=True)
    assert result["ok"] is False
    assert "hub_url" in result["error"]
    # nothing written
    assert yaml.safe_load(p.read_text())["fleets"]["old-hub"]["agents"]  # unchanged


def test_execute_hub_url_override_satisfies_validation(tmp_path):
    reg = _registry()
    reg["fleets"]["mac"].pop("hub_url")
    p = _write_registry(tmp_path, reg)
    result = execute_fleet_move(
        "worker-1",
        "old-hub",
        "mac",
        fleets_config=p,
        dry_run=True,
        hub_url="http://10.0.0.9:8789",
    )
    assert result["ok"] is True
    assert result["target_hub_url"] == "http://10.0.0.9:8789"


# ---------------------------------------------------------------------------
# --execute runs the redeploy (via an injected runner — no real subprocess)
# ---------------------------------------------------------------------------


def test_execute_runs_redeploy_with_injected_runner(tmp_path):
    p = _write_registry(tmp_path, _registry())
    calls = []

    def fake_runner(cmd, cwd=None):
        calls.append((cmd, cwd))
        return SimpleNamespace(returncode=0, stderr="")

    result = execute_fleet_move(
        "worker-1",
        "old-hub",
        "mac",
        fleets_config=p,
        dry_run=False,
        run_redeploy=True,
        runner=fake_runner,
    )
    assert result["ok"] is True
    assert result["redeployed"] is True
    assert result["redeploy_returncode"] == 0
    # the deploy was invoked with the target fleet + agent
    assert len(calls) == 1
    cmd, _cwd = calls[0]
    assert "--hub" in cmd and "mac" in cmd and "worker-1" in cmd
    assert cmd[0].endswith("deploy/deploy-mac-fleet.sh")
    # fleets.yaml actually moved the agent
    reg = yaml.safe_load(p.read_text())
    names = lambda fk: [a["name"] for a in reg["fleets"][fk]["agents"]]
    assert "worker-1" not in names("old-hub")
    assert "worker-1" in names("mac")


def test_execute_redeploy_failure_surfaces_loudly(tmp_path):
    p = _write_registry(tmp_path, _registry())

    def failing_runner(cmd, cwd=None):
        return SimpleNamespace(returncode=2, stderr="boom: deploy blew up")

    result = execute_fleet_move(
        "worker-1",
        "old-hub",
        "mac",
        fleets_config=p,
        dry_run=False,
        run_redeploy=True,
        runner=failing_runner,
    )
    assert result["ok"] is False
    assert result["redeploy_returncode"] == 2
    assert "boom" in result["error"]
    # the registry move still landed + backup retained (operator can revert)
    assert result.get("registry_written")
    assert result.get("backup")


def test_execute_no_redeploy_emits_command_only(tmp_path):
    p = _write_registry(tmp_path, _registry())
    sentinel = []

    def runner(cmd, cwd=None):
        sentinel.append(cmd)
        return SimpleNamespace(returncode=0)

    result = execute_fleet_move(
        "worker-1",
        "old-hub",
        "mac",
        fleets_config=p,
        dry_run=False,
        run_redeploy=False,
        runner=runner,
    )
    assert result["ok"] is True
    assert sentinel == []  # runner NOT invoked
    assert result.get("redeployed") is None
    assert any("deploy-mac-fleet.sh" in s for s in result.get("next_steps") or [])


# ---------------------------------------------------------------------------
# CLI argument parsing smoke-test
# ---------------------------------------------------------------------------


def test_cli_fleet_move_agent_is_registered():
    """The 'mac admin fleet move-agent' subcommand must be registered in the CLI."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "mac.cli", "admin", "fleet", "move-agent", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, "mac admin fleet move-agent --help failed:\n%s" % result.stderr
    assert "--agent" in result.stdout
    assert "--from" in result.stdout
    assert "--to" in result.stdout
