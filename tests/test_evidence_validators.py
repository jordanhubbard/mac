import subprocess

import pytest

from mac.codegraph_audit import CODEGRAPH_AUDIT_SCHEMA
from mac.evidence_validators import (
    normalize_manifest_tests,
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
        "codegraph": {
            "schema": CODEGRAPH_AUDIT_SCHEMA,
            "status": "pass",
            "reason": "affected_computed",
            "relevant_files": ["src/mac/services.py"],
            "commands": [
                {"argv": ["codegraph", "sync"], "returncode": 0},
                {"argv": ["codegraph", "affected"], "returncode": 0},
            ],
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
        "investigation",
        "no_change",
        "operator_result",
        "plan_decomposed",
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


def test_investigation_reuses_substantive_operator_result_gate():
    assert validate_evidence_type(
        "investigation",
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "investigation",
            "summary": "Verified the historical failure no longer reproduces.",
            "findings": [{"status": "not_actionable"}],
        },
        passed_check_count=_passed_check_count,
    ) == []
    assert "requires summary" in validate_evidence_type(
        "investigation",
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "investigation",
        },
        passed_check_count=_passed_check_count,
    )[0]


def test_plan_decomposed_requires_routable_children_and_rationale():
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "plan_decomposed",
        "children": [
            {"node_id": "inspect", "title": "Inspect current behavior"},
            {
                "node_id": "repair",
                "title": "Implement the repair",
                "depends_on": ["inspect"],
            },
        ],
        "ordering_rationale": "Inspect before modifying.",
        "coverage_claim": "The children cover diagnosis and implementation.",
    }
    assert validate_evidence_type(
        "plan_decomposed",
        manifest,
        passed_check_count=_passed_check_count,
        repo_coupled=True,
    ) == []
    problems = validate_evidence_type(
        "plan_decomposed",
        {
            **manifest,
            "children": [{}],
            "ordering_rationale": "",
            "coverage_claim": "",
        },
        passed_check_count=_passed_check_count,
    )
    assert "plan_decomposed child 1 requires a title" in problems
    assert "plan_decomposed evidence requires ordering_rationale" in problems
    assert "plan_decomposed evidence requires coverage_claim" in problems


def test_repo_change_does_not_require_codegraph_for_source_changes():
    manifest = _repo_manifest()
    manifest.pop("codegraph")
    problems = validate_evidence_type(
        "repo_change",
        manifest,
        passed_check_count=_passed_check_count,
    )
    assert problems == []

    docs_only = _repo_manifest(
        repo={
            **_repo_manifest()["repo"],
            "files_changed": ["README.md"],
        }
    )
    docs_only.pop("codegraph")
    assert validate_evidence_type(
        "repo_change",
        docs_only,
        passed_check_count=_passed_check_count,
    ) == []


def test_repo_change_ignores_malformed_advisory_codegraph_evidence():
    manifest = _repo_manifest(
        codegraph={
            "schema": CODEGRAPH_AUDIT_SCHEMA,
            "status": "pass",
            "relevant_files": ["src/mac/services.py"],
        }
    )

    problems = validate_evidence_type(
        "repo_change",
        manifest,
        passed_check_count=_passed_check_count,
    )

    assert problems == []


def test_artifact_validator_does_not_require_codegraph_for_source_changes():
    manifest = _repo_manifest(
        evidence_type="artifact",
        artifacts=["artifact://build"],
    )
    manifest.pop("codegraph")

    problems = validate_evidence_type(
        "artifact",
        manifest,
        passed_check_count=_passed_check_count,
    )

    assert problems == []


def test_operator_result_rejected_for_repo_coupled_task():
    # mem-11: a repo-coupled task must anchor on a pushed commit, not a free-text
    # operator_result (the verified task_d7c51a0b "hello hello…" jam).
    # Use a substantive summary so this exercises the *repo-coupled* gate and not
    # the separate substance gate (see test_operator_result_rejects_degenerate…).
    manifest = {
        "schema": "mac.verification.v1",
        "status": "complete",
        "evidence_type": "operator_result",
        "summary": "Produced the rollout plan and identified the three blocking dependencies.",
    }
    # Not repo-coupled (default): a substantive planning result is accepted.
    assert validate_evidence_type("operator_result", manifest, passed_check_count=_passed_check_count) == []
    # Repo-coupled: rejected, with guidance to use a pushed repo anchor.
    problems = validate_evidence_type(
        "operator_result", manifest, passed_check_count=_passed_check_count, repo_coupled=True
    )
    assert problems and "not accepted for a repo-coupled task" in problems[0]


