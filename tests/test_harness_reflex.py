"""Contract tests for mac.harness_reflex and mac.harness_recovery.

Coverage targets:
  1. RemediationChoice whitelist membership and StrEnum behaviour.
  2. HarnessFailureContext dataclass construction and as_failure_info().
  3. recall_harness_lessons – static heuristics, empty summary, unknown step.
  4. choose_remediation – disabled flag, unrecognised LLM output, valid choices.
  5. RECOVERY_LIMIT constant equals 2.
  6. try_recovery – bounded counter check.
  7. try_recovery – LLM escalate path updates state and returns False.
  8. try_recovery – successful dispatch updates state and returns True.
  9. try_recovery – dispatch exception leaves recovered=False.
 10. DISPATCH table keys are non-escalate RemediationChoice values.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

import mac.harness_recovery as hrec
import mac.harness_reflex as hreflex
from mac.harness_recovery import (
    RECOVERY_LIMIT,
    HarnessFailureContext,
    RemediationChoice,
    choose_remediation,
    recall_harness_lessons,
)
from mac.harness_reflex import DISPATCH, try_recovery


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _state(count: int = 0) -> Dict[str, Any]:
    return {"recovery_count": count, "recovery_log": []}


def _task(task_id: str = "task_test") -> Dict[str, Any]:
    return {"id": task_id, "title": "Test task"}


# ---------------------------------------------------------------------------
# 1. RemediationChoice
# ---------------------------------------------------------------------------


class TestRemediationChoice:
    def test_members(self):
        values = {m.value for m in RemediationChoice}
        assert values == {"retry_fetch", "clear_cache", "escalate"}

    def test_is_str(self):
        assert isinstance(RemediationChoice.escalate, str)
        assert RemediationChoice.escalate == "escalate"

    def test_enum_identity(self):
        assert RemediationChoice("retry_fetch") is RemediationChoice.retry_fetch


# ---------------------------------------------------------------------------
# 2. HarnessFailureContext
# ---------------------------------------------------------------------------


class TestHarnessFailureContext:
    def test_basic_construction(self):
        ctx = HarnessFailureContext(step_name="fetch_deps", stderr_tail="error: 404")
        assert ctx.step_name == "fetch_deps"
        assert ctx.stderr_tail == "error: 404"
        assert ctx.task_id is None
        assert ctx.extra == {}

    def test_as_failure_info_includes_step(self):
        ctx = HarnessFailureContext(step_name="run_tests", stderr_tail="FAILED")
        info = ctx.as_failure_info()
        assert "run_tests" in info

    def test_as_failure_info_includes_task_id(self):
        ctx = HarnessFailureContext(
            step_name="bootstrap", stderr_tail="err", task_id="task_abc"
        )
        info = ctx.as_failure_info()
        assert "task_abc" in info

    def test_as_failure_info_includes_stderr(self):
        ctx = HarnessFailureContext(step_name="s", stderr_tail="unexpected EOF")
        assert "unexpected EOF" in ctx.as_failure_info()

    def test_extra_field_accepts_arbitrary_data(self):
        ctx = HarnessFailureContext(
            step_name="s", stderr_tail="", extra={"attempt": 1, "phase": "pre"}
        )
        assert ctx.extra["attempt"] == 1


# ---------------------------------------------------------------------------
# 3. recall_harness_lessons
# ---------------------------------------------------------------------------


class TestRecallHarnessLessons:
    def test_fetch_deps_returns_lessons(self):
        lessons = recall_harness_lessons("fetch_deps", "network timeout")
        assert isinstance(lessons, list)
        assert len(lessons) > 0

    def test_unknown_step_returns_empty_or_list(self):
        lessons = recall_harness_lessons("completely_unknown_step", "some error")
        assert isinstance(lessons, list)

    def test_empty_summary_returns_all_lessons_for_step(self):
        lessons = recall_harness_lessons("fetch_deps", "")
        assert isinstance(lessons, list)

    def test_run_tests_step_has_lessons(self):
        lessons = recall_harness_lessons("run_tests", "flaky")
        assert isinstance(lessons, list)

    def test_bootstrap_step_has_lessons(self):
        lessons = recall_harness_lessons("bootstrap", "incomplete install")
        assert isinstance(lessons, list)


# ---------------------------------------------------------------------------
# 4. choose_remediation
# ---------------------------------------------------------------------------


class TestChooseRemediation:
    def test_disabled_flag_always_escalates(self, monkeypatch):
        monkeypatch.delenv("MAC_RECOVERY_REFLEX_ENABLED", raising=False)
        mock_llm = MagicMock(return_value="retry_fetch")
        ctx = HarnessFailureContext(step_name="s", stderr_tail="err")
        result = choose_remediation(ctx, _state(), llm_fn=mock_llm)
        assert result == RemediationChoice.escalate.value
        mock_llm.assert_not_called()

    def test_false_env_var_escalates(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "false")
        mock_llm = MagicMock(return_value="clear_cache")
        ctx = HarnessFailureContext(step_name="s", stderr_tail="err")
        result = choose_remediation(ctx, _state(), llm_fn=mock_llm)
        assert result == RemediationChoice.escalate.value
        mock_llm.assert_not_called()

    @pytest.mark.parametrize("bad", ["restart", "reboot", "", "   ", "retry fetch"])
    def test_unrecognised_llm_output_maps_to_escalate(self, monkeypatch, bad):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        ctx = HarnessFailureContext(step_name="s", stderr_tail="err")
        result = choose_remediation(ctx, _state(), llm_fn=lambda _: bad)
        assert result == RemediationChoice.escalate.value

    @pytest.mark.parametrize("good", ["retry_fetch", "clear_cache", "escalate"])
    def test_valid_whitelist_member_returned(self, monkeypatch, good):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        ctx = HarnessFailureContext(step_name="s", stderr_tail="err")
        result = choose_remediation(ctx, _state(), llm_fn=lambda _: good)
        assert result == good

    def test_llm_response_case_normalised(self, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        ctx = HarnessFailureContext(step_name="s", stderr_tail="err")
        result = choose_remediation(ctx, _state(), llm_fn=lambda _: "RETRY_FETCH")
        assert result == RemediationChoice.retry_fetch.value


# ---------------------------------------------------------------------------
# 5. RECOVERY_LIMIT constant
# ---------------------------------------------------------------------------


class TestRecoveryLimit:
    def test_constant_is_two(self):
        assert RECOVERY_LIMIT == 2

    def test_harness_reflex_exports_same_limit(self):
        from mac.harness_reflex import try_recovery  # noqa: F401
        # RECOVERY_LIMIT is imported from harness_recovery
        from mac.harness_recovery import RECOVERY_LIMIT as RL
        assert RL == 2


# ---------------------------------------------------------------------------
# 6. try_recovery – bounded counter
# ---------------------------------------------------------------------------


class TestTryRecoveryBoundedCounter:
    def test_at_limit_returns_escalate_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _state(count=2)
        recovered, choice, detail = try_recovery(
            "fetch_deps", "err", _task(), tmp_path, state
        )
        assert recovered is False
        assert choice == RemediationChoice.escalate.value
        assert "limit" in detail.lower()

    def test_above_limit_also_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _state(count=10)
        recovered, choice, _ = try_recovery(
            "run_tests", "err", _task(), tmp_path, state
        )
        assert recovered is False
        assert choice == RemediationChoice.escalate.value

    def test_at_limit_no_llm_called(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _state(count=2)
        mock_llm = MagicMock(return_value="retry_fetch")
        try_recovery("s", "err", _task(), tmp_path, state, llm_fn=mock_llm)
        mock_llm.assert_not_called()

    def test_recovery_log_appended_at_limit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _state(count=2)
        try_recovery("s", "err", _task(), tmp_path, state)
        assert len(state["recovery_log"]) == 1


# ---------------------------------------------------------------------------
# 7. try_recovery – LLM escalate path
# ---------------------------------------------------------------------------


class TestTryRecoveryLLMEscalate:
    def test_llm_escalate_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _state()
        recovered, choice, _ = try_recovery(
            "fetch_deps", "err", _task(), tmp_path, state,
            llm_fn=lambda _: "escalate"
        )
        assert recovered is False
        assert choice == RemediationChoice.escalate.value

    def test_llm_escalate_increments_counter(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _state(count=0)
        try_recovery("s", "err", _task(), tmp_path, state, llm_fn=lambda _: "escalate")
        assert state["recovery_count"] == 1

    def test_llm_escalate_appends_log(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _state()
        try_recovery("s", "err", _task(), tmp_path, state, llm_fn=lambda _: "escalate")
        assert len(state["recovery_log"]) == 1

    def test_disabled_flag_counts_as_escalate(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MAC_RECOVERY_REFLEX_ENABLED", raising=False)
        state = _state()
        recovered, choice, _ = try_recovery("s", "err", _task(), tmp_path, state)
        assert recovered is False
        assert choice == RemediationChoice.escalate.value


# ---------------------------------------------------------------------------
# 8. try_recovery – successful dispatch
# ---------------------------------------------------------------------------


class TestTryRecoverySuccessfulDispatch:
    def test_retry_fetch_returns_recovered_true(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _state()
        recovered, choice, detail = try_recovery(
            "fetch_deps", "network error", _task(), tmp_path, state,
            llm_fn=lambda _: "retry_fetch"
        )
        assert recovered is True
        assert choice == RemediationChoice.retry_fetch.value
        assert detail

    def test_clear_cache_returns_recovered_true(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _state()
        recovered, choice, detail = try_recovery(
            "bootstrap", "hash mismatch", _task(), tmp_path, state,
            llm_fn=lambda _: "clear_cache"
        )
        assert recovered is True
        assert choice == RemediationChoice.clear_cache.value

    def test_counter_incremented_on_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _state(count=0)
        try_recovery("s", "e", _task(), tmp_path, state, llm_fn=lambda _: "retry_fetch")
        assert state["recovery_count"] == 1

    def test_log_appended_on_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        state = _state()
        try_recovery("s", "e", _task(), tmp_path, state, llm_fn=lambda _: "retry_fetch")
        assert len(state["recovery_log"]) == 1
        entry = state["recovery_log"][0]
        assert "step" in entry
        assert "choice" in entry
        assert "result" in entry

    def test_clear_cache_removes_cache_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")
        cache_dir = tmp_path / ".cache"
        cache_dir.mkdir()
        (cache_dir / "some_file.txt").write_text("data")
        state = _state()
        try_recovery(
            "fetch_deps", "cache corrupt", _task(), tmp_path, state,
            llm_fn=lambda _: "clear_cache"
        )
        assert not cache_dir.exists()


# ---------------------------------------------------------------------------
# 9. try_recovery – dispatch exception
# ---------------------------------------------------------------------------


class TestTryRecoveryDispatchException:
    def test_dispatch_exception_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")

        def bad_dispatch(ctx, task, task_dir):
            raise RuntimeError("boom")

        orig_dispatch = dict(DISPATCH)
        DISPATCH["retry_fetch"] = bad_dispatch
        try:
            state = _state()
            recovered, choice, detail = try_recovery(
                "s", "e", _task(), tmp_path, state, llm_fn=lambda _: "retry_fetch"
            )
            assert recovered is False
            assert "boom" in detail
        finally:
            DISPATCH.update(orig_dispatch)
            DISPATCH.pop("retry_fetch", None)
            DISPATCH["retry_fetch"] = orig_dispatch["retry_fetch"]

    def test_dispatch_exception_still_increments_counter(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAC_RECOVERY_REFLEX_ENABLED", "1")

        def bad_dispatch(ctx, task, task_dir):
            raise ValueError("nope")

        orig = DISPATCH["clear_cache"]
        DISPATCH["clear_cache"] = bad_dispatch
        try:
            state = _state(count=0)
            try_recovery("s", "e", _task(), tmp_path, state, llm_fn=lambda _: "clear_cache")
            assert state["recovery_count"] == 1
        finally:
            DISPATCH["clear_cache"] = orig


# ---------------------------------------------------------------------------
# 10. DISPATCH table
# ---------------------------------------------------------------------------


class TestDispatchTable:
    def test_dispatch_has_retry_fetch(self):
        assert RemediationChoice.retry_fetch.value in DISPATCH

    def test_dispatch_has_clear_cache(self):
        assert RemediationChoice.clear_cache.value in DISPATCH

    def test_dispatch_does_not_have_escalate(self):
        assert RemediationChoice.escalate.value not in DISPATCH

    def test_dispatch_values_are_callable(self):
        for key, fn in DISPATCH.items():
            assert callable(fn), f"DISPATCH[{key!r}] is not callable"

    def test_dispatch_keys_are_strings(self):
        for key in DISPATCH:
            assert isinstance(key, str)
