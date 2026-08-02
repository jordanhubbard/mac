# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

## Issue Tracking

Issues live in the MAC hub task ledger (`mac task`), which is the **canonical**
execution store. `.tickets/<id>.md` (wedow/ticket-compatible: YAML frontmatter +
body with optional Design / Acceptance Criteria / Notes sections) is a **local,
gitignored** human/IDE mirror auto-emitted on task create/close — it is
operational state, NOT product content, so it is **not** checked into the repo
(a generic product tree must not carry one operator's task history). A fresh
clone has no `.tickets/`; populate it locally from the ledger as needed.

This project does **not** use beads (`bd`) or its dolt/DoltHub backend — those
caused the sync problems we moved away from. Issue tracking is the **mac task
ledger** (`mac task`), a beads-equivalent durable ledger that avoids those
problems. It is correct to say we do not use beads; it is **not** correct to say
we do not use a durable task ledger — we use mac tasks. The legacy beads bridge
is removed (no runtime flag gates it — the code paths are gone), `bd dolt
push/pull` is disabled, and `.beads/` directories were removed. Do not run `bd`;
use `mac task`.

### Quick reference

```bash
# Discover work
mac task show <task_id>             # one issue (the ledger is authoritative)
ls .tickets 2>/dev/null             # browse the LOCAL mirror, if populated (gitignored)
mac task ready --limit 10           # ledger view: open + no unfinished deps + unclaimed
mac task stats                      # counts by state
mac task search <keyword>           # title/description match
mac task list --state=open          # state-filtered list

# Lifecycle
mac task create "title" --description-file=desc.txt --metadata-file=meta.json
mac task claim <task_id> <agent_id>
mac task show <task_id>             # detail + history
mac task close <task_id> --reason="..."

# Memories (cross-session knowledge)
mac memory remember <key> "<content>" --project=mac
mac memory list --project=mac
mac memory forget <key> --project=mac

# Migration / inspection of repos that still have legacy beads state
mac task detect-beads <repo>
mac task migrate-beads <repo> --project=<name> --tickets-only
```

> For multi-line / shell-hostile values (parens, backticks, `$VAR`, newlines), use the `--<name>-file` variants of `--description` / `--metadata` to avoid shell-quoting hazards. `--<name>-file -` reads from stdin.

### Rules

- Read issues via `mac task show <id>` (the ledger is authoritative). A local `.tickets/<id>.md` mirror, if present, is a convenience copy — do NOT commit it (`.tickets/` is gitignored).
- Create / claim / close issues via `mac task` — do NOT run `bd`, do NOT use TodoWrite / TaskCreate / markdown TODO lists.
- Use `mac memory remember` for persistent project knowledge — do NOT use MEMORY.md files.
- `mac task create` / `close` auto-emit the local `.tickets/<id>.md` mirror; it stays out of git. Never check `.tickets/` into the repo — it is operational state, not product content.

### How `mac task` finds the hub

The `mac` CLI is hub-aware. Resolution order (highest priority first):

1. `--db <dsn>` (or `$MAC_DB`) → a direct PostgreSQL control-plane
   authority. Prints a redacted stderr banner naming the DSN, so silent
   direct writes are impossible. `$MAC_DB` on its own does not grant direct
   access: on a deployed hub it is server configuration, so aiming `--db` at
   the live hub authority additionally requires `--local-authority` against
   a stopped hub.
2. `--hub-url URL` (+ optional `--token`) → HTTP to that hub.
3. `$MAC_API_URL` / `$MAC_URL` / `$MAC_HUB_URL` → HTTP to that hub.
4. `--fleet <name>` (or `$MAC_FLEET`) → reads `~/.mac/fleets.yaml`
   for the named fleet's `hub_url` and selects `MAC_API_TOKEN__<FLEET>`.
5. Nothing configured → **error** with help text. No silent fallback.

`mac` is the only documented CLI. The legacy `hgmac` binary is gone —
all of its functionality lives under `mac` now.

The commands that once needed `--db` no longer do. `memory list/forget` and
`observability prune` are served over the hub via `/memory/remembered` and
`/observability/prune`, as are `task ready/search/stats`
(parity-ready-http-01). `--db` is now for hub maintenance, standalone
development, tests, and migration; it is not a repository ticket store or an
offline hub replica, and it never synchronizes with a hub.

## Working Checkout: use your own worktree

**Do not edit `~/Src/mac` directly.** Several agents run against this repository
concurrently, and the main checkout is shared. Create an isolated worktree and
work there:

```bash
git -C ~/Src/mac worktree add /tmp/mac-<short-task-name> -b <agent>/<task>
cd /tmp/mac-<short-task-name>
```

Commit and push from your worktree, then remove it when the work has landed:

```bash
git -C ~/Src/mac worktree remove /tmp/mac-<short-task-name>
```

**Why this is mandatory, not advisory.** Two agents sharing the main checkout
on 2026-07-29 collided twice in one session:

- One agent's `git add -A` was moments from sweeping ~1,200 lines of another's
  half-finished work into an unrelated commit; it was avoided only by staging
  explicit paths.
- A second agent's `git commit -a` did sweep up another agent's uncommitted
  retry implementation, landing it inside a commit titled "Wire provisioning
  demand to bounded HGX autoscaling". The code was fine; the history is now
  wrong and `git log -S` is the only way to find where that change came from.
  It is unfixable after the fact without rewriting pushed history.

Both failures are silent — nothing errors, the tests pass, and the damage is
only visible later in `git log`. A worktree removes the shared mutable state
that makes them possible.

**If you must work in the main checkout anyway**, never use `git add -A`,
`git add .`, or `git commit -a`. Stage explicit paths you know you changed, and
run `git status` first to see whose work is present.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File follow-up issues** for remaining work via `mac task create`
2. **Run quality gates** (if code changed) — `scripts/run-contract-tests.sh`
3. **Update issue status** via `mac task close`
4. **PUSH TO REMOTE** — MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** — clear stashes, prune remote branches
6. **Verify** all changes committed AND pushed
7. **Hand off** context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing — that leaves work stranded locally
- NEVER say "ready to push when you are" — YOU must push
- If push fails, resolve and retry until it succeeds


## Build & Test

The test suite runs against **PostgreSQL**, not SQLite, because that is what the
fleet runs. Start one and export the DSN before running tests:

```bash
eval "$(scripts/start-test-postgres.sh)"   # finds a local server or starts a container
uv run --extra dev pytest -q -n 8          # full suite (~10 min)
scripts/run-contract-tests.sh              # the gate CI runs
```

`start-test-postgres.sh` sets `max_locks_per_transaction=1024`. Each test gets
its own schema and applying the full DDL takes one lock per object in a single
transaction, so at Postgres' default of 64 a parallel run fails with "out of
shared memory" rather than anything that looks like a test failure.

Without `MAC_TEST_PG_URL` the suite fails fast with instructions rather than
skipping, because a suite that silently covers nothing is worse than a red one.

## Architecture Overview

_Add a brief overview of your project architecture_

## Conventions & Patterns

_Add your project-specific conventions here_
