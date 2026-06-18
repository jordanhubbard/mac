"""Tests for the first-class agent migration helpers (mac agent migrate).

Covers the pure, testable core: the soul-backup exclude set, the fleets.yaml
retarget, the migration runbook, and the step runner (with an injected runner
so no real ssh/deploy happens).
"""

from __future__ import annotations

import pytest

from mac import agent_migrate as am


# --- soul backup excludes ---------------------------------------------------


def test_excludes_drop_host_config_skills_and_cruft():
    ex = set(am.SOUL_BACKUP_EXCLUDES)
    # host-specific config + secrets + deploy-managed skills must NOT migrate
    assert ".hermes/config.yaml" in ex
    assert ".hermes/.env" in ex
    assert ".hermes/skills" in ex
    # cruft
    assert ".hermes/hermes-agent.old-feature-branch" in ex
    assert ".hermes/logs" in ex


def test_tar_exclude_args_format():
    args = am.soul_backup_tar_excludes()
    assert all(a.startswith("--exclude=") for a in args)
    assert "--exclude=.hermes/.env" in args


# --- retarget_fleet_agent ---------------------------------------------------


def _registry():
    return {
        "fleets": {
            "rocky": {
                "agents": [
                    {"name": "rocky", "target": "jkh@do-host1", "os": "linux"},
                    {"name": "bullwinkle", "target": "jkh@puck.local", "os": "darwin"},
                ]
            }
        }
    }


def test_retarget_updates_target_and_os_and_returns_old():
    reg = _registry()
    old = am.retarget_fleet_agent(reg, "rocky", "bullwinkle", target="jkh@madmax.local", os="linux")
    assert old == ("jkh@puck.local", "darwin")
    bw = [a for a in reg["fleets"]["rocky"]["agents"] if a["name"] == "bullwinkle"][0]
    assert bw["target"] == "jkh@madmax.local"
    assert bw["os"] == "linux"


def test_retarget_missing_agent_raises():
    with pytest.raises(KeyError):
        am.retarget_fleet_agent(_registry(), "rocky", "ghost", target="jkh@x")


def test_retarget_missing_fleet_raises():
    with pytest.raises(KeyError):
        am.retarget_fleet_agent(_registry(), "nope", "bullwinkle", target="jkh@x")


# --- migration_plan ---------------------------------------------------------


def test_plan_is_ordered_and_soul_safe():
    steps = am.migration_plan(
        "bullwinkle", src_target="jkh@puck.local", dst_target="jkh@madmax.local", fleet="rocky"
    )
    order = [s for s, _ in steps]
    # backup must precede transfer/deploy; restore must come AFTER deploy
    assert order.index("backup-soul") < order.index("transfer") < order.index("deploy")
    assert order.index("deploy") < order.index("restore-soul") < order.index("verify")
    # default decommissions the source
    assert "decommission-source" in order
    assert "retire-source-agent" not in order


def test_plan_keep_source_skips_decommission():
    steps = am.migration_plan(
        "x", src_target="a@b", dst_target="a@c", fleet="rocky", keep_source=True
    )
    assert "decommission-source" not in [s for s, _ in steps]


def test_plan_retire_source_agent_appends_delete():
    steps = am.migration_plan(
        "x", src_target="a@b", dst_target="a@c", fleet="rocky", retire_source_agent="agent_madmax"
    )
    cmds = dict(steps)
    assert "retire-source-agent" in cmds
    assert "mac agent delete agent_madmax" in cmds["retire-source-agent"]


def test_plan_backup_excludes_host_config():
    steps = dict(am.migration_plan("x", src_target="a@b", dst_target="a@c", fleet="rocky"))
    assert "--exclude=.hermes/config.yaml" in steps["backup-soul"]
    assert "--exclude=.hermes/.env" in steps["backup-soul"]


# --- execute_migration (injected runner; no real ssh/deploy) ----------------


def test_execute_runs_steps_in_order_and_skips_retarget_marker():
    steps = am.migration_plan("x", src_target="a@b", dst_target="a@c", fleet="rocky")
    ran = []
    res = am.execute_migration("x", steps, runner=lambda cmd: ran.append(cmd) or 0)
    assert res["ok"] is True
    # retarget-fleet is handled in-process, not shelled out
    assert all("fleets.yaml" not in c for c in ran)
    assert any(c.startswith("ssh ") and "tar czf" in c for c in ran)  # backup ran


def test_execute_stops_on_first_failure():
    steps = am.migration_plan("x", src_target="a@b", dst_target="a@c", fleet="rocky")

    calls = {"n": 0}

    def runner(cmd: str) -> int:
        calls["n"] += 1
        return 1 if "scp" in cmd else 0  # fail at the transfer step

    res = am.execute_migration("x", steps, runner=runner)
    assert res["ok"] is False
    assert res["failed_step"] == "transfer"
