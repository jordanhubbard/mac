"""The hub must decode evidence with the task's DECLARED outcome type.

Reproduces the 2026-09-25 kit onboarding failure: the task declares
evidence_type=investigation / repository_required=False and instructs the agent
NOT to push, the executor defaults its manifest to repo_change because it made a
local commit, and RepoChangeValidator then demands pushed=true/pr_url -- which
the task forbade. The worker already coerces this for its own pre-submit checks;
the hub did not, so the two halves disagreed and the task could never pass.
"""

from __future__ import annotations

from mac.services import ControlPlane


def _cp():
    return ControlPlane.in_memory()


def _investigation_task(cp):
    """A task whose contract declares a non-repository investigation outcome."""
    task = cp.create_task(
        "Onboard repository contract",
        description="READ-ONLY: do NOT push or open a pull request.",
        metadata={
            "evidence_type": "investigation",
            "execution_contract": {
                "evidence_type": "investigation",
                "repository_required": False,
                "reason": "explicit_non_repository_outcome",
                "type": "operator_directive",
            },
        },
    )
    return task


def _repo_change_manifest():
    """What the executor actually emits after a local commit, unpushed."""
    return {
        "schema": "mac.verification.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": "0" * 40,
            "files_changed": ["/.mac/project.yaml"],
            "dirty": False,
            "pushed": False,
            "remote_ref": "refs/heads/mac/agent/task-lease",
        },
    }


def test_declared_investigation_is_not_held_to_repo_push_rules():
    cp = _cp()
    task = _investigation_task(cp)

    problems = cp._verification_type_problems(task, _repo_change_manifest(), "repo_change")

    pushed = [p for p in problems if "pushed=true" in p or "pr_url" in p]
    assert not pushed, (
        "a task declaring repository_required=False must not be required to push: %r" % problems
    )


def test_repo_coupled_task_still_requires_a_push():
    """The coercion must not weaken genuine repo work."""
    cp = _cp()
    task = cp.create_task(
        "Implement a feature",
        description="normal code task",
        metadata={"execution_contract": {"type": "repository", "evidence_type": "repo_change"}},
    )

    problems = cp._verification_type_problems(task, _repo_change_manifest(), "repo_change")

    assert any("pushed=true" in p or "pr_url" in p for p in problems), (
        "an ordinary repo_change task must still require a pushed anchor: %r" % problems
    )


def test_investigation_claim_without_a_declared_contract_is_still_rejected():
    """The forward check is unchanged: you cannot self-declare investigation."""
    cp = _cp()
    task = cp.create_task(
        "Implement a feature",
        description="normal code task",
        metadata={"execution_contract": {"type": "repository", "evidence_type": "repo_change"}},
    )
    manifest = {"schema": "mac.verification.v1", "status": "complete", "evidence_type": "investigation"}

    problems = cp._verification_type_problems(task, manifest, "investigation")

    assert any("operator-authored" in p for p in problems)
