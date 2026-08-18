"""The read-only observability console: snapshot endpoint + shell.

These tests cover the three properties the console is built on:

1. it is READ-ONLY (no route it uses can mutate, and the whole app it serves
   is asserted read-only by ``observe/tests/readonly.test.ts``);
2. it reports movement, not just counts — dwell percentiles and bucketed
   transitions;
3. it DEGRADES HONESTLY — a section that cannot be read is absent and named in
   ``degraded``, never rendered as a plausible zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from mac.api import _required_scope, create_app
from mac.models import new_id, utcnow
from mac.observability_console import (
    SCHEMA_VERSION,
    bucket_transitions,
    build_console_snapshot,
    build_task_drilldown,
    build_transcript_entry,
    dwell_percentiles,
)
from mac.services import ControlPlane


# ---------------------------------------------------------------------------
# Pure shaping logic — no database
# ---------------------------------------------------------------------------


def test_dwell_percentiles_are_real_observations_not_interpolations():
    ages = [10.0, 20.0, 30.0, 40.0, 100.0]
    out = dwell_percentiles(ages)
    assert out["count"] == 5
    assert out["max"] == 100.0
    # Nearest-rank: every reported value is an age some task actually has.
    assert out["p50"] in ages
    assert out["p90"] in ages
    assert out["p50"] <= out["p90"] <= out["max"]


def test_dwell_percentiles_of_nothing_is_none_not_zero():
    """An empty sample must not read as "0 seconds of dwell"."""
    out = dwell_percentiles([])
    assert out == {"count": 0, "p50": None, "p90": None, "max": None}


def test_dwell_percentiles_single_sample():
    assert dwell_percentiles([42.0]) == {
        "count": 1,
        "p50": 42.0,
        "p90": 42.0,
        "max": 42.0,
    }


def test_bucket_transitions_emits_the_whole_grid_including_gaps():
    end = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    start = end - timedelta(hours=1)
    rows = [
        {"bucket": "2026-08-17T11:05", "to_state": "running", "n": 2},
        {"bucket": "2026-08-17T11:55", "to_state": "running", "n": 3},
        {"bucket": "2026-08-17T11:55", "to_state": "blocked", "n": 7},
    ]
    out = bucket_transitions(rows, start=start, end=end, buckets=6)
    assert len(out["bucket_starts"]) == 6
    assert out["bucket_seconds"] == 600.0
    # 11:05 lands in bucket 0, 11:55 in bucket 5 — the four empty buckets are
    # still present, so a quiet hour reads as quiet rather than as a short chart.
    assert out["series"]["running"] == [2, 0, 0, 0, 0, 3]
    assert out["series"]["blocked"] == [0, 0, 0, 0, 0, 7]
    assert out["dropped_rows"] == 0


def test_bucket_transitions_counts_rows_it_could_not_place():
    end = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    rows = [
        {"bucket": "not-a-timestamp", "to_state": "running", "n": 1},
        {"bucket": "1999-01-01T00:00", "to_state": "running", "n": 1},
    ]
    out = bucket_transitions(rows, start=end - timedelta(hours=1), end=end, buckets=4)
    assert out["dropped_rows"] == 2
    assert out["series"] == {}


# ---------------------------------------------------------------------------
# Snapshot against a real control plane
# ---------------------------------------------------------------------------


@pytest.fixture()
def cp() -> ControlPlane:
    return ControlPlane.in_memory()


def test_snapshot_of_an_empty_hub_is_all_zeros_and_no_degradation(cp: ControlPlane):
    snap = build_console_snapshot(cp)
    assert snap["schema"] == SCHEMA_VERSION
    # `dreams` is the one section allowed to be missing on a fresh hub:
    # dream_runs is created lazily by mac.dreaming.store, not by schema.sql, so
    # its absence is the true statement "dreaming has never run here" and must
    # be reported as such rather than as zero dream runs.
    assert {entry["section"] for entry in snap["degraded"]} <= {"dreams"}, snap[
        "degraded"
    ]
    assert snap["tasks"]["total"] == 0
    assert snap["tasks"]["by_state"] == {}
    assert snap["agents"]["rows"] == []
    assert snap["stuck"] == []
    # Sections present-and-empty is a different claim from sections missing.
    for key in ("tasks", "agents", "projects", "flow", "pipelines", "telemetry"):
        assert key in snap


def test_snapshot_counts_tasks_by_state_and_reports_dwell(cp: ControlPlane):
    for _ in range(3):
        cp.create_task(title="stuck thing", description="", project="alpha")
    snap = build_console_snapshot(cp)
    assert snap["tasks"]["by_state"]["open"] == 3
    assert snap["tasks"]["total"] == 3
    dwell = snap["tasks"]["dwell_seconds"]["open"]
    assert dwell["count"] == 3
    assert dwell["p50"] is not None and dwell["p50"] >= 0.0
    projects = {row["project"] for row in snap["projects"]["rows"]}
    assert "alpha" in projects


def test_snapshot_surfaces_the_oldest_stuck_work_first(cp: ControlPlane):
    old = cp.create_task(title="ancient", description="")
    cp.create_task(title="fresh", description="")
    long_ago = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    cp.store.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", (long_ago, old.id)
    )
    snap = build_console_snapshot(cp)
    assert snap["stuck"][0]["id"] == old.id
    assert snap["stuck"][0]["dwell_seconds"] > 8 * 86400


def test_snapshot_reports_transition_flow_over_the_window(cp: ControlPlane):
    task = cp.create_task(title="moves", description="")
    now = datetime.now(timezone.utc)
    for minutes, to_state in ((50, "claimed"), (20, "running"), (5, "blocked")):
        cp.store.execute(
            "INSERT INTO task_history (id, task_id, event_type, actor, from_state, "
            "to_state, detail, created_at) VALUES (?, ?, 'task.transitioned', "
            "'tester', 'open', ?, '{}', ?)",
            (
                new_id("hist"),
                task.id,
                to_state,
                (now - timedelta(minutes=minutes)).isoformat(),
            ),
        )
    snap = build_console_snapshot(cp, window_hours=1.0, buckets=6)
    assert snap["flow"]["total"] == 3
    assert set(snap["flow"]["series"]) == {"claimed", "running", "blocked"}
    assert len(snap["flow"]["bucket_starts"]) == 6
    ticker = snap["transitions"]
    assert [row["to_state"] for row in ticker] == ["blocked", "running", "claimed"]
    assert ticker[0]["title"] == "moves"


def test_snapshot_flags_agents_whose_reported_status_is_not_believable(
    cp: ControlPlane,
):
    machine = cp.register_machine(hostname="m1")
    agent = cp.register_agent(
        machine_id=machine.id, name="ghost", capabilities=["python"]
    )
    stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    cp.store.execute(
        "UPDATE agents SET status = 'busy', last_seen_at = ? WHERE id = ?",
        (stale, agent.id),
    )
    snap = build_console_snapshot(cp)
    row = next(r for r in snap["agents"]["rows"] if r["id"] == agent.id)
    assert row["status"] == "busy"
    assert row["seconds_since_seen"] > 3000
    # We report the contradiction; we do not silently rewrite the hub's belief.
    assert row["belief_contradicted"] is True


def test_snapshot_does_not_flag_a_freshly_seen_agent(cp: ControlPlane):
    machine = cp.register_machine(hostname="m2")
    agent = cp.register_agent(machine_id=machine.id, name="live", capabilities=[])
    cp.store.execute(
        "UPDATE agents SET status = 'busy', last_seen_at = ? WHERE id = ?",
        (utcnow(), agent.id),
    )
    snap = build_console_snapshot(cp)
    row = next(r for r in snap["agents"]["rows"] if r["id"] == agent.id)
    assert row["belief_contradicted"] is False


def test_a_broken_section_is_named_and_omitted_never_rendered_as_zero(
    cp: ControlPlane,
):
    """The core honesty property.

    If a table cannot be read, the section must vanish from the payload and
    appear in ``degraded``. A dashboard that shows "0 failures" because it
    could not reach the table is the worst possible thing to add to this
    codebase, so the absence has to be structural rather than a convention.
    """
    cp.store.execute("DROP TABLE IF EXISTS nap_runs CASCADE")
    snap = build_console_snapshot(cp)
    assert "cycles" not in snap
    reasons = {entry["section"]: entry["reason"] for entry in snap["degraded"]}
    assert "cycles" in reasons
    assert reasons["cycles"]
    # Unaffected sections still answer.
    assert "tasks" in snap


def test_window_is_clamped_rather_than_trusted(cp: ControlPlane):
    assert build_console_snapshot(cp, window_hours=99999)["window"]["hours"] == 168.0
    assert build_console_snapshot(cp, window_hours=0)["window"]["hours"] == 0.25
    assert build_console_snapshot(cp, window_hours="nonsense")["window"]["hours"] == 6.0


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def _client(cp: ControlPlane, **kwargs) -> TestClient:
    return TestClient(create_app(control_plane=cp, **kwargs))


def test_endpoint_returns_the_snapshot(cp: ControlPlane):
    resp = _client(cp).get("/dashboard/observe")
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema"] == SCHEMA_VERSION
    assert "observability_sequence" in body
    assert isinstance(body["build_ms"], float)


def test_endpoint_is_get_only(cp: ControlPlane):
    client = _client(cp)
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/dashboard/observe").status_code == 405


def test_endpoint_rejects_an_out_of_range_window(cp: ControlPlane):
    client = _client(cp)
    assert client.get("/dashboard/observe?window_hours=100000").status_code == 422
    assert client.get("/dashboard/observe?buckets=100000").status_code == 422
    assert client.get("/dashboard/observe?window_hours=24&buckets=48").status_code == 200


def test_endpoint_carries_the_same_scope_bar_as_the_legacy_dashboard():
    # Not narrowed to "read": `write` does not imply `read` in this codebase
    # (only admin is a catch-all), so an operator token that opens today's
    # dashboard must keep opening the console.
    assert _required_scope("GET", "/dashboard/observe") == _required_scope(
        "GET", "/dashboard/state"
    )


def test_console_shell_is_served_and_public(cp: ControlPlane):
    client = _client(cp, auth_tokens={"secret": "admin"})
    for path in ("/ui/console", "/ui/console/"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["content-type"].startswith("text/html")
    assert 'id="root"' in client.get("/ui/console").text


def test_console_bundle_is_served_from_the_existing_ui_assets_mount(cp: ControlPlane):
    resp = _client(cp).get("/ui/assets/console/console.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_legacy_dashboard_shell_is_untouched(cp: ControlPlane):
    """The console is additive. /ui/ must still serve the legacy shell."""
    html = _client(cp).get("/ui").text
    assert 'id="loginScreen"' in html
    assert "/ui/assets/app.js?v=" in html


# ---------------------------------------------------------------------------
# Task drill-down: what actually happened during a task
# ---------------------------------------------------------------------------


def _record_transcript(cp: ControlPlane, task_id: str, **kwargs) -> str:
    """Insert one transcript turn through the control plane's own writer."""
    return cp.record_task_transcript(task_id, **kwargs)["id"]


