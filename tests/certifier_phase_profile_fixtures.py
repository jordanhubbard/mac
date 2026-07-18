"""Reusable certification phase-profile fixtures.

The c26 profile is an example for controller and repository tests only.  It is
not a deployed c26 repository contract or a substitute for a digest-pinned c26
certifier image.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _with_checksum(profile: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(profile))
    unsigned = dict(value)
    unsigned.pop("checksum", None)
    encoded = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    value["checksum"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return value


def mac_phase_profile() -> dict[str, Any]:
    """Return MAC's exact six-mode immutable-image selector contract."""

    return _with_checksum(
        {
            "schema": "mac.certifier_phase_profile.v1",
            "version": 1,
            "full_targets": ["plugin/test_tools.py", "tests"],
            "focused_required_tests": [
                "tests/test_openshell_certifier.py",
                "tests/test_publication_lane.py",
                "tests/test_repository_contract_certification.py",
            ],
            "selection_modes": {
                "authoritative_full": {
                    "authoritative": {
                        "mode": "full",
                        "reason": "source_change_has_no_frozen_test_mapping",
                    },
                    "supplemental": {
                        "mode": "skipped",
                        "reason": "authoritative_full_is_sufficient",
                    },
                    "expected_full_suite_count": 1,
                },
                "candidate_test_focused": {
                    "authoritative": {
                        "mode": "focused",
                        "reason": "candidate_tests_are_non_authoritative_frozen_invariants",
                    },
                    "supplemental": {
                        "mode": "skipped",
                        "reason": "candidate_tests_are_worker_evidence",
                    },
                    "expected_full_suite_count": 0,
                },
                "documentation_fast_lane": {
                    "authoritative": {
                        "mode": "focused",
                        "reason": "documentation_only_invariants",
                    },
                    "supplemental": {
                        "mode": "skipped",
                        "reason": "documentation_only",
                    },
                    "expected_full_suite_count": 0,
                },
                "mixed_unmapped_rejected": {
                    "authoritative": {
                        "mode": "rejected",
                        "reason": "unmapped_source_and_candidate_root_scope_require_two_full_phases",
                    },
                    "supplemental": {
                        "mode": "skipped",
                        "reason": "selection_rejected",
                    },
                    "expected_full_suite_count": 0,
                },
                "source_focused": {
                    "authoritative": {
                        "mode": "focused",
                        "reason": "mapped_source_and_root_owned_invariants",
                    },
                    "supplemental": {
                        "mode": "skipped",
                        "reason": "no_candidate_root_visible_change",
                    },
                    "expected_full_suite_count": 0,
                },
                "supplemental_full": {
                    "authoritative": {
                        "mode": "focused",
                        "reason": "root_owned_invariants_and_mapped_source_tests",
                    },
                    "supplemental": {
                        "mode": "full",
                        "reason": "candidate_root_visible_change_requires_supplemental_full",
                    },
                    "expected_full_suite_count": 1,
                },
            },
        }
    )


def c26_phase_profile() -> dict[str, Any]:
    """Return the proposed full-only c26 profile used by tests and documentation."""

    return _with_checksum(
        {
            "schema": "mac.certifier_phase_profile.v1",
            "version": 1,
            "full_targets": ["Makefile", "scripts/smoke.py", "tests"],
            "focused_required_tests": [],
            "selection_modes": {
                "c26_full_suite": {
                    "authoritative": {
                        "mode": "full",
                        "reason": "c26_always_full",
                    },
                    "supplemental": {
                        "mode": "skipped",
                        "reason": "c26_full_suite_is_authoritative",
                    },
                    "expected_full_suite_count": 1,
                }
            },
        }
    )
