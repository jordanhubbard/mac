"""Historical probe for availability-aware reviewer selection.

This is deliberately not a pytest test. scripts/fault-replay.py executes it
against both the fixed source and the parent of the fixing commit.
"""

from __future__ import annotations

import os

from mac.services import ControlPlane, sign_verification_manifest


def main() -> int:
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
            "files_changed": ["src/example.py"],
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
    result = cp.advance_default_review_workflow(task.id)
    if result.get("status") != "waiting_for_reviewer_verdict":
        print(f"fault reproduced: {result}")
        return 1
    if result.get("reviewer_agent_id") != agent.id:
        print(f"unexpected fallback reviewer: {result}")
        return 1
    print("fault absent: sole eligible node received an audited fallback review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
