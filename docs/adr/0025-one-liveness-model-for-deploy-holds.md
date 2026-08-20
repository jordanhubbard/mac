# ADR 0025 - One liveness model for the deploy path; every hold names its owner

- Status: **Proposed**
- Date: 2026-08-20
- Decision owner: MAC fleet owner
- Related: ADR 0013 (one authoritative hub allocator), ADR 0015 (macOS nodes are
  host installs), ADR 0020 (a running task is not editable; stop it first),
  ADR 0021 (schema changes need versioned migrations), ADR 0022 (a gate returns
  a named decision, not a boolean)
- Scope: this ADR settles the rules. It changes no deploy code. Every rule below
  names the file and function that must implement it; those are separate
  changes.

## Context

A fleet deploy is guarded by six independent mutual-exclusion mechanisms. On
2026-08-11 they interacted to produce a state no single mechanism considered
illegal and none could exit, and the fleet stayed blocked for nine days.

### The six mechanisms, as they exist today

| # | Where | Mechanism | Identity it records | Ages on |
|---|---|---|---|---|
| 1 | controller | `.cohort-transaction.lock` flock on the journal dir (`deploy/fleet-cohort-transaction.py:103`) | none — flock is held by the live process | process exit (kernel) |
| 2 | controller | journal `owner` record (`fleet-cohort-transaction.py:142`, written by `_owner` at `:475`) | nonce, pid, `boot_id_sha256`, `process_start_sha256`, `acquired_at` | liveness probe `_owner_alive` (`:463`) |
| 3 | node | `~/.mac/deploy-controller.guard` flock around the acquisition critical section (`deploy/deploy-mac-fleet.sh:6839-6841`) | none | process exit (kernel) |
| 4 | node | `~/.mac/deploy-controller.lock` (atomic `mkdir`) + `owner.json` (`deploy-mac-fleet.sh:6853-6885`) | **`deployment_id` only** | wall clock, `MAC_DEPLOY_STALE_LOCK_SECONDS` (default 3600) gated by `MAC_DEPLOY_TAKEOVER_STALE_LOCK` |
| 5 | transport | ssh control sockets + pinned `.route` files (`deploy-mac-fleet.sh:1484`, `:1490`, `start_ssh_control_master` at `:1538`) | route args | never (file presence short-circuits at `:1545-1551`) |
| 6 | hub | per-agent `dispatch_hold` (`src/mac/dispatch.py:1379` set, `:1387` clear; `src/mac/models.py:1520-1522`) | reason string, timestamp | never — set and cleared explicitly, no TTL |

**Both sides are justified.** A controller-side lock cannot exclude a deploy
launched from a different workstation, because that deploy's journal directory
is on a different machine. Per-node ownership is required. The problem is not
duplication — it is that six holds age by five different rules, and three of
them do not age at all.

### The defects

**1. Incompatible staleness models, and one of them has nothing to work with.**
Mechanism 2 ages on evidence: `_owner_alive` (`fleet-cohort-transaction.py:463`)
checks that the pid is live *and* that `boot_id_sha256` and
`process_start_sha256` still match, so a reboot or a pid reuse reads as dead
immediately and correctly. Mechanism 4 ages on wall clock alone, because its
`owner.json` payload (`deploy-mac-fleet.sh:6880-6885`) contains exactly four
fields:

    {"schema": "mac.deploy_controller_lock.v1", "deployment_id": ...,
     "created_at_epoch": ..., "renewed_at_epoch": ...}

There is no pid, no host, no boot id. The node lock does not fail to reconcile
with the journal owner because the two disagree about policy; it cannot
reconcile because **it records nothing that liveness could be computed from**.
That is the root cause, and it is a missing field before it is a missing rule.

**2. Recovery cannot take the lock it exists to release.** Both durable-recovery
paths call the acquisition helper with takeover pinned off —
`deploy-mac-fleet.sh:13326` (cohort abort recovery) and `:13765` (committed
finalization), the first carrying the comment:

> Never use ambient stale-takeover authority during recovery. A successor
> deployment lock is proof that this controller no longer owns the node.

