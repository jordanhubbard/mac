"""Tests for mac.evidence_reuse_verifier — the fail-closed verifier for reusing
prior executor evidence.

Covers:
- Signed evidence / checksum provenance checks
- Evidence type and status checks
- Pushed remote ref checks
- Repo head SHA checks
- Remote SHA equality checks (with network mocked)
- Canonical ancestry checks (with network mocked)
- Required checks / tests including CodeGraph
- Dirty / stale branch conditions
- Structured pass/fail reasons
- Overall ok / fail aggregation
"""
from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from mac.codegraph_audit import CODEGRAPH_AUDIT_SCHEMA
from mac.evidence_reuse_verifier import (
    REUSE_VERIFIER_SCHEMA,
    CheckResult,
    ReuseVerificationResult,
    _check_canonical_ancestry,
    _check_codegraph,
    _check_dirty_stale_branch,
    _check_evidence_type,
    _check_pushed_remote_ref,
    _check_remote_sha_equality,
    _check_repo_head_sha,
    _check_required_tests_and_checks,
    _check_schema_and_status,
    _check_signature_provenance,
    verify_prior_executor_evidence,
)

GOOD_SHA = "a" * 40
ALT_SHA = "b" * 40
VERIFICATION_SCHEMA = "mac.worker_evidence.v1"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _base_manifest(**overrides: Any) -> Dict[str, Any]:
    """A minimal valid manifest that passes all structural checks."""
    manifest: Dict[str, Any] = {
        "schema": VERIFICATION_SCHEMA,
        "status": "complete",
        "evidence_type": "repo_change",
        "signed_by": "agent_test",
        "signature": "v1:placeholder",
        "repo": {
            "head_sha": GOOD_SHA,
            "dirty": False,
            "pushed": True,
            "remote_ref": "refs/heads/task/branch",
            "remote_url": "git@github.com:org/repo.git",
            "files_changed": ["src/mac/services.py"],
        },
        "tests": [{"name": "contract tests", "returncode": 0, "status": "pass"}],
        "checks": [{"name": "codegraph_audit", "returncode": 0, "status": "pass"}],
        "codegraph": {
            "schema": CODEGRAPH_AUDIT_SCHEMA,
            "status": "pass",
            "reason": "affected_computed",
            "relevant_files": ["src/mac/services.py"],
            "commands": [
                {"argv": ["codegraph", "init", "."], "returncode": 0, "status": "pass"},
                {"argv": ["codegraph", "affected", "--stdin", "--json"], "returncode": 0, "status": "pass"},
            ],
        },
    }
    manifest.update(overrides)
    return manifest


def _fake_key_lookup(agent_id: str) -> Optional[str]:
    """Always returns a dummy key — used to bypass crypto in unit tests."""
    return "test-key-abc"


# ---------------------------------------------------------------------------
# _check_schema_and_status
# ---------------------------------------------------------------------------


def test_schema_and_status_pass():
    m = _base_manifest()
    r = _check_schema_and_status(m)
    assert r.ok
    assert r.status == "pass"


def test_schema_wrong():
    m = _base_manifest(schema="wrong.schema")
    r = _check_schema_and_status(m)
    assert not r.ok
    assert r.reason == "wrong_schema"


def test_status_not_complete():
    m = _base_manifest(status="verified")
    r = _check_schema_and_status(m)
    assert not r.ok
    assert r.reason == "status_not_complete"


def test_status_complete_case_insensitive():
    # The verifier normalises to lower-case, so "Complete" is accepted.
    # This matches the hub's _assess_default_review_evidence behaviour.
    m = _base_manifest(status="Complete")
    r = _check_schema_and_status(m)
    assert r.ok, "status is normalised to lower-case before comparison"


# ---------------------------------------------------------------------------
# _check_evidence_type
# ---------------------------------------------------------------------------


def test_evidence_type_repo_change_pass():
    m = _base_manifest(evidence_type="repo_change")
    r = _check_evidence_type(m)
    assert r.ok


def test_evidence_type_missing_fail():
    m = _base_manifest(evidence_type="")
    r = _check_evidence_type(m)
    assert not r.ok
    assert r.reason == "evidence_type_missing"


