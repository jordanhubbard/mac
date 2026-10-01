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

Hosts are provisioned by hand from the "Provision a new host" checklist in
[Updating the fleet with fleet-update](../operations/fleet-update.md), then
moved to a commit with `scripts/fleet-update`. Resolve targets from
`~/.mac/fleets.yaml` and `~/.mac/fleet-hosts`.

```bash
test -x "$DOCS_ROOT/scripts/fleet-update"
python3 "$DOCS_ROOT/scripts/image-publication-identity.py" --help >/dev/null
test -f "$DOCS_ROOT/docs/operations/fleet-update.md"
test -f "$DOCS_ROOT/docs/fleet-registry-schema.md"
```

Onboarding is complete only when registry identity, route, credentials, the
host's reported source commit, runtime attestation and a role-specific
acceptance task all pass. Registration alone is not readiness.
