# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Issue Tracking

This project is migrating off **beads** (`bd`). Issues now live as one
markdown file per ticket in `.tickets/<id>.md` (wedow/ticket compatible
format: YAML frontmatter + body with optional Design / Acceptance
Criteria / Notes sections). The MAC hub task ledger remains the
canonical execution store; `.tickets/<id>.md` is the git-trackable
human/IDE mirror that travels with the repo.

### Quick reference (transitional — beads still works locally)

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
bd export -o .beads/issues.jsonl   # Refresh JSONL before re-migrating

mac task migrate-beads <repo> --project=<name>          # full import: JSONL + memories + MAC tasks + .tickets/
mac task migrate-beads <repo> --project=<name> --tickets-only   # .tickets/ files only (no DB writes)
mac task detect-beads <repo>                            # read-only inspection of .beads/ state
```

### Rules

- Read issues from `.tickets/<id>.md` whenever possible; the file IS authoritative for the issue's text.
- `bd` remains available for issue lifecycle until the replacement CLI lands — DO NOT use TodoWrite / TaskCreate / markdown TODO lists.
- `bd dolt push/pull` is disabled (see services.py:_sync_beads_database). Issues travel via git, not dolt.
- After `bd create` / `bd close` / etc., refresh the JSONL + ticket mirror before pushing:
  ```bash
  bd export -o .beads/issues.jsonl
  mac task migrate-beads . --project=mac --tickets-only
  ```
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files (the bd memory store will be migrated alongside issues).

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
   Do NOT run `bd dolt push`/`bd dolt pull` — dolt sync is disabled (see services.py:_sync_beads_database). Beads JSONL files under `.beads/` are tracked via git; pushing the repo is sufficient.
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->


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