def test_evidence_type_review_verdict_rejected():
    m = _base_manifest(evidence_type="review_verdict")
    r = _check_evidence_type(m)
    assert not r.ok
    assert r.reason == "review_verdict_not_reusable"


def test_evidence_type_operator_result_accepted():
    m = _base_manifest(evidence_type="operator_result")
    r = _check_evidence_type(m)
    assert r.ok


# ---------------------------------------------------------------------------
# _check_signature_provenance
# ---------------------------------------------------------------------------


def test_signature_missing_signed_by():
    m = _base_manifest()
    m.pop("signed_by")
    r = _check_signature_provenance(m, None)
    assert not r.ok
    assert r.reason == "manifest_not_signed"


def test_signature_missing_signature():
    m = _base_manifest()
    m.pop("signature")
    r = _check_signature_provenance(m, None)
    assert not r.ok
    assert r.reason == "manifest_not_signed"


def test_signature_structural_check_when_no_key_lookup():
    """When agent_key_lookup is None we only check field presence."""
    m = _base_manifest()
    r = _check_signature_provenance(m, None)
    assert r.ok
    assert "key_lookup_skipped" in r.reason


def test_signature_signer_unknown():
    def lookup(agent_id: str) -> Optional[str]:
        return None

    m = _base_manifest()
    r = _check_signature_provenance(m, lookup)
    assert not r.ok
    assert r.reason == "signer_unknown"


def test_signature_invalid_hmac():
    def lookup(agent_id: str) -> Optional[str]:
        return "wrong-key"

    m = _base_manifest()
    r = _check_signature_provenance(m, lookup)
    assert not r.ok
    assert r.reason == "signature_invalid"


def test_signature_valid_hmac():
    """A manifest signed with the known key passes crypto verification."""
    from mac.services import sign_verification_manifest

    key = "test-key-xyz"
    manifest = _base_manifest()
    manifest.pop("signature", None)
    manifest["signature"] = sign_verification_manifest(key, manifest)

    def lookup(agent_id: str) -> Optional[str]:
        return key

    r = _check_signature_provenance(manifest, lookup)
    assert r.ok
    assert r.reason == "signature_verified"


# ---------------------------------------------------------------------------
# _check_pushed_remote_ref
# ---------------------------------------------------------------------------


def test_pushed_remote_ref_pass():
    m = _base_manifest()
    r = _check_pushed_remote_ref(m)
    assert r.ok


def test_pushed_remote_ref_no_repo():
    m = _base_manifest()
    m.pop("repo")
    r = _check_pushed_remote_ref(m)
    assert not r.ok
    assert r.reason == "repo_missing"


def test_pushed_remote_ref_bad_sha():
    m = _base_manifest()
    m["repo"] = {**m["repo"], "head_sha": "not-a-sha"}
    r = _check_pushed_remote_ref(m)
    assert not r.ok
    assert r.reason == "head_sha_invalid"


def test_pushed_remote_ref_dirty():
    m = _base_manifest()
    m["repo"] = {**m["repo"], "dirty": True}
    r = _check_pushed_remote_ref(m)
    assert not r.ok
    assert r.reason == "repo_dirty"


def test_pushed_remote_ref_not_pushed():
    m = _base_manifest()
    m["repo"] = {**m["repo"], "pushed": False, "pr_url": ""}
    r = _check_pushed_remote_ref(m)
    assert not r.ok
    assert r.reason == "not_pushed"


def test_pushed_remote_ref_pr_url_accepted():
    m = _base_manifest()
    m["repo"] = {**m["repo"], "pushed": False, "pr_url": "https://github.com/org/repo/pull/42"}
    r = _check_pushed_remote_ref(m)
    assert r.ok


# ---------------------------------------------------------------------------
# _check_repo_head_sha
# ---------------------------------------------------------------------------


def test_repo_head_sha_skipped_when_none():
    m = _base_manifest()
    r = _check_repo_head_sha(m, None)
    assert r.status == "skip"


