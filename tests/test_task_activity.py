from __future__ import annotations

from mac.services import ControlPlane
from mac.worker import _prose_tail


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


def test_prose_tail_prefers_agent_prose_over_diff_noise():
    """Worker narrative should read like the agent's recap, not raw diff lines."""
    out = "\n".join([
        "Created sandbox: mac-task-x",
        "+    } else {}",
        '     (println "ALL OK")',
        "diff --git a/x b/x",
        "I added mathx_lcm and a shadow test; make test-quick passes.",
        "Pushed the branch and recorded evidence.",
    ])
    tail = _prose_tail(out, 2)
    assert tail == [
        "I added mathx_lcm and a shadow test; make test-quick passes.",
        "Pushed the branch and recorded evidence.",
    ]
    # all-noise input still yields something (fallback to non-empty lines)
    assert _prose_tail("+a\n-b\n@@ c", 2)


def test_fleet_default_publication_target_env_fallback(monkeypatch):
    """Opt-in fleet default lets HUB-publishable tasks auto-complete; it is gated
    away from non-publishable tasks (would break the git publish), and explicit
    per-task targets always win."""
    cp = ControlPlane.in_memory()
    monkeypatch.delenv("MAC_DEFAULT_PUBLICATION_TARGET", raising=False)
    repo = cp.create_task(
        "repo task",
        required_capabilities=[],
        metadata={"origin": {"repository_url": "git@github.com:o/r.git"}},
    )
    localrepo = cp.create_task(
        "local repo task",
        required_capabilities=[],
        metadata={"origin": {"repository_path": "/agent/only/path/nanolang"}},
    )
    plain = cp.create_task("plain task", required_capabilities=[])  # no repo origin

    assert cp._default_publication_target(cp.get_task(repo.id)) is None
    monkeypatch.setenv("MAC_DEFAULT_PUBLICATION_TARGET", "git://main")
    # repo tasks get the fleet default (incl. a local-repo task with only an
    # agent-side path: the publish can clone the remote the worker pushed to)
    assert cp._default_publication_target(cp.get_task(repo.id)) == "git://main"
    assert cp._default_publication_target(cp.get_task(localrepo.id)) == "git://main"
    # a non-repo (operator) task is gated out -- a git default would break it
    assert cp._default_publication_target(cp.get_task(plain.id)) is None
    # an explicit per-task target still takes precedence over the fleet default
    cp.update_task(
        repo.id,
        metadata={
            "origin": {"repository_url": "git@github.com:o/r.git"},
            "publication_target": "git://origin/main",
        },
    )
    assert cp._default_publication_target(cp.get_task(repo.id)) == "git://origin/main"
