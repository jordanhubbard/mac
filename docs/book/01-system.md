---
schema: mac.docs.chapter.v1
chapter: 1
title: MAC as a System
audiences: [user, operator, integrator, contributor]
timeout_seconds: 30
---

# MAC as a System

MAC is the durable control plane for a fleet of AI agents. The hub owns the
official record of projects, tasks, leases, evidence, review, publication,
deployments, and operational events. Machines provide resources; agents provide
capabilities; Hermes supplies conversational identity and skills. None of those
components replaces the task ledger.

The central rule is simple: work is not complete because an agent says it is.
It is complete when the ledger contains the required evidence, review decision,
and—where a repository is involved—proof that the accepted commit reached the
canonical remote branch.

Start by discovering the installed surface. Help commands are safe and require
neither a database nor network access.

```bash
mac --help >/dev/null
mac project --help >/dev/null
mac task --help >/dev/null
```

The important nouns will recur throughout the book:

- A **project** groups related work and may bind a repository contract.
- A **task** is a durable unit of intent and lifecycle state.
- A **lease** grants one agent a bounded right to mutate a task.
- **Evidence** is structured proof, not free-form confidence.
- **Review** is independent evaluation of a specific attempt.
- **Publication** proves that accepted work became canonical.

The next chapter creates a private, disposable authority so these ideas can be
experienced without touching a production fleet.