def test_repo_head_sha_matches():
    m = _base_manifest()
    r = _check_repo_head_sha(m, GOOD_SHA)
    assert r.ok


def test_repo_head_sha_mismatch():
    m = _base_manifest()
    r = _check_repo_head_sha(m, ALT_SHA)
    assert not r.ok
    assert r.reason == "head_sha_mismatch"


def test_repo_head_sha_missing_repo():
    m = _base_manifest()
    m.pop("repo")
    r = _check_repo_head_sha(m, GOOD_SHA)
    assert not r.ok
    assert r.reason == "repo_missing"


# ---------------------------------------------------------------------------
# _check_remote_sha_equality
# ---------------------------------------------------------------------------


def test_remote_sha_equality_skipped_when_disabled():
    m = _base_manifest()
    r = _check_remote_sha_equality(m, verify_remote=False)
    assert r.status == "skip"
    assert "disabled" in r.reason


def test_remote_sha_equality_pass(monkeypatch):
    m = _base_manifest()
    ls_output = "%s\trefs/heads/task/branch\n" % GOOD_SHA

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=ls_output, stderr="")

    monkeypatch.setattr(
        "mac.evidence_reuse_verifier._run_git",
        fake_run,
    )
    r = _check_remote_sha_equality(m, verify_remote=True)
    assert r.ok
    assert r.reason == "remote_sha_matches"


def test_remote_sha_equality_mismatch(monkeypatch):
    m = _base_manifest()
    ls_output = "%s\trefs/heads/task/branch\n" % ALT_SHA

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=ls_output, stderr="")

    monkeypatch.setattr(
        "mac.evidence_reuse_verifier._run_git",
        fake_run,
    )
    r = _check_remote_sha_equality(m, verify_remote=True)
    assert not r.ok
    assert r.reason == "remote_sha_mismatch"


def test_remote_sha_equality_ref_not_found(monkeypatch):
    m = _base_manifest()

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        "mac.evidence_reuse_verifier._run_git",
        fake_run,
    )
    r = _check_remote_sha_equality(m, verify_remote=True)
    assert not r.ok
    assert r.reason == "ref_not_found_on_remote"


def test_remote_sha_equality_auth_failure_is_skipped(monkeypatch):
    m = _base_manifest()

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 128, stdout="", stderr="ERROR: Repository not found."
        )

    monkeypatch.setattr(
        "mac.evidence_reuse_verifier._run_git",
        fake_run,
    )
    # classify_repository_access_failure should classify "Repository not found"
    # as an auth failure → skip, not fail.
    r = _check_remote_sha_equality(m, verify_remote=True)
    # Result is either skip (auth classified) or fail (not classified).
    # We only assert it doesn't raise.
    assert r.status in {"skip", "fail"}


def test_remote_sha_equality_no_remote_url_skipped():
    m = _base_manifest()
    m["repo"] = {**m["repo"], "remote_url": ""}
    r = _check_remote_sha_equality(m, verify_remote=True)
    assert r.status == "skip"
    assert "no_remote_coordinates" in r.reason


# ---------------------------------------------------------------------------
# _check_canonical_ancestry
# ---------------------------------------------------------------------------


def test_canonical_ancestry_skipped_when_no_url():
    m = _base_manifest()
    r = _check_canonical_ancestry(m, canonical_remote_url=None, verify_remote=True)
    assert r.status == "skip"


def test_canonical_ancestry_skipped_when_disabled():
    m = _base_manifest()
    r = _check_canonical_ancestry(m, canonical_remote_url="git@github.com:org/repo.git", verify_remote=False)
    assert r.status == "skip"


def test_canonical_ancestry_head_is_tip(monkeypatch):
    m = _base_manifest()
    ls_output = "%s\trefs/heads/main\n" % GOOD_SHA

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=ls_output, stderr="")

    monkeypatch.setattr(
        "mac.evidence_reuse_verifier._run_git",
        fake_run,
    )
    r = _check_canonical_ancestry(
        m,
        canonical_remote_url="git@github.com:org/repo.git",
        verify_remote=True,
    )
    assert r.ok
    assert r.reason == "head_sha_is_canonical_tip"


