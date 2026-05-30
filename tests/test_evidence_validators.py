import pytest

from mac.evidence_validators import (
    registered_evidence_types,
    validate_evidence_type,
)


def _repo_manifest(**overrides):
    manifest = {
        "schema": "mac.verification.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": "abcdef1234567890",
            "dirty": False,
            "pushed": True,
            "remote_ref": "origin/main",
            "files_changed": ["src/mac/services.py"],
        },
        "checks": [{"name": "pytest", "returncode": 0}],
    }
    manifest.update(overrides)
    return manifest


def _passed_check_count(manifest):
    return sum(1 for item in manifest.get("checks", []) if item.get("returncode") == 0)


def test_evidence_validators_are_registry_backed_by_type():
    assert registered_evidence_types() == [
        "artifact",
        "deployment",
        "documentation",
        "no_change",
        "operator_result",
        "repo_change",
        "review_verdict",
        "test",
    ]
    assert validate_evidence_type(
        "repo_change",
        _repo_manifest(),
        passed_check_count=_passed_check_count,
    ) == []

    assert validate_evidence_type(
        "operator_result",
        {
            "schema": "mac.verification.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "summary": "Plan produced",
            "result": "Story graph produced.",
        },
        passed_check_count=_passed_check_count,
    ) == []


def test_repo_change_validator_reuses_repo_anchor_and_check_gates():
    manifest = _repo_manifest()
    manifest["repo"]["dirty"] = True
    manifest["repo"]["files_changed"] = []
    manifest["checks"] = [{"name": "pytest", "returncode": 1}]

    problems = validate_evidence_type(
        "repo_change",
        manifest,
        passed_check_count=_passed_check_count,
    )

    assert "repo evidence must declare dirty=false" in problems
    assert "repo evidence requires changed files" in problems
    assert "repo code evidence requires at least one passing test/check" in problems


def test_non_code_validators_keep_type_specific_requirements():
    deployment = _repo_manifest(
        evidence_type="deployment",
        targets=["rocky"],
    )
    assert validate_evidence_type(
        "deployment",
        deployment,
        passed_check_count=_passed_check_count,
    ) == []

    artifact = _repo_manifest(evidence_type="artifact")
    problems = validate_evidence_type(
        "artifact",
        artifact,
        passed_check_count=_passed_check_count,
    )
    assert "artifact evidence requires artifacts" in problems

    no_change = _repo_manifest(
        evidence_type="no_change",
        repo={**_repo_manifest()["repo"], "files_changed": []},
        reason="already implemented",
    )
    assert validate_evidence_type(
        "no_change",
        no_change,
        passed_check_count=_passed_check_count,
    ) == []


def test_review_verdict_validator_requires_verdict_anchor_and_digest():
    manifest = _repo_manifest(
        evidence_type="review_verdict",
        verdict="approved",
        reviewed_evidence_id="ev_123",
        worktree_digest="sha256:" + "a" * 64,
    )

    assert validate_evidence_type(
        "review_verdict",
        manifest,
        passed_check_count=_passed_check_count,
    ) == []

    manifest.pop("reviewed_evidence_id")
    manifest["worktree_digest"] = "not-a-digest"
    problems = validate_evidence_type(
        "review_verdict",
        manifest,
        passed_check_count=_passed_check_count,
    )
    assert "review_verdict evidence requires reviewed_evidence_id" in problems
    assert "review_verdict evidence requires worktree_digest sha256" in problems


def test_review_verdict_validator_allows_repo_less_operator_result_verdict():
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": "approved",
        "reviewed_evidence_id": "ev_123",
        "worktree_digest": "sha256:" + "0" * 64,
        "checks": [{"name": "reviewer independent verification", "returncode": 0}],
    }

    assert validate_evidence_type(
        "review_verdict",
        manifest,
        passed_check_count=_passed_check_count,
    ) == []


# ---------------------------------------------------------------------------
# mem-13: when a manifest declares pushed=true + remote_url + remote_ref,
# the validator must verify the ref actually resolves on the remote.
# ---------------------------------------------------------------------------


def _patch_remote_ref_check(monkeypatch, result):
    """Replace the real git ls-remote call with a stub returning ``result``."""
    import mac.evidence_validators as ev
    monkeypatch.setattr(ev, "_verify_remote_ref_resolves", lambda url, ref: result)


def test_remote_ref_resolution_disabled_when_env_off(monkeypatch):
    """mem-13: MAC_VALIDATE_REMOTE_REFS=0 short-circuits the check
    even when remote_url is supplied — used for offline dev / CI."""
    monkeypatch.setenv("MAC_VALIDATE_REMOTE_REFS", "0")
    # No monkeypatching of _verify_remote_ref_resolves: if the gate
    # doesn't short-circuit, this would try a network call.
    manifest = _repo_manifest()
    manifest["repo"]["remote_url"] = "https://example.invalid/repo.git"
    manifest["repo"]["remote_ref"] = "refs/heads/main"
    assert validate_evidence_type(
        "repo_change",
        manifest,
        passed_check_count=_passed_check_count,
    ) == []


def test_remote_ref_resolves_clean_manifest(monkeypatch):
    """mem-13: the check runs and a real-looking ref resolution returns
    None (i.e., no problems added)."""
    monkeypatch.setenv("MAC_VALIDATE_REMOTE_REFS", "1")
    _patch_remote_ref_check(monkeypatch, None)
    manifest = _repo_manifest()
    manifest["repo"]["remote_url"] = "https://example.com/repo.git"
    manifest["repo"]["remote_ref"] = "refs/heads/feature/x"
    assert validate_evidence_type(
        "repo_change",
        manifest,
        passed_check_count=_passed_check_count,
    ) == []


def test_remote_ref_resolution_rejects_phantom_push(monkeypatch):
    """mem-13: when git ls-remote returns no match for the claimed ref,
    the validator rejects the evidence. This closes the loop on the
    runaway-loop incident (executor lied about pushing → validator
    couldn't see it → reviewer fetch failed → review retracted)."""
    monkeypatch.setenv("MAC_VALIDATE_REMOTE_REFS", "1")
    _patch_remote_ref_check(
        monkeypatch,
        "repo.remote_ref refs/heads/phantom does not resolve on "
        "https://example.com/repo.git (git ls-remote returncode=2: ...)",
    )
    manifest = _repo_manifest()
    manifest["repo"]["remote_url"] = "https://example.com/repo.git"
    manifest["repo"]["remote_ref"] = "refs/heads/phantom"
    problems = validate_evidence_type(
        "repo_change",
        manifest,
        passed_check_count=_passed_check_count,
    )
    assert any("does not resolve" in p for p in problems), problems


def test_remote_ref_resolution_best_effort_on_network_failure(monkeypatch):
    """mem-13: a network failure (helper returns None for that case)
    must not reject the evidence — flaky CI shouldn't break the
    contract. Other anchor checks still run."""
    monkeypatch.setenv("MAC_VALIDATE_REMOTE_REFS", "1")
    _patch_remote_ref_check(monkeypatch, None)
    manifest = _repo_manifest()
    manifest["repo"]["remote_url"] = "https://unreachable.invalid/repo.git"
    manifest["repo"]["remote_ref"] = "refs/heads/main"
    # Other checks should still run normally; an empty problems list
    # means the network-failure best-effort path didn't reject.
    assert validate_evidence_type(
        "repo_change",
        manifest,
        passed_check_count=_passed_check_count,
    ) == []
