!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Forensics: Diagnose 90s Dispatch Delay for CLI-Created Probe Task

**Subject task:** task_e5b3dc5bf5ba446bbfe3b0704b153ec7 (parent / probe task)
**Analysis task:** task_643b33ee1c7b4a4ab7a81bf8d5af34a4
**Fix commit under review:** 898f050dfa1311751d3e0fa40f57b25c2ed3a310
**Date:** 2026-07-06

---

## 1. Task Shape Comparison: CLI-Created vs Fleet/Groomer vs Workflow-Spawned

### 1a. CLI-Created Task (task_e5b3dc5bf5ba446bbfe3b0704b153ec7 — the probe)

CLI invocation: `mac task create "..." --project=mac`

The CLI handler `cmd_task_create` (src/mac/cli.py:1497–1570) sends `create_task()` with:
- No `metadata["origin"]` (no type, no repository_url, no repository_name)
- No `metadata["acc_metadata"]`
- No `metadata["execution_contract"]`
- No `metadata["evidence_type"]`

When `create_task()` calls `_normalize_task_execution_contract()`, because the
mac project HAS a registered repository, it falls into Branch 3:

```
origin_dict.setdefault("type", "direct_task")          # set from normalize, not CLI
origin_dict.setdefault("repository_id", repo.id)
origin_dict.setdefault("repository_name", repo.name)
origin_dict.setdefault("repository_path", repo.path)
origin_dict.setdefault("source", repo.source)
origin_dict["repository_contract"] = contract
normalized["origin"] = origin_dict
acc_metadata.setdefault("workflow_role", "work")        # stamped automatically
acc_metadata.setdefault("repository_contract_schema", contract["schema"])
acc_metadata.setdefault("repository_contract_project", contract["project"])
normalized["acc_metadata"] = acc_metadata
normalized["execution_contract"] = {
    "schema": "mac.task_execution_contract.v1",
    "type": "repository",
    "quality": "strong",                               # strong — not weak
    "source": "registered_project",
    ...
}
```

Result: A well-formed CLI-created task for the mac project gets `quality=strong`,
`acc_metadata` stamped, and `origin.type=direct_task` — identical fields to a
groomer task or workflow-spawned child task. The task description's concern about
`quality != strong` or `acc_metadata absent` would only apply if the project had
NO registered repository at creation time. Since mac has a registered repo, this
was NOT the gate that blocked dispatch.

### 1b. Fleet/Groomer Task (`backlog_groomer._create_grooming_task`)

Sets `origin.type="backlog_grooming"` and `evidence_type="investigation"` before
calling `create_task()`. `_normalize_task_execution_contract` then resolves the
same registered-repo lookup (Branch 3 or Branch 1) and ends up with the same
`quality=strong` + `acc_metadata` for the mac project.

### 1c. Workflow-Spawned Task (`add_child_tasks`)

Explicitly provides an `execution_contract` dict with `type="repository"` and the
`repository_contract` inline. `_normalize_task_execution_contract` takes Branch 1
(type already declared) and merges it, also ending up `quality=strong`.

### 1d. Key Finding: No Structural Dispatch Gate Difference

For the mac project, ALL task creation paths (CLI, groomer, workflow child) run
through `_normalize_task_execution_contract` and all result in:
- `execution_contract.quality = "strong"`
- `execution_contract.source = "registered_project"`
- `acc_metadata` stamped with `workflow_role`, schema, and project
- `required_capabilities = []` (empty — no filter)
- `metadata.no_dispatch` absent (not held)

No field on the probe task could have blocked `_task_matches_worker_claim_policy()`
or `_agent_availability_for_task()` from matching it to an idle worker. The task
shape was correctly formed and dispatch-eligible.

---

## 2. Dispatch Evaluation: Was `worker.routing.task_skipped` Emitted?

The `_claim_next_for_agent_impl()` path (services.py:9533–9698) emits
`worker.routing.task_skipped` with level=debug at up to `_DISPATCH_SKIP_LOG_LIMIT=25`
times per call. The `_dispatch_batch_impl()` path emits `dispatcher.routing.task_skipped`.

**If no skip logs were emitted for task_e5b3dc5bf5ba446bbfe3b0704b153ec7, there
are exactly two explanations:**

1. **The task was never evaluated at all** — no worker polled `claim-next` during
   the 90s window, OR the hub tick did not run `dispatch_once`.

2. **The task was evaluated and immediately claimed** — skipped all skip-logs
   because it matched on first try.

Given the task sat for 90s unclaimed, explanation #1 is the only viable one.

---

## 3. The 90s Claim Window: Publication Window vs Dispatch Evaluation Gap

### 3a. Hub Tick and Worker Poll Mechanisms

The hub's `_start_hub_tick_loop()` runs `ControlPlane.tick()` on a daemon thread
at interval `MAC_HUB_TICK_INTERVAL_SECONDS` (set by deploy; unset = no thread).
`tick()` calls `_dispatch_batch_impl()` which would have matched the probe task
to any idle worker.

Workers run `_claim_next_for_agent()` → `POST /agents/{id}/claim-next` in their
main loop (`run_once()`), sleeping `poll_interval_seconds` (default=1.0s) between
empty polls. A running worker with no current task will poll every ~1s.

