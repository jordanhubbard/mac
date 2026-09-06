# Fleet recovery: problems, evidence, and the path to readiness

This document is the operational record for the long recovery attempt that
followed an interrupted deploy of the `mac` fleet. It is deliberately candid:
"a supervisor is running" is not treated as equivalent to "the worker can
complete work." The goal is to make the next recovery repeatable and to state
the exact evidence required before declaring the fleet ready.

Last updated: 2026-09-03.

## Scope and target state

The fleet under discussion has three registered workers:

| Worker | Host class | Supervisor | Intended role |
| --- | --- | --- | --- |
| Rocky | macOS hub/worker | launchd | hub plus worker/gateway |
| Natasha | Linux worker | systemd | GPU-capable worker |
| Bullwinkle | Linux worker | systemd | GPU-capable worker |

The target source revision during this recovery was
`474408245fe2cc33fd22c6f9420fd78fcd87d4ef` (`47440824`), the current release
tip at the time. The desired end state is not merely that these hosts answer
SSH or have processes. It is that each named current worker can accept a
targeted, read-only canary, execute it with its normal worker credentials,
produce an evidence manifest that the hub validates, pass normal review, and
return to an idle healthy state.

The HGX runners discussed separately are disposable, **fungible** capacity.
They are not part of the three-worker identity-preserving recovery above until
they exist, have immutable HGX session IDs, and are reconciled into the fleet
registry.

## Executive status

At the latest live check:

| Worker | Hub status | Health | Heartbeat | Canary conclusion |
| --- | --- | --- | --- | --- |
| Rocky | idle | healthy | live | Executed a canary, but hub rejected its evidence signature. Not proven. |
| Natasha | idle | degraded | live | Cannot receive a targeted break-glass canary while unhealthy. Not proven. |
| Bullwinkle | idle | degraded | live | Cannot receive a targeted break-glass canary while unhealthy. Not proven. |

Therefore the accurate statement is: **all three current supervisors and
heartbeats are live, but none of the three has a completed, hub-verified
end-to-end canary receipt.**

## Deep-dive root causes

Subsequent implementation and live-state inspection proved that the recovery
is blocked by multiple independent authority failures. Repeating supervisor
restarts or canaries cannot repair them.

### Attestation authority split after an interrupted phase two

The fleet deploy installs each staged attestation candidate into the node's
`mac.env` and restarts the worker before the hub commits the epoch. The hub
continues to verify evidence with the predecessor key until commit. If phase
two is interrupted and recovery chooses `retain_forward`, the candidate stays
active on the node while hub abort deletes the staged candidate and retains the
predecessor as authoritative.

This is the exact Rocky failure. The fingerprint of the key used by Rocky's
failed canary matched Rocky's candidate in aborted epoch
`474408245fe2cc33fd22c6f9420fd78fcd87d4ef:20260903T032757Z:4e24978af16255192533a7c23db2764c`.
The existing `reconcile_bound_worker_attestation_key` deploy helper implements
the necessary probe/recover/install/re-prove shape but is not called by the
deployment workflow. Hub retained-key verification does not cover this
direction of divergence: it tolerates evidence signed before a completed
rotation, not evidence signed by an uncommitted candidate that is ahead of hub
authority.

The later local cohort journal for epoch
`474408245fe2cc33fd22c6f9420fd78fcd87d4ef:20260903T040051Z:c4620df9a042ed4fbda70f489130638e`
is stale at `phase=quiesced` and `hub_state=open`. The authoritative hub receipt
records that epoch as aborted at `2026-09-03T04:06:39.351183Z`. It did not reach
phase two, so its three different staged candidates are not the source of the
current key split, but the journal must be reconciled before another rollout.

### Missing live OpenClaw sandboxes hidden by stale projections

Natasha and Bullwinkle both report the same current executable failure:

```text
code: 'Some requested entity was not found', message: "sandbox not found"
```

