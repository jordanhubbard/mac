"""fleet-02: live fleet snapshot + runtime-context refresh (passive group awareness)."""

from __future__ import annotations

from mac.services import ControlPlane
from mac.hermes_runtime import (
    render_fleet_section,
    refresh_fleet_section,
    FLEET_SECTION_BEGIN,
    FLEET_SECTION_END,
)


def _agent(cp, name, caps=("python",)):
    m = cp.register_machine("host-%s" % name)
    return cp.register_agent(m.id, name, capabilities=list(caps))


def test_fleet_snapshot_reports_members_and_their_work():
    cp = ControlPlane.in_memory()
    hosta = _agent(cp, "hosta")
    hostc = _agent(cp, "hostc")
    task = cp.create_task("Ship the connector", required_capabilities=["python"])
    cp.claim_task(task.id, hosta.id)

    snap = cp.fleet_snapshot()
    members = {m["name"]: m for m in snap["members"]}
    assert {"hosta", "hostc"} <= set(members)
    assert members["hosta"]["current_task_title"] == "Ship the connector"
    assert members["hostc"]["current_task_title"] is None

    # exclude self
    snap2 = cp.fleet_snapshot(exclude_agent_id=hosta.id)
    assert "hosta" not in {m["name"] for m in snap2["members"]}


def test_render_and_refresh_fleet_section_is_idempotent(tmp_path):
    snap = {
        "generated_at": "2026-05-31T00:00:00Z",
        "members": [
            {"name": "hosta", "status": "busy", "health": "healthy", "current_task_title": "Ship X"},
            {"name": "hostc", "status": "idle", "health": "healthy", "current_task_id": None},
        ],
    }
    section = render_fleet_section(snap)
    assert "## Fleet — your teammates (live)" in section
    assert "**hosta** [busy/healthy] — Ship X" in section
    assert "**hostc** [idle/healthy] — idle" in section

    md = tmp_path / "mac-runtime-context.md"
    md.write_text("# Runtime Context\n\n## Identity\n\nhosta\n", encoding="utf-8")
    refresh_fleet_section(md, section)
    text = md.read_text()
    assert FLEET_SECTION_BEGIN in text and FLEET_SECTION_END in text
    assert "## Identity" in text  # existing content preserved

    # refresh again with a new snapshot → block replaced in place, not duplicated
    snap["members"][0]["current_task_title"] = "Ship Y"
    refresh_fleet_section(md, render_fleet_section(snap))
    text2 = md.read_text()
    assert text2.count(FLEET_SECTION_BEGIN) == 1
    assert "Ship Y" in text2 and "Ship X" not in text2


def test_render_fleet_section_handles_empty_fleet():
    section = render_fleet_section({"generated_at": "t", "members": []})
    assert "no other agents currently online" in section
