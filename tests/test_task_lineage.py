"""Task lineage: bidirectional, and expressible by merged pull request.

Covers :mod:`mac.task_lineage` plus the supersession contract in
:mod:`mac.repository_hygiene`, which previously hard-required a replacement
*task* and so could not name a merged pull request at all.
"""

from __future__ import annotations

import pytest

from mac.models import ValidationError
from mac.repository_hygiene import normalize_cancellation_detail
from mac.task_lineage import (
    LineageError,
    build_lineage_index,
    lineage_entries,
    lineage_view,
    normalize_reference,
    record_lineage,
)


PRIOR = "task_" + "a" * 32
SUCCESSOR = "task_" + "b" * 32
THIRD = "task_" + "c" * 32
PR_URL = "https://github.com/example/mac/pull/498"


def _task(task_id, metadata=None):
    return {"id": task_id, "metadata": metadata or {}}


def test_reference_parsing_accepts_tasks_urls_and_slugs():
    assert normalize_reference(PRIOR) == {"kind": "task", "ref": PRIOR}
    assert normalize_reference(PR_URL) == {
        "kind": "pull_request",
        "ref": PR_URL,
        "repository": "example/mac",
        "number": 498,
    }
    assert normalize_reference("example/mac#498")["number"] == 498
    # Already-normalized mappings round-trip.
    assert normalize_reference({"kind": "task", "ref": PRIOR})["ref"] == PRIOR


def test_a_bare_pull_request_number_is_refused_as_unresolvable():
    # "#498" without a repository is not resolvable across the fleet, which is
    # exactly what the free text it replaces already was.
    with pytest.raises(LineageError, match="not a task_"):
        normalize_reference("#498")
    with pytest.raises(LineageError, match="must not be empty"):
        normalize_reference("")


def test_recording_lineage_is_pure_idempotent_and_demands_a_reason():
    original = {"project": "mac"}
    updated = record_lineage(original, "retry_of", PRIOR, reason="scope amended")
    assert original == {"project": "mac"}  # not mutated
    assert updated["lineage"]["retry_of"] == {"kind": "task", "ref": PRIOR}
    assert len(updated["lineage"]["entries"]) == 1

    again = record_lineage(updated, "retry_of", PRIOR, reason="scope amended")
    assert len(again["lineage"]["entries"]) == 1

    with pytest.raises(LineageError, match="reason"):
        record_lineage({}, "retry_of", PRIOR, reason="")
    with pytest.raises(LineageError, match="unsupported lineage relation"):
        record_lineage({}, "invented", PRIOR, reason="why")


def test_lineage_answers_both_directions_for_a_replacement_chain():
    successor_metadata = record_lineage(
        {}, "retry_of", PRIOR, reason="reopened after terminal evidence"
    )
    tasks = [_task(PRIOR), _task(SUCCESSOR, successor_metadata)]

    # What replaced the prior row?
    prior_view = lineage_view(PRIOR, tasks)
    assert prior_view["replaces"] == []
    assert prior_view["replaced_by"] == [
        {
            "relation": "retried_by",
            "source": {"kind": "task", "ref": SUCCESSOR},
            "reason": "reopened after terminal evidence",
        }
    ]

    # And what did the successor replace?
    successor_view = lineage_view(SUCCESSOR, tasks)
    assert successor_view["replaced_by"] == []
    assert successor_view["replaces"][0]["target"] == {"kind": "task", "ref": PRIOR}


def test_supersession_by_a_merged_pull_request_is_queryable_lineage():
    metadata = record_lineage(
        {}, "replaces", PR_URL, reason="the work landed as a merged pull request"
    )
    index = build_lineage_index([_task(SUCCESSOR, metadata)])
    assert index["reverse"][PR_URL][0]["relation"] == "replaced_by"
    assert index["forward"][SUCCESSOR][0]["target"]["number"] == 498


def test_a_scope_amendment_may_fan_out_into_several_successors():
    tasks = [
        _task(PRIOR),
        _task(SUCCESSOR, record_lineage({}, "amends", PRIOR, reason="scope split")),
        _task(THIRD, record_lineage({}, "amends", PRIOR, reason="scope split")),
    ]
    replaced_by = lineage_view(PRIOR, tasks)["replaced_by"]
    assert {entry["source"]["ref"] for entry in replaced_by} == {SUCCESSOR, THIRD}
    assert {entry["relation"] for entry in replaced_by} == {"amended_by"}


def test_legacy_replacement_pointers_stay_queryable():
    # Rows cancelled as duplicate/superseded before lineage existed named their
    # successor from the prior side. That edge is projected into both
    # directions rather than being lost.
    legacy = _task(
        PRIOR,
        {"repository_ref_lifecycle": {"replacement_task_id": SUCCESSOR}},
    )
    view = lineage_view(PRIOR, [legacy, _task(SUCCESSOR)])
    assert view["replaced_by"] == [
        {
            "relation": "replaced_by",
            "source": {"kind": "task", "ref": SUCCESSOR},
            "reason": "recorded by the prior row's cancellation disposition",
        }
    ]
    assert lineage_view(SUCCESSOR, [legacy, _task(SUCCESSOR)])["replaces"][0][
        "target"
    ] == {"kind": "task", "ref": PRIOR}


def test_flat_lineage_keys_without_an_entry_list_are_still_read():
    entries = lineage_entries({"lineage": {"retry_of": PRIOR, "reason": "manual"}})
    assert entries == [
        {
            "relation": "retry_of",
            "target": {"kind": "task", "ref": PRIOR},
            "reason": "manual",
        }
    ]


# --- supersession contract -------------------------------------------------


def test_superseded_cancellation_accepts_a_merged_pull_request():
    normalized = normalize_cancellation_detail(
        {
            "disposition": "superseded",
            "replacement_pull_request": PR_URL,
            "reason": "the work landed as PR 498",
        }
    )
    assert normalized["replacement_pull_request"] == PR_URL
    assert "replacement_task_id" not in normalized


def test_superseded_cancellation_still_accepts_a_replacement_task():
    normalized = normalize_cancellation_detail(
        {
            "disposition": "duplicate",
            "replacement_task_id": SUCCESSOR,
            "reason": "duplicate of the successor",
        }
    )
    assert normalized["replacement_task_id"] == SUCCESSOR
    assert "replacement_pull_request" not in normalized


def test_superseded_cancellation_still_requires_naming_something():
    with pytest.raises(ValidationError, match="replacement_pull_request"):
        normalize_cancellation_detail(
            {"disposition": "superseded", "reason": "it went away somehow"}
        )


def test_a_replacement_pull_request_must_actually_be_a_pull_request():
    with pytest.raises(ValidationError, match="not a task"):
        normalize_cancellation_detail(
            {
                "disposition": "superseded",
                "replacement_pull_request": SUCCESSOR,
                "reason": "wrong field",
            }
        )
    with pytest.raises(ValidationError, match="pull request URL"):
        normalize_cancellation_detail(
            {
                "disposition": "superseded",
                "replacement_pull_request": "PR 498, probably",
                "reason": "free text is what we are replacing",
            }
        )


def test_a_malformed_replacement_task_id_is_still_refused():
    with pytest.raises(ValidationError, match="task_<32 hex>"):
        normalize_cancellation_detail(
            {
                "disposition": "preserve",
                "replacement_task_id": "bad",
                "reason": "invalid replacement fixture",
            }
        )