The reasoning is correct for a **live** successor and wrong for a **dead** one.
Recovery passes the *original* `deployment_id`, so the fast path at
`deploy-mac-fleet.sh:6862-6864` (`if current.get('deployment_id') ==
deployment_id: raise SystemExit(0)`) only helps when nothing else has taken the
node. Once a successor deployment has written its own `deployment_id` and then
died, the lock is held by a corpse, and the single code path whose purpose is to
release it is the one path forbidden from breaking it. That is the deadlock.

**3. Nothing expires.** No journal is ever deleted — the only `unlink` in
`fleet-cohort-transaction.py` is the temporary file in the atomic-write path at
`:1630`. The incident directory held 634 journals back to 2026-07-19. This is
not merely untidy: both `command_init` (`:2725-2729`) and `command_discover`
(`:3859-3863`) re-verify every journal's hub-open, hub-prove and release plan
paths on **every** invocation, so retained history is an O(n) tax on each
deploy. Node locks (mechanism 4) have no TTL, and hub dispatch holds (mechanism
6) have neither TTL nor owner. Same shape as the agent TTL work
(`task_9ce412c4`) and the review graveyard.

**4. The error names the symptom, not the lock.** Nine days of blockage surfaced
as `jordanh-worker5: authoritative SSH route resolved empty`
(`deploy-mac-fleet.sh:1554`) — a transport message emitted while the actual
cause was a held epoch. Three unrelated remedies were applied before any message
named an epoch.

**5. Serial attempts compound — but not the way the incident report assumed.**
Worth correcting the record, because it changes the fix: `command_init` already
refuses to create a second incomplete epoch, raising `active_epoch_conflict`
(`fleet-cohort-transaction.py:2751-2757`), and `command_discover` raises
`multiple_active_epochs` if it ever finds two (`:3867-3869`). Parallel
non-terminal epochs are therefore not what accumulated. What accumulated is
(a) a growing pile of retained terminal journals, each re-validated on every
call, and (b) rollback candidates within the one live epoch slot, since
`_rollback_candidates` grows with every node the failed attempt touched and
`command_abort` refuses to terminate while any remain (`:3780-3783`). The single
active-epoch slot then has to be cleared before *any* new deploy can begin, so
each failed attempt made the next one strictly harder.

**6. No honest terminal path for an absent node.** `command_abort`
(`:3788-3791`) requires every cohort node to be in `{planned, route_bound,
aborted}`, and reaching `aborted` requires recovery evidence bound to the node
(`command_aborted_node`, `:3266`). When a cohort member's host no longer exists,
no truthful evidence can be produced, so the only options are to fabricate proof
or bypass the journal. The hub level already solved exactly this:
`hub-orphaned --absence-file` (`:3495`, `:4134`) accepts a receipt validated by
`_absence_receipt` (`:3343`), which binds schema, `status == "absent"`,
`epoch_id`, and the hub authority id digest recorded when the epoch opened, and
explicitly refuses `mismatch`. Nodes need the same and do not have it.

## Decision

### 1. One liveness model, three-valued. Age is an input, never an authority.

Every deploy hold is classified by one shared function into exactly one of:

- `live` — the holder is demonstrably running.
- `provably_dead` — the holder cannot be running, on evidence, not elapsed time.
- `indeterminate` — neither can be shown. **This includes "old".**

Wall-clock age never produces `provably_dead`. Age may make a hold *reportable*
as expired and may gate an explicit operator override, but it is not evidence of
death and does not authorise a break on its own. `MAC_DEPLOY_STALE_LOCK_SECONDS`
keeps its current meaning under this rule and loses its promotion to authority.

### 2. Every hold records the same identity block

Introduce `mac.deploy_holder_identity.v1`, carrying the fields the journal owner
already has plus the host binding it lacks a use for and the node lock lacks
entirely:

    {"nonce", "host_identity_sha256", "pid", "boot_id_sha256",
     "process_start_sha256", "epoch_id", "deployment_id",
     "acquired_at", "renewed_at"}

- Mechanism 2 (journal owner) gains `host_identity_sha256`, `epoch_id` and
  `deployment_id`; its existing fields keep their current meanings.