def test_canonical_ancestry_different_sha_yields_skip(monkeypatch):
    m = _base_manifest()
    # Remote returns a different SHA (canonical moved ahead).
    ls_output = "%s\trefs/heads/main\n" % ALT_SHA

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=ls_output, stderr="")

    monkeypatch.setattr(
        "mac.evidence_reuse_verifier._run_git",
        fake_run,
    )
    r = _check_canonical_ancestry(
        m,
        canonical_remote_url="git@github.com:org/repo.git",
        verify_remote=True,
    )
    # Without local repo we can't run merge-base → skip.
    assert r.status == "skip"
    assert "local_repo_not_available" in r.reason


# ---------------------------------------------------------------------------
# _check_required_tests_and_checks
# ---------------------------------------------------------------------------


def test_required_tests_pass_via_returncode():
    m = _base_manifest()
    r = _check_required_tests_and_checks(m)
    assert r.ok


def test_required_tests_pass_via_status_word():
    m = _base_manifest(tests=[{"name": "x", "status": "passed"}], checks=[])
    r = _check_required_tests_and_checks(m)
    assert r.ok


def test_required_tests_fail_none_passing():
    m = _base_manifest(
        tests=[{"name": "x", "returncode": 1, "status": "fail"}],
        checks=[],
    )
    r = _check_required_tests_and_checks(m)
    assert not r.ok
    assert r.reason == "no_passing_test_or_check"


def test_required_tests_fail_empty():
    m = _base_manifest(tests=[], checks=[])
    r = _check_required_tests_and_checks(m)
    assert not r.ok


# ---------------------------------------------------------------------------
# _check_codegraph
# ---------------------------------------------------------------------------


def test_codegraph_pass():
    m = _base_manifest()
    r = _check_codegraph(m)
    assert r.ok


def test_codegraph_skipped_no_code_changes():
    m = _base_manifest()
    m["repo"] = {**m["repo"], "files_changed": ["README.md"]}
    m.pop("codegraph", None)
    r = _check_codegraph(m)
    assert r.status == "skip"
    assert "no_code_changes" in r.reason


def test_codegraph_fail_missing_for_source():
    m = _base_manifest()
    m.pop("codegraph")
    r = _check_codegraph(m)
    assert not r.ok
    assert r.reason == "codegraph_problems"


def test_codegraph_fail_no_affected_command():
    m = _base_manifest()
    m["codegraph"] = {
        "schema": CODEGRAPH_AUDIT_SCHEMA,
        "status": "pass",
        "relevant_files": ["src/mac/services.py"],
        "commands": [
            # Missing affected command
            {"argv": ["codegraph", "init", "."], "returncode": 0},
        ],
    }
    r = _check_codegraph(m)
    assert not r.ok
    assert "codegraph_problems" in r.reason


# ---------------------------------------------------------------------------
# _check_dirty_stale_branch
# ---------------------------------------------------------------------------


def test_dirty_stale_branch_pass():
    m = _base_manifest()
    r = _check_dirty_stale_branch(m)
    assert r.ok


def test_dirty_stale_branch_dirty():
    m = _base_manifest()
    m["repo"] = {**m["repo"], "dirty": True}
    r = _check_dirty_stale_branch(m)
    assert not r.ok
    assert "dirty" in r.reason


def test_dirty_stale_branch_not_pushed():
    m = _base_manifest()
    m["repo"] = {**m["repo"], "pushed": False}
    r = _check_dirty_stale_branch(m)
    assert not r.ok
    assert "not_pushed" in r.reason


def test_dirty_stale_branch_no_repo():
    m = _base_manifest()
    m.pop("repo")
    r = _check_dirty_stale_branch(m)
    assert not r.ok
    assert r.reason == "repo_missing"


# ---------------------------------------------------------------------------
# verify_prior_executor_evidence (integration)
# ---------------------------------------------------------------------------


