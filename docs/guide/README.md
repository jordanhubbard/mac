# mac documentation

Read in order if you are new; jump if you are not.

| | |
|---|---|
| **[1. System Architecture](01-architecture.md)** | What the pieces are and how work flows. Mostly diagrams. |
| **[2. Getting Started](02-getting-started.md)** | Stand up a fleet, run a task, diagnose one that does not move. |
| **[3. Advanced Concepts](03-advanced.md)** | Leases, evidence, review, publication, the merge queue — and the known gaps. |
| **[4. The UI](04-ui.md)** | The read-only console and the mutating Fleet IDE. |
| **[5. Developer Guide](05-developer-guide.md)** | How to hack on mac: tests, gates, schema, deploys. |
| **[Contributing](https://github.com/jordanhubbard/mac/blob/main/CONTRIBUTING.md)** | Filing issues and PRs that are actually tested. |

## What these pages promise

They are written from the code. Every state, transition, table, route and
environment variable named here was read out of the source or the live route
table at the time of writing, and anything partial, unwired or broken is
labelled as such rather than omitted.

That constraint exists because the alternative was demonstrated: this
repository's README described a dashboard source file, its compiled output and
a vendored dependency for some time after all three were deleted. Documentation
that quietly diverges is worse than none, because it is trusted.

If you find a page describing something that does not exist, that is a bug —
file it.

## Reference material

The pages above are the guide. These are the reference and the record:

- `docs/reference/cli.md`, `docs/reference/openapi.md` — **generated**; do not
  hand-edit
- `docs/env-config-reference.md` — **generated** registry of every
  `MAC_*` variable
- `docs/adr/` — architecture decision records, including what was tried and
  removed
- `docs/archive/field-notes/` — incident write-ups and investigations
- `skills/` — task-shaped instructions for coding agents, gated by tests that
  check every command and flag they name against the real parser