- Mechanism 4 (`owner.json`) is versioned to
  `mac.deploy_controller_lock.v2` and embeds the block. Per ADR 0021 this is a
  versioned migration, not an append: readers accept v1 and v2, and **a v1 lock
  is classified `indeterminate` by definition** — which is precisely today's
  behaviour, so no deploy regresses while v1 locks remain in the field.
- Mechanism 6 (hub `dispatch_hold`) gains the same block, so a hold can name the
  epoch that placed it and be reconciled when that epoch goes terminal.
- Mechanisms 1, 3 and 5 are unchanged. Flocks are already exact — the kernel
  releases them on process exit — and a pinned `.route` file is a cache, not a
  hold. `start_ssh_control_master` (`:1545-1551`) must be reachable only *after*
  the epoch preflight in rule 6.

### 3. What counts as proof of death

`provably_dead` requires one of the following, and nothing else qualifies:

- **Local probe.** The classifier runs on the host named by
  `host_identity_sha256` and `_owner_alive` semantics fail there: the pid is
  gone, or `boot_id_sha256` differs (the host rebooted), or
  `process_start_sha256` differs (the pid was reused). This is the existing
  check at `fleet-cohort-transaction.py:463-472` and it is already correct.
- **Journal-backed proof.** The classifier holds the journal for the `epoch_id`
  named in the hold, that journal's owner is `provably_dead` by local probe on
  the journal's own host, and the hold's `deployment_id` belongs to that epoch.
- **Authority-attested absence.** A receipt under rule 5 proves the holder's host
  no longer exists.

A subtlety that must be encoded rather than rediscovered: `_owner_alive` compares
against the *calling machine's* boot id, so it is a valid oracle only when
evaluated on the host that owns the record. Evaluated anywhere else it returns
`False` for a perfectly healthy holder. The classifier must therefore refuse to
answer `provably_dead` off-host and return `indeterminate` instead. A node can
never classify its controller by itself; it can only report the identity block
and let a controller that holds the matching journal do the classification.

### 4. Recovery may break a provably-dead hold, and only that

Replace the boolean `takeover` parameter of `acquire_remote_deployment_lock`
(`deploy-mac-fleet.sh:6802`) with a three-valued break authority:

| Authority | May break | Granted to |
|---|---|---|
| `none` | nothing | default for a fresh interactive deploy |
| `proven_dead_only` | `provably_dead` only | **durable recovery** (`:13326`, `:13765`) |
| `aged_operator_forced` | `indeterminate` older than the stale threshold | operator, explicit, unchanged from today's `MAC_DEPLOY_TAKEOVER_STALE_LOCK=1` |

Recovery is granted `proven_dead_only` and is never granted
`aged_operator_forced` — the comment at `:13324-13325` remains true as written,
because a *live* successor still fences the old controller out. What changes is
that a dead successor no longer does. Every break emits an audit record naming
the broken hold's identity block, the classification, and the evidence that
produced it.

### 5. Absence is admissible for a node, on the hub's word, never on a timeout

Add `node-absent --absence-file` to `fleet-cohort-transaction.py`, modelled
field-for-field on `hub-orphaned` / `_absence_receipt` (`:3343`, `:3495`). The
node receipt binds `epoch_id`, `stable_id`, `generation`, and the node's route
identity recorded when the epoch bound the route, and it is issued by the hub's
node registry — the authority that outlives the node — not by the deploy
controller that wants to proceed. Any status other than `absent` (including
`mismatch`) is refused, exactly as `_absence_receipt` refuses it today. A node in
state `absent` then satisfies `command_abort`'s cohort predicate (`:3788-3791`)
alongside `{planned, route_bound, aborted}`.

The principle: **absence is a positive attestation from a surviving authority,
never an inference from silence.** A node that fails to answer is
`indeterminate`, and `indeterminate` blocks.

### 6. The epoch is named before any SSH

Deploy preflight runs the local journal `discover` first — it is local and needs
no transport — and if a non-terminal epoch exists, it fails immediately with a
named decision per ADR 0022, before route resolution, before
`start_ssh_control_master`, before any remote command. The decision payload
carries: `epoch_id`, `state`, `phase`, the owner's liveness classification and
the evidence for it, the rollback- and finalization-candidate counts, and the
exact remedy command to run. `authoritative SSH route resolved empty`
(`:1554`) stays where it is and stays true; it simply can no longer be the first
thing an operator sees when the cause is a held epoch.

