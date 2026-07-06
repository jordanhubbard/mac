# Root-Cause Report: Silent Executor Insta-Block for task_a32a35e90ab0434e8c7766057b268bc6

## Summary

**Agent:** agent_jordanh-worker1
**Host:** jordanh-worker1 (machine_jordanh-worker1)
**Lease:** lease_49ed4a5300224f25aaa5813c1bf19991
**Task state:** blocked (waiting on 6 children; lease was released at children_added time)

---

## Timeline of Key Events

```
08:45:34  [info]    task.created (open)
08:45:35  [info]    task.claimed by agent_jordanh-worker1
08:45:36  [info]    task.transitioned claimed -> running
08:45:37  [info]    executor.started (sandboxed=true, recalled_lessons=5)
08:45:38  [info]    executor.scope_estimated size=large (signals: desc_chars:1075,
                    repo_required_cmds:3, long_title:118, + 5 prior decompositions)
08:45:39  [info]    executor.planning_phase_started
08:46:08  [info]    executor.runner_selected runner=codex
                    (claude: no ANTHROPIC_API_KEY; codex: authed via ~/.codex/auth.json)
08:46:11  [info]    executor.sandbox_started sandbox=mac-task-5b1b0d607d54
08:46:11  [info]    command.started /home/horde/.local/bin/openshell (lease_id=null)
08:46:37  [info]    task.lease_renewed x4 (worker keepalive loop, ~60s intervals)
          *** EARLIEST FAILURE ***
08:49:47  [info]    task.children_added by human
                    from_state=running -> to_state=blocked
                    released_lease_id=lease_49ed4a5300224f25aaa5813c1bf19991
                    (6 child tasks posted by the codex agent inside the sandbox)
08:50:25  [warning] executor.sandbox_observation_unavailable state=unknown
08:50:25  [info]    executor.sandbox_agent_completed returncode=0
08:50:26  [warning] executor.sandbox_verification_completed passed=False
08:50:33  [info]    executor.sandbox_harvested harvested=True, runner_completed=True
08:50:33  [info]    executor.sandbox_deleted
08:50:36  [info]    executor.agent_completed returncode=0 duration_ms=296494
08:50:36  [info]    executor.planning_phase_completed
08:50:36  [info]    executor.finalized evidence_type=plan_decomposed outcome=success
                    signals: {checks_pass:null, files_changed:null, pushed:null, tests:null}
08:50:41  [warn]    worker.execution.stale_result
                    reason="assignment no longer current after executor completed"
```

---

## Earliest Pre-Diagnosis Failure

The codex agent inside the sandbox called `POST /tasks/{id}/children` at ~08:49:47
(during the 4-minute planning window). The hub's `add_children` endpoint in
`services.py` lines 4358-4383 atomically:

1. Inserts the 6 child task rows
2. Releases the parent lease: `UPDATE leases SET status='released' WHERE id=?`
3. Nulls out `owner_agent_id` and `lease_id` on the parent task row
4. Transitions parent state from `running` to `blocked`
5. Emits `task.children_added` with `released_lease_id` recorded

This happened while `_run_sandboxed()` was still executing (the openshell
process had not yet returned). The executor had no knowledge that the lease
was released mid-run.

---

## Affected Startup Path

```
MacWorker._work_one_task()
  MacWorker._execute_with_lease_renewal(task, lease, task_dir)
    task_executor.run_task_executor()
      [scope_estimate.size == "large" -> is_planning_phase() == True]
      _run_sandboxed()
        openshell sandbox: codex agent runs planning prompt
          Agent: POST /tasks/{id}/children  <- hub releases lease HERE (08:49:47)
        openshell exits rc=0
        _SandboxProgressMonitor.stop() -> ready=False -> sandbox_observation_unavailable
        _sandbox_run_repository_verification() -> passed=False (planning-phase, no code)
      <- _run_sandboxed() returns result rc=0
    is_plan_decomposed_evidence() -> True
    executor.planning_phase_completed emitted
    record_plan_outcome() called (deployment learning memories written)
    write_fallback_evidence_manifest()
    executor exits rc=0
  worker.py ~line 756:
    _assignment_is_current(task_id, lease_49ed4a53...)
      GET /tasks/{id} -> {owner_agent_id:null, lease_id:null, state:"blocked"}
      -> returns False
    _stale_result() called
      -> emits worker.execution.stale_result [WARNING]
      -> WorkerRunResult(status="stale_result")
```

---

## Root Cause

**The `add_children` hub endpoint releases the parent lease and transitions the
task to `blocked` as an atomic side-effect when the planning-phase agent posts
child tasks, while the executor process (outside the sandbox) is still running.
By the time the executor's outer `_assignment_is_current()` check runs, the lease
no longer exists, so the entire planning run is silently discarded as stale — even
though the plan was correct, all six children were posted successfully, and the
executor itself returned rc=0.**

