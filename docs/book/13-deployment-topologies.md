---
schema: mac.docs.chapter.v1
chapter: 13
title: Deployment Topologies
audiences: [operator]
timeout_seconds: 60
---

# Deployment Topologies

MAC supports two production shapes. A single host can run one API process
against a local PostgreSQL server under systemd or launchd. A containerized
single instance keeps the same boundary.

PostgreSQL is the only supported control-plane authority; there is no
embedded-database topology.

Spokes are API clients. They must not retain a private `MAC_DB` or
`MAC_DATABASE_URL`.

```bash
test -f "$DOCS_ROOT/deploy/systemd/mac.service"
test -f "$DOCS_ROOT/Dockerfile"
test -x "$DOCS_ROOT/scripts/fleet-update"
```

Choose the smallest topology that satisfies availability and write-load needs.
Record database ownership, secret management, hub reachability, supervisor,
backup authority, and rollback procedure before deployment.

The production runbook contains concrete systemd, container, VPN, and
SSH-forward procedures. Treat its environment table as a reference; do not
copy every optional variable into every node.
