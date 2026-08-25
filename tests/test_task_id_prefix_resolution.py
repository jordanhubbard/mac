"""Tests for git-style task-id prefix resolution (parity-task-prefix-01).

Acceptance criteria from the task description:
- mac task show task_d95bcaee resolves the full id (unique prefix)
- ambiguous prefix -> error with candidates
- contract tests cover unique / ambiguous / unknown

Resolution is centralised in ControlPlane._resolve_task_id so every
id-taking command (show, claim, close, summary, transition, …) inherits
it automatically.
"""

from __future__ import annotations

import pytest
from mac.models import AmbiguousIdError, NotFoundError, ValidationError
from mac.services import ControlPlane


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


# ---------------------------------------------------------------------------
# _resolve_task_id unit tests
# ---------------------------------------------------------------------------


class TestResolveTaskIdUnit:
    """White-box unit tests for ControlPlane._resolve_task_id."""

    def test_full_id_returned_as_is(self, cp):
        """A 32-hex-char id is returned without any DB round-trip."""
        full_id = "task_" + "a" * 32
        assert cp._resolve_task_id(full_id) == full_id

    def test_non_task_prefix_passthrough(self, cp):
        """Non-task-prefixed ids pass through unchanged."""
        assert cp._resolve_task_id("lease_abc123") == "lease_abc123"

    def test_non_hex_suffix_passthrough(self, cp):
        """Ids with non-hex characters after 'task_' pass through unchanged."""
        assert cp._resolve_task_id("task_abcXYZ") == "task_abcXYZ"

    def test_prefix_too_short_raises_validation_error(self, cp):
        """Prefixes shorter than 6 hex chars raise ValidationError."""
        with pytest.raises(ValidationError, match="too short"):
            cp._resolve_task_id("task_abc12")  # only 5 hex chars

    def test_prefix_too_short_edge_5_chars(self, cp):
        """5 hex chars is exactly one below the minimum (6)."""
        with pytest.raises(ValidationError, match="too short"):
            cp._resolve_task_id("task_fffff")

    def test_minimum_6_chars_accepted(self, cp):
        """6 hex chars is the minimum; it should *attempt* a lookup."""
        # No tasks in DB → NotFoundError, not ValidationError.
        with pytest.raises(NotFoundError):
            cp._resolve_task_id("task_abcdef")


# ---------------------------------------------------------------------------
# get_task integration tests (prefix resolution end-to-end)
# ---------------------------------------------------------------------------


