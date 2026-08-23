"""Contract tests for plan-outcome learning loop (plan-learn-01).

Covers:
- build_plan_learning_record: schema, children_count, children_titles,
  ordering_rationale, wall_clock_ms
- record_plan_outcome: reads plan manifest and records learning (happy path,
  missing manifest, non-plan evidence, invalid JSON all gracefully handled)
- _format_plan_learning_content: renders a stored blob as a lesson string
- _plan_family_terms: extracts meaningful keywords from title/description
- recall_plan_lessons: searches hub for prior plan records matching family terms
- _run_executor integration: plan lesson recorded after planning_phase_completed
- _run_executor integration: recalled plan lessons injected into planning prompt
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import mac.task_executor as te
from mac import executor_memory as memory
from mac.task_executor import (
    _PLAN_LEARNING_SCHEMA,
    _format_plan_learning_content,
    _plan_family_terms,
    build_plan_learning_record,
    record_plan_outcome,
    recall_plan_lessons,
    DEPLOYMENT_LEARNING_PREFIX,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    *,
    title: str = "Migrate database schema",
    description: str = "Migrate the postgres schema to the new shape.",
    project: str = "mac",
    task_id: str = "task_test_plan",
    attempt_count: int = 1,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    t: Dict[str, Any] = {
        "id": task_id,
        "title": title,
        "description": description,
        "project": project,
        "attempt_count": attempt_count,
    }
    if metadata is not None:
        t["metadata"] = metadata
    return t


def _large_task(task_id: str = "task_large_plan") -> Dict[str, Any]:
    return _task(
        task_id=task_id,
        metadata={
            "decomposition": {"max_children": 10, "kind": "one per subsystem"},
            "scope_estimate": {
                "schema": "mac.scope_estimate.v1",
                "size": "large",
                "estimated_units": 3,
                "signals": ["desc_words:300"],
                "rationale": "large",
            }
        },
    )


def _plan_manifest(children_count: int = 3) -> Dict[str, Any]:
    return {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "plan_decomposed",
        "summary": "Decomposed into %d children." % children_count,
        "children": [
            {"title": "Step %d: do subtask %d" % (i, i), "description": "detail %d" % i}
            for i in range(1, children_count + 1)
        ],
        "ordering_rationale": "leaves before cores; step 1 unblocks step 2",
        "coverage_claim": "covers all deliverables",
    }


# ---------------------------------------------------------------------------
# build_plan_learning_record — pure unit tests
# ---------------------------------------------------------------------------


class TestBuildPlanLearningRecord:
    def test_schema_is_plan_learning(self):
        task = _task()
        manifest = _plan_manifest()
        rec = build_plan_learning_record(task, manifest, wall_clock_ms=12345.0)
        content = json.loads(rec["content"])
        assert content["schema"] == _PLAN_LEARNING_SCHEMA

    def test_children_count_matches_manifest(self):
        task = _task()
        manifest = _plan_manifest(children_count=4)
        rec = build_plan_learning_record(task, manifest, wall_clock_ms=0)
        content = json.loads(rec["content"])
        assert content["children_count"] == 4

    def test_children_titles_extracted(self):
        task = _task()
        manifest = _plan_manifest(children_count=2)
        rec = build_plan_learning_record(task, manifest, wall_clock_ms=0)
        content = json.loads(rec["content"])
        assert len(content["children_titles"]) == 2
        assert all("Step" in t for t in content["children_titles"])

    def test_ordering_rationale_recorded(self):
        task = _task()
        manifest = _plan_manifest()
        rec = build_plan_learning_record(task, manifest, wall_clock_ms=0)
        content = json.loads(rec["content"])
        assert "leaves before cores" in content["ordering_rationale"]

    def test_wall_clock_ms_recorded(self):
        task = _task()
        manifest = _plan_manifest()
        rec = build_plan_learning_record(task, manifest, wall_clock_ms=99876.5)
        content = json.loads(rec["content"])
        assert content["wall_clock_ms"] == 99876

    def test_record_type_is_deployment_learning_project(self):
        task = _task(project="mig-proj")
        manifest = _plan_manifest()
        rec = build_plan_learning_record(task, manifest, wall_clock_ms=0)
        assert rec["record_type"] == "%s:%s" % (DEPLOYMENT_LEARNING_PREFIX, "mig-proj")

    def test_subject_type_and_id(self):
        task = _task(project="myproj")
        manifest = _plan_manifest()
        rec = build_plan_learning_record(task, manifest, wall_clock_ms=0)
        assert rec["subject_type"] == "project"
        assert rec["subject_id"] == "myproj"

    def test_task_id_and_title_included(self):
        task = _task(task_id="task_abc", title="My big migration")
        manifest = _plan_manifest()
        rec = build_plan_learning_record(task, manifest, wall_clock_ms=0)
        content = json.loads(rec["content"])
        assert content["task_id"] == "task_abc"
        assert content["task_title"] == "My big migration"

    def test_empty_children_gives_zero_count(self):
        task = _task()
        manifest = {
            "evidence_type": "plan_decomposed",
            "children": [],
            "ordering_rationale": "",
            "coverage_claim": "",
        }
        rec = build_plan_learning_record(task, manifest, wall_clock_ms=0)
        content = json.loads(rec["content"])
        assert content["children_count"] == 0
        assert content["children_titles"] == []

    def test_missing_children_gives_zero_count(self):
        task = _task()
        manifest = {"evidence_type": "plan_decomposed"}
        rec = build_plan_learning_record(task, manifest, wall_clock_ms=0)
        content = json.loads(rec["content"])
        assert content["children_count"] == 0


# ---------------------------------------------------------------------------
# record_plan_outcome — filesystem + hub
# ---------------------------------------------------------------------------


class TestRecordPlanOutcome:
    def test_records_learning_from_valid_manifest(self, tmp_path, monkeypatch):
        posted = []
        monkeypatch.setattr(memory, "_hub_post", lambda path, payload: posted.append(payload) or True)
        task = _task()
        manifest = _plan_manifest(children_count=3)
        (tmp_path / "mac-evidence.json").write_text(json.dumps(manifest), encoding="utf-8")

        result = record_plan_outcome(task, tmp_path, wall_clock_ms=5000.0)
        assert result is True
        assert len(posted) == 1
        content = json.loads(posted[0]["content"])
        assert content["schema"] == _PLAN_LEARNING_SCHEMA
        assert content["children_count"] == 3

    def test_returns_false_when_no_manifest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(memory, "_hub_post", lambda path, payload: True)
        assert record_plan_outcome(_task(), tmp_path, wall_clock_ms=0) is False

    def test_returns_false_for_non_plan_evidence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(memory, "_hub_post", lambda path, payload: True)
        (tmp_path / "mac-evidence.json").write_text(
            json.dumps({"evidence_type": "repo_change"}), encoding="utf-8"
        )
        assert record_plan_outcome(_task(), tmp_path, wall_clock_ms=0) is False

    def test_returns_false_for_invalid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(memory, "_hub_post", lambda path, payload: True)
        (tmp_path / "mac-evidence.json").write_text("not-json", encoding="utf-8")
        assert record_plan_outcome(_task(), tmp_path, wall_clock_ms=0) is False

    def test_returns_false_when_hub_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(memory, "_hub_post", lambda path, payload: False)
        task = _task()
        (tmp_path / "mac-evidence.json").write_text(json.dumps(_plan_manifest()), encoding="utf-8")
        assert record_plan_outcome(task, tmp_path, wall_clock_ms=0) is False

    def test_wall_clock_ms_passed_through(self, tmp_path, monkeypatch):
        posted = []
        monkeypatch.setattr(memory, "_hub_post", lambda path, payload: posted.append(payload) or True)
        (tmp_path / "mac-evidence.json").write_text(json.dumps(_plan_manifest()), encoding="utf-8")
        record_plan_outcome(_task(), tmp_path, wall_clock_ms=42000.0)
        content = json.loads(posted[0]["content"])
        assert content["wall_clock_ms"] == 42000


# ---------------------------------------------------------------------------
# _format_plan_learning_content
# ---------------------------------------------------------------------------


class TestFormatPlanLearningContent:
    def test_renders_title_and_children_count(self):
        task = _task(title="Big migration task")
        manifest = _plan_manifest(children_count=5)
        rec = build_plan_learning_record(task, manifest, wall_clock_ms=0)
        rendered = _format_plan_learning_content(rec["content"])
        assert "Big migration task" in rendered
        assert "5 children" in rendered

    def test_renders_ordering_rationale(self):
        task = _task()
        manifest = _plan_manifest()
        rec = build_plan_learning_record(task, manifest, wall_clock_ms=0)
        rendered = _format_plan_learning_content(rec["content"])
        assert "leaves before cores" in rendered

    def test_empty_string_for_non_plan_schema(self):
        blob = json.dumps({"schema": "mac.deployment_learning.v1", "outcome": "success"})
        assert _format_plan_learning_content(blob) == ""

    def test_graceful_on_invalid_json(self):
        result = _format_plan_learning_content("not-json")
        # should not raise; returns something truncated
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _plan_family_terms
# ---------------------------------------------------------------------------


class TestPlanFamilyTerms:
    def test_extracts_nouns_from_title(self):
        task = _task(title="Migrate postgres schema migrations")
        terms = _plan_family_terms(task)
        # Should have at least one non-stop word
        assert len(terms) > 0
        assert all(len(t) >= 3 for t in terms)

    def test_skips_stop_words(self):
        task = _task(title="Fix the and for to in")
        terms = _plan_family_terms(task)
        stop = {"the", "and", "for", "to", "in", "fix"}
        assert all(t not in stop for t in terms)

    def test_limits_to_four_terms(self):
        task = _task(title="alpha bravo charlie delta echo foxtrot")
        terms = _plan_family_terms(task)
        assert len(terms) <= 4

    def test_empty_title_returns_empty(self):
        task = _task(title="", description="")
        terms = _plan_family_terms(task)
        assert isinstance(terms, list)

    def test_uses_description_when_title_sparse(self):
        task = _task(title="Do it", description="vector embedding pipeline migration")
        terms = _plan_family_terms(task)
        # At least one term should come from description
        assert len(terms) > 0


# ---------------------------------------------------------------------------
# recall_plan_lessons — uses _hub_get stub
# ---------------------------------------------------------------------------


def _make_plan_record_raw(
    task_title: str = "Big migration",
    project: str = "mac",
    children_count: int = 3,
    ordering: str = "leaves first",
) -> str:
    content = {
        "schema": _PLAN_LEARNING_SCHEMA,
        "task_id": "task_old",
        "task_title": task_title,
        "project": project,
        "evidence_type": "plan_decomposed",
        "children_count": children_count,
        "children_titles": ["Step 1", "Step 2", "Step 3"][:children_count],
        "ordering_rationale": ordering,
        "coverage_claim": "full",
        "wall_clock_ms": 30000,
        "at": "2026-01-01T00:00:00Z",
    }
    return json.dumps(content)


class TestRecallPlanLessons:
    def test_returns_empty_when_hub_unreachable(self, monkeypatch):
        monkeypatch.setattr(memory, "_hub_get", lambda path: None)
        task = _task(title="Migrate schema")
        assert recall_plan_lessons(task) == []

    def test_returns_empty_when_no_matching_records(self, monkeypatch):
        monkeypatch.setattr(memory, "_hub_get", lambda path: [])
        task = _task(title="Migrate schema")
        assert recall_plan_lessons(task) == []

    def test_surfaces_plan_learning_record(self, monkeypatch):
        raw = _make_plan_record_raw(task_title="Schema migration plan", ordering="leaves first")
        records = [{"content": raw, "record_type": "deployment_learning:mac"}]
        monkeypatch.setattr(memory, "_hub_get", lambda path: records)
        task = _task(title="Schema migration plan")
        lessons = recall_plan_lessons(task, limit=5)
        assert len(lessons) >= 1
        assert "Schema migration plan" in lessons[0]

    def test_skips_non_plan_records(self, monkeypatch):
        # A record with deployment_learning schema should be filtered out
        non_plan = json.dumps({"schema": "mac.deployment_learning.v1", "outcome": "success"})
        records = [{"content": non_plan, "record_type": "deployment_learning:mac"}]
        monkeypatch.setattr(memory, "_hub_get", lambda path: records)
        task = _task(title="Schema migration")
        assert recall_plan_lessons(task) == []

    def test_deduplicates_identical_lessons(self, monkeypatch):
        raw = _make_plan_record_raw()
        records = [
            {"content": raw, "record_type": "deployment_learning:mac"},
            {"content": raw, "record_type": "deployment_learning:mac"},
        ]
        monkeypatch.setattr(memory, "_hub_get", lambda path: records)
        task = _task(title="Big migration task here")
        lessons = recall_plan_lessons(task, limit=5)
        # duplicates should be filtered
        assert len(lessons) == len(set(lessons))

    def test_respects_limit(self, monkeypatch):
        recs = [
            {"content": _make_plan_record_raw(task_title="Plan %d" % i, children_count=i + 1)}
            for i in range(10)
        ]
        monkeypatch.setattr(memory, "_hub_get", lambda path: recs)
        task = _task(title="Migration task")
        lessons = recall_plan_lessons(task, limit=2)
        assert len(lessons) <= 2

    def test_returns_list_on_invalid_hub_response(self, monkeypatch):
        monkeypatch.setattr(memory, "_hub_get", lambda path: "bad-response")
        assert recall_plan_lessons(_task()) == []


# ---------------------------------------------------------------------------
# _run_executor integration: plan lesson recorded + recalled
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run_executor_planning(monkeypatch, *, tmp_path: Path) -> Dict[str, list]:
    """Minimal patches for _run_executor to exercise the planning path."""
    state: Dict[str, list] = {
        "prompts": [],
        "telemetry": [],
        "plan_outcome_calls": [],
    }

    def fake_invoke_agent(runner, prompt, workspace, audit_id, opts):
        state["prompts"].append(prompt)
        # Write a plan_decomposed manifest so planning_phase_completed fires
        manifest = _plan_manifest(children_count=3)
        (workspace / "mac-evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
        return _FakeResult(0)

    monkeypatch.setattr(te, "recall_deployment_lessons", lambda task: [])
    monkeypatch.setattr(te, "recall_plan_lessons", lambda task, limit=3: [])
    monkeypatch.setattr(te, "emit_telemetry", lambda event, **kw: state["telemetry"].append(event) or True)
    monkeypatch.setattr(te, "_openshell_enabled", lambda: False)
    monkeypatch.setattr(te, "_openshell_required_for_local_agent", lambda: False)
    monkeypatch.setattr(te, "_invoke_agent", fake_invoke_agent)
    monkeypatch.setattr(te, "run_deterministic_git_finalizer", lambda *a, **kw: None)
    monkeypatch.setattr(te, "maybe_auto_decompose", lambda *a, **kw: False)
    monkeypatch.setattr(te, "write_fallback_evidence_manifest", lambda *a, **kw: None)
    monkeypatch.setattr(te, "classify_outcome", lambda *a, **kw: {"outcome": "success", "evidence_type": "plan_decomposed", "signals": []})
    monkeypatch.setattr(te, "record_deployment_learning", lambda *a, **kw: True)
    monkeypatch.setattr(te, "record_curated_lessons", lambda *a, **kw: 0)
    monkeypatch.setattr(te, "record_plan_outcome", lambda task, workspace, wall_clock_ms: state["plan_outcome_calls"].append(wall_clock_ms) or True)
    monkeypatch.setattr(te, "maybe_preflight_scope_estimate", lambda task: None)
    monkeypatch.setattr(
        te,
        "hub_write_capability",
        lambda **kw: {
            "schema": "mac.sandbox_hub_connectivity.v1",
            "ready": True,
            "reason": "ready",
            "has_url": True,
            "has_token": True,
            "reachable": True,
            "loopback_url": False,
            "url_host": "hub.example.test",
        },
    )
    monkeypatch.setattr(te, "_manifest_is_complete", lambda *a, **kw: True)
    monkeypatch.setattr(te, "_review_experiment_assignment", lambda t: {})

    return state


def _fake_runner(argv, cwd, task_id, metadata):
    return _FakeResult(0)


class TestRunExecutorPlanLearningIntegration:
    def test_record_plan_outcome_called_after_planning_phase_completed(
        self, monkeypatch, tmp_path: Path
    ):
        """After a successful planning phase, record_plan_outcome must be called."""
        state = _patch_run_executor_planning(monkeypatch, tmp_path=tmp_path)

        task = _large_task(task_id="task_plan_rec_001")
        te._run_executor(
            runner=_fake_runner,
            task=task,
            task_file=tmp_path / "task.json",
            task_workspace=tmp_path,
            task_id="task_plan_rec_001",
            review_context=None,
            is_review=False,
        )

        assert len(state["plan_outcome_calls"]) == 1, (
            "record_plan_outcome must be called once after planning_phase_completed"
        )
        # wall_clock_ms must be a positive number
        assert state["plan_outcome_calls"][0] > 0

    def test_recall_plan_lessons_called_for_planning_prompt(
        self, monkeypatch, tmp_path: Path
    ):
        """recall_plan_lessons must be called when building the planning prompt."""
        state = _patch_run_executor_planning(monkeypatch, tmp_path=tmp_path)
        plan_recall_calls = []

        def fake_recall(task, limit=3):
            plan_recall_calls.append(task.get("id"))
            return ["[plan] Prior migration -> 4 children. titles: Step 1; Step 2"]

        monkeypatch.setattr(te, "recall_plan_lessons", fake_recall)

        task = _large_task(task_id="task_plan_recall_001")
        te._run_executor(
            runner=_fake_runner,
            task=task,
            task_file=tmp_path / "task.json",
            task_workspace=tmp_path,
            task_id="task_plan_recall_001",
            review_context=None,
            is_review=False,
        )

        assert len(plan_recall_calls) >= 1, (
            "recall_plan_lessons must be called when building the planning prompt"
        )

    def test_plan_lesson_injected_into_planning_prompt(
        self, monkeypatch, tmp_path: Path
    ):
        """Recalled plan lessons must appear in the planning prompt text."""
        state = _patch_run_executor_planning(monkeypatch, tmp_path=tmp_path)

        plan_lesson = "[plan] Database schema migration -> 4 children. ordering: leaves before cores"
        monkeypatch.setattr(te, "recall_plan_lessons", lambda task, limit=3: [plan_lesson])

        task = _large_task(task_id="task_plan_inject_001")
        te._run_executor(
            runner=_fake_runner,
            task=task,
            task_file=tmp_path / "task.json",
            task_workspace=tmp_path,
            task_id="task_plan_inject_001",
            review_context=None,
            is_review=False,
        )

        assert len(state["prompts"]) == 1
        assert plan_lesson in state["prompts"][0], (
            "The recalled plan lesson must appear verbatim in the planning prompt"
        )

    def test_record_plan_outcome_not_called_for_normal_task(
        self, monkeypatch, tmp_path: Path
    ):
        """record_plan_outcome must NOT be called for non-planning (small) tasks."""
        state = _patch_run_executor_planning(monkeypatch, tmp_path=tmp_path)

        # Override _invoke_agent to NOT write plan manifest (normal task)
        def fake_invoke_no_manifest(runner, prompt, workspace, audit_id, opts):
            state["prompts"].append(prompt)
            return _FakeResult(0)

        monkeypatch.setattr(te, "_invoke_agent", fake_invoke_no_manifest)

        task = {
            "id": "task_small_nop",
            "title": "Fix a small typo",
            "description": "one line fix",
            "project": "mac",
            "attempt_count": 1,
            "metadata": {"scope_estimate": {"size": "small"}},
        }
        te._run_executor(
            runner=_fake_runner,
            task=task,
            task_file=tmp_path / "task.json",
            task_workspace=tmp_path,
            task_id="task_small_nop",
            review_context=None,
            is_review=False,
        )

        assert state["plan_outcome_calls"] == [], (
            "record_plan_outcome must NOT be called for non-planning tasks"
        )

    def test_record_plan_outcome_not_called_when_no_plan_manifest(
        self, monkeypatch, tmp_path: Path
    ):
        """If the agent writes no plan manifest, record_plan_outcome is not called."""
        state = _patch_run_executor_planning(monkeypatch, tmp_path=tmp_path)

        # Override _invoke_agent to NOT write plan manifest
        def fake_invoke_no_manifest(runner, prompt, workspace, audit_id, opts):
            state["prompts"].append(prompt)
            return _FakeResult(0)

        monkeypatch.setattr(te, "_invoke_agent", fake_invoke_no_manifest)

        task = _large_task(task_id="task_plan_nomanifest")
        te._run_executor(
            runner=_fake_runner,
            task=task,
            task_file=tmp_path / "task.json",
            task_workspace=tmp_path,
            task_id="task_plan_nomanifest",
            review_context=None,
            is_review=False,
        )

        assert state["plan_outcome_calls"] == [], (
            "record_plan_outcome must NOT be called when plan_decomposed evidence is absent"
        )


# ---------------------------------------------------------------------------
# End-to-end: plan lesson recorded -> recalled in next planning prompt
# ---------------------------------------------------------------------------


def test_plan_lesson_recorded_and_recalled_e2e(tmp_path: Path, monkeypatch):
    """Contract test: plan lesson recorded by first run -> recalled into a
    subsequent planning prompt for a similar task.

    This is the core plan-learn-01 contract: the second big migration on a
    project starts from the first one's shape.
    """
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    cp.create_project("mac")

    # Step 1: Write a plan_learning record for a prior "schema migration" task
    prior_task = _task(
        title="Migrate postgres schema v1",
        task_id="task_prior_migration",
        project="mac",
    )
    # create_task so add_memory can find it
    created = cp.create_task(prior_task["title"], project="mac")
    prior_task_id = created.id

    prior_manifest = _plan_manifest(children_count=4)
    prior_manifest["ordering_rationale"] = "migrate models first, then views, then indexes"
    prior_manifest["children"][0]["title"] = "Migrate user table schema"
    prior_manifest["children"][1]["title"] = "Migrate product table schema"
    prior_manifest["children"][2]["title"] = "Update view definitions"
    prior_manifest["children"][3]["title"] = "Rebuild indexes"

    prior_rec = build_plan_learning_record(prior_task, prior_manifest, wall_clock_ms=60000.0)
    cp.add_memory(
        prior_task_id,
        prior_rec["subject_type"],
        prior_rec["subject_id"],
        prior_rec["record_type"],
        prior_rec["content"],
        None,
        prior_rec["created_by"],
    )

    # Step 2: Recall plan lessons for a similar "schema migration" task
    subsequent_task = _task(
        title="Migrate postgres schema v2",
        task_id="task_subsequent_migration",
        project="mac",
    )

    # Stub the hub_get to use the in-memory ControlPlane
    def fake_hub_get(path):
        from urllib.parse import urlparse, parse_qs
        # Parse the query string from path like /memory?subject_type=project&...
        parts = urlparse("http://hub" + path)
        qs = parse_qs(parts.query)
        content_contains = (qs.get("content_contains") or [None])[0]
        records = cp.search_memory(
            subject_type=qs.get("subject_type", [None])[0],
            subject_id=qs.get("subject_id", [None])[0],
            record_type=(qs.get("record_type") or [None])[0],
            content_contains=content_contains,
        )
        return [
            {
                "content": r.content,
                "record_type": r.record_type,
                "task_id": r.task_id,
            }
            for r in records
        ]

    monkeypatch.setattr(memory, "_hub_get", fake_hub_get)

    lessons = recall_plan_lessons(subsequent_task, limit=5)

    # Must have recalled at least one lesson
    assert len(lessons) >= 1, (
        "recall_plan_lessons must find the prior plan record for a similar task"
    )
    # The lesson must reference the prior task title
    combined = " ".join(lessons)
    assert "Migrate postgres schema v1" in combined, (
        "The recalled lesson must contain the prior task title"
    )

    # Step 3: Verify the lesson appears in the planning prompt
    prompt = te.build_planning_prompt(subsequent_task, tmp_path / "task.json", lessons=lessons)
    assert "Migrate postgres schema v1" in prompt, (
        "Recalled plan lesson must appear in the planning prompt for the subsequent task"
    )
