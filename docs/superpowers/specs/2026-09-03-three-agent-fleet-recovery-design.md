# Three-agent fleet recovery design

## Purpose

Restore Rocky, Natasha, and Bullwinkle to correct end-to-end operation after
the interrupted `47440824` rollout, and remove the code paths that made
supervisor liveness look like successful recovery.

The recovery is source-first. Production host mutation follows merged,
released, and deployed fixes, except for reversible credential containment.
The operational evidence and reproduction details are recorded in
[`docs/problems.md`](../../problems.md).

## Scope

This program contains five independently reviewable source changes followed by
one deployment-and-proof phase:

1. fail-closed durable evidence redaction;
2. canonical fleet credential naming;
3. interruption-safe attestation activation and journal reconciliation;
4. live OpenShell and gateway readiness projection;
5. Linux gateway and coding-route restoration;
6. successor rollout and three independent canaries.

The source changes must not weaken signature verification, health policy,
sandbox confinement, review admission, or credential fencing.

## Approaches considered

### Source repair before host recovery

Fix the persistence and transaction boundaries, release them, then use the
corrected rollout to repair hosts. This is the selected approach because
another manual repair under the existing semantics can recreate attestation
split-brain or leak fresh credentials into evidence.

### Host recovery before source repair

Recreate sandboxes and manually align keys immediately, then repair source.
This restores apparent service sooner but repeats the non-transactional path
that caused the incident and cannot safely complete credential rotation.

### Parallel source and host repair

Operate both tracks concurrently. This shortens ideal elapsed time but makes
host evidence race changing source and authority, complicating attribution and
rollback. It is unsuitable while credentials and signing identity are in
question.

## Design

### 1. Durable evidence redaction

All executor-controlled strings must pass through one structural redaction
function before entering task history, evidence metadata, evidence artifacts,
review payloads, or error messages. The function recursively traverses JSON
objects and arrays while preserving their shape. It redacts:

- shell-style assignments whose names are credential-like;
- bearer, authorization, password, secret, token, private-key, and API-key
  fields;
- known MAC and coding-provider credential variables;
- embedded environment dumps containing those assignments.

Redaction happens at the control-plane persistence boundary even when the
worker or model claims output is already redacted. Worker-side redaction
remains defense in depth. Error handling may report the affected field path and
redaction count but never the matched value.

The implementation must avoid broad replacement of ordinary prose containing
words such as "token" or "password." Field names and assignment syntax, not
free-text keywords alone, define secret-shaped content.

### 2. Canonical fleet credential naming

The registry key (`rocky`) is a lookup alias; `fleet_name` (`mac`) is the
runtime identity. One resolver must return both values explicitly:

- registry key for locating topology and SSH routes;
- runtime fleet identity for all scoped credential names written to or read
  from workers.

Rotation, synchronization, deployment, and migration must use the runtime
identity when deriving `MAC_API_TOKEN__<FLEET>` and
`MAC_WORKER_TOKEN__<FLEET>`. Commands must fail when registry aliases resolve
to conflicting runtime identities or when an operation would update only the
alias namespace.

Migration may temporarily write both old and canonical names, but reads prefer
the canonical runtime name and emit a non-secret diagnostic for legacy
fallback. The old hub token is pruned only after all three workers prove the
new token and the predecessor fails authentication.

### 3. Attestation activation protocol

An attestation candidate must not become the worker's active evidence-signing
key before hub promotion.

Phase two writes the candidate into an owner-private pending file and generates
a possession proof from that file. The running worker continues signing with
the predecessor key. Hub `prove` verifies candidate possession without making
it authoritative. Hub `commit` promotes the candidate and retains the
predecessor for a bounded transition. Node finalization then atomically moves
the pending candidate into `mac.env`, restarts the worker, and proves that the
active key verifies against current hub authority.

Recovery rules are:

- abort before commit deletes the pending node candidate and leaves the active
  predecessor untouched;
- interruption after commit but before node finalization resumes finalization;
- `retain_forward` preserves diagnostics and pending state, never an
  uncommitted active key;
- journal discovery reconciles a local non-terminal record to an authoritative
  terminal hub receipt before opening another epoch;
- no generic attestation recovery bypasses an open epoch reservation.

