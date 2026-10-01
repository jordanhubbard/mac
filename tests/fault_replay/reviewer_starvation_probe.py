"""Historical probe for review progress with only one physical worker.

This is deliberately not a pytest test. scripts/fault-replay.py executes it
against both the fixed source and the parent of the fixing commit.
"""

from __future__ import annotations

import os

from mac.services import ControlPlane, sign_verification_manifest


def main() -> int:
    # The old same-worker fallback was retired: the hub-reviewer now approves
    # from the worker's validated evidence, so a single physical worker never
    # has to review its own task. Keep the legacy hub opt-in off on the
    # historical tree, so it exercises its default reviewer route rather than
    # bypassing the original defect.
    os.environ["MAC_REVIEW_HUB_VERIFY"] = "0"
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("only-reviewer-host", resources={"cpu": 4, "memory_gb": 8})
    agent = cp.register_agent(
        machine.id,
        "only-reviewer",
        capabilities=["python", "review"],
        resources={
            "commands": {
                "schema": "mac.command_inventory.v1",
                "available": ["python3", "git", "gh"],
            }
        },
    )
    task = cp.create_task(
        "historical single-node review probe",
        required_capabilities=["python"],
        metadata={"publication_target": "test://fault-replay"},
    )
    cp.claim_task(task.id, agent.id)
    cp.start_task(task.id, agent.id)
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": "abcdef1234567890abcdef1234567890abcdef12",
            "pushed": True,
            "remote_ref": "refs/heads/task/fault-replay",
            "dirty": False,
            "files_changed": ["README.md"],
        },
        "tests": [{"command": "pytest tests/test_example.py", "returncode": 0}],
        "signed_by": agent.id,
    }
    manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(agent.id), manifest
    )
    cp.add_evidence(
        task.id,
        "log",
        "artifact://fault-replay",
        "tests passed",
        agent.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    cp.submit_for_review(task.id, agent.id)
    try:
        result = cp.advance_default_review_workflow(task.id)
        approved = [review for review in cp.list_reviews(task.id) if review.status == "approved"]
    finally:
        cp.store.close()
    if not approved:
        print(f"fault reproduced: {result}")
        return 1
    if any(review.reviewer_agent_id == agent.id for review in approved):
        print(f"executor was incorrectly allowed to review itself: {result}")
        return 1
    print("fault absent: single physical worker progressed to an independent approval")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