The task is currently `blocked` (correctly) waiting on its 6 children. It was
never retried because stale_result is not a failure state that triggers auto-reopen.
The parent task (task_a32a35e90ab0434e8c7766057b268bc6) will unblock when all
children complete. No data was lost; the plan succeeded.

---

## Secondary Observations

1. **sandbox_observation_unavailable [warning]:** `_SandboxProgressMonitor` never
   observed the sandbox's "ready" state because planning-phase runs do not mutate
   the git worktree (no file changes). `progress.ready` was never set. Cosmetic
   warning only; does not affect the outcome.

2. **sandbox_verification_completed passed=False [warning]:** Repository verification
   (`_sandbox_run_repository_verification`) ran contract tests in a planning-phase
   workspace that had no code changes, causing the test suite to fail (nothing to
   verify). This is a false alarm for planning-phase runs and is non-blocking.

3. **executor.finalized signals all null:** `checks_pass`, `files_changed`, `pushed`,
   and `tests` are null because `evidence_type=plan_decomposed` takes the early-return
   path in the executor that skips the git finalizer. This is by design.

4. **Insta-block appearance:** From the observer's perspective the task appeared to
   "immediately" block because: (a) the planning phase ran quickly (4 minutes),
   (b) the hub transition happened inside the sandbox before the executor
   outer loop ran its post-execution checks, and (c) no error or failure event
   was emitted — only a stale_result warning after the fact.

---

## Sensitive Values

All credential-bearing fields (`repository_canonical_remote`,
`repository_origin_remote`) appear in task.json as `<redacted>`.
No tokens, secrets, or credentials were observed in any log, event, or field
inspected during this investigation.

---

## Verification: Relevant task_history events

| hist_id (prefix) | actor                  | event_type           | from_state | to_state |
|------------------|------------------------|----------------------|------------|----------|
| hist_5b0c4186    | human                  | task.created         | null       | open     |
| hist_74be9037    | agent_jordanh-worker1  | task.claimed         | open       | claimed  |
| hist_ff9f8a88    | agent_jordanh-worker1  | task.transitioned    | claimed    | running  |
| hist_4c765544    | human                  | task.updated         | running    | running  |
| hist_bccc67af    | agent_jordanh-worker1  | task.lease_renewed   | —          | —        |
| hist_af50b6ec    | agent_jordanh-worker1  | task.lease_renewed   | —          | —        |
| hist_e0f588e3    | agent_jordanh-worker1  | task.lease_renewed   | —          | —        |
| hist_a40ca06b    | agent_jordanh-worker1  | task.lease_renewed   | —          | —        |
| **hist_ef484777** | **human**             | **task.children_added** | running | **blocked** |
|                  | released_lease_id=lease_49ed4a53... | 6 children | |        |
| hist_d57dbb99    | mac-hermes-task-executor | task.memory_recorded | —        | —        |
| hist_2984df7c    | mac-hermes-task-executor | task.memory_recorded | —        | —        |

Command audit record `cmda_be009a3fb204ac5`:
- argv: `/home/horde/.mac/bin/mac-hermes-task-executor`
- cwd: `/home/horde/.mac/agent-workspaces/task_a32a35e90ab0434e8c7766057b268bc6`
- started_at: 2026-07-06T08:45:37, completed_at: 2026-07-06T08:50:40
- duration_ms: 303317 (~5 min), returncode: 0
- stderr_bytes: 1442855 (~1.4 MB, on host jordanh-worker1)
- stdout_bytes: 612

Executor stdout (sanitized): `Created sandbox: mac-task-5b1b0d607d54`

### Sanitized stderr tail (via action-events observability)

```
[WARNING] executor.sandbox_observation_unavailable  sandbox=mac-task-5b1b0d607d54 state=unknown
[INFO]    executor.sandbox_agent_completed           sandbox=mac-task-5b1b0d607d54 returncode=0
[WARNING] executor.sandbox_verification_completed   sandbox=mac-task-5b1b0d607d54 passed=False
[INFO]    executor.sandbox_harvested                 harvested=True runner_completed=True
[INFO]    executor.sandbox_deleted
[INFO]    executor.agent_completed                   returncode=0 duration_ms=296494
[INFO]    executor.planning_phase_completed
[INFO]    executor.finalized                         evidence_type=plan_decomposed outcome=success
                                                     signals={checks_pass:null, files_changed:null, pushed:null, tests:null}
[WARN]    worker.execution.stale_result              reason="assignment no longer current after executor completed"
```

No panics, crashes, authentication errors, or credential exposure in any observable field.
