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
- `bd ready --json` — canonical ready queue (`docs/integration-authority-contract.md:57`, `docs/production-deployment.md:569`).
- `bd memories --json` — persistent memories export (`beads_migrator.py:228`).
- `bd update <id> --claim`, `bd close <id>` — claim/close writeback (`integration-authority-contract.md:87-90`).
- `bd dolt push/pull` — cross-machine sync of the embedded Dolt DB + `.beads/issues.jsonl` mirror (`docs/linear-bridge-spec.md`, `docs/metadata-sync-assessment.md:27`).
- Project tagging tied to the repo a bead was filed from (`docs/hermes-integration.md:169`).

## `mac task` surface today (`src/mac/cli.py`)

`create, list, show, ready, claim, close, search, stats, start, submit-review,
evidence, detect-beads, migrate-beads, detect-ticketing, convert-ticketing`.
`ready`, `search`, `stats` require `--db` (direct SQLite) and are **refused in
hub mode** (`dispatch.py` `_RemoteStore._refuse`).

`.tickets/<id>.md` mirror frontmatter (`beads_migrator._render_ticket`):
`id, status, deps, links, created, type, priority` (+ optional `assignee`,
`external-ref`, `parent`, `mac-task-id`) and body sections Design / Acceptance
Criteria / Notes / Close Reason — a faithful superset of the bead fields.

## Parity table

| `bd` capability | `mac task` equivalent | Status |
| --- | --- | --- |
| Project from the repo you file in | `create` defaults `--project` from cwd (git top-level / cwd basename) | **Present** (PR #124) |
| Scope reads to current project | `list`/`ready`/`search` default to cwd project; `--all` / `--project` widen | **Present** (this PR) |
| Ready queue (`bd ready --json`) | `mac task ready` (open + deps satisfied + unclaimed) | **Partial — `--db` only, not over the hub** |
| Keyword search | `mac task search` | **Partial — `--db` only** |
| Stats / counts | `mac task stats` | **Partial — `--db` only** |
| Dependencies / blockers | `--dependencies` on create; `.tickets` `deps:`/`links:`/`parent:` | Present |
| Status lifecycle | TaskState transitions; `close --cancelled` | Present |
| Priority | `--priority` (numeric) | Present |
| Body sections (design/acceptance/notes) | `.tickets` body + metadata | Present |
| Assignment (assignee/owner) | metadata + `.tickets` `assignee` | Present |
| External refs | metadata + `.tickets` `external-ref` | Present |
| Memories | `mac memory remember/list/forget` | Present (`list/forget` are `--db`-only) |
| `.tickets` markdown mirror **auto-emitted** on create/close | only emitted by `migrate-beads`; not on `mac task create/close` | **Missing — roadmap** (`CLAUDE.md`) |
| `bd dolt push/pull` cross-machine sync | — | Removed by design (mac hub is the store) |
| Two-way `bd update/close` writeback | — | Removed by design (`MAC_BEADS_BRIDGE_ENABLED` gated off) |

## Remaining gaps (actionable)

1. **`ready` / `search` / `stats` are SQLite-only.** `bd ready --json` worked
   against the canonical store from anywhere; the `mac task` equivalents refuse
   in hub mode, so an operator pointed at the rocky hub can't run them. This is
   the biggest live parity gap. → serve them over HTTP. (`parity-ready-http-01`)
2. **No `.tickets` auto-emit.** `bd` kept the JSONL mirror in sync; `mac task
   create/close` doesn't write/update `.tickets/<id>.md`, so the git-trackable
   mirror drifts from the ledger. → emit on create/close. (`parity-tickets-autoemit-01`)

## Deliberately not parity (recorded so they aren't re-opened as "gaps")

- Dolt push/pull and two-way `bd` writeback are intentionally gone: the mac hub
  is the single authoritative store (see `metadata-sync-assessment.md`,
  `CLAUDE.md`). `bd` must not be run.

## Verdict

Core issue-tracking parity is **achieved** (fields, dependencies, lifecycle,
priority, sections, project-from-cwd for both create and read). The two real
gaps are operational, not data-model: the read commands don't work over the
hub, and the `.tickets` mirror isn't auto-emitted. Both are tracked as
follow-ups; everything else either matches `bd` or was dropped on purpose.