def test_operator_result_rejects_degenerate_and_placeholder_text():
    # autonomy-loop fix: the executor fallback turned agent chatter / its own
    # no-output stub into a PUBLISHED operator_result. Both must now be rejected.
    def _op(summary="", result=""):
        return {
            "schema": "mac.verification.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "summary": summary,
            "result": result,
        }

    # The literal jam payload.
    bad = validate_evidence_type("operator_result", _op(summary="hello hello hello"),
                                 passed_check_count=_passed_check_count)
    assert bad and "not substantive" in bad[0]
    # The fallback writer's own placeholder.
    bad2 = validate_evidence_type(
        "operator_result",
        _op(summary="Hermes executor completed without textual output."),
        passed_check_count=_passed_check_count,
    )
    assert bad2 and "not substantive" in bad2[0]
    # A genuine planning summary clears the bar.
    good = validate_evidence_type(
        "operator_result",
        _op(summary="Story graph produced", result="Mapped the milestones and owners."),
        passed_check_count=_passed_check_count,
    )
    assert good == []
    # Structured findings/artifacts always pass, regardless of summary text.
    structured = {
        "schema": "mac.verification.v1",
        "status": "complete",
        "evidence_type": "operator_result",
        "summary": "ok",
        "findings": [{"id": 1, "note": "x"}],
    }
    assert validate_evidence_type("operator_result", structured, passed_check_count=_passed_check_count) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("summary", "Worker host runtime checks all passed."),
        ("result", "Recovered the worker and verified normal execution."),
        ("findings", [{"check": "runtime", "status": "PASS"}]),
        ("artifacts", [{"uri": "artifact://recovery-report"}]),
    ],
)
def test_operator_result_accepts_canonical_nested_content(field, value):
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "operator_result",
        "operator_result": {field: value},
    }

    assert (
        validate_evidence_type(
            "operator_result",
            manifest,
            passed_check_count=_passed_check_count,
        )
        == []
    )


@pytest.mark.parametrize(
    "nested",
    [None, {}, "not-an-object"],
)
def test_operator_result_rejects_empty_or_malformed_nested_content(nested):
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "operator_result",
        "operator_result": nested,
    }

    problems = validate_evidence_type(
        "operator_result",
        manifest,
        passed_check_count=_passed_check_count,
    )

    assert problems == [
        "operator_result evidence requires summary, result, findings, or artifacts"
    ]


def test_operator_result_live_host_requires_check_and_reviewable_anchor():
    # A live-host operator_result (ops/deployment run against a real host) must
    # carry independently reviewable anchors, not a substantive summary alone.
    substantive = "Deployed the new agent build to the host and confirmed it."

    # Substantive summary but no passing check and no anchor -> both problems.
    bare = validate_evidence_type(
        "operator_result",
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "live_host": True,
            "summary": substantive,
        },
        passed_check_count=_passed_check_count,
    )
    assert any("at least one passing check" in p for p in bare)
    assert any("reviewable anchor" in p for p in bare)

    # Passing check but no reviewable anchor -> only the anchor problem.
    no_anchor = validate_evidence_type(
        "operator_result",
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "live_host": True,
            "summary": substantive,
            "checks": [{"name": "health", "returncode": 0}],
        },
        passed_check_count=_passed_check_count,
    )
    assert no_anchor == [
        "live-host operator_result evidence requires an independently "
        "reviewable anchor (artifact/target/host identifier or artifact "
        "digest), not a summary alone"
    ]

    # Reviewable anchor but no passing check -> only the check problem.
    no_check = validate_evidence_type(
        "operator_result",
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "operator_result": {
                "live_host": "true",
                "summary": substantive,
                "artifact_digest": "sha256:abc",
            },
        },
        passed_check_count=_passed_check_count,
    )
    assert no_check == [
        "live-host operator_result evidence requires at least one passing "
        "check verifying the work on the host"
    ]

    # Passing check plus a verifiable target -> accepted.
    anchored = validate_evidence_type(
        "operator_result",
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "live_host": True,
            "summary": substantive,
            "targets": ["rocky-01.example"],
            "checks": [{"name": "health", "returncode": 0}],
        },
        passed_check_count=_passed_check_count,
    )
    assert anchored == []


@pytest.mark.parametrize(
    ("anchor_key", "anchor_value"),
    [
        ("target", "rocky-01.example"),
        ("host", "rocky-01.example"),
        ("artifact", "agent-build-42.tgz"),
        ("artifact_digest", "sha256:abc"),
        ("image_digest", "sha256:def"),
    ],
)
def test_operator_result_live_host_accepts_each_reviewable_anchor(anchor_key, anchor_value):
    # A passing check plus any one of the independently reviewable anchor keys
    # (target/host/artifact/artifact_digest/image_digest) satisfies the gate.
    accepted = validate_evidence_type(
        "operator_result",
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "live_host": True,
            "summary": "Deployed the new agent build to the host and confirmed it.",
            anchor_key: anchor_value,
            "checks": [{"name": "health", "returncode": 0}],
        },
        passed_check_count=_passed_check_count,
    )
    assert accepted == []


