---
schema: mac.docs.chapter.v1
chapter: 7
title: Repository Contracts
audiences: [operator, integrator, contributor]
timeout_seconds: 60
---

# Repository Contracts

A repository contract tells MAC how to bootstrap, test, and validate work in a
project. It records supported platforms, required commands, the canonical
remote, and the evidence expected from an executor. The contract belongs in
`.mac/project.yaml`; a CodeGraph index is generated local state and is never the
task ledger.

The MAC checkout contains its own production contract. Registering it in a
disposable authority demonstrates the binding between project and hub-visible
checkout.

```bash
mac --db "$DOCS_DB" init
test -f "$DOCS_ROOT/.mac/project.yaml"
mac --db "$DOCS_DB" project create mac --active
mac --db "$DOCS_DB" bridge repository register mac "$DOCS_ROOT" \
  --project mac --source docs-book
mac --db "$DOCS_DB" bridge repository repos
```

For a new repository, use `mac project onboard` first. Its contract-authoring
task should inspect the real build rather than guessing from filenames. Register
the checkout only after the contract exists.

Code executors must run the repository's mandatory tests and a CodeGraph audit
before pushing. A missing test command is a review condition, not permission to
skip verification.
