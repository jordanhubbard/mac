---
schema: mac.docs.chapter.v1
chapter: 12
title: Operating the Fleet
audiences: [operator]
timeout_seconds: 60
---

# Operating the Fleet

Production operation begins with bounded, structured observations. Task history
explains lifecycle decisions; events join audit records across resources;
observability stores metrics and logs; diagnostics evaluate control-plane
invariants.

Run read-only checks before choosing a repair. A stale worker, unreachable hub,
expired lease, missing credential, and publication conflict can all present as
"work is not moving" but require different actions.

```bash
mac --db "$DOCS_DB" init
mac --db "$DOCS_DB" diagnostics
mac --db "$DOCS_DB" task stats --all
mac --db "$DOCS_DB" events list --limit 20
mac --db "$DOCS_DB" observability list --limit 20
mac repo refs status --help >/dev/null
```

Backups must be restored and verified, not merely copied. Hub deployments use
scheduled ledger snapshots and off-host shipment; Postgres deployments rely on
the database service's durable backup and recovery contract. Soul and memory
journals protect agent identity separately from the task ledger.

Break-glass authorization is exact, single-use, task/agent scoped, audited, and
revocable. It is a recovery mechanism after ordinary dispatch is proven
unavailable, never a general shortcut around the scheduler.