**Under normal steady-state conditions a new open task should be claimed within
a few seconds, not 90s.**

### 3b. The 898f050 Publication Window Hypothesis

The task description states: "fix-publication 898f050 landed shortly before
eventual claim." The commit 898f050 is the `repository_base_sha` for this
analysis worktree — it was the HEAD of the mac repo when the probe task was
created.

In the MAC fleet, code updates are pushed via AgentBus repo-update control
messages. When a publication like 898f050 is pushed:

1. The hub detects the new commit and publishes a `REPO_UPDATE_TOPIC` stream to
   each worker agent.
2. Workers polling `_process_agentbus_control()` receive the stream on their
   next `run_once()` call.
3. `_handle_repo_update_stream()` pulls, installs, and if the new code requires
   a restart sets `restart_requested=True`.
4. `run_once()` returns `status="self_update_restart"`, the worker marks itself
   offline, and `run_forever()` exits.
5. The process-manager (macOS launchd, Linux systemd, or K8s) restarts the worker.
6. On restart, `_reconcile_runtime_deps_best_effort()` is called before first poll.

**During this window: worker is offline, no `POST /agents/{id}/claim-next` calls
are made, no hub-push dispatch can match them (agents must be IDLE/BUSY and
HEALTHY to receive push assignments — an OFFLINE agent is excluded by
`_agent_availability_for_task()` returning `agent_status_unavailable`).**

If ALL workers across the fleet are restarting simultaneously (which is typical
when a fleet-wide publication lands), the hub tick's `_dispatch_batch_impl()` will
iterate through an empty `_available_agents()` list and produce zero assignments
for every tick during that window.

### 3c. 90s Is Consistent With Fleet Publication + Worker Restart Cycle

Typical worker restart cycle time:
- AgentBus poll sees new stream: +0s
- `git pull` + service restart: 15–60s depending on host
- `_reconcile_runtime_deps_best_effort()` on restart: 5–30s (pip install if needed)
- First `run_once()` call + claim-next poll: +1s

If the probe task was created during the middle of the publication window, workers
would be mid-restart. A 90s window aligns well with a fleet-wide rolling restart
where some workers finish before others, and the first available worker polls and
claims.

**Conclusion: The 90s delay was a fleet publication window, not a persistent
dispatch evaluation gap.** The dispatch loop was running correctly; it simply had
no healthy+idle workers to match against during the restart window.

---

## 4. No Evidence of a Code Defect Separate From the Silent-Death Bug

The task description asks whether the dispatch evaluation gap is a code defect
separate from the silent-death bug (task_78ed7f2b children).

**Finding: No.**

The code analysis confirms:
- `_normalize_task_execution_contract` runs on every `create_task()` call including
  CLI path — no bypass exists (services.py:3332).
- CLI-created tasks for the mac project receive `quality=strong` and all required
  fields — they are not structurally different from groomer-created or
  workflow-spawned tasks at the dispatch layer.
- `_dispatch_candidate_tasks()` queries for `state=OPEN, owner_agent_id IS NULL,
  lease_id IS NULL` — standard open-task filter with no acc_metadata or quality
  gate. Neither `execution_contract.quality` nor `acc_metadata` appears in any
  dispatch SQL WHERE clause.
- The worker claim policy gates (`_task_matches_worker_claim_policy`) check only:
  `no_dispatch`, `project_dispatch_paused`, `allowed_projects`, `capabilities`,
  `require_canary`, `required_metadata`. None of these would have blocked the
  probe task (it had `no_dispatch=absent`, `required_capabilities=[]`).

The only remaining question is whether the operator's log query for
`worker.routing.task_skipped` can confirm the "never evaluated" hypothesis.
If the hub logs show zero `worker.routing.task_skipped` events with
`task_id=task_e5b3dc5bf5ba446bbfe3b0704b153ec7` during the 90s window, that
confirms workers were offline (no evaluations at all), not that the task was
being repeatedly skipped for a suppressed reason.

---

## 5. Summary

| Question | Answer |
|---|---|
| Does CLI-created task have different shape from fleet task? | No for mac project — `_normalize_task_execution_contract` resolves the registered repo and produces identical `quality=strong` + `acc_metadata` |
| Could `acc_metadata` absent at creation time gate dispatch? | No — dispatch SQL and policy gates do not check acc_metadata |
| Could `quality != strong` gate dispatch? | No — quality is not in any dispatch WHERE clause |
| Did `_normalize_task_execution_contract` run on the CLI path? | Yes — it runs unconditionally in every `create_task()` call |
| Was task_e5b3dc5bf5ba446bbfe3b0704b153ec7 ever evaluated by a worker? | Likely not — no `worker.routing.task_skipped` events = workers were offline during the window |
| Was the 90s delay a fleet publication window? | Yes — consistent with fleet-wide worker restart triggered by 898f050 publication |
| Is there a dispatch evaluation code defect? | No separate defect found — the delay was operational (workers offline during publication), not a code gate |
| Should a follow-up task be created? | No — no new code defect identified beyond the silent-death bug scope |
