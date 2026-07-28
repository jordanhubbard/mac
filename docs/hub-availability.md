# Hub Availability

The fleet is hub-and-spoke on purpose: the ledger's atomicity, auditability,
and credential centralization all come from having one authority. This
document covers the three mechanisms that keep that choice from meaning "one
process on one machine is the fleet's single point of failure", and the
promote procedure for replacing a failed hub.

## 1. Inference is decoupled from the ledger process

`MAC_DEPLOY_ROUTER_BACKEND=standalone` runs the LLM router as its own process
(`mac-router`, listening on `MAC_DEPLOY_ROUTER_PORT`, default 8790) instead of
mounting `/v1` inside the hub ledger API:

- Hub API restarts no longer drop in-flight model streams.
- Ledger lock pressure and model traffic stop sharing one event loop.
- Provider credentials stay centralized: on the hub host the router resolves
  `secret:` refs from the co-located control-plane store, exactly like the
  in-process mount did. `llm.route` observations still land in the ledger.

Run it under the host's supervisor next to the hub API, e.g. systemd:

```ini
[Service]
EnvironmentFile=%h/.mac/mac.env
ExecStart=%h/.mac/venv/bin/mac-router --host 127.0.0.1 --port 8790
Restart=on-failure
```

(or the equivalent launchd plist on a macOS hub). The deploy writes the
gateway base URLs to point at the router port; the hub API's own `/v1` mount
is a no-op for any backend other than `inproc`, so the two can never both
serve model traffic.

**Per-site replica.** A wing whose network path to the hub is unreliable
(e.g. NAT/DERP-relayed pods) can run its own `mac-router` and set
`MAC_DEPLOY_ROUTER_URL=http://<replica>:8790/v1` for the agents in that wing.
Only inference moves — task claims, evidence, and messaging still go to the
hub. The replica authenticates callers with the same hub-facing bearer tokens
(`MAC_ROUTER_TOKENS`); its upstream providers use env keys or `key=none`
private endpoints — hub vault material is never shipped to it.

## 2. Ledger bytes are bounded: evidence blobs live outside the DB

With `MAC_EVIDENCE_BLOB_DIR` set (the deploy defaults it to
`~/.mac/evidence-blobs` on the hub), evidence artifact bytes above
`MAC_EVIDENCE_INLINE_MAX_BYTES` (default 64 KiB) are written to a
content-addressed, integrity-verified blob directory; the ledger row keeps
digest, size, metadata, and a `content_uri`. The secret-scoped artifact GET
materializes content transparently, so nothing downstream changes. This keeps
ledger snapshots small and replication fast — the availability story below
gets cheaper because the DB carries coordination state, not payload bytes.

Blob files are deduplicated by digest and never rewritten; reclaim space with
an age-based sweep of the directory. Back the directory up alongside ledger
snapshots if artifact bytes must survive a hub loss (`rsync -a` in the same
sync hook works).

## 3. The ledger itself: two availability tiers

### Tier A — Postgres backend (smallest RPO; recommended once HA matters)

Postgres is a first-class store backend (`MAC_DATABASE_URL`; schema, trigger,
and translation layers are in-tree with live tests). Set
`MAC_DEPLOY_DATABASE_URL=postgresql://...` for the hub and the deploy writes
`MAC_DATABASE_URL` and drops `MAC_DB` — the node still declares exactly one
durable authority. Availability then becomes a database problem with
off-the-shelf answers (streaming replica + managed failover: CloudNativePG,
Patroni, RDS/Cloud SQL). Hub *processes* are stateless in this mode — the k8s
manifests already run `replicas: 2` against one Postgres — so hub failover is
"start the API pointing at the same DSN".

**In-tree verified backups (`mac-pg-backup`).** Streaming replication is the
RTO answer, but the hub also keeps its own restore-verified artifact so a
break-glass recovery does not depend solely on the database operator.
`mac-pg-backup` (also `python -m mac.pg_backup`) takes a consistent logical
dump and proves it is restorable:

- `pg_dump -Fc` (custom archive, single snapshot-isolated transaction) →
  owner-only `0600` artifact in a `0700` `<out>/postgres/` directory → sha256
  manifest → retention (`--keep-last`, default 14) → off-box ship hook
  (`MAC_PG_BACKUP_SYNC_CMD`, with `MAC_PG_BACKUP_PATH` / `_SHA256` / `_MANIFEST`
  exported, mirroring the ledger path's hook contract).
- **Restore-to-scratch drill.** Each backup (or every
  `MAC_PG_BACKUP_VERIFY_EVERY` runs) restores the dump into a throwaway
  `mac_restore_verify_*` database via `pg_restore` and proves the schema plus
  representative row counts (`tasks`, `agents`, `events`) came back, then drops
  the scratch database. A backup that cannot be restored fails closed — it is
  not counted as a backup.

```console
MAC_PG_BACKUP_SYNC_CMD='rsync -a "$MAC_PG_BACKUP_PATH"* standby:~/.mac/backups/postgres/' \
  mac-pg-backup --dsn "$MAC_DATABASE_URL" --out ~/.mac/backups
```

The `PgBackupScheduler` runs this on an interval inside the hub process,
default-ON **only** when the authority is Postgres, a no-op on the SQLite tier
and with `MAC_PG_BACKUP_ENABLED=0`. A dump/verify/ship failure is a loud ledger
observation (`pg.backup.failed`) plus an operator notification. Verify an
artifact's integrity offline with `mac-pg-backup --verify-manifest <dump>`.

**No SQLite fallback.** A PostgreSQL failure — connection loss, a failed dump,
a failed restore drill — is surfaced loudly and never downgraded to a SQLite
backup or authority. The Postgres and SQLite tiers are mutually exclusive: a
hub declares exactly one durable authority. The immutable 2026-07-28 SQLite
cutover archive (`mac migrate` archive: mode-`0600`, sha256 manifest, verified
at creation) is preserved as *recovery evidence* — a frozen snapshot of the
pre-cutover authority for forensic/legal recovery — and is explicitly **not** a
live fallback authority. Nothing restarts it as a second live ledger.

### Tier B — SQLite + verified snapshots (default; RPO = snapshot cadence)

`mac-ledger-backup` (also `python -m mac.ledger_backup`) takes a verified
snapshot: WAL checkpoint → SQLite online backup → `integrity_check` → sha256
manifest, with retention (`--keep-last`, default 14) and a ship hook:

```console
MAC_LEDGER_BACKUP_SYNC_CMD='rsync -a "$MAC_LEDGER_SNAPSHOT_PATH"* standby:~/.mac/backups/ledger/' \
  mac-ledger-backup --db ~/.mac/mac.db --out ~/.mac/backups
```

Schedule it (systemd timer / launchd / cron) at the cadence that bounds your
acceptable data loss. A failed ship hook exits non-zero — a silent standby
gap is treated as an error, not a shrug. Verify any snapshot on the standby
with `mac-ledger-backup --verify <snapshot.db>`.

## Promote procedure (standby takes over)

Split-brain rule first: **the old hub must be fenced before the standby
serves a single write.** Two live authorities silently diverge; nothing
reconciles them after the fact.

1. **Fence the old hub.** Stop its service (`systemctl stop <fleet>` /
   `launchctl bootout ...`). If the host is unreachable, ensure agents can't
   reach it either (tailnet ACL or shut the node down) before proceeding.
2. **Restore the ledger on the standby.**
   - Tier A: promote the Postgres replica (or let the operator/managed
     failover do it) and skip to step 3. If no replica survived, restore the
     newest shipped `mac-pg-backup` artifact into an empty cluster:
     `mac-pg-backup --verify-manifest <dump>` (integrity), then
     `createdb mac && pg_restore --no-owner --no-privileges --dbname=mac <dump>`,
     and point `MAC_DATABASE_URL` at it. Never restore into a database that is
     still serving — a Postgres failure is repaired in Postgres, not by falling
     back to SQLite.
   - Tier B: `mac-ledger-backup --verify` the newest shipped snapshot, then
     install it as `~/.mac/mac.db` (no `-wal`/`-shm` leftovers) on the
     standby. Restore the evidence-blob directory alongside it.
3. **Make the standby the hub.** Deploy it as the fleet's hub
   (`MAC_CONTROL_PLANE_ROLE=hub` — i.e. it is the `hub_agent` /
   shared-services manager in the deploy), same `MAC_API_TOKEN`/worker-token
   material (restore `mac.env` from the standard deploy backups).
4. **Repoint the fleet.** Update `hub_url` for the fleet in
   `~/.mac/fleets.yaml` and redeploy the agents (the `--hub-url` /
   `MAC_DEPLOY_NEW_HUB_URL` path), or — much better — give the hub a stable
   tailnet DNS name from day one and rebind that name to the standby so no
   per-agent env changes are needed. `hub_url` accepts a DNS name anywhere an
   IP works today.
5. **Old hub stays fenced.** When the old host returns, redeploy it as a
   spoke (its `MAC_CONTROL_PLANE_ROLE` becomes `client`, and the store
   factory refuses to let a client own a DB). Any tasks written to its ledger
   while partitioned are rescueable with the local-ledger migration tooling
   (`mac migrate` local-authority path) — they do not merge automatically.

## What this deliberately does not do

- No automatic failover for the SQLite tier: promoting is an operator action
  precisely because fencing (step 1) cannot be safely inferred by the standby
  from "I can't reach the hub".
- No hub-to-hub replication protocol: Tier A delegates replication to
  Postgres; Tier B ships verified snapshots. Both keep the single-authority
  invariant that the rest of the system (dispatch guards, task authority,
  attestation) is built on.

- No cross-tier fallback: a Postgres authority never silently degrades to a
  SQLite ledger, and the 2026-07-28 SQLite cutover archive stays immutable
  recovery evidence, not a live authority. Restoring either tier is an
  explicit operator action gated by the fence in step 1.
