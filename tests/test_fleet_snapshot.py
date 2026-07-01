"""fleet-02: live fleet snapshot + runtime-context refresh (passive group awareness)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.hermes_runtime import (
    render_fleet_section,
    refresh_fleet_section,
    FLEET_SECTION_BEGIN,
    FLEET_SECTION_END,
)
from mac.services import ControlPlane


def _agent(cp, name, caps=("python",)):
    m = cp.register_machine("host-%s" % name)
    return cp.register_agent(m.id, name, capabilities=list(caps))


def test_fleet_snapshot_reports_members_and_their_work():
    cp = ControlPlane.in_memory()
    rocky = _agent(cp, "rocky")
    _agent(cp, "natasha")
    task = cp.create_task("Ship the connector", required_capabilities=["python"])
    cp.claim_task(task.id, rocky.id)

    snap = cp.fleet_snapshot()
    members = {m["name"]: m for m in snap["members"]}
    assert {"rocky", "natasha"} <= set(members)
    assert members["rocky"]["current_task_title"] == "Ship the connector"
    assert members["natasha"]["current_task_title"] is None

    # exclude self
    snap2 = cp.fleet_snapshot(exclude_agent_id=rocky.id)
    assert "rocky" not in {m["name"] for m in snap2["members"]}

    client = TestClient(create_app(control_plane=cp))
    response = client.get(
        "/fleet/snapshot",
        params={"exclude_agent_id": rocky.id, "limit": 1},
    )
    assert response.status_code == 200
    assert response.json()["schema"] == "mac.fleet_snapshot.v1"
    assert [member["name"] for member in response.json()["members"]] == ["natasha"]


def test_render_and_refresh_fleet_section_is_idempotent(tmp_path):
    snap = {
        "generated_at": "2026-05-31T00:00:00Z",
        "members": [
            {"name": "rocky", "status": "busy", "health": "healthy", "current_task_title": "Ship X"},
            {"name": "natasha", "status": "idle", "health": "healthy", "current_task_id": None},
        ],
    }
    section = render_fleet_section(snap)
    assert "## Fleet — your teammates (live)" in section
    assert "**rocky** [busy/healthy] — Ship X" in section
    assert "**natasha** [idle/healthy] — idle" in section

    md = tmp_path / "mac-runtime-context.md"
    md.write_text("# Runtime Context\n\n## Identity\n\nrocky\n", encoding="utf-8")
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
