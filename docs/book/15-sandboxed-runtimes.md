---
schema: mac.docs.chapter.v1
chapter: 15
title: Sandboxed Agent Runtimes
audiences: [operator, integrator, contributor]
timeout_seconds: 60
---

# Sandboxed Agent Runtimes

OpenShell constrains an agent's filesystem, process, network, and credential
boundaries. The sandbox is part of the execution contract: installing the
binary without a policy, verified runtime image, coding CLI, and projected
identity does not make a worker ready.

Report executors and code executors have different permissions. A read-only
report path cannot silently become a repository mutation path. Host execution
requires an exact break-glass authorization when the ordinary sandbox route is
unavailable.

```bash
mac admin openshell --help >/dev/null
mac admin openshell policy --help >/dev/null
mac admin runtime --help >/dev/null
test -f "$DOCS_ROOT/deploy/openshell/bootstrap-openshell.sh"
test -f "$DOCS_ROOT/deploy/openshell/mac-hermes.Containerfile"
test -f "$DOCS_ROOT/docs/openshell-sandbox.md"
```

The default runtime includes CodeGraph and the permitted coding frontends. The
frontend selected for one task and the model resolved by the in-MAC router are
separate facts and should be reported separately.

Runtime qualification proves the exact image digest on every required
architecture. Mutable package repositories mean the input digest is a reviewed
recipe identity, not a claim of bit-for-bit reproducibility across arbitrary
future rebuilds.
