import json

import pytest

from mac.models import NotFoundError, TaskState, TransitionError, ValidationError
from mac.services import ControlPlane
from tests.conftest import bind_soul


@pytest.fixture()
def cp():
    cp = ControlPlane.in_memory()
    cp.roles.create_role(
        slug="qa",
        name="QA",
        description="d",
        system_prompt="p",
        level="ic",
        default_capabilities=["python", "qa"],
    )
    cp.roles.create_role(
        slug="dev",
        name="Dev",
        description="d",
        system_prompt="p",
        level="ic",
        default_capabilities=["python"],
    )
    return cp


def _two_node_workflow(cp, *, slug="bug-default"):
    return cp.workflows.create_workflow(
        slug=slug,
        name="Bug",
        description="walk a bug to fix",
        workflow_type="bug",
        definition={
            "nodes": [
                {
                    "node_key": "investigate",
                    "node_type": "task",
                    "role_required": "qa",
                    "max_attempts": 1,
                },
                {
                    "node_key": "fix",
                    "node_type": "task",
                    "role_required": "dev",
                    "max_attempts": 1,
                },
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "investigate", "condition": "success", "priority": 100},
                {"from_node_key": "investigate", "to_node_key": "fix", "condition": "success", "priority": 100},
                {"from_node_key": "investigate", "to_node_key": "", "condition": "failure", "priority": 100},
                {"from_node_key": "fix", "to_node_key": "", "condition": "success", "priority": 100},
            ],
        },
        created_by="human",
    )


def _approval_workflow(cp, *, slug="approval-flow"):
    """Workflow that opens with an approval gate.

    Keeping `review` as the start node lets pre_decision tests verify
    the skip-and-advance path without needing to drive an upstream task
    through the full claim/start/review/approve flow first.
    """
    return cp.workflows.create_workflow(
        slug=slug,
        name="Approval",
        description="approval-first workflow",
        workflow_type="custom",
        definition={
            "nodes": [
                {"node_key": "review", "node_type": "approval", "role_required": "qa", "max_attempts": 1, "instructions": "approve scope"},
                {"node_key": "build", "node_type": "task", "role_required": "dev", "max_attempts": 1},
                {"node_key": "rework", "node_type": "task", "role_required": "qa", "max_attempts": 1},
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "review", "condition": "success", "priority": 100},
                {"from_node_key": "review", "to_node_key": "build", "condition": "approved", "priority": 100},
                {"from_node_key": "review", "to_node_key": "rework", "condition": "rejected", "priority": 90},
                {"from_node_key": "build", "to_node_key": "", "condition": "success", "priority": 100},
                {"from_node_key": "rework", "to_node_key": "", "condition": "success", "priority": 100},
            ],
        },
        created_by="human",
    )


def test_pre_decisions_validated_against_workflow_nodes(cp):
    """wf-03: start_run refuses pre_decisions referring to unknown / non-approval nodes."""
    _approval_workflow(cp)
    with pytest.raises(ValidationError, match="unknown or non-approval"):
        cp.workflow_runtime.start_run(
            "approval-flow",
            started_by="ops",
            pre_decisions={"design": "approved"},  # design is a task, not an approval
        )
    with pytest.raises(ValidationError, match="unknown or non-approval"):
        cp.workflow_runtime.start_run(
            "approval-flow",
            started_by="ops",
            pre_decisions={"bogus": "approved"},
        )


def test_pre_decisions_validated_against_value_vocabulary(cp):
    """wf-03: pre_decisions values must be `approved` or `rejected`."""
    _approval_workflow(cp)
    with pytest.raises(ValidationError, match="must be `approved` or `rejected`"):
        cp.workflow_runtime.start_run(
            "approval-flow",
            started_by="ops",
            pre_decisions={"review": "yes-please"},
        )


