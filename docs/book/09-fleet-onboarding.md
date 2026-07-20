---
schema: mac.docs.chapter.v1
chapter: 9
title: Heterogeneous Fleet Onboarding
audiences: [operator]
timeout_seconds: 60
---

# Heterogeneous Fleet Onboarding

A fleet can contain macOS hosts, Linux PCs, ARM systems, and init-less pods.
Uniformity comes from contracts and evidence rather than identical operating
systems. The registry records targets and roles; deployment discovers the
supervisor, architecture, filesystem, container runtime, and available coding
CLIs on each selected node.

Before any fleet mutation, resolve targets from `~/.mac/fleets.yaml`. The
prepublication qualifier performs a read-only cohort check and writes an
owner-private receipt for the exact candidate.

```bash
python3 "$DOCS_ROOT/scripts/prepublish-fleet-qualification.py" --help >/dev/null
bash "$DOCS_ROOT/deploy/deploy-mac-fleet.sh" --help >/dev/null
python3 "$DOCS_ROOT/scripts/image-publication-identity.py" --help >/dev/null
test -f "$DOCS_ROOT/docs/fleet-node-onboarding-checklist.md"
test -f "$DOCS_ROOT/docs/fleet-registry-schema.md"
```

Onboarding is complete only when registry identity, route, prerequisites,
credential projection, source convergence, runtime attestation, anonymous image
readback, and a role-specific acceptance task all pass. Registration alone is
not readiness.

Read-only preflight aggregates every node failure so operators can repair the
whole cohort. Mutation phases are bounded and fail fast to protect rollback.
