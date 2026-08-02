---
schema: mac.docs.chapter.v1
chapter: 13
title: Deployment Topologies
audiences: [operator]
timeout_seconds: 60
---

# Deployment Topologies

MAC supports three production shapes. A single host can run one API process
against a local PostgreSQL server under systemd or launchd. A containerized
single instance keeps the same boundary. A multi-replica Kubernetes API shares
one PostgreSQL authority so replicas agree transactionally.

PostgreSQL is the only supported control-plane authority; there is no
embedded-database topology.

Spokes are API clients. They must not retain a private `MAC_DB` or
`MAC_DATABASE_URL`.

```bash
test -f "$DOCS_ROOT/deploy/systemd/mac.service"
test -f "$DOCS_ROOT/Dockerfile"
test -f "$DOCS_ROOT/deploy/k8s/mac-api/deployment.yaml"
test -f "$DOCS_ROOT/deploy/k8s/mac-api/service.yaml"
test -f "$DOCS_ROOT/deploy/k8s/mac-runner/deployment.yaml"
grep -q 'MAC_DATABASE_URL' "$DOCS_ROOT/deploy/k8s/mac-api/deployment.yaml"
bash "$DOCS_ROOT/deploy/deploy-mac-fleet.sh" --help >/dev/null
```

Choose the smallest topology that satisfies availability and write-load needs.
Record database ownership, secret management, hub reachability, supervisor,
backup authority, and rollback procedure before deployment.

The production runbook contains concrete systemd, container, VPN, SSH-forward,
and Kubernetes procedures. Treat its environment table as a reference; do not
copy every optional variable into every node.