The deployed self-test correctly classifies this runtime probe failure as
non-blocking, leaving the worker process alive and normally dispatch-capable
under advisory health rules. It does not mean the advertised gateway works.
The hub's `chat_gateway` projection still reports each missing sandbox as
`verified=true` based on the earlier deployment, while `mac admin openshell
status` reports historical deployment rows as active with `sandbox_id=null`.
`OpenShellService.get_agent_status` derives `deployed=true` and
`fail_closed=false` solely from the persisted status string; it does not check
report age, sandbox identity, or the current startup probe. These are stale
control-plane projections, not evidence of a live gateway.

### Conflicting health predicates block the chosen canary path

Normal allocation accepts `health_status=degraded` when the startup report is
`degraded` with no blocking problems. Break-glass authorization instead
requires the raw health status to equal `healthy`. Consequently Natasha and
Bullwinkle are eligible for normal targeted dispatch but cannot receive the
break-glass canaries originally chosen for contained recovery. This is a policy
asymmetry, not evidence that either gateway is healthy.

### Coding execution is also unproven

Rocky currently has verified Claude and OpenCode routes, while its Cursor and
Codex host probes fail. Natasha and Bullwinkle have no verified coding CLI:
their configured Claude/Cursor probes fail, Natasha's Codex probe fails, and
Bullwinkle's Codex provider returns a server error. A gateway-only sentinel
therefore cannot establish that either Linux worker can complete the normal
repository execution contract.

### Evidence persistence crossed the secret boundary

One Rocky canary copied the worker process environment into durable
`metadata.verification.result`, including credential values, despite
`HERMES_REDACT_SECRETS=true`. The task and cascading evidence/artifacts were
deleted from the authoritative hub. A replacement hub token has been staged
with an overlap window, but the old value must remain accepted until every
worker receives the replacement; other exposed authorities still require
owner-side rotation. Prompt-level redaction is not a security boundary.
Follow-up task: `task_e2dcfa7ebaa14478b0b2d51a45b7d79c` (P0, held).

The overlap cannot yet be pruned safely because token scoping has two names for
the same fleet: the registry key is `rocky`, while its configured `fleet_name`
and worker runtime identity are `mac`. `rotate-token --fleet rocky` updated
`MAC_API_TOKEN__ROCKY`; deployed workers resolve `MAC_WORKER_TOKEN__MAC` and
the deployment helpers prefer `MAC_API_TOKEN__MAC`. Rotation and rollout must
derive one canonical suffix from the same immutable fleet identity before the
old token is revoked.

## Timeline of what was observed and attempted

### 1. Interrupted release and retained successor state

The normal fleet rollout was initially attempted against `47440824`. Two
independent failure modes appeared:

1. Gateway/startup timing exceeded the original 90-second command budget even
   though Rocky later became ready. Local deployment timeout overrides were
   increased to 300 seconds, including the OpenClaw startup verification
   timeout.
2. A subsequent rollout stalled in phase two after creating a small supervisor
   backup and before recording a completion journal. The retained successor
   epoch was left non-terminal rather than being rolled back automatically.

This was handled as a fix-forward incident. The failed newest generation and
its diagnostics were retained; normal dispatch was held. No automatic rollback
was performed. That is intentional: a rollback would erase the evidence needed
to repair the cutover and can itself make fleet identity/credential state less
clear.

### 2. Initial worker liveness failure

All three workers were reported offline or otherwise unavailable. Direct,
authoritative host checks established:

* Rocky's `com.mac.agent` launchd job was not loaded.
* Natasha's `mac-agent.service` was inactive.
* Bullwinkle's `mac-agent.service` was inactive.
* The installed source on each host was already `47440824`; this was not a
  stale-source or failed-git-update problem.

The supervisors were restarted under explicit user-authorized break-glass.
Natasha began heartbeating immediately. Rocky and Bullwinkle repeatedly exited
their startup self-tests.

### 3. Worker credential drift

