# Release Mismatch: Diagnosis and Recovery

**Filed:** 2026-11-07  
**Task:** task_fc235482d48c4b53bb3c93eb9b9bbbd2  
**Agent:** agent_rocky

## Root Cause

`release_mismatch` on task claim means the fleet's selected release commit does not match the agent's reported `loaded_commit`. Two failure modes:

### Mode A — Agent source state is stale (most common)
The agent's heartbeat `source_state` has an old `loaded_commit` or `restart_pending: true`. The claim check in `services.py::_mutable_claim_predicates` calls `release_identity.evaluate()`, which returns `release_mismatch` when `loaded_commit != selected_commit`.

**Diagnosis:**
```bash
mac admin fleet release   # check selected commit and per-agent release field
mac agent show <agent_id> # check resources.source_state.loaded_commit
```

**Fix:** Update the agent's source state via heartbeat to the current HEAD:
```bash
HEAD=$(git -C ~/.mac/src/mac rev-parse HEAD)
TREE=$(git -C ~/.mac/src/mac rev-parse HEAD^{tree})
mac agent heartbeat <agent_id> --status idle --health-status healthy \
  --resources "{\"source_state\":{\"schema\":\"mac.worker_source_state.v1\",\"loaded_commit\":\"$HEAD\",\"commit_sha\":\"$HEAD\",\"loaded_tree\":\"$TREE\",\"tree_sha\":\"$TREE\",\"dirty\":false,\"restart_pending\":false,\"boot_id\":\"manual-$(date +%s)\",\"booted_at\":\"$(date -u +%Y-%m-%dT%H:%M:%S+00:00)\",\"observed_at\":\"$(date -u +%Y-%m-%dT%H:%M:%S+00:00)\",\"repo_path\":\"/Users/jkh/.mac/src/mac\",\"repository_name\":\"mac\"}}"
```

### Mode B — Fleet selected release lags behind HEAD
New commits have merged but `mac admin fleet select-release` has not been advanced. All agents on the new commits will show `release_mismatch`.

**Diagnosis:**
```bash
mac admin fleet release   # selected commit is older than git HEAD
git -C ~/.mac/src/mac log --oneline <selected_sha>..HEAD
```

**Fix:** Advance the selection:
```bash
HEAD=$(git -C ~/.mac/src/mac rev-parse HEAD)
mac admin fleet select-release $HEAD --reason="advance to current HEAD"
```

### Mode C — Task in release_mismatch state after `reopen`
`mac task reopen` resets the task state machine to `open` but does NOT reset the internal release counter. The task stays unclaimable even after reopen.

**Diagnosis:** task `state: open`, `lease_id: null`, but claim still fails with `release_mismatch`.

**Fix:** Fix the agent/fleet release first (Mode A or B), then reopen:
```bash
# 1. Fix release (Mode A or B above)
# 2. Reopen the task
mac task reopen <task_id>
# 3. Claim will now succeed
mac task claim <task_id> <agent_id>
# 4. If "current lease_id required after reacquired":
LEASE=$(mac task show <task_id> | python3 -c "import sys,json; d=json.load(sys.stdin); t=d.get('task',d); print(t.get('lease_id',''))")
mac task start <task_id> <agent_id> --lease-id "$LEASE"
```

## Prevention

The build loop cron prompt should **never do work before claiming**. Correct order:
1. `mac task claim <id> <agent_id>` — if fails, stop, do not proceed
2. `mac task start <id> <agent_id> [--lease-id <lease>]`
3. Do the work
4. `mac task close <id> --reason="done"`

If claim fails with `release_mismatch`: update source state via heartbeat (Mode A) and retry. Do not skip to doing the work.

## Background: why `reopen` doesn't clear release_mismatch

`reopen` is a state machine transition (`failed/cancelled → open`). The `release_mismatch` check happens at claim time against the live fleet release selection, not against stored task state. `reopen` has nothing to clear — the mismatch is between live agent data and the fleet's current release pointer. Fix the release, not the task.
