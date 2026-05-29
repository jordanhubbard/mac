# Agent Instructions

Issues live as one markdown file per ticket under `.tickets/<id>.md`
(wedow/ticket-compatible YAML frontmatter + markdown body). The MAC
hub task ledger (`mac task`) is the canonical execution store;
`.tickets/` is the git-trackable mirror.

The legacy beads (`bd`) integration is shut off — dolt sync is
disabled, the beads bridge is gated off by default
(`MAC_BEADS_BRIDGE_ENABLED`), and `.beads/` has been removed from this
repo. Do not run `bd`.

## Quick Reference

```bash
# Read issues
ls .tickets/                                 # all issues
cat .tickets/mac-y7ha.md                     # one issue (the file IS authoritative for text)
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
