"""Tests for scope-estimate preflight (scope-01).

Covers:
- compute_scope_estimate: pure deterministic sizing logic
- needs_scope_estimate: guard logic (attempt_count, existing estimate)
- record_scope_estimate / _hub_put: hub PUT seam
- maybe_preflight_scope_estimate: integration of the above
- _run_executor integration: emits telemetry and calls preflight on attempt 1

The hub HTTP seam (_hub_put, _hub_post) and agent runner are injected so
nothing here spawns Hermes or hits a network.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from mac import executor_scope as scope
from mac import task_executor as te


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _task(
    *,
    title: str = "Fix a small bug",
    description: str = "One-line description.",
    attempt_count: int = 1,
    metadata: Optional[Dict[str, Any]] = None,
    task_id: str = "task_test123",
) -> Dict[str, Any]:
    """Build a minimal task fixture."""
    t: Dict[str, Any] = {
        "id": task_id,
        "title": title,
        "description": description,
        "attempt_count": attempt_count,
        "project": "mac",
    }
    if metadata is not None:
        t["metadata"] = metadata
    return t


def _repo_task(
    *,
    title: str = "Fix a bug",
    description: str = "Short fix.",
    attempt_count: int = 1,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a task with a repository contract in metadata."""
    metadata: Dict[str, Any] = {
        "execution_contract": {
            "repository_contract": {
                "toolchain": {
                    "required_commands": ["python3", "git", "gh"]
                }
            }
        }
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return _task(title=title, description=description, attempt_count=attempt_count, metadata=metadata)


# ---------------------------------------------------------------------------
# compute_scope_estimate — pure / deterministic
# ---------------------------------------------------------------------------


def test_compute_scope_small_task():
    task = _task(title="Fix a small bug", description="One sentence fix.")
    result = te.compute_scope_estimate(task)
    assert result["schema"] == "mac.scope_estimate.v1"
    assert result["size"] == "small"
    assert result["estimated_units"] == 1
    assert isinstance(result["signals"], list)
    assert isinstance(result["rationale"], str)


def test_compute_scope_large_desc_words():
    # 250-word description → desc_words signal
    long_desc = " ".join(["word"] * 250)
    task = _task(description=long_desc)
    result = te.compute_scope_estimate(task)
    assert any("desc_words" in s for s in result["signals"])


def test_compute_scope_large_desc_chars():
    # 900-char description → desc_chars signal
    long_desc = "x" * 900
    task = _task(description=long_desc)
    result = te.compute_scope_estimate(task)
    assert any("desc_chars" in s for s in result["signals"])


def test_compute_scope_repo_required_cmds():
    # 3 required commands → repo_required_cmds signal
    task = _repo_task()
    result = te.compute_scope_estimate(task)
    assert any("repo_required_cmds" in s for s in result["signals"])


def test_compute_scope_large_two_signals():
    # Two signals → "large"
    long_desc = " ".join(["word"] * 250)  # desc_words signal
    task = _repo_task(description=long_desc)  # also repo_required_cmds signal
    result = te.compute_scope_estimate(task)
    assert result["size"] == "large"
    assert result["estimated_units"] == 2


def test_compute_scope_plan_detected():
    # Plan-detection signals are captured
    task = _task(
        title="Implement authentication and add user management",
        description="1. Add login\n2. Add register\n3. Add profile\n4. Add sessions\n5. Add tokens",
    )
    result = te.compute_scope_estimate(task)
    assert any("plan" in s for s in result["signals"])


def test_compute_scope_long_title():
    # Title longer than 100 chars → long_title signal
    task = _task(title="A" * 101)
    result = te.compute_scope_estimate(task)
    assert any("long_title" in s for s in result["signals"])


def test_compute_scope_none_metadata():
    # None metadata should not raise
    task = _task(metadata=None)
    result = te.compute_scope_estimate(task)
    assert result["schema"] == "mac.scope_estimate.v1"
    assert result["size"] in {"small", "large"}


def test_compute_scope_missing_fields():
    # Minimal task with no title/description
    result = te.compute_scope_estimate({})
    assert result["size"] == "small"
    assert result["estimated_units"] == 1


def test_compute_scope_origin_contract():
    # origin.repository_contract path also recognized
    task = _task(
        metadata={
            "origin": {
                "repository_contract": {
                    "toolchain": {
                        "required_commands": ["python3", "git", "gh"]
                    }
                }
            }
        }
    )
    result = te.compute_scope_estimate(task)
    assert any("repo_required_cmds" in s for s in result["signals"])


# ---------------------------------------------------------------------------
# needs_scope_estimate — guard logic
# ---------------------------------------------------------------------------


def test_needs_estimate_first_attempt_no_metadata():
    task = _task(attempt_count=1)
    # no metadata key → should need estimate
    assert te.needs_scope_estimate(task) is True


def test_needs_estimate_none_metadata():
    task = _task(attempt_count=1, metadata=None)
    assert te.needs_scope_estimate(task) is True


def test_needs_estimate_empty_metadata():
    task = _task(attempt_count=1, metadata={})
    assert te.needs_scope_estimate(task) is True


def test_needs_estimate_already_has_estimate():
    task = _task(attempt_count=1, metadata={"scope_estimate": {"size": "small"}})
    assert te.needs_scope_estimate(task) is False


def test_needs_estimate_attempt_2():
    task = _task(attempt_count=2, metadata={})
    assert te.needs_scope_estimate(task) is False


def test_needs_estimate_attempt_0():
    task = _task(attempt_count=0, metadata={})
    assert te.needs_scope_estimate(task) is False


def test_needs_estimate_non_dict_task():
    assert te.needs_scope_estimate(None) is False  # type: ignore[arg-type]
    assert te.needs_scope_estimate("string") is False  # type: ignore[arg-type]


def test_needs_estimate_missing_attempt_count():
    # No attempt_count key → treated as 0
    task = {"id": "t1", "title": "x", "metadata": {}}
    assert te.needs_scope_estimate(task) is False


# ---------------------------------------------------------------------------
# _hub_put — HTTP seam
# ---------------------------------------------------------------------------


def test_hub_put_noop_without_env(monkeypatch):
    monkeypatch.delenv("MAC_HUB_URL", raising=False)
    monkeypatch.delenv("MAC_URL", raising=False)
    monkeypatch.delenv("MAC_WORKER_TOKEN", raising=False)
    monkeypatch.delenv("MAC_TOKEN", raising=False)
    monkeypatch.delenv("MAC_API_TOKEN", raising=False)
    result = te._hub_put("/tasks/t1", {"metadata": {"scope_estimate": {"size": "small"}}})
    assert result is False


def test_hub_put_sends_put_method(monkeypatch):
    calls: List[Dict] = []

    def fake_urlopen(req, timeout):
        calls.append({"method": req.method, "url": req.full_url, "data": req.data})

        class FakeResp:
            def read(self):
                return b""
        return FakeResp()

    monkeypatch.setenv("MAC_HUB_URL", "http://localhost:9999")
    monkeypatch.setenv("MAC_WORKER_TOKEN", "tok123")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = te._hub_put("/tasks/task_xyz", {"metadata": {"scope_estimate": {"size": "small"}}})
    assert result is True
    assert len(calls) == 1
    assert calls[0]["method"] == "PUT"
    assert "/tasks/task_xyz" in calls[0]["url"]
    body = json.loads(calls[0]["data"].decode())
    assert body["metadata"]["scope_estimate"]["size"] == "small"


# ---------------------------------------------------------------------------
# record_scope_estimate
# ---------------------------------------------------------------------------


def test_record_scope_estimate_merges_metadata(monkeypatch):
    puts: List[Dict] = []
    monkeypatch.setattr(scope, "_hub_put", lambda path, payload, **kw: puts.append({"path": path, "payload": payload}) or True)

    estimate = {"schema": "mac.scope_estimate.v1", "size": "small", "estimated_units": 1, "signals": [], "rationale": "small"}
    existing = {"execution_contract": {"evidence_type": "repo_change"}}
    result = te.record_scope_estimate("task_abc", estimate, existing)

    assert result is True
    assert len(puts) == 1
    assert puts[0]["path"] == "/tasks/task_abc"
    merged = puts[0]["payload"]["metadata"]
    # scope_estimate was added
    assert merged["scope_estimate"]["size"] == "small"
    # existing keys preserved
    assert "execution_contract" in merged


def test_record_scope_estimate_no_task_id(monkeypatch):
    puts: List[Dict] = []
    monkeypatch.setattr(scope, "_hub_put", lambda path, payload, **kw: puts.append(1) or True)
    result = te.record_scope_estimate("", {"size": "small"})
    assert result is False
    assert not puts


def test_record_scope_estimate_none_existing(monkeypatch):
    puts: List[Dict] = []
    monkeypatch.setattr(scope, "_hub_put", lambda path, payload, **kw: puts.append({"payload": payload}) or True)
    estimate = {"schema": "mac.scope_estimate.v1", "size": "large", "estimated_units": 2, "signals": [], "rationale": "x"}
    te.record_scope_estimate("t1", estimate, None)
    merged = puts[0]["payload"]["metadata"]
    assert merged["scope_estimate"]["size"] == "large"


# ---------------------------------------------------------------------------
# maybe_preflight_scope_estimate — integration
# ---------------------------------------------------------------------------


def test_maybe_preflight_returns_estimate_on_first_attempt(monkeypatch):
    monkeypatch.setattr(scope, "record_scope_estimate", lambda *a, **kw: True)
    task = _task(attempt_count=1)
    result = te.maybe_preflight_scope_estimate(task)
    assert result is not None
    assert "size" in result
    assert result["schema"] == "mac.scope_estimate.v1"


def test_maybe_preflight_returns_none_on_second_attempt(monkeypatch):
    monkeypatch.setattr(scope, "record_scope_estimate", lambda *a, **kw: True)
    task = _task(attempt_count=2)
    result = te.maybe_preflight_scope_estimate(task)
    assert result is None


def test_maybe_preflight_returns_none_when_already_estimated(monkeypatch):
    monkeypatch.setattr(scope, "record_scope_estimate", lambda *a, **kw: True)
    task = _task(attempt_count=1, metadata={"scope_estimate": {"size": "small"}})
    result = te.maybe_preflight_scope_estimate(task)
    assert result is None


def test_maybe_preflight_calls_record(monkeypatch):
    recorded: List[Dict] = []
    monkeypatch.setattr(
        scope,
        "record_scope_estimate",
        lambda task_id, estimate, existing=None: recorded.append(
            {"task_id": task_id, "estimate": estimate, "existing": existing}
        ) or True,
    )
    task = _task(attempt_count=1, metadata={"some_key": "some_val"}, task_id="task_preflight")
    te.maybe_preflight_scope_estimate(task)
    assert len(recorded) == 1
    assert recorded[0]["task_id"] == "task_preflight"
    assert recorded[0]["estimate"]["schema"] == "mac.scope_estimate.v1"
    assert recorded[0]["existing"]["some_key"] == "some_val"


# ---------------------------------------------------------------------------
# _run_executor integration — scope estimate is called and emits telemetry
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_runner(argv, cwd, task_id, metadata):
    return _FakeResult(0)


def test_run_executor_calls_scope_estimate_on_attempt_1(monkeypatch, tmp_path):
    """Scope estimate preflight fires on attempt_count=1 and emits telemetry."""
    scope_calls: List[Dict] = []
    telemetry_events: List[str] = []

    monkeypatch.setattr(
        te,
        "maybe_preflight_scope_estimate",
        lambda task: scope_calls.append(task)
        or {"schema": "mac.scope_estimate.v1", "size": "small", "estimated_units": 1, "signals": [], "rationale": "x"},
    )
    monkeypatch.setattr(
        te,
        "emit_telemetry",
        lambda event, **kw: telemetry_events.append(event) or True,
    )
    monkeypatch.setattr(te, "recall_deployment_lessons", lambda task: [])
    monkeypatch.setattr(te, "build_task_prompt", lambda *a, **kw: "prompt")
    monkeypatch.setattr(te, "_openshell_enabled", lambda: False)
    monkeypatch.setattr(te, "_invoke_agent", lambda *a, **kw: _FakeResult(0))
    monkeypatch.setattr(te, "run_deterministic_git_finalizer", lambda *a, **kw: None)
    monkeypatch.setattr(te, "maybe_auto_decompose", lambda *a, **kw: False)
    monkeypatch.setattr(te, "write_fallback_evidence_manifest", lambda *a, **kw: None)
    monkeypatch.setattr(te, "classify_outcome", lambda *a, **kw: {"outcome": "success", "evidence_type": "operator_result", "signals": []})
    monkeypatch.setattr(te, "record_deployment_learning", lambda *a, **kw: True)
    monkeypatch.setattr(te, "record_curated_lessons", lambda *a, **kw: 0)

    task = _task(attempt_count=1)
    te._run_executor(
        runner=_fake_runner,
        task=task,
        task_file=tmp_path / "task.json",
        task_workspace=tmp_path,
        task_id="task_test123",
        review_context=None,
        is_review=False,
    )

    assert len(scope_calls) == 1
    assert "scope_estimated" in telemetry_events


def test_run_executor_skips_scope_estimate_for_reviews(monkeypatch, tmp_path):
    """Scope estimate preflight must not fire for review tasks."""
    scope_calls: List[Dict] = []

    monkeypatch.setattr(
        te,
        "maybe_preflight_scope_estimate",
        lambda task: scope_calls.append(task),
    )
    monkeypatch.setattr(te, "build_review_prompt", lambda *a, **kw: "review prompt")
    monkeypatch.setattr(te, "emit_telemetry", lambda *a, **kw: True)
    monkeypatch.setattr(te, "_openshell_enabled", lambda: False)
    monkeypatch.setattr(te, "_review_experiment_assignment", lambda task: {})
    monkeypatch.setattr(te, "_invoke_agent", lambda *a, **kw: _FakeResult(0))
    monkeypatch.setattr(te, "run_deterministic_review_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(te, "write_fallback_evidence_manifest", lambda *a, **kw: None)

    task = _task(attempt_count=1)
    te._run_executor(
        runner=_fake_runner,
        task=task,
        task_file=tmp_path / "task.json",
        task_workspace=tmp_path,
        task_id="task_test123",
        review_context={"task_id": "task_original"},
        is_review=True,
    )

    assert scope_calls == [], "scope estimate must not run for reviews"


# ---------------------------------------------------------------------------
# _compute_scope_signals — pure inner function
# ---------------------------------------------------------------------------


def _make_plan_learning_record(task_id: str = "task_prior_001") -> Dict[str, Any]:
    """Build a minimal raw hub record dict containing a mac.plan_learning.v1 blob."""
    content = {
        "schema": "mac.plan_learning.v1",
        "task_id": task_id,
        "task_title": "Prior decomposed task",
        "project": "mac",
        "evidence_type": "plan_decomposed",
        "children_count": 3,
        "children_titles": ["Step 1", "Step 2", "Step 3"],
        "ordering_rationale": "leaves before cores",
        "coverage_claim": "full",
        "wall_clock_ms": 30000,
        "at": "2026-01-01T00:00:00Z",
    }
    return {"content": json.dumps(content), "record_type": "deployment_learning:mac"}


def test_compute_scope_signals_no_prior_lessons():
    """With no prior lessons, signals match the pure-textual path."""
    result = te._compute_scope_signals("Fix a bug", "Short fix.", {}, [])
    assert isinstance(result, list)
    # no memory signal
    assert not any("memory" in s for s in result)


def test_compute_scope_signals_memory_signal_appended():
    """One plan_learning.v1 record → one memory:prior_decomposition signal."""
    record = _make_plan_learning_record("task_prior_abc")
    signals = te._compute_scope_signals("Fix a bug", "Short fix.", {}, [record])
    memory_signals = [s for s in signals if s.startswith("memory:prior_decomposition:")]
    assert len(memory_signals) == 1
    assert "task_prior_abc" in memory_signals[0]


def test_compute_scope_signals_memory_signal_is_auditable():
    """The memory signal encodes the prior task_id for auditability."""
    task_id = "task_auditable_001"
    record = _make_plan_learning_record(task_id)
    signals = te._compute_scope_signals("x", "y", {}, [record])
    assert any(task_id in s for s in signals)


def test_compute_scope_signals_multiple_records_multiple_signals():
    """Two plan_learning records → two separate memory signals."""
    records = [
        _make_plan_learning_record("task_a"),
        _make_plan_learning_record("task_b"),
    ]
    signals = te._compute_scope_signals("x", "y", {}, records)
    memory_signals = [s for s in signals if s.startswith("memory:prior_decomposition:")]
    assert len(memory_signals) == 2
    assert any("task_a" in s for s in memory_signals)
    assert any("task_b" in s for s in memory_signals)


def test_compute_scope_signals_non_plan_record_ignored():
    """Records with wrong schema are not converted to memory signals."""
    content = json.dumps({"schema": "mac.deployment_learning.v1", "outcome": "success"})
    record = {"content": content}
    signals = te._compute_scope_signals("x", "y", {}, [record])
    assert not any("memory" in s for s in signals)


def test_compute_scope_signals_invalid_json_record_skipped():
    """Records with invalid JSON content are silently skipped."""
    record = {"content": "not-valid-json"}
    signals = te._compute_scope_signals("x", "y", {}, [record])
    assert not any("memory" in s for s in signals)


def test_compute_scope_signals_non_dict_record_skipped():
    """Non-dict entries in prior_lessons list are silently skipped."""
    signals = te._compute_scope_signals("x", "y", {}, ["bad", None, 42])
    assert not any("memory" in s for s in signals)


def test_compute_scope_signals_preserves_d1_to_d5():
    """D1-D5 signals still appear when prior_lessons is empty."""
    long_desc = " ".join(["word"] * 250)  # D1 trigger
    signals = te._compute_scope_signals("x", long_desc, {}, [])
    assert any("desc_words" in s for s in signals)


# ---------------------------------------------------------------------------
# recall_scope_lessons — hub seam
# ---------------------------------------------------------------------------


def test_recall_scope_lessons_returns_empty_no_hub(monkeypatch):
    """No hub env → returns empty list, never raises."""
    monkeypatch.delenv("MAC_HUB_URL", raising=False)
    monkeypatch.delenv("MAC_URL", raising=False)
    monkeypatch.delenv("MAC_WORKER_TOKEN", raising=False)
    monkeypatch.delenv("MAC_TOKEN", raising=False)
    monkeypatch.delenv("MAC_API_TOKEN", raising=False)
    task = _task(title="Migrate schema")
    result = te.recall_scope_lessons(task)
    assert result == []


def test_recall_scope_lessons_returns_empty_hub_unreachable(monkeypatch):
    """Hub returns None (unreachable) → empty list."""
    monkeypatch.setattr(scope, "_hub_get", lambda path: None)
    result = te.recall_scope_lessons(_task(title="Migrate schema"))
    assert result == []


def test_recall_scope_lessons_returns_empty_no_records(monkeypatch):
    """Hub returns empty list → empty list."""
    monkeypatch.setattr(scope, "_hub_get", lambda path: [])
    result = te.recall_scope_lessons(_task(title="Migrate schema"))
    assert result == []


def test_recall_scope_lessons_filters_non_plan_records(monkeypatch):
    """Non-plan records in hub response are filtered out."""
    bad_content = json.dumps({"schema": "mac.deployment_learning.v1"})
    monkeypatch.setattr(scope, "_hub_get", lambda path: [{"content": bad_content}])
    result = te.recall_scope_lessons(_task())
    assert result == []


def test_recall_scope_lessons_returns_plan_records(monkeypatch):
    """Valid plan_learning.v1 records are returned."""
    record = _make_plan_learning_record("task_plan_001")
    monkeypatch.setattr(scope, "_hub_get", lambda path: [record])
    result = te.recall_scope_lessons(_task(title="Migrate schema"))
    assert len(result) == 1
    data = json.loads(result[0]["content"])
    assert data["schema"] == "mac.plan_learning.v1"


def test_recall_scope_lessons_deduplicates_by_task_id(monkeypatch):
    """Records with identical task_id are deduplicated."""
    record = _make_plan_learning_record("task_dup")
    monkeypatch.setattr(scope, "_hub_get", lambda path: [record, record])
    result = te.recall_scope_lessons(_task())
    assert len(result) == 1


def test_recall_scope_lessons_respects_limit(monkeypatch):
    """limit= parameter caps the number of returned records."""
    records = [_make_plan_learning_record("task_%d" % i) for i in range(10)]
    monkeypatch.setattr(scope, "_hub_get", lambda path: records)
    result = te.recall_scope_lessons(_task(), limit=3)
    assert len(result) <= 3


# ---------------------------------------------------------------------------
# compute_scope_estimate — two-layer integration (new contracts)
# ---------------------------------------------------------------------------


def test_compute_scope_estimate_no_regression_hub_unreachable(monkeypatch):
    """Hub unreachable → output identical to pure-textual estimate."""
    monkeypatch.setattr(scope, "recall_scope_lessons", lambda task, **kw: [])
    task = _task(description=" ".join(["word"] * 250))
    result = te.compute_scope_estimate(task)
    assert result["schema"] == "mac.scope_estimate.v1"
    assert "desc_words" in " ".join(result["signals"])
    assert not any("memory" in s for s in result["signals"])


def test_compute_scope_estimate_no_regression_raises(monkeypatch):
    """recall_scope_lessons raising → silently falls back to textual-only."""
    def boom(task, **kw):
        raise RuntimeError("hub exploded")
    monkeypatch.setattr(scope, "recall_scope_lessons", boom)
    task = _task(title="Fix a small bug", description="Short fix.")
    result = te.compute_scope_estimate(task)
    assert result["schema"] == "mac.scope_estimate.v1"
    assert result["size"] in {"small", "large"}


def test_compute_scope_estimate_memory_signal_appears(monkeypatch):
    """One prior decomposition lesson → memory:prior_decomposition in signals."""
    record = _make_plan_learning_record("task_prior_42")
    monkeypatch.setattr(scope, "recall_scope_lessons", lambda task, **kw: [record])
    task = _task(title="Fix a small bug", description="Short fix.")
    result = te.compute_scope_estimate(task)
    assert any("memory:prior_decomposition" in s for s in result["signals"])
    assert any("task_prior_42" in s for s in result["signals"])


def test_compute_scope_estimate_memory_signal_flips_size(monkeypatch):
    """One textual large-signal + one memory hit → size='large'."""
    record = _make_plan_learning_record("task_flip")
    monkeypatch.setattr(scope, "recall_scope_lessons", lambda task, **kw: [record])
    # _repo_task gives one textual large-signal (repo_required_cmds)
    task = _repo_task()
    result = te.compute_scope_estimate(task)
    assert result["size"] == "large", (
        "One textual signal + one memory hit must produce size='large'; got: %r" % result
    )
    assert result["estimated_units"] == 2


def test_compute_scope_estimate_calls_recall_scope_lessons(monkeypatch):
    """compute_scope_estimate must call recall_scope_lessons with the task."""
    calls: List[Dict] = []

    def fake_recall(task, **kw):
        calls.append(task)
        return []

    monkeypatch.setattr(scope, "recall_scope_lessons", fake_recall)
    task = _task()
    te.compute_scope_estimate(task)
    assert len(calls) == 1
    assert calls[0] is task


def test_compute_scope_estimate_public_signature_unchanged():
    """Public signature: (task: Dict) -> Dict — unchanged after refactor."""
    import inspect
    sig = inspect.signature(te.compute_scope_estimate)
    params = list(sig.parameters.keys())
    assert params == ["task"], "Public signature must remain (task,); got: %r" % params


# ---------------------------------------------------------------------------
# Branch coverage for _compute_scope_signals edge cases
# ---------------------------------------------------------------------------


def test_compute_scope_signals_json_decoded_non_dict_skipped():
    """Content that decodes to a non-dict (e.g. JSON list) is silently skipped."""
    record = {"content": json.dumps(["list", "not", "dict"])}
    signals = te._compute_scope_signals("x", "y", {}, [record])
    assert not any("memory" in s for s in signals)


def test_compute_scope_signals_json_decoded_string_skipped():
    """Content that decodes to a string (not a dict) is silently skipped."""
    record = {"content": json.dumps("just a string")}
    signals = te._compute_scope_signals("x", "y", {}, [record])
    assert not any("memory" in s for s in signals)


# ---------------------------------------------------------------------------
# Branch coverage for recall_scope_lessons edge cases
# ---------------------------------------------------------------------------


def test_recall_scope_lessons_non_dict_hub_record_skipped(monkeypatch):
    """Non-dict entries in the hub response list are silently skipped."""
    monkeypatch.setattr(scope, "_hub_get", lambda path: ["not-a-dict", 42, None])
    result = te.recall_scope_lessons(_task())
    assert result == []


def test_recall_scope_lessons_invalid_json_in_hub_record_skipped(monkeypatch):
    """Records whose content field is invalid JSON are silently skipped."""
    monkeypatch.setattr(scope, "_hub_get", lambda path: [{"content": "bad-json!!"}])
    result = te.recall_scope_lessons(_task())
    assert result == []


def test_recall_scope_lessons_break_on_limit_across_terms(monkeypatch):
    """When limit is reached mid-search across multiple family terms, search stops."""
    call_count = [0]

    def fake_hub_get(path):
        call_count[0] += 1
        # Return two distinct plan records each time
        return [
            _make_plan_learning_record("task_x%d" % call_count[0]),
            _make_plan_learning_record("task_y%d" % call_count[0]),
        ]

    monkeypatch.setattr(scope, "_hub_get", fake_hub_get)
    # Use a task with multiple family terms so the loop would iterate multiple times
    task = _task(title="schema migrate deploy pipeline workflow")
    result = te.recall_scope_lessons(task, limit=2)
    # Must stop at limit
    assert len(result) <= 2


# ---------------------------------------------------------------------------
# recall_scope_lessons — id and rendered return shape (task spec)
# ---------------------------------------------------------------------------


def test_recall_scope_lessons_returns_id_field(monkeypatch):
    """Each returned dict has an 'id' field equal to the task_id from content."""
    record = _make_plan_learning_record("task_id_check_001")
    monkeypatch.setattr(scope, "_hub_get", lambda path: [record])
    result = te.recall_scope_lessons(_task(title="Migrate schema"))
    assert len(result) == 1
    assert result[0]["id"] == "task_id_check_001"


def test_recall_scope_lessons_returns_rendered_field(monkeypatch):
    """Each returned dict has a non-empty 'rendered' string field."""
    record = _make_plan_learning_record("task_rendered_001")
    monkeypatch.setattr(scope, "_hub_get", lambda path: [record])
    result = te.recall_scope_lessons(_task(title="Migrate schema"))
    assert len(result) == 1
    assert isinstance(result[0]["rendered"], str)
    assert result[0]["rendered"]  # non-empty


def test_recall_scope_lessons_rendered_contains_plan_info(monkeypatch):
    """The 'rendered' field summarises the plan_learning record content."""
    record = _make_plan_learning_record("task_rendered_info_001")
    monkeypatch.setattr(scope, "_hub_get", lambda path: [record])
    result = te.recall_scope_lessons(_task(title="Migrate schema"))
    assert len(result) == 1
    # _format_plan_learning_content produces '[plan] ...' prefix
    assert result[0]["rendered"].startswith("[plan]")


def test_recall_scope_lessons_preserves_content_field(monkeypatch):
    """The original 'content' field is preserved alongside 'id' and 'rendered'."""
    record = _make_plan_learning_record("task_content_compat_001")
    monkeypatch.setattr(scope, "_hub_get", lambda path: [record])
    result = te.recall_scope_lessons(_task(title="Migrate schema"))
    assert len(result) == 1
    # Backward compat: callers that read 'content' still work
    data = json.loads(result[0]["content"])
    assert data["schema"] == "mac.plan_learning.v1"


def test_recall_scope_lessons_id_empty_when_task_id_absent(monkeypatch):
    """'id' is '' when the content record has no task_id key."""
    content = json.dumps({"schema": "mac.plan_learning.v1"})
    record = {"content": content, "record_type": "deployment_learning:mac"}
    monkeypatch.setattr(scope, "_hub_get", lambda path: [record])
    result = te.recall_scope_lessons(_task())
    # task_id is None/missing → id should be ''
    assert len(result) == 1
    assert result[0]["id"] == ""


def test_recall_scope_lessons_no_side_effects_no_scope_estimate_call(monkeypatch):
    """recall_scope_lessons must not call compute_scope_estimate."""
    calls: List = []
    monkeypatch.setattr(te, "compute_scope_estimate", lambda t: calls.append(t) or {})
    record = _make_plan_learning_record("task_side_effect_001")
    monkeypatch.setattr(scope, "_hub_get", lambda path: [record])
    te.recall_scope_lessons(_task(title="Migrate schema"))
    assert calls == [], "recall_scope_lessons must not call compute_scope_estimate"
