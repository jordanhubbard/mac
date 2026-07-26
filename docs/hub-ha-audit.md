# Ground-Truth Audit: Hub High-Availability Primitives

Read-only audit for the parent task
`task_repair_7b70a5264dbdf0cde89fdecb` (title: "Repair environment prerequisites:
Audit existing HA primitives and produce the smallest active-passive reuse
design"). This document records ground truth **only**: it changed **no** source
or test logic. It enumerates exactly what the fleet's high-availability
primitives are today, how they behave, their RPO/RTO character, and their
current test coverage, with exact `file:line` references. It closes by listing
the gaps a design child must resolve to reach a minimal active-passive posture.

The companion runbook `docs/hub-availability.md` is the operator-facing
narrative; this audit is the verified inventory behind it.

## Summary Verdict

The fleet already has four cooperating HA primitives, all in-tree with live
tests:

1. **Inference/router decoupling** — the LLM router can run as its own process
   or a per-site replica, so ledger restarts and network pathologies stop
   killing model traffic. **Active-active** for inference reads (multiple
   routers may serve concurrently); no shared write authority is involved.
2. **Evidence-blob externalization** — large artifact bytes move out of the
   ledger DB into a content-addressed, integrity-verified blob store, bounding
   DB growth and making ledger snapshots small. A **supporting** primitive
   (makes the tiers below cheaper), not itself a failover mechanism.
3. **Ledger availability, two tiers** — Tier A Postgres (stateless hub
   processes, `replicas: 2`) and Tier B SQLite verified snapshots (scheduled,
   shipped off-box). Tier A is **active-active** at the process layer over one
   DB authority; Tier B is **active-passive / operator-driven** (RPO = snapshot
   cadence).
4. **Operator promote/fence procedure** — a documented, manual standby
   takeover built on the single-authority and control-plane-role (hub/client)
   invariants. **Operator-driven** today.

No automatic failover, leader election, or hub-to-hub replication protocol
exists for the SQLite tier; promotion is deliberately an operator action
(fencing cannot be safely inferred by a standby). The remaining gaps for a
minimal active-passive posture are enumerated in section 6.

## 1. Inference / Router Decoupling

**What exists.** The standalone router service is `src/mac/router_service.py`.
Its module docstring (src/mac/router_service.py:1) states the motivation: the
router historically only existed mounted *inside* the hub ledger API
(`MAC_ROUTER_BACKEND=inproc`), coupling inference to coordination so that a hub
API restart drops every in-flight LLM stream.

- Entry point `build_router_app` (src/mac/router_service.py:122) builds a
  FastAPI app carrying only the router routes, bearer auth, and `/healthz`. It
  forces the mount gate open for its own process by setting a local copy's
  `MAC_ROUTER_BACKEND=inproc` (src/mac/router_service.py:133) without mutating
  the real environment, then calls `mount_router` (src/mac/router_service.py:174).
- `main` (src/mac/router_service.py:189) runs it under uvicorn, defaulting the
  port to `DEFAULT_PORT = 8790` (src/mac/router_service.py:42) via
  `MAC_ROUTER_PORT` (src/mac/router_service.py:43) and binding
  `127.0.0.1` by default (src/mac/router_service.py:193).
- The in-process mount is gated in `mount_router` (src/mac/router_app.py:1242):
  it is a **no-op** unless `MAC_ROUTER_BACKEND` normalizes to `inproc`, so the
  hub API and a standalone router can never both serve `/v1`.

**`MAC_DEPLOY_ROUTER_BACKEND` / `MAC_DEPLOY_ROUTER_URL`.** These are the
deploy-time inputs, resolved in `src/mac/deploy_env.py`:

- `_deploy_router_config` (src/mac/deploy_env.py:712) reads
  `MAC_DEPLOY_ROUTER_BACKEND` into `backend` (src/mac/deploy_env.py:714),
  `MAC_DEPLOY_ROUTER_PORT` (default `8790`) into `port`
  (src/mac/deploy_env.py:721), and `MAC_DEPLOY_ROUTER_URL` into `url`
  (src/mac/deploy_env.py:725).
- `_apply_router` (src/mac/deploy_env.py:902) treats `inproc` and `standalone`
  identically for hub validation, spoke wiring, and credential centralization —
  "only where the router listens differs" (src/mac/deploy_env.py:903). Any
  other backend value flows to `_apply_non_inproc_router`
  (src/mac/deploy_env.py:890).
- Hub wiring `_apply_inproc_router_hub` (src/mac/deploy_env.py:763): when
  `backend == "standalone"` it sets `MAC_ROUTER_PORT` and points the hub's own
  gateway base URLs at `http://127.0.0.1:<port>/v1`
  (src/mac/deploy_env.py:779) so the hub API (backend != inproc) does not mount
  `/v1` and ledger/inference stop sharing fate.

**Per-site replica behavior.** `_apply_inproc_router_spoke`
(src/mac/deploy_env.py:810) sets the routing base to `MAC_DEPLOY_ROUTER_URL`
when present, else the hub base: `route_base = (router["url"] or hub_base)`
(src/mac/deploy_env.py:821). The spoke authenticates with a hub-facing bearer
token and holds **no upstream keys** (src/mac/deploy_env.py:814). The router
service confirms the credential posture in code: `_store_backed_secret_resolver`
(src/mac/router_service.py:71) resolves `secret:` refs **only** when the process
can legitimately own the store (hub host with `MAC_DB`/`MAC_DATABASE_URL`,
src/mac/router_service.py:82); elsewhere the resolver is `None` and providers
must use env keys or `key=none` private endpoints — hub vault material is never
shipped to a worker node (src/mac/router_service.py:20). Caller auth uses
constant-time bearer comparison against `MAC_ROUTER_TOKENS` plus the node's own
`MAC_API_TOKEN`/`MAC_WORKER_TOKEN` (`_configured_tokens`,
src/mac/router_service.py:49; `_token_matches`, src/mac/router_service.py:62).

**Posture / RPO / RTO.** **Active-active for inference.** Multiple routers (hub
plus per-site replicas) can serve model traffic concurrently; there is no shared
write state, so there is no RPO to bound and no promote step — a failed router is
replaced by restarting the process or repointing a wing to another `/v1`. Route
observations are best-effort (`_route_observer_for`, src/mac/router_service.py:99;
failures are swallowed so observability never breaks routing,
src/mac/router_service.py:116).

**Test coverage.** `tests/test_router_service.py` (6 tests):
- Token requirement: `test_requires_at_least_one_bearer_token`
  (tests/test_router_service.py:24).
- Auth boundary: `test_healthz_is_open_and_v1_requires_bearer`
  (tests/test_router_service.py:31), `test_accepts_any_configured_token`
  (tests/test_router_service.py:47).
- Mount exclusivity: `test_hub_api_does_not_mount_v1_when_backend_standalone`
  (tests/test_router_service.py:63).
- Fail-closed with no providers: `test_no_providers_fails_closed`
  (tests/test_router_service.py:78).
- Observation subject/source: `test_route_observer_prefers_task_subject_and_keeps_agent_source`
  (tests/test_router_service.py:85).

Gap: no direct test of the `MAC_DEPLOY_ROUTER_URL` spoke-repoint path or the
per-site `secret:`-resolver-disabled posture at the `router_service` layer
(the deploy-env wiring is exercised in `deploy_env` tests, not here).

## 2. Evidence-Blob Externalization

**What exists.** `src/mac/evidence_blobs.py` is a hub-local content-addressed
blob store for evidence artifact bytes. Its docstring (src/mac/evidence_blobs.py:1)
explains the coupling it removes: artifacts historically rode inside the ledger
as base64 rows, tying DB growth to artifact volume.

- `MAC_EVIDENCE_BLOB_DIR` (`BLOB_DIR_ENV`, src/mac/evidence_blobs.py:45) is the
  opt-in switch. `blob_root` (src/mac/evidence_blobs.py:60) returns `None` when
  unset, so the ledger inlines bytes exactly as before (zero-config default,
  src/mac/evidence_blobs.py:12).
- `MAC_EVIDENCE_INLINE_MAX_BYTES` (`INLINE_MAX_ENV`,
  src/mac/evidence_blobs.py:46) sets the inline threshold via `inline_max_bytes`
  (src/mac/evidence_blobs.py:68); an empty or unparseable value falls back to
  `DEFAULT_INLINE_MAX_BYTES = 65536` (64 KiB, src/mac/evidence_blobs.py:51),
  and a negative value is floored to 0 (src/mac/evidence_blobs.py:77). Bytes at
  or below the threshold stay inline even when a blob root is configured.
- Storage is content-addressed and integrity-verified: `store_blob`
  (src/mac/evidence_blobs.py:106) writes to `<root>/<aa>/<sha256hex>` atomically
  (tmp + `os.replace`), deduplicates by digest, and enforces mode `0600` on
  both the new-blob and dedup paths (src/mac/evidence_blobs.py:120,
  src/mac/evidence_blobs.py:129). `read_blob` (src/mac/evidence_blobs.py:135)
  re-hashes on read and raises `BlobIntegrityError`
  (src/mac/evidence_blobs.py:56) on any digest mismatch — it never returns
  unverified bytes. `blob_uri` (src/mac/evidence_blobs.py:95) is a
  location-independent `evidence-blob:` URI so the root can move (or a ledger
  can be restored on a standby with a different layout) without invalidating
  rows.

**Posture / RPO / RTO.** A **supporting** primitive, not a failover mechanism.
It has no active/passive character of its own; its role is to keep ledger
snapshots small so Tier B replication is fast and Tier A DB size stays bounded.
It does add a recovery obligation: the blob directory must be backed up
alongside ledger snapshots (see section 4, step 2 of the promote procedure).

**Test coverage.** `tests/test_evidence_blobs.py` (18 tests) covers roundtrip,
mode-0600 on new and dedup paths, corruption fail-closed, large-vs-small
inline/externalize selection, unconfigured-store inline fallback, missing-blob
fail-closed, cross-evidence dedup, `inline_max_bytes` default on unset and on
non-integer, invalid-digest and wrong-URI-scheme rejection, expected-sha256
mismatch, and write-error cleanup — e.g.
`test_large_artifact_externalizes_and_reads_back`
(tests/test_evidence_blobs.py:98), `test_small_artifact_stays_inline`
(tests/test_evidence_blobs.py:126), `test_unconfigured_blob_store_keeps_inline_behavior`
(tests/test_evidence_blobs.py:145), `test_read_blob_fails_closed_on_corruption`
(tests/test_evidence_blobs.py:87).

## 3. Ledger Availability — Tier A (Postgres)

**What exists.** Postgres is a first-class store backend selected by
`MAC_DATABASE_URL` in `make_store_from_env` (src/mac/store.py:80). A DSN with a
`postgres://`/`postgresql://` scheme constructs a `PostgresStore` with the
bundled schema auto-applied (src/mac/store.py:102); any other scheme is rejected
(src/mac/store.py:105). The k8s manifest runs stateless hub processes against
one shared DB: `deploy/k8s/mac-api/deployment.yaml:3` documents "N replicas with
the same shared `MAC_DATABASE_URL`" and `deploy/k8s/mac-api/deployment.yaml:17`
sets `replicas: 2`, with `MAC_DATABASE_URL` injected from the `mac-api-config`
secret (deploy/k8s/mac-api/deployment.yaml:62). The runbook records that this
turns availability into "a database problem with off-the-shelf answers"
(docs/hub-availability.md:61).

**Posture / RPO / RTO.** **Active-active at the process layer over a single DB
authority.** Hub processes are stateless in this mode, so failover is "start the
API pointing at the same DSN" (docs/hub-availability.md:69). RPO/RTO are
delegated to the chosen Postgres HA product (streaming replica + managed
failover: CloudNativePG, Patroni, RDS/Cloud SQL) — this is the smaller-RPO tier.

**Test coverage.** Backend selection and schema handling are covered by the
store tests (SQLite/Postgres translation, live Postgres in
`tests/test_postgres_live.py`, referenced by the docs CI gate at
tests/test_documentation_book.py:99). This audit did not run the live-Postgres
suite (requires a database); it verified the selection logic and the manifest
statically.

## 4. Ledger Availability — Tier B (SQLite Verified Snapshots)

**What exists — the snapshot primitive.** `src/mac/ledger_backup.py` produces
verified, shippable snapshots. `snapshot` (src/mac/ledger_backup.py:86):
1. `_verified_backup` (src/mac/ledger_backup.py:62): `PRAGMA wal_checkpoint` +
   SQLite online backup API + `PRAGMA integrity_check`, deleting the copy on any
   failure (src/mac/ledger_backup.py:72).
2. Writes a sha256 sidecar manifest (`mac.ledger_snapshot.v1`,
   src/mac/ledger_backup.py:45) at mode 0600 (src/mac/ledger_backup.py:124).
3. Prunes to `--keep-last` (default `DEFAULT_KEEP_LAST = 14`,
   src/mac/ledger_backup.py:47; `prune`, src/mac/ledger_backup.py:144).
4. Runs the ship hook `MAC_LEDGER_BACKUP_SYNC_CMD` (`SYNC_CMD_ENV`,
   src/mac/ledger_backup.py:46) with `MAC_LEDGER_SNAPSHOT_PATH`/`_SHA256`/
   `_MANIFEST` in the environment (src/mac/ledger_backup.py:130); a **non-zero
   hook exit is raised as an error** (src/mac/ledger_backup.py:137) so a broken
   standby sync is loud, not silent.

`verify` (src/mac/ledger_backup.py:158) re-checks a snapshot against its
manifest sha256 and `integrity_check` on the standby side; `--verify` is exposed
on the CLI (src/mac/ledger_backup.py:189). `main`
(src/mac/ledger_backup.py:176) is the `mac-ledger-backup` /
`python -m mac.ledger_backup` entry point.

**What exists — the scheduler.** `src/mac/ledger_backup_scheduler.py` is the
daemon that actually runs the primitive; its docstring
(src/mac/ledger_backup_scheduler.py:1) notes that before it, "nothing ever ran"
the backup command. It is a threaded daemon (`LedgerBackupScheduler`,
src/mac/ledger_backup_scheduler.py:90) started from the API lifespan
(src/mac/api.py:4121) and:

- Is **default-ON but hub-only**: `LedgerBackupConfig.from_env`
  (src/mac/ledger_backup_scheduler.py:62) enables when
  `MAC_LEDGER_BACKUP_ENABLED` is truthy (default `1`) **and**
  `MAC_CONTROL_PLANE_ROLE` is not `client`
  (src/mac/ledger_backup_scheduler.py:76). `start`
  (src/mac/ledger_backup_scheduler.py:107) is a no-op when disabled.
- Snapshots every `MAC_LEDGER_BACKUP_INTERVAL_SECONDS` (default
  `DEFAULT_INTERVAL_SECONDS = 900.0` = 15 min, min 60 s,
  src/mac/ledger_backup_scheduler.py:34) after an initial delay (default 120 s,
  src/mac/ledger_backup_scheduler.py:36), via `_loop`
  (src/mac/ledger_backup_scheduler.py:140) → `run_once`
  (src/mac/ledger_backup_scheduler.py:150).
- `run_once` **never raises** (src/mac/ledger_backup_scheduler.py:170): a backup
  failure is loud in telemetry/notifications (`_observe`,
  src/mac/ledger_backup_scheduler.py:176; `_notify_failure`,
  src/mac/ledger_backup_scheduler.py:187) but must not crash the hub.

**Posture / RPO / RTO.** **Active-passive / operator-driven.** Exactly one node
takes snapshots (the hub); the standby holds shipped copies and does not serve
until an operator promotes it. **RPO = the snapshot/ship cadence** (default 15
min, bounded by the interval and ship-hook success). **RTO** is the manual
promote procedure below — verify, install, redeploy, repoint — i.e. minutes-plus
of operator work, not automatic. The scheduler's own docstring frames this as
"the RPO-bounding half of hub availability … automated leader
election/replication is the RTO half" (src/mac/ledger_backup_scheduler.py:14).

**Test coverage.**
- `tests/test_ledger_backup.py` (5 tests): verified+restorable snapshot
  (tests/test_ledger_backup.py:27), keep-last pruning
  (tests/test_ledger_backup.py:40), ship-hook env + loud failure
  (tests/test_ledger_backup.py:57), tampered-snapshot rejection
  (tests/test_ledger_backup.py:76), missing-DB fail-closed
  (tests/test_ledger_backup.py:86).
- `tests/test_ledger_backup_scheduler.py` (7 tests): default-on-hub/off-client
  gate (tests/test_ledger_backup_scheduler.py:41), env config paths/interval
  (tests/test_ledger_backup_scheduler.py:47), verified snapshot via `run_once`
  (tests/test_ledger_backup_scheduler.py:60), ship hook env
  (tests/test_ledger_backup_scheduler.py:80), keep-last pruning
  (tests/test_ledger_backup_scheduler.py:92), loud-but-non-raising failure
  (tests/test_ledger_backup_scheduler.py:106), disabled-scheduler no-op start
  (tests/test_ledger_backup_scheduler.py:117).

## 5. Operator Promote / Fence Procedure and Invariants

**What exists.** `docs/hub-availability.md` documents the standby takeover
(docs/hub-availability.md:89). The controlling rule is stated first:
"**the old hub must be fenced before the standby serves a single write**"
(docs/hub-availability.md:91) because two live authorities silently diverge and
nothing reconciles them after the fact. The five steps:

1. **Fence the old hub** — stop its service, or make it unreachable (tailnet
   ACL / shut the node) if it cannot be stopped (docs/hub-availability.md:95).
2. **Restore the ledger on the standby** — Tier A promotes the Postgres replica;
   Tier B verifies and installs the newest shipped snapshot as `mac.db` and
   restores the evidence-blob directory alongside it (docs/hub-availability.md:98).
3. **Make the standby the hub** — deploy with `MAC_CONTROL_PLANE_ROLE=hub` and
   the same token material (docs/hub-availability.md:104).
4. **Repoint the fleet** — update `hub_url` (or, better, rebind a stable DNS
   name) and redeploy agents (docs/hub-availability.md:108).
5. **Old hub stays fenced** — when it returns, redeploy it as a spoke
   (`MAC_CONTROL_PLANE_ROLE=client`), and the store factory refuses to let a
   client own a DB (docs/hub-availability.md:114).

**Single-authority + control-plane-role invariants (verified in code).**
- The store factory `make_store_from_env` (src/mac/store.py:80) enforces exactly
  one durable authority per node: `MAC_CONTROL_PLANE_ROLE=client` **cannot own a
  database** and raises `StoreError` (src/mac/store.py:98); there is
  deliberately no home-directory fallback (src/mac/store.py:86), so a client
  cannot silently acquire a private `~/.mac/mac.db`. A node declares Postgres
  (`MAC_DATABASE_URL`) or SQLite (`MAC_DB`), never both implicitly.
- `MAC_CONTROL_PLANE_ROLE` is written by the deploy as `hub` for the hub and
  `client` for spokes (src/mac/deploy_env.py:423), and it is the same flag that
  gates the backup scheduler (section 4) and hub-only dispatch
  (src/mac/dispatch.py:2855).

**Posture / RPO / RTO.** **Operator-driven.** Fencing is intentionally manual —
the runbook's "what this deliberately does not do" section states a standby
cannot safely infer fencing from "I can't reach the hub"
(docs/hub-availability.md:122), and there is no hub-to-hub replication protocol
(docs/hub-availability.md:125).

**Test coverage.** The invariants are covered indirectly by the store-role tests
(client-cannot-own-DB) and the scheduler role gate
(`test_default_on_for_hub_off_for_client`,
tests/test_ledger_backup_scheduler.py:41). The promote *procedure itself* is a
documented runbook, not an automated code path, so it has no dedicated test —
consistent with its operator-driven nature.

## 6. Gaps / Decisions for the Active-Passive Design Child

To reach a **minimal active-passive** posture (single standby, operator-assisted
but tighter), the design child must resolve — not this audit:

1. **Fencing.** There is no programmatic fence today: step 1 is entirely manual
   (stop service / tailnet ACL, docs/hub-availability.md:95). Decide whether the
   standby gets a fencing primitive (e.g. a lease/token the old hub must hold to
   accept writes, or an external STONITH-style action) so promotion can be
   safer than "operator promises the old hub is down."
2. **Standby promotion.** For Tier B there is no automated "install newest
   verified snapshot + flip role" action — it is manual (docs/hub-availability.md:98).
   Decide whether to add a promote command that wraps
   `ledger_backup.verify` (src/mac/ledger_backup.py:158) + install +
   `MAC_CONTROL_PLANE_ROLE=hub`, keeping the fence as the human gate.
3. **Repointing.** Fleet repoint is manual `hub_url` editing/redeploy
   (docs/hub-availability.md:108). Decide the canonical mechanism (stable
   tailnet DNS rebind vs. `MAC_DEPLOY_NEW_HUB_URL` redeploy) and whether the
   standby advertises readiness.
4. **Standby freshness / liveness.** Nothing on the standby currently verifies
   that shipped snapshots are arriving on cadence or that the standby copy is
   restorable before a real failover. Decide whether the standby runs a
   continuous `--verify` and surfaces staleness (the ship hook is loud on the
   *sender* side only, src/mac/ledger_backup.py:137).
5. **Evidence-blob co-recovery.** The blob directory must be restored alongside
   the ledger (docs/hub-availability.md:103) but nothing binds the two backups
   together. Decide whether snapshot + blob-dir shipping become one atomic
   operator step.
6. **RPO target.** Default cadence is 15 min (src/mac/ledger_backup_scheduler.py:34).
   Decide the fleet's acceptable RPO and whether Tier A (Postgres, section 3) is
   the answer where the SQLite cadence is too coarse.

None of these are implemented here; this audit only establishes the verified
ground truth they build on.

## 7. Verification Performed

- Static ground-truth read of every referenced primitive with `file:line`
  citations (sections 1–5); no source or test file was modified.
- Confirmed the four test modules exist and enumerated their cases:
  `tests/test_router_service.py` (6), `tests/test_evidence_blobs.py` (18),
  `tests/test_ledger_backup.py` (5), `tests/test_ledger_backup_scheduler.py` (7).
- Confirmed `deploy/k8s/mac-api/deployment.yaml:17` sets `replicas: 2` against a
  shared `MAC_DATABASE_URL`.
- Ran the docs contract to confirm this new page does not break it (the page is
  added to `mkdocs.yml` nav under Runbooks; it uses generic role names /
  `<placeholder>` tokens only, so `tests/test_docs_no_operator_identity.py` and
  `tests/test_documentation_book.py` stay green).

## 8. Conclusion

The fleet's HA story is real and tested: inference is decoupled (active-active),
ledger bytes are bounded (supporting), the ledger has a Postgres active-active
tier and a SQLite verified-snapshot active-passive tier, and a documented
operator promote/fence procedure rests on enforced single-authority and
control-plane-role invariants. The minimal-active-passive design child should
resolve the six items in section 6 — chiefly fencing, standby promotion, and
repointing — reusing these primitives rather than designing new replication.
This audit changed no source or test logic.