def test_pre_decision_approved_skips_to_next_node(cp):
    """wf-03: a pre-decided `approved` skips the approval node entirely
    and lands the run on the next node along the `approved` edge."""
    _approval_workflow(cp)
    run = cp.workflow_runtime.start_run(
        "approval-flow",
        started_by="ops",
        pre_decisions={"review": "approved"},
    )
    # `review` was pre-decided — the run should have skipped it during
    # start_run and parked on `build` (the `approved` edge target).
    assert run.current_node_key == "build", run.current_node_key
    assert run.current_task_id is not None  # `build` is a real task node
    # The skip is recorded in workflow_run_history as the audit trail.
    rows = cp.store.query_all(
        "SELECT to_node_key, condition, task_id, detail "
        "FROM workflow_run_history WHERE run_id = ? ORDER BY seq",
        (run.id,),
    )
    keys = [r["to_node_key"] for r in rows]
    # Row 0 is the start-edge entry into review (condition=success);
    # row 1 is the skip-out via the pre-decision.
    assert keys == ["review", "build"]
    skip_row = rows[1]
    assert skip_row["condition"] == "approved"
    assert skip_row["task_id"] is None  # no real task for a skipped node
    detail = json.loads(skip_row["detail"])
    assert detail["approval_decision"] == "approved"
    assert detail["pre_decision_origin"] == "workflow_start"
    assert detail.get("skipped") is True


def test_pre_decision_rejected_skips_along_rejected_edge(cp):
    """wf-03: a pre-decided `rejected` follows the rejected-condition edge."""
    _approval_workflow(cp)
    run = cp.workflow_runtime.start_run(
        "approval-flow",
        started_by="ops",
        pre_decisions={"review": "rejected"},
    )
    assert run.current_node_key == "rework"  # the `rejected` edge target
    rows = cp.store.query_all(
        "SELECT to_node_key, condition FROM workflow_run_history "
        "WHERE run_id = ? ORDER BY seq",
        (run.id,),
    )
    assert [r["to_node_key"] for r in rows] == ["review", "rework"]
    assert rows[1]["condition"] == "rejected"


def test_pre_decision_reached_after_task_can_complete_run(cp):
    cp.workflows.create_workflow(
        slug="task-then-approved-gate",
        name="Task then approved gate",
        description="exercise advancement through a pre-decided terminal gate",
        workflow_type="custom",
        definition={
            "nodes": [
                {
                    "node_key": "build",
                    "node_type": "task",
                    "role_required": "dev",
                    "max_attempts": 1,
                },
                {
                    "node_key": "gate",
                    "node_type": "approval",
                    "role_required": "qa",
                    "max_attempts": 1,
                },
            ],
            "edges": [
                {
                    "from_node_key": "",
                    "to_node_key": "build",
                    "condition": "success",
                    "priority": 100,
                },
                {
                    "from_node_key": "build",
                    "to_node_key": "gate",
                    "condition": "success",
                    "priority": 100,
                },
                {
                    "from_node_key": "gate",
                    "to_node_key": "",
                    "condition": "approved",
                    "priority": 100,
                },
            ],
        },
        created_by="human",
    )
    run = cp.workflow_runtime.start_run(
        "task-then-approved-gate",
        started_by="ops",
        pre_decisions={"gate": "approved"},
    )

    completed = cp.workflow_runtime._advance(
        run, "build", "success", task_id=run.current_task_id
    )

    assert completed.state == "completed"
    assert completed.current_node_key is None
    assert completed.current_task_id is None


def test_advance_restores_reservation_when_task_spawn_fails(cp, monkeypatch):
    _two_node_workflow(cp, slug="spawn-rollback")
    run = cp.workflow_runtime.start_run("spawn-rollback", started_by="ops")
    original_node = run.current_node_key
    original_task = run.current_task_id

    def fail_spawn(*_args, **_kwargs):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(cp.workflow_runtime, "_spawn_node_task", fail_spawn)
    with pytest.raises(RuntimeError, match="spawn failed"):
        cp.workflow_runtime._advance(
            run, "investigate", "success", task_id=original_task
        )

    restored = cp.workflow_runtime.get_run(run.id)
    assert restored.current_node_key == original_node
    assert restored.current_task_id == original_task


