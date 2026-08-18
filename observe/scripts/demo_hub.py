"""Run a throwaway hub seeded with fleet-shaped data, for looking at the console.

Not a test fixture and not shipped: this exists so a developer (or a
screenshot) can see the console against data with the same shape as the real
fleet -- lots of blocked work, a few runners, an agent whose reported status is
not believable.

    uv run --extra dev python observe/scripts/demo_hub.py    # then open /ui/console
"""

from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone

import uvicorn

from mac.api import create_app
from mac.models import new_id, utcnow as utcnow_str
from mac.services import ControlPlane

PORT = int(os.environ.get("MAC_DEMO_PORT", "8799"))


def seed(cp: ControlPlane) -> None:
    rng = random.Random(7)
    now = datetime.now(timezone.utc)
    machine = cp.register_machine(hostname="rocky")

    agents = []
    for name, status, health, seen_minutes in (
        ("rocky-build", "busy", "healthy", 0),
        ("natasha-review", "idle", "healthy", 1),
        ("bullwinkle-int", "busy", "degraded", 240),
        ("gke-worker-1", "idle", "healthy", 2),
        ("gke-worker-2", "offline", "unhealthy", 4000),
    ):
        agent = cp.register_agent(machine_id=machine.id, name=name, capabilities=["python"])
        cp.store.execute(
            "UPDATE agents SET status = ?, health_status = ?, last_seen_at = ? WHERE id = ?",
            (
                status,
                health,
                (now - timedelta(minutes=seen_minutes)).isoformat(),
                agent.id,
            ),
        )
        agents.append(agent)

    shape = {
        "blocked": 360,
        "reviewing": 17,
        "needs_input": 6,
        "running": 2,
        "open": 41,
        "completed": 720,
        "failed": 207,
        "cancelled": 352,
    }
    projects = ["mac", "openclaw", "hermes", "fleet-ops"]
    for state, total in shape.items():
        for index in range(total):
            task = cp.create_task(
                title="%s work item %d" % (state, index),
                description="",
                project=rng.choice(projects),
            )
            age = timedelta(minutes=rng.randint(1, 60 * 24 * 12))
            cp.store.execute(
                "UPDATE tasks SET state = ?, updated_at = ?, owner_agent_id = ? WHERE id = ?",
                (
                    state,
                    (now - age).isoformat(),
                    agents[rng.randrange(len(agents))].id if state == "running" else None,
                    task.id,
                ),
            )
            # A transition trail so the flow chart and ticker have something real.
            if rng.random() < 0.06:
                cp.store.execute(
                    "INSERT INTO task_history (id, task_id, event_type, actor, "
                    "from_state, to_state, detail, created_at) VALUES "
                    "(?, ?, 'task.transitioned', 'demo', 'open', ?, '{}', ?)",
                    (
                        new_id("hist"),
                        task.id,
                        state,
                        (now - timedelta(minutes=rng.randint(1, 355))).isoformat(),
                    ),
                )


def seed_transcripts(cp: ControlPlane) -> None:
    """A handful of tasks with a recorded conversation, most with none.

    Mirrors the live hub's shape on purpose: coverage around 2%, and every
    historical row unattributed (coding_agent/model empty) so the console's
    "unattributed" path is exercised rather than only its happy path.
    """
    rows = cp.store.query_all("SELECT id FROM tasks ORDER BY id LIMIT 30")
    for index, row in enumerate(rows[:6]):
        for turn in range(3):
            cp.record_task_transcript(
                row["id"],
                prompt="turn %d: implement the change and run the tests" % turn,
                response=("ok, edited src/mac/thing.py\n" * (40 * (turn + 1))),
                stderr="" if turn < 2 else "warning: 2 tests skipped",
                agent_id="agent_demo",
                command_id="cmd_%d_%d" % (index, turn),
                returncode=0 if turn < 2 else 1,
            )
        # The attribution bug: empty on every historical row.
        cp.store.execute(
            "UPDATE task_agent_transcripts SET coding_agent = '', model = NULL "
            "WHERE task_id = ?",
            (row["id"],),
        )
        cp.store.execute(
            "INSERT INTO command_audit (id, command_id, agent_id, phase, argv, "
            "cwd, task_id, started_at, duration_ms, returncode, stdout_bytes, "
            "stderr_bytes, metadata, created_at) VALUES "
            "(?, ?, 'agent_demo', 'completed', "
            "'[\"openshell\",\"sandbox\",\"exec\"]', '/w', ?, ?, 812.0, 0, "
            "4096, 0, '{}', ?)",
            (
                new_id("audit"),
                "cmd_%d_0" % index,
                row["id"],
                utcnow_str(),
                utcnow_str(),
            ),
        )


def main() -> None:
    cp = ControlPlane.in_memory()
    seed(cp)
    seed_transcripts(cp)
    app = create_app(control_plane=cp)
    print("console: http://127.0.0.1:%d/ui/console" % PORT, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
