# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

## Issue Tracking

Issues live as one markdown file per ticket under `.tickets/<id>.md`
(wedow/ticket-compatible format: YAML frontmatter + body with optional
Design / Acceptance Criteria / Notes sections). The MAC hub task ledger
(`mac task`) is the canonical execution store; `.tickets/<id>.md` is
the git-trackable human/IDE mirror that travels with the repo.

The legacy beads (`bd`) integration is shut off: `bd dolt push/pull`
is disabled, the beads bridge is gated off by default
(`MAC_BEADS_BRIDGE_ENABLED`), and `.beads/` directories were removed
from this repo. Do not run `bd`.

### Quick reference

```bash
# Discover work
ls .tickets                         # browse all tickets
cat .tickets/mac-y7ha.md            # one issue (the file IS authoritative for text)
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

- Read issues from `.tickets/<id>.md` whenever possible; the file IS authoritative for the issue's text.
- Create / claim / close issues via `mac task` — do NOT run `bd`, do NOT use TodoWrite / TaskCreate / markdown TODO lists.
- Use `mac memory remember` for persistent project knowledge — do NOT use MEMORY.md files.
- After `mac task create` / `mac task close`, refresh the .tickets mirror if you want a markdown copy of newly-created tasks (auto-emit is on the roadmap).

### How `mac task` finds the hub

The `mac` CLI is hub-aware. Resolution order (highest priority first):

1. `--db <path>` (or `$MAC_DB`) → local SQLite. Prints a stderr banner
   showing the absolute path, so silent local writes are impossible.
2. `--hub-url URL` (+ optional `--token`) → HTTP to that hub.
3. `$MAC_API_URL` / `$MAC_URL` / `$MAC_HUB_URL` → HTTP to that hub.
4. `--fleet <name>` (or `$MAC_FLEET`) → reads `~/.mac/fleets.yaml`
   for the named fleet's `hub_url` and selects `MAC_API_TOKEN__<FLEET>`.
5. Nothing configured → **error** with help text. No silent fallback.

`mac` is the only documented CLI. The legacy `hgmac` binary is gone —
all of its functionality lives under `mac` now.

A small set of commands still requires `--db` because they reach into
SQLite directly: `memory list/forget` and `observability prune`. Running
these in hub mode emits a clear error. (`task ready/search/stats` are now
served over the hub — parity-ready-http-01.)

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

_Add your build and test commands here_

```bash
# Example:
# npm install
# npm test
```

## Architecture Overview

_Add a brief overview of your project architecture_

## Conventions & Patterns

_Add your project-specific conventions here_
