# Agent Instructions

## Skills: read these first

`skills/` holds the durable guidance that would otherwise live in someone's
memory of a previous session. Read the one that matches what you are about to
do, before you do it:

| skill | read it when |
| --- | --- |
| `skills/agentbus-context/SKILL.md` | STARTING any task — read the bus first: has this work already landed, has the trunk moved under you, is a peer in this repository |
| `skills/mac-cli/SKILL.md` | running any `mac` command — the object groups, the `admin` re-parenting, and the verbs that are not what you would guess |
| `skills/record-user-directed-work/SKILL.md` | acting on user direction, or finding a defect while working — conversation is not a record |
| `skills/setup-mac-fleet/SKILL.md` | standing up or reconfiguring fleet hosts |
| `skills/mac-agent-terminal-timeout/SKILL.md` | an agent terminal hangs or times out |
| `skills/cut-a-release/SKILL.md` | the release gates are green and the next step is tagging — the documentation pass, the pinned capabilities deck, and the traps that make a docs gate fail |
| `skills/judgement/SKILL.md` | the hub's hourly process-quality checklist — gate count, task-state pile-ups, and when to stop a task, hold an agent, or redeploy the fleet |

The CLI skill is enforced: `tests/test_mac_cli_skill.py` fails if it names a
command the parser does not have, so it cannot rot into confident nonsense.

## Working checkout: use your own worktree

**Do not edit `~/Src/mac` directly.** Multiple agents run against this
repository at the same time and the main checkout is shared. Work in an
isolated worktree:

```bash
git -C ~/Src/mac worktree add /tmp/mac-<short-task-name> -b <agent>/<task>
cd /tmp/mac-<short-task-name>
# ... work, commit, push from here ...
git -C ~/Src/mac worktree remove /tmp/mac-<short-task-name>
```

This is mandatory because the failure is silent. On 2026-07-29 two agents
sharing the main checkout collided twice in one session: one nearly swept
~1,200 lines of another's half-finished work into an unrelated commit, and one
did sweep another agent's uncommitted implementation into a commit titled
"Wire provisioning demand to bounded HGX autoscaling". Nothing errored, tests
passed, and the damage only showed up later in `git log` — by which point it
could not be corrected without rewriting pushed history.

**If you work in the main checkout anyway**, never use `git add -A`, `git add
.`, or `git commit -a`. Run `git status` first to see whose changes are
present, then stage only paths you know you touched.

Issues live in the MAC hub task ledger (`mac task`), which is the
canonical execution store. `.tickets/` is ignored local operational
state for migration/compatibility workflows only; do not rely on it as
a checked-in source of truth and do not create or commit `.tickets/`
files during normal work.

Finding and investigation write-ups belong under `docs/` (for example `docs/investigations/` or `docs/archive/field-notes/`), not the repository root; keep the repository root for genuine top-level project files.

This project does **not** use beads (`bd`) or dolt — issue tracking is the
**mac task ledger** (`mac task`), a beads-equivalent durable ledger that avoids
the beads/dolt sync problems. It is correct to say we do not use beads; it is
**not** correct to say we lack a durable task ledger — we use mac tasks. The
legacy beads read/write bridge is not the normal execution path, dolt sync is
disabled, and `.beads/` has been removed. Remaining beads commands are for
read-only detection and one-way migration. Do not run `bd`; use `mac task`.

## Fleet Host Resolution

`~/.mac/fleets.yaml` is the definitive source of truth for fleet agent targets.
When checking, refreshing, deploying to, or SSHing into a fleet agent, resolve
the agent's current `target` from that file first. Do not assume a hostname from
the agent name, local SSH aliases, known_hosts entries, old fleet backups, hub
history, or prior conversation context; those can be stale after host swaps.

For some provider-managed workers, `$HOME/.local/bin/hgx` is also a direct SSH
transport. Use it only when that executable is present and `hgx list` returns a
session relevant to the requested fleet work. Resolve duplicate or recreated
workers by immutable HGX session ID, then use `hgx ssh <session-id>` rather than
guessing from a display name. When working interactively, `hgx login` may be run
to authenticate the user before retrying `hgx list`; do not start an interactive
login flow from unattended automation. HGX access supplements the registered
target in `~/.mac/fleets.yaml`; reconcile any replacement endpoint and agent
identity back into the fleet registry instead of allowing the two views to
silently diverge.

## Quick Reference

