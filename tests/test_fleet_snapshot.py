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
            {
                "name": "rocky",
                "status": "busy",
                "health": "healthy",
                "current_task_title": "Ship X",
            },
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


def test_fleet_snapshot_keeps_live_agents_ahead_of_offline_ones_under_the_cap():
    cp = ControlPlane.in_memory()
    for i in range(5):
        stale = _agent(cp, "aaa-old-session-%d" % i)
        cp.heartbeat_agent(stale.id, status="offline")
    _agent(cp, "natasha")

    names = [m["name"] for m in cp.fleet_snapshot(limit=3)["members"]]

    assert names[0] == "natasha" and len(names) == 3


def test_render_fleet_section_counts_offline_members_instead_of_listing_them():
    section = render_fleet_section(
        {
            "generated_at": "t",
            "members": [
                {"name": "natasha", "status": "idle", "health": "healthy"},
                {"name": "old-session", "status": "offline", "health": "healthy"},
                {"name": "gone", "status": "offline", "health": "healthy", "departed_at": "t"},
            ],
        }
    )
    assert "**natasha**" in section and "**gone**" in section
    assert "old-session" not in section
    assert "1 more offline" in section


def test_refresh_context_reads_the_fleet_with_the_agents_own_credential(
    tmp_path, monkeypatch, capsys
):
    """A worker's operator MAC_API_TOKEN can be stale while its worker token works."""
    import argparse

    from mac import cli

    seen = {}

    class Plane:
        def fleet_snapshot(self, exclude_agent_id=None):
            return {"generated_at": "t", "members": []}

    def plane(args):
        seen["token"] = args.token
        return Plane()

    monkeypatch.setattr(cli, "_plane", plane)
    monkeypatch.delenv("MAC_HUB_URL", raising=False)
    monkeypatch.delenv("MAC_URL", raising=False)
    monkeypatch.setenv("MAC_FLEET", "mac")
    monkeypatch.setenv("MAC_API_TOKEN", "stale-operator-token")
    monkeypatch.setenv("MAC_WORKER_TOKEN__MAC", "worker-token")
    md = tmp_path / "ctx.md"

    cli.cmd_fleet_refresh_context(argparse.Namespace(agent="agent_x", markdown=str(md), token=None))

    assert seen["token"] == "worker-token"
    assert FLEET_SECTION_BEGIN in md.read_text()

    cli.cmd_fleet_refresh_context(
        argparse.Namespace(agent="agent_x", markdown=str(md), token="explicit")
    )
    assert seen["token"] == "explicit"