class TestGetTaskPrefixResolution:
    """Integration tests: prefix resolution flows through get_task."""

    def _make_task_with_prefix(self, cp, prefix_hex: str, title: str = "task") -> str:
        """Create a real task whose id starts with task_<prefix_hex>."""
        # We cannot control the random part of new_id, so we use the
        # internal store to insert a row directly.
        from mac.models import utcnow, TaskState

        full_id = "task_" + prefix_hex + "0" * (32 - len(prefix_hex))
        now = utcnow()
        cp.store.execute(
            """INSERT INTO tasks
               (id, title, description, state, project, priority, required_capabilities,
                dependencies, metadata, owner_agent_id, lease_id, created_at, updated_at,
                started_at, completed_at)
               VALUES (?, ?, '', ?, '', 0, '[]', '[]', '{}', NULL, NULL, ?, ?, NULL, NULL)""",
            (full_id, title, TaskState.OPEN.value, now, now),
        )
        return full_id

    def test_unique_prefix_resolves(self, cp):
        """A unique prefix returns the full task via get_task."""
        full_id = self._make_task_with_prefix(cp, "d95bcaee12345678", "show test")
        # Use a short 8-char prefix (matches task description example)
        task = cp.get_task("task_d95bcaee")
        assert task.id == full_id

    def test_unique_prefix_6_chars(self, cp):
        """The minimum 6-char prefix works when it is unique."""
        full_id = self._make_task_with_prefix(cp, "abcdef1234567890", "six-char prefix")
        task = cp.get_task("task_abcdef")
        assert task.id == full_id

    def test_unknown_prefix_raises_not_found(self, cp):
        """An unmatched prefix raises NotFoundError."""
        # Insert one task that does NOT share the prefix
        self._make_task_with_prefix(cp, "aabbccdd11223344", "other task")
        with pytest.raises(NotFoundError, match="task_99zzzz"):
            # '99zzzz' is not valid hex → passthrough → not found at exact lookup
            cp.get_task("task_99zzzz")

    def test_unknown_hex_prefix_raises_not_found(self, cp):
        """A valid hex prefix with no match raises NotFoundError."""
        self._make_task_with_prefix(cp, "aabbccdd11223344", "other task")
        with pytest.raises(NotFoundError):
            cp.get_task("task_ffffff")

    def test_ambiguous_prefix_raises_ambiguous_error(self, cp):
        """Two tasks sharing the same prefix → AmbiguousIdError with candidates."""
        id1 = self._make_task_with_prefix(cp, "cafebabe00000001", "task alpha")
        id2 = self._make_task_with_prefix(cp, "cafebabe00000002", "task beta")
        with pytest.raises(AmbiguousIdError) as exc_info:
            cp.get_task("task_cafebabe")
        err = exc_info.value
        assert id1 in err.candidates
        assert id2 in err.candidates
        assert len(err.candidates) == 2
        # The error message should mention both candidates
        assert "cafebabe" in str(err)

    def test_ambiguous_error_lists_all_candidates(self, cp):
        """AmbiguousIdError.candidates contains every matching id."""
        prefix_hex = "deadbeef"
        ids = [
            self._make_task_with_prefix(cp, prefix_hex + "0000000%d" % i, "t%d" % i)
            for i in range(3)
        ]
        with pytest.raises(AmbiguousIdError) as exc_info:
            cp.get_task("task_" + prefix_hex)
        assert sorted(exc_info.value.candidates) == sorted(ids)

    def test_full_id_bypasses_prefix_lookup(self, cp):
        """A full 32-hex id is fetched directly (no prefix expansion)."""
        full_id = self._make_task_with_prefix(cp, "f00d1234abcd5678", "full id")
        task = cp.get_task(full_id)
        assert task.id == full_id

    def test_task_detail_uses_resolved_id_for_related_records(self, cp):
        """Prefix detail lookup returns records stored under the canonical id."""
        full_id = self._make_task_with_prefix(cp, "12345678abcdef01", "detail test")
        cp.add_evidence(
            full_id,
            "test",
            "artifact://prefix-proof",
            "prefix evidence",
            "operator",
            _trusted_internal=True,
        )
        cp.close_task(
            full_id,
            "cancelled",
            "operator",
            {"reason": "prefix detail regression proof"},
        )

        detail = cp.task_detail("task_12345678")

        assert detail["task"]["id"] == full_id
        assert [item["summary"] for item in detail["evidence"]] == ["prefix evidence"]
        assert any(
            item["to_state"] == "cancelled"
            and item["detail"]["reason"] == "prefix detail regression proof"
            for item in detail["history"]
        )

    def test_task_summary_returns_canonical_id_for_prefix(self, cp):
        full_id = self._make_task_with_prefix(cp, "87654321abcdef01", "summary test")
        cp.add_evidence(
            full_id,
            "test",
            "artifact://summary-prefix-proof",
            "summary evidence",
            "operator",
            _trusted_internal=True,
        )

        summary = cp.task_summary("task_87654321")

        assert summary["task_id"] == full_id
        assert summary["evidence_count"] == 1

    def test_add_evidence_persists_under_canonical_id_for_prefix(self, cp):
        full_id = self._make_task_with_prefix(cp, "2468ace0abcdef01", "evidence mutation")

        evidence = cp.add_evidence(
            "task_2468ace0",
            "test",
            "artifact://short-id-evidence",
            "short-id evidence",
            "operator",
            _trusted_internal=True,
        )

        assert evidence.task_id == full_id
        detail = cp.task_detail(full_id)
        assert [item["id"] for item in detail["evidence"]] == [evidence.id]
        assert any(
            item["event_type"] == "task.evidence_added" and item["task_id"] == full_id
            for item in detail["history"]
        )

    def test_transition_persists_under_canonical_id_for_prefix(self, cp):
        full_id = self._make_task_with_prefix(cp, "13579bdfabcdef01", "transition mutation")

        transitioned = cp.close_task(
            "task_13579bdf",
            "cancelled",
            "operator",
            {"reason": "short-id transition proof"},
        )

        assert transitioned.id == full_id
        assert transitioned.state == "cancelled"
        detail = cp.task_detail(full_id)
        assert any(
            item["event_type"] == "task.transitioned"
            and item["task_id"] == full_id
            and item["to_state"] == "cancelled"
            for item in detail["history"]
        )

    def test_prefix_resolution_case_insensitive(self, cp):
        """Uppercase hex prefix is normalised and resolved."""
        full_id = self._make_task_with_prefix(cp, "abcdef0011223344", "case test")
        # Pass uppercase prefix — should still resolve
        task = cp.get_task("task_ABCDEF00")
        assert task.id == full_id


# ---------------------------------------------------------------------------
# AmbiguousIdError model tests
# ---------------------------------------------------------------------------


class TestAmbiguousIdErrorModel:
    def test_candidates_stored(self):
        candidates = ["task_aaaa", "task_bbbb"]
        err = AmbiguousIdError("ambiguous", candidates)
        assert err.candidates == candidates

    def test_is_mac_error_subclass(self):
        from mac.models import MACError

        err = AmbiguousIdError("x", [])
        assert isinstance(err, MACError)

    def test_str_is_message(self):
        err = AmbiguousIdError("my message", ["a", "b"])
        assert str(err) == "my message"