```bash
# Read issues
mac task ready --limit 10                    # open + deps done + unclaimed + dispatchable
mac task stats                               # counts by state
mac task search <keyword>                    # title/description match
mac task throughput                          # throughput KPIs + stranded work + resource collisions

# Projects
mac project create <project> --active        # manual project, immediately dispatchable
mac project register                         # onboard the current Git checkout
mac project register <path>                  # onboard another local checkout
mac project register <git-url>[#branch]      # remote-first; defaults to #main
mac project update <project> --branch=<branch>
mac project unregister <project> --force
mac bridge repository register <name> <path> --project=<project>
mac bridge repository repos
mac project activate <project>               # clear project-level dispatch pause

# Lifecycle
mac task create "title" --project=<project> --description-file=desc.txt --metadata-file=meta.json
mac task create "title" --no-dispatch        # stage task; writes metadata.no_dispatch=true
mac task release <task_id>                   # clear no_dispatch so fleet can claim it
mac task break-glass <task_id> <agent_id> --reason="..."  # admin, single-use direct-host recovery
mac task break-glass-list <task_id>          # inspect durable recovery authorization
mac task break-glass-revoke <auth_id> --reason="..."
mac task claim <task_id> <agent_id>
mac task start <task_id> <agent_id>
mac task cancel <task_id>                    # revoke lease + abort a running executor
mac task show <task_id>                      # detail + history
mac task close <task_id> --reason="..."
mac dispatch tick --limit 10                 # ask dispatcher to assign ready work now

# Memories (cross-session knowledge)
mac memory remember <key> "<content>" --project=mac
mac memory list --project=mac
mac memory forget <key> --project=mac

# Inspect or migrate other repos that still have legacy beads state
mac task detect-beads <repo>
mac task migrate-beads <repo> --project=<name> --tickets-only
```

## Fleet Operational Learning

Treat operational outcomes as shared control inputs, not disposable log lines.
Repository-access attempts record secret-free `mac.fleet_learning.v1` memories;
reviewer routing prefers recent success and temporarily avoids a newer
authentication failure. Do not repeatedly retry the same credential pattern on
the same agent. Allow the workflow to choose a peer with a proven success, or
repair the credential and supersede the failure with a successful attempt.

Never store credential values, authenticated URLs, or raw secret-bearing
command output in memory. Store only the credential source name, redacted host,
operation, outcome, classified failure, and actionable remediation. See
`docs/fleet-operational-learning.md` for the schema and lifecycle contract.

> Pass multi-line / shell-hostile content (parens, backticks, `$VAR`, newlines) via the `--<name>-file` variants instead of inline quotes. `--<name>-file -` reads from stdin.

`no_dispatch` is a hold flag, not a lifecycle state. The held form is
`metadata.no_dispatch=true`; `mac task release` removes that key instead of
writing `false`. A task with no `no_dispatch` key is dispatchable, subject to
dependencies, worker capabilities, leases, and project dispatch pause.

To tell agents to work on a project, create or onboard the project, create
project-scoped tasks, make sure the project is active, release any staged
tasks, and let loop-mode agents claim from `mac task ready`. Use
`mac dispatch tick` for an immediate dispatcher pass, or `mac task claim` /
`mac task start` when assigning a specific agent manually.

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

1. **Detect test command** — an executable repository-owned
   `scripts/run-sanity-tests.sh` plus `test-policy.toml` is preferred and gets
   the prepared base SHA; otherwise `package.json` (`test` script) → `npm test`;
   `pyproject.toml`/`pytest.ini`/`setup.cfg`/`setup.py` → `pytest`;
   `Makefile` (`test` target) → `make test`; otherwise scan
   `README.md`/`CONTRIBUTING.md`. If none can be detected the gate does
   **not** skip — the task is routed to `needs_review` ("could not detect
   test command — manual verification required").
2. **Lint/format** (auto-fix attempted; non-blocking) — `npm run lint`
   (`-- --fix`), `eslint .` (`--fix`), `prettier --check .` (`--write`),
   `ruff check .` (`--fix`), or `flake8 .`. Lint failures are recorded in
   evidence but do not block; tests are the hard gate. In this repo the
   canonical Python lint gate is `make lint` (ruff check + format --check) /
   `make lint-fix` (those same tools, applied), both driven by the single
   shared `[tool.ruff]` configuration in `pyproject.toml` via
   `scripts/run-lint.sh`.
3. **Run tests** — execute the detected command in the repo root,
   capturing full stdout+stderr.
4. **Gate decision** — tests pass → push +
   open MR; failure → STOP (no push, no MR), transition to `needs_review`
   with full evidence.

Every coding task's `mac.worker_evidence.v1` manifest therefore always
carries numbered evidence items: `1 | Lint/Format`, `2 | Tests`, and on
success `3 | Push` + `4 | MR`; on test failure item 3 becomes
`Test Failures` (full output, failing test names, suggested fix).

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
5. **Clean up** — clear stashes and verify the hub-owned branch reconciler with
   `mac repo refs status`. It retires eligible managed task refs automatically;
   use `mac repo refs audit --repo .` for diagnosis and manual executable prune
   only for refs the same lifecycle policy marks eligible
6. **Verify** all changes committed AND pushed
7. **Hand off** context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing — that leaves work stranded locally
- NEVER say "ready to push when you are" — YOU must push
- If push fails, resolve and retry until it succeeds