def test_drilldown_of_an_unknown_task_says_not_found_not_empty(cp: ControlPlane):
    """A missing task must be distinguishable from a task with no activity."""
    out = build_task_drilldown(cp, "task_does_not_exist")
    assert out["found"] is False
    assert "task" not in out
    # Crucially: no empty history/transcript lists that would render as
    # "this task did nothing".
    assert "history" not in out
    assert "transcripts" not in out


def test_drilldown_returns_state_history_and_the_task_itself(cp: ControlPlane):
    task = cp.create_task(title="drill me", description="", project="mac")
    cp.store.execute(
        "INSERT INTO task_history (id, task_id, event_type, actor, from_state, "
        "to_state, detail, created_at) VALUES "
        "(?, ?, 'task.transitioned', 'tester', 'open', 'running', '{}', ?)",
        (new_id("hist"), task.id, utcnow()),
    )
    out = build_task_drilldown(cp, task.id)
    assert out["found"] is True
    assert out["task"]["title"] == "drill me"
    assert out["task"]["dwell_seconds"] is not None
    kinds = {event["event_type"] for event in out["history"]}
    assert "task.transitioned" in kinds
    assert out["transcripts"]["count"] == 0
    # An empty list here is a real reading: the task exists, nothing was
    # recorded for it. The UI turns this into "no transcript recorded".
    assert out["transcripts"]["rows"] == []