# ---------------------------------------------------------------------------
# Acceptance tests: replacement_task_id prefix resolution in _transition_task_impl
# ---------------------------------------------------------------------------


class TestReplacementTaskIdPrefixResolution:
    """Acceptance criteria for task-description:
    (1) Unambiguous short prefix -> full canonical ID stored in lifecycle.
    (2) Ambiguous prefix -> AmbiguousIdError before any state change.
    (3) Unknown prefix -> NotFoundError before any state change.
    (4) Full 37-char ID is accepted unchanged.
    """

    def _insert_task(self, cp, hex_prefix: str, state: str = "open", title: str = "t") -> str:
        from mac.models import utcnow, TaskState

        full_id = "task_" + hex_prefix + "0" * (32 - len(hex_prefix))
        now = utcnow()
        state_val = (
            getattr(TaskState, state.upper()).value if hasattr(TaskState, state.upper()) else state
        )
        cp.store.execute(
            """INSERT INTO tasks
               (id, title, description, state, project, priority, required_capabilities,
                dependencies, metadata, owner_agent_id, lease_id, created_at, updated_at,
                started_at, completed_at)
               VALUES (?, ?, '', ?, '', 0, '[]', '[]', '{}', NULL, NULL, ?, ?, NULL, NULL)""",
            (full_id, title, state_val, now, now),
        )
        return full_id

    def test_ac1_short_prefix_resolves_to_canonical_id(self, cp):
        """(AC-1) An unambiguous short prefix resolves to the full canonical ID."""
        subject_id = self._insert_task(cp, "aaaa11110000bbbb", title="subject task")
        replacement_id = self._insert_task(cp, "bbbb22220000cccc", title="replacement task")
        short_prefix = "task_bbbb2222"

        result = cp.close_task(
            subject_id,
            "cancelled",
            "operator",
            {
                "disposition": "superseded",
                "replacement_task_id": short_prefix,
                "reason": "prefix resolution test",
            },
        )

        assert result.state == "cancelled"
        import json

        metadata = (
            result.metadata
            if isinstance(result.metadata, dict)
            else json.loads(result.metadata or "{}")
        )
        lifecycle = metadata.get("repository_ref_lifecycle", {})
        assert lifecycle.get("replacement_task_id") == replacement_id

    def test_ac2_ambiguous_prefix_raises_before_state_change(self, cp):
        """(AC-2) Ambiguous prefix raises AmbiguousIdError before any state change."""
        subject_id = self._insert_task(cp, "cccc11110000aaaa", title="subject")
        self._insert_task(cp, "dddd11110000bbbb", title="replacement-A")
        self._insert_task(cp, "dddd11110000cccc", title="replacement-B")
        ambiguous_prefix = "task_dddd1111"

        with pytest.raises(AmbiguousIdError):
            cp.close_task(
                subject_id,
                "cancelled",
                "operator",
                {
                    "disposition": "superseded",
                    "replacement_task_id": ambiguous_prefix,
                    "reason": "ambiguous test",
                },
            )

        # State must not have changed
        task = cp.get_task(subject_id)
        assert task.state == "open"

    def test_ac3_unknown_prefix_raises_before_state_change(self, cp):
        """(AC-3) Unknown prefix raises NotFoundError before any state change."""
        subject_id = self._insert_task(cp, "eeee11110000ffff", title="subject")
        unknown_prefix = "task_00000000dead"

        with pytest.raises(NotFoundError):
            cp.close_task(
                subject_id,
                "cancelled",
                "operator",
                {
                    "disposition": "superseded",
                    "replacement_task_id": unknown_prefix,
                    "reason": "unknown prefix test",
                },
            )

        task = cp.get_task(subject_id)
        assert task.state == "open"

    def test_ac4_full_id_passes_through_unchanged(self, cp):
        """(AC-4) A full 37-char task ID is accepted and stored without modification."""
        subject_id = self._insert_task(cp, "ffff11110000aaaa", title="subject-full")
        replacement_id = self._insert_task(cp, "ffff22220000bbbb", title="replacement-full")

        result = cp.close_task(
            subject_id,
            "cancelled",
            "operator",
            {
                "disposition": "superseded",
                "replacement_task_id": replacement_id,
                "reason": "full id test",
            },
        )

        assert result.state == "cancelled"
        import json

        metadata = (
            result.metadata
            if isinstance(result.metadata, dict)
            else json.loads(result.metadata or "{}")
        )
        lifecycle = metadata.get("repository_ref_lifecycle", {})
        assert lifecycle.get("replacement_task_id") == replacement_id