Rocky and Bullwinkle logs both showed the same concrete authentication failure:

```text
HTTP 403: Forbidden
unknown bearer token
```

Their older flat `MAC_WORKER_TOKEN` values no longer authenticated with the
hub. The intended per-worker bound-credential issuance path was fenced by the
retained open release epoch, so issuing a new ordinary credential was correctly
refused. The recovery used the documented out-of-band fleet token
resynchronization path over SSH, then installed a temporary fleet-scoped
compatibility alias for `mac` so precedence could not select the stale flat
worker token.

Important details:

* Secrets were transferred through stdin and never intentionally printed or
  placed in command arguments.
* This restored liveness; it is not a substitute for completing the ordinary
  bound-worker credential promotion once the retained epoch is repaired.
* The root cause was a namespace/precedence mismatch: the fleet configuration
  name (`rocky`) and the worker fleet name (`mac`) select different scoped
  environment variable suffixes. A valid value under one suffix did not
  override a stale value under the other.

### 4. Rocky OpenClaw gateway recovery

After authentication was fixed, Rocky's strict agent startup guard exposed the
next blocking condition:

```text
OpenClaw runtime advertisement is missing or has the wrong implementation
OpenClaw gateway ownership proof is missing
```

The gateway LaunchAgent existed but was unloaded. Bootstrapping and starting it
regenerated an artifact, but the artifact initially contained a failed startup
report rather than a valid runtime advertisement. Investigation then found the
more fundamental fault inside the OpenShell gateway sandbox:

```text
database disk image is malformed
Failed to open the plugin state database
```

The affected SQLite database was preserved in Rocky's OpenClaw archive before
recovery. A first clean-recreation attempt did not solve the problem because a
graceful shutdown/checkpoint race copied the corrupted database back into host
state. The successful sequence was:

1. Preserve the corrupt sandbox and host database copies in the OpenClaw
   archive.
2. Stop the gateway without allowing the corrupt checkpoint to overwrite the
   freshly cleared state.
3. Delete only the managed OpenShell sandbox.
4. Move the host-side corrupted SQLite state out of the active state tree.
5. Re-bootstrap the existing gateway LaunchAgent and let it create a fresh
   sandbox state database.
6. Verify that the in-sandbox OpenClaw CLI could run.
7. Run the repository-owned OpenClaw `verify` then `finalize` sequence.

The final `verify -> finalize` step was essential. It published the valid
runtime advertisement only after exclusive gateway ownership had been proven.
After this, Rocky's agent startup self-test passed and Rocky resumed healthy
heartbeats.

### 5. Current liveness result

The resulting state was verified through the hub and host supervisors:

* Rocky: launchd agent running, healthy, idle, heartbeating.
* Natasha: systemd agent running, idle, heartbeating, but degraded.
* Bullwinkle: systemd agent running, idle, heartbeating, but degraded.

The earlier safety hold was retained during the immediate recovery. Later live
status showed the workers no longer individually marked dispatch-held, but
normal task dispatch should still not be interpreted as approved until the
canary criteria below pass.

## Canary attempt and the evidence it produced

The recovery needed stronger proof than a heartbeat, so three read-only,
operator-result canaries were staged—one each for Rocky, Natasha, and
Bullwinkle. They were intentionally:

* no-dispatch tasks;
* repository-free operator directives (no source edit and no repository gate);
* manually bound to one exact target at a time;
* authorized with narrow, time-limited break-glass only where the hub considered
  the target healthy.

An initial repository-scoped canary was cancelled before execution because it
received a code-change contract. That was the correct cancellation: a health
canary must not be forced through a repository mutation workflow.

### Rocky canary: execution succeeded; review admission failed

Rocky claimed and ran its operator-result canary. The worker produced durable
result, stdout/stderr, and evidence-manifest artifacts. The failure occurred
at hub review admission, not at host execution:

```text
task needs verifiable evidence before review:
verification.signature does not verify against signed_by's attestation key
```

