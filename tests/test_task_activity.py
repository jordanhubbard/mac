from __future__ import annotations

from mac.services import ControlPlane
from mac.worker import _extract_marked_summary, _prose_tail
from mac.task_executor import MAC_TASK_SUMMARY_BEGIN, MAC_TASK_SUMMARY_END


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


def test_task_detail_includes_attributed_resolved_llm_usage():
    cp = ControlPlane.in_memory()
    task = cp.create_task("model-ledger task", required_capabilities=[])
    cp.record_log(
        "llm.route",
        layer="router",
        source="agent_rocky",
        subject_type="task",
        subject_id=task.id,
        detail={
            "schema": "mac.llm_route.v1",
            "agent_id": "agent_rocky",
            "lease_id": "lease_1",
            "requested_model": "*",
            "resolved_model": "azure/anthropic/claude-sonnet-4-6",
            "response_model": "claude-sonnet-4-6",
            "provider": "nvidia",
            "status_code": 200,
            "outcome": "success",
            "input_tokens": 100,
            "output_tokens": 25,
            "total_tokens": 125,
        },
    )
    cp.record_log(
        "llm.route",
        layer="router",
        source="agent_rocky",
        subject_type="agent",
        subject_id="agent_rocky",
        detail={
            "resolved_model": "unattributed/model",
            "provider": "other",
            "total_tokens": 999,
        },
    )

    usage = cp.task_detail(task.id)["llm_usage"]

    assert usage["schema"] == "mac.task_llm_usage.v1"
    assert usage["observed_route_count"] == 1
    assert usage["resolved_models"] == [
        "azure/anthropic/claude-sonnet-4-6"
    ]
    assert usage["response_models"] == ["claude-sonnet-4-6"]
    assert usage["providers"] == ["nvidia"]
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 25
    assert usage["total_tokens"] == 125
    assert usage["routes"][0]["agent_id"] == "agent_rocky"


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


def test_extract_marked_summary_prefers_the_agents_delimited_recap():
    """The definitive prose: parse the agent's delimited summary block (clean
    recap) rather than scraping diff/setup noise; matches the real markers the
    executor injects, tolerates ANSI, and returns '' when absent (so the caller
    falls back to _prose_tail)."""
    recap = "Added mathx_lcm + a shadow test; make test-quick passed; pushed the branch."
    out = "\n".join([
        "Created sandbox: x",
        "+ some diff line",
        '(println "ALL OK")',
        MAC_TASK_SUMMARY_BEGIN,
        recap,
        MAC_TASK_SUMMARY_END,
        "make test-quick: 6 passed",  # trailing test output AFTER the block
    ])
    assert _extract_marked_summary(out) == recap
    # tolerant of ANSI styling around the markers
    ansi = "\x1b[1m%s\x1b[0m\nclean recap line\n%s" % (MAC_TASK_SUMMARY_BEGIN, MAC_TASK_SUMMARY_END)
    assert _extract_marked_summary(ansi) == "clean recap line"
    # absent -> empty so the caller falls back to _prose_tail
    assert _extract_marked_summary("no markers here, just output") == ""


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