def test_a_task_with_no_transcript_is_not_the_same_as_a_broken_read(
    cp: ControlPlane,
):
    task = cp.create_task(title="quiet", description="")
    healthy = build_task_drilldown(cp, task.id)
    assert healthy["transcripts"]["count"] == 0
    assert healthy["degraded"] == []

    cp.store.execute("DROP TABLE IF EXISTS task_agent_transcripts CASCADE")
    broken = build_task_drilldown(cp, task.id)
    assert "transcripts" not in broken
    assert any(entry["section"] == "transcripts" for entry in broken["degraded"])


def test_drilldown_lists_transcript_turns_without_their_payloads(cp: ControlPlane):
    task = cp.create_task(title="talky", description="")
    _record_transcript(
        cp,
        task.id,
        prompt="write the thing",
        response="wrote the thing",
        stderr="",
        agent_id="agent_1",
        command_id="cmd_1",
        returncode=0,
    )
    out = build_task_drilldown(cp, task.id)
    turn = out["transcripts"]["rows"][0]
    # Metadata is present...
    assert turn["sequence"] == 0
    assert turn["command_id"] == "cmd_1"
    assert turn["returncode"] == 0
    assert turn["payload_bytes"] > 0
    assert turn["has_payload"] is True
    # ...and the text is NOT, because a list view must not ship zlib blobs.
    assert "prompt" not in turn
    assert "response" not in turn
    assert "payload" not in turn