The task was blocked and then cancelled after preserving the diagnostic.
Consequences:

* Rocky can execute a read-only workload and communicate with the hub.
* Rocky cannot yet prove an end-to-end task because the hub rejects the
  worker's signed evidence.
* Do not force-complete this canary. Doing so would manufacture the very proof
  the exercise was intended to test.

Follow-up task: `task_99d9fccdb580449a81095b55a17442de` (held).

### Natasha and Bullwinkle canaries: blocked before assignment

Natasha and Bullwinkle were live but had `health_status=degraded`. Their
startup self-test reports contained:

```text
OpenClaw agent self-test exited 1
```

The break-glass API refused to authorize a task to an unhealthy target. That
is an intentional safety property, not a scheduler bug. The two staged
canaries were cancelled rather than bypassing health policy with an arbitrary
host command.

Follow-up task: `task_f2758902a67c4a11af432c169a1b3923` (held).

## HGX capacity attempt

Two HGX workers were planned as fungible extra capacity. No HGX runner was
created.

Observed sequence:

1. `hgx list` reported an expired/revoked login.
2. `hgx login` was attempted against the default OV Next endpoint.
3. The browser flow opened, but NVIDIA Entra rejected token issuance with
   `AADSTS53003` (Conditional Access).
4. A second requested login retry had the same result.
5. `hgx doctor` found a stale local token file which the server rejected.
6. The stale cached login was cleared and a clean login was attempted; Entra
   again rejected it with `AADSTS53003`.
7. The user chose to stop HGX provisioning for now.

The HGX rollout task records the three attempts and their failure class. Do not
continue to retry the same endpoint until the Conditional Access policy permits
token issuance. When access is restored, create exactly two fungible sessions,
record their immutable HGX session IDs, deploy the current source, and reconcile
their endpoints/identities into `~/.mac/fleets.yaml` before relying on them.

## Things that did not work, and why

| Attempt | Result | Why it was insufficient or failed |
| --- | --- | --- |
| Increasing only the original deploy timeout | Insufficient | A later phase-two stall remained after the gateway timing issue. |
| Re-running normal deploy over the retained open epoch | Avoided | The epoch fenced credential changes and was non-terminal; repeating it would obscure diagnosis. |
| Restarting agent supervisors alone | Partial | It exposed authentication drift and strict startup failures but did not repair them. |
| Using stale flat worker credentials | Failed | Hub returned `403 unknown bearer token`. |
| Issuing ordinary new worker credentials | Correctly refused | The open release epoch reserved the worker credential state. |
| First Rocky sandbox recreation | Failed | Shutdown checkpoint race restored corrupt SQLite state. |
| Starting Rocky gateway without finalization | Insufficient | Runtime/ownership advertisement was missing, so the strict agent guard exited. |
| Repository-scoped health canary | Cancelled before execution | It acquired a repository code-change contract, unsuitable for a read-only proof. |
| Rocky operator-result canary | Blocked after execution | Evidence signature did not verify against the registered attestation key. |
| Direct canaries on Natasha/Bullwinkle | Correctly refused | Their health status is degraded. |
| HGX login/retry/cache reset | Blocked externally | NVIDIA Entra Conditional Access denied token issuance. |

## What has not yet been done

The following work is outstanding and must not be silently assumed complete:

1. Repair the retained release epoch and return worker credentials from the
   temporary compatibility recovery path to the intended per-agent bound
   credential lifecycle.
2. Reconcile the stale local cohort journal with the hub's authoritative
   aborted receipt.
3. Repair node/hub attestation authority on all three workers and fix
   retain-forward recovery so an aborted candidate cannot remain active on a
   node after the hub discards it.
4. Recreate and verify the missing Natasha and Bullwinkle OpenClaw sandboxes,
   replace stale gateway/OpenShell projections, and restore healthy status.
5. Establish at least one verified coding CLI route on each Linux worker.
6. Re-run one targeted read-only canary for each current worker and let each
   complete normal evidence verification and review.
