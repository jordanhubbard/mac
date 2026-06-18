# Agent Instructions

Issues live in the MAC hub task ledger (`mac task`), which is the
canonical execution store. `.tickets/` is ignored local operational
state for migration/compatibility workflows only; do not rely on it as
a checked-in source of truth and do not create or commit `.tickets/`
files during normal work.

The legacy beads (`bd`) integration is shut off — dolt sync is
disabled, the beads bridge is gated off by default
(`MAC_BEADS_BRIDGE_ENABLED`), and `.beads/` has been removed from this
repo. Do not run `bd`.

## Quick Reference

```bash
# Read issues
mac task ready --limit 10                    # ledger view: open + no unfinished deps + unclaimed
mac task stats                               # counts by state
mac task search <keyword>                    # title/description match

# Lifecycle
mac task create "title" --description-file=desc.txt --metadata-file=meta.json
mac task claim <task_id> <agent_id>
mac task show <task_id>                      # detail + history
mac task close <task_id> --reason="..."

# Memories (cross-session knowledge)
mac memory remember <key> "<content>" --project=mac
mac memory list --project=mac
mac memory forget <key> --project=mac

# Inspect or migrate other repos that still have legacy beads state
mac task detect-beads <repo>
mac task migrate-beads <repo> --project=<name> --tickets-only
```

> Pass multi-line / shell-hostile content (parens, backticks, `$VAR`, newlines) via the `--<name>-file` variants instead of inline quotes. `--<name>-file -` reads from stdin.

## Mandatory Pre-Push Test Gate (all code executor tasks)

Every code-executor worker (`mac-worker-python-coder-opencode` and any
other code executor) enforces a **mandatory pre-push verification gate**
before it pushes a branch or opens a Merge Request. The gate is
implemented at the worker execution layer in
`deploy/codex-runner/mac-task-executor-opencode-build` so it **cannot be
bypassed** by task-level instructions or per-project config, and it
applies uniformly to **every** repo (`mac`, `ivan-plugin`,
`hermes-agent-custom`, and any future repo).

Sequence before any `git push` / MR:

1. **Detect test command** — `package.json` (`test` script) → `npm test`;
   `pyproject.toml`/`pytest.ini`/`setup.cfg`/`setup.py` → `pytest`;
   `Makefile` (`test` target) → `make test`; otherwise scan
   `README.md`/`CONTRIBUTING.md`. If none can be detected the gate does
   **not** skip — the task is routed to `needs_review` ("could not detect
   test command — manual verification required").
2. **Lint/format** (auto-fix attempted; non-blocking) — `npm run lint`
   (`-- --fix`), `eslint .` (`--fix`), `prettier --check .` (`--write`),
   `ruff check .` (`--fix`), or `flake8 .`. Lint failures are recorded in
   evidence but do not block; tests are the hard gate.
3. **Run tests** — execute the detected command in the repo root,
   capturing full stdout+stderr.
4. **Gate decision** — exit 0 → push + open MR; non-zero → STOP (no push,
   no MR), transition to `needs_review` with full evidence.

Every coding task's `mac.worker_evidence.v1` manifest therefore always
carries numbered evidence items: `1 | Lint/Format`, `2 | Tests`, and on
success `3 | Push` + `4 | MR`; on test failure item 3 becomes
`Test Failures` (full output, failing test names, suggested fix) and no
push/MR items are present.

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File follow-up issues** via `mac task create` for anything that needs follow-up
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