def test_missing_attribution_is_reported_as_absent_not_blank(cp: ControlPlane):
    """`coding_agent` and `model` are empty on every row on the live hub.

    An empty string renders as a blank cell that reads as "no CLI was used".
    Normalising to None gives the UI one thing to test so it can say
    "unattributed" instead.
    """
    task = cp.create_task(title="anon", description="")
    _record_transcript(cp, task.id, prompt="p", response="r", stderr="")
    cp.store.execute(
        "UPDATE task_agent_transcripts SET coding_agent = '', model = NULL "
        "WHERE task_id = ?",
        (task.id,),
    )
    out = build_task_drilldown(cp, task.id)
    turn = out["transcripts"]["rows"][0]
    assert turn["coding_agent"] is None
    assert turn["model"] is None
    assert out["transcripts"]["attributed"] == 0
    assert out["transcripts"]["unattributed"] == 1


def test_drilldown_joins_command_audit_by_task(cp: ControlPlane):
    task = cp.create_task(title="ran things", description="")
    cp.store.execute(
        "INSERT INTO command_audit (id, command_id, agent_id, phase, argv, cwd, "
        "task_id, started_at, duration_ms, returncode, metadata, created_at) "
        "VALUES (?, 'cmd_1', 'agent_1', 'completed', "
        "'[\"openshell\",\"sandbox\",\"create\"]', '/w', ?, ?, 12.5, 0, '{}', ?)",
        (new_id("audit"), task.id, utcnow(), utcnow()),
    )
    out = build_task_drilldown(cp, task.id)
    assert out["commands"][0]["command_id"] == "cmd_1"
    assert out["commands"][0]["returncode"] == 0
    assert out["commands"][0]["age_seconds"] is not None


def test_transcript_entry_returns_the_decompressed_text(cp: ControlPlane):
    task = cp.create_task(title="expand me", description="")
    transcript_id = _record_transcript(
        cp, task.id, prompt="the prompt", response="the response", stderr="a warning"
    )
    entry = build_transcript_entry(cp, transcript_id)
    assert entry["found"] is True
    assert entry["prompt"]["text"] == "the prompt"
    assert entry["response"]["text"] == "the response"
    assert entry["stderr"]["text"] == "a warning"
    assert entry["prompt"]["clipped"] is False
    assert entry["coding_agent"] is None


