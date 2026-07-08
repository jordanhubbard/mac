# Break-glass host recovery

Some infrastructure tasks cannot repair the environment from inside that same
environment's sandbox. Examples include repairing OpenShell itself, replacing a
broken worker launcher, correcting host-only routing or credential projection,
and deploying a fix to the executor that would otherwise run the fix.

MAC represents these as normal ledger tasks with a separate, durable
`mac.break_glass_authorization.v1` record. Ordinary task metadata cannot select
host execution.

## Safety contract

- The authorization API requires an explicitly admin-scoped principal.
- One authorization binds one open task to one exact healthy agent on a trusted
  machine.
- The unclaimed authorization expires after 60–3600 seconds and is single-use.
- Claiming atomically binds the authorization to the exact lease. Replays with a
  different task, agent, or lease fail closed in the executor.
- The authorization may bypass a task hold, project pause, worker claim lane,
  agent dispatch hold, normal task/agent compatibility checks, and OpenShell
  only for that task/agent pair. Host trust, health, capacity, tenant isolation,
  evidence, review, and push gates remain.
- Authorization, claim, revocation, command execution, and final evidence are
  auditable. Secrets must never be placed in the reason or task description.

## Operator workflow

Create or identify the recovery task, leaving it staged if desired:

```bash
mac task create "Repair the OpenShell worker launcher" --project=mac --no-dispatch
```

Verify the target host and its coding route outside the broken sandbox, then
authorize the exact pair:

```bash
mac task break-glass <task_id> <agent_id> \
  --reason="OpenShell launcher must be repaired from its trusted host" \
  --ttl-seconds=900
```

The loop worker can claim that task even while the task and agent remain held.
No broad `mac agent resume` is required. Inspect or revoke before claim with:

```bash
mac task break-glass-list <task_id>
mac task break-glass-revoke <authorization_id> --reason="Recovery no longer required"
```

Once claimed, the authorization is bound to the running lease and cannot be
revoked as if it had never started. Use the normal task cancellation and worker
containment controls if an executing recovery must be stopped.

After a claim, the task runs through the normal host task executor but OpenShell
wrapping is disabled for that lease. The prompt explicitly identifies the host
recovery boundary. Normal deterministic evidence and review still apply.

## Referencing host work from sandboxed work

A host recovery task remains an ordinary MAC task, so sandboxed tasks can depend
on it through normal task dependencies. A downstream task stays blocked until
the host task is reviewed and completed; it then executes under the ordinary
sandbox boundary. The privileged execution boundary is not inherited through
dependencies.

## Recovery invariant

Never clear every fleet hold merely to repair the hold mechanism. Verify one
host, authorize only the recovery task on that host, repair and validate the
execution environment, and clear ordinary holds only after end-to-end probes
pass.