def test_non_live_operator_result_keeps_substance_only_gate():
    # Planning/answer/report operator_result (no live-host marker) is unchanged:
    # a substantive summary alone still passes, with no check/anchor demand.
    assert (
        validate_evidence_type(
            "operator_result",
            {
                "schema": "mac.worker_evidence.v1",
                "status": "complete",
                "evidence_type": "operator_result",
                "summary": "Mapped the milestones and owners across the plan.",
            },
            passed_check_count=_passed_check_count,
        )
        == []
    )


def test_repo_change_requires_tests_only_when_contract_demands():
    # mac-wjy3: tests:null is rejected only when the contract requires tests.
    base = _repo_manifest()  # has a passing check, no tests list
    assert validate_evidence_type("repo_change", base, passed_check_count=_passed_check_count) == []
    problems = validate_evidence_type(
        "repo_change", base, passed_check_count=_passed_check_count, require_tests=True
    )
    assert any("tests is null/missing" in p for p in problems)
    with_tests = _repo_manifest(tests=[{"command": "scripts/run-contract-tests.sh", "returncode": 0}])
    assert validate_evidence_type(
        "repo_change", with_tests, passed_check_count=_passed_check_count, require_tests=True
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

    local_no_change = _repo_manifest(
        evidence_type="no_change",
        repo={
            "head_sha": "abcdef1234567890",
            "dirty": False,
            "pushed": False,
            "files_changed": [],
        },
        reason="The requested behavior is already present at the inspected commit.",
    )
    assert validate_evidence_type(
        "no_change",
        local_no_change,
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


def test_remote_ref_resolution_uses_environment_backed_auth(monkeypatch):
    import mac.evidence_validators as ev

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="abcdef refs/heads/private\n",
            stderr="",
        )

    monkeypatch.setenv("GH_TOKEN", "top-secret")
    monkeypatch.setattr(ev.subprocess, "run", fake_run)

    assert (
        ev._verify_remote_ref_resolves(
            "https://github.com/acme/private.git",
            "refs/heads/private",
        )
        is None
    )
    assert captured["argv"][2].startswith("https://x-access-token:top-secret@")
    assert captured["kwargs"]["timeout"] == ev._REMOTE_REF_VERIFY_TIMEOUT_SEC


def test_remote_ref_auth_failure_is_indeterminate_and_secret_free(monkeypatch):
    import mac.evidence_validators as ev

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("MAC_TASK_GIT_TOKEN", raising=False)
    monkeypatch.setattr(
        ev.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            128,
            stdout="",
            stderr=(
                "fatal: unable to access "
                "https://x-access-token:top-secret@github.com/acme/private.git: "
                "could not read Username for 'https://github.com'"
            ),
        ),
    )

    assert (
        ev._verify_remote_ref_resolves(
            "https://github.com/acme/private.git",
            "refs/heads/private",
        )
        is None
    )


def test_remote_ref_definitive_failure_redacts_embedded_credentials(monkeypatch):
    import mac.evidence_validators as ev

    monkeypatch.setattr(
        ev.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            128,
            stdout="",
            stderr=(
                "fatal: repository "
                "https://x-access-token:top-secret@github.com/acme/missing.git "
                "not found"
            ),
        ),
    )

    failure = ev._verify_remote_ref_resolves(
        "https://x-access-token:top-secret@github.com/acme/missing.git",
        "refs/heads/private",
    )

    assert failure is not None
    assert "does not resolve" in failure
    assert "not found" in failure
    assert "top-secret" not in failure
    assert "<redacted>" in failure


# ---------------------------------------------------------------------------
# normalize_manifest_tests — regression for the ADR/DAG-schema executor bug
# where a single dict test result caused submit-for-review to reject passing
# evidence as "tests null/missing".  task_d9b043263f864340a41b2679b019a906 /
# task_35e1283e57e9443bbca9e948664fa428 regression fixtures.
# ---------------------------------------------------------------------------


def test_normalize_manifest_tests_dict_wrapped_to_list():
    """A single test result dict must be wrapped in a one-element list."""
    raw = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "tests": {
            "command": "scripts/run-contract-tests.sh",
            "returncode": 0,
            "passed": 2181,
            "skipped": 19,
            "status": "pass",
        },
    }
    normalised = normalize_manifest_tests(raw)
    assert isinstance(normalised["tests"], list)
    assert len(normalised["tests"]) == 1
    assert normalised["tests"][0]["returncode"] == 0
    # Original must not be mutated.
    assert isinstance(raw["tests"], dict)


def test_normalize_manifest_tests_list_unchanged():
    """A tests value that is already a list must pass through unmodified."""
    tests_list = [{"command": "pytest", "returncode": 0}]
    raw = {"tests": tests_list}
    assert normalize_manifest_tests(raw)["tests"] is tests_list