def test_transcript_entry_declares_when_it_clipped(cp: ControlPlane):
    from mac import observability_console as oc

    task = cp.create_task(title="huge", description="")
    big = "x" * (oc.TRANSCRIPT_TEXT_CAP + 500)
    transcript_id = _record_transcript(cp, task.id, prompt=big, response="", stderr="")
    entry = build_transcript_entry(cp, transcript_id)
    assert entry["prompt"]["clipped"] is True
    assert entry["prompt"]["full_length"] == len(big)
    assert len(entry["prompt"]["text"]) == oc.TRANSCRIPT_TEXT_CAP


def test_unknown_transcript_id_is_found_false(cp: ControlPlane):
    entry = build_transcript_entry(cp, "transcript_nope")
    assert entry["found"] is False
    assert "prompt" not in entry


def test_snapshot_reports_fleet_wide_transcript_coverage(cp: ControlPlane):
    for index in range(4):
        task = cp.create_task(title="t%d" % index, description="")
        if index == 0:
            _record_transcript(cp, task.id, prompt="p", response="r", stderr="")
    coverage = build_console_snapshot(cp)["transcripts"]
    assert coverage["tasks_total"] == 4
    assert coverage["tasks_with_transcript"] == 1
    assert coverage["coverage_fraction"] == 0.25
    assert coverage["rows_total"] == 1


def test_coverage_fraction_is_none_when_there_is_nothing_to_divide_by(
    cp: ControlPlane,
):
    coverage = build_console_snapshot(cp)["transcripts"]
    assert coverage["tasks_total"] == 0
    # Not 0.0: "no tasks" is not "0% coverage".
    assert coverage["coverage_fraction"] is None


def test_drilldown_endpoints_are_get_only_and_answer(cp: ControlPlane):
    task = cp.create_task(title="http", description="")
    client = _client(cp)
    resp = client.get("/dashboard/observe/tasks/%s" % task.id)
    assert resp.status_code == 200
    assert resp.json()["found"] is True
    for method in ("post", "put", "patch", "delete"):
        assert (
            getattr(client, method)("/dashboard/observe/tasks/%s" % task.id).status_code
            == 405
        )
    assert client.get("/dashboard/observe/tasks/nope").json()["found"] is False
    assert client.get("/dashboard/observe/transcripts/nope").json()["found"] is False


# ---------------------------------------------------------------------------
# Merge queue — the newest first-class object, and the one built to be watched
# ---------------------------------------------------------------------------


