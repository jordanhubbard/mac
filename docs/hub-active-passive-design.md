# Hub Active-Passive Design (Minimal Standby)

This design specifies the **smallest** active-passive high-availability posture
for the fleet hub. It is a *reuse* design: it adds the minimum new mechanism on
top of the primitives the ground-truth audit (`docs/hub-ha-audit.md`) verified
and the operator narrative (`docs/hub-availability.md`) already documents. It
resolves the six gaps the audit left to the design child (audit section 6) —
chiefly fencing, standby promotion, and repointing — while preserving the
single-authority invariant.

Scope: **one** warm standby hub, operator-assisted promotion, tighter than the
current "operator promises the old hub is down" runbook. It does **not**
introduce hub-to-hub replication or auto-failover for the SQLite tier; see
"What is deliberately NOT built".

## 1. What the standby reuses (no new services)

The standby is an *ordinary hub deployment that is not yet the authority*. It
reuses, unchanged:

- **Control-plane role invariant.** `MAC_CONTROL_PLANE_ROLE` selects `hub` vs
  `client`; the store factory refuses to let a `client` own a database. While
  passive the standby runs as `client` (or offline), so it can never silently
  become a second authority.
- **Router decoupling.** The standby runs `mac-router`
  (`MAC_DEPLOY_ROUTER_BACKEND=standalone`, `MAC_DEPLOY_ROUTER_PORT`, default
  8790) exactly like any hub. Because inference is active-active and holds no
  write state, the standby's router may serve traffic *before* promotion with
  no split-brain risk — only the ledger authority is single.
- **Evidence-blob externalization.** `MAC_EVIDENCE_BLOB_DIR` /
  `MAC_EVIDENCE_INLINE_MAX_BYTES` keep artifact bytes out of the DB. Blob URIs
  are location-independent (`evidence-blob:`), so a restored ledger on the
  standby resolves rows against the standby's own blob root.
- **Ledger tiers.** Tier A Postgres (`MAC_DATABASE_URL`, stateless
  `replicas: 2` hub processes) and Tier B SQLite verified snapshots
  (`mac-ledger-backup`, `MAC_LEDGER_BACKUP_SYNC_CMD`, `--verify`).
- **Backup scheduler role gate.** The snapshot scheduler is default-ON but
  hub-only (`MAC_LEDGER_BACKUP_ENABLED` truthy **and** role != `client`), so a
  passive standby does not itself take snapshots of a stale copy.
- **Promote/fence procedure.** The existing `docs/hub-availability.md` promote
  steps remain the backbone; this design tightens steps 1, 2, and 4.

No new service or daemon is introduced. Every knob above already exists.

## 2. Replication path per tier

### Tier A — Postgres (recommended where RPO must be small)

The standby hub processes are stateless; replication is delegated to the
Postgres product (streaming replica + managed failover: CloudNativePG, Patroni,
RDS/Cloud SQL). The standby carries **no** SQLite authority in this tier. RPO
and RTO are the Postgres replica's, typically seconds. This is unchanged from
the audit's Tier A and needs no new mechanism.

### Tier B — SQLite verified snapshots + evidence-blob rsync

The active hub already ships each verified snapshot through
`MAC_LEDGER_BACKUP_SYNC_CMD`. This design makes that one hook ship **both** the
snapshot and the evidence-blob directory as a single atomic operator step, so
the two backups can never drift (resolves audit gap 5):

```console
MAC_LEDGER_BACKUP_SYNC_CMD='
  set -e
  rsync -a "$MAC_LEDGER_SNAPSHOT_PATH"* standby:~/.mac/backups/ledger/
  rsync -a --delete ~/.mac/evidence-blobs/ standby:~/.mac/evidence-blobs/
' mac-ledger-backup --db ~/.mac/mac.db --out ~/.mac/backups
```

The hook already exits non-zero on failure, so a ledger snapshot that ships
without its blobs (or vice versa) is a loud error, not a silent gap. RPO for
Tier B is the snapshot cadence (`MAC_LEDGER_BACKUP_INTERVAL_SECONDS`, default
900 s / 15 min).

**Standby freshness (resolves audit gap 4).** The standby runs a small periodic
check — a cron/timer, not a new daemon — that calls the existing verifier over
the newest shipped snapshot and alarms on staleness:

```console
mac-ledger-backup --verify "$(ls -t ~/.mac/backups/ledger/*.db | head -1)"
```

The check treats "no snapshot newer than N intervals" or a failed `--verify`
as staleness and surfaces it through the host's normal alerting. This reuses
`mac-ledger-backup --verify`; it adds no new code path, only a schedule on the
standby.

## 3. Fencing / split-brain guarantee (resolves audit gap 1)

The invariant is unchanged: **the old hub must be fenced before the standby
serves a single write.** This design makes the fence *checkable* instead of a
verbal promise, using only existing config surface — a single-holder promote
lease file:

- A promote lease is a small file at a well-known path on shared/standby-visible
  storage (e.g. `~/.mac/ha/promote.lease`) recording the fenced generation and
  the identity of the hub that currently holds write authority.
- **Promotion refuses to start unless the operator has recorded the fence** in
  the lease (old hub generation marked stopped). The standby's promote runbook
  step reads the lease and aborts if the old hub still claims the live
  generation.
- Fencing itself remains an operator action — `systemctl stop` /
  `launchctl bootout`, or a tailnet ACL / node shutdown when the host is
  unreachable — because a standby cannot safely infer "the old hub is down"
  from "I can't reach the hub." The lease only makes the human's fence explicit
  and machine-verifiable, closing the "operator promises" gap without adding an
  unsafe automatic STONITH.

This is the minimum new mechanism: a single lease file and a promote-time check,
not a distributed lock service or leader election.

## 4. Stable repoint strategy (resolves audit gap 3)

Agents must not need a per-agent env change on failover. The canonical
mechanism is a **stable hub DNS / tailnet name** bound to the hub from day one:

- Give the hub a stable tailnet DNS name (e.g. `hub.<tailnet>`) and set every
  agent's `hub_url` to that name, never to a raw IP. `hub_url` accepts a DNS
  name anywhere an IP works today.
- On promotion, **rebind the name** to the standby (tailnet MagicDNS / DNS
  record update). No agent env changes, no fleet-wide redeploy.
- The `~/.mac/fleets.yaml` `hub_url` + `MAC_DEPLOY_NEW_HUB_URL` redeploy path
  remains the **fallback** for fleets that did not adopt the stable name, but
  the stable-name rebind is the recommended default.
- The standby advertises readiness through its own `/healthz` (already served
  by the hub API and `mac-router`), so the operator confirms the standby is up
  before rebinding the name.

## 5. Promote runbook deltas vs `docs/hub-availability.md`

The existing five-step promote procedure stands; this design changes three
steps and adds one pre-check:

- **Step 1 (Fence) — tightened.** After stopping the old hub, record the fence
  in the promote lease (mark the old generation stopped). Promotion tooling
  reads the lease and refuses to proceed if the old hub still holds it.
- **Step 2 (Restore) — atomic blobs.** Because the ship hook now rsyncs the
  evidence-blob directory alongside every snapshot, the standby already holds a
  matching blob set; restore is `--verify` newest snapshot → install as
  `~/.mac/mac.db` (no `-wal`/`-shm` leftovers). No separate blob-fetch step.
- **Step 4 (Repoint) — stable name.** Rebind the stable hub DNS/tailnet name to
  the standby instead of editing `hub_url` per fleet. The redeploy path stays as
  fallback only.
- **New pre-check (freshness).** Before fencing, confirm the standby's freshness
  check is green (latest snapshot within cadence, `--verify` passing) so the
  operator is not promoting a stale copy.

Steps 3 (make standby the hub: `MAC_CONTROL_PLANE_ROLE=hub`, restore
`mac.env` token material) and 5 (old hub returns as a `client` spoke; local
divergence recovered by an operator, never merged automatically) are unchanged.

## 6. New surface introduced (do not gold-plate)

The entire new footprint is:

- **One promote lease file** at a well-known path (e.g. `~/.mac/ha/promote.lease`)
  and a promote-time check that reads it. No new service, no new network
  protocol.
- **One standby freshness schedule** (cron/timer) that calls the existing
  `mac-ledger-backup --verify`. No new code path.
- **One convention** that the Tier B ship hook rsyncs the evidence-blob
  directory with the snapshot. Pure `MAC_LEDGER_BACKUP_SYNC_CMD` content — no
  code change.

If a fleet wants the lease and freshness wrapped in a `mac` subcommand later,
that is an additive convenience over these primitives, not a prerequisite for
this posture. This design deliberately stops here.

## 7. What is deliberately NOT built (and why)

- **No hub-to-hub replication protocol.** Tier A delegates replication to
  Postgres; Tier B ships verified snapshots. Building a bespoke replication
  protocol would create a second write path and threaten the single-authority
  invariant that dispatch guards, task authority, and attestation depend on.
- **No auto-failover for the SQLite tier.** Promotion stays an operator action
  because fencing cannot be safely inferred by a standby from "I can't reach the
  hub." An automatic promoter would risk two live authorities that silently
  diverge with nothing to reconcile them.
- **No leader election / consensus service.** A single warm standby with an
  operator-verified fence lease is the minimum that makes promotion safe; a
  quorum system is unjustified for one standby and would add operational
  surface without changing the RPO/RTO envelope.
- **No automatic old-hub reintegration merge.** Tasks written to a fenced hub's
  ledger while partitioned are recovered deliberately by an operator; they
  never merge automatically, preserving one authority.

The result is one warm standby, an explicit and checkable fence, an atomic
snapshot+blob replication step, a freshness guard, and a stable-name repoint —
reusing every existing primitive and adding only a lease file, a verify
schedule, and a ship-hook convention.
