# Integration Authority Contract

mac integrates state from systems that already have their own authority:
Beads, git hosting, Hermes, Slack, deployment services, and future project
trackers. Each integration must make the authority boundary explicit so the
fleet never guesses which copy of state should win.

## Contract

Every integration adapter must define:

- **Canonical authority:** the system and API whose state is authoritative for
  decisions that create, claim, close, deploy, or notify.
- **Derived copies:** exports, caches, dashboard projections, local checkouts,
  and temporary files that are convenient but not authoritative.
- **Read policy:** which source is used during normal operation and which
  fallback is allowed when the canonical API is unavailable.
- **Write policy:** which system receives state changes and how derived copies
  are refreshed after writes.
- **Reconciliation policy:** what drift is detected, what is auto-resolved,
  and what becomes an operator-visible finding.
- **Evidence:** observations and findings written to mac so operators can see
  why a decision was made.

## Durable Ledger

mac stores two generic integration records:

- `integration_observations`: timestamped snapshots from an adapter. These are
  useful for answering "what did mac see?".
- `integration_findings`: idempotent open/resolved findings for drift or broken
  integration contracts. These are useful for answering "what needs attention?".

Findings are de-duplicated by `(source_kind, source_id, finding_type,
fingerprint)`. If the same problem is observed again, `last_seen_at` is
refreshed. If a resolved problem reappears, it is reopened. When an adapter no
longer observes a problem, it should resolve the stale finding instead of
leaving dashboard noise behind.

Operators can inspect the ledger through:

```bash
mac --db ~/.mac/mac.db integrations findings
mac --db ~/.mac/mac.db integrations observations --source-kind beads_repository
```

The HTTP API exposes the same state at:

- `GET /integrations/findings`
- `GET /integrations/observations`

The dashboard includes recent integration findings in the Observability view.

## Beads Authority

For registered Beads repositories, the canonical task source is the Beads
database as exposed by `bd ready --json`. The tracked export at
`.beads/issues.jsonl` is a derived copy. mac uses the JSONL export only when the
canonical Beads CLI path is unavailable, which preserves lightweight local test
fixtures without letting a stale export override the database.

mac does not poll the live runtime checkout directly. The registered repository
path is the canonical project path for workers and task-owned git worktrees, but
the bridge refreshes a disposable managed checkout under
`MAC_BEADS_BRIDGE_ROOT` before polling. With `MAC_BEADS_AUTO_PULL=1`, that
checkout is cloned or fast-forwarded from its upstream branch. Dirty tracked
files in the disposable checkout are reset because the bridge owns it; dirty
files in the registered runtime checkout are recorded as source state and, when
needed, become an idempotent remediation task for the agent that owns that
environment.

When `bd ready --json` succeeds, the Beads bridge also parses the tracked JSONL
export and compares the ready issue IDs. If the JSONL export contains ready
open issues that the canonical DB does not expose, mac opens a
`beads.export_drift.jsonl_only_ready` integration finding and does not import
those JSONL-only issues. That is intentional: importing them would make the
export authoritative and recreate the split-brain failure.

When the canonical DB later exposes the same ready IDs, the bridge imports them
normally and resolves the drift finding as no longer observed.

## Beads Write Policy

mac writes back only derived state needed for human visibility and tracker
convergence:

- Claim sync: when a Beads-backed mac task is claimed, mac runs `bd update
  <id> --claim` in the managed bridge checkout.
- Close sync: when publication completes the mac task, mac runs `bd close <id>`
  with a reason that points back to the mac task and publication.
- Failure sync: failed mapped tasks append a note and either reopen for a
  bounded retry or remain failed when retries are exhausted.
- Human ledger: key workflow milestones are appended as Beads comments prefixed
  `mac-ledger v1`.

Those writes never make Beads authoritative for mac execution state. The mac
task history, evidence, reviews, publications, command audit, observability,
and notifications remain the execution ledger. Beads comments are a compact
operator-facing mirror so someone reading the issue can see imports, claims,
state gates, evidence, review events, publication, retries, and exhaustion
without opening the mac database. Lease renewals intentionally stay inside mac
to avoid filling issue logs with liveness noise.
