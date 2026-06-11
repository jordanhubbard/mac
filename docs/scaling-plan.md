# Scaling Plan

`mac` should scale from one personal fleet to many users, many Hermes
instances, and many worker pools without losing the clean control-plane shape.

## Principles

1. Keep the control plane boring.
   Durable state, permissions, routing, evidence, and audit trails should be
   easy to inspect and easy to test.

2. Keep personality out of the control plane.
   Hermes or another agent runtime owns souls, memory, conversation style, and
   adaptive behavior.

3. Make identity explicit.
   Every task that comes from a conversation should carry tenant, Hermes
   instance, persona, optional user, optional platform binding, and
   conversation reference metadata.

4. Do not copy private memory into work.
   Hermes should pass a task brief, constraints, and relevant sanitized context,
   not its raw user memory or private conversation history.

5. Share fleet capacity through policy.
   Agents and machines can serve many tenants only when capability,
   visibility, secret, and runtime policies allow it.

## Phases

### Phase 1: Local Control Plane

Status: implemented in the local prototype.

- SQLite storage.
- One FastAPI process.
- One CLI.
- Durable task/review/secret/runtime/rollout contracts.
- Hermes identity records and interaction task metadata.

This phase proves the clean contracts without operational complexity.

### Phase 2: Real Hermes Integration

Status: implemented as a thin API adapter, ready for Hermes-side wiring.

- `mac.hermes_adapter` registers tenant/persona/instance/platform bindings.
- `ConversationTaskInput` creates tasks from sanitized context.
- `GET /tasks/{id}/summary` and adapter reply rendering provide user-facing
  status summaries.
- Completed tasks can produce a Hermes memory write-back payload and record the
  operational write-back event in `mac`.

The adapter calls `mac` APIs. It does not bypass the task ledger. A production
Hermes gateway still needs to import or vendor this adapter and connect the
memory `sink` to Hermes' actual memory writer.

### Phase 3: Multi-Tenant Policy

Status: implemented as local policy gates.

Harden the identity model:

- API authentication and scoped tokens are available through `MAC_API_TOKEN`,
  `MAC_API_TOKENS`, or explicit `create_app(auth_tokens=...)`.
- Tenant-scoped task visibility uses Hermes interaction origin metadata.
- Secret leases are scoped by tenant, agent, machine policy, and capability.
- Machine pools use `labels.tenant_policy` with shared/private/denied behavior.
- Platform binding ownership checks prevent cross-tenant or cross-instance use.

### Phase 4: Fleet-Scale Dispatch

Status: implemented as deterministic local dispatch policy plus a worker
registration/run loop.

Improve dispatch without changing the core task contract:

- Worker agents can self-register machine/agent identity, heartbeat without
  claiming, or enter an executor-backed claim/start/evidence/submit loop.
- Heartbeat freshness can mark stale agents offline during `tick`.
- Agent resources can declare `capacity` / `max_concurrent_tasks`.
- Dispatch round-robins tenants inside a tick while preserving per-tenant priority.
- Task metadata can declare numeric/list/exact `resources` requirements.
- Expired leases retry until `max_attempts`; exhausted tasks appear in
  `/dispatch/dead-letters`.
- AgentBus typed streams provide ordered JSON/text/base64 chunks with durable
  reads and NDJSON tailing for high-volume agent-to-agent content exchange.

### Phase 5: Review, Evidence, and Publication Hardening

Status: implemented.

Move from prototype gates to production gates:

- Reviewer independence from current or prior task owners is enforced.
- Approved reviews must reference task evidence.
- Evidence kinds are explicit: `test`, `review`, `artifact`, `publication`, `log`.
- Default auto-review requires a `mac.worker_evidence.v1` verification manifest;
  returncode `0` without verifiable repo/deployment/artifact facts is not enough.
- Publications carry `content_hash` from publication evidence when provided.
- Tasks with `metadata.policy.require_publication_evidence` cannot publish
  without publication evidence and checksum.

### Phase 6: Rollout and Rescue

Status: implemented.

Make self-update safe:

- Runtime manifests reject `:latest`, raw secret fields, unpinned
  dependencies, and image references without `@sha256:` digests.
- Canary rollout promotion requires a passing health gate.
- Failed health checks automatically move the rollout into rescue, drop target
  percent to 0, and create an ops rescue task.
- Rollouts can target `fleet` or named tenant channels and can be filtered by
  `tenant_id` / `channel`.
- Canary start and promotion require a runtime environment plus a verified
  `sha256:<digest>` artifact.

### Phase 7: Organizational Workflows and Operator Visibility

Status: partially implemented.

Move from single-task routing to coordinated agent work:

- The role catalog is implemented, including seeded Loom-style roles, role
  assignment, persona allowlists, role capability aggregation, and role-aware
  task eligibility.
- Provisioning requests are implemented as durable records so the control plane
  can ask for missing role/capability capacity instead of silently waiting.
- Data-driven workflows are implemented as versioned DAG definitions with
  required roles, node instructions, timeout handling, YAML import, default
  seeds for bug/feature/UI/self-improvement, and immutable run snapshots.
- Workflow runs create real mac tasks, advance from task terminal states, and
  keep append-only run history. Callers cannot forge membership by setting
  `metadata.workflow_run_id`; only the `tasks.workflow_run_id` column written
  by the runtime drives callbacks.
- Observability now includes low-level metrics/logs, integration findings,
  operator notifications, command audit, and Beads `mac-ledger v1` issue
  comments for human-facing task milestones.

The main remaining Phase 7 gap is the human workflow-creation dashboard:
operators can use the API/CLI today, but the UI does not yet let a human build
a multi-step plan visually, answer all agent questions up front, manually edit
each step, and then materialize it into workflow-backed tasks.

## Current Regression Coverage

The current suite has regression coverage for these contracts:

- API calls require scoped identity for mutating routes.
- A worker cannot approve its own task.
- Completion requires an approved review with evidence.
- Two dispatchers cannot claim the same task concurrently.
- A Hermes interaction task cannot use a user or platform binding from another tenant.
- Secret handles cannot be revealed without a granted audit.
- Runtime manifests reject unpinned images and version ranges.
- Rollouts cannot install or promote without verified artifacts and health gates.
- Worker registration and dispatch run both in-process and through an
  end-to-end uvicorn plus `mac-agent` subprocess path against a real SQLite DB.
- Beads bridge imports use `bd ready --json` as canonical, detect JSONL drift,
  bootstrap/pull registered repositories, and isolate bridge polling in managed
  checkouts.
- The default review workflow requires verifiable executor evidence, an
  independent reviewer, signed verdict evidence, and publication evidence when
  policy demands it.
- Command audit, integration findings, operator notifications, Beads ledger
  comments, and deploy-token redaction have regression tests.
- Workflow definitions, YAML import, seed loading, run advancement, cancellation,
  timeout ticks, and forged metadata rejection are covered.

High-value remaining tests are mostly deployment matrix tests: macOS/Linux/WSL2
fresh-host bootstrap for registered projects, multi-agent workflow runs over a
live hub, and UI-driven workflow authoring once that UI exists.

## Success Definition

A user can talk to a remembered Hermes identity. Hermes can turn intent into a
durable `mac` task. The fleet can execute, review, and publish the work. Hermes
can report the outcome in the user's conversation and update its own memory.

At no point should private personality memory become a fleet-wide operational
secret, and at no point should fleet work depend on unrecorded chat state.