7. Decide whether to keep normal dispatch held until all three canaries pass;
   the safe default is yes.
8. Complete credential-exposure response: rotate every exposed authority,
   reject old values, audit other evidence, and add structural redaction before
   persistence.
9. Obtain valid HGX authentication before creating the two optional fungible
   runners.
10. After any source/configuration repair, make a successor release and deploy
   it via the normal fix-forward procedure; do not claim a manual host repair
   is a released fleet state.

## Recipe for success

The recovery is complete only after all gates below are true, in order.

### A. Repair control-plane acceptance

1. Reproduce the Rocky evidence signature failure using the held remediation
   task.
2. Verify the worker's signing identity, its current attestation key, and the
   hub's registered key are the same expected principal.
3. Fix the mismatch with normal source/configuration changes, tests, review,
   merge, release, and deployment.
4. Confirm a fresh Rocky operator-result canary reaches normal review without
   break-glass completion.

### B. Repair worker health

1. On Natasha and Bullwinkle, inspect the complete OpenClaw startup report and
   gateway logs, redacting credentials.
2. Recreate the named managed sandbox and prove the OpenClaw sentinel from
   inside it; do not accept a historical `verified` advertisement or active
   OpenShell row as proof.
3. Publish a fresh gateway ownership/runtime advertisement tied to the live
   sandbox identity.
4. Correct the stale OpenShell status projection so absent or expired
   sandboxes fail closed.
5. Restart each ordinary supervisor and wait for fresh heartbeats that report
   `health_status=healthy`.
6. Establish a verified coding CLI route and confirm the normal repository
   executor can select it.
7. Confirm a health gate can authorize each one normally.

### C. Establish per-worker end-to-end proof

For Rocky, Natasha, and Bullwinkle separately:

1. Create a no-dispatch, repository-free read-only canary.
2. Bind it to the intended agent using the explicit targeted-task mechanism.
3. Use narrow break-glass only if normal dispatch is intentionally still held;
   never use it to bypass an unhealthy worker.
4. Verify claim, start, worker execution, artifact upload, signature
   validation, review, and completion.
5. Record the completed task ID, agent ID, source revision, worker digest,
   evidence ID, review ID, and completion time in the release evidence.

Completion of three isolated canaries is the minimum proof that all current
workers can perform an end-to-end task. A green SSH probe, a systemd/launchd
`running` state, or a heartbeat is necessary but not sufficient.

### D. Restore ordinary fleet operation

1. Confirm no retained epoch or credential reservation still blocks normal
   lifecycle operations.
2. Publish a successor release carrying the fixes and documentation update.
3. Deploy that release to all three workers.
4. Verify source commit and running digest on every worker.
5. Run the three targeted canaries again if deployment changed any affected
   runtime or credential path.
6. Only then release normal dispatch and observe at least one ordinary task
   through completion under the normal gate.

### E. Optional HGX expansion

After Conditional Access is repaired:

1. Authenticate once with `hgx login` and verify `hgx list` succeeds.
2. Create two fungible workers—not identity-preserving permanent members.
3. Resolve each by immutable session ID, deploy the current released source,
   and record the resulting agent identity and endpoint in the fleet registry.
4. Run the same end-to-end canary sequence on each before exposing either to
   general dispatch.

## Operational principles retained from this incident

* Fix forward. Preserve a failed newest generation with diagnostic evidence;
  rollback is break-glass, not the normal response.
* Keep normal dispatch contained until proof exists. A live process is not a
  passing worker.
* Do not use a broad hub credential as a long-term worker-identity solution.
  It was a recovery bridge, not the intended steady state.
* Do not force-complete tests whose purpose is to validate a guardrail.
* Treat agent health gates and evidence signature validation as useful signals;
  repair the condition they detect instead of weakening the gate.
* Treat HGX workers as fungible only after their actual provider session and
  fleet registration have been reconciled.