The hub may accept the predecessor only for an explicit bounded transition
associated with a committed rotation. It must not accept an uncommitted
candidate or silently heal mismatched authority.

### 4. Truthful OpenShell and gateway status

Persisted deployment rows and gateway advertisements are historical
observations. They are not live readiness by themselves.

The effective OpenShell status combines:

- required policy assignment;
- a non-null named sandbox identity;
- a fresh host report within a configured bounded age;
- the current startup self-test result;
- policy and sandbox identity agreement.

If any required live input is missing, expired, or contradictory,
`effective.deployed` is false and `effective.fail_closed` is true. A startup
probe returning `sandbox not found` invalidates the matching gateway
verification projection. The system retains the historical report for
diagnosis but labels it stale rather than presenting it as current proof.

Advisory degraded health may remain dispatchable for ordinary non-gateway
work when `blocking_problems=[]`. Break-glass continues to require healthy
status. Documentation and canary tooling must choose normal target-pinned
dispatch for advisory-degraded workers instead of treating break-glass as a
general targeting mechanism.

### 5. Linux runtime restoration

After the source fixes are deployed, Natasha and Bullwinkle each receive a
fresh managed OpenClaw sandbox through the repository-owned installer. The
installer must:

1. confirm the expected sandbox is absent or invalid;
2. preserve diagnostic state without restoring corrupt or stale checkpoints;
3. create the exact configured sandbox identity;
4. run the OpenClaw sentinel inside the sandbox;
5. publish fresh ownership and runtime advertisements;
6. restart the ordinary worker supervisor;
7. establish at least one verified in-sandbox coding route.

Gateway readiness and coding-route readiness remain separate checks. Passing
the OpenClaw sentinel does not prove repository execution.

### 6. Release and proof

The fixes merge before host recovery. A successor release is deployed through
the typed cohort protocol. Normal dispatch remains contained until all proof
criteria pass.

For each agent, create a repository-free target-pinned canary that does not
print environment state. Each canary must durably record:

- agent and task identity;
- source commit and running digest;
- claim and start events;
- execution result and artifact identities;
- verification signer and accepted current key fingerprint;
- review and completion identity;
- return to idle;
- fresh health and gateway status.

At least one separate repository execution on each Linux worker must prove a
verified coding route in its task sandbox.

## Failure handling

- Redaction uncertainty fails closed before persistence and reports only the
  JSON path.
- Ambiguous fleet identity aborts rotation or deployment before writing any
  credential.
- Candidate proof failure leaves the predecessor active and the epoch open for
  bounded recovery.
- Hub commit ambiguity is resolved by querying the authoritative epoch receipt,
  never by replaying promotion blindly.
- Missing or stale sandbox evidence projects unavailable even if supervisors
  and heartbeats are live.
- Failed canaries remain failed; operators do not force review or completion.

## Verification strategy

Every source slice uses test-driven development and receives focused unit and
fault-injection tests before the full contract gate:

- nested stdout/stderr/result/manifest secret redaction, including error paths;
- alias/runtime fleet-name mismatch during rotate, sync, deploy, and migration;
- crashes before candidate install, after pending install, after prove, after
  commit, and during finalization for every cohort ordinal;
- stale local journal plus authoritative committed and aborted receipts;
- absent, stale, mismatched, and fresh OpenShell sandbox reports;
- stale gateway advertisement invalidation;
- advisory-degraded normal targeting versus healthy-only break-glass;
- Linux sentinel success with independent coding-route failure and success.

Completion requires `scripts/run-contract-tests.sh`, merged remote CI, a
successor fleet deployment receipt, predecessor credential rejection, and the
three per-agent canary receipts. Heartbeats, supervisor state, historical
`verified` flags, and a single shared canary do not satisfy completion.

## Ledger and roadmap

- Umbrella: `task_216da2b6e43b4316af8f8146e8f711e1`
- Evidence and credential containment:
  `task_e2dcfa7ebaa14478b0b2d51a45b7d79c`
- Attestation and epoch recovery:
  `task_99d9fccdb580449a81095b55a17442de`
- OpenShell, gateway, health, and Linux coding routes:
  `task_f2758902a67c4a11af432c169a1b3923`

All roadmap boxes remain unchecked until the repository completion rule is
met: merged, gated, deployed to the configured fleet, and verified live with
durable evidence.
