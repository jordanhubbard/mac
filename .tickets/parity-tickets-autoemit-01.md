---
id: parity-tickets-autoemit-01
status: closed
deps: []
links: [parity-ready-http-01]
created: 2026-06-09T00:00:00Z
type: feature
priority: 2
audit: mac-task-bd-parity
discovered_via: parity_audit
resolution: done
---

## Done (2026-06-10)

`src/mac/tickets_mirror.py` renders a wedow-compatible ticket from a mac task
dict (reusing `beads_migrator._render_ticket` so the format never drifts) and
writes `.tickets/<id>.md` into the repo's existing `.tickets/` dir (git root,
else cwd; never creates one). `mac task create` and `mac task close` call it
(close passes the reason as the Close Reason section); idempotent, and opt-out
via `--no-ticket` / `MAC_NO_TICKET_MIRROR`.

# Auto-emit the `.tickets/<id>.md` mirror on task create/close

## Why this exists

`bd` kept its git-distributed mirror (`.beads/issues.jsonl`) in sync with every
change. `mac task` only writes `.tickets/<id>.md` during `migrate-beads`, so the
git-trackable mirror drifts from the ledger as soon as you `mac task create` /
`close` (CLAUDE.md notes auto-emit is "on the roadmap"). The renderer already
exists (`src/mac/beads_migrator._render_ticket`).

## Acceptance criteria

- `mac task create` and `mac task close` write/update `.tickets/<id>.md` using
  the existing ticket renderer (frontmatter: id/status/deps/links/created/type/
  priority + body sections), when run inside a repo with a `.tickets/` dir.
- Idempotent: re-emitting an unchanged task is a no-op; status/close-reason
  updates rewrite the file in place.
- Opt-out via a flag/env for hub-only flows that don't want local files.

## Notes

Pairs with project-from-cwd (PR #124) so a task filed from a repo both lands in
that project *and* leaves a reviewable `.tickets` entry in the same checkout.
Related: [[parity-ready-http-01]].