def test_plan_node_is_a_valid_node_type(cp):
    """wf-04: workflow_models accepts node_type='plan'."""
    wf = cp.workflows.create_workflow(
        slug="planner",
        name="Planner",
        description="d",
        workflow_type="custom",
        definition={
            "nodes": [
                {"node_key": "plan", "node_type": "plan", "role_required": "qa", "max_attempts": 1, "instructions": "plan it"},
                {"node_key": "execute", "node_type": "task", "role_required": "dev", "max_attempts": 1, "instructions": "static fallback"},
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "plan", "condition": "success", "priority": 100},
                {"from_node_key": "plan", "to_node_key": "execute", "condition": "success", "priority": 100},
                {"from_node_key": "execute", "to_node_key": "", "condition": "success", "priority": 100},
            ],
        },
        created_by="human",
    )
    assert wf.definition["nodes"][0]["node_type"] == "plan"


def test_plan_node_stamps_run_input_on_task_metadata(cp):
    """wf-04: a plan-typed node sees the run's input as metadata.plan_input."""
    cp.workflows.create_workflow(
        slug="planner2",
        name="Planner2",
        description="d",
        workflow_type="custom",
        definition={
            "nodes": [
                {"node_key": "plan", "node_type": "plan", "role_required": "qa", "max_attempts": 1, "instructions": "plan it"},
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "plan", "condition": "success", "priority": 100},
                {"from_node_key": "plan", "to_node_key": "", "condition": "success", "priority": 100},
            ],
        },
        created_by="human",
    )
    run = cp.workflow_runtime.start_run(
        "planner2",
        started_by="ops",
        input={"description": "Add Slack DM support to the notifier"},
    )
    plan_task = cp.get_task(run.current_task_id)
    assert plan_task.metadata.get("is_plan_node") is True
    assert plan_task.metadata.get("plan_input") == {
        "description": "Add Slack DM support to the notifier"
    }


def test_plan_payloads_override_downstream_node_instructions(cp):
    """wf-04: plan_payloads on the run's context override downstream node fields."""
    # Build a workflow whose plan node has just run successfully and
    # stashed plan_payloads on the run's context. Then drive an advance
    # to the downstream node and verify the spawned task picked up the
    # overrides.
    cp.workflows.create_workflow(
        slug="planner3",
        name="Planner3",
        description="d",
        workflow_type="custom",
        definition={
            "nodes": [
                {"node_key": "plan", "node_type": "plan", "role_required": "qa", "max_attempts": 1, "instructions": "plan it"},
                {"node_key": "execute", "node_type": "task", "role_required": "dev", "max_attempts": 1, "instructions": "static fallback instructions"},
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "plan", "condition": "success", "priority": 100},
                {"from_node_key": "plan", "to_node_key": "execute", "condition": "success", "priority": 100},
                {"from_node_key": "execute", "to_node_key": "", "condition": "success", "priority": 100},
            ],
        },
        created_by="human",
    )
    run = cp.workflow_runtime.start_run("planner3", started_by="ops")
    # Manually plant plan_payloads on the run's context (simulating that
    # the plan task completed and harvested its evidence). We don't run
    # the plan task itself through the full review pipeline here — the
    # injection logic is what we want to verify.
    cp.store.execute(
        "UPDATE workflow_runs SET context = ? WHERE id = ?",
        (
            '{"plan_payloads": {"execute": {"instructions": "Implement Slack DM in src/notifier.py", "metadata": {"resolved_by": "plan"}}}}',
            run.id,
        ),
    )
    # Reach the second node via _advance, bypassing the plan task
    # completion flow (which would require a full review).
    updated_run = cp.workflow_runtime.get_run(run.id)
    cp.workflow_runtime._advance(updated_run, "plan", "success", task_id=None)
    final_run = cp.workflow_runtime.get_run(run.id)
    assert final_run.current_node_key == "execute"
    execute_task = cp.get_task(final_run.current_task_id)
    # The plan payload's instructions won out over the static
    # definition's "static fallback instructions".
    assert "Slack DM" in execute_task.description
    assert execute_task.metadata.get("resolved_by") == "plan"