Every lock outcome gets a reason code — `hold_live`, `hold_indeterminate_aged`,
`hold_broken_proven_dead`, `hold_v1_no_identity`, `epoch_non_terminal`,
`node_absent_attested` — so the diagnosis is the return value rather than a
second system that has to agree with the first.

### 7. Terminal journals are reaped by the next writer, under the lock that exists

No new daemon and no cron. `command_init` and `command_discover` already hold
`.cohort-transaction.lock` (`:103`) and already walk every journal; reaping
happens there, in that walk:

- Retention: keep terminal journals newer than
  `MAC_COHORT_JOURNAL_RETENTION_DAYS` (default 14) **and** always keep the most
  recent `MAC_COHORT_JOURNAL_RETAIN_MIN` (default 20) regardless of age.
- Never reap a non-terminal journal, whatever its age.
- Reaping moves the journal and its plan files to `<dir>/reaped/`; deletion is a
  separate explicit `--purge`. The nine-day incident was diagnosed from the
  journal backup, so the reaper must not be the reason the next one cannot be.
- Each reap is a named decision and is logged.

Node locks (mechanism 4) gain `expires_at_epoch = renewed_at + lease_seconds`,
renewed by the live holder. Expiry makes a hold **reportable**, not breakable —
an expired hold is still `indeterminate` unless rule 3 applies. Hub dispatch
holds (mechanism 6) are reconciled when the epoch named in their identity block
reaches a terminal state.

## Consequences

- The node lock stops being a bare `deployment_id` and starts being an
  attributable hold. This is the enabling change; rules 1, 3 and 4 are
  unimplementable without it.
- The deadlock closes: a dead controller's node lock becomes breakable by
  durable recovery, and only by durable recovery, and only when death is proven.
- A wrong `provably_dead` verdict splits the brain across two live controllers.
  That risk is why the default is `indeterminate`, why off-host classification is
  refused outright, and why `aged_operator_forced` stays a human decision.
- Deploy preflight gets marginally slower (a local journal scan) and materially
  more honest. Reaping makes it faster again by bounding the O(n) walk.
- Absence becomes a first-class terminal path, so decommissioning a node no
  longer requires fabricating recovery evidence or bypassing the journal.
- Migration is dual-read for one release: v1 locks classify as `indeterminate`,
  so the floor is today's behaviour and the ceiling improves as v2 locks appear.
- Work this ADR authorises but does not perform: the `v2` lock payload and its
  writer/reader migration, the shared classifier and its reason codes, the
  three-valued break authority at the two recovery call sites, `node-absent`,
  the reaper, the preflight reordering, and the dispatch-hold reconciliation.

## Alternatives considered

- **One global lock.** Rejected for the reason the incident inventory already
  gives: a controller-side lock cannot exclude a deploy launched from another
  workstation whose journal lives on another machine. Per-node ownership is not
  duplication.
- **Wall-clock TTL everywhere, uniformly.** Rejected. A phase-2 rollback window
  can legitimately exceed an hour, so any threshold short enough to unblock the
  incident is short enough to break a healthy deploy. Uniform *and* unsafe is
  worse than divergent and safe.
- **Keep both models and publish a mapping between them.** Rejected. The mapping
  has no place to live, no test that can fail when it drifts, and — decisively —
  no data to map from, since the node lock records no identity at all.
- **Delete the node lock and rely on the journal.** Rejected: the journal is
  per-workstation and cannot exclude a second workstation.
- **A cron reaper daemon.** Rejected: a new moving part that would have to
  acquire a lock the deploy writers already hold, in order to do work those
  writers are already walking the directory to find.
- **Let recovery use ambient stale takeover (flip `:13326` to `1`).** Rejected.
  It unblocks the incident and reintroduces exactly the hazard the comment at
  `:13324` describes, because it cannot tell a live successor from a dead one.
  Proving death is the whole point.