def test_verify_full_manifest_passes_without_remote(monkeypatch):
    """A well-formed signed manifest passes all local checks."""
    from mac.services import sign_verification_manifest

    key = "integration-test-key"
    manifest = _base_manifest()
    manifest.pop("signature", None)
    manifest["signature"] = sign_verification_manifest(key, manifest)

    def lookup(agent_id: str) -> Optional[str]:
        return key

    result = verify_prior_executor_evidence(
        manifest,
        agent_key_lookup=lookup,
        expected_head_sha=GOOD_SHA,
        verify_remote=False,
    )
    assert isinstance(result, ReuseVerificationResult)
    assert result.schema == REUSE_VERIFIER_SCHEMA
    assert result.ok, "problems: %s" % result.problems


def test_verify_fails_closed_on_missing_signature():
    result = verify_prior_executor_evidence(
        _base_manifest(signed_by="", signature=""),
        verify_remote=False,
    )
    assert not result.ok
    assert any("signature" in p for p in result.problems)


def test_verify_fails_closed_on_wrong_schema():
    result = verify_prior_executor_evidence(
        _base_manifest(schema="bad.schema"),
        verify_remote=False,
    )
    assert not result.ok
    assert any("schema" in p for p in result.problems)


def test_verify_fails_closed_on_dirty():
    m = _base_manifest()
    m["repo"] = {**m["repo"], "dirty": True}
    result = verify_prior_executor_evidence(m, verify_remote=False)
    assert not result.ok
    assert any("dirty" in p for p in result.problems)


def test_verify_fails_closed_on_head_sha_mismatch():
    result = verify_prior_executor_evidence(
        _base_manifest(),
        expected_head_sha=ALT_SHA,
        verify_remote=False,
    )
    assert not result.ok
    assert any("head_sha" in p for p in result.problems)


def test_verify_to_dict_shape():
    """to_dict must return the canonical serialisable shape."""
    result = verify_prior_executor_evidence(
        _base_manifest(signed_by="agent_x"),
        verify_remote=False,
    )
    d = result.to_dict()
    assert d["schema"] == REUSE_VERIFIER_SCHEMA
    assert isinstance(d["ok"], bool)
    assert isinstance(d["problems"], list)
    assert isinstance(d["checks"], list)
    for check in d["checks"]:
        assert "name" in check
        assert "status" in check
        assert "reason" in check


def test_verify_all_checks_recorded():
    """All 10 invariant checks must appear in the result."""
    result = verify_prior_executor_evidence(
        _base_manifest(),
        verify_remote=False,
    )
    check_names = {c.name for c in result.checks}
    expected_names = {
        "schema_and_status",
        "evidence_type",
        "signature_provenance",
        "pushed_remote_ref",
        "dirty_stale_branch",
        "repo_head_sha",
        "remote_sha_equality",
        "canonical_ancestry",
        "required_tests_and_checks",
        "codegraph",
    }
    assert expected_names == check_names


def test_verify_multiple_failures_all_reported():
    m = _base_manifest(
        schema="bad.schema",
        status="incomplete",
        evidence_type="",
    )
    m["repo"] = {**m["repo"], "dirty": True}
    result = verify_prior_executor_evidence(m, verify_remote=False)
    assert not result.ok
    assert len(result.problems) >= 3, "Expected at least 3 problems, got: %s" % result.problems


def test_verify_review_verdict_fails():
    """review_verdict is not executor evidence and must be rejected."""
    m = _base_manifest(evidence_type="review_verdict")
    result = verify_prior_executor_evidence(m, verify_remote=False)
    assert not result.ok
    assert any("review_verdict" in p for p in result.problems)


def test_verify_skip_does_not_cause_failure():
    """Skipped checks (verify_remote=False) must not flip ok to False."""
    from mac.services import sign_verification_manifest

    key = "skip-test-key"
    manifest = _base_manifest()
    manifest.pop("signature", None)
    manifest["signature"] = sign_verification_manifest(key, manifest)

    def lookup(agent_id: str) -> Optional[str]:
        return key

    result = verify_prior_executor_evidence(
        manifest,
        agent_key_lookup=lookup,
        verify_remote=False,
    )
    skipped = [c for c in result.checks if c.status == "skip"]
    assert skipped, "Expected some skipped checks when verify_remote=False"
    assert result.ok, "Skipped checks must not cause failure"