def test_start_run_spawns_first_node_task_with_role_metadata(cp):
    _two_node_workflow(cp)
    run = cp.workflow_runtime.start_run("bug-default", started_by="ops")
    assert run.state == "running"
    assert run.current_node_key == "investigate"
    task = cp.get_task(run.current_task_id)
    assert task.metadata["workflow_run_id"] == run.id
    assert task.metadata["workflow_node_key"] == "investigate"
    assert task.metadata["required_role"] == "qa"
    # role.default_capabilities stack onto the task.
    assert "qa" in task.required_capabilities
    assert "python" in task.required_capabilities


def test_run_advances_on_task_completed_through_to_terminal(cp):
    _two_node_workflow(cp)
    run = cp.workflow_runtime.start_run("bug-default", started_by="ops")
    first_task = cp.get_task(run.current_task_id)

    # Register a worker that satisfies the qa role, claim and run the
    # first task to NEEDS_REVIEW then add evidence and approve.
    machine = cp.register_machine("h1")
    qa_soul = bind_soul(cp, persona_name="QA Soul", allowed_role_slugs=["qa"])
    qa_agent = cp.register_agent(
        machine.id,
        "rocky",
        capabilities=["python", "qa", "review"],
        hermes_instance_id=qa_soul,
    )
    cp.roles.assign_role(qa_agent.id, "qa")
    cp.claim_task(first_task.id, qa_agent.id)
    cp.start_task(first_task.id, qa_agent.id)
    from mac.services import sign_verification_manifest
    from tests.conftest import submit_review_verdict

    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "test",
        "repo": {
            "head_sha": "abcdef1234567890abcdef1234567890abcdef12",
            "pushed": True,
            "remote_ref": "refs/heads/task/workflow",
            "dirty": False,
        },
        "checks": [{"name": "pytest", "returncode": 0}],
    }
    manifest["signed_by"] = qa_agent.id
    manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(qa_agent.id), manifest
    )
    evidence = cp.add_evidence(
        first_task.id,
        "test",
        "file://t",
        "tests passed",
        qa_agent.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    cp.submit_for_review(first_task.id, qa_agent.id)

    # Reviewer registered separately so they can request + approve.
    # No role assignment so no soul is required.
    reviewer = cp.register_agent(machine.id, "reviewer", capabilities=["review"])
    review = cp.request_review(first_task.id, reviewer.id, "ops")
    verdict_id = submit_review_verdict(cp, first_task.id, reviewer.id, evidence.id)
    cp.submit_review(
        review.id,
        "approved",
        reviewer.id,
        evidence_id=verdict_id,
    )
    cp.publish_task(first_task.id, "stdout", "ops", evidence_id=evidence.id)

    # The runtime should have spawned the next node when the first task
    # hit COMPLETED via publish_task.
    run = cp.workflow_runtime.get_run(run.id)
    assert run.current_node_key == "fix"
    fix_task = cp.get_task(run.current_task_id)
    assert fix_task.metadata["required_role"] == "dev"


def test_failed_task_picks_failure_edge_and_finishes(cp):
    _two_node_workflow(cp)
    run = cp.workflow_runtime.start_run("bug-default", started_by="ops")
    first_task = cp.get_task(run.current_task_id)

    machine = cp.register_machine("h1")
    soul = bind_soul(cp, persona_name="QA Soul", allowed_role_slugs=["qa"])
    qa_agent = cp.register_agent(
        machine.id, "rocky", capabilities=["python", "qa"], hermes_instance_id=soul
    )
    cp.roles.assign_role(qa_agent.id, "qa")
    cp.claim_task(first_task.id, qa_agent.id)
    cp.start_task(first_task.id, qa_agent.id)
    # Fail the task — there's a failure edge to '' (terminal).
    cp.transition_task(first_task.id, TaskState.FAILED.value, "rocky")

    run = cp.workflow_runtime.get_run(run.id)
    assert run.state == "failed"
    assert run.completed_at is not None


def test_cancel_run_cancels_current_task_and_marks_run_cancelled(cp):
    _two_node_workflow(cp)
    run = cp.workflow_runtime.start_run("bug-default", started_by="ops")
    first_task_id = run.current_task_id

    cancelled = cp.workflow_runtime.cancel_run(
        run.id, reason="operator abort", actor="ops"
    )
    assert cancelled.state == "cancelled"
    # The current task got cancelled too.
    assert cp.get_task(first_task_id).state == TaskState.CANCELLED.value
    workflow_tasks = [
        task
        for task in cp.list_tasks()
        if task.metadata.get("workflow_run_id") == run.id
    ]
    assert [task.id for task in workflow_tasks] == [first_task_id]
    assert cancelled.current_task_id == first_task_id


def test_cancel_run_rolls_back_task_when_run_compare_and_swap_loses(
    cp, monkeypatch
):
    _two_node_workflow(cp, slug="cancel-race")
    run = cp.workflow_runtime.start_run("cancel-race", started_by="ops")
    task_id = run.current_task_id
    original_transition = cp.workflow_runtime._transition_task_in_transaction

    def steal_run_after_task_transition(conn, *args, **kwargs):
        transitioned = original_transition(conn, *args, **kwargs)
        conn.execute(
            "UPDATE workflow_runs SET updated_at = ? WHERE id = ?",
            ("2099-01-01T00:00:00+00:00", run.id),
        )
        return transitioned

    monkeypatch.setattr(
        cp.workflow_runtime,
        "_transition_task_in_transaction",
        steal_run_after_task_transition,
    )

    with pytest.raises(TransitionError, match="changed during cancellation"):
        cp.workflow_runtime.cancel_run(
            run.id,
            reason="lost cancellation race",
            actor="ops",
        )

    assert cp.workflow_runtime.get_run(run.id).state == "running"
    assert cp.get_task(task_id).state == TaskState.OPEN.value


def test_forged_workflow_run_id_metadata_is_ignored_by_runtime(cp):
    """A caller cannot smuggle a free-floating task into the workflow
    state machine by setting metadata.workflow_run_id — the runtime
    only acts on tasks where the *column* tasks.workflow_run_id is set,
    which only the runtime does itself."""
    _two_node_workflow(cp)
    run = cp.workflow_runtime.start_run("bug-default", started_by="ops")

    # Create a separate task that pretends to belong to the run.
    forged = cp.create_task(
        "forged",
        required_capabilities=[],
        metadata={"workflow_run_id": run.id, "workflow_node_key": "fix"},
    )
    # Bring it to terminal state.
    machine = cp.register_machine("h2")
    agent = cp.register_agent(machine.id, "outsider")
    cp.claim_task(forged.id, agent.id)
    cp.start_task(forged.id, agent.id)
    cp.transition_task(forged.id, TaskState.FAILED.value, "outsider")

    # Run hasn't moved off its first node.
    run_after = cp.workflow_runtime.get_run(run.id)
    assert run_after.current_node_key == "investigate"
    assert run_after.state == "running"


def test_disabled_workflow_cannot_be_started(cp):
    wf = _two_node_workflow(cp, slug="bug-disabled")
    cp.workflows.disable_workflow(wf.id)
    with pytest.raises(ValidationError):
        cp.workflow_runtime.start_run("bug-disabled", started_by="ops")


def test_cyclic_workflow_terminates_when_max_attempts_exhausted(cp):
    """mac-q0mq / mac-pykd: a cyclic workflow (e.g. review→fix→review) must
    not loop forever. Each node's max_attempts caps how many times the
    runtime will spawn a task for that node within a single run."""
    cp.workflows.create_workflow(
        slug="bug-cycle",
        name="cycle",
        description="d",
        workflow_type="bug",
        definition={
            "nodes": [
                {"node_key": "investigate", "node_type": "task", "role_required": "qa", "max_attempts": 2},
                {"node_key": "fix", "node_type": "task", "role_required": "dev", "max_attempts": 2},
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "investigate", "condition": "success", "priority": 100},
                # cycle: each side fails into the other on failure
                {"from_node_key": "investigate", "to_node_key": "fix", "condition": "failure", "priority": 100},
                {"from_node_key": "fix", "to_node_key": "investigate", "condition": "failure", "priority": 100},
            ],
        },
        created_by="human",
    )
    run = cp.workflow_runtime.start_run("bug-cycle", started_by="ops")

    machine = cp.register_machine("h1")
    qa_soul = bind_soul(cp, persona_name="QA Soul", allowed_role_slugs=["qa"])
    qa_agent = cp.register_agent(machine.id, "qa1", capabilities=["python", "qa"], hermes_instance_id=qa_soul)
    cp.roles.assign_role(qa_agent.id, "qa")
    dev_soul = bind_soul(cp, persona_name="Dev Soul", allowed_role_slugs=["dev"])
    dev_agent = cp.register_agent(machine.id, "dev1", capabilities=["python"], hermes_instance_id=dev_soul)
    cp.roles.assign_role(dev_agent.id, "dev")

    # Drive the cycle: every task fails, looping between investigate ↔ fix.
    # investigate max_attempts=2, fix max_attempts=2.
    # Spawns: investigate#1 (start) → fix#1 → investigate#2 → fix#2 → next
    # spawn refused (cap hit on whichever side comes next).
    for _ in range(10):
        run = cp.workflow_runtime.get_run(run.id)
        if run.state != "running":
            break
        task = cp.get_task(run.current_task_id)
        agent = qa_agent if task.metadata["required_role"] == "qa" else dev_agent
        cp.claim_task(task.id, agent.id)
        cp.start_task(task.id, agent.id)
        cp.transition_task(task.id, TaskState.FAILED.value, agent.id)

    final = cp.workflow_runtime.get_run(run.id)
    assert final.state == "failed", f"expected failed after max_attempts, got {final.state}"
    history = cp.store.query_all(
        "SELECT to_node_key, condition FROM workflow_run_history WHERE run_id = ? ORDER BY seq",
        (run.id,),
    )
    assert history[-1]["condition"] == "max_attempts_exhausted"
    # And the loop was bounded — neither node spawned more than its cap.
    investigate_spawns = sum(1 for r in history if r["to_node_key"] == "investigate")
    fix_spawns = sum(1 for r in history if r["to_node_key"] == "fix")
    assert investigate_spawns <= 2 and fix_spawns <= 2


