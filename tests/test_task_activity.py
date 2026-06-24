from __future__ import annotations

from mac.services import ControlPlane


def _activity(cp: ControlPlane, task_id: str):
    return (cp.get_task(task_id).metadata or {}).get("activity") or []


def test_append_task_activity_caps_trims_and_orders():
    cp = ControlPlane.in_memory()
    t = cp.create_task("activity task", required_capabilities=[])

    for i in range(30):
        cp.append_task_activity(t.id, "worker", "agent_rocky", "line %d" % i)
    act = _activity(cp, t.id)
    assert len(act) == 24, "should cap to max_entries (24)"
    assert act[-1]["summary"] == "line 29", "newest entry kept"
    assert act[0]["summary"] == "line 6", "oldest entries dropped"
    assert act[-1]["phase"] == "worker" and act[-1]["actor"] == "agent_rocky"
    assert act[-1].get("at"), "entry is timestamped"

    # multi-line prose is trimmed to a few lines (glanceable)
    cp.append_task_activity(t.id, "review", "agent_natasha", "\n".join("l%d" % j for j in range(20)))
    assert len(_activity(cp, t.id)[-1]["summary"].splitlines()) <= 6

    # empty/whitespace summary is a no-op (doesn't pollute the narrative)
    before = len(_activity(cp, t.id))
    cp.append_task_activity(t.id, "worker", "agent_rocky", "   \n  ")
    assert len(_activity(cp, t.id)) == before


def test_append_task_activity_is_additive_not_evidence():
    """Activity must not masquerade as durable evidence / verification."""
    cp = ControlPlane.in_memory()
    t = cp.create_task("additive task", required_capabilities=[])
    cp.append_task_activity(t.id, "env", "agent_natasha", "rebuilt sandbox image: added cc")
    detail = cp.task_detail(t.id)
    data = detail.to_dict() if hasattr(detail, "to_dict") else detail
    # narrative lives in task.metadata.activity, not in the evidence list
    assert (data["task"]["metadata"]["activity"][-1]["phase"]) == "env"
    assert not data.get("evidence"), "activity append must not create evidence rows"
