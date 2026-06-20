# Integration Authority Contract

mac integrates state from systems that already have their own authority:
git hosting, Hermes, Slack, deployment services, legacy import sources, and
future project trackers. Each integration must make the authority boundary
explicit so the fleet never guesses which copy of state should win.

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
mac --db ~/.mac/mac.db integrations observations
```

The HTTP API exposes the same state at:

- `GET /integrations/findings`
- `GET /integrations/observations`

The dashboard includes recent integration findings in the Observability view.

## Legacy Beads Import Authority

Beads is no longer a read/write task authority for normal MAC operation. The MAC
task ledger is the canonical execution store, and operators should use
`mac task` for task lifecycle. Legacy Beads repositories are handled through
read-only detection and one-way migration commands:

```bash
mac task detect-beads <repo>
mac task migrate-beads <repo> --project <project>
```

For migration, `.beads/issues.jsonl` is treated as a legacy source snapshot.
Imported tasks keep provenance in task metadata, but follow normal MAC task
lifecycle afterwards. MAC does not write claim, close, failure, or human-ledger
events back into Beads during current operation.

`.tickets/<id>.md` files are optional ignored local compatibility output. The
emitter writes them only when a `.tickets/` directory already exists, and they
are never the source of truth for task dispatch, claim, review, or completion.
