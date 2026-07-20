---
schema: mac.docs.chapter.v1
chapter: 2
title: Install and Start Locally
audiences: [user, operator, contributor]
timeout_seconds: 60
---

# Install and Start Locally

Production clients talk to a hub. For learning and tests, MAC also supports an
explicit standalone SQLite authority selected with `--db`. It is deliberately
not an offline replica: nothing written here synchronizes to another hub.

The documentation lab supplies `DOCS_DB` as a disposable path. Outside the
lab, choose your own temporary path and keep passing it explicitly.

```bash
mac --db "$DOCS_DB" init
mac --db "$DOCS_DB" diagnostics
mac --db "$DOCS_DB" task stats --all
```

A successful diagnostic report ends with `ok: True`. The database is now a
complete single-process authority suitable for the foundational chapters.

For repository development, `python3 scripts/bootstrap-project.py` creates the
project environment and `make test` runs the mandatory contract gate. Those are
checkout-maintenance operations, while `mac --db ...` drives the product.

Never point direct SQLite mode at a running hub database. Production authority
is reached through a scoped client profile and the hub API; stopped-hub
maintenance requires the explicit `--local-authority` recovery mode.
