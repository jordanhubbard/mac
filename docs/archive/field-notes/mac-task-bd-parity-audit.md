!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# `mac task` ↔ `bd` (beads) functional-parity audit

- Date: 2026-06-09
- Scope: does `mac task` match the `bd` workflow it replaced? (the stated goal
  of the `mac task` subcommand). Grounded in this repo's beads migration/bridge
  code + docs — not external memory of `bd`.

## What `bd` provided (evidence in-repo)

Data model — from `src/mac/beads_migrator.py` (`_create_task_from_bead`,
`_render_ticket`): `id`, `status` (open/closed/blocked/deferred/in_progress),
`title`, `description`, `issue_type`, `priority` (numeric), `assignee`, `owner`,
`dependencies` (typed: `blocks` → deps, `related` → links, `parent-child` →
parent), `design`, `acceptance_criteria`, `notes`, `close_reason`,
`external_ref`, timestamps.

`bd` commands referenced in-repo (only these appear — no broader CLI is
documented here):
- `bd ready --json` — historical canonical ready queue for Beads.
- `bd memories --json` — persistent memories export (`beads_migrator.py:228`).
- `bd update <id> --claim`, `bd close <id>` — historical claim/close writeback.
- `bd dolt push/pull` — cross-machine sync of the embedded Dolt DB + `.beads/issues.jsonl` mirror (`docs/linear-bridge-spec.md`, `docs/metadata-sync-assessment.md:27`).
- Project tagging tied to the repo a bead was filed from (`docs/hermes-integration.md:169`).

## `mac task` surface today (`src/mac/cli.py`)

`create, list, show, ready, claim, close, reopen, force-complete, search, stats,
start, release, submit-review, evidence, detect-beads, migrate-beads,
detect-ticketing, convert-ticketing`. `ready`, `search`, and `stats` work in hub
mode through `/tasks/ready`, `/tasks/search`, and `/tasks/stats`, with the
direct SQLite path still available under `--db`.

`.tickets/<id>.md` mirror frontmatter (`beads_migrator._render_ticket`):
`id, status, deps, links, created, type, priority` (+ optional `assignee`,
`external-ref`, `parent`, `mac-task-id`) and body sections Design / Acceptance
Criteria / Notes / Close Reason — a faithful superset of the bead fields. The
mirror is optional local compatibility output: `tickets_mirror.py` writes it
only when a `.tickets/` directory already exists, and this repo ignores
`.tickets/` instead of checking it into git.

## Parity table

| `bd` capability | `mac task` equivalent | Status |
| --- | --- | --- |
| Project from the repo you file in | `create` defaults `--project` from cwd (git top-level / cwd basename) | **Present** (PR #124) |
| Scope reads to current project | `list`/`ready`/`search` default to cwd project; `--all` / `--project` widen | **Present** (this PR) |
| Ready queue (`bd ready --json`) | `mac task ready` (open + deps satisfied + unclaimed) | **Present** — served over the hub (parity-ready-http-01) |
| Keyword search | `mac task search` | **Present** — served over the hub |
| Stats / counts | `mac task stats` | **Present** — served over the hub |
| Dependencies / blockers | `--dependencies` on create; `.tickets` `deps:`/`links:`/`parent:` | Present |
| Status lifecycle | TaskState transitions; `close --cancelled` | Present |
| Priority | `--priority` (numeric) | Present |
| Body sections (design/acceptance/notes) | `.tickets` body + metadata | Present |
| Assignment (assignee/owner) | metadata + `.tickets` `assignee` | Present |
| External refs | metadata + `.tickets` `external-ref` | Present |
| Memories | `mac admin memory remember/list/forget` | Present in local and hub modes |
| `.tickets` markdown mirror **auto-emitted when a local `.tickets/` directory exists** | `mac task create`/`close` write `.tickets/<id>.md` (`tickets_mirror.py`) and otherwise no-op | **Present** as local compatibility output (parity-tickets-autoemit-01) |
| `bd dolt push/pull` cross-machine sync | — | Removed by design (mac hub is the store) |
| Two-way `bd update/close` writeback | — | Removed by design; legacy Beads state is one-way migration only |

## Remaining gaps (actionable)

Both gaps are now closed:

1. ~~**`ready` / `search` / `stats` are SQLite-only.**~~ **DONE** —
   `GET /tasks/ready|search|stats` now serve these from the hub; the CLI uses
   them in hub mode and keeps the direct-SQL path under `--db`
   (`parity-ready-http-01`).
2. ~~**No `.tickets` auto-emit.**~~ **DONE** — `mac task create`/`close` now
   write/update `.tickets/<id>.md` via `src/mac/tickets_mirror.py` when a local
   `.tickets/` directory already exists (reusing the migrator's renderer),
   idempotent and opt-out via `--no-ticket` / `MAC_NO_TICKET_MIRROR`
   (`parity-tickets-autoemit-01`). It deliberately does not create `.tickets/`
   or make that mirror authoritative.

## Deliberately not parity (recorded so they aren't re-opened as "gaps")

- Dolt push/pull and two-way `bd` writeback are intentionally gone: the mac hub
  is the single authoritative store (see `metadata-sync-assessment.md`,
  `CLAUDE.md`). `bd` must not be run.

## Verdict

Core issue-tracking parity is **achieved** (fields, dependencies, lifecycle,
priority, sections, project-from-cwd for both create and read). The two
operational gaps that remained — read commands not working over the hub, and the
`.tickets` mirror not being auto-emitted — are now **both closed**
(`parity-ready-http-01`, `parity-tickets-autoemit-01`). Everything else either
matches `bd` or was dropped on purpose.