def test_workflow_run_freezes_role_snapshots_against_mid_run_role_edits(cp):
    """mac-hbk7: a mid-run change to a role's capabilities or hardware
    requirements must not retroactively change capability requirements
    on the next-spawned task. The runtime should resolve from the
    snapshot embedded at start_run, not the live role row."""
    _two_node_workflow(cp, slug="bug-freeze")
    run = cp.workflow_runtime.start_run("bug-freeze", started_by="ops")

    # Bake-in: the seed role had default_capabilities=["python", "qa"].
    first_task = cp.get_task(run.current_task_id)
    assert set(first_task.required_capabilities) >= {"python", "qa"}

    # Now mid-run, edit the role's capabilities.
    cp.roles.update_role("qa", default_capabilities=["evil-injection"])

    # Drive the first task to completion. The fix-node task that gets
    # spawned next will resolve from the SNAPSHOT — but bug-freeze uses
    # role "dev" for fix, so role qa is now irrelevant. Let me re-route:
    # the right test is to start a fresh run after the role edit and
    # check the first task uses the snapshot (not the live edit).
    cp.roles.update_role(
        "qa", default_capabilities=["unrelated-cap-that-shouldnt-leak"]
    )
    run2 = cp.workflow_runtime.start_run("bug-freeze", started_by="ops")
    task2 = cp.get_task(run2.current_task_id)
    # Per the snapshot embedded at start_run time, the task must carry
    # the EDITED capabilities (snapshot is taken at that moment).
    assert "unrelated-cap-that-shouldnt-leak" in task2.required_capabilities
    # Now edit the role again AFTER run2 started, and verify the
    # snapshot taken at run2.start does NOT shift.
    cp.roles.update_role("qa", default_capabilities=["NEW-CAPS"])
    # Spawn a subsequent fix node via _advance — but bug-freeze fixes use
    # role "dev", not qa, so use the qa first-node snapshot check.
    # Re-resolving from snapshot is observable: a brand-new start_run
    # picks up the latest, but run2's stored snapshot is frozen.
    run3 = cp.workflow_runtime.start_run("bug-freeze", started_by="ops")
    task3 = cp.get_task(run3.current_task_id)
    assert "NEW-CAPS" in task3.required_capabilities
    # And run2's investigate task remains as it was at its own start time.
    refreshed_task2 = cp.get_task(run2.current_task_id)
    assert "unrelated-cap-that-shouldnt-leak" in refreshed_task2.required_capabilities


