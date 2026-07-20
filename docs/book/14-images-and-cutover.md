---
schema: mac.docs.chapter.v1
chapter: 14
title: Qualified Images and Synchronized Cutover
audiences: [operator, contributor]
timeout_seconds: 60
---

# Qualified Images and Synchronized Cutover

Controller source identity and runtime-image identity are related but different.
A deterministic runtime input digest identifies the reviewed build recipe.
Publication binds it to one immutable multi-architecture OCI digest, verifies
anonymous pull and platform manifests, and records GitHub provenance. Deployment
records that runtime digest alongside the controller source commit.

The deploy controller stages one verified bundle per node, reuses it through
arm and apply, journals parent-owned phase intent, and executes bounded node
phases in parallel. Preflight aggregates failures; mutating phases stop on the
first unsafe condition and retain rollback evidence.

```bash
python3 "$DOCS_ROOT/scripts/image-publication-identity.py" --help >/dev/null
python3 "$DOCS_ROOT/scripts/prepublish-fleet-qualification.py" --help >/dev/null
python3 "$DOCS_ROOT/scripts/verify-runtime-publication.py" --help >/dev/null
bash "$DOCS_ROOT/deploy/deploy-mac-fleet.sh" --help | \
  grep -- '--preflight-only' >/dev/null
test -f "$DOCS_ROOT/docs/fleet-cutover-transaction-protocol.md"
```

A synchronized cutover does not require identical hardware or equal worker
speed. It requires a fixed cohort, candidate, plan version, epoch, deadline,
barrier, and acceptance contract. Nodes can prepare independently, but none may
publish readiness for a different candidate or cross the activation barrier
without the whole required cohort.