def _queue_entry(cp: ControlPlane, **over) -> None:
    """Insert one merge_queue_entries row directly.

    The queue's own API is exercised in tests/test_native_merge_queue.py. What
    this section must get right is READING, so rows go in by SQL: a section that
    only works against state its own writer produced is a section that has not
    been tested against the table.
    """
    row = {
        "id": new_id("mqe"),
        "repository": "jordanhubbard/mac",
        "branch": "main",
        "task_id": new_id("task"),
        "pull_request_number": 1,
        "head_sha": "a" * 40,
        "state": "queued",
        "position": 0,
        "speculation_epoch": 0,
        "tested_base_sha": "",
        "tested_base_tree": "",
        "tested_merge_tree": "",
        "predecessors": "[]",
        "attempts": 0,
        "eviction_reason": "",
        "landed_sha": "",
        "detail": "{}",
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    row.update(over)
    cp.store.execute(
        "INSERT INTO merge_queue_entries (%s) VALUES (%s)"
        % (", ".join(row), ", ".join("?" for _ in row)),
        tuple(row.values()),
    )


def test_an_empty_merge_queue_is_reported_not_omitted(cp: ControlPlane):
    """Zero queues is a fact. Absent would mean the section could not be read."""
    section = build_console_snapshot(cp)["merge_queue"]

    assert section["queue_count"] == 0
    assert section["total_depth"] == 0
    assert section["queues"] == []
    assert section["recent_evictions"] == []


def test_depth_counts_only_live_entries(cp: ControlPlane):
    """A landed change is not still waiting to land.

    Counting terminal rows in depth would make the queue look permanently
    backed up, which is the failure mode this whole section exists to prevent:
    a watcher that reports work in flight that is not in flight.
    """
    for state in ("queued", "testing", "tested", "landed", "evicted", "superseded"):
        _queue_entry(cp, state=state)

    section = build_console_snapshot(cp)["merge_queue"]
    queue = section["queues"][0]

    assert queue["depth"] == 3, "depth must count queued + testing + tested only"
    assert section["total_depth"] == 3
    # Every state is still reported, so nothing is hidden -- just not counted.
    assert queue["by_state"]["landed"] == 1
    assert queue["by_state"]["superseded"] == 1


def test_queues_are_separated_by_repository_and_branch(cp: ControlPlane):
    """One queue per (repository, branch) is the queue's own partitioning."""
    _queue_entry(cp, repository="a/one", branch="main")
    _queue_entry(cp, repository="a/one", branch="release")
    _queue_entry(cp, repository="b/two", branch="main")

    section = build_console_snapshot(cp)["merge_queue"]

    assert section["queue_count"] == 3
    assert {(q["repository"], q["branch"]) for q in section["queues"]} == {
        ("a/one", "main"),
        ("a/one", "release"),
        ("b/two", "main"),
    }


def test_the_deepest_queue_is_listed_first(cp: ControlPlane):
    """The console shows a list; the backed-up queue is the one to look at."""
    _queue_entry(cp, repository="shallow/repo")
    for _ in range(3):
        _queue_entry(cp, repository="deep/repo")

    section = build_console_snapshot(cp)["merge_queue"]

    assert section["queues"][0]["repository"] == "deep/repo"


def test_the_window_and_its_counters_are_reported(cp: ControlPlane):
    """The AIMD window is the queue's control signal; a shrinking window is how
    an operator sees that speculation is failing."""
    cp.store.execute(
        "INSERT INTO merge_queue_windows (repository, branch, window_size, "
        "landed_count, failure_count, speculation_discarded, last_event, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("jordanhubbard/mac", "main", 3, 12, 2, 5, "landed", utcnow()),
    )
    _queue_entry(cp)

    queue = build_console_snapshot(cp)["merge_queue"]["queues"][0]

    assert queue["window_size"] == 3
    assert queue["landed_count"] == 12
    assert queue["failure_count"] == 2
    assert queue["speculation_discarded"] == 5
    assert queue["last_event"] == "landed"


def test_a_queue_with_no_window_row_reports_an_unknown_window(cp: ControlPlane):
    """None, not 1. A fresh queue has never sized its window, and inventing a
    floor here would make 'never speculated' indistinguishable from 'backed all
    the way off'."""
    _queue_entry(cp)

    assert build_console_snapshot(cp)["merge_queue"]["queues"][0]["window_size"] is None


def test_recent_evictions_carry_the_reason(cp: ControlPlane):
    """Why a change did NOT land is the question an operator arrives with."""
    _queue_entry(
        cp,
        state="evicted",
        eviction_reason="tests_failed_on_speculative_base",
        pull_request_number=406,
    )

    evictions = build_console_snapshot(cp)["merge_queue"]["recent_evictions"]

    assert len(evictions) == 1
    assert evictions[0]["eviction_reason"] == "tests_failed_on_speculative_base"
    assert evictions[0]["pull_request_number"] == 406


def test_an_eviction_with_no_reason_is_not_listed(cp: ControlPlane):
    """A blank reason answers nothing; listing it spends a row on no signal."""
    _queue_entry(cp, state="evicted", eviction_reason="")

    assert build_console_snapshot(cp)["merge_queue"]["recent_evictions"] == []


def test_the_section_degrades_honestly(cp: ControlPlane, monkeypatch):
    """The console's central promise: unreadable is absent and named, never a
    plausible zero. A merge queue that renders as 'depth 0' when the table
    cannot be read is exactly the gate-reporting-healthy failure it exists to
    catch."""
    real = cp.store.query_all

    def explode(sql, params=()):
        if "merge_queue" in sql:
            raise RuntimeError('relation merge_queue_entries does not exist')
        return real(sql, params)

    monkeypatch.setattr(cp.store, "query_all", explode)
    payload = build_console_snapshot(cp)

    assert "merge_queue" not in payload, "a failed section must be ABSENT, not zero"
    assert any(d["section"] == "merge_queue" for d in payload["degraded"])