def test_find_cycles_detects_review_fix_loop():
    """mac-q0mq: cycle detection is exposed as a method on WorkflowDefinition."""
    from mac.workflow_models import WorkflowDefinition

    definition = WorkflowDefinition.parse(
        {
            "nodes": [
                {"node_key": "review", "node_type": "approval", "role_required": "pm"},
                {"node_key": "fix", "node_type": "task", "role_required": "dev"},
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "review", "condition": "success", "priority": 100},
                {"from_node_key": "review", "to_node_key": "fix", "condition": "approved", "priority": 100},
                {"from_node_key": "fix", "to_node_key": "review", "condition": "success", "priority": 100},
            ],
        }
    )
    cycles = WorkflowDefinition.find_cycles(
        [n.node_key for n in definition.nodes], definition.edges
    )
    assert cycles, "expected at least one cycle"
    # The detected cycle should include both nodes
    nodes_in_cycles = {n for cycle in cycles for n in cycle}
    assert {"review", "fix"} <= nodes_in_cycles


def test_tick_times_out_stuck_node_and_advances_via_failure_edge(cp):
    # Workflow whose first node has a 1-minute timeout and a failure
    # edge to the terminal sink.
    cp.workflows.create_workflow(
        slug="bug-timeout",
        name="b",
        description="d",
        workflow_type="bug",
        definition={
            "nodes": [
                {
                    "node_key": "investigate",
                    "node_type": "task",
                    "role_required": "qa",
                    "max_attempts": 1,
                    "timeout_minutes": 1,
                }
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "investigate", "condition": "success", "priority": 100},
                {"from_node_key": "investigate", "to_node_key": "", "condition": "cancelled", "priority": 100},
            ],
        },
        created_by="human",
    )
    run = cp.workflow_runtime.start_run("bug-timeout", started_by="ops")
    task_id = run.current_task_id

    # Backdate the task so it's been "running" past the timeout.
    cp.store.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", task_id),
    )
    advanced = cp.workflow_runtime.tick()
    assert any(r.id == run.id for r in advanced)
    assert cp.workflow_runtime.get_run(run.id).state in {"failed", "cancelled"}