def test_normalize_manifest_tests_none_unchanged():
    """A missing/None tests value must be left alone (fail-closed path)."""
    raw = {"schema": "mac.worker_evidence.v1"}
    result = normalize_manifest_tests(raw)
    assert "tests" not in result or result.get("tests") is None


def test_normalize_manifest_tests_non_dict_non_list_unchanged():
    """An unexpected scalar is left as-is (fail-closed: don't fabricate a list)."""
    raw = {"tests": "not-a-result"}
    assert normalize_manifest_tests(raw)["tests"] == "not-a-result"


def _adr_task_style_manifest():
    """Reproduce the evidence shape that task_d9b043263f864340 produced:
    verification.tests is a single dict (not a list)."""
    return {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": "abcdef1234567890",
            "dirty": False,
            "pushed": True,
            "remote_ref": "refs/heads/mac/agent/task-adr",
            "files_changed": ["docs/adr/0001-example.md"],
        },
        "codegraph": {
            "schema": CODEGRAPH_AUDIT_SCHEMA,
            "status": "skipped",
            "reason": "non_code_change",
        },
        "tests": {
            "command": "scripts/run-contract-tests.sh",
            "returncode": 0,
            "passed": 2181,
            "skipped": 19,
            "failed": 0,
            "status": "pass",
        },
        "checks": [
            {"name": "contract_tests", "returncode": 0, "status": "pass"}
        ],
    }


def test_adr_task_evidence_with_dict_tests_passes_require_tests():
    """Regression: the Fleet ADR task produced tests as a dict and was rejected
    at submit-for-review.  A dict tests value must be accepted as a list by the
    validator when require_tests=True."""
    manifest = _adr_task_style_manifest()
    problems = validate_evidence_type(
        "repo_change",
        manifest,
        passed_check_count=_passed_check_count,
        require_tests=True,
    )
    assert problems == [], "Expected no validation problems, got: %s" % problems


def test_dag_schema_task_evidence_with_dict_tests_passes_require_tests():
    """Regression: task_35e1283e57e9443bbca9e948664fa428 (DAG schema task)
    produced signed evidence with tests as a dict — 132 passed, 4 skipped,
    returncode 0 — and was rejected.  Validate the same shape."""
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": "1234abcd5678ef90",
            "dirty": False,
            "pushed": True,
            "remote_ref": "refs/heads/mac/agent/task-dag-schema",
            "files_changed": ["src/mac/dag_schema.py"],
        },
        "codegraph": {
            "schema": CODEGRAPH_AUDIT_SCHEMA,
            "status": "pass",
            "reason": "affected_computed",
            "relevant_files": ["src/mac/dag_schema.py"],
            "commands": [
                {"argv": ["codegraph", "sync"], "returncode": 0},
                {"argv": ["codegraph", "affected"], "returncode": 0},
            ],
        },
        "tests": {
            "command": "scripts/run-contract-tests.sh",
            "returncode": 0,
            "passed": 132,
            "skipped": 4,
            "failed": 0,
            "status": "pass",
        },
        "checks": [
            {"name": "contract_tests", "returncode": 0, "status": "pass"}
        ],
    }
    problems = validate_evidence_type(
        "repo_change",
        manifest,
        passed_check_count=_passed_check_count,
        require_tests=True,
    )
    assert problems == [], "Expected no validation problems, got: %s" % problems


def test_require_tests_still_fails_when_tests_absent():
    """Normalization must not accidentally accept a truly missing tests field."""
    manifest = _repo_manifest()  # no 'tests' key — only 'checks'
    # Without require_tests: valid (checks alone suffice).
    assert validate_evidence_type(
        "repo_change", manifest, passed_check_count=_passed_check_count
    ) == []
    # With require_tests: must still report tests null/missing.
    problems = validate_evidence_type(
        "repo_change", manifest, passed_check_count=_passed_check_count, require_tests=True
    )
    assert any("null/missing" in p for p in problems), problems


def test_require_tests_still_fails_when_tests_is_null():
    """Explicit tests:null must not pass when require_tests=True."""
    manifest = _repo_manifest(tests=None)
    problems = validate_evidence_type(
        "repo_change", manifest, passed_check_count=_passed_check_count, require_tests=True
    )
    assert any("null/missing" in p for p in problems), problems


def test_tests_list_with_one_item_passes_require_tests():
    """A canonical one-element list must be accepted unchanged."""
    manifest = _repo_manifest(
        tests=[{"command": "scripts/run-contract-tests.sh", "returncode": 0, "status": "pass"}]
    )
    assert validate_evidence_type(
        "repo_change", manifest, passed_check_count=_passed_check_count, require_tests=True
    ) == []
