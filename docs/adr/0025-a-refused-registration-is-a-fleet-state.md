# ADR 0025 - A refused registration is a fleet state, not a client error

- Status: **Proposed**
- Date: 2026-08-20
- Decision owner: MAC fleet owner
- Related: ADR 0022 (a gate returns a named decision, not a boolean), ADR 0021
  (schema changes need versioned migrations — why this adds no table)

## Context

On 2026-08-20 a worker flapped between idle, degraded and offline all day. Its
journal:

```
mac API POST /machines failed:
  {"detail":"machine.resources exceeds 65536-byte limit"}
mac-agent.service: Main process exited, code=exited, status=1/FAILURE
mac-agent.service: Scheduled restart job, restart counter is at 8
```

Registration refused, process exits 1, systemd restarts it, refused again.

Three separate things made a five-word error message cost a day:

1. **The limit is enforced before any row is written.** That is correct — the
   point is to keep the oversized blob out of the database — but it means the
   hub retains no evidence the attempt happened. `mac agent list` showed
   `offline`, which is what a powered-off machine shows.
2. **systemd made the failure quiet.** `systemctl is-active` reported `active`
   for a service whose every invocation failed, and `pgrep` found a transient
   pid. Every local liveness signal agreed the agent was fine.
3. **Nothing warned before the line.** Measured live: 65,194 bytes (99.5%),
   48,861 (74.6%), 27,438 (41.9%). Nothing was wrong with the first host except
   that it got there first. Every worker accumulates machine resources, so the
   fleet had a fuse and no gauge.

## Decision

**A refused registration is recorded and rendered as its own fleet state.**
`refused` is not `offline` and not absent, in the data and in every surface.

Concretely:

- `machine.resources` / `agent.resources` are measured at registration. Over the
  limit, a `registration.refused` observation is written **before** the
  `ValidationError` is raised, and the error message carries the size, the
  utilization and the largest contributing keys — `machine.resources exceeds
  65536-byte limit -- machine.resources is 65,537 bytes, 100.0% of the
  65,536-byte limit (over); largest: commands=59,204`.
- `GET /agents/registration-refusals`, `mac agent list` and the observability
  console fold those events per host. Every agent row carries
  `registration_state`; a host with no row at all is appended with
  `registered: false`, because "never admitted" was the case that was fully
  invisible.
- Pressure is reported at 75% (`warn`) and 90% (`critical`) as a
  `registration.payload_pressure` metric, **on band change only**. A row per
  registration is how `observability_events` reached 3.1GB before (mem-04).
- The heartbeat measures but does **not** enforce. Growth happens between
  restarts, so the heartbeat is where it is observed; refusing one would take an
  agent the hub already admitted offline over a payload it already stored.
- The worker measures its own payload before sending, sheds the largest
  unprotected block in the `critical` band, and — if refused anyway — sheds hard
  and retries **once** rather than exiting. Dispatch policy, credential proof,
  hardware and the executor attestation are never shed: a registration that only
  fits without them has become a different agent.
- The unbounded grower, `_detect_command_inventory()`, is bounded in **bytes**
  (16 KB, `MAC_WORKER_COMMAND_INVENTORY_MAX_BYTES`). Its previous cap of 10,000
  *names* was never a bound on what the hub rejects: 10,000 names encode to well
  over 100 KB, twice the whole registration budget. Explicitly probed names are
  kept first, and `bounded_by` / `omitted` travel in the payload so a contract
  that does not find `podman` can tell "not installed" from "did not fit".

## Considered and rejected

**Raise the limit.** Moves the same cliff further out. The fleet grows into it
either way, and a larger limit makes the eventual failure more expensive, not
less.

**Store refusals in a new table.** Refusals are bounded, low-volume, and already
have an owner that understands retention and pruning. A new table would need a
versioned migration (ADR 0021) to buy nothing. `observability_events` also
already has `idx_observability_events_subject_sequence` on
`(kind, name, subject_type, subject_id, sequence DESC)`, which is exactly the
"last band for this subject" lookup.

**Store the oversized payload so the operator can inspect it.** This would
persist precisely what the limit exists to refuse. The measurement — size,
utilization, and the ranked top-level keys — is what an operator acts on, and it
is a few hundred bytes.

**Move the large blocks out of the payload into their own lifecycle** (a
`machine_resource_documents` table, or blob storage keyed by digest). Considered
and **deferred, deliberately**. It is the right long-term shape for the command
inventory specifically — it is host-scoped, changes rarely, and is re-sent
verbatim on every registration and every ~300s refresh — but it is a schema
change, a new lifecycle, and a migration for every existing worker, and it does
not by itself fix any of the three failures above. A relocated blob that still
grows without a gauge produces the same outage against a different limit. The
bound and the gauge come first; relocation becomes worth doing when a *bounded*
inventory is still the largest thing in the payload. Nothing here forecloses it:
`payload_budget.measure()` is the measurement either design needs.

## Consequences

- Adds `mac.payload_budget` (pure, stdlib-only, importable by hub, worker and
  CLI) and `mac.registration_budget` (hub-side recording and rendering).
- `mac agent list` rows gain `registration_state`, `registration_refusal`,
  `registered`, and a `resources_bytes` / `resources_utilization` /
  `resources_band` gauge. Additive; a plane that cannot answer degrades to the
  previous output rather than failing the listing.
- `POST /machines` responses gain `resources_budget`, so a worker learns its own
  headroom from the registration it just completed.
- A worker can now be in the fleet with an incomplete command inventory. That is
  the intended trade: degraded and visible beats correct and absent. The reason
  is recorded in `resources.payload_budget.shed`.
- The 64 KB limit itself is unchanged.
